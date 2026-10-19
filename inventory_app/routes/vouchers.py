from datetime import datetime
from decimal import Decimal
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from flask_login import login_required, current_user
from shared.extensions import db
from shared.tenancy import scoped_get, scoped_get_404
from shared.models.stock_ledger import StockLedger, VoucherNumber
from shared.stock_utils import dependency_check, validate_no_dependents
from shared.ledger_utils import post_journal_entry, get_or_create_account
from shared.costing import (cost_of_issue, current_unit_cost, lock_products, on_hand,
                            on_hand_at, record_in, record_out, reverse_voucher_stock)
from shared.models.ledger import ChartOfAccount
from shared.models.project_label import ProjectLabel
from shared.permissions import deny_page
from shared.posting_helpers import parse_doc_date
from shared.models.vouchers import (
    ConsumptionVoucher, ConsumptionItem,
    ScrapVoucher, ScrapItem,
    StockAdjustmentVoucher, StockAdjustmentItem,
    StockTake, StockTakeItem,
)

inv_vouchers_bp = Blueprint("inv_vouchers", __name__, url_prefix="/inventory/vouchers")


def _resolve_product(pid):
    from ..models.product import InvProduct
    return scoped_get(InvProduct, pid)


def _cost_from_ledger(pid):
    # Valuation-method cost (weighted average / FIFO) from the costing engine.
    return current_unit_cost(pid)


def _charge_accounts():
    """Postable (level-5 operational) accounts a voucher's value can be
    charged to (expenses, employee accounts, receivables, projects...)."""
    return ChartOfAccount.query.filter(
        ChartOfAccount.level >= ChartOfAccount.POSTING_LEVEL,
        ChartOfAccount.is_active == True).order_by(ChartOfAccount.code).all()


def _next_take_reference():
    """Next free ST-##### reference (skips any issued by the old count()+1)."""
    while True:
        ref = VoucherNumber.next("ST")
        if not StockTake.query.filter_by(reference=ref).first():
            return ref


def _labels():
    return ProjectLabel.query.filter_by(is_active=True).order_by(ProjectLabel.name).all()


def _form_ctx():
    from datetime import date as _date
    return {"accounts": _charge_accounts(), "labels": _labels(),
            "today_iso": _date.today().isoformat()}


def _locked(voucher, list_endpoint):
    """An approved voucher is posted: its stock and journal are final until it
    is unapproved. Re-submitting the form used to delete and re-create its
    items and issue the stock a SECOND time (or flip it to unapproved while
    the stock and journal stayed posted). Refuse instead."""
    if voucher is not None and voucher.status == "approved":
        flash(f"{voucher.voucher_number} is approved and posted. Unapprove it "
              f"to make changes.", "error")
        return redirect(url_for(list_endpoint))
    return None


def _post_voucher_journal(vtype, v, lines, entry_date=None):
    vname = {"CONS": "Consumption", "SCRAP": "Scrap",
             "ADJ": "Stock Adjustment", "ST": "Stock Take"}.get(vtype, vtype)
    label_id = getattr(v, "label_id", None)
    for l in lines:
        l.setdefault("label_id", label_id)
    post_journal_entry(
        voucher_type=vtype,
        voucher_id=v.id,
        voucher_number=v.voucher_number,
        description=f"{vname} {v.voucher_number}: {getattr(v, 'reason', '') or getattr(v, 'reference', '')}",
        lines=lines,
        entry_date=entry_date or getattr(v, "date", None),
        created_by=current_user.id
    )


# ─────────────────────────────────────────────
# Consumption Voucher
# ─────────────────────────────────────────────

