from flask import Blueprint, render_template, request, jsonify
from flask_login import login_required, current_user
from datetime import datetime
from decimal import Decimal
from inventory_app.extensions import db
from shared.tenancy import scoped_get, scoped_get_404
from shared.doc_numbers import is_placeholder, number_from_id, placeholder
from shared.concurrency import bump_edit, stale_edit
from shared import payment_tracking as _pt
from inventory_app.models.purchase_return import InvPurchaseReturn, InvPurchaseReturnItem
from inventory_app.models.purchase_invoice import InvPurchaseInvoice, InvPurchaseInvoiceItem
from inventory_app.models.supplier import InvSupplier
from inventory_app.models.product import InvProduct
from inventory_app.models.stock_movement import InvStockMovement
from shared.ledger_utils import post_journal_entry, reverse_journal_entry, posting_account, party_account
from shared.models.ledger import ChartOfAccount
from shared.permissions import deny_json
from shared.costing import lock_products, record_out, reverse_voucher_stock, original_receipt_cost
from shared.posting_helpers import as_date, parse_doc_date

inv_preturn_bp = Blueprint("inv_purchase_return", __name__,
                           url_prefix="/inventory/purchase-return")


def assign_number(ret):
    """DN-YYYYMM-<the return's own row id>: two users saving new returns
    at once can never draw the same number ("last id + 1" could)."""
    return number_from_id(ret, "DN", "return_number")


@inv_preturn_bp.route("/", defaults={"id": None})
@inv_preturn_bp.route("/<int:id>")
@login_required
def return_form(id):
    ret = scoped_get(InvPurchaseReturn, id) if id else None
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
        prod = scoped_get(InvProduct, orig.product_id)
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


