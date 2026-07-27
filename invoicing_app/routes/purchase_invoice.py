from flask import Blueprint, render_template, redirect, url_for, flash, request, jsonify
from flask_login import login_required, current_user
from datetime import datetime
from decimal import Decimal
from inventory_app.extensions import db
from inventory_app.models.purchase_invoice import InvPurchaseInvoice, InvPurchaseInvoiceItem
from inventory_app.models.purchase_order import InvPurchaseOrder
from inventory_app.models.additional_charge import AdditionalCharge
from inventory_app.models.supplier import InvSupplier
from inventory_app.models.product import InvProduct
from inventory_app.models.stock_movement import InvStockMovement
from shared.models.vouchers import ConsumptionItem as ConsItem, ScrapItem, StockAdjustmentItem as AdjItem
from shared.ledger_utils import post_journal_entry, reverse_journal_entry, posting_account, party_account
from shared.models.ledger import ChartOfAccount
from shared.models.company_settings import CompanyInfo, ReportSettings
from shared.models.invoice_settings import InvoiceSettings
from shared.models.invoice_template import InvoiceTemplate, render_invoice_template, build_totals_table
from shared.permissions import deny_json, deny_page
from shared.costing import record_in, reverse_voucher_stock

inv_pinv_bp = Blueprint("inv_purchase_invoice", __name__,
                         url_prefix="/inventory/purchase-invoice")


def _charge_columns():
    return {c.key for c in AdditionalCharge.__table__.columns}


def build_charge(doc_id, chg):
    """Persist one additional charge for a purchase invoice.

    The data contract carries treatment(bill|absorb|expense) and independent
    st/wht/extra tax-base switches. This writes to the shared model's dedicated
    columns when they exist, and otherwise falls back to the columns the model
    does have (treatment -> tax_base, st_taxable -> taxable) so the feature
    works on the current schema and upgrades cleanly once the columns are added.
    """
    cols = _charge_columns()
    treatment = chg.get("treatment", "bill")
    if treatment not in ("bill", "absorb", "expense"):
        treatment = "bill"
    # Only a supplier-billed charge can sit in a tax base: absorbed carriage is
    # already inside item cost and an expense-only charge is not on the
    # supplier's invoice at all, so neither can be taxed on it.
    billed = treatment == "bill"
    st = billed and bool(chg.get("st_taxable", True))
    wht = billed and bool(chg.get("wht_taxable", False))
    extra = bool(chg.get("extra_taxable", False))
    kwargs = dict(
        doc_type="PI", doc_id=doc_id,
        charge_account_id=int(chg["charge_account_id"]),
        description=chg.get("description", ""),
        amount=float(chg.get("amount", 0)),
        scope=chg.get("scope", "general"),
    )
    kwargs["treatment" if "treatment" in cols else "tax_base"] = treatment
    kwargs["st_taxable" if "st_taxable" in cols else "taxable"] = st
    if "wht_taxable" in cols:
        kwargs["wht_taxable"] = wht
    if "extra_taxable" in cols:
        kwargs["extra_taxable"] = extra
    return AdditionalCharge(**{k: v for k, v in kwargs.items() if k in cols
                               or k in ("doc_type", "doc_id")})


def charge_buckets(charges):
    """Split saved/incoming charge dicts into absorb / bill / expense totals and
    keep the billed & expense rows (with their charge ledgers) for GL posting."""
    absorb_total = bill_total = expense_total = 0.0
    billed, expensed = [], []
    for c in charges:
        amt = float(c.get("amount", 0) or 0)
        if amt <= 0 or not c.get("charge_account_id"):
            continue
        t = c.get("treatment", "bill")
        if t == "absorb":
            absorb_total += amt
        elif t == "expense":
            expense_total += amt
            expensed.append(c)
        else:
            bill_total += amt
            billed.append(c)
    return absorb_total, bill_total, expense_total, billed, expensed


def next_voucher():
    """The document number. A purchase invoice has exactly one.

    Was two generators off the same counter — VCH-… into voucher_number and
    PINV-… into invoice_number — so the document carried two numbers for itself.
    Now one, prefixed PI to match the SI / SO / PO document codes used
    everywhere else; VCH belongs to an actual voucher, which this is not.

    Both columns are still written with the same value — see the note on
    invoices.next_voucher() for why.
    """
    last = InvPurchaseInvoice.query.order_by(InvPurchaseInvoice.id.desc()).first()
    n = (last.id + 1) if last else 1
    return f"PI-{datetime.utcnow():%Y%m}-{n:04d}"


