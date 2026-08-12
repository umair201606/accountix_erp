"""Invoicing > Tracking — the payment feed, AR/AP tracking and assignment.

Every figure on these pages is derived from two sources that already exist: the
approved accounting vouchers (the money) and the approved invoices (the debt).
Assignment records the link between them and nothing else — see
``shared/payment_tracking.py`` for why no journal entry is ever written here.
"""

from datetime import datetime

from flask import (Blueprint, render_template, request, jsonify, redirect,
                   url_for, flash)
from flask_login import login_required, current_user

from inventory_app.models.customer import InvCustomer
from inventory_app.models.supplier import InvSupplier
from shared.permissions import deny_json, deny_page
from shared import payment_tracking as pt
from shared.payment_tracking import AllocationError, SALES, PURCHASE

inv_track_bp = Blueprint("inv_tracking", __name__,
                         url_prefix="/invoicing/tracking")

RESOURCE = "payment_tracking"

DOC_META = {
    SALES: {
        "title": "Sales Invoice Tracking",
        "kicker": "Invoicing / Receivables",
        "blurb": "Every approved sales invoice with what has been received "
                 "against it, what is still outstanding, and how late it is.",
        "party_label": "Customer",
        "party_plural": "customers",
        "money_word": "Received",
        "open_word": "Receivable",
        "endpoint": "inv_tracking.sales",
        "flow": "receipt",
    },
    PURCHASE: {
        "title": "Purchase Invoice Tracking",
        "kicker": "Invoicing / Payables",
        "blurb": "Every approved purchase invoice with what has been paid "
                 "against it, what is still outstanding, and how late it is.",
        "party_label": "Supplier",
        "party_plural": "suppliers",
        "money_word": "Paid",
        "open_word": "Payable",
        "endpoint": "inv_tracking.purchases",
        "flow": "payment",
    },
}


def _parse_date(value):
    value = (value or "").strip()
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def _eod(dt):
    """End of the given day, so a "to" filter includes that day's documents."""
    return dt.replace(hour=23, minute=59, second=59) if dt else None


def _split_party(raw):
    """``"customer:12"`` -> ``("customer", 12)``; anything else -> ``("", None)``."""
    kind, _, ident = (raw or "").partition(":")
    if kind not in ("customer", "supplier") or not ident.isdigit():
        return "", None
    return kind, int(ident)


def _selected(name, allowed, default=None):
    """Multi-select filter values from the query string.

    ``?state=`` (present but empty) means "all" — that is how the Clear action
    and the "All" chip switch a default off, which a plain getlist could not
    distinguish from an absent parameter.
    """
    if name not in request.args:
        return list(default or [])
    picked = [v for v in request.args.getlist(name) if v in allowed]
    return picked


# ── Feed ────────────────────────────────────────────────────────────────────

@inv_track_bp.route("/")
@login_required
def feed():
    if deny_page(RESOURCE, "view"):
        return redirect(url_for("invoicing.dashboard"))

    # Everything that still needs a decision by default; settled items are one
    # chip away.
    states = _selected("state", pt.ASSIGN_STATES, pt.DEFAULT_ASSIGN_STATES)
    flow = request.args.get("flow", "")
    flow = flow if flow in ("receipt", "payment") else ""
    date_from = _parse_date(request.args.get("from"))
    date_to = _eod(_parse_date(request.args.get("to")))
    search = request.args.get("q", "").strip()

    # One picker lists both sides ("customer:12" / "supplier:4"), because the
    # user thinks "show me Acme's money", not "show me a customer, then which".
    party_raw = request.args.get("party", "")
    party_type, party_id = _split_party(party_raw)
    # The chosen party settles exactly one side of the books, so an incompatible
    # direction chip is dropped rather than returning a silently empty feed.
    if party_type:
        flow = "receipt" if party_type == "customer" else "payment"

    rows = pt.feed_rows(assign_states=states or None, flow=flow or None,
                        party_type=party_type or None, party_id=party_id,
                        date_from=date_from, date_to=date_to,
                        search=search or None)
    return render_template(
        "tracking/feed.html",
        rows=rows,
        summary=pt.feed_summary(),
        drafts=pt.draft_settlement_count(),
        customers=InvCustomer.query.order_by(InvCustomer.name).all(),
        suppliers=InvSupplier.query.order_by(InvSupplier.name).all(),
        filters={"state": states, "flow": flow, "party": party_raw,
                 "from": request.args.get("from", ""),
                 "to": request.args.get("to", ""), "q": search},
        can_assign=current_user.can(RESOURCE, "edit"),
    )


