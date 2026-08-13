"""Executive Reports — receivables, payables and the accounts they watch.

Read-only over the general ledger. The only thing this module writes is the
list of accounts to include; see ``shared/executive_reports.py`` for why no
figure here is ever stored.
"""

from datetime import datetime

from flask import (Blueprint, render_template, request, redirect, url_for,
                   flash)
from flask_login import login_required, current_user

from shared import executive_reports as er
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
    return render_template(
        "executive/dashboard.html",
        receivable=sides[er.RECEIVABLE],
        payable=sides[er.PAYABLE],
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
    return render_template(
        "executive/party.html",
        row=row, meta=er.SIDE_META[row["side"]],
        lines=er.account_ledger(account_id, as_of),
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
