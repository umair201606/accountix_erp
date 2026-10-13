"""Executive Reports — receivables, payables and the accounts they watch.

Read-only over the general ledger. The only thing this module writes is the
list of accounts to include; see ``shared/executive_reports.py`` for why no
figure here is ever stored.
"""

from datetime import datetime
from shared.clock import local_now

from flask import (Blueprint, render_template, request, redirect, url_for,
                   flash)
from flask_login import login_required, current_user

from shared import executive_reports as er
from shared import report_export as rx
from shared.permissions import deny_page

exec_bp = Blueprint("executive", __name__, url_prefix="/executive",
                    template_folder="../templates")

RESOURCE = "executive_reports"


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


@exec_bp.route("/")
@login_required
def dashboard():
    if deny_page(RESOURCE, "view"):
        return redirect(url_for("dashboard.hub"))
    as_of = _parse_date(request.args.get("as_of"))
    sides = er.both_sides(as_of)
    ages = er.aging(as_of)
    # Profitability is ledger-derived (er.profitability calls the same
    # _pl_by_section the P&L statement renders from), so the headline figures
    # here and on the statement can never disagree. It is independent of the
    # exec account selection, which only scopes the receivable/payable side —
    # so it renders whether or not the module has been configured.
    profit = er.profitability(as_of=as_of)
    return render_template(
        "executive/dashboard.html",
        receivable=sides[er.RECEIVABLE],
        payable=sides[er.PAYABLE],
        recv_age=ages[er.RECEIVABLE],
        pay_age=ages[er.PAYABLE],
        liq=er.liquidity(as_of),
        profit=profit,
        profit_geo=er.profit_chart(profit),
        configured=bool(er.selections()),
        as_of=request.args.get("as_of", ""),
        can_edit=current_user.can(RESOURCE, "edit"),
    )


def _register(side):
    if deny_page(RESOURCE, "view"):
        return redirect(url_for("dashboard.hub"))
    as_of = _parse_date(request.args.get("as_of"))
    search = request.args.get("q", "").strip()
    group_id = request.args.get("group", type=int)
    rows = er.party_rows(side=side, as_of=as_of, search=search or None,
                         group_id=group_id)
    fmt = request.args.get("format")
    if fmt:
        return _export_register(fmt, side, rows, as_of, search, group_id)
    return render_template(
        "executive/register.html",
        side=side, meta=er.SIDE_META[side], rows=rows,
        totals=er.totals(rows),
        groups=er.groups(),
        configured=bool(er.selections()),
        filters={"q": search, "group": group_id,
                 "as_of": request.args.get("as_of", "")},
        can_edit=current_user.can(RESOURCE, "edit"),
    )


def _as_of_text(as_of):
    return f"Balances as at {as_of:%d %b %Y}" if as_of else         f"Balances as at {local_now():%d %b %Y} (all postings)"


def _export_register(fmt, side, rows, as_of, search, group_id):
    """Aged receivables / payables: each party's balance split into the
    dashboard's FIFO aging buckets."""
    meta = er.SIDE_META[side]
    ages = er.party_aging(as_of)
    bucket_labels = [label for _k, label, *_ in er.BUCKET_DEFS]
    headers = (["Code", "Party / account", "Group", "Debit", "Credit",
                meta["title"]] + bucket_labels + ["Avg days", "Oldest days"])
    data = []
    sums = {k: 0.0 for k, *_ in er.BUCKET_DEFS}
    for r in rows:
        a = ages.get(r["account_id"]) or {}
        b = a.get("buckets") or {}
        for k in sums:
            sums[k] += b.get(k, 0.0)
        data.append([r["code"], r["name"], r["group"], r["debit"], r["credit"],
                     r["amount"]] + [b.get(k, 0.0) for k, *_ in er.BUCKET_DEFS] +
                    [round(a["avg_days"]) if a else "", a.get("oldest_days", "") if a else ""])
    t = er.totals(rows)
    data.append(["", f"Total ({t['parties']} parties)", "",
                 sum(r["debit"] for r in rows), sum(r["credit"] for r in rows),
                 t["amount"]] + [sums[k] for k, *_ in er.BUCKET_DEFS] + ["", ""])
    filters = []
    if search:
        filters.append(f"Search: {search}")
    if group_id:
        g = next((g for g in er.groups() if g["id"] == group_id), None)
        filters.append(f"Group: {g['label'] if g else group_id}")
    return rx.export_table(fmt, f"Aged {meta['title']}", headers, data,
                           period=_as_of_text(as_of) + " · FIFO aging",
                           filters=filters,
                           row_kinds=["plain"] * len(rows) + ["grand"],
                           col_formats={11: "0", 12: "0"},
                           file_period=(as_of.date() if as_of else local_now().date()))