@inv_track_bp.route("/assign/<int:line_id>")
@login_required
def assign_workspace(line_id):
    """The allocation workspace for one receipt/payment."""
    if deny_page(RESOURCE, "view"):
        return redirect(url_for("invoicing.dashboard"))
    row = pt.feed_row(line_id)
    if row is None:
        flash("That payment is not an approved customer receipt or supplier "
              "payment, so it cannot be assigned.", "error")
        return redirect(url_for("inv_tracking.feed"))
    return render_template(
        "tracking/assign.html",
        row=row,
        invoices=pt.open_invoices(row["party_type"], row["party_id"], line_id),
        splits=pt.history_for_line(line_id),
        doc_meta=DOC_META[row["doc_type"]],
        can_assign=current_user.can(RESOURCE, "edit"),
    )


# ── Invoice tracking (sales / purchase) ─────────────────────────────────────

def _tracking_page(doc_type):
    if deny_page(RESOURCE, "view"):
        return redirect(url_for("invoicing.dashboard"))
    meta = DOC_META[doc_type]
    pay_states = _selected("pay", pt.PAY_STATES)
    buckets = _selected("age", [k for k, _l, _lo, _hi in pt.AGE_BUCKETS])
    overdue_only = request.args.get("overdue") == "1"
    party_id = request.args.get("party_id", type=int)
    search = request.args.get("q", "").strip()
    date_from = _parse_date(request.args.get("from"))
    date_to = _eod(_parse_date(request.args.get("to")))

    rows = pt.invoice_rows(doc_type, party_id=party_id,
                           pay_states=pay_states or None,
                           age_buckets=buckets or None,
                           overdue_only=overdue_only,
                           search=search or None,
                           date_from=date_from, date_to=date_to)
    parties = (InvCustomer.query.order_by(InvCustomer.name).all()
               if doc_type == SALES
               else InvSupplier.query.order_by(InvSupplier.name).all())
    # Money already in the books for this party but not yet assigned to any of
    # its invoices — the row shows it so an unpaid invoice that has actually
    # been paid is visible as a matching job, not as a debt.
    waiting = {}
    for item in pt.feed_rows(assign_states=("unassigned", "partial"),
                             party_type=pt.PARTY_FOR_DOC[doc_type]):
        waiting[item["party_id"]] = waiting.get(item["party_id"], 0.0) \
            + item["unassigned"]
    return render_template(
        "tracking/invoices.html",
        doc_type=doc_type, doc_meta=meta, rows=rows,
        summary=pt.outstanding_summary(doc_type),
        buckets_meta=pt.AGE_BUCKETS,
        parties=parties,
        filters={"pay": pay_states, "age": buckets,
                 "overdue": "1" if overdue_only else "",
                 "party_id": party_id, "q": search,
                 "from": request.args.get("from", ""),
                 "to": request.args.get("to", "")},
        waiting=waiting,
    )


@inv_track_bp.route("/sales")
@login_required
def sales():
    return _tracking_page(SALES)


@inv_track_bp.route("/purchases")
@login_required
def purchases():
    return _tracking_page(PURCHASE)


