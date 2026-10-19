from flask import Blueprint, render_template, redirect, url_for, flash, request, jsonify
from flask_login import login_required, current_user
from datetime import date, datetime
from inventory_app.extensions import db
from shared.tenancy import scoped_get, scoped_get_404
from shared.order_linkage import invoices_drawing_on
from shared.concurrency import bump_edit, stale_edit
from shared.doc_numbers import is_placeholder, number_from_id, placeholder
from inventory_app.models.purchase_order import InvPurchaseOrder, InvPurchaseOrderItem
from inventory_app.models.supplier import InvSupplier
from inventory_app.models.product import InvProduct
from inventory_app.models.stock_movement import InvStockMovement
from inventory_app.models.additional_charge import AdditionalCharge
from shared.permissions import deny_json, deny_page
from shared.models.ledger import ChartOfAccount
from shared.models.company_settings import ReportSettings
from shared.models.invoice_settings import InvoiceSettings

inv_pur_bp = Blueprint("inv_purchases", __name__, url_prefix="/inventory/purchases")


def assign_number(order):
    """PO-YYYYMM-<the order's own row id>: two users saving new orders at
    once can never draw the same number ("last id + 1" could)."""
    return number_from_id(order, "PO", "po_number")


@inv_pur_bp.route("/", defaults={"id": None})
@inv_pur_bp.route("/<int:id>")
@login_required
def purchase_form(id):
    order = scoped_get(InvPurchaseOrder, id) if id else None
    suppliers = InvSupplier.query.filter_by(is_active=True).order_by(InvSupplier.name).all()
    products = InvProduct.query.filter_by(is_active=True).order_by(InvProduct.name).all()
    order_items = []
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
                "sales_tax_pct": it.sales_tax_pct,
                "total_before_discount": it.total_before_discount,
                "total_after_discount": it.total_after_discount,
            })
    # An order has no charges; the template still reads this key.
    order_charges = []
    rs = ReportSettings.get()
    return render_template("purchases/form_inv.html",
                           order=order,
                           order_items=order_items,
                           order_charges=order_charges,
                           suppliers=suppliers,
                           party_mode=rs.party_mode("purchase"),
                           invoice_settings=InvoiceSettings.get(),
                           products=[{
                               "id": p.id, "name": p.name, "sku": p.sku,
                               "unit_price": p.unit_price, "current_stock": p.current_stock,
                               "unit": p.unit,
                           } for p in products],
                           now=datetime.utcnow())


@inv_pur_bp.route("/save", methods=["POST"])
@login_required
def save_purchase():
    data = request.get_json(force=True)
    order_id = data.get("id")
    action = data.get("action", "save")

    denied = deny_json("purchase_orders",
                       "approve" if action == "approve" else ("edit" if order_id else "create"))
    if denied:
        return denied

    if order_id:
        order = scoped_get_404(InvPurchaseOrder, order_id, for_update=True)
        if order.status == "approved":
            return jsonify({"ok": False, "error": "Cannot modify approved order"}), 400
        stale = stale_edit(order, data.get("edit_version"), f"Order {order.po_number}")
        if stale:
            return jsonify({"ok": False, "error": stale, "stale": True}), 409
        billed = invoices_drawing_on("purchase", order.id)
        if billed:
            return jsonify({"ok": False, "error": (
                f"Cannot change {order.po_number}: invoice(s) {', '.join(billed)} were "
                "billed from it. Unapprove and delete those invoices first.")}), 400
    else:
        order = InvPurchaseOrder(
            # The form sends "(auto)" for a new order; that text used to be
            # saved as the number itself (and the next order then clashed).
            po_number=(data.get("po_number") if data.get("po_number") not in (None, "", "(auto)")
                   else placeholder()),
            created_by=current_user.id,
        )
        db.session.add(order)

    order.supplier_id = data.get("supplier_id")
    order.party_account_id = data.get("party_account_id") or None
    # An order carries no discount, charges, withholding or further tax — those
    # are decided at invoicing (their columns were dropped). Tax scope stays.
    order.tax_mode = data.get("tax_mode", "general")
    order.order_date = datetime.strptime(data.get("order_date"), "%Y-%m-%d").date() if data.get("order_date") else date.today()
    order.expected_date = datetime.strptime(data.get("expected_date"), "%Y-%m-%d").date() if data.get("expected_date") else None

    order.global_sales_tax_pct = float(data.get("global_sales_tax_pct", 0))
    order.notes = data.get("notes", "")
    order.driver_name = data.get("driver_name", "")
    order.driver_contact = data.get("driver_contact", "")
    order.vehicle_number = data.get("vehicle_number", "")
    order.gate_pass = data.get("gate_pass", "")
    order.subtotal = float(data.get("subtotal", 0))
    order.total_tax = float(data.get("total_tax", 0))
    order.total_amount = float(data.get("total_amount", 0))

    if action == "approve":
        order.status = "approved"
        order.approved_by = current_user.id
        order.approved_at = datetime.utcnow()
    elif order.status == "new":
        order.status = "unapproved"

    db.session.flush()
    if is_placeholder(order.po_number):
        assign_number(order)

    InvPurchaseOrderItem.query.filter_by(po_id=order.id).delete()
    for row in data.get("items", []):
        item = InvPurchaseOrderItem(
            po_id=order.id,
            product_id=row.get("product_id"),
            description=row.get("description", ""),
            quantity=float(row.get("quantity", 1)),
            unit=row.get("unit", "pcs"),
            unit_price=float(row.get("unit_price", 0)),

            sales_tax_pct=float(row.get("sales_tax_pct", 0)),
            total_before_discount=float(row.get("total_before_discount", 0)),
            total_after_discount=float(row.get("total_after_discount", 0)),
            total_price=float(row.get("total_price", 0)),
        )
        db.session.add(item)

    # Charges are not eligible on an order. The delete stays so rows written
    # by an older build are cleared the first time the order is re-saved.
    AdditionalCharge.query.filter_by(doc_type="PO", doc_id=order.id).delete()

    db.session.commit()
    if action == "approve":
        msg = "approved and locked"
    elif order_id:
        msg = "changes saved"
    else:
        msg = "saved as unapproved"
    return jsonify({"ok": True, "id": order.id, "status": order.status,
                    "edit_version": order.edit_version,
                    "number": order.po_number, "message": f"Order {msg}"})


