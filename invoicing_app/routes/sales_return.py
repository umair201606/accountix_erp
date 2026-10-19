from flask import Blueprint, render_template, request, jsonify
from flask_login import login_required, current_user
from datetime import datetime

from inventory_app.extensions import db
from shared.tenancy import scoped_get, scoped_get_404
from shared.doc_numbers import is_placeholder, number_from_id, placeholder
from shared.concurrency import bump_edit, stale_edit
from shared import payment_tracking as _pt
from inventory_app.models.sales_return import InvSalesReturn, InvSalesReturnItem
from inventory_app.models.invoice import InvInvoice, InvInvoiceItem
from inventory_app.models.customer import InvCustomer
from inventory_app.models.product import InvProduct
from inventory_app.models.stock_movement import InvStockMovement
from shared.ledger_utils import (post_journal_entry, reverse_journal_entry,
                                 posting_account, party_account)
from shared.permissions import deny_json
from shared.costing import lock_products, record_in, reverse_voucher_stock, original_issue_cost
from shared.posting_helpers import as_date, parse_doc_date

inv_sreturn_bp = Blueprint("inv_sales_return", __name__,
                           url_prefix="/invoicing/sales-return")


def assign_number(ret):
    """CN-YYYYMM-<the return's own row id>: two users saving new returns
    at once can never draw the same number ("last id + 1" could)."""
    return number_from_id(ret, "CN", "return_number")


@inv_sreturn_bp.route("/", defaults={"id": None})
@inv_sreturn_bp.route("/<int:id>")
@login_required
def return_form(id):
    ret = scoped_get(InvSalesReturn, id) if id else None
    approved_invoices = InvInvoice.query.filter_by(voucher_status="approved").order_by(
        InvInvoice.id.desc()
    ).all()
    return render_template("sales_return/form_return.html",
                           return_doc=ret,
                           approved_invoices=approved_invoices,
                           now=datetime.utcnow())


@inv_sreturn_bp.route("/api/invoice/<int:invoice_id>")
@login_required
def api_invoice_detail(invoice_id):
    inv = InvInvoice.query.filter_by(id=invoice_id, voucher_status="approved").first()
    if not inv:
        return jsonify({"ok": False, "error": "Invoice not found or not approved"}), 404

    items = []
    for orig in inv.items.all():
        previously_returned = db.session.query(
            db.func.coalesce(db.func.sum(InvSalesReturnItem.current_return_qty), 0)
        ).join(
            InvSalesReturn,
            InvSalesReturn.id == InvSalesReturnItem.return_id
        ).filter(
            InvSalesReturn.original_invoice_id == inv.id,
            InvSalesReturnItem.product_id == orig.product_id,
            InvSalesReturn.status.in_(["unapproved", "approved"])
        ).scalar()

        max_qty = orig.quantity - previously_returned
        prod = scoped_get(InvProduct, orig.product_id) if orig.product_id else None
        # The cost this line left stock at. Shown so the user can see what the
        # return puts back, and stored on the line at approval.
        basis = original_issue_cost("SI", inv.id, orig.product_id) if orig.product_id else None
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
            "delivery": orig.delivery,
            "installation": orig.installation,
            "sales_tax_pct": orig.sales_tax_pct,
            "total_before_discount": orig.total_before_discount,
            "total_after_discount": orig.total_after_discount,
            "cost_basis": float(basis) if basis is not None else 0,
        })

    return jsonify({
        "ok": True,
        "invoice": {
            "id": inv.id,
            "invoice_number": inv.invoice_number,
            "voucher_number": inv.voucher_number,
            "date": inv.invoice_date.strftime("%Y-%m-%d") if inv.invoice_date else "",
            "customer_id": inv.customer_id,
            "customer_name": inv.customer.name if inv.customer else "",
            "subtotal": inv.subtotal,
            "total_discount": inv.total_discount,
            "total_charges": inv.total_charges,
            "total_tax": inv.total_tax,
            "total_amount": inv.total_amount,
            "discount_mode": inv.discount_mode,
            "charges_mode": inv.charges_mode,
            "tax_mode": inv.tax_mode,
        },
        "items": items,
    })