def _over_returned(ret, data):
    """Server-side cap: purchased quantity less what approved returns took."""
    bought = {}
    for it in InvPurchaseInvoiceItem.query.filter_by(invoice_id=ret.original_invoice_id).all():
        if it.product_id:
            bought[it.product_id] = bought.get(it.product_id, 0.0) + float(it.quantity or 0)
    already = {}
    for other in InvPurchaseReturn.query.filter(
            InvPurchaseReturn.original_invoice_id == ret.original_invoice_id,
            InvPurchaseReturn.status == "approved",
            InvPurchaseReturn.id != (ret.id or 0)).all():
        for it in InvPurchaseReturnItem.query.filter_by(return_id=other.id).all():
            if it.product_id:
                already[it.product_id] = (already.get(it.product_id, 0.0)
                                          + float(it.current_return_qty or 0))
    asked = {}
    for row in data.get("items", []):
        pid = row.get("product_id")
        if pid:
            asked[int(pid)] = asked.get(int(pid), 0.0) + float(row.get("current_return_qty", 0) or 0)
    errors = []
    for pid, qty in asked.items():
        room = bought.get(pid, 0.0) - already.get(pid, 0.0)
        if qty > room + 0.0001:
            errors.append(f"Product #{pid}: returning {qty:g} but only {room:g} "
                          f"remain returnable on the invoice")
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
        # Locked until commit: a second user approving the same return waits,
        # then finds it approved instead of posting it again.
        ret = scoped_get_404(InvPurchaseReturn, ret_id, for_update=True)
        if ret.status == "approved":
            return jsonify({"ok": False, "error": "Cannot modify approved return"}), 400
        stale = stale_edit(ret, data.get("edit_version"), f"Return {ret.return_number}")
        if stale:
            return jsonify({"ok": False, "error": stale, "stale": True}), 409
    else:
        ret = InvPurchaseReturn(
            return_number=placeholder(),
            created_by=current_user.id,
        )
        db.session.add(ret)

    if action == "approve":
        validation_errors = validate_return(data)
        if validation_errors:
            return jsonify({"ok": False, "error": "; ".join(validation_errors)}), 400

    ret.original_invoice_id = data.get("original_invoice_id")
    ret.supplier_id = data.get("supplier_id")
    # The debit note posts on its own date — and can never precede the purchase.
    ret.return_date = parse_doc_date(data.get("date"), fallback=ret.return_date)
    # Approving locks the invoice being returned against: two returns raised
    # on it at the same instant both passed the over-return check below.
    _orig = (scoped_get(InvPurchaseInvoice, ret.original_invoice_id, for_update=(action == "approve"))
             if ret.original_invoice_id else None)
    if action == "approve":
        if _orig is None or _orig.status != "approved":
            return jsonify({"ok": False, "error":
                            "A return can only be raised against an approved invoice"}), 400
        if _orig.invoice_date and as_date(ret.return_date) < as_date(_orig.invoice_date):
            return jsonify({"ok": False, "error":
                            f"Return date cannot be before the invoice date "
                            f"({_orig.invoice_date:%d %b %Y})"}), 400
        over = _over_returned(ret, data)
        if over:
            return jsonify({"ok": False, "error": "; ".join(over)}), 400
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
    if is_placeholder(ret.return_number):
        assign_number(ret)
    if action == "approve":
        # Lock order: document, order lines, products — as every posting does.
        from shared.order_linkage import lock_order_lines
        lock_order_lines("purchase", [it.source_order_item_id for it in
                                   InvPurchaseInvoiceItem.query.filter_by(invoice_id=ret.original_invoice_id)])
        lock_products(r.get("product_id") for r in data.get("items", [])
                      if float(r.get("current_return_qty", 0) or 0) > 0)

    InvPurchaseReturnItem.query.filter_by(return_id=ret.id).delete()
    returned_by_product = {}
    stock_cost_out = 0.0
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
            prod = scoped_get(InvProduct, item.product_id)
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
                # Returned goods leave stock at the LANDED cost they came in
                # at on the original invoice, so the inventory credit undoes
                # exactly the inventory debit. (This used to be the return's
                # net value per unit — tax-inclusive and net of withholding —
                # which credited stock with tax and never reversed the input
                # tax.) Any gap between that cost and what the supplier
                # credits is a price difference, posted separately below.
                basis = original_receipt_cost("PI", ret.original_invoice_id,
                                              item.product_id)
                _u, line_cost = record_out(
                    item.product_id, "PRV", ret.id, ret.return_number,
                    qty=qty, unit_cost=basis,
                    notes=f"Purchase return {ret.return_number}",
                    created_by=current_user.id,
                    txn_date=ret.return_date,
                    prefer=("PI", ret.original_invoice_id))
                stock_cost_out += float(line_cost)

    if action == "approve":
        # Mirror of the purchase posting, for the returned share:
        #   Dr Supplier (AP)        debit note value (what they now owe back)
        #   Dr WHT Payable          withholding no longer owed on the goods
        #     Cr Inventory          landed cost of the stock that left
        #     Cr Input Sales Tax    input tax no longer recoverable
        #     Cr/Dr Price variance  supplier credit vs landed cost
        # Debit the same supplier account the original invoice credited.
        orig = _orig
        ap_acc = party_account("supplier", ret.supplier_id,
                               ret.supplier.name if ret.supplier else None,
                               orig.party_account_id if orig else None)
        inv_acc = posting_account("inventory")
        net = round(float(ret.net_return_amount or 0), 2)
        tax = round(float(ret.total_tax or 0), 2)
        wht = round(float(ret.gross_return_value or 0) - float(ret.total_discount or 0)
                    + float(ret.total_expenses or 0) + tax - net, 2)
        wht = wht if wht > 0.005 else 0.0
        cost = round(stock_cost_out, 2)
        diff = round(net + wht - tax - cost, 2)
        label_id = orig.label_id if orig else None
        lines = [{"account_id": ap_acc.id, "debit": net, "credit": 0,
                  "label_id": label_id,
                  "description": f"AP - {ret.return_number}"}]
        if wht:
            lines.append({"account_id": posting_account("wht_payable").id,
                          "debit": wht, "credit": 0,
                          "description": f"WHT reversal - {ret.return_number}"})
        if cost:
            lines.append({"account_id": inv_acc.id, "debit": 0, "credit": cost,
                          "label_id": label_id,
                          "description": f"Inventory - {ret.return_number}"})
        if tax:
            lines.append({"account_id": posting_account("input_tax").id,
                          "debit": 0, "credit": tax,
                          "description": f"Input tax reversal - {ret.return_number}"})
        if abs(diff) >= 0.01:
            var_acc = posting_account("inventory_variance")
            lines.append({"account_id": var_acc.id,
                          "debit": -diff if diff < 0 else 0,
                          "credit": diff if diff > 0 else 0,
                          "label_id": label_id,
                          "description": f"Purchase price difference - {ret.return_number}"})
        post_journal_entry(
            voucher_type="PR",
            voucher_id=ret.id,
            voucher_number=ret.return_number,
            description=f"Purchase Return {ret.return_number}",
            lines=lines,
            entry_date=ret.return_date,
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
    # A return settles part of its invoice: the invoice's paid/unpaid
    # status moves with it (shared/payment_tracking.credited_by_doc).
    if ret.original_invoice_id:
        _pt.sync_invoice(_pt.PURCHASE, ret.original_invoice_id)
    if action == "approve":
        msg = "approved and posted"
    elif ret_id:
        msg = "changes saved"
    else:
        msg = "saved as unapproved"
    return jsonify({"ok": True, "id": ret.id, "status": ret.status,
                    "edit_version": ret.edit_version,
                    "return_number": ret.return_number, "message": f"Return {msg}"})


@inv_preturn_bp.route("/unapprove/<int:id>", methods=["POST"])
@login_required
def unapprove_return(id):
    denied = deny_json("purchase_returns", "approve")
    if denied:
        return denied
    ret = scoped_get_404(InvPurchaseReturn, id, for_update=True)
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
    reverse_voucher_stock("PRV", ret.id, created_by=current_user.id)

    bump_edit(ret)
    db.session.commit()
    # A return settles part of its invoice: the invoice's paid/unpaid
    # status moves with it (shared/payment_tracking.credited_by_doc).
    if ret.original_invoice_id:
        _pt.sync_invoice(_pt.PURCHASE, ret.original_invoice_id)
    return jsonify({"ok": True, "status": "unapproved", "edit_version": ret.edit_version,
                    "message": "Return has been unapproved and unlocked for editing"})


@inv_preturn_bp.route("/list")
@login_required
def list_returns():
    returns = InvPurchaseReturn.query.order_by(InvPurchaseReturn.id.desc()).all()
    return render_template("purchase_return/list_return.html", returns=returns)
