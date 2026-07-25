from flask import Blueprint, render_template, redirect, url_for, flash, request, jsonify
from flask_login import login_required, current_user
from datetime import date, datetime
from inventory_app.extensions import db
from inventory_app.models.sales_order import InvSalesOrder, InvSalesOrderItem
from inventory_app.models.customer import InvCustomer
from inventory_app.models.product import InvProduct
from inventory_app.models.additional_charge import AdditionalCharge
from inventory_app.models.invoice import InvInvoice
from inventory_app.models.stock_movement import InvStockMovement
from shared.permissions import deny_json, deny_page
from shared.models.ledger import ChartOfAccount
from shared.models.company_settings import ReportSettings
from shared.models.invoice_settings import InvoiceSettings

inv_sale_bp = Blueprint("inv_sales", __name__, url_prefix="/inventory/sales")


def next_so_number():
    last = InvSalesOrder.query.order_by(InvSalesOrder.id.desc()).first()
    num = (last.id + 1) if last else 1
    return f"SO-{datetime.utcnow():%Y%m}-{num:04d}"


@inv_sale_bp.route("/", defaults={"id": None})
@inv_sale_bp.route("/<int:id>")
@login_required
def sale_form(id):
    order = InvSalesOrder.query.get(id) if id else None
    customers = InvCustomer.query.filter_by(is_active=True).order_by(InvCustomer.name).all()
    products = InvProduct.query.filter_by(is_active=True).order_by(InvProduct.name).all()
    order_items = []
    order_charges = []
    if order:
        for it in order.items.all():
            order_items.append({
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
                "delivery": it.delivery,
                "installation": it.installation,
                "sales_tax_pct": it.sales_tax_pct,
                "total_before_discount": it.total_before_discount,
                "total_after_discount": it.total_after_discount,
            })
        for chg in order.charges_list:
            acct = chg.charge_account
            display = (f"{acct.code} — {acct.name}" if acct else (chg.description or ""))
            order_charges.append({
                "charge_account_id": chg.charge_account_id,
                "_display": display,
                "description": chg.description or (acct.name if acct else ""),
                "amount": chg.amount,
                "scope": chg.scope or "general",
                "treatment": getattr(chg, "treatment", None) or "bill",
                "st_taxable": bool(getattr(chg, "st_taxable", chg.taxable)),
                "wht_taxable": bool(getattr(chg, "wht_taxable", False)),
                "extra_taxable": bool(getattr(chg, "extra_taxable", False)),
            })
    rs = ReportSettings.get()
    return render_template("sales/form_inv.html",
                           order=order,
                           order_items=order_items,
                           order_charges=order_charges,
                           customers=customers,
                           party_mode=rs.party_mode("sales"),
                           invoice_settings=InvoiceSettings.get(),
                           products=[{
                               "id": p.id, "name": p.name, "sku": p.sku,
                               "unit_price": p.unit_price, "current_stock": p.current_stock,
                               "unit": p.unit,
                           } for p in products],
                           now=datetime.utcnow())


