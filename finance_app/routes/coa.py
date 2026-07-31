from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from flask_login import login_required, current_user
from shared.extensions import db
from shared.models.ledger import ChartOfAccount, JournalLine
from shared.models.company_settings import PL_SECTIONS
from shared.coa import next_child_code
from shared.tenancy import scoped_get, scoped_get_404

coa_bp = Blueprint("coa", __name__, url_prefix="/accounting/coa",
                   template_folder="../../finance_app/templates")

LEVEL_LABELS = {1: "Level 1 — Account Class", 2: "Level 2 — Sub-Group",
                3: "Level 3 — Parent Head", 4: "Level 4 — Child Account",
                5: "Level 5 — Operational Account"}

CF_ACTIVITIES = ["operating", "investing", "financing", "cash"]


def _has_lines(acct_id):
    return db.session.query(JournalLine.id).filter_by(account_id=acct_id).first() is not None


def _tree():
    """Accounts as a flat list with depth info for template rendering."""
    all_accts = ChartOfAccount.query.order_by(ChartOfAccount.code).all()
    children = {}
    for a in all_accts:
        children.setdefault(a.parent_id, []).append(a)
    rows = []

    def walk(node, depth):
        kids = sorted(children.get(node.id, []), key=lambda x: x.code)
        rows.append({"acct": node, "depth": depth, "has_children": bool(kids)})
        for child in kids:
            walk(child, depth + 1)

    for r in sorted(children.get(None, []), key=lambda x: x.code):
        walk(r, 0)
    return rows


@coa_bp.route("/")
@login_required
def list_accounts():
    return render_template("accounting/coa_list.html", tree=_tree(),
                           level_labels=LEVEL_LABELS)


@coa_bp.route("/add", methods=["GET", "POST"])
@login_required
def add_account():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        parent_id = request.form.get("parent_id", type=int)
        desc = request.form.get("description", "")
        cf = request.form.get("cash_flow_activity", "") or None
        pl = request.form.get("pl_section", "") or None
        if not name:
            flash("Account name is required.", "error")
            return redirect(url_for("coa.add_account"))
        parent = scoped_get(ChartOfAccount, parent_id) if parent_id else None
        if not parent:
            flash("Parent account is required.", "error")
            return redirect(url_for("coa.add_account"))
        if parent.level >= 5:
            flash("Level 5 is the operational level — it cannot have children.", "error")
            return redirect(url_for("coa.add_account"))
        if cf and cf not in CF_ACTIVITIES:
            cf = None
        if pl and pl not in PL_SECTIONS:
            pl = None
        level = parent.level + 1
        acct = ChartOfAccount(code=next_child_code(parent), name=name,
                              type=parent.type, parent_id=parent.id,
                              level=level, description=desc,
                              cash_flow_activity=cf, pl_section=pl)
        db.session.add(acct)
        db.session.commit()
        flash(f"{LEVEL_LABELS.get(level, 'Account')} \"{name}\" created as {acct.code}.", "success")
        return redirect(url_for("coa.list_accounts"))

    parent_id = request.args.get("parent_id", type=int)
    parents = ChartOfAccount.query.filter(ChartOfAccount.level.in_([1, 2, 3, 4]))\
        .order_by(ChartOfAccount.code).all()
    l1 = [a for a in parents if a.level == 1]
    return render_template("accounting/coa_form.html", account=None,
                           parents=parents, l1=l1, parent_id=parent_id,
                           level_labels=LEVEL_LABELS,
                           cf_activities=CF_ACTIVITIES, pl_sections=PL_SECTIONS)


@coa_bp.route("/<int:id>/edit", methods=["GET", "POST"])
@login_required
def edit_account(id):
    acct = scoped_get_404(ChartOfAccount, id)
    if request.method == "POST":
        # Fixed structural accounts keep name/code, but tags stay editable so
        # reports can be tuned without unlocking the tree.
        if not acct.is_fixed:
            acct.name = request.form.get("name", acct.name).strip() or acct.name
            acct.description = request.form.get("description", "")
            acct.is_active = request.form.get("is_active") == "1"
        cf = request.form.get("cash_flow_activity", "") or None
        pl = request.form.get("pl_section", "") or None
        acct.cash_flow_activity = cf if cf in CF_ACTIVITIES else None
        acct.pl_section = pl if pl in PL_SECTIONS else None
        db.session.commit()
        flash(f"Account \"{acct.name}\" updated.", "success")
        return redirect(url_for("coa.list_accounts"))
    return render_template("accounting/coa_form.html", account=acct,
                           parents=[], l1=[], parent_id=acct.parent_id,
                           level_labels=LEVEL_LABELS,
                           cf_activities=CF_ACTIVITIES, pl_sections=PL_SECTIONS)


@coa_bp.route("/<int:id>/delete", methods=["POST"])
@login_required
def delete_account(id):
    acct = scoped_get_404(ChartOfAccount, id)
    if not acct.can_delete():
        if acct.is_fixed:
            flash(f"\"{acct.name}\" is a fixed account and cannot be deleted.", "error")
        else:
            flash(f"\"{acct.name}\" has child accounts. Delete children first.", "error")
        return redirect(url_for("coa.list_accounts"))
    if _has_lines(acct.id):
        # Never delete history — deactivate instead, as the COA guide requires.
        acct.is_active = False
        db.session.commit()
        flash(f"\"{acct.name}\" carries journal entries, so it was deactivated "
              f"instead of deleted (history is preserved).", "warning")
        return redirect(url_for("coa.list_accounts"))
    db.session.delete(acct)
    db.session.commit()
    flash(f"Account \"{acct.name}\" deleted.", "success")
    return redirect(url_for("coa.list_accounts"))


@coa_bp.route("/api/children")
@login_required
def api_children():
    parent_id = request.args.get("parent_id", type=int)
    if not parent_id:
        return jsonify([])
    children = ChartOfAccount.query.filter_by(parent_id=parent_id).order_by(ChartOfAccount.code).all()
    return jsonify([{"id": c.id, "code": c.code, "name": c.name,
                     "level": c.level, "is_fixed": c.is_fixed,
                     "has_children": ChartOfAccount.query.filter_by(parent_id=c.id).count() > 0}
                    for c in children])


@coa_bp.route("/api/next-code")
@login_required
def api_next_code():
    parent_id = request.args.get("parent_id", type=int)
    parent = scoped_get(ChartOfAccount, parent_id) if parent_id else None
    if parent is None or parent.level >= 5:
        return jsonify({"code": ""})
    return jsonify({"code": next_child_code(parent), "level": parent.level + 1,
                    "level_label": LEVEL_LABELS.get(parent.level + 1, "")})


@coa_bp.route("/api/level-accounts")
@login_required
def api_level_accounts():
    level = request.args.get("level", type=int)
    parent_id = request.args.get("parent_id", type=int)
    q = ChartOfAccount.query.filter_by(level=level)
    if parent_id:
        q = q.filter_by(parent_id=parent_id)
    accounts = q.order_by(ChartOfAccount.code).all()
    return jsonify([{"id": a.id, "code": a.code, "name": a.name} for a in accounts])