@inv_sreturn_bp.route("/api/invoices")
@login_required
def api_invoices():
    q = request.args.get("q", "").strip()
    query = InvInvoice.query.filter_by(voucher_status="approved")
    if q:
        query = query.join(InvCustomer).filter(
            db.or_(
                InvInvoice.invoice_number.ilike(f"%{q}%"),
                InvCustomer.name.ilike(f"%{q}%"),
            )
        )
    invoices = query.order_by(InvInvoice.id.desc()).limit(30).all()
    return jsonify([{
        "id": inv.id,
        "invoice_number": inv.invoice_number,
        "customer_name": inv.customer.name if inv.customer else "",
        "date": inv.invoice_date.strftime("%Y-%m-%d") if inv.invoice_date else "",
        "total_amount": inv.total_amount,
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


@inv_sreturn_bp.route("/save", methods=["POST"])
@login_required
def save_return():
    data = request.get_json(force=True)
    ret_id = data.get("id")
    action = data.get("action", "save")

    denied = deny_json("sales_returns",
                       "approve" if action == "approve" else ("edit" if ret_id else "create"))
    if denied:
        return denied

    if ret_id:
        # Locked until commit: a second user approving the same return waits,
        # then finds it approved instead of posting it again.
        ret = scoped_get_404(InvSalesReturn, ret_id, for_update=True)
        if ret.status == "approved":
            return jsonify({"ok": False, "error": "Cannot modify approved return"}), 400
        stale = stale_edit(ret, data.get("edit_version"), f"Return {ret.return_number}")
        if stale:
            return jsonify({"ok": False, "error": stale, "stale": True}), 409
    else:
        ret = InvSalesReturn(
            return_number=placeholder(),
            created_by=current_user.id,
        )
        db.session.add(ret)

    if action == "approve":
        validation_errors = validate_return(data)
        if validation_errors:
            return jsonify({"ok": False, "error": "; ".join(validation_errors)}), 400

    ret.original_invoice_id = data.get("original_invoice_id")
    ret.customer_id = data.get("customer_id")
    # The return posts on its own date — and can never precede the sale.
    ret.return_date = parse_doc_date(data.get("date"), fallback=ret.return_date)
    # Approving locks the invoice being returned against: two returns raised
    # on it at the same instant both passed the over-return check below.
    _orig = (scoped_get(InvInvoice, ret.original_invoice_id, for_update=(action == "approve"))
             if ret.original_invoice_id else None)
    if action == "approve":
        if _orig is None or _orig.voucher_status != "approved":
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
    ret.reverse_charges = data.get("reverse_charges", True)
    ret.gross_return_value = float(data.get("gross_return_value", 0))
    ret.total_discount = float(data.get("total_discount", 0))
    ret.total_charges = float(data.get("total_charges", 0))
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
        lock_order_lines("sales", [it.source_order_item_id for it in
                                   InvInvoiceItem.query.filter_by(invoice_id=ret.original_invoice_id)])
        lock_products(r.get("product_id") for r in data.get("items", [])
                      if float(r.get("current_return_qty", 0) or 0) > 0)

    InvSalesReturnItem.query.filter_by(return_id=ret.id).delete()
    total_cost_returned = 0.0
    returned_by_product = {}
    for row in data.get("items", []):
        qty = float(row.get("current_return_qty", 0))
        if qty <= 0:
            continue
        item = InvSalesReturnItem(
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
            delivery=float(row.get("delivery", 0)),
            installation=float(row.get("installation", 0)),
            sales_tax_pct=float(row.get("sales_tax_pct", 0)),
            total_before_discount=float(row.get("total_before_discount", 0)),
            total_after_discount=float(row.get("total_after_discount", 0)),
            proportional_discount=float(row.get("proportional_discount", 0)),
            proportional_sales_tax=float(row.get("proportional_sales_tax", 0)),
            proportional_delivery=float(row.get("proportional_delivery", 0)),
            proportional_installation=float(row.get("proportional_installation", 0)),
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
                    type="sales_return_in",
                    quantity=qty,
                    reference_type="sales_return",
                    reference_id=ret.id,
                    notes=f"Approved return {ret.return_number}",
                    created_by=current_user.id,
                ))
                # Returned goods re-enter stock at the cost they LEFT at on the
                # original invoice — never at today's valuation. Selling a unit
                # costed at 10 and taking it back at a current average of 18
                # would invent 8 of inventory value from a round trip that
                # changed nothing, and the COGS credited here would not match
                # the COGS the sale debited.
                basis = original_issue_cost("SI", ret.original_invoice_id,
                                            item.product_id)
                if basis is None:
                    # No issue row for this line (e.g. a service line, or an
                    # invoice approved before the costing engine existed).
                    # Nothing left stock, so nothing comes back into it.
                    continue
                item.cost_basis = float(basis)
                item.total_cost_returned = float(basis) * qty
                total_cost_returned += item.total_cost_returned
                record_in(item.product_id, "SRV", ret.id, ret.return_number,
                          qty=qty, unit_cost=basis,
                          notes=f"Sales return {ret.return_number}",
                          created_by=current_user.id,
                          txn_date=ret.return_date)

    ret.total_cost_returned = total_cost_returned

    if action == "approve":
        # §4.4 — crediting a posted invoice restores the order balances it
        # consumed and reopens the order, so the quantity can be billed again.
        from shared.order_linkage import apply_return_writeback
        apply_return_writeback(
            "sales",
            InvInvoiceItem.query.filter_by(
                invoice_id=ret.original_invoice_id).all(),
            returned_by_product, sign=-1)

        # Credit the same customer account the original invoice debited.
        ar_acc = party_account("customer", ret.customer_id,
                               ret.customer.name if ret.customer else None,
                               ret.party_account_id)
        returns_acc = posting_account("sales_returns")
        cogs_acc = posting_account("cogs")
        inv_acc = posting_account("inventory")

        gross = float(ret.net_return_amount or 0)
        tax = float(ret.total_tax or 0)
        net_of_tax = round(gross - tax, 2)

        # Value side: the contra-revenue account carries the return, so Sales
        # stays gross and the P&L's "Less: Sales Returns" line is what moves.
        lines = [
            {"account_id": returns_acc.id, "debit": net_of_tax, "credit": 0,
             "description": f"Sales Return - {ret.return_number}"},
        ]
        if tax > 0:
            out_tax_acc = posting_account("sales_tax_payable")
            lines.append(
                {"account_id": out_tax_acc.id, "debit": tax, "credit": 0,
                 "description": f"Output Tax reversal - {ret.return_number}"})
        lines.append(
            {"account_id": ar_acc.id, "debit": 0, "credit": gross,
             "description": f"AR - {ret.return_number}"})

        # Cost side: stock comes back on at the basis it left at, and the same
        # amount comes off COGS.
        if total_cost_returned > 0 and cogs_acc and inv_acc:
            lines.append(
                {"account_id": inv_acc.id, "debit": round(total_cost_returned, 2),
                 "credit": 0, "description": f"Inventory - {ret.return_number}"})
            lines.append(
                {"account_id": cogs_acc.id, "debit": 0,
                 "credit": round(total_cost_returned, 2),
                 "description": f"COGS reversal - {ret.return_number}"})

        post_journal_entry(
            voucher_type="SR",
            voucher_id=ret.id,
            voucher_number=ret.return_number,
            description=f"Sales Return {ret.return_number} - "
                        f"{ret.customer.name if ret.customer else ''}",
            lines=lines,
            entry_date=ret.return_date,
            created_by=current_user.id,
        )

    db.session.commit()
    # A return settles part of its invoice: the invoice's paid/unpaid
    # status moves with it (shared/payment_tracking.credited_by_doc).
    if ret.original_invoice_id:
        _pt.sync_invoice(_pt.SALES, ret.original_invoice_id)
    if action == "approve":
        msg = "approved and posted"
    elif ret_id:
        msg = "changes saved"
    else:
        msg = "saved as unapproved"
    return jsonify({"ok": True, "id": ret.id, "status": ret.status,
                    "edit_version": ret.edit_version,
                    "return_number": ret.return_number, "message": f"Return {msg}"})


def _over_returned(ret, data):
    """Server-side cap: sold quantity less what approved returns already took.

    The browser sends its own ``max_returnable_qty``; trusting it would let a
    tampered request return (and re-stock) more than was ever sold.
    """
    sold = {}
    for it in InvInvoiceItem.query.filter_by(invoice_id=ret.original_invoice_id).all():
        if it.product_id:
            sold[it.product_id] = sold.get(it.product_id, 0.0) + float(it.quantity or 0)
    already = {}
    for other in InvSalesReturn.query.filter(
            InvSalesReturn.original_invoice_id == ret.original_invoice_id,
            InvSalesReturn.status == "approved",
            InvSalesReturn.id != (ret.id or 0)).all():
        for it in InvSalesReturnItem.query.filter_by(return_id=other.id).all():
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
        room = sold.get(pid, 0.0) - already.get(pid, 0.0)
        if qty > room + 0.0001:
            errors.append(f"Product #{pid}: returning {qty:g} but only {room:g} "
                          f"remain returnable on the invoice")
    return errors


@inv_sreturn_bp.route("/unapprove/<int:id>", methods=["POST"])
@login_required
def unapprove_return(id):
    denied = deny_json("sales_returns", "approve")
    if denied:
        return denied
    ret = scoped_get_404(InvSalesReturn, id, for_update=True)
    if ret.status != "approved":
        return jsonify({"ok": False, "error": "Only approved returns can be unapproved"}), 400

    reverse_journal_entry("SR", ret.id, current_user.id)

    # Withdrawing the credit note re-consumes the order balances it released
    # (§4.4). Skipping this would leave the balance permanently inflated and let
    # the same quantity be billed twice.
    from shared.order_linkage import (apply_return_writeback,
                                      tally_returned_quantities)
    apply_return_writeback(
        "sales",
        InvInvoiceItem.query.filter_by(invoice_id=ret.original_invoice_id).all(),
        tally_returned_quantities(
            InvSalesReturnItem.query.filter_by(return_id=ret.id).all()),
        sign=1)

    ret.status = "unapproved"
    ret.approved_by = None
    ret.approved_at = None

    InvStockMovement.query.filter_by(
        reference_type="sales_return", reference_id=ret.id
    ).delete()

    # Withdraw the returned stock. Refuses if it has since been re-sold: that
    # sale drew its cost from this return's layer and posted it, so the layer
    # cannot be removed without leaving that cost backed by nothing.
    reverse_voucher_stock("SRV", ret.id, created_by=current_user.id)

    bump_edit(ret)
    db.session.commit()
    # A return settles part of its invoice: the invoice's paid/unpaid
    # status moves with it (shared/payment_tracking.credited_by_doc).
    if ret.original_invoice_id:
        _pt.sync_invoice(_pt.SALES, ret.original_invoice_id)
    return jsonify({"ok": True, "status": "unapproved", "edit_version": ret.edit_version,
                    "message": "Return has been unapproved and unlocked for editing"})


@inv_sreturn_bp.route("/list")
@login_required
def list_returns():
    returns = InvSalesReturn.query.order_by(InvSalesReturn.id.desc()).all()
    return render_template("sales_return/list_return.html", returns=returns)
