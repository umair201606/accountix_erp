from flask import Blueprint, render_template, request, jsonify
from flask_login import login_required, current_user
from datetime import datetime
from decimal import Decimal
from inventory_app.extensions import db
from inventory_app.models.purchase_return import InvPurchaseReturn, InvPurchaseReturnItem
from inventory_app.models.purchase_invoice import InvPurchaseInvoice, InvPurchaseInvoiceItem
from inventory_app.models.supplier import InvSupplier
from inventory_app.models.product import InvProduct
from inventory_app.models.stock_movement import InvStockMovement
from shared.ledger_utils import post_journal_entry, reverse_journal_entry, posting_account, party_account
from shared.models.ledger import ChartOfAccount
from shared.permissions import deny_json
from shared.costing import record_out, reverse_voucher_stock

inv_preturn_bp = Blueprint("inv_purchase_return", __name__,
                           url_prefix="/inventory/purchase-return")


def next_return_number():
    last = InvPurchaseReturn.query.order_by(InvPurchaseReturn.id.desc()).first()
    n = (last.id + 1) if last else 1
    return f"DN-{datetime.utcnow():%Y%m}-{n:04d}"


@inv_preturn_bp.route("/", defaults={"id": None})
@inv_preturn_bp.route("/<int:id>")
@login_required
def return_form(id):
    ret = InvPurchaseReturn.query.get(id) if id else None
    approved_invoices = InvPurchaseInvoice.query.filter_by(status="approved").order_by(
        InvPurchaseInvoice.id.desc()
    ).all()
    return render_template("purchase_return/form_return.html",
                           return_doc=ret,
                           approved_invoices=approved_invoices,
                           now=datetime.utcnow())


@inv_preturn_bp.route("/api/invoice/<int:invoice_id>")
@login_required
def api_invoice_detail(invoice_id):
    inv = InvPurchaseInvoice.query.filter_by(id=invoice_id, status="approved").first()
    if not inv:
        return jsonify({"ok": False, "error": "Invoice not found or not approved"}), 404

    items = []
    for orig in inv.items.all():
        previously_returned = db.session.query(
            db.func.coalesce(db.func.sum(InvPurchaseReturnItem.current_return_qty), 0)
        ).join(
            InvPurchaseReturn,
            InvPurchaseReturn.id == InvPurchaseReturnItem.return_id
        ).filter(
            InvPurchaseReturn.original_invoice_id == inv.id,
            InvPurchaseReturnItem.product_id == orig.product_id,
            InvPurchaseReturn.status.in_(["unapproved", "approved"])
        ).scalar()

        max_qty = orig.quantity - previously_returned
        prod = InvProduct.query.get(orig.product_id)
        items.append({
            "product_id": orig.product_id,
            "sku": prod.sku if prod else "",
            "description": orig.description,
            "original_quantity": orig.quantity,
            "previously_returned": previously_returned,
            "max_returnable_qty": max(max_qty, 0),
            "unit": orig.unit,
            "unit_price": orig.unit_price,
            "discount_pct": orig.discount_pct,
            "discount_amount": orig.discount_amount,
            "commission": orig.commission,
            "freight": orig.freight,
            "loading_unloading": orig.loading_unloading,
            "sales_tax_pct": orig.sales_tax_pct,
            "withholding_tax_pct": orig.withholding_tax_pct,
            "total_before_discount": orig.total_before_discount,
            "total_after_discount": orig.total_after_discount,
            "proportional_ratio": 0,
        })

    return jsonify({
        "ok": True,
        "invoice": {
            "id": inv.id,
            "invoice_number": inv.invoice_number,
            "voucher_number": inv.voucher_number,
            "date": inv.created_at.strftime("%Y-%m-%d") if inv.created_at else "",
            "supplier_id": inv.supplier_id,
            "supplier_name": inv.supplier.name if inv.supplier else "",
            "driver_name": inv.driver_name or "",
            "driver_contact": inv.driver_contact or "",
            "vehicle_number": inv.vehicle_number or "",
            "gate_pass": inv.gate_pass or "",
            "subtotal": inv.subtotal,
            "total_discount": inv.total_discount,
            "total_expenses": inv.total_expenses,
            "total_tax": inv.total_tax,
            "net_payable": inv.net_payable,
            "discount_mode": inv.discount_mode,
            "expenses_mode": inv.expenses_mode,
            "tax_mode": inv.tax_mode,
        },
        "items": items,
    })


