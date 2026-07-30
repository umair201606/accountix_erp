from datetime import date, datetime, timedelta
from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required, current_user
from shared.extensions import db
from shared.ledger_utils import (post_journal_entry, reverse_journal_entry,
                                 posting_account, create_fixed_asset_accounts)
from ..models.asset import FixedAsset, AssetDepreciation

fa_dep_bp = Blueprint("fa_dep", __name__, url_prefix="/fixed-assets/depreciation")


def last_depreciation_date(asset):
    """The date depreciation was last posted for an asset (or purchase_date if none).

    Deliberately a fresh query rather than ``asset.depreciation_entries``: that
    relationship declares ``order_by=entry_date`` and ``Query.order_by()``
    APPENDS, so ``.order_by(entry_date.desc())`` resolved to
    ``ORDER BY entry_date ASC, entry_date DESC`` and returned the OLDEST row.
    Every catch-up was then measured from the first charge ever posted and
    over-charged the months in between.

    Only live rows count, so reversing the latest charge moves this date back
    and that period becomes chargeable again.
    """
    last = (asset.live_depreciation_query()
            .order_by(AssetDepreciation.entry_date.desc(),
                      AssetDepreciation.id.desc())
            .first())
    d = last.entry_date if last else asset.purchase_date
    return d.date() if isinstance(d, datetime) else d


def _months_between(start, end):
    """Whole calendar months from start to end.

    Calendar months, not 30-day blocks: the old ``days / 30`` drifted ~6 days a
    year, so a 5-year asset was declared fully depreciated about two months
    early and month counts disagreed with the period they were charged for.
    """
    if end <= start:
        return 0
    months = (end.year - start.year) * 12 + (end.month - start.month)
    if end.day < start.day:
        months -= 1
    return max(0, months)


def due_depreciation(asset, as_of=None):
    """Depreciation chargeable from the last live posting up to as_of.

    Derived from live rows only, so this is self-correcting: reverse or delete a
    charge and the amount it covered simply becomes due again.
    """
    if as_of is None:
        as_of = date.today()
    if asset.status != "active" or asset.useful_life <= 0 or asset.purchase_cost <= 0:
        return 0
    last_d = last_depreciation_date(asset)
    months = _months_between(last_d, as_of)
    if months <= 0:
        return 0
    # Never charge past the asset's life...
    charged_months = _months_between(asset.purchase_date, last_d)
    months = min(months, max(0, asset.useful_life * 12 - charged_months))
    if months <= 0:
        return 0
    amount = round((asset.annual_depreciation / 12) * months, 2)
    # ...nor past the salvage floor, whatever the method or the month count.
    return round(min(amount, asset.remaining_depreciable), 2)


def post_asset_depreciation(asset, entry_date, created_by, amount=None):
    """Post one depreciation charge for an asset. Returns the amount charged.

    ``amount`` overrides the computed figure (manual charge) but is still capped
    at the salvage floor.

    The journal is keyed ``voucher_id=<AssetDepreciation.id>``, NOT the asset id.
    Sharing the asset id across every charge meant
    ``reverse_journal_entry("FA-DEP", asset.id)`` un-posted an asset's entire
    depreciation history in one go.
    """
    computed = due_depreciation(asset, entry_date)
    amount = computed if amount is None else round(
        min(float(amount), asset.remaining_depreciable), 2)
    if amount <= 0:
        return 0
    if not asset.accum_dep_account_id:
        _, accum_acct = create_fixed_asset_accounts(asset, asset.name)
        asset.accum_dep_account_id = accum_acct.id
    accum_dep_acct_id = asset.accum_dep_account_id
    dep_expense_acct_id = asset.dep_expense_account_id or posting_account("depreciation_expense").id
    dep_entry = AssetDepreciation(
        asset_id=asset.id, entry_date=entry_date, amount=amount,
        notes=f"Depreciation posted {entry_date}",
    )
    db.session.add(dep_entry)
    db.session.flush()
    je = post_journal_entry(
        voucher_type="FA-DEP", voucher_id=dep_entry.id,
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
    dep_entry.journal_entry_id = je.id
    db.session.flush()
    asset.recalculate()
    dep_entry.accumulated_after = asset.accumulated_depreciation
    dep_entry.net_book_value_after = asset.current_book_value
    return amount


def reverse_asset_depreciation(dep_entry, created_by=1):
    """Un-post one depreciation charge and re-derive the asset's balances.

    The journal row and the AssetDepreciation row are both kept; un-posting is
    what removes them from every total, so the audit trail survives and the
    period the charge covered simply becomes due again.
    """
    asset = FixedAsset.query.get(dep_entry.asset_id)
    reverse_journal_entry("FA-DEP", dep_entry.id, created_by=created_by)
    if asset:
        asset.recalculate()
    return asset


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


@fa_dep_bp.route("/entry/<int:dep_id>/reverse", methods=["POST"])
@login_required
def reverse_entry(dep_id):
    if not current_user.module_access("fixed_assets"):
        return render_template("access_denied.html")
    dep_entry = AssetDepreciation.query.get_or_404(dep_id)
    if not dep_entry.is_live:
        flash("That depreciation charge is already reversed.", "error")
        return redirect(url_for("fa_assets.view_asset", asset_id=dep_entry.asset_id))
    try:
        asset = reverse_asset_depreciation(dep_entry, current_user.id)
    except Exception as e:
        db.session.rollback()
        flash(f"Reversal failed: {e}", "error")
        return redirect(url_for("fa_assets.view_asset", asset_id=dep_entry.asset_id))
    db.session.commit()
    flash(f"Depreciation of {dep_entry.amount:,.2f} reversed; book value is now "
          f"{asset.current_book_value:,.2f}.", "success")
    return redirect(url_for("fa_assets.view_asset", asset_id=dep_entry.asset_id))