@inv_vouchers_bp.route("/consumption", methods=["GET", "POST"])
@inv_vouchers_bp.route("/consumption/<int:id>", methods=["GET", "POST"])
@login_required
def consumption_form(id=None):
    # A save or approval locks the voucher until commit: two users approving
    # it at once post it once (the second finds it approved).
    voucher = scoped_get(ConsumptionVoucher, id, for_update=request.method == "POST") if id else None
    if id and voucher is None:
        flash("Voucher not found.", "error")
        return redirect(url_for("inv_vouchers.consumption_list"))
    if request.method == "POST":
        if deny_page("consumption_vouchers", "edit" if voucher else "create"):
            return redirect(url_for("inv_vouchers.consumption_list"))
        locked = _locked(voucher, "inv_vouchers.consumption_list")
        if locked:
            return locked
        status = request.form.get("status", "unapproved")
        if status == "approved" and deny_page("consumption_vouchers", "approve"):
            return redirect(url_for("inv_vouchers.consumption_list"))
        is_new = voucher is None
        if is_new:
            voucher = ConsumptionVoucher(
                voucher_number=VoucherNumber.next("CONS"),
                created_by=current_user.id
            )
            db.session.add(voucher)
        else:
            ConsumptionItem.query.filter_by(voucher_id=voucher.id).delete()
            db.session.flush()

        voucher.date = parse_doc_date(request.form.get("date"), fallback=voucher.date)
        voucher.department = request.form.get("department", "")
        voucher.reason = request.form.get("reason", "")
        voucher.charge_account_id = request.form.get("charge_account_id", type=int)
        voucher.label_id = request.form.get("label_id", type=int)

        db.session.flush()

        products = request.form.getlist("product_id[]")
        qtys = request.form.getlist("qty[]")
        new_items = []
        for i, pid in enumerate(products):
            if not pid or not pid.strip():
                continue
            qty = Decimal(str(float(qtys[i]) if i < len(qtys) else 0))
            if qty <= 0:
                continue
            cost, line_total = cost_of_issue(int(pid), qty)
            item = ConsumptionItem(
                voucher_id=voucher.id, product_id=int(pid),
                product_name=(_resolve_product(int(pid)).name if _resolve_product(int(pid))
                              else f"Product #{pid}"),
                quantity=qty, unit_cost=cost, total_cost=line_total
            )
            db.session.add(item)
            new_items.append(item)

        if not new_items:
            db.session.rollback()
            flash("Add at least one item with quantity > 0.", "error")
            return render_template("vouchers/consumption_form.html",
                                   voucher=None if is_new else voucher, **_form_ctx())

        if status == "approved":
            db.session.flush()
            lock_products(i.product_id for i in new_items)
            # Issue stock through the costing engine — it computes the true
            # historic cost (weighted avg / FIFO) at this moment, which is
            # exactly what gets charged to the target ledger account.
            for item in new_items:
                unit, total = record_out(
                    product_id=item.product_id,
                    voucher_type="CONS", voucher_id=voucher.id,
                    voucher_number=voucher.voucher_number,
                    qty=float(item.quantity),
                    notes=f"Consumption: {voucher.reason}",
                    created_by=current_user.id,
                    txn_date=voucher.date,
                )
                item.unit_cost, item.total_cost = unit, total
            total_value = sum(float(i.total_cost) for i in new_items)
            charge_acct_id = (voucher.charge_account_id or
                              get_or_create_account("5700", "Consumption Expense", "expense").id)
            _post_voucher_journal("CONS", voucher, [
                {"account_id": charge_acct_id,
                 "debit": total_value, "credit": 0,
                 "description": f"Consumption: {voucher.reason}"},
                {"account_id": get_or_create_account("1200","Inventory","asset").id,
                 "debit": 0, "credit": total_value,
                 "description": "Stock reduction at historic cost"},
            ])
            voucher.approved_by = current_user.id
            voucher.approved_at = datetime.utcnow()

        voucher.status = status
        db.session.commit()
        flash(f"Consumption voucher {voucher.voucher_number} saved.", "success")
        return redirect(url_for("inv_vouchers.consumption_list"))

    return render_template("vouchers/consumption_form.html", voucher=voucher,
                           **_form_ctx())


@inv_vouchers_bp.route("/consumption/list")
@login_required
def consumption_list():
    vouchers = ConsumptionVoucher.query.order_by(ConsumptionVoucher.id.desc()).all()
    return render_template("vouchers/consumption_list.html", vouchers=vouchers)


@inv_vouchers_bp.route("/consumption/<int:id>/delete", methods=["POST"])
@login_required
def consumption_delete(id):
    if deny_page("consumption_vouchers", "delete"):
        return redirect(url_for("inv_vouchers.consumption_list"))
    v = scoped_get_404(ConsumptionVoucher, id, for_update=True)
    if v.status == "approved":
        flash("Cannot delete an approved voucher. Unapprove it first.", "error")
        return redirect(url_for("inv_vouchers.consumption_list"))
    db.session.delete(v)
    db.session.commit()
    flash("Consumption voucher deleted.", "success")
    return redirect(url_for("inv_vouchers.consumption_list"))


@inv_vouchers_bp.route("/consumption/<int:id>/unapprove", methods=["POST"])
@login_required
def consumption_unapprove(id):
    if deny_page("consumption_vouchers", "approve"):
        return redirect(url_for("inv_vouchers.consumption_list"))
    v = scoped_get_404(ConsumptionVoucher, id, for_update=True)
    if v.status != "approved":
        flash("Voucher is not approved.", "error")
        return redirect(url_for("inv_vouchers.consumption_list"))
    from shared.ledger_utils import reverse_journal_entry
    reverse_journal_entry("CONS", v.id, created_by=current_user.id)
    if "CONS" == "ADJ":
        # A stock take's adjustment posted its journal under the take.
        take = StockTake.query.filter_by(adjustment_voucher_id=v.id).first()
        if take is not None:
            reverse_journal_entry("ST", take.id, created_by=current_user.id)
            take.status = "in_progress"
            take.adjustment_voucher_id = None
    # Later issues of the same products are re-costed in date order and any
    # change is journalled (shared/costing.py).
    reverse_voucher_stock("CONS", v.id, created_by=current_user.id)
    v.status = "unapproved"
    v.approved_by = None
    v.approved_at = None
    db.session.commit()
    flash(f"{v.voucher_number} unapproved and stock reversed.", "success")
    return redirect(url_for("inv_vouchers.consumption_list"))