@inv_preturn_bp.route("/api/invoices")
@login_required
def api_invoices():
    q = request.args.get("q", "").strip()
    query = InvPurchaseInvoice.query.filter_by(status="approved")
    if q:
        query = query.join(InvSupplier).filter(
            db.or_(
                InvPurchaseInvoice.invoice_number.ilike(f"%{q}%"),
                InvSupplier.name.ilike(f"%{q}%"),
            )
        )
    invoices = query.order_by(InvPurchaseInvoice.id.desc()).limit(30).all()
    return jsonify([{
        "id": inv.id,
        "invoice_number": inv.invoice_number,
        "supplier_name": inv.supplier.name if inv.supplier else "",
        "date": inv.created_at.strftime("%Y-%m-%d") if inv.created_at else "",
        "net_payable": inv.net_payable,
    } for inv in invoices])


def validate_return(data):
    errors = []
    if not data.get("original_invoice_id"):
        errors.append("Original invoice is required")
    items = data.get("items", [])
    has_return = False
    for i, row in enumerate(items):
        qty = float(row.get("current_return_qty", 0))
        max_q = float(row.get("max_returnable_qty", 0))
        if qty < 0:
            errors.append(f"Row {i+1}: Return quantity cannot be negative")
        if qty > max_q + 0.001:
            errors.append(f"Row {i+1}: Return quantity ({qty}) exceeds max returnable ({max_q})")
        if qty > 0:
            has_return = True
    if not has_return:
        errors.append("At least one item must have a return quantity greater than 0")
    return errors


@inv_preturn_bp.route("/save", methods=["POST"])
@login_required
def save_return():
    data = request.get_json(force=True)
    ret_id = data.get("id")
    action = data.get("action", "save")

    denied = deny_json("purchase_returns",
                       "approve" if action == "approve" else ("edit" if ret_id else "create"))
    if denied:
        return denied

    if ret_id:
        ret = InvPurchaseReturn.query.get_or_404(ret_id)
        if ret.status == "approved":
            return jsonify({"ok": False, "error": "Cannot modify approved return"}), 400
    else:
        ret = InvPurchaseReturn(
            return_number=next_return_number(),
            created_by=current_user.id,
        )
        db.session.add(ret)

    if action == "approve":
        validation_errors = validate_return(data)
        if validation_errors:
            return jsonify({"ok": False, "error": "; ".join(validation_errors)}), 400

    ret.original_invoice_id = data.get("original_invoice_id")
    ret.supplier_id = data.get("supplier_id")
    ret.notes = data.get("notes", "")
    ret.reverse_expenses = data.get("reverse_expenses", True)
    ret.gross_return_value = float(data.get("gross_return_value", 0))
    ret.total_discount = float(data.get("total_discount", 0))
    ret.total_expenses = float(data.get("total_expenses", 0))
    ret.total_tax = float(data.get("total_tax", 0))
    ret.net_return_amount = float(data.get("net_return_amount", 0))

    if action == "approve":
        ret.status = "approved"
        ret.approved_by = current_user.id
        ret.approved_at = datetime.utcnow()
    elif ret.status == "new":
        ret.status = "unapproved"

    db.session.flush()

    InvPurchaseReturnItem.query.filter_by(return_id=ret.id).delete()
    returned_by_product = {}
    for row in data.get("items", []):
        qty = float(row.get("current_return_qty", 0))
        if qty <= 0:
            continue
        item = InvPurchaseReturnItem(
            return_id=ret.id,
            product_id=row.get("product_id"),
            description=row.get("description", ""),
            original_quantity=float(row.get("original_quantity", 0)),
            previously_returned_qty=float(row.get("previously_returned", 0)),
            max_returnable_qty=float(row.get("max_returnable_qty", 0)),
            current_return_qty=qty,
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
            proportional_discount=float(row.get("proportional_discount", 0)),
            proportional_sales_tax=float(row.get("proportional_sales_tax", 0)),
            proportional_withholding_tax=float(row.get("proportional_withholding_tax", 0)),
            proportional_commission=float(row.get("proportional_commission", 0)),
            proportional_freight=float(row.get("proportional_freight", 0)),
            proportional_loading=float(row.get("proportional_loading", 0)),
            net_return_value=float(row.get("net_return_value", 0)),
        )
        db.session.add(item)
        if item.product_id:
            # Tallied here rather than from the cost block below, which skips
            # lines that never moved stock. A service line still consumed an
            # order balance, so returning it still has to give that back.
            returned_by_product[item.product_id] = (
                returned_by_product.get(item.product_id, 0.0) + qty)

        if action == "approve" and item.product_id:
            prod = InvProduct.query.get(item.product_id)
            if prod:
                db.session.add(InvStockMovement(
                    product_id=item.product_id,
                    type="purchase_return_out",
                    quantity=-qty,
                    reference_type="purchase_return",
                    reference_id=ret.id,
                    notes=f"Approved return {ret.return_number}",
                    created_by=current_user.id,
                ))
                # Returned goods leave stock at the ORIGINAL invoice cost
                # basis (net return value per unit), not the current average
                # — the payable reversal must match what was booked in.
                basis = (float(item.net_return_value or 0) / qty) if qty else 0
                record_out(item.product_id, "PRV", ret.id, ret.return_number,
                           qty=qty, unit_cost=basis or None,
                           notes=f"Purchase return {ret.return_number}",
                           created_by=current_user.id)

    if action == "approve":
        # Debit the same supplier account the original invoice credited.
        ap_acc = party_account("supplier", ret.supplier_id,
                               ret.supplier.name if ret.supplier else None)
        inv_acc = posting_account("inventory")
        if ap_acc and inv_acc:
            post_journal_entry(
                voucher_type="PR",
                voucher_id=ret.id,
                voucher_number=ret.return_number,
                description=f"Purchase Return {ret.return_number}",
                lines=[
                    {"account_id": ap_acc.id, "debit": float(ret.net_return_amount), "credit": 0,
                     "description": f"AP - {ret.return_number}"},
                    {"account_id": inv_acc.id, "debit": 0, "credit": float(ret.net_return_amount),
                     "description": f"Inventory - {ret.return_number}"},
                ],
                entry_date=datetime.utcnow(),
                created_by=current_user.id,
            )

    if action == "approve":
        # §4.4 — crediting a posted invoice restores the order balances it
        # consumed and reopens the order, so the quantity can be billed again.
        from shared.order_linkage import apply_return_writeback
        apply_return_writeback(
            "purchase",
            InvPurchaseInvoiceItem.query.filter_by(
                invoice_id=ret.original_invoice_id).all(),
            returned_by_product, sign=-1)

    db.session.commit()
    if action == "approve":
        msg = "approved and posted"
    elif ret_id:
        msg = "changes saved"
    else:
        msg = "saved as unapproved"
    return jsonify({"ok": True, "id": ret.id, "status": ret.status,
                    "return_number": ret.return_number, "message": f"Return {msg}"})


