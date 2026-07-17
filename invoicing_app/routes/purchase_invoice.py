from flask import Blueprint, render_template, redirect, url_for, flash, request, jsonify
from flask_login import login_required, current_user
from datetime import datetime
from decimal import Decimal
from inventory_app.extensions import db
from inventory_app.models.purchase_invoice import InvPurchaseInvoice, InvPurchaseInvoiceItem
from inventory_app.models.supplier import InvSupplier
from inventory_app.models.product import InvProduct
from inventory_app.models.stock_movement import InvStockMovement
from shared.models.vouchers import ConsumptionItem as ConsItem, ScrapItem, StockAdjustmentItem as AdjItem
from shared.ledger_utils import post_journal_entry, reverse_journal_entry, posting_account, party_account
from shared.models.ledger import ChartOfAccount
from shared.models.company_settings import CompanyInfo, ReportSettings
from shared.models.invoice_template import InvoiceTemplate, render_invoice_template, build_totals_table
from shared.permissions import deny_json
from shared.costing import record_in, reverse_voucher_stock

inv_pinv_bp = Blueprint("inv_purchase_invoice", __name__,
                         url_prefix="/inventory/purchase-invoice")


def next_voucher():
    last = InvPurchaseInvoice.query.order_by(InvPurchaseInvoice.id.desc()).first()
    n = (last.id + 1) if last else 1
    return f"VCH-{datetime.utcnow():%Y%m}-{n:04d}"


def next_invoice_num(supplier_id=None):
    last = InvPurchaseInvoice.query.order_by(InvPurchaseInvoice.id.desc()).first()
    n = (last.id + 1) if last else 1
    return f"PINV-{datetime.utcnow():%Y%m}-{n:04d}"


@inv_pinv_bp.route("/", defaults={"id": None})
@inv_pinv_bp.route("/<int:id>")
@login_required
def invoice_form(id):
    invoice = InvPurchaseInvoice.query.get(id) if id else None
    suppliers = InvSupplier.query.filter_by(is_active=True).order_by(InvSupplier.name).all()
    products = InvProduct.query.filter_by(is_active=True).order_by(InvProduct.name).all()
    invoice_items = []
    if invoice:
        for it in invoice.items.all():
            invoice_items.append({
                "product_id": it.product_id,
                "product": {"sku": it.product.sku if it.product else ""},
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
            "commission": f"{invoice.total_commission:.2f}" if invoice.total_commission else "0.00",
            "freight": f"{invoice.total_freight:.2f}" if invoice.total_freight else "0.00",
            "loading_unloading": f"{invoice.total_loading_unloading:.2f}" if invoice.total_loading_unloading else "0.00",
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
        net = (invoice.subtotal or 0) - (invoice.total_discount or 0) + (invoice.total_tax or 0) + (invoice.total_commission or 0) + (invoice.total_freight or 0) + (invoice.total_loading_unloading or 0) - (invoice.total_withholding_tax or 0)
        ctx["grand_total"] = f"{net:.2f}"
        rendered_template = render_invoice_template(invoice_template_obj.body_html, ctx)

    return render_template("purchase_invoice/form_inv.html",
                           invoice=invoice,
                           invoice_items=invoice_items,
                           suppliers=suppliers,
                           party_mode=rs.party_mode("purchase"),
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
        inv = InvPurchaseInvoice(
            voucher_number=next_voucher(),
            invoice_number=data.get("invoice_number") or next_invoice_num(),
            created_by=current_user.id,
        )
        db.session.add(inv)

    if action == "approve":
        validation_errors = validate_approve(data)
        if validation_errors:
            return jsonify({"ok": False, "error": "; ".join(validation_errors)}), 400

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
    inv.notes = data.get("notes", "")
    inv.subtotal = float(data.get("subtotal", 0))
    inv.total_discount = float(data.get("total_discount", 0))
    inv.total_expenses = float(data.get("total_expenses", 0))
    inv.total_tax = float(data.get("total_tax", 0))
    inv.net_payable = float(data.get("net_payable", 0))

    if action == "approve":
        inv.status = "approved"
        inv.approved_by = current_user.id
        inv.approved_at = datetime.utcnow()
    elif inv.status == "new":
        inv.status = "unapproved"

    db.session.flush()

    InvPurchaseInvoiceItem.query.filter_by(invoice_id=inv.id).delete()
    for row in data.get("items", []):
        item = InvPurchaseInvoiceItem(
            invoice_id=inv.id,
            product_id=row.get("product_id"),
            description=row.get("description", ""),
            quantity=float(row.get("quantity", 1)),
            unit=row.get("unit", "pcs"),
            unit_price=float(row.get("unit_price", 0)),
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

        if action == "approve" and item.product_id:
            prod = InvProduct.query.get(item.product_id)
            if prod:
                db.session.add(InvStockMovement(
                    product_id=item.product_id, type="purchase_in",
                    quantity=item.quantity,
                    reference_type="purchase_invoice",
                    reference_id=inv.id,
                    notes=f"Approved invoice {inv.invoice_number}",
                    created_by=current_user.id,
                ))
                # Costing engine: receive stock at LANDED cost (goods value
                # after discount plus per-item expenses). This purchase layer
                # is what future issues (sales, scrap, consumption) draw on.
                landed_total = (float(item.total_after_discount or 0)
                                + float(item.commission or 0)
                                + float(item.freight or 0)
                                + float(item.loading_unloading or 0))
                qty_f = float(item.quantity or 0)
                if qty_f > 0:
                    record_in(item.product_id, "PI", inv.id, inv.voucher_number,
                              qty=qty_f, unit_cost=landed_total / qty_f,
                              notes=f"Purchase {inv.invoice_number}",
                              created_by=current_user.id)

    if action == "approve":
        inv_acc = posting_account("inventory")
        # Payable posts to the supplier's own subledger account (or an
        # explicit override), so the supplier's ledger carries the balance.
        ap_acc = party_account("supplier", inv.supplier_id,
                               inv.supplier.name if inv.supplier else None,
                               inv.party_account_id)
        if inv_acc and ap_acc:
            # Goods value (subtotal net of discount, plus capitalised expenses)
            # is debited to Inventory; recoverable input sales tax is debited to
            # its own asset; any withholding deducted from the payable is
            # credited to WHT Payable so the entry still balances:
            #   Dr Inventory (goods) + Dr Input Tax (sales tax)
            #   Cr Accounts Payable (net_payable) + Cr WHT Payable (residual)
            net_payable = float(inv.net_payable or 0)
            input_tax = float(inv.total_tax or 0)
            goods = round(float(inv.subtotal or 0) - float(inv.total_discount or 0)
                          + float(inv.total_expenses or 0), 2)
            wht = round(goods + input_tax - net_payable, 2)
            lines = [
                {"account_id": inv_acc.id, "debit": goods, "credit": 0,
                 "description": f"Inventory - {inv.invoice_number}"},
            ]
            if input_tax > 0:
                in_tax_acc = posting_account("input_tax")
                lines.append(
                    {"account_id": in_tax_acc.id, "debit": input_tax, "credit": 0,
                     "description": f"Input Tax - {inv.invoice_number}"},
                )
            lines.append(
                {"account_id": ap_acc.id, "debit": 0, "credit": net_payable,
                 "description": f"AP - {inv.invoice_number}"},
            )
            if abs(wht) > 0.005:
                wht_acc = posting_account("wht_payable")
                lines.append(
                    {"account_id": wht_acc.id, "debit": 0, "credit": wht,
                     "description": f"WHT - {inv.invoice_number}"},
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
        "unit": p.unit,
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