@exec_bp.route("/integrity")
@login_required
def integrity():
    """Books integrity: does every sub-ledger still agree with the GL?

    Runs the same checks the test suite asserts after every transaction type
    (shared/integrity.py) against the live books of the active company, and
    lists the most recent stock re-costings with the journals that moved them.
    """
    if deny_page(RESOURCE, "view"):
        return redirect(url_for("dashboard.hub"))
    from shared import integrity as books
    from shared.models.stock_layer import StockCostAdjustment
    # The checks scan the books, so they run when asked (Run checks / export).
    if not (request.args.get("run") or request.args.get("format")):
        return render_template("executive/integrity.html", checks=[], all_ok=None,
                               adjustments=[], loaded=False)
    checks = books.run_all()
    adjustments = (StockCostAdjustment.query
                   .order_by(StockCostAdjustment.id.desc()).limit(25).all())
    fmt = request.args.get("format")
    if fmt:
        rows = [[c["label"], "Pass" if c["ok"] else "Fail", c["expected"],
                 c["actual"], c.get("detail") or ""] for c in checks]
        passed = sum(1 for c in checks if c["ok"])
        verdict = ("All checks pass" if passed == len(checks)
                   else f"{len(checks) - passed} of {len(checks)} checks FAIL")
        extra = [("Recent re-costings",
                  ["Document", "Posted on", "Was", "Now", "Change", "Why"],
                  [[a.voucher_number, a.entry_date, float(a.old_cost or 0),
                    float(a.new_cost or 0), float(a.delta or 0), a.trigger or ""]
                   for a in adjustments])]
        title = "Books Integrity"
        headers = ["Check", "Status", "Expected", "Actual", "Detail"]
        now = local_now()     # the company's clock, not the server's UTC
        period = f"{verdict} · run {now:%d %b %Y %H:%M}"
        if fmt in ("excel", "xlsx"):
            buf = rx.build_excel(title, headers, rows, period=period, extra_sheets=extra)
            return rx.send_export(buf, "excel", title, now.date())
        return rx.export_table(fmt, title, headers, rows, period=period,
                               file_period=now.date())
    return render_template("executive/integrity.html", loaded=True, checks=checks,
                           all_ok=all(c["ok"] for c in checks),
                           adjustments=adjustments)


@exec_bp.route("/receivables")
@login_required
def receivables():
    return _register(er.RECEIVABLE)


@exec_bp.route("/payables")
@login_required
def payables():
    return _register(er.PAYABLE)


@exec_bp.route("/party/<int:account_id>")
@login_required
def party(account_id):
    """The postings behind one balance."""
    if deny_page(RESOURCE, "view"):
        return redirect(url_for("dashboard.hub"))
    row = next((r for r in er.party_rows() if r["account_id"] == account_id),
               None)
    if row is None:
        flash("That account is not in the executive report scope, or carries "
              "no balance.", "error")
        return redirect(url_for("executive.receivables"))
    as_of = _parse_date(request.args.get("as_of"))
    lines = er.account_ledger(account_id, as_of)
    fmt = request.args.get("format")
    if fmt:
        # Every posting, oldest first, so the running balance is right; the
        # screen's list is newest-first and capped.
        chrono = list(reversed(er.account_ledger(account_id, as_of, limit=None)))
        data, bal = [], 0.0
        for l in chrono:
            bal += float(l.get("debit") or 0) - float(l.get("credit") or 0)
            data.append([l.get("date"), " ".join(x for x in (l.get("voucher_type"),
                                                            l.get("voucher_number")) if x),
                         l.get("description") or "", float(l.get("debit") or 0),
                         float(l.get("credit") or 0), bal])
        data.append(["", "", "Balance", row["debit"], row["credit"], row["amount"]])
        return rx.export_table(fmt, f"Party Ledger — {row['name']}",
                               ["Date", "Voucher", "Narration", "Debit", "Credit",
                                "Running balance (Dr+)"], data,
                               period=f"{row['code']} · {row['group']} · " + _as_of_text(as_of),
                               row_kinds=["plain"] * len(chrono) + ["grand"],
                               file_period=row["code"])
    return render_template(
        "executive/party.html",
        row=row, meta=er.SIDE_META[row["side"]],
        lines=lines,
    )


@exec_bp.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    """Pick the accounts the two registers read.

    A parent can be taken whole or its children picked individually, which is
    what ``include_children`` on each selection records.
    """
    if deny_page(RESOURCE, "view"):
        return redirect(url_for("dashboard.hub"))

    if request.method == "POST":
        if deny_page(RESOURCE, "edit"):
            return redirect(url_for("executive.settings"))
        picked = request.form.getlist("account")
        deep = request.form.getlist("deep")
        count = er.set_selection(picked, deep, user_id=current_user.id)
        flash(f"{count} account{'' if count == 1 else 's'} now feed the "
              "executive reports.", "success")
        return redirect(url_for("executive.settings"))

    return render_template(
        "executive/settings.html",
        accounts=er.selectable_accounts(),
        can_edit=current_user.can(RESOURCE, "edit"),
    )