# ─────────────────────────────────────────────
# Scrap Voucher
# ─────────────────────────────────────────────

@inv_vouchers_bp.route("/scrap", methods=["GET", "POST"])
@inv_vouchers_bp.route("/scrap/<int:id>", methods=["GET", "POST"])
@login_required
def scrap_form(id=None):
    # A save or approval locks the voucher until commit: two users approving
    # it at once post it once (the second finds it approved).
    voucher = scoped_get(ScrapVoucher, id, for_update=request.method == "POST") if id else None
    if id and voucher is None:
        flash("Voucher not found.", "error")
        return redirect(url_for("inv_vouchers.scrap_list"))
    if request.method == "POST":
        if deny_page("scrap_vouchers", "edit" if voucher else "create"):
            return redirect(url_for("inv_vouchers.scrap_list"))
        locked = _locked(voucher, "inv_vouchers.scrap_list")
        if locked:
            return locked
        status = request.form.get("status", "unapproved")
        if status == "approved" and deny_page("scrap_vouchers", "approve"):
            return redirect(url_for("inv_vouchers.scrap_list"))
        is_new = voucher is None
        if is_new:
            voucher = ScrapVoucher(
                voucher_number=VoucherNumber.next("SCRAP"),
                created_by=current_user.id
            )
            db.session.add(voucher)
        else:
            ScrapItem.query.filter_by(voucher_id=voucher.id).delete()
            db.session.flush()

        voucher.date = parse_doc_date(request.form.get("date"), fallback=voucher.date)
        voucher.reason = request.form.get("reason", "")
        voucher.charge_account_id = request.form.get("charge_account_id", type=int)
        voucher.label_id = request.form.get("label_id", type=int)

        db.session.flush()

        products = request.form.getlist("product_id[]")
        qtys = request.form.getlist("qty[]")
        new_items = []
        for i, pid in enumerate(products):
            if not pid or not pid.strip():
                continue
            qty = Decimal(str(float(qtys[i]) if i < len(qtys) else 0))
            if qty <= 0:
                continue
            cost, line_total = cost_of_issue(int(pid), qty)
            item = ScrapItem(
                voucher_id=voucher.id, product_id=int(pid),
                product_name=(_resolve_product(int(pid)).name if _resolve_product(int(pid))
                              else f"Product #{pid}"),
                quantity=qty, unit_cost=cost, total_cost=line_total
            )
            db.session.add(item)
            new_items.append(item)

        if not new_items:
            db.session.rollback()
            flash("Add at least one item with quantity > 0.", "error")
            return render_template("vouchers/scrap_form.html",
                                   voucher=None if is_new else voucher, **_form_ctx())

        if status == "approved":
            db.session.flush()
            lock_products(i.product_id for i in new_items)
            # Cost computed from the product's purchase history at this
            # moment — the receivable/loss is booked at true historic cost.
            for item in new_items:
                unit, total = record_out(
                    product_id=item.product_id,
                    voucher_type="SCRAP", voucher_id=voucher.id,
                    voucher_number=voucher.voucher_number,
                    qty=float(item.quantity),
                    notes=f"Scrap: {voucher.reason}",
                    created_by=current_user.id,
                    txn_date=voucher.date,
                )
                item.unit_cost, item.total_cost = unit, total
            total_value = sum(float(i.total_cost) for i in new_items)
            charge_acct_id = (voucher.charge_account_id or
                              get_or_create_account("5800", "Scrap/Write-off", "expense").id)
            _post_voucher_journal("SCRAP", voucher, [
                {"account_id": charge_acct_id,
                 "debit": total_value, "credit": 0,
                 "description": f"Scrap: {voucher.reason}"},
                {"account_id": get_or_create_account("1200","Inventory","asset").id,
                 "debit": 0, "credit": total_value,
                 "description": "Stock scrapped at historic cost"},
            ])
            voucher.approved_by = current_user.id
            voucher.approved_at = datetime.utcnow()

        voucher.status = status
        db.session.commit()
        flash(f"Scrap voucher {voucher.voucher_number} saved.", "success")
        return redirect(url_for("inv_vouchers.scrap_list"))

    return render_template("vouchers/scrap_form.html", voucher=voucher,
                           **_form_ctx())


