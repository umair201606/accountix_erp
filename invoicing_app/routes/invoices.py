from flask import Blueprint, render_template, redirect, url_for, flash, request, jsonify
from flask_login import login_required, current_user
from datetime import datetime, date
import json
from decimal import Decimal
from inventory_app.extensions import db
from shared.tenancy import scoped_get, scoped_get_404
from inventory_app.models.invoice import InvInvoice, InvInvoiceItem
from inventory_app.models.additional_charge import AdditionalCharge
from inventory_app.models.customer import InvCustomer
from inventory_app.models.product import InvProduct
from inventory_app.models.stock_movement import InvStockMovement
from inventory_app.models.sales_order import InvSalesOrder, InvSalesOrderItem
from shared.ledger_utils import post_journal_entry, reverse_journal_entry, posting_account, party_account
from shared.models.ledger import ChartOfAccount
from shared.models.company_settings import CompanyInfo, ReportSettings
from shared.models.invoice_settings import InvoiceSettings
from shared.models.invoice_template import (
    InvoiceTemplate, render_invoice_template, build_totals_table,
    items_table_metrics)
from shared.formatting import format_amount as _m
from shared.permissions import deny_json, deny_page
from shared.costing import record_out, reverse_voucher_stock

inv_inv_bp = Blueprint("inv_invoices", __name__, url_prefix="/inventory/invoices")


# The v3 §8 chain lives in one place so the posted journal, the printed
# invoice and the FBR payload can never disagree about the same invoice.
from shared.invoice_totals import (  # noqa: E402
    sales_totals as _sales_totals,
    revenue_splits as _revenue_splits,
    output_tax_splits as _output_tax_splits,
)


def _manual_allocations(chg):
    """Per-line amounts for a "Manual per line" charge (§5.1), as JSON.

    Stored only for that method — every other one derives the split from the
    lines, so a saved copy would just go stale. Anything unparseable is dropped
    rather than saved half-read: the split falls back to pro-rata, which is
    visible, instead of to a silently wrong allocation.
    """
    if chg.get("distribution") != "manual":
        return ""
    try:
        return json.dumps([round(float(v or 0), 2) for v in (chg.get("manual") or [])])
    except (TypeError, ValueError):
        return ""


def _read_manual(raw):
    """The stored manual split, or [] if it is absent or unreadable."""
    if not raw:
        return []
    try:
        return [float(v or 0) for v in json.loads(raw)]
    except (TypeError, ValueError):
        return []


def next_voucher():
    """The document number. A sales invoice has exactly one.

    There used to be two generators off the same counter — SI-… into
    voucher_number and INV-… into invoice_number — so every invoice carried two
    different numbers for itself and the form showed both. They are one thing.

    Both columns are still written, with the same value: 48 places across the
    app, the journal and the FBR payload read one or the other, and each column
    carries its own NOT NULL and UNIQUE constraint. Storing the same string in
    both keeps every reader working (a value is unique per column, so this does
    not collide) without a migration.
    """
    last = InvInvoice.query.order_by(InvInvoice.id.desc()).first()
    n = (last.id + 1) if last else 1
    return f"SI-{datetime.utcnow():%Y%m}-{n:04d}"