@inv_pinv_bp.route("/", defaults={"id": None})
@inv_pinv_bp.route("/<int:id>")
@login_required
def invoice_form(id):
    invoice = InvPurchaseInvoice.query.get(id) if id else None
    suppliers = InvSupplier.query.filter_by(is_active=True).order_by(InvSupplier.name).all()
    products = InvProduct.query.filter_by(is_active=True).order_by(InvProduct.name).all()
    invoice_items = []
    invoice_charges = []
    if invoice:
        for it in invoice.items.all():
            invoice_items.append({
                "product_id": it.product_id,
                "product": {"sku": it.product.sku if it.product else ""},
                # §6.2 — the "By weight" split needs the line's unit weight
                # client-side; a reopened invoice must carry it too.
                "weight": (it.product.weight or 0) if it.product else 0,
                "description": it.description,
                "quantity": it.quantity,
                "unit": it.unit,
                "unit_price": it.unit_price,
                "discount_pct": it.discount_pct,
                "discount_amount": it.discount_amount,
                "commission": it.commission,
                "freight": it.freight,
                "loading_unloading": it.loading_unloading,
                "sales_tax_pct": it.sales_tax_pct,
                "total_before_discount": it.total_before_discount,
                "total_after_discount": it.total_after_discount,
            })
        # Additional charges (polymorphic, doc_type='PI'). The purchase side
        # carries a per-charge treatment (bill / absorb=carriage inward /
        # expense) and independent sales-tax & withholding tax-base switches.
        # These map onto whatever columns the shared model exposes; getattr
        # fallbacks keep this working before/after the model gains the
        # dedicated columns (treatment stored in tax_base, st in taxable).
        for chg in invoice.charges_list:
            treatment = getattr(chg, "treatment", None)
            if treatment not in ("bill", "absorb", "expense"):
                treatment = chg.tax_base if chg.tax_base in ("bill", "absorb", "expense") else "bill"
            invoice_charges.append({
                "id": chg.id,
                "charge_account_id": chg.charge_account_id,
                "account_code": chg.charge_account.code if chg.charge_account else "",
                "account_name": chg.charge_account.name if chg.charge_account else "",
                # The modal's account picker renders from _display, so reopening
                # a saved invoice has to hand it back the same "code — name".
                "_display": (f"{chg.charge_account.code} — {chg.charge_account.name}"
                             if chg.charge_account else ""),
                "description": chg.description,
                "amount": chg.amount,
                "scope": chg.scope,
                "treatment": treatment,
                "st_taxable": bool(getattr(chg, "st_taxable", chg.taxable)),
                "wht_taxable": bool(getattr(chg, "wht_taxable", False)),
                "extra_taxable": bool(getattr(chg, "extra_taxable", False)),
            })
    rs = ReportSettings.get()
    company = CompanyInfo.get()

    rendered_template = None
    invoice_template_obj = None
    if invoice:
        tid = rs.purchase_template_id
        if tid:
            invoice_template_obj = InvoiceTemplate.query.get(tid)
        if not invoice_template_obj:
            invoice_template_obj = InvoiceTemplate.get_default("purchase")

    if invoice_template_obj and invoice:
        topts = invoice_template_obj.options

        if invoice.discount_mode == "individual":
            show_disc_col = topts.get("discount_display") == "per_line"
        else:
            show_disc_col = False

        if invoice.tax_mode == "individual":
            show_tax_col = topts.get("tax_display") == "per_line"
        else:
            show_tax_col = False

        if invoice.expenses_mode == "individual":
            show_chg_col = topts.get("charges_display") == "per_line"
        else:
            show_chg_col = False

        # Auto-size font when extra columns are shown, so the table fits A4
        extra = (1 if show_disc_col else 0) + (1 if show_tax_col else 0) + (1 if show_chg_col else 0)
        fs = max(9, 12 - extra)
        tds = f"padding:6px 8px;border:1px solid #e2e8f0;font-size:{fs}px;"
        tdc = tds + "text-align:center;"
        tdr = tds + "text-align:right;"

        tot_qty = 0
        tot_disc_amt = 0.0
        tot_excl = 0.0
        tot_comm = 0.0
        tot_freight = 0.0
        tot_ld = 0.0
        tot_tax_amt = 0.0
        tot_incl = 0.0
        tot_line = 0.0

        items_rows = ""
        for i, it in enumerate(invoice.items.all(), start=1):
            da = it.discount_amount or 0
            tp = it.sales_tax_pct or 0
            line_total = it.total_before_discount or 0
            amt_excl = line_total - da
            tax_amt = amt_excl * tp / 100
            amt_incl = amt_excl + tax_amt

            tot_qty += it.quantity or 0
            tot_disc_amt += da
            tot_excl += amt_excl
            tot_comm += it.commission or 0
            tot_freight += it.freight or 0
            tot_ld += it.loading_unloading or 0
            tot_tax_amt += tax_amt
            tot_incl += amt_incl
            tot_line += line_total

            cells = (
                f"<td style='{tdc}'>{i}</td>"
                f"<td style='{tds}'>{it.product.sku if it.product else ''}</td>"
                f"<td style='{tds}'>{it.description or ''}</td>"
                f"<td style='{tdc}'>{it.quantity}</td>"
                f"<td style='{tdc}'>{it.unit or ''}</td>"
                f"<td style='{tdr}'>{it.unit_price:.2f}</td>")
            if show_disc_col:
                cells += f"<td style='{tdr}'>{it.discount_pct:.1f}%</td>"
                cells += f"<td style='{tdr}'>{da:.2f}</td>"
            cells += f"<td style='{tdr}'>{amt_excl:.2f}</td>"
            if show_tax_col:
                cells += f"<td style='{tdr}'>{tp:.1f}%</td>"
                cells += f"<td style='{tdr}'>{tax_amt:.2f}</td>"
            if show_chg_col:
                cells += f"<td style='{tdr}'>{it.commission:.2f}</td>"
                cells += f"<td style='{tdr}'>{it.freight:.2f}</td>"
                cells += f"<td style='{tdr}'>{it.loading_unloading:.2f}</td>"
            cells += f"<td style='{tdr}'>{tax_amt:.2f}</td>"
            cells += f"<td style='{tdr}'>{amt_incl:.2f}</td>"
            cells += f"<td style='{tdr}'>{line_total:.2f}</td>"
            items_rows += "<tr>" + cells + "</tr>"

        tds_b = f"padding:6px 8px;border:1px solid #e2e8f0;font-weight:700;background:#f1f5f9;font-size:{fs}px;"
        tdr_b = tds_b + "text-align:right;"
        foot = (
            f"<td style='{tds_b};text-align:center;'></td>"
            f"<td style='{tds_b}'></td>"
            f"<td style='{tds_b}'>Total</td>"
            f"<td style='{tds_b};text-align:center;'>{tot_qty}</td>"
            f"<td style='{tds_b}'></td>"
            f"<td style='{tdr_b}'></td>")
        if show_disc_col:
            foot += f"<td style='{tdr_b}'></td>"
            foot += f"<td style='{tdr_b}'>{tot_disc_amt:.2f}</td>"
        foot += f"<td style='{tdr_b}'>{tot_excl:.2f}</td>"
        if show_tax_col:
            foot += f"<td style='{tdr_b}'></td>"
            foot += f"<td style='{tdr_b}'>{tot_tax_amt:.2f}</td>"
        if show_chg_col:
            foot += f"<td style='{tdr_b}'>{tot_comm:.2f}</td>"
            foot += f"<td style='{tdr_b}'>{tot_freight:.2f}</td>"
            foot += f"<td style='{tdr_b}'>{tot_ld:.2f}</td>"
        foot += f"<td style='{tdr_b}'>{tot_tax_amt:.2f}</td>"
        foot += f"<td style='{tdr_b}'>{tot_incl:.2f}</td>"
        foot += f"<td style='{tdr_b}'>{tot_line:.2f}</td>"

        hds = "padding:8px;border:1px solid #1e293b;"
        hdr = hds + "text-align:right;"
        hdl = hds + "text-align:left;"
        hdc = hds + "text-align:center;"

        head = (
            f"<th style='{hdc}'>#</th>"
            f"<th style='{hdl}'>SKU</th>"
            f"<th style='{hdl}'>Description</th>"
            f"<th style='{hdc}'>Qty</th>"
            f"<th style='{hdc}'>Unit</th>"
            f"<th style='{hdr}'>Per Unit Price</th>")
        if show_disc_col:
            head += f"<th style='{hdr}'>Discount %</th>"
            head += f"<th style='{hdr}'>Discount allowed</th>"
        head += f"<th style='{hdr}'>Amount Excl. of Sales Tax</th>"
        if show_tax_col:
            head += f"<th style='{hdr}'>Sales Tax %</th>"
            head += f"<th style='{hdr}'>Sales Tax Amount per Unit</th>"
        if show_chg_col:
            head += f"<th style='{hdr}'>Commission</th>"
            head += f"<th style='{hdr}'>Freight</th>"
            head += f"<th style='{hdr}'>Ld/Unld</th>"
        head += f"<th style='{hdr}'>Total Sales Tax</th>"
        head += f"<th style='{hdr}'>Amount Incl. of Sales Tax</th>"
        head += f"<th style='{hdr}'>Total</th>"

        items_table = (
            '<table style="width:100%;border-collapse:collapse;table-layout:fixed;font-size:12px;">'
            '<thead><tr style="background:#1e293b;color:#fff;">' + head +
            '</tr></thead><tbody>' + items_rows +
            '<tr>' + foot + '</tr></tbody></table>'
        )

        party = invoice.supplier
        ctx = {
            "company_logo": f'<img src="{company.logo_url}" style="max-height:60px;" alt="Logo">' if company.logo_url else "",
            "company_name": company.company_name or "",
            "company_address": company.address or "",
            "company_city": company.city or "",
            "company_phone": company.phone or "",
            "company_email": company.email or "",
            "company_tax_id": company.tax_id or "",
            "invoice_no": invoice.voucher_number or "",
            "invoice_date": invoice.invoice_date.strftime("%d-%b-%Y") if invoice.invoice_date else "",
            "due_date": invoice.due_date.strftime("%d-%b-%Y") if invoice.due_date else "",
            "status": ("Approved" if invoice.approved_at else "Unapproved"),
            "party_name": party.name if party else "",
            "party_address": party.address if party and party.address else "",
            "party_city": party.city if party and party.city else "",
            "party_phone": party.phone if party and party.phone else "",
            "party_email": party.email if party and party.email else "",
            "party_tax_id": party.tax_id if party and party.tax_id else "",
            "items_table": items_table,
            "totals_table": "",
            "subtotal": f"{invoice.subtotal:.2f}" if invoice.subtotal else "0.00",
            "discount": f"{invoice.total_discount:.2f}" if invoice.total_discount else "0.00",
            "tax": f"{invoice.total_tax:.2f}" if invoice.total_tax else "0.00",
            "commission": "0.00",
            "freight": "0.00",
            "loading_unloading": f"{invoice.total_expenses:.2f}" if invoice.total_expenses else "0.00",
            "withholding_tax": f"{invoice.total_withholding_tax:.2f}" if invoice.total_withholding_tax else "0.00",
            "grand_total": "0.00",
            "delivery_charges": "0.00",
            "installation_charges": "0.00",
            "notes": invoice.notes or "",
        }
        # Build totals table — respect invoice mode per section
        display_opts = dict(topts)
        if invoice.discount_mode == "individual":
            if topts.get("discount_display") == "per_line":
                display_opts["show_discount"] = False
        if invoice.tax_mode == "individual":
            if topts.get("tax_display") == "per_line":
                display_opts["show_tax"] = False
        if invoice.expenses_mode == "individual":
            if topts.get("charges_display") == "per_line":
                display_opts["show_commission"] = False
                display_opts["show_freight"] = False
                display_opts["show_loading"] = False
                display_opts["show_withholding"] = False
        ctx["totals_table"] = build_totals_table("purchase", display_opts,
                                                  invoice_template_obj.accent_color or "#0f766e")
        net = invoice.net_payable or (
            (invoice.subtotal or 0) - (invoice.total_discount or 0)
            + (invoice.total_tax or 0) - (invoice.total_withholding_tax or 0))
        ctx["grand_total"] = f"{net:.2f}"
        rendered_template = render_invoice_template(invoice_template_obj.body_html, ctx)

    return render_template("purchase_invoice/form_inv.html",
                           invoice=invoice,
                           invoice_items=invoice_items,
                           invoice_charges=invoice_charges,
                           suppliers=suppliers,
                           party_mode=rs.party_mode("purchase"),
                           invoice_settings=InvoiceSettings.get(),
                           invoice_template_text=rs.template_text("purchase"),
                           rendered_template=rendered_template,
                           products=products,
                           now=datetime.utcnow())