@inv_vouchers_bp.route("/scrap/list")
@login_required
def scrap_list():
    vouchers = ScrapVoucher.query.order_by(ScrapVoucher.id.desc()).all()
    return render_template("vouchers/scrap_list.html", vouchers=vouchers)


@inv_vouchers_bp.route("/scrap/<int:id>/delete", methods=["POST"])
@login_required
def scrap_delete(id):
    if deny_page("scrap_vouchers", "delete"):
        return redirect(url_for("inv_vouchers.scrap_list"))
    v = scoped_get_404(ScrapVoucher, id, for_update=True)
    if v.status == "approved":
        flash("Cannot delete an approved voucher. Unapprove it first.", "error")
        return redirect(url_for("inv_vouchers.scrap_list"))
    db.session.delete(v)
    db.session.commit()
    flash("Scrap voucher deleted.", "success")
    return redirect(url_for("inv_vouchers.scrap_list"))


@inv_vouchers_bp.route("/scrap/<int:id>/unapprove", methods=["POST"])
@login_required
def scrap_unapprove(id):
    if deny_page("scrap_vouchers", "approve"):
        return redirect(url_for("inv_vouchers.scrap_list"))
    v = scoped_get_404(ScrapVoucher, id, for_update=True)
    if v.status != "approved":
        flash("Voucher is not approved.", "error")
        return redirect(url_for("inv_vouchers.scrap_list"))
    from shared.ledger_utils import reverse_journal_entry
    reverse_journal_entry("SCRAP", v.id, created_by=current_user.id)
    if "SCRAP" == "ADJ":
        # A stock take's adjustment posted its journal under the take.
        take = StockTake.query.filter_by(adjustment_voucher_id=v.id).first()
        if take is not None:
            reverse_journal_entry("ST", take.id, created_by=current_user.id)
            take.status = "in_progress"
            take.adjustment_voucher_id = None
    # Later issues of the same products are re-costed in date order and any
    # change is journalled (shared/costing.py).
    reverse_voucher_stock("SCRAP", v.id, created_by=current_user.id)
    v.status = "unapproved"
    v.approved_by = None
    v.approved_at = None
    db.session.commit()
    flash(f"{v.voucher_number} unapproved and stock reversed.", "success")
    return redirect(url_for("inv_vouchers.scrap_list"))


# ─────────────────────────────────────────────
# Stock Adjustment Voucher
# ─────────────────────────────────────────────