@inv_sale_bp.route("/save", methods=["POST"])
@login_required
def save_sale():
    data = request.get_json(force=True)
    order_id = data.get("id")
    action = data.get("action", "save")

    denied = deny_json("sales_orders",
                       "approve" if action == "approve" else ("edit" if order_id else "create"))
    if denied:
        return denied

    if order_id:
        order = InvSalesOrder.query.get_or_404(order_id)
        if order.status == "approved":
            return jsonify({"ok": False, "error": "Cannot modify approved order"}), 400
    else:
        order = InvSalesOrder(
            so_number=data.get("so_number") or next_so_number(),
            created_by=current_user.id,
        )
        db.session.add(order)

    order.customer_id = data.get("customer_id")
    order.party_account_id = data.get("party_account_id") or None
    order.discount_mode = data.get("discount_mode", "general")
    order.charges_mode = data.get("charges_mode", "general")
    order.tax_mode = data.get("tax_mode", "general")
    order.order_date = datetime.strptime(data.get("order_date"), "%Y-%m-%d").date() if data.get("order_date") else date.today()
    order.expected_date = datetime.strptime(data.get("expected_date"), "%Y-%m-%d").date() if data.get("expected_date") else None

    order.global_discount_pct = float(data.get("global_discount_pct", 0))
    order.global_discount_value = float(data.get("global_discount_value", 0))
    order.global_delivery = float(data.get("global_delivery", 0))
    order.global_installation = float(data.get("global_installation", 0))
    order.global_sales_tax_pct = float(data.get("global_sales_tax_pct", 0))
    order.further_tax_pct = float(data.get("further_tax_pct", 0))
    order.apply_further_tax = bool(data.get("apply_further_tax", False))
    order.withholding_tax_pct = float(data.get("withholding_tax_pct", 0))
    order.apply_withholding_tax = bool(data.get("apply_withholding_tax", False))
    order.notes = data.get("notes", "")
    order.subtotal = float(data.get("subtotal", 0))
    order.total_discount = float(data.get("total_discount", 0))
    order.total_charges = float(data.get("total_charges", 0))
    order.total_tax = float(data.get("total_tax", 0))
    order.total_further_tax = float(data.get("total_further_tax", 0))
    order.total_withholding_tax = float(data.get("total_withholding_tax", 0))
    order.total_amount = float(data.get("total_amount", 0))

    if action == "approve":
        order.status = "approved"
        order.approved_by = current_user.id
        order.approved_at = datetime.utcnow()
    elif order.status == "new":
        order.status = "unapproved"

    db.session.flush()

    InvSalesOrderItem.query.filter_by(so_id=order.id).delete()
    for row in data.get("items", []):
        item = InvSalesOrderItem(
            so_id=order.id,
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
            total_price=float(row.get("total_price", 0)),
        )
        db.session.add(item)

    AdditionalCharge.query.filter_by(doc_type="SO", doc_id=order.id).delete()
    for chg in data.get("charges", []):
        if not chg.get("charge_account_id"):
            continue
        if float(chg.get("amount", 0)) > 0:
            st_taxable = bool(chg.get("st_taxable", chg.get("taxable", True)))
            db.session.add(AdditionalCharge(
                doc_type="SO", doc_id=order.id,
                charge_account_id=int(chg["charge_account_id"]),
                description=chg.get("description", ""),
                amount=float(chg["amount"]),
                scope=chg.get("scope", "general"),
                treatment=chg.get("treatment", "bill"),
                st_taxable=st_taxable,
                wht_taxable=bool(chg.get("wht_taxable", False)),
                extra_taxable=bool(chg.get("extra_taxable", False)),
                taxable=st_taxable,
                tax_base=chg.get("tax_base", "after_discount"),
            ))

    db.session.commit()
    if action == "approve":
        msg = "approved and locked"
    elif order_id:
        msg = "changes saved"
    else:
        msg = "saved as unapproved"
    return jsonify({"ok": True, "id": order.id, "status": order.status,
                    "number": order.so_number, "message": f"Order {msg}"})


@inv_sale_bp.route("/unapprove/<int:id>", methods=["POST"])
@login_required
def unapprove_sale(id):
    denied = deny_json("sales_orders", "approve")
    if denied:
        return denied
    order = InvSalesOrder.query.get_or_404(id)
    if order.status != "approved":
        return jsonify({"ok": False, "error": "Only approved orders can be unapproved"}), 400
    order.status = "unapproved"
    order.approved_by = None
    order.approved_at = None
    db.session.commit()
    return jsonify({"ok": True, "status": "unapproved", "message": "Order unapproved"})


@inv_sale_bp.route("/delete/<int:id>", methods=["POST"])
@login_required
def delete_sale(id):
    denied = deny_json("sales_orders", "delete")
    if denied:
        return denied
    order = InvSalesOrder.query.get_or_404(id)
    if order.status == "approved":
        return jsonify({"ok": False, "error": "Cannot delete approved order. Unapprove first."}), 400
    try:
        InvSalesOrderItem.query.filter_by(so_id=order.id).delete()
        AdditionalCharge.query.filter_by(doc_type="SO", doc_id=order.id).delete()
        db.session.delete(order)
        db.session.commit()
        return jsonify({"ok": True, "message": "Order deleted"})
    except Exception as e:
        db.session.rollback()
        return jsonify({"ok": False, "error": str(e)}), 500


