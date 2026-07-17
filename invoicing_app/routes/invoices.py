from flask import Blueprint, render_template, redirect, url_for, flash, request, jsonify
from flask_login import login_required, current_user
from datetime import datetime, date
from decimal import Decimal
from inventory_app.extensions import db
from inventory_app.models.invoice import InvInvoice, InvInvoiceItem
from inventory_app.models.customer import InvCustomer
from inventory_app.models.product import InvProduct
from inventory_app.models.stock_movement import InvStockMovement
from inventory_app.models.sales_order import InvSalesOrder
from shared.ledger_utils import post_journal_entry, reverse_journal_entry, posting_account, party_account
from shared.models.ledger import ChartOfAccount
from shared.models.company_settings import CompanyInfo, ReportSettings
from shared.models.invoice_template import InvoiceTemplate, render_invoice_template, build_totals_table
from shared.permissions import deny_json, deny_page
from shared.costing import record_out, reverse_voucher_stock

inv_inv_bp = Blueprint("inv_invoices", __name__, url_prefix="/inventory/invoices")


def next_voucher():
    last = InvInvoice.query.order_by(InvInvoice.id.desc()).first()
    n = (last.id + 1) if last else 1
    return f"SI-{datetime.utcnow():%Y%m}-{n:04d}"


def next_invoice_num():
    last = InvInvoice.query.order_by(InvInvoice.id.desc()).first()
    n = (last.id + 1) if last else 1
    return f"INV-{datetime.utcnow():%Y%m}-{n:04d}"