@inv_preturn_bp.route("/unapprove/<int:id>", methods=["POST"])
@login_required
def unapprove_return(id):
    denied = deny_json("purchase_returns", "approve")
    if denied:
        return denied
    ret = InvPurchaseReturn.query.get_or_404(id)
    if ret.status != "approved":
        return jsonify({"ok": False, "error": "Only approved returns can be unapproved"}), 400

    reverse_journal_entry("PR", ret.id, current_user.id)

    # Withdrawing the credit note re-consumes the order balances it released
    # (§4.4). Skipping this would leave the balance permanently inflated and let
    # the same quantity be billed twice.
    from shared.order_linkage import (apply_return_writeback,
                                      tally_returned_quantities)
    apply_return_writeback(
        "purchase",
        InvPurchaseInvoiceItem.query.filter_by(
            invoice_id=ret.original_invoice_id).all(),
        tally_returned_quantities(
            InvPurchaseReturnItem.query.filter_by(return_id=ret.id).all()),
        sign=1)

    ret.status = "unapproved"
    ret.approved_by = None
    ret.approved_at = None

    InvStockMovement.query.filter_by(
        reference_type="purchase_return", reference_id=ret.id
    ).delete()

    # Remove the return's stock rows and rebuild running balances.
    reverse_voucher_stock("PRV", ret.id)

    db.session.commit()
    return jsonify({"ok": True, "status": "unapproved",
                    "message": "Return has been unapproved and unlocked for editing"})


@inv_preturn_bp.route("/list")
@login_required
def list_returns():
    returns = InvPurchaseReturn.query.order_by(InvPurchaseReturn.id.desc()).all()
    return render_template("purchase_return/list_return.html", returns=returns)
