from datetime import date, datetime, timedelta
from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required, current_user
from shared.extensions import db
from shared.ledger_utils import post_journal_entry, posting_account, create_fixed_asset_accounts
from ..models.asset import FixedAsset, AssetDepreciation

fa_dep_bp = Blueprint("fa_dep", __name__, url_prefix="/fixed-assets/depreciation")


def last_depreciation_date(asset):
    """The date depreciation was last posted for an asset (or purchase_date if none)."""
    last = asset.depreciation_entries.order_by(AssetDepreciation.entry_date.desc()).first()
    d = last.entry_date if last else asset.purchase_date
    return d.date() if isinstance(d, datetime) else d


def due_depreciation(asset, as_of=None):
    """Calculate total depreciation due from last posting to as_of."""
    if as_of is None:
        as_of = date.today()
    if asset.status != "active" or asset.useful_life <= 0 or asset.purchase_cost <= 0:
        return 0
    last_d = last_depreciation_date(asset)
    if last_d >= as_of:
        return 0
    days = (as_of - last_d).days
    if days < 30:
        return 0
    months = int(days / 30)
    max_months = asset.useful_life * 12
    elapsed_months = int((as_of - asset.purchase_date).days / 30)
    remaining = max_months - elapsed_months
    months = min(months, max(0, remaining))
    if months <= 0:
        return 0
    return round((asset.annual_depreciation / 12) * months, 2)


def post_asset_depreciation(asset, entry_date, created_by):
    """Post due depreciation for one asset. Returns amount or None on failure."""
    amount = due_depreciation(asset, entry_date)
    if amount <= 0:
        return 0
    if not asset.accum_dep_account_id:
        _, accum_acct = create_fixed_asset_accounts(asset, asset.name)
        asset.accum_dep_account_id = accum_acct.id
    accum_dep_acct_id = asset.accum_dep_account_id
    dep_expense_acct_id = asset.dep_expense_account_id or posting_account("depreciation_expense").id
    new_accumulated = asset.accumulated_depreciation + amount
    dep_entry = AssetDepreciation(
        asset_id=asset.id, entry_date=entry_date, amount=amount,
        accumulated_after=new_accumulated,
        net_book_value_after=asset.purchase_cost - new_accumulated,
        notes=f"Depreciation posted {entry_date}",
    )
    db.session.add(dep_entry)
    db.session.flush()
    post_journal_entry(
        voucher_type="FA-DEP", voucher_id=asset.id,
        voucher_number=f"FA-DEP-{asset.asset_code}-{dep_entry.id}",
        description=f"Depreciation for {asset.name} - {entry_date}",
        entry_date=entry_date, created_by=created_by,
        lines=[
            {"account_id": dep_expense_acct_id, "debit": amount, "credit": 0,
             "description": f"Depreciation expense - {asset.name}"},
            {"account_id": accum_dep_acct_id, "debit": 0, "credit": amount,
             "description": f"Accumulated depreciation - {asset.name}"},
        ],
    )
    asset.accumulated_depreciation = new_accumulated
    asset.current_book_value = asset.purchase_cost - new_accumulated
    if asset.current_book_value <= 0:
        asset.current_book_value = 0
    return amount


@fa_dep_bp.route("/")
@login_required
def index():
    if not current_user.module_access("fixed_assets"):
        return render_template("access_denied.html")
    as_of = date.today()
    assets = FixedAsset.query.filter_by(status="active", is_active=True).all()
    due = []
    for a in assets:
        amount = due_depreciation(a, as_of)
        if amount > 0:
            last_d = last_depreciation_date(a)
            months = int((as_of - last_d).days / 30)
            due.append({"asset": a, "amount": amount, "months": months})
    return render_template("fixed_assets/depreciation/index.html", due=due, as_of=as_of)


@fa_dep_bp.route("/post", methods=["POST"])
@login_required
def post_depreciation():
    if not current_user.module_access("fixed_assets"):
        return render_template("access_denied.html")
    asset_ids = request.form.getlist("asset_ids[]")
    entry_date_str = request.form.get("entry_date", "")
    entry_date = datetime.strptime(entry_date_str, "%Y-%m-%d").date() if entry_date_str else date.today()
    posted = 0
    errors = []
    for aid in asset_ids:
        asset = FixedAsset.query.get(int(aid))
        if not asset or asset.status != "active":
            continue
        try:
            amt = post_asset_depreciation(asset, entry_date, current_user.id)
            if amt:
                posted += 1
        except Exception as e:
            errors.append(f"{asset.name}: {e}")
    if posted:
        db.session.commit()
        flash(f"Depreciation posted for {posted} asset(s).", "success")
    if errors:
        for e in errors:
            flash(e, "error")
    return redirect(url_for("fa_dep.index"))