@inv_inv_bp.route("/", defaults={"id": None})
@inv_inv_bp.route("/<int:id>")
@login_required
def invoice_form(id):
    invoice = InvInvoice.query.get(id) if id else None
    customers = InvCustomer.query.filter_by(is_active=True).order_by(InvCustomer.name).all()
    products = InvProduct.query.filter_by(is_active=True).order_by(InvProduct.name).all()
    invoice_items = []
    if invoice:
        for it in invoice.items.all():
            product = it.product
            invoice_items.append({
                "product_id": it.product_id,
                "product": {"sku": product.sku if product else ""},
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
            })
    rs = ReportSettings.get()
    company = CompanyInfo.get()

    rendered_template = None
    invoice_template_obj = None
    if invoice:
        tid = rs.sales_template_id
        if tid:
            invoice_template_obj = InvoiceTemplate.query.get(tid)
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

        # Style constants
        # Auto-size font when extra columns are shown, so the table fits A4
        extra = (1 if show_disc_col else 0) + (1 if show_tax_col else 0) + (1 if show_chg_col else 0)
        fs = max(9, 12 - extra)
        tds = f"padding:6px 8px;border:1px solid #e2e8f0;font-size:{fs}px;"
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
                cells += f"<td style='{tdr}'>{it.delivery:.2f}</td>"
                cells += f"<td style='{tdr}'>{it.installation:.2f}</td>"
            cells += f"<td style='{tdr}'>{tax_amt:.2f}</td>"
            cells += f"<td style='{tdr}'>{amt_incl:.2f}</td>"
            cells += f"<td style='{tdr}'>{line_total:.2f}</td>"
            items_rows += "<tr>" + cells + "</tr>"

        # Totals footer row
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
            foot += f"<td style='{tdr_b}'>{tot_delivery:.2f}</td>"
            foot += f"<td style='{tdr_b}'>{tot_install:.2f}</td>"
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
            head += f"<th style='{hdr}'>Carriage Expense</th>"
            head += f"<th style='{hdr}'>Installation</th>"
        head += f"<th style='{hdr}'>Total Sales Tax</th>"
        head += f"<th style='{hdr}'>Amount Incl. of Sales Tax</th>"
        head += f"<th style='{hdr}'>Total</th>"

        items_table = (
            '<table style="width:100%;border-collapse:collapse;table-layout:fixed;font-size:12px;">'
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
            "delivery_charges": f"{invoice.global_delivery:.2f}" if invoice.global_delivery else "0.00",
            "installation_charges": f"{invoice.global_installation:.2f}" if invoice.global_installation else "0.00",
            "grand_total": "0.00",
            "commission": "0.00",
            "freight": "0.00",
            "loading_unloading": "0.00",
            "withholding_tax": "0.00",
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
        # Calculate grand total
        net = (invoice.subtotal or 0) - (invoice.total_discount or 0) + (invoice.total_tax or 0) + (invoice.global_delivery or 0) + (invoice.global_installation or 0)
        ctx["grand_total"] = f"{net:.2f}"
        rendered_template = render_invoice_template(invoice_template_obj.body_html, ctx)

    return render_template("invoices/form_inv.html",
                           invoice=invoice, invoice_items=invoice_items,
                           customers=customers,
                           party_mode=rs.party_mode("sales"),
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
    data = request.get_json(force=True)
    inv_id = data.get("id")
    action = data.get("action", "save")

    denied = deny_json("sales_invoices",
                       "approve" if action == "approve" else ("edit" if inv_id else "create"))
    if denied:
        return denied

    if inv_id:
        inv = InvInvoice.query.get_or_404(inv_id)
        if inv.voucher_status == "approved":
            return jsonify({"ok": False, "error": "Cannot modify approved invoice"}), 400
    else:
        inv = InvInvoice(
            voucher_number=next_voucher(),
            invoice_number=data.get("invoice_number") or next_invoice_num(),
            created_by=current_user.id,
        )
        db.session.add(inv)

    if action == "approve":
        validation_errors = validate_invoice(data)
        if validation_errors:
            return jsonify({"ok": False, "error": "; ".join(validation_errors)}), 400

    inv.customer_id = data.get("customer_id")
    inv.party_account_id = data.get("party_account_id") or None
    inv.due_date = datetime.strptime(data.get("due_date"), "%Y-%m-%d") if data.get("due_date") else None
    inv.discount_mode = data.get("discount_mode", "general")
    inv.charges_mode = data.get("charges_mode", "general")
    inv.tax_mode = data.get("tax_mode", "general")

    inv.global_discount_pct = float(data.get("global_discount_pct", 0))
    inv.global_discount_value = float(data.get("global_discount_value", 0))
    inv.global_delivery = float(data.get("global_delivery", 0))
    inv.global_installation = float(data.get("global_installation", 0))
    inv.global_sales_tax_pct = float(data.get("global_sales_tax_pct", 0))
    inv.notes = data.get("notes", "")
    inv.subtotal = float(data.get("subtotal", 0))
    inv.total_discount = float(data.get("total_discount", 0))
    inv.total_charges = float(data.get("total_charges", 0))
    inv.total_tax = float(data.get("total_tax", 0))
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
            prod = InvProduct.query.get(item.product_id)
            if prod:
                db.session.add(InvStockMovement(
                    product_id=item.product_id, type="sale_out",
                    quantity=item.quantity,
                    reference_type="sales_invoice",
                    reference_id=inv.id,
                    notes=f"Approved invoice {inv.invoice_number}",
                    created_by=current_user.id,
                ))
                # Costing engine computes true historic COGS (weighted avg /
                # FIFO across all purchase layers) at issue time.
                _unit, line_cogs = record_out(
                    item.product_id, "SI", inv.id, inv.voucher_number,
                    qty=item.quantity,
                    notes=f"Sale {inv.invoice_number}",
                    created_by=current_user.id)
                total_cogs += line_cogs

    if action == "approve":
        # Receivable posts to the customer's own subledger account (or an
        # explicit override), so the customer's ledger carries the balance.
        ar_acc = party_account("customer", inv.customer_id,
                               inv.customer.name if inv.customer else None,
                               inv.party_account_id)
        rev_acc = posting_account("revenue")
        cogs_acc = posting_account("cogs")
        inv_acc = posting_account("inventory")
        # Split output sales tax into its own liability so revenue is stated
        # net of tax (Dr Receivable = gross, Cr Revenue = net, Cr Output Tax).
        total = float(inv.total_amount or 0)
        output_tax = float(inv.total_tax or 0)
        revenue = round(total - output_tax, 2)
        lines = [
            {"account_id": ar_acc.id, "debit": total, "credit": 0,
             "description": f"AR - {inv.invoice_number}"},
            {"account_id": rev_acc.id, "debit": 0, "credit": revenue,
             "description": f"Revenue - {inv.invoice_number}"},
        ]
        if output_tax > 0:
            out_tax_acc = posting_account("sales_tax_payable")
            lines.append(
                {"account_id": out_tax_acc.id, "debit": 0, "credit": output_tax,
                 "description": f"Output Tax - {inv.invoice_number}"},
            )
        # total_cogs accumulated above from the costing engine (historic cost
        # of each item at issue time — not the static product cost_price).
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


@inv_inv_bp.route("/unapprove/<int:id>", methods=["POST"])
@login_required
def unapprove_invoice(id):
    denied = deny_json("sales_invoices", "approve")
    if denied:
        return denied
    inv = InvInvoice.query.get_or_404(id)
    if inv.voucher_status != "approved":
        return jsonify({"ok": False, "error": "Only approved invoices can be unapproved"}), 400

    reverse_journal_entry("SI", inv.id, current_user.id)

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
    return jsonify({"ok": True, "voucher_status": "unapproved",
                    "message": "Invoice unapproved and unlocked"})


@inv_inv_bp.route("/delete/<int:id>", methods=["POST"])
@login_required
def delete_invoice(id):
    denied = deny_json("sales_invoices", "delete")
    if denied:
        return denied
    inv = InvInvoice.query.get_or_404(id)
    if inv.voucher_status == "approved":
        return jsonify({"ok": False, "error": "Cannot delete an approved invoice. Unapprove it first."}), 400
    try:
        InvInvoiceItem.query.filter_by(invoice_id=inv.id).delete()
        db.session.delete(inv)
        db.session.commit()
        return jsonify({"ok": True, "message": "Invoice deleted successfully"})
    except Exception as e:
        db.session.rollback()
        return jsonify({"ok": False, "error": str(e)}), 500


@inv_inv_bp.route("/pay/<int:id>", methods=["POST"])
@login_required
def pay_invoice(id):
    if deny_page("sales_invoices", "edit"):
        return redirect(url_for("inv_invoices.list_invoices"))
    inv = InvInvoice.query.get_or_404(id)
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
        # Credit the same account the invoice debited, so the customer's
        # ledger nets to the unpaid balance.
        ar_acc = party_account("customer", inv.customer_id,
                               inv.customer.name if inv.customer else None,
                               inv.party_account_id)
        if cash_acc and ar_acc:
            post_journal_entry(
                voucher_type="PMT",
                voucher_id=inv.id,
                voucher_number=f"PMT-{inv.invoice_number}-{datetime.utcnow():%Y%m%d%H%M%S}",
                description=f"Payment received for {inv.invoice_number} - {inv.customer.name if inv.customer else ''}",
                lines=[
                    {"account_id": cash_acc.id, "debit": amount, "credit": 0,
                     "description": f"Cash - {inv.invoice_number}"},
                    {"account_id": ar_acc.id, "debit": 0, "credit": amount,
                     "description": f"AR - {inv.invoice_number}"},
                ],
                entry_date=datetime.utcnow(),
                created_by=current_user.id,
            )
        db.session.commit()
        flash(f"Payment of {amount} recorded", "success")
    return redirect(url_for("inv_invoices.list_invoices"))


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
        "unit": p.unit,
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
    } for c in customers])