@inv_track_bp.route("/invoice/<doc_type>/<int:doc_id>")
@login_required
def invoice_history(doc_type, doc_id):
    """One invoice: its settlement progress and every receipt behind it."""
    if doc_type not in (SALES, PURCHASE):
        flash("Unknown invoice type.", "error")
        return redirect(url_for("inv_tracking.feed"))
    if deny_page(RESOURCE, "view"):
        return redirect(url_for("invoicing.dashboard"))
    row = pt.invoice_row(doc_type, doc_id)
    if row is None:
        flash("Invoice not found.", "error")
        return redirect(url_for("inv_tracking.sales" if doc_type == SALES
                                else "inv_tracking.purchases"))
    unassigned = pt.feed_rows(assign_states=("unassigned", "partial"),
                              party_type=row["party_type"],
                              party_id=row["party_id"])
    return render_template(
        "tracking/invoice_history.html",
        doc_type=doc_type, doc_meta=DOC_META[doc_type], row=row,
        history=pt.history_for_doc(doc_type, doc_id),
        available=unassigned,
        edit_url=url_for("inv_invoices.invoice_form", id=doc_id)
        if doc_type == SALES
        else url_for("inv_purchase_invoice.invoice_form", id=doc_id),
        can_assign=current_user.can(RESOURCE, "edit"),
    )


# ── JSON API (the workspace and the inline assign drawer) ───────────────────

@inv_track_bp.route("/api/feed-item/<int:line_id>")
@login_required
def api_feed_item(line_id):
    denied = deny_json(RESOURCE, "view")
    if denied:
        return denied
    row = pt.feed_row(line_id)
    if row is None:
        return jsonify({"ok": False, "error": "Not an assignable payment."}), 404
    return jsonify({
        "ok": True, "row": row,
        "invoices": pt.open_invoices(row["party_type"], row["party_id"], line_id),
        "splits": pt.history_for_line(line_id),
    })


@inv_track_bp.route("/api/auto-split/<int:line_id>")
@login_required
def api_auto_split(line_id):
    denied = deny_json(RESOURCE, "view")
    if denied:
        return denied
    return jsonify({"ok": True, "split": pt.auto_split(line_id)})


@inv_track_bp.route("/api/assign/<int:line_id>", methods=["POST"])
@login_required
def api_assign(line_id):
    denied = deny_json(RESOURCE, "edit")
    if denied:
        return denied
    data = request.get_json(silent=True) or {}
    try:
        row = pt.allocate(line_id, data.get("entries") or [],
                          user_id=current_user.id,
                          note=(data.get("note") or "").strip())
    except AllocationError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True, "row": row,
                    "splits": pt.history_for_line(line_id),
                    "message": "Assignment saved"})


@inv_track_bp.route("/api/force-close/<int:line_id>", methods=["POST"])
@login_required
def api_force_close(line_id):
    """Close a receipt/payment against no invoice, with a stated reason."""
    denied = deny_json(RESOURCE, "edit")
    if denied:
        return denied
    data = request.get_json(silent=True) or {}
    try:
        row = pt.force_close(line_id, amount=data.get("amount"),
                             reason=(data.get("reason") or "").strip(),
                             user_id=current_user.id)
    except AllocationError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True, "row": row,
                    "splits": pt.history_for_line(line_id),
                    "message": "Closed as not applicable"})


@inv_track_bp.route("/api/release-force/<int:line_id>", methods=["POST"])
@login_required
def api_release_force(line_id):
    denied = deny_json(RESOURCE, "edit")
    if denied:
        return denied
    try:
        row = pt.release_force(line_id)
    except AllocationError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True, "row": row,
                    "splits": pt.history_for_line(line_id),
                    "message": "Reopened for assignment"})


@inv_track_bp.route("/api/unassign/<int:alloc_id>", methods=["POST"])
@login_required
def api_unassign(alloc_id):
    denied = deny_json(RESOURCE, "edit")
    if denied:
        return denied
    try:
        result = pt.unallocate(alloc_id)
    except AllocationError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True, "message": "Assignment removed", **result})


@inv_track_bp.route("/api/open-invoices")
@login_required
def api_open_invoices():
    denied = deny_json(RESOURCE, "view")
    if denied:
        return denied
    party_type = request.args.get("party_type", "customer")
    if party_type not in ("customer", "supplier"):
        return jsonify({"ok": False, "error": "Unknown party type."}), 400
    party_id = request.args.get("party_id", type=int)
    if not party_id:
        return jsonify({"ok": False, "error": "Party is required."}), 400
    line_id = request.args.get("line_id", type=int)
    return jsonify({"ok": True,
                    "invoices": pt.open_invoices(party_type, party_id, line_id)})