@inv_vouchers_bp.route("/adjustment", methods=["GET", "POST"])
@inv_vouchers_bp.route("/adjustment/<int:id>", methods=["GET", "POST"])
@login_required
def adjustment_form(id=None):
    # A save or approval locks the voucher until commit: two users approving
    # it at once post it once (the second finds it approved).
    voucher = scoped_get(StockAdjustmentVoucher, id, for_update=request.method == "POST") if id else None
    if id and voucher is None:
        flash("Voucher not found.", "error")
        return redirect(url_for("inv_vouchers.adjustment_list"))
    if request.method == "POST":
        if deny_page("adjustment_vouchers", "edit" if voucher else "create"):
            return redirect(url_for("inv_vouchers.adjustment_list"))
        locked = _locked(voucher, "inv_vouchers.adjustment_list")
        if locked:
            return locked
        status = request.form.get("status", "unapproved")
        if status == "approved" and deny_page("adjustment_vouchers", "approve"):
            return redirect(url_for("inv_vouchers.adjustment_list"))
        is_new = voucher is None
        if is_new:
            voucher = StockAdjustmentVoucher(
                voucher_number=VoucherNumber.next("ADJ"),
                created_by=current_user.id
            )
            db.session.add(voucher)
        else:
            StockAdjustmentItem.query.filter_by(voucher_id=voucher.id).delete()
            db.session.flush()

        voucher.date = parse_doc_date(request.form.get("date"), fallback=voucher.date)
        voucher.reason = request.form.get("reason", "")
        voucher.label_id = request.form.get("label_id", type=int)

        db.session.flush()

        products = request.form.getlist("product_id[]")
        phys_qtys = request.form.getlist("physical_qty[]")
        if status == "approved":
            # Locked before the system quantity is read, so a sale posted at
            # the same moment cannot slip between the count and the posting.
            lock_products(int(p) for p in products if p and p.strip())
        new_items = []
        for i, pid in enumerate(products):
            if not pid or not pid.strip():
                continue
            # The system quantity is the ledger's, at the voucher's date — not
            # the figure the form showed when it was opened. Stock that moved
            # since (another user's sale, a late purchase) used to make the
            # adjustment post the wrong difference.
            sq = on_hand_at(int(pid), voucher.date)
            pq = Decimal(str(float(phys_qtys[i]) if i < len(phys_qtys) else 0))
            diff = pq - sq
            if diff == 0:
                continue
            cost, line_total = cost_of_issue(int(pid), abs(diff))
            item = StockAdjustmentItem(
                voucher_id=voucher.id, product_id=int(pid),
                product_name=(_resolve_product(int(pid)).name if _resolve_product(int(pid))
                              else f"Product #{pid}"),
                system_qty=sq, physical_qty=pq,
                difference=diff, unit_cost=cost, total_cost=line_total
            )
            db.session.add(item)
            new_items.append(item)

        if not new_items:
            db.session.rollback()
            flash("No items with quantity differences to adjust.", "error")
            return render_template("vouchers/adjustment_form.html",
                                   voucher=None if is_new else voucher, **_form_ctx())

        if status == "approved":
            db.session.flush()
            total_debit = 0
            total_credit = 0
            inv_acct = get_or_create_account("1200", "Inventory", "asset").id
            adj_acct = get_or_create_account("5900", "Inventory Adjustment", "expense").id
            jlines = []
            for item in new_items:
                if item.difference > 0:
                    # Excess found — book it in at the current valuation cost.
                    # The journal takes the ledger row's own 2dp total: the
                    # 4dp qty x cost product is what used to put the
                    # inventory account a few paisa off the stock valuation.
                    unit = current_unit_cost(item.product_id)
                    row = record_in(item.product_id, "ADJ", voucher.id,
                                    voucher.voucher_number,
                                    qty=float(item.difference), unit_cost=unit,
                                    notes=f"Adjustment: {voucher.reason}",
                                    created_by=current_user.id,
                                    txn_date=voucher.date)
                    item.unit_cost = row.unit_cost
                    item.total_cost = row.total_cost
                else:
                    unit, total = record_out(
                        item.product_id, "ADJ", voucher.id,
                        voucher.voucher_number,
                        qty=float(abs(item.difference)),
                        notes=f"Adjustment: {voucher.reason}",
                        created_by=current_user.id,
                        txn_date=voucher.date)
                    item.unit_cost, item.total_cost = unit, total
                val = float(item.total_cost)
                if item.difference > 0:
                    jlines.append({"account_id": inv_acct, "debit": val, "credit": 0})
                    jlines.append({"account_id": adj_acct, "debit": 0, "credit": val})
                    total_debit += val
                else:
                    jlines.append({"account_id": adj_acct, "debit": val, "credit": 0})
                    jlines.append({"account_id": inv_acct, "debit": 0, "credit": val})
                    total_credit += val
            _post_voucher_journal("ADJ", voucher, jlines)
            voucher.approved_by = current_user.id
            voucher.approved_at = datetime.utcnow()

        voucher.status = status
        db.session.commit()
        flash(f"Adjustment voucher {voucher.voucher_number} saved.", "success")
        return redirect(url_for("inv_vouchers.adjustment_list"))

    return render_template("vouchers/adjustment_form.html", voucher=voucher,
                           **_form_ctx())


@inv_vouchers_bp.route("/adjustment/list")
@login_required
def adjustment_list():
    vouchers = StockAdjustmentVoucher.query.order_by(StockAdjustmentVoucher.id.desc()).all()
    return render_template("vouchers/adjustment_list.html", vouchers=vouchers)


@inv_vouchers_bp.route("/adjustment/<int:id>/delete", methods=["POST"])
@login_required
def adjustment_delete(id):
    if deny_page("adjustment_vouchers", "delete"):
        return redirect(url_for("inv_vouchers.adjustment_list"))
    v = scoped_get_404(StockAdjustmentVoucher, id, for_update=True)
    if v.status == "approved":
        flash("Cannot delete an approved voucher. Unapprove it first.", "error")
        return redirect(url_for("inv_vouchers.adjustment_list"))
    db.session.delete(v)
    db.session.commit()
    flash("Adjustment voucher deleted.", "success")
    return redirect(url_for("inv_vouchers.adjustment_list"))


@inv_vouchers_bp.route("/adjustment/<int:id>/unapprove", methods=["POST"])
@login_required
def adjustment_unapprove(id):
    if deny_page("adjustment_vouchers", "approve"):
        return redirect(url_for("inv_vouchers.adjustment_list"))
    v = scoped_get_404(StockAdjustmentVoucher, id, for_update=True)
    if v.status != "approved":
        flash("Voucher is not approved.", "error")
        return redirect(url_for("inv_vouchers.adjustment_list"))
    from shared.ledger_utils import reverse_journal_entry
    reverse_journal_entry("ADJ", v.id, created_by=current_user.id)
    if "ADJ" == "ADJ":
        # A stock take's adjustment posted its journal under the take.
        take = StockTake.query.filter_by(adjustment_voucher_id=v.id).first()
        if take is not None:
            reverse_journal_entry("ST", take.id, created_by=current_user.id)
            take.status = "in_progress"
            take.adjustment_voucher_id = None
    # Later issues of the same products are re-costed in date order and any
    # change is journalled (shared/costing.py).
    reverse_voucher_stock("ADJ", v.id, created_by=current_user.id)
    v.status = "unapproved"
    v.approved_by = None
    v.approved_at = None
    db.session.commit()
    flash(f"{v.voucher_number} unapproved and stock reversed.", "success")
    return redirect(url_for("inv_vouchers.adjustment_list"))