def validate_approve(data):
    errors = []
    if not data.get("supplier_id"):
        errors.append("Supplier is required")
    items = data.get("items", [])
    if not items:
        errors.append("At least one item is required")
    else:
        for i, row in enumerate(items):
            if not row.get("product_id"):
                errors.append(f"Row {i+1}: Product is required")
            qty = float(row.get("quantity", 0))
            if qty <= 0:
                errors.append(f"Row {i+1}: Quantity must be greater than 0")
    return errors


@inv_pinv_bp.route("/save", methods=["POST"])
@login_required
def save_invoice():
    data = request.get_json(force=True)
    inv_id = data.get("id")
    action = data.get("action", "save")

    denied = deny_json("purchase_invoices",
                       "approve" if action == "approve" else ("edit" if inv_id else "create"))
    if denied:
        return denied

    if inv_id:
        inv = InvPurchaseInvoice.query.get_or_404(inv_id)
        if inv.status == "approved":
            return jsonify({"ok": False, "error": "Cannot modify approved invoice"}), 400
    else:
        number = data.get("invoice_number")
        if not number or number == "(auto)":
            number = next_voucher()
        inv = InvPurchaseInvoice(
            voucher_number=number,
            invoice_number=number,
            created_by=current_user.id,
        )
        db.session.add(inv)

    if action == "approve":
        validation_errors = validate_approve(data)
        if validation_errors:
            return jsonify({"ok": False, "error": "; ".join(validation_errors)}), 400
        # §4.4: billing past a purchase order's balance is blocked at save,
        # beyond the administrator's tolerance (§11.2).
        from types import SimpleNamespace
        from shared.order_linkage import check_over_invoicing
        over = check_over_invoicing("purchase", [
            SimpleNamespace(source_order_item_id=r.get("source_order_item_id"),
                            quantity=float(r.get("quantity", 0) or 0))
            for r in data.get("items", [])
        ])
        if over:
            return jsonify({"ok": False,
                            "error": "Over-invoicing blocked — " + "; ".join(over)}), 400

    inv.supplier_id = data.get("supplier_id")
    inv.party_account_id = data.get("party_account_id") or None
    inv.driver_name = data.get("driver_name", "")
    inv.driver_contact = data.get("driver_contact", "")
    inv.vehicle_number = data.get("vehicle_number", "")
    inv.gate_pass = data.get("gate_pass", "")
    inv.discount_mode = data.get("discount_mode", "general")
    inv.expenses_mode = data.get("expenses_mode", "general")
    inv.tax_mode = data.get("tax_mode", "general")

    inv.global_discount_pct = float(data.get("global_discount_pct", 0))
    inv.global_discount_value = float(data.get("global_discount_value", 0))
    inv.global_commission = float(data.get("global_commission", 0))
    inv.global_freight = float(data.get("global_freight", 0))
    inv.global_loading = float(data.get("global_loading", 0))
    inv.global_sales_tax_pct = float(data.get("global_sales_tax_pct", 0))
    inv.global_withholding_tax_pct = float(data.get("global_withholding_tax_pct", 0))
    # A purchase invoice never carries further tax — it is a sales-side levy
    # (§8), so the switch is pinned off no matter what the form sends.
    inv.further_tax_pct = 0
    inv.apply_further_tax = False
    inv.apply_withholding_tax = bool(data.get("apply_withholding_tax", False))
    inv.notes = data.get("notes", "")
    inv.subtotal = float(data.get("subtotal", 0))
    inv.total_discount = float(data.get("total_discount", 0))
    inv.total_expenses = float(data.get("total_expenses", 0))
    inv.total_tax = float(data.get("total_tax", 0))
    inv.total_further_tax = float(data.get("total_further_tax", 0))
    inv.total_withholding_tax = float(data.get("total_withholding_tax", 0))
    inv.net_payable = float(data.get("net_payable", 0))
    inv.total_amount = float(data.get("total_amount", 0))
    inv.paid_amount = float(data.get("paid_amount", 0))
    inv.payment_status = data.get("payment_status", "unpaid")
    inv.purchase_order_id = data.get("purchase_order_id") or None

    if action == "approve":
        inv.status = "approved"
        inv.approved_by = current_user.id
        inv.approved_at = datetime.utcnow()
    elif inv.status == "new":
        inv.status = "unapproved"

    db.session.flush()

    # Additional charges by treatment. Absorb = carriage inward (capitalised
    # into inventory cost); bill = supplier's own charge line (adds to AP,
    # debits its own ledger); expense = we bear it (Dr expense / Cr accrued).
    charges_data = data.get("charges", [])
    absorb_total, bill_total, expense_total, billed_charges, expense_charges = \
        charge_buckets(charges_data)
    # Discount reduces inventory cost. In combined mode it is a document-level
    # figure not yet in any line, so it is spread out of inventory; in per-item
    # mode it already sits in each line's total_after_discount.
    global_discount = float(inv.total_discount or 0) if inv.discount_mode == "general" else 0.0

    InvPurchaseInvoiceItem.query.filter_by(invoice_id=inv.id).delete()
    cost_rows = []
    for row in data.get("items", []):
        item = InvPurchaseInvoiceItem(
            invoice_id=inv.id,
            product_id=row.get("product_id"),
            description=row.get("description", ""),
            quantity=float(row.get("quantity", 1)),
            unit=row.get("unit", "pcs"),
            unit_price=float(row.get("unit_price", 0)),
            source_order_id=row.get("source_order_id") or None,
            source_order_item_id=row.get("source_order_item_id") or None,
            source_order_number=row.get("source_order_number", "") or "",
            discount_pct=float(row.get("discount_pct", 0)),
            discount_amount=float(row.get("discount_amount", 0)),
            commission=float(row.get("commission", 0)),
            freight=float(row.get("freight", 0)),
            loading_unloading=float(row.get("loading_unloading", 0)),
            sales_tax_pct=float(row.get("sales_tax_pct", 0)),
            withholding_tax_pct=float(row.get("withholding_tax_pct", 0)),
            total_before_discount=float(row.get("total_before_discount", 0)),
            total_after_discount=float(row.get("total_after_discount", 0)),
            comments=row.get("comments", ""),
        )
        db.session.add(item)
        cost_rows.append(item)

    # Per-item absorbed expenses (carriage inward columns) already capitalise.
    per_item_absorb = sum(float(it.commission or 0) + float(it.freight or 0)
                          + float(it.loading_unloading or 0) for it in cost_rows)
    base_values = [float(it.total_after_discount or 0) for it in cost_rows]
    total_base = sum(base_values)

    # Re-derive the tax figures rather than trusting the browser's copy — a
    # posted journal must not depend on a client-supplied number. Absorbed
    # carriage is inside the goods value, so it is in every base; withholding
    # is never compounded onto the input tax.
    st_charges = sum(float(c.get("amount", 0) or 0) for c in billed_charges
                     if c.get("st_taxable", c.get("taxable", True)))
    wht_charges = sum(float(c.get("amount", 0) or 0) for c in billed_charges
                      if c.get("wht_taxable"))
    effective_goods = round(total_base + per_item_absorb + absorb_total
                            - global_discount, 2)
    input_tax_base = round(effective_goods + st_charges, 2)
    if (inv.tax_mode or "general") == "general":
        inv.total_tax = round(input_tax_base * float(inv.global_sales_tax_pct or 0) / 100, 2)
    inv.total_withholding_tax = round(
        (effective_goods + wht_charges) * float(inv.global_withholding_tax_pct or 0) / 100, 2
    ) if inv.apply_withholding_tax else 0.0
    inv.total_further_tax = 0.0
    inv.net_payable = round(effective_goods + bill_total + float(inv.total_tax or 0)
                            - float(inv.total_withholding_tax or 0), 2)
    inv.total_amount = inv.net_payable

    if action == "approve":
        # Spread the document-level absorbed carriage (less the combined
        # discount) across lines pro-rata by value, so each purchase layer's
        # value sums exactly to the Inventory debit (costing invariant).
        spread = round(absorb_total - global_discount, 2)
        spread_left = spread
        n = len(cost_rows)
        denom = total_base or 1.0
        for idx, item in enumerate(cost_rows):
            if not item.product_id:
                continue
            prod = InvProduct.query.get(item.product_id)
            if not prod:
                continue
            db.session.add(InvStockMovement(
                product_id=item.product_id, type="purchase_in",
                quantity=item.quantity,
                reference_type="purchase_invoice",
                reference_id=inv.id,
                notes=f"Approved invoice {inv.invoice_number}",
                created_by=current_user.id,
            ))
            if idx == n - 1:
                share = spread_left
            else:
                share = round(spread * base_values[idx] / denom, 2)
                spread_left = round(spread_left - share, 2)
            # Landed cost = line value after its discount + per-item carriage +
            # its pro-rata slice of document-level absorbed carriage/discount.
            landed_total = (float(item.total_after_discount or 0)
                            + float(item.commission or 0)
                            + float(item.freight or 0)
                            + float(item.loading_unloading or 0)
                            + share)
            qty_f = float(item.quantity or 0)
            if qty_f > 0:
                record_in(item.product_id, "PI", inv.id, inv.voucher_number,
                          qty=qty_f, unit_cost=landed_total / qty_f,
                          notes=f"Purchase {inv.invoice_number}",
                          created_by=current_user.id)

    # §4.4: approving bills the source order lines — invoiced quantities rise
    # and each order moves Open -> Partially invoiced -> Fully invoiced.
    if action == "approve":
        from shared.order_linkage import apply_writeback
        apply_writeback("purchase",
                        InvPurchaseInvoiceItem.query.filter_by(invoice_id=inv.id).all())

    # Save additional charges
    AdditionalCharge.query.filter_by(doc_type="PI", doc_id=inv.id).delete()
    for chg in charges_data:
        if float(chg.get("amount", 0) or 0) > 0 and chg.get("charge_account_id"):
            db.session.add(build_charge(inv.id, chg))

    if action == "approve":
        inv_acc = posting_account("inventory")
        # Payable posts to the supplier's own subledger account (or an
        # explicit override), so the supplier's ledger carries the balance.
        ap_acc = party_account("supplier", inv.supplier_id,
                               inv.supplier.name if inv.supplier else None,
                               inv.party_account_id)
        if inv_acc and ap_acc:
            # §12.3 mirror of the sales invoice:
            #   Dr Inventory        effectiveGoods - discount + per-item carriage
            #   Dr Input Sales Tax  recoverable input tax
            #   Dr each billed charge ledger (supplier's own cost lines)
            #   Dr each expense charge ledger (we bear it)
            #     Cr Accounts Payable   amount payable to supplier (net of WHT)
            #     Cr WHT Payable        withheld from the supplier
            #     Cr Accrued Expenses   expense-only charges
            inventory_dr = round(total_base + per_item_absorb
                                 + (absorb_total - global_discount), 2)
            input_tax = round(float(inv.total_tax or 0), 2)
            wht = round(float(inv.total_withholding_tax or 0), 2)
            invoice_gross = round(inventory_dr + bill_total + input_tax, 2)
            amount_payable = round(invoice_gross - wht, 2)

            lines = [
                {"account_id": inv_acc.id, "debit": inventory_dr, "credit": 0,
                 "description": f"Inventory - {inv.invoice_number}"},
            ]
            if input_tax > 0:
                in_tax_acc = posting_account("input_tax")
                lines.append(
                    {"account_id": in_tax_acc.id, "debit": input_tax, "credit": 0,
                     "description": f"Input Tax - {inv.invoice_number}"},
                )
            for c in billed_charges:
                lines.append(
                    {"account_id": int(c["charge_account_id"]),
                     "debit": round(float(c["amount"]), 2), "credit": 0,
                     "description": f"{c.get('description') or 'Charge'} - {inv.invoice_number}"},
                )
            for c in expense_charges:
                lines.append(
                    {"account_id": int(c["charge_account_id"]),
                     "debit": round(float(c["amount"]), 2), "credit": 0,
                     "description": f"{c.get('description') or 'Expense'} - {inv.invoice_number}"},
                )
            lines.append(
                {"account_id": ap_acc.id, "debit": 0, "credit": amount_payable,
                 "description": f"AP - {inv.invoice_number}"},
            )
            if wht > 0.005:
                wht_acc = posting_account("wht_payable")
                lines.append(
                    {"account_id": wht_acc.id, "debit": 0, "credit": wht,
                     "description": f"WHT - {inv.invoice_number}"},
                )
            if expense_total > 0.005:
                accrued_acc = posting_account("accrued")
                lines.append(
                    {"account_id": accrued_acc.id, "debit": 0,
                     "credit": round(expense_total, 2),
                     "description": f"Charges payable - {inv.invoice_number}"},
                )
            post_journal_entry(
                voucher_type="PI",
                voucher_id=inv.id,
                voucher_number=inv.voucher_number,
                description=f"Purchase Invoice {inv.invoice_number} - {inv.supplier.name if inv.supplier else ''}",
                lines=lines,
                entry_date=datetime.utcnow(),
                created_by=current_user.id,
            )

    db.session.commit()
    if action == "approve":
        msg = "approved and locked"
    elif inv_id:
        msg = "changes saved"
    else:
        msg = "saved as unapproved"
    return jsonify({"ok": True, "id": inv.id, "status": inv.status,
                    "voucher": inv.voucher_number, "message": f"Invoice {msg}"})