@inv_sale_bp.route("/list")
@login_required
def list_sales():
    status = request.args.get("status", "")
    query = InvSalesOrder.query
    if status:
        query = query.filter_by(status=status)
    orders = query.order_by(InvSalesOrder.id.desc()).all()
    return render_template("sales/list_inv.html", orders=orders)


@inv_sale_bp.route("/deliver/<int:id>")
@login_required
def deliver_sale(id):
    if deny_page("sales_orders", "approve"):
        return redirect(url_for("inv_sales.list_sales"))
    so = InvSalesOrder.query.get_or_404(id)
    if so.status in ("delivered", "cancelled"):
        flash("Order already delivered or cancelled", "error")
        return redirect(url_for("inv_sales.list_sales"))

    insufficient = []
    for item in so.items.all():
        prod = InvProduct.query.get(item.product_id)
        if prod and prod.current_stock < item.quantity:
            insufficient.append(f"{prod.name} (have {prod.current_stock}, need {item.quantity})")

    if insufficient:
        flash(f"Insufficient stock: {', '.join(insufficient)}", "error")
        return redirect(url_for("inv_sales.list_sales"))

    for item in so.items.all():
        prod = InvProduct.query.get(item.product_id)
        if prod:
            prod.current_stock -= item.quantity
            db.session.add(InvStockMovement(
                product_id=prod.id, type="sale_out",
                quantity=item.quantity,
                reference_type="sales_order",
                reference_id=so.id,
                notes=f"Delivered via SO {so.so_number}",
                created_by=current_user.id,
            ))

    so.status = "delivered"
    db.session.commit()

    inv_num = f"INV-{so.so_number}"
    if not InvInvoice.query.filter_by(invoice_number=inv_num).first():
        inv = InvInvoice(
            invoice_number=inv_num,
            sales_order_id=so.id,
            customer_id=so.customer_id,
            invoice_date=date.today(),
            due_date=date.today(),
            status="unpaid",
            total_amount=so.total_amount,
        )
        db.session.add(inv)
        db.session.commit()

    flash(f"SO {so.so_number} delivered", "success")
    return redirect(url_for("inv_sales.list_sales"))


@inv_sale_bp.route("/cancel/<int:id>")
@login_required
def cancel_sale(id):
    if deny_page("sales_orders", "edit"):
        return redirect(url_for("inv_sales.list_sales"))
    order = InvSalesOrder.query.get_or_404(id)
    order.status = "cancelled"
    db.session.commit()
    flash(f"SO {order.so_number} cancelled", "warning")
    return redirect(url_for("inv_sales.list_sales"))


@inv_sale_bp.route("/api/products")
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


@inv_sale_bp.route("/api/customers")
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


@inv_sale_bp.route("/api/accounts")
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
    return jsonify([{
        "id": a.id, "code": a.code, "name": a.name, "type": a.type,
    } for a in accts])


@inv_sale_bp.route("/api/orders/<int:customer_id>")
@login_required
def api_orders_for_customer(customer_id):
    orders = InvSalesOrder.query.filter_by(customer_id=customer_id, status="approved").all()
    result = []
    for o in orders:
        items = []
        for i in o.items.all():
            items.append({
                "id": i.id,
                "product_id": i.product_id,
                "product_name": i.product.name if i.product else "",
                "product_sku": i.product.sku if i.product else "",
                "ordered_qty": i.quantity,
                "unit_price": i.unit_price,
            })
        result.append({
            "id": o.id,
            "so_number": o.so_number,
            "order_date": o.order_date.strftime("%Y-%m-%d") if o.order_date else "",
            "total_amount": o.total_amount,
            "items": items,
        })
    return jsonify(result)