@inv_pur_bp.route("/unapprove/<int:id>", methods=["POST"])
@login_required
def unapprove_purchase(id):
    denied = deny_json("purchase_orders", "approve")
    if denied:
        return denied
    order = scoped_get_404(InvPurchaseOrder, id, for_update=True)
    if order.status != "approved":
        return jsonify({"ok": False, "error": "Only approved orders can be unapproved"}), 400
    billed = invoices_drawing_on("purchase", order.id)
    if billed:
        return jsonify({"ok": False, "error": (
            f"Cannot unapprove {order.po_number}: invoice(s) {', '.join(billed)} were "
            "billed from it. Unapprove and delete those invoices first, or cancel "
            "the order to stop further billing.")}), 400
    order.status = "unapproved"
    order.approved_by = None
    order.approved_at = None
    bump_edit(order)
    db.session.commit()
    return jsonify({"ok": True, "status": "unapproved", "edit_version": order.edit_version,
                    "message": "Order unapproved"})


@inv_pur_bp.route("/delete/<int:id>", methods=["POST"])
@login_required
def delete_purchase(id):
    denied = deny_json("purchase_orders", "delete")
    if denied:
        return denied
    order = scoped_get_404(InvPurchaseOrder, id, for_update=True)
    if order.status == "approved":
        return jsonify({"ok": False, "error": "Cannot delete approved order. Unapprove first."}), 400
    billed = invoices_drawing_on("purchase", order.id)
    if billed:
        return jsonify({"ok": False, "error": (
            f"Cannot delete {order.po_number}: invoice(s) {', '.join(billed)} were "
            "billed from it.")}), 400
    try:
        InvPurchaseOrderItem.query.filter_by(po_id=order.id).delete()
        AdditionalCharge.query.filter_by(doc_type="PO", doc_id=order.id).delete()
        db.session.delete(order)
        db.session.commit()
        return jsonify({"ok": True, "message": "Order deleted"})
    except Exception as e:
        db.session.rollback()
        return jsonify({"ok": False, "error": str(e)}), 500


@inv_pur_bp.route("/list")
@login_required
def list_purchases():
    status = request.args.get("status", "")
    query = InvPurchaseOrder.query
    if status:
        query = query.filter_by(status=status)
    orders = query.order_by(InvPurchaseOrder.id.desc()).all()
    return render_template("purchases/list_inv.html", orders=orders)


@inv_pur_bp.route("/receive/<int:id>")
@login_required
def receive_purchase(id):
    """Retired: goods come into stock on the purchase invoice.

    "Receive" (a GET link) added the quantity to the product's on-hand figure
    with no cost layer and no journal; billing the order then added it again.
    The purchase invoice (Import from order) brings the stock in at its
    landed cost and posts Inventory against the supplier in one step.
    """
    po = scoped_get_404(InvPurchaseOrder, id)
    flash(f"Bill {po.po_number} with a purchase invoice (Import from order): stock comes in "
          "at landed cost and the supplier is credited when it is approved.", "info")
    return redirect(url_for("inv_purchase_invoice.invoice_form"))


@inv_pur_bp.route("/cancel/<int:id>", methods=["POST"])
@login_required
def cancel_purchase(id):
    if deny_page("purchase_orders", "edit"):
        return redirect(url_for("inv_purchases.list_purchases"))
    order = scoped_get_404(InvPurchaseOrder, id, for_update=True)
    if order.status not in ("unapproved", "approved", "new"):
        flash(f"PO {order.po_number} is {order.status}; it cannot be cancelled.", "error")
        return redirect(url_for("inv_purchases.list_purchases"))
    order.status = "cancelled"
    db.session.commit()
    flash(f"PO {order.po_number} cancelled; what is still unbilled can no longer be invoiced.",
          "warning")
    return redirect(url_for("inv_purchases.list_purchases"))


@inv_pur_bp.route("/api/products")
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


@inv_pur_bp.route("/api/suppliers")
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


@inv_pur_bp.route("/api/accounts")
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


@inv_pur_bp.route("/api/orders/<int:supplier_id>")
@login_required
def api_orders_for_supplier(supplier_id):
    orders = InvPurchaseOrder.query.filter_by(supplier_id=supplier_id, status="approved").all()
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
            "po_number": o.po_number,
            "order_date": o.order_date.strftime("%Y-%m-%d") if o.order_date else "",
            "total_amount": o.total_amount,
            "items": items,
        })
    return jsonify(result)