@inv_pinv_bp.route("/unapprove/<int:id>", methods=["POST"])
@login_required
def unapprove_invoice(id):
    denied = deny_json("purchase_invoices", "approve")
    if denied:
        return denied
    inv = InvPurchaseInvoice.query.get_or_404(id)
    if inv.status != "approved":
        return jsonify({"ok": False, "error": "Only approved invoices can be unapproved"}), 400

    # Dependency check: has any item been consumed/sold/adjusted?
    product_ids = [item.product_id for item in inv.items.all() if item.product_id]
    if product_ids:
        cons = ConsItem.query.filter(
            ConsItem.product_id.in_(product_ids)
        ).first()
        if cons:
            return jsonify({"ok": False, "error": "Cannot unapprove: Items already consumed"}), 400
        scrap = ScrapItem.query.filter(
            ScrapItem.product_id.in_(product_ids)
        ).first()
        if scrap:
            return jsonify({"ok": False, "error": "Cannot unapprove: Items already scrapped"}), 400
        adj = AdjItem.query.filter(
            AdjItem.product_id.in_(product_ids)
        ).first()
        if adj:
            return jsonify({"ok": False, "error": "Cannot unapprove: Items already adjusted"}), 400

    reverse_journal_entry("PI", inv.id, current_user.id)

    # §4.4: un-posting restores each source purchase order's balance and
    # reopens it (Fully -> Partially -> Open).
    from shared.order_linkage import apply_writeback
    apply_writeback("purchase",
                    InvPurchaseInvoiceItem.query.filter_by(invoice_id=inv.id).all(),
                    sign=-1)

    inv.status = "unapproved"
    inv.approved_by = None
    inv.approved_at = None

    InvStockMovement.query.filter_by(
        reference_type="purchase_invoice", reference_id=inv.id
    ).delete()

    # Remove this invoice's purchase layers from the cost history and rebuild
    # each product's running balances (also re-syncs current_stock).
    #
    # allow_variance: stock from this invoice may already have been issued, and
    # that issue posted a cost drawn from it which cannot now be restated. The
    # difference is booked to Inventory Cost Variance rather than blocking the
    # unapprove or letting inventory drift away from COGS.
    from shared.ledger_utils import post_variance_journal
    variances = reverse_voucher_stock("PI", inv.id, allow_variance=True,
                                      created_by=current_user.id)
    post_variance_journal(variances, inv.invoice_number, current_user.id)

    db.session.commit()
    message = "Invoice has been unapproved and unlocked for editing"
    if variances:
        total = sum(variances.values())
        message += (f". {abs(total):,.2f} was booked to Inventory Cost Variance: "
                    f"stock from this invoice had already been issued at a cost "
                    f"that is already posted and cannot be changed")
    return jsonify({"ok": True, "status": "unapproved", "message": message,
                    "variance": float(sum(variances.values())) if variances else 0})