@inv_inv_bp.route("/", defaults={"id": None})
@inv_inv_bp.route("/<int:id>")
@login_required
def invoice_form(id):
    invoice = scoped_get(InvInvoice, id) if id else None
    customers = InvCustomer.query.filter_by(is_active=True).order_by(InvCustomer.name).all()
    products = InvProduct.query.filter_by(is_active=True).order_by(InvProduct.name).all()
    invoice_items = []
    invoice_charges = []
    if invoice:
        for it in invoice.items.all():
            product = it.product
            invoice_items.append({
                "product_id": it.product_id,
                "product": {"sku": product.sku if product else ""},
                # §6.2 — the "By weight" split needs the line's unit weight
                # client-side; a reopened invoice must carry it too.
                "weight": (product.weight or 0) if product else 0,
                "description": it.description,
                "quantity": it.quantity,
                "unit": it.unit,
                "unit_price": it.unit_price,
                "discount_pct": it.discount_pct,
                "discount_amount": it.discount_amount,
                "delivery": it.delivery,
                "installation": it.installation,
                "sales_tax_pct": it.sales_tax_pct,
                "total_before_discount": it.total_before_discount,
                "total_after_discount": it.total_after_discount,
                # §4.3/§5.1 — the line's source order, for the Order ref column
                # and so re-approving credits the right order line.
                "source_order_id": it.source_order_id,
                "source_order_item_id": it.source_order_item_id,
                "order_ref": it.source_order_number or "",
            })
        for chg in invoice.charges_list:
            code = chg.charge_account.code if chg.charge_account else ""
            name = chg.charge_account.name if chg.charge_account else ""
            invoice_charges.append({
                "id": chg.id,
                "charge_account_id": chg.charge_account_id,
                "account_code": code,
                "account_name": name,
                # The modal's account picker renders from _display, so reopening
                # a saved invoice has to hand it back the same "code — name".
                "_display": f"{code} — {name}" if code else name,
                "description": chg.description,
                "amount": chg.amount,
                "scope": chg.scope,
                # Without these two a reopened invoice silently reverted to
                # pro-rata by value, losing the chosen method and any manual
                # split along with it.
                "distribution": chg.distribution or "pro_rata_value",
                "manual": _read_manual(chg.manual_allocations),
                "treatment": chg.treatment or "bill",
                "st_taxable": bool(chg.st_taxable),
                "wht_taxable": bool(chg.wht_taxable),
                "extra_taxable": bool(chg.extra_taxable),
                # Legacy keys kept for any reader still looking for them.
                "taxable": chg.taxable,
                "tax_base": chg.tax_base,
            })
    rs = ReportSettings.get()
    company = CompanyInfo.get()

    rendered_template = None
    invoice_template_obj = None
    if invoice:
        tid = rs.sales_template_id
        if tid:
            invoice_template_obj = scoped_get(InvoiceTemplate, tid)
        if not invoice_template_obj:
            invoice_template_obj = InvoiceTemplate.get_default("sales")

    if invoice_template_obj and invoice:
        topts = invoice_template_obj.options

        # Decide per-section display based on invoice's mode
        if invoice.discount_mode == "individual":
            show_disc_col = topts.get("discount_display") == "per_line"
        else:
            show_disc_col = False

        if invoice.tax_mode == "individual":
            show_tax_col = topts.get("tax_display") == "per_line"
        else:
            show_tax_col = False

        if invoice.charges_mode == "individual":
            show_chg_col = topts.get("charges_display") == "per_line"
        else:
            show_chg_col = False

        # Style constants. Sizing keys off the real column count, not off how
        # many option groups are on: a group can add two columns, so counting
        # groups under-reports the width the table actually needs.
        n_cols = (10
                  + (2 if show_disc_col else 0)
                  + (2 if show_tax_col else 0)
                  + (2 if show_chg_col else 0))
        m = items_table_metrics(n_cols)
        fs = m["font"]
        tds = f"padding:{m['pad']};border:1px solid #e2e8f0;white-space:nowrap;"
        tdc = tds + "text-align:center;"
        tdr = tds + "text-align:right;"

        tot_qty = 0
        tot_disc_amt = 0.0
        tot_excl = 0.0
        tot_delivery = 0.0
        tot_install = 0.0
        tot_tax_amt = 0.0
        tot_incl = 0.0
        tot_line = 0.0

        items_rows = ""
        for i, it in enumerate(invoice.items.all(), start=1):
            product = it.product
            da = it.discount_amount or 0
            tp = it.sales_tax_pct or 0
            line_total = it.total_before_discount or 0
            amt_excl = line_total - da
            tax_amt = amt_excl * tp / 100
            amt_incl = amt_excl + tax_amt

            tot_qty += it.quantity or 0
            tot_disc_amt += da
            tot_excl += amt_excl
            tot_delivery += it.delivery or 0
            tot_install += it.installation or 0
            tot_tax_amt += tax_amt
            tot_incl += amt_incl
            tot_line += line_total

            cells = (
                f"<td style='{tdc}'>{i}</td>"
                f"<td style='{tds}'>{product.sku if product else ''}</td>"
                f"<td class='inv-desc' style='{tds}white-space:normal;'>{it.description or ''}</td>"
                f"<td style='{tdc}'>{it.quantity}</td>"
                f"<td style='{tdc}'>{it.unit or ''}</td>"
                f"<td style='{tdr}'>{_m(it.unit_price)}</td>")
            if show_disc_col:
                cells += f"<td style='{tdr}'>{it.discount_pct:.1f}%</td>"
                cells += f"<td style='{tdr}'>{_m(da)}</td>"
            cells += f"<td style='{tdr}'>{_m(amt_excl)}</td>"
            if show_tax_col:
                # Per-unit sales tax (distinct from the line's Total Sales Tax
                # column below — the two must not render the same figure twice).
                pu_tax = tax_amt / (it.quantity or 1)
                cells += f"<td style='{tdr}'>{tp:.1f}%</td>"
                cells += f"<td style='{tdr}'>{_m(pu_tax)}</td>"
            if show_chg_col:
                cells += f"<td style='{tdr}'>{_m(it.delivery)}</td>"
                cells += f"<td style='{tdr}'>{_m(it.installation)}</td>"
            cells += f"<td style='{tdr}'>{_m(tax_amt)}</td>"
            cells += f"<td style='{tdr}'>{_m(amt_incl)}</td>"
            cells += f"<td style='{tdr}'>{_m(line_total)}</td>"
            items_rows += "<tr>" + cells + "</tr>"

        # Totals footer row
        tds_b = (f"padding:{m['pad']};border:1px solid #e2e8f0;font-weight:700;"
                 f"background:#f1f5f9;white-space:nowrap;")
        tdr_b = tds_b + "text-align:right;"
        foot = (
            f"<td style='{tds_b};text-align:center;'></td>"
            f"<td style='{tds_b}'></td>"
            f"<td class='inv-desc' style='{tds_b}'>Total</td>"
            f"<td style='{tds_b};text-align:center;'>{tot_qty}</td>"
            f"<td style='{tds_b}'></td>"
            f"<td style='{tdr_b}'></td>")
        if show_disc_col:
            foot += f"<td style='{tdr_b}'></td>"
            foot += f"<td style='{tdr_b}'>{_m(tot_disc_amt)}</td>"
        foot += f"<td style='{tdr_b}'>{_m(tot_excl)}</td>"
        if show_tax_col:
            foot += f"<td style='{tdr_b}'></td>"
            foot += f"<td style='{tdr_b}'>{_m(tot_tax_amt)}</td>"
        if show_chg_col:
            foot += f"<td style='{tdr_b}'>{_m(tot_delivery)}</td>"
            foot += f"<td style='{tdr_b}'>{_m(tot_install)}</td>"
        foot += f"<td style='{tdr_b}'>{_m(tot_tax_amt)}</td>"
        foot += f"<td style='{tdr_b}'>{_m(tot_incl)}</td>"
        foot += f"<td style='{tdr_b}'>{_m(tot_line)}</td>"

        # One style for every header: centred in the cell both ways.
        hd = (f"padding:{m['pad']};border:1px solid #1e293b;white-space:normal;"
              "text-align:center;vertical-align:middle;")

        head = (
            f"<th style='{hd}'>#</th>"
            f"<th style='{hd}'>SKU</th>"
            f"<th class='inv-desc' style='{hd}'>Description</th>"
            f"<th style='{hd}'>Qty</th>"
            f"<th style='{hd}'>Unit</th>"
            f"<th style='{hd}'>Per Unit Price</th>")
        if show_disc_col:
            head += f"<th style='{hd}'>Discount %</th>"
            head += f"<th style='{hd}'>Discount allowed</th>"
        head += f"<th style='{hd}'>Amount Excl. of Sales Tax</th>"
        if show_tax_col:
            head += f"<th style='{hd}'>Sales Tax %</th>"
            head += f"<th style='{hd}'>Sales Tax Amount per Unit</th>"
        if show_chg_col:
            head += f"<th style='{hd}'>Carriage Expense</th>"
            head += f"<th style='{hd}'>Installation</th>"
        head += f"<th style='{hd}'>Total Sales Tax</th>"
        head += f"<th style='{hd}'>Amount Incl. of Sales Tax</th>"
        head += f"<th style='{hd}'>Total</th>"

        items_table = (
            f'<table class="inv-items" style="width:100%;border-collapse:collapse;'
            f'font-size:{fs}px;">'
            '<thead><tr style="background:#1e293b;color:#fff;">' + head +
            '</tr></thead><tbody>' + items_rows +
            '<tr>' + foot + '</tr></tbody></table>'
        )

        party = invoice.customer
        ctx = {
            "company_logo": f'<img src="{company.logo_url}" style="max-height:60px;" alt="Logo">' if company.logo_url else "",
            "company_name": company.company_name or "",
            "company_address": company.address or "",
            "company_city": company.city or "",
            "company_phone": company.phone or "",
            "company_email": company.email or "",
            "company_tax_id": company.tax_id or "",
            "company_bank_name": company.bank_name or "",
            "company_bank_account_title": company.bank_account_title or "",
            "company_bank_account_number": company.bank_account_number or "",
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
            "subtotal": _m(invoice.subtotal),
            "discount": _m(invoice.total_discount),
            "tax": _m(invoice.total_tax),
            "delivery_charges": _m(invoice.global_delivery),
            "installation_charges": _m(invoice.global_installation),
            "grand_total": _m(0),
            "commission": _m(0),
            "freight": _m(0),
            "loading_unloading": _m(0),
            "taxable_value": _m(_sales_totals(invoice)["sales_tax_base"]),
            "additional_charges": _m(invoice.total_charges),
            "further_tax": _m(invoice.total_further_tax),
            "withholding_tax": _m(invoice.total_withholding_tax),
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
        if invoice.charges_mode == "individual":
            if topts.get("charges_display") == "per_line":
                display_opts["show_delivery"] = False
                display_opts["show_installation"] = False
        ctx["totals_table"] = build_totals_table("sales", display_opts,
                                                  invoice_template_obj.accent_color or "#0f766e")
        # The printed total must be the figure that was posted, not a second
        # derivation of it — recomputing here from a few legacy fields silently
        # dropped additional charges, further tax and withholding.
        ctx["grand_total"] = _m(invoice.total_amount)
        rendered_template = render_invoice_template(invoice_template_obj.body_html, ctx)

    # §5.1: the Order ref column exists only on an order-sourced invoice.
    order_sourced = any(i.get("source_order_id") for i in invoice_items)
    return render_template("invoices/form_inv.html",
                           invoice=invoice, invoice_items=invoice_items,
                           invoice_charges=invoice_charges,
                           order_sourced=order_sourced,
                           customers=customers,
                           party_mode=rs.party_mode("sales"),
                           invoice_settings=InvoiceSettings.get(),
                           invoice_template_text=rs.template_text("sales"),
                           rendered_template=rendered_template,
                           products=products, now=datetime.utcnow())


@inv_inv_bp.route("/list")
@login_required
def list_invoices():
    status = request.args.get("status", "")
    query = InvInvoice.query
    if status:
        query = query.filter_by(voucher_status=status)
    invoices = query.order_by(InvInvoice.id.desc()).all()
    return render_template("invoices/list_inv.html", invoices=invoices)


def validate_invoice(data):
    errors = []
    if not data.get("customer_id"):
        errors.append("Customer is required")
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


@inv_inv_bp.route("/save", methods=["POST"])
@login_required
def save_invoice():
    import traceback as _tb
    try:
        data = request.get_json(force=True)
        inv_id = data.get("id")
        action = data.get("action", "save")

        denied = deny_json("sales_invoices",
                       "approve" if action == "approve" else ("edit" if inv_id else "create"))
        if denied:
            return denied
    
        if inv_id:
            inv = scoped_get_404(InvInvoice, inv_id)
            if inv.voucher_status == "approved":
                return jsonify({"ok": False, "error": "Cannot modify approved invoice"}), 400
        else:
            number = data.get("invoice_number")
            if not number or number == "(auto)":
                number = next_voucher()
            inv = InvInvoice(
                voucher_number=number,
                invoice_number=number,
                created_by=current_user.id,
            )
            db.session.add(inv)
    
        if action == "approve":
            validation_errors = validate_invoice(data)
            if validation_errors:
                return jsonify({"ok": False, "error": "; ".join(validation_errors)}), 400
            from types import SimpleNamespace
            from shared.order_linkage import check_over_invoicing
            over = check_over_invoicing("sales", [
                SimpleNamespace(source_order_item_id=r.get("source_order_item_id"),
                                quantity=float(r.get("quantity", 0) or 0))
                for r in data.get("items", [])
            ])
            if over:
                return jsonify({"ok": False,
                                "error": "Over-invoicing blocked — " + "; ".join(over)}), 400
    
        inv.customer_id = data.get("customer_id")
        inv.party_account_id = data.get("party_account_id") or None
        inv.sales_order_id = data.get("sales_order_id") or None
        inv.due_date = datetime.strptime(data.get("due_date"), "%Y-%m-%d") if data.get("due_date") else None
        inv.discount_mode = data.get("discount_mode", "general")
        inv.charges_mode = data.get("charges_mode", "general")
        inv.tax_mode = data.get("tax_mode", "general")
    
        inv.global_discount_pct = float(data.get("global_discount_pct", 0))
        inv.global_discount_value = float(data.get("global_discount_value", 0))
        inv.global_delivery = float(data.get("global_delivery", 0))
        inv.global_installation = float(data.get("global_installation", 0))
        inv.global_sales_tax_pct = float(data.get("global_sales_tax_pct", 0))
        inv.further_tax_pct = float(data.get("further_tax_pct", 0))
        inv.apply_further_tax = bool(data.get("apply_further_tax", False))
        inv.withholding_tax_pct = float(data.get("withholding_tax_pct", 0))
        inv.apply_withholding_tax = bool(data.get("apply_withholding_tax", False))
        inv.notes = data.get("notes", "")
        inv.subtotal = float(data.get("subtotal", 0))
        inv.total_discount = float(data.get("total_discount", 0))
        inv.total_charges = float(data.get("total_charges", 0))
        inv.total_tax = float(data.get("total_tax", 0))
        inv.total_further_tax = float(data.get("total_further_tax", 0))
        inv.total_withholding_tax = float(data.get("total_withholding_tax", 0))
        inv.total_amount = float(data.get("total_amount", 0))
    
        if action == "approve":
            inv.voucher_status = "approved"
            inv.approved_by = current_user.id
            inv.approved_at = datetime.utcnow()
        elif inv.voucher_status != "approved":
            inv.voucher_status = "unapproved"
    
        db.session.flush()
    
        total_cogs = Decimal("0")
        InvInvoiceItem.query.filter_by(invoice_id=inv.id).delete()
        for row in data.get("items", []):
            item = InvInvoiceItem(
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
                delivery=float(row.get("delivery", 0)),
                installation=float(row.get("installation", 0)),
                sales_tax_pct=float(row.get("sales_tax_pct", 0)),
                total_before_discount=float(row.get("total_before_discount", 0)),
                total_after_discount=float(row.get("total_after_discount", 0)),
                comments=row.get("comments", ""),
            )
            db.session.add(item)
    
            if action == "approve" and item.product_id:
                prod = scoped_get(InvProduct, item.product_id)
                if prod:
                    db.session.add(InvStockMovement(
                        product_id=item.product_id, type="sale_out",
                        quantity=item.quantity,
                        reference_type="sales_invoice",
                        reference_id=inv.id,
                        notes=f"Approved invoice {inv.invoice_number}",
                        created_by=current_user.id,
                    ))
                    _unit, line_cogs = record_out(
                        item.product_id, "SI", inv.id, inv.voucher_number,
                        qty=item.quantity,
                        notes=f"Sale {inv.invoice_number}",
                        created_by=current_user.id)
                    total_cogs += line_cogs
    
        if action == "approve":
            from shared.order_linkage import apply_writeback
            apply_writeback("sales", InvInvoiceItem.query.filter_by(invoice_id=inv.id).all())
    
        AdditionalCharge.query.filter_by(doc_type="SI", doc_id=inv.id).delete()
        for chg in data.get("charges", []):
            if float(chg.get("amount", 0)) > 0 and chg.get("charge_account_id"):
                treatment = chg.get("treatment", "bill")
                if treatment not in ("bill", "absorb", "expense"):
                    treatment = "bill"
                billed = treatment == "bill"
                st = billed and bool(chg.get("st_taxable", chg.get("taxable", True)))
                db.session.add(AdditionalCharge(
                    doc_type="SI", doc_id=inv.id,
                    charge_account_id=int(chg["charge_account_id"]),
                    description=chg.get("description", ""),
                    amount=float(chg["amount"]),
                    scope=chg.get("scope", "general"),
                    distribution=chg.get("distribution", "pro_rata_value"),
                    manual_allocations=_manual_allocations(chg),
                    treatment=treatment,
                    st_taxable=st,
                    wht_taxable=billed and bool(chg.get("wht_taxable", False)),
                    extra_taxable=bool(chg.get("extra_taxable", False)),
                    taxable=st,
                    tax_base=chg.get("tax_base", "after_discount"),
                ))
        db.session.flush()
    
        totals = _sales_totals(inv)
        inv.total_charges = totals["billed"]
        inv.total_tax = totals["sales_tax"]
        inv.total_further_tax = totals["further_tax"]
        inv.total_withholding_tax = totals["wht"]
        inv.total_amount = totals["net_receivable"]
    
        if action == "approve":
            ar_acc = party_account("customer", inv.customer_id,
                                   inv.customer.name if inv.customer else None,
                                   inv.party_account_id)
            rev_acc = posting_account("revenue")
            cogs_acc = posting_account("cogs")
            inv_acc = posting_account("inventory")
            out_tax_acc = posting_account("sales_tax_payable")
            t = totals
            lines = [
                {"account_id": ar_acc.id, "debit": t["net_receivable"], "credit": 0,
                 "description": f"AR - {inv.invoice_number}"},
            ]
            for account_id, amount in _revenue_splits(inv, t["effective_subtotal"]):
                lines.append(
                    {"account_id": account_id or rev_acc.id, "debit": 0, "credit": amount,
                     "description": f"Revenue - {inv.invoice_number}"},
                )
            if t["discount"] > 0:
                disc_acc = ChartOfAccount.query.filter_by(code="4-02-02-01-0001").first() \
                    or posting_account("sales_returns")
                lines.append(
                    {"account_id": disc_acc.id, "debit": t["discount"], "credit": 0,
                     "description": f"Discount allowed - {inv.invoice_number}"},
                )
            for row in t["pools"]["billed_rows"]:
                lines.append(
                    {"account_id": row.charge_account_id, "debit": 0,
                     "credit": round(float(row.amount), 2),
                     "description": f"{row.description or 'Charge'} - {inv.invoice_number}"},
                )
            if t["sales_tax"] > 0 and out_tax_acc:
                for account_id, amount in _output_tax_splits(inv, t["sales_tax"]):
                    lines.append(
                        {"account_id": account_id or out_tax_acc.id, "debit": 0,
                         "credit": amount,
                         "description": f"Output Tax - {inv.invoice_number}"},
                    )
            if t["further_tax"] > 0:
                ft_acc = posting_account("further_tax_payable")
                lines.append(
                    {"account_id": ft_acc.id, "debit": 0, "credit": t["further_tax"],
                     "description": f"Further Tax - {inv.invoice_number}"},
                )
            if t["wht"] > 0:
                wht_recv_acct = ChartOfAccount.query.filter_by(code="1-01-05-02-0001").first()
                if wht_recv_acct is None:
                    from shared.ledger_utils import get_or_create_account
                    wht_recv_acct = get_or_create_account(
                        "1-01-05-02-0001", "WHT Receivable", "asset")
                lines.append(
                    {"account_id": wht_recv_acct.id, "debit": t["wht"], "credit": 0,
                     "description": f"WHT Receivable - {inv.invoice_number}"},
                )
            for row in t["pools"]["expense_rows"]:
                amt = round(float(row.amount), 2)
                accrued_acc = posting_account("accrued")
                lines.append(
                    {"account_id": row.charge_account_id, "debit": amt, "credit": 0,
                     "description": f"{row.description or 'Charge'} (absorbed cost) - {inv.invoice_number}"},
                )
                lines.append(
                    {"account_id": accrued_acc.id, "debit": 0, "credit": amt,
                     "description": f"{row.description or 'Charge'} accrued - {inv.invoice_number}"},
                )
            if total_cogs > 0 and cogs_acc and inv_acc:
                lines.append(
                    {"account_id": cogs_acc.id, "debit": float(total_cogs), "credit": 0,
                     "description": f"COGS - {inv.invoice_number}"},
                )
                lines.append(
                    {"account_id": inv_acc.id, "debit": 0, "credit": float(total_cogs),
                     "description": f"Inventory - {inv.invoice_number}"},
                )
            post_journal_entry(
                voucher_type="SI",
                voucher_id=inv.id,
                voucher_number=inv.voucher_number,
                description=f"Sales Invoice {inv.invoice_number} - {inv.customer.name if inv.customer else ''}",
                lines=lines,
                entry_date=datetime.utcnow(),
                created_by=current_user.id,
            )
    
        db.session.commit()
        # Changing the total changes what the existing assignments settle, so
        # the invoice's paid/partial/unpaid state is re-derived here. Subledger
        # matching only — no journal is written by this call.
        from shared import payment_tracking as _pt
        _pt.sync_invoice(_pt.SALES, inv.id)
        if action == "approve":
            msg = "approved and locked"
        elif inv_id:
            msg = "changes saved"
        else:
            msg = "saved"
        return jsonify({"ok": True, "id": inv.id, "voucher_status": inv.voucher_status,
                        "payment_status": inv.payment_status,
                        "number": inv.invoice_number, "voucher": inv.voucher_number,
                        "message": f"Invoice {msg}"})
    except Exception as e:
        _tb.print_exc()
        return jsonify({"ok": False, "error": f"Server error: {e}"}), 500


@inv_inv_bp.route("/unapprove/<int:id>", methods=["POST"])
@login_required
def unapprove_invoice(id):
    denied = deny_json("sales_invoices", "approve")
    if denied:
        return denied
    inv = scoped_get_404(InvInvoice, id)
    if inv.voucher_status != "approved":
        return jsonify({"ok": False, "error": "Only approved invoices can be unapproved"}), 400

    reverse_journal_entry("SI", inv.id, current_user.id)

    # §4.4: un-posting gives the quantities back to the source orders, which
    # reopen (Fully -> Partially -> Open) as their balances are restored.
    from shared.order_linkage import apply_writeback
    apply_writeback("sales", InvInvoiceItem.query.filter_by(invoice_id=inv.id).all(),
                    sign=-1)

    inv.voucher_status = "unapproved"
    inv.payment_status = "unpaid"
    inv.approved_by = None
    inv.approved_at = None

    InvStockMovement.query.filter_by(
        reference_type="sales_invoice", reference_id=inv.id
    ).delete()

    # Remove this invoice's issues from the cost history and rebuild each
    # product's running balances (also re-syncs current_stock).
    reverse_voucher_stock("SI", inv.id)

    db.session.commit()
    # An unapproved invoice owes nothing, so anything assigned to it stops
    # counting and the receipts return to the feed.
    from shared import payment_tracking as _pt
    _pt.sync_invoice(_pt.SALES, inv.id)
    return jsonify({"ok": True, "voucher_status": "unapproved",
                    "message": "Invoice unapproved and unlocked"})


@inv_inv_bp.route("/delete/<int:id>", methods=["POST"])
@login_required
def delete_invoice(id):
    denied = deny_json("sales_invoices", "delete")
    if denied:
        return denied
    inv = scoped_get_404(InvInvoice, id)
    if inv.voucher_status == "approved":
        return jsonify({"ok": False, "error": "Cannot delete an approved invoice. Unapprove it first."}), 400
    try:
        InvInvoiceItem.query.filter_by(invoice_id=inv.id).delete()
        from shared import payment_tracking as _pt
        _pt.drop_allocations_for_doc(_pt.SALES, inv.id)
        db.session.delete(inv)
        db.session.commit()
        return jsonify({"ok": True, "message": "Invoice deleted successfully"})
    except Exception as e:
        db.session.rollback()
        return jsonify({"ok": False, "error": str(e)}), 500


@inv_inv_bp.route("/pay/<int:id>", methods=["POST"])
@login_required
def pay_invoice(id):
    """Retired: receipts are recorded as vouchers and assigned in Tracking.

    This used to post its own ``PMT`` journal (Dr Cash / Cr AR) and add to
    ``paid_amount`` directly. That made a second, invisible way for money to
    enter the books — one with no voucher behind it, unreachable from the
    accounting module and impossible to assign to anything — and it wrote the
    same ``paid_amount`` the tracker derives from assignments, so the two
    disagreed the moment both were used.

    A receipt is now a CRV/BRV (or a JV line) like any other, and Invoicing >
    Tracking assigns it to the invoices it settles. The route is kept so old
    links land somewhere useful instead of on a 404, and it deliberately
    neither posts nor writes anything.
    """
    inv = scoped_get_404(InvInvoice, id)
    flash("Receipts are recorded as a Cash/Bank Receipt Voucher and then "
          "assigned to invoices here — that keeps one trail for the money.",
          "info")
    return redirect(url_for("inv_tracking.invoice_history",
                            doc_type="SI", doc_id=inv.id))


@inv_inv_bp.route("/api/products")
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


@inv_inv_bp.route("/api/customers")
@login_required
def api_customers():
    q = request.args.get("q", "").strip()
    query = InvCustomer.query.filter_by(is_active=True)
    if q:
        query = query.filter(InvCustomer.name.ilike(f"%{q}%"))
    customers = query.order_by(InvCustomer.name).limit(20).all()
    return jsonify([{
        "id": c.id, "name": c.name, "city": c.city or "",
        "phone": c.phone or "", "address": c.address or "",
        # Shown in the party card once a customer is picked, as on the mockup.
        "tax_id": c.tax_id or "", "payment_terms": c.payment_terms or "",
    } for c in customers])


@inv_inv_bp.route("/api/orders/<int:customer_id>")
@login_required
def api_orders_for_customer(customer_id):
    """Approved orders for the picker (§4.2).

    Fully-invoiced orders are still returned so the user can see they exist,
    but flagged unselectable — the document greys them rather than hiding them.
    """
    from shared.order_linkage import picker_payload
    orders = InvSalesOrder.query.filter_by(
        customer_id=customer_id, status="approved").order_by(InvSalesOrder.id.desc()).all()
    return jsonify([picker_payload("sales", o) for o in orders])


@inv_inv_bp.route("/api/accounts")
@login_required
def api_charge_accounts():
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
    # §11.1: a ledger's admin defaults ride along, so picking it in the charges
    # form seeds the treatment and tax-base switches instead of leaving the
    # user to set the same thing every time.
    from shared.models.invoice_settings import ChargeLedgerDefault
    defaults = {d.account_id: d for d in ChargeLedgerDefault.query.filter(
        ChargeLedgerDefault.account_id.in_([a.id for a in accts] or [0])).all()}
    out = []
    for a in accts:
        row = {"id": a.id, "code": a.code, "name": a.name, "type": a.type}
        d = defaults.get(a.id)
        if d and d.applies_to in ("both", "sales"):
            row["defaults"] = {"treatment": d.treatment, "st_taxable": d.st_taxable,
                               "wht_taxable": d.wht_taxable, "extra_taxable": d.extra_taxable}
        out.append(row)
    return jsonify(out)
