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
    """``"customer:12"`` -> ``("customer", 12)``; anything else -> ``("", None)``.

    Negative ids are valid: they are the ledger parties (a whole subledger
    account standing in for a payer that has no customer/supplier record), so
    a plain ``isdigit()`` would silently drop the ledger filter.
    """
    kind, _, ident = (raw or "").partition(":")
    if kind not in ("customer", "supplier"):
        return "", None
    ident = ident.strip()
    digits = ident[1:] if ident.startswith("-") else ident
    if not digits.isdigit():
        return "", None
    return kind, int(ident)


PER_PAGE = 100


def _page(rows):
    """One page of ``rows`` (``?page=``, 100 a page) and where it sits. Every
    row is still computed — the tiles and totals need them all — but the
    browser is sent a page: 2,760 invoices or 1,900 receipts rendered at once
    took five seconds and scrolled forever on a phone."""
    total = len(rows)
    pages = max(1, -(-total // PER_PAGE))
    page = min(max(request.args.get("page", 1, type=int) or 1, 1), pages)
    start = (page - 1) * PER_PAGE
    return rows[start:start + PER_PAGE], {
        "page": page, "pages": pages, "total": total,
        "first": start + 1 if total else 0, "last": min(start + PER_PAGE, total)}


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

    # Two controls, one idea. "Side" narrows to a whole subledger (every
    # customer, or every supplier); the picker below it names one party. They
    # are kept in step rather than left to contradict each other:
    #
    #   * naming a party implies its side, so the side follows the picker;
    #   * changing the side to the other subledger strands the named party, so
    #     the party is cleared instead of filtering to an empty feed.
    side = request.args.get("side", "")
    side = side if side in ("customer", "supplier") else ""

    party_raw = request.args.get("party", "")
    party_type, party_id = _split_party(party_raw)
    if party_type and side and party_type != side:
        party_raw, party_type, party_id = "", "", None
    if party_type:
        side = party_type

    # The side settles exactly one direction of the books, so an incompatible
    # direction chip is dropped rather than returning a silently empty feed.
    if side:
        flow = "receipt" if side == "customer" else "payment"

    # One pass over the books: every row once, then narrowed here. The tiles
    # count the whole feed, the list shows the filtered page.
    all_rows, stale = pt.feed_snapshot()
    rows = pt.filter_feed(all_rows, assign_states=states or None, flow=flow or None,
                          party_type=side or None, party_id=party_id,
                          date_from=date_from, date_to=date_to, search=search or None)
    for r in all_rows:
        # Whole-ledger payers have no invoices: they are closed, never assigned.
        r["is_ledger"] = pt.is_ledger_party(r["party_id"])
    # What "Match oldest first" would use: the open money of the chosen party
    # (or side, or everyone) — the date and text filters do not narrow it, so
    # the count beside the button must not either.
    matchable = [r for r in pt.filter_feed(all_rows, assign_states=("unassigned", "partial"),
                                           party_type=side or None, party_id=party_id)
                 if not r["is_ledger"]]
    page_rows, pager = _page(rows)
    return render_template(
        "tracking/feed.html",
        rows=page_rows,
        pager=pager,
        matchable=len(matchable),
        # Money in and money out are never added together.
        match_in=[sum(1 for r in matchable if r["flow"] == "receipt"),
                  sum(r["unassigned"] for r in matchable if r["flow"] == "receipt")],
        match_out=[sum(1 for r in matchable if r["flow"] == "payment"),
                   sum(r["unassigned"] for r in matchable if r["flow"] == "payment")],
        match_scope=(next((r["party_name"] for r in all_rows
                           if party_id and r["party_id"] == party_id), "")
                     or {"customer": "all customers", "supplier": "all suppliers"}.get(
                         side, "all customers and suppliers")),
        summary=pt.feed_summary(all_rows),
        drafts=pt.draft_settlement_count(),
        # Assignments that stopped holding because a voucher or invoice was
        # edited underneath them. Shown regardless of the state filter — it is
        # a data-integrity notice, not a feed row.
        stale=pt.stale_allocations(stale),
        customers=InvCustomer.query.order_by(InvCustomer.name).all(),
        suppliers=InvSupplier.query.order_by(InvSupplier.name).all(),
        # Whole-ledger payers (Trade Debtors/Creditors and their catch-alls),
        # which settle without a customer or supplier record behind them.
        ledger_customers=pt.ledger_parties("customer"),
        ledger_suppliers=pt.ledger_parties("supplier"),
        filters={"state": states, "flow": flow, "side": side,
                 "party": party_raw,
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
        flash("Only approved receipts and payments can be assigned.", "error")
        return redirect(url_for("inv_tracking.feed"))
    row["is_ledger"] = pt.is_ledger_party(row["party_id"])
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
    # Open invoices by default — what is still owed is what this page is for;
    # settled ones are one chip away. ``?pay=`` (present, empty) means all.
    pay_states = _selected("pay", pt.PAY_STATES, ("unpaid", "partial"))
    sort = request.args.get("sort", "newest")
    sort = sort if sort in SORTS else "newest"
    buckets = _selected("age", [k for k, _l, _lo, _hi in pt.AGE_BUCKETS])
    overdue_only = request.args.get("overdue") == "1"
    party_id = request.args.get("party_id", type=int)
    search = request.args.get("q", "").strip()
    date_from = _parse_date(request.args.get("from"))
    date_to = _eod(_parse_date(request.args.get("to")))

    # Every invoice once: the headline figures need them all, and the list
    # is narrowed from the same rows instead of a second pass over the books.
    all_rows = pt.invoice_rows(doc_type)
    rows = pt.filter_invoices(all_rows, party_id=party_id,
                              pay_states=pay_states or None,
                              age_buckets=buckets or None,
                              overdue_only=overdue_only,
                              search=search or None,
                              date_from=date_from, date_to=date_to)
    key, reverse = SORTS[sort][1]
    rows.sort(key=key, reverse=reverse)
    page_rows, pager = _page(rows)
    parties = (InvCustomer.query.order_by(InvCustomer.name).all()
               if doc_type == SALES
               else InvSupplier.query.order_by(InvSupplier.name).all())
    # Money already in the books for this party but not yet assigned to any of
    # its invoices — the row shows it so an unpaid invoice that has actually
    # been paid is visible as a matching job, not as a debt.
    waiting = {}
    for item in pt.feed_rows(assign_states=("unassigned", "partial"),
                             party_type=pt.PARTY_FOR_DOC[doc_type]):
        if pt.is_ledger_party(item["party_id"]):
            continue
        waiting[item["party_id"]] = waiting.get(item["party_id"], 0.0) \
            + item["unassigned"]
    return render_template(
        "tracking/invoices.html",
        doc_type=doc_type, doc_meta=meta, rows=page_rows, pager=pager,
        sort=sort, sorts=[(k, v[0]) for k, v in SORTS.items()],
        waiting_total=sum(waiting.values()), waiting_parties=len(waiting),
        summary=pt.outstanding_summary(doc_type, rows=all_rows),
        buckets_meta=pt.AGE_BUCKETS,
        parties=parties,
        filters={"pay": pay_states, "age": buckets,
                 "overdue": "1" if overdue_only else "",
                 "party_id": party_id, "q": search,
                 "from": request.args.get("from", ""),
                 "to": request.args.get("to", "")},
        waiting=waiting,
        can_assign=current_user.can(RESOURCE, "edit"),
    )


# Register orderings: label, (key, reverse).
SORTS = {
    "newest": ("Newest first", (lambda r: (r["date"] or datetime.min.date(), r["id"]), True)),
    "oldest": ("Oldest first", (lambda r: (r["date"] or datetime.min.date(), r["id"]), False)),
    "late": ("Most overdue", (lambda r: (r["overdue_days"], r["outstanding"]), True)),
    "owed": ("Largest outstanding", (lambda r: (r["outstanding"], r["overdue_days"]), True)),
}


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
        return_endpoint=("inv_sales_return.return_form" if doc_type == SALES
                         else "inv_purchase_return.return_form"),
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


@inv_track_bp.route("/api/auto-match", methods=["POST"])
@login_required
def api_auto_match():
    """Assign every open receipt/payment (optionally of one party) to its
    party's invoices, oldest invoice first."""
    denied = deny_json(RESOURCE, "edit")
    if denied:
        return denied
    data = request.get_json(silent=True) or {}
    party_type, party_id = _split_party(data.get("party", ""))
    side = data.get("side") if data.get("side") in ("customer", "supplier") else None
    result = pt.auto_match(party_type=party_type or side, party_id=party_id,
                           user_id=current_user.id)
    if not result["lines"]:
        msg = "Nothing to match: no open receipt or payment has an open invoice to settle."
    else:
        def count(n, word):
            return f"{n:,} {word}{'' if n == 1 else 's'}"
        what = " and ".join(count(n, w) for n, w in ((result["receipts"], "receipt"),
                                                    (result["payments"], "payment")) if n)
        msg = f"Matched {what} to {count(result['invoices'], 'invoice')}, oldest first."
    return jsonify({"ok": True, "message": msg, **result})


@inv_track_bp.route("/api/apply/<int:line_id>/<doc_type>/<int:doc_id>", methods=["POST"])
@login_required
def api_apply_here(line_id, doc_type, doc_id):
    """From an invoice's page: put as much of this receipt/payment on the
    invoice as both have room for, leaving the rest of its split alone."""
    denied = deny_json(RESOURCE, "edit")
    if denied:
        return denied
    if doc_type not in (SALES, PURCHASE):
        return jsonify({"ok": False, "error": "Unknown invoice type."}), 400
    row = pt.feed_row(line_id)
    inv = pt.invoice_row(doc_type, doc_id)
    if row is None or inv is None:
        return jsonify({"ok": False, "error": "That receipt or invoice is no longer open."}), 404
    already = next((s["amount"] for s in pt.history_for_line(line_id)
                    if not s["forced"] and s["doc_type"] == doc_type
                    and int(s["doc_id"]) == int(doc_id)), 0.0)
    take = min(row["unassigned"], inv["outstanding"])
    if take <= 0:
        return jsonify({"ok": False, "error": "Nothing left to apply — the receipt or the "
                                              "invoice is already fully settled."}), 400
    try:
        pt.allocate(line_id, [{"doc_id": doc_id, "amount": round(already + take, 2)}],
                    user_id=current_user.id)
    except AllocationError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True, "message": f"Applied {take:,.2f} to {inv['number']}"})


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


@inv_track_bp.route("/api/clear-stale", methods=["POST"])
@login_required
def api_clear_stale():
    """Drop assignments that no longer hold. With no ids, drops them all."""
    denied = deny_json(RESOURCE, "edit")
    if denied:
        return denied
    data = request.get_json(silent=True) or {}
    ids = data.get("ids")
    cleared = pt.clear_stale_allocations(ids if ids else None)
    return jsonify({"ok": True, "cleared": cleared,
                    "message": (f"Cleared {cleared} broken assignment"
                                f"{'' if cleared == 1 else 's'}")
                    if cleared else "There was nothing to clear"})


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