@inv_pinv_bp.route("/delete/<int:id>", methods=["POST"])
@login_required
def delete_invoice(id):
    denied = deny_json("purchase_invoices", "delete")
    if denied:
        return denied
    inv = InvPurchaseInvoice.query.get_or_404(id)
    if inv.status == "approved":
        return jsonify({"ok": False, "error": "Cannot delete an approved invoice. Unapprove it first."}), 400
    try:
        InvPurchaseInvoiceItem.query.filter_by(invoice_id=inv.id).delete()
        db.session.delete(inv)
        db.session.commit()
        return jsonify({"ok": True, "message": "Invoice deleted successfully"})
    except Exception as e:
        db.session.rollback()
        return jsonify({"ok": False, "error": str(e)}), 500


@inv_pinv_bp.route("/pay/<int:id>", methods=["POST"])
@login_required
def pay_invoice(id):
    if deny_page("purchase_invoices", "edit"):
        return redirect(url_for("inv_purchase_invoice.list_invoices"))
    inv = InvPurchaseInvoice.query.get_or_404(id)
    amount = request.form.get("amount", 0, type=float)
    if amount <= 0:
        flash("Invalid payment amount", "error")
    else:
        inv.paid_amount = (inv.paid_amount or 0) + amount
        if inv.paid_amount >= inv.total_amount:
            inv.payment_status = "paid"
        else:
            inv.payment_status = "partial"
        cash_acc = posting_account("cash")
        ap_acc = party_account("supplier", inv.supplier_id,
                               inv.supplier.name if inv.supplier else None,
                               inv.party_account_id)
        if cash_acc and ap_acc:
            post_journal_entry(
                voucher_type="PMT",
                voucher_id=inv.id,
                voucher_number=f"PMT-{inv.invoice_number}-{datetime.utcnow():%Y%m%d%H%M%S}",
                description=f"Payment for {inv.invoice_number} - {inv.supplier.name if inv.supplier else ''}",
                lines=[
                    {"account_id": ap_acc.id, "debit": amount, "credit": 0,
                     "description": f"AP - {inv.invoice_number}"},
                    {"account_id": cash_acc.id, "debit": 0, "credit": amount,
                     "description": f"Cash - {inv.invoice_number}"},
                ],
                entry_date=datetime.utcnow(),
                created_by=current_user.id,
            )
        db.session.commit()
        flash(f"Payment of {amount} recorded", "success")
    return redirect(url_for("inv_purchase_invoice.list_invoices"))