# ─────────────────────────────────────────────
# Stock Take
# ─────────────────────────────────────────────

@inv_vouchers_bp.route("/stock-take", methods=["GET", "POST"])
@inv_vouchers_bp.route("/stock-take/<int:id>", methods=["GET", "POST"])
@login_required
def stock_take_form(id=None):
    # A save or approval locks the voucher until commit: two users approving
    # it at once post it once (the second finds it approved).
    st = scoped_get(StockTake, id, for_update=request.method == "POST") if id else None
    if id and st is None:
        flash("Stock take not found.", "error")
        return redirect(url_for("inv_vouchers.stock_take_list"))
    if request.method == "POST":
        if deny_page("stock_take_vouchers", "edit" if st else "create"):
            return redirect(url_for("inv_vouchers.stock_take_list"))
        if st is not None and st.status == "approved":
            flash(f"{st.reference} is approved and posted. Unapprove its "
                  f"adjustment voucher to make changes.", "error")
            return redirect(url_for("inv_vouchers.stock_take_list"))
        is_new = st is None
        if is_new:
            st = StockTake(
                # A sequence, not count()+1: deleting a take made count()+1
                # re-issue an existing reference and the insert failed.
                reference=_next_take_reference(),
                created_by=current_user.id
            )
            db.session.add(st)
        else:
            StockTakeItem.query.filter_by(stock_take_id=st.id).delete()
            db.session.flush()

        st.date = parse_doc_date(request.form.get("date"), fallback=st.date)
        st.location = request.form.get("location", "")
        status = request.form.get("status", "in_progress")

        db.session.flush()

        products = request.form.getlist("product_id[]")
        phys_q = request.form.getlist("physical_qty[]")
        adj_number = None
        if status == "approved":
            # The adjustment's number first, then the products — the order
            # every other stock document takes them in (a number series stays
            # locked until commit; the opposite order could deadlock against
            # an adjustment voucher being approved at the same moment).
            adj_number = VoucherNumber.next("ADJ")
            # Locked before the system quantity is read (see adjustment_form).
            lock_products(int(p) for p in products if p and p.strip())
        new_items = []
        for i, pid in enumerate(products):
            if not pid or not pid.strip():
                continue
            # Counted against the ledger's quantity at the take's date, not
            # the figure the form showed (see adjustment_form).
            sq = on_hand_at(int(pid), st.date)
            pq = Decimal(str(float(phys_q[i]) if i < len(phys_q) else 0))
            diff = pq - sq
            cost = _cost_from_ledger(int(pid))
            item = StockTakeItem(
                stock_take_id=st.id, product_id=int(pid),
                product_name=(_resolve_product(int(pid)).name if _resolve_product(int(pid))
                              else f"Product #{pid}"),
                system_qty=sq, physical_qty=pq,
                difference=diff, unit_cost=cost
            )
            db.session.add(item)
            new_items.append(item)

        if not new_items:
            flash("Add at least one product to the stock take.", "error")
            db.session.rollback()
            return render_template("vouchers/stock_take_form.html",
                                   st=None if is_new else st, **_form_ctx())

        if status == "approved":
            db.session.flush()
            adj = StockAdjustmentVoucher(
                voucher_number=adj_number,
                date=st.date,
                reason=f"Stock Take: {st.reference} ({st.location})",
                status="approved",
                created_by=current_user.id,
                approved_by=current_user.id,
                approved_at=datetime.utcnow()
            )
            db.session.add(adj)
            db.session.flush()
            st.adjustment_voucher_id = adj.id
            st.approved_by = current_user.id
            st.approved_at = datetime.utcnow()

            inv_acct = get_or_create_account("1200", "Inventory", "asset").id
            adj_acct = get_or_create_account("5900", "Inventory Adjustment", "expense").id
            jlines = []
            for item in new_items:
                diff = item.difference
                if diff == 0:
                    continue
                ttype = "IN" if diff > 0 else "OUT"
                cost = float(item.unit_cost)
                val = abs(float(diff)) * cost
                adj_item = StockAdjustmentItem(
                    voucher_id=adj.id, product_id=item.product_id,
                    product_name=item.product_name,
                    system_qty=item.system_qty, physical_qty=item.physical_qty,
                    difference=diff, unit_cost=item.unit_cost,
                    total_cost=Decimal(str(val))
                )
                db.session.add(adj_item)

                if diff > 0:
                    unit = current_unit_cost(item.product_id)
                    row = record_in(item.product_id, "ADJ", adj.id,
                                    adj.voucher_number,
                                    qty=float(diff), unit_cost=unit,
                                    notes=f"Stock Take {st.reference} adjustment ({st.location})",
                                    created_by=current_user.id,
                                    txn_date=st.date)
                    val = float(row.total_cost)
                    adj_item.unit_cost, adj_item.total_cost = row.unit_cost, row.total_cost
                else:
                    unit, total = record_out(
                        item.product_id, "ADJ", adj.id,
                        adj.voucher_number,
                        qty=float(abs(diff)),
                        notes=f"Stock Take {st.reference} adjustment ({st.location})",
                        created_by=current_user.id,
                        txn_date=st.date)
                    val = float(total)
                    adj_item.unit_cost, adj_item.total_cost = unit, total
                if diff > 0:
                    jlines.append({"account_id": inv_acct, "debit": val, "credit": 0})
                    jlines.append({"account_id": adj_acct, "debit": 0, "credit": val})
                else:
                    jlines.append({"account_id": adj_acct, "debit": val, "credit": 0})
                    jlines.append({"account_id": inv_acct, "debit": 0, "credit": val})
            # Posted under the ADJUSTMENT voucher that carries the stock rows,
            # so unapproving that voucher reverses stock and journal together
            # (the journal used to sit under the take, and unapproving the
            # adjustment reversed the stock while the journal stayed posted).
            if jlines:
                _post_voucher_journal("ADJ", adj, jlines, entry_date=st.date)

        st.status = status
        db.session.commit()
        flash(f"Stock take {st.reference} saved.", "success")
        return redirect(url_for("inv_vouchers.stock_take_list"))

    return render_template("vouchers/stock_take_form.html", st=st, **_form_ctx())


@inv_vouchers_bp.route("/stock-take/list")
@login_required
def stock_take_list():
    takes = StockTake.query.order_by(StockTake.id.desc()).all()
    return render_template("vouchers/stock_take_list.html", takes=takes)


@inv_vouchers_bp.route("/stock-take/<int:id>/delete", methods=["POST"])
@login_required
def stock_take_delete(id):
    if deny_page("stock_take_vouchers", "delete"):
        return redirect(url_for("inv_vouchers.stock_take_list"))
    st = scoped_get_404(StockTake, id, for_update=True)
    if st.status == "approved":
        flash("Cannot delete an approved stock take.", "error")
        return redirect(url_for("inv_vouchers.stock_take_list"))
    db.session.delete(st)
    db.session.commit()
    flash("Stock take deleted.", "success")
    return redirect(url_for("inv_vouchers.stock_take_list"))


# ─────────────────────────────────────────────
# Product Ledger (per-product stock ledger view)
# ─────────────────────────────────────────────

@inv_vouchers_bp.route("/product-ledger")
@login_required
def product_ledger():
    from ..models.product import InvProduct
    product_id = request.args.get("product_id", type=int)
    products = InvProduct.query.filter_by(is_active=True).order_by(InvProduct.name).all()
    product = None
    entries = []
    if product_id:
        product = scoped_get(InvProduct, product_id)
        entries = (StockLedger.query.filter_by(product_id=product_id)
                   .order_by(StockLedger.txn_date, StockLedger.id).all())
    from shared.models.stock_layer import StockCostAdjustment
    adjustments = (StockCostAdjustment.query.filter_by(product_id=product_id)
                   .order_by(StockCostAdjustment.id.desc()).all()
                   if product_id else [])
    fmt = request.args.get("format")
    if fmt and product:
        from ..routes.reports import export_stock_ledger
        return export_stock_ledger(fmt, product, entries, adjustments)
    return render_template("vouchers/product_ledger.html",
                           products=products, product=product,
                           entries=entries, selected_id=product_id,
                           adjustments=adjustments)


@inv_vouchers_bp.route("/product-ledger/list")
@login_required
def product_ledger_list():
    """List all products as sub-ledgers of Inventory account"""
    from ..models.product import InvProduct
    products = InvProduct.query.filter_by(is_active=True).order_by(InvProduct.name).all()
    rows = []
    for p in products:
        bal = StockLedger.get_running_balance(p.id)
        rows.append({
            "id": p.id, "sku": p.sku, "name": p.name,
            "qty": float(bal[0]), "cost": float(bal[2]),
            # The ledger's carried value, not qty x the rounded average.
            "value": round(float(bal[1]), 2),
            "unit": p.unit
        })
    fmt = request.args.get("format")
    if fmt:
        from datetime import date
        from shared import report_export as rx
        data = [[r["sku"], r["name"], r["unit"] or "", r["qty"], r["cost"], r["value"]]
                for r in rows]
        data.append(["", "Total", "", "", "", round(sum(r["value"] for r in rows), 2)])
        return rx.export_table(fmt, "Product Sub-Ledgers",
                               ["SKU", "Product", "Unit", "Qty", "Avg Cost", "Value"],
                               data, period=f"Balances as at {date.today():%d %b %Y}",
                               row_kinds=["plain"] * len(rows) + ["grand"],
                               col_formats={3: rx.QTY_FMT}, file_period=date.today())
    return render_template("vouchers/product_ledger_list.html", rows=rows)