@inv_pinv_bp.route("/list")
@login_required
def list_invoices():
    invoices = InvPurchaseInvoice.query.order_by(InvPurchaseInvoice.id.desc()).all()
    return render_template("purchase_invoice/list_inv.html", invoices=invoices)


@inv_pinv_bp.route("/api/products")
@login_required
def api_products():
    q = request.args.get("q", "").strip()
    query = InvProduct.query.filter_by(is_active=True)
    if q:
        query = query.filter(
            db.or_(
                InvProduct.name.ilike(f"%{q}%"),
                InvProduct.sku.ilike(f"%{q}%"),
            )
        )
    products = query.order_by(InvProduct.name).limit(20).all()
    return jsonify([{
        "id": p.id, "name": p.name, "sku": p.sku,
        "unit_price": p.unit_price, "current_stock": p.current_stock,
        "unit": p.unit, "weight": p.weight or 0,
    } for p in products])


@inv_pinv_bp.route("/api/suppliers")
@login_required
def api_suppliers():
    q = request.args.get("q", "").strip()
    query = InvSupplier.query.filter_by(is_active=True)
    if q:
        query = query.filter(InvSupplier.name.ilike(f"%{q}%"))
    suppliers = query.order_by(InvSupplier.name).limit(20).all()
    return jsonify([{
        "id": s.id, "name": s.name, "city": s.city or "",
        "phone": s.phone or "", "address": s.address or "",
    } for s in suppliers])


@inv_pinv_bp.route("/api/new-product", methods=["POST"])
@login_required
def api_new_product():
    data = request.get_json(force=True)
    sku = data.get("sku", "").strip()
    name = data.get("name", "").strip()
    if not sku or not name:
        return jsonify({"ok": False, "error": "SKU and Name required"}), 400
    if InvProduct.query.filter_by(sku=sku).first():
        return jsonify({"ok": False, "error": "SKU already exists"}), 400
    p = InvProduct(
        sku=sku, name=name,
        unit_price=float(data.get("unit_price", 0)),
        cost_price=float(data.get("cost_price", 0)),
        unit=data.get("unit", "pcs"),
        current_stock=0,
    )
    db.session.add(p)
    db.session.commit()
    return jsonify({"ok": True, "product": {
        "id": p.id, "name": p.name, "sku": p.sku,
        "unit_price": p.unit_price, "current_stock": p.current_stock,
        "unit": p.unit,
    }})


@inv_pinv_bp.route("/api/orders/<int:supplier_id>")
@login_required
def api_orders_for_supplier(supplier_id):
    """Approved purchase orders for the picker (§4.2), mirroring the sales side.

    Fully-invoiced orders are returned flagged unselectable rather than hidden.
    """
    from shared.order_linkage import picker_payload
    orders = InvPurchaseOrder.query.filter_by(
        supplier_id=supplier_id, status="approved").order_by(InvPurchaseOrder.id.desc()).all()
    return jsonify([picker_payload("purchase", o) for o in orders])


@inv_pinv_bp.route("/api/accounts")
@login_required
def api_pi_charge_accounts():
    q = request.args.get("q", "").strip()
    query = ChartOfAccount.query.filter_by(is_active=True, level=5)
    if q:
        query = query.filter(
            db.or_(
                ChartOfAccount.name.ilike(f"%{q}%"),
                ChartOfAccount.code.ilike(f"%{q}%"),
            )
        )
    accts = query.order_by(ChartOfAccount.code).limit(30).all()
    return jsonify([{
        "id": a.id, "code": a.code, "name": a.name, "type": a.type,
    } for a in accts])