# ─────────────────────────────────────────────
# Voucher Preview / Print
# ─────────────────────────────────────────────

@inv_vouchers_bp.route("/preview/<vtype>/<int:vid>")
@login_required
def voucher_preview(vtype, vid):
    voucher = None
    title = "Voucher"
    normalized_items = []
    extra_fields = {}

    if vtype == "CONS":
        from shared.models.vouchers import ConsumptionVoucher
        voucher = scoped_get_404(ConsumptionVoucher, vid)
        title = "Consumption Voucher"
        for it in voucher.items:
            normalized_items.append({
                "product": it.product_name, "qty": it.quantity,
                "unit_cost": it.unit_cost, "total": it.total_cost
            })
        extra_fields = {"Department": voucher.department, "Reason": voucher.reason}

    elif vtype == "SCRAP":
        from shared.models.vouchers import ScrapVoucher
        voucher = scoped_get_404(ScrapVoucher, vid)
        title = "Scrap Voucher"
        for it in voucher.items:
            normalized_items.append({
                "product": it.product_name, "qty": it.quantity,
                "unit_cost": it.unit_cost, "total": it.total_cost
            })
        extra_fields = {"Reason": voucher.reason}

    elif vtype == "ADJ":
        from shared.models.vouchers import StockAdjustmentVoucher
        voucher = scoped_get_404(StockAdjustmentVoucher, vid)
        title = "Stock Adjustment Voucher"
        for it in voucher.items:
            normalized_items.append({
                "product": it.product_name, "qty": it.difference,
                "system_qty": it.system_qty, "physical_qty": it.physical_qty,
                "diff": it.difference, "unit_cost": it.unit_cost, "total": it.total_cost
            })
        extra_fields = {"Reason": voucher.reason}

    elif vtype == "PI":
        from ..models.purchase_invoice import InvPurchaseInvoice
        voucher = scoped_get_404(InvPurchaseInvoice, vid)
        title = "Purchase Invoice"
        for it in voucher.items:
            normalized_items.append({
                "product": it.product.name if it.product else it.description,
                "qty": it.quantity, "unit_cost": it.unit_price,
                "total": it.total_after_discount or it.quantity * it.unit_price
            })
        extra_fields = {"Supplier": voucher.supplier.name if voucher.supplier else "",
                        "Net Payable": voucher.net_payable}

    elif vtype == "PR":
        from ..models.purchase_return import InvPurchaseReturn
        voucher = scoped_get_404(InvPurchaseReturn, vid)
        title = "Purchase Return"
        for it in voucher.items:
            normalized_items.append({
                "product": it.product.name if it.product else it.description,
                "qty": it.current_return_qty, "unit_cost": it.unit_price,
                "total": it.net_return_value or it.current_return_qty * it.unit_price
            })
        extra_fields = {"Supplier": voucher.supplier.name if voucher.supplier else "",
                        "Net Return": voucher.net_return_amount}

    else:
        flash("Unknown voucher type.", "error")
        return redirect(url_for("inv_vouchers.consumption_list"))

    return render_template("vouchers/voucher_preview.html",
                           voucher=voucher, vtype=vtype, title=title,
                           items=normalized_items, extra_fields=extra_fields)


# ─────────────────────────────────────────────
# API: Product autocomplete
# ─────────────────────────────────────────────

@inv_vouchers_bp.route("/api/products")
@login_required
def api_products():
    from ..models.product import InvProduct
    q = request.args.get("q", "").strip()
    if not q:
        products = InvProduct.query.filter_by(is_active=True).order_by(InvProduct.name).limit(20).all()
    else:
        products = InvProduct.query.filter(
            InvProduct.is_active == True,
            (InvProduct.name.ilike(f"%{q}%") | InvProduct.sku.ilike(f"%{q}%"))
        ).limit(20).all()
    return jsonify([{
        "id": p.id, "name": p.name, "sku": p.sku,
        "stock": float(p.current_stock)
    } for p in products])


@inv_vouchers_bp.route("/api/product-stock/<int:pid>")
@login_required
def api_product_stock(pid):
    return jsonify({
        "product_id": pid,
        "qty": float(on_hand(pid)),
        "cost": float(current_unit_cost(pid)),
    })
