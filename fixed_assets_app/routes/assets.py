from datetime import date, datetime
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from flask_login import login_required, current_user
from shared.extensions import db
from shared.models.ledger import ChartOfAccount
from shared.ledger_utils import (post_journal_entry, reverse_journal_entry,
                                 posting_account, create_fixed_asset_accounts)
from ..models.asset import FixedAsset, AssetCategory, AssetDepreciation
from .depreciation import post_asset_depreciation


def post_acquisition(asset, created_by, credit_account_id=None):
    """Book the asset into the GL: Dr fixed asset, Cr whatever paid for it.

    Without this the subledger carried the cost and the GL carried nothing, so
    depreciation and disposal drove the fixed-asset account negative.

    Keyed ``voucher_id=asset.id`` and skipped when a posted FA-ACQ already
    exists, so re-running it cannot double-book.
    """
    from shared.models.ledger import JournalEntry
    if not asset.purchase_cost:
        return None
    existing = JournalEntry.query.filter_by(
        voucher_type="FA-ACQ", voucher_id=asset.id, is_posted=True).first()
    if existing:
        return existing
    if not asset.fixed_asset_account_id:
        fa_acct, _ = create_fixed_asset_accounts(asset, asset.name)
        asset.fixed_asset_account_id = fa_acct.id
        db.session.flush()
    credit_id = (credit_account_id or asset.acquisition_credit_account_id
                 or posting_account("ap").id)
    asset.acquisition_credit_account_id = credit_id
    return post_journal_entry(
        voucher_type="FA-ACQ", voucher_id=asset.id,
        voucher_number=f"FA-ACQ-{asset.asset_code}",
        description=f"Acquisition of {asset.name}",
        entry_date=asset.purchase_date, created_by=created_by,
        lines=[
            {"account_id": asset.fixed_asset_account_id,
             "debit": asset.purchase_cost, "credit": 0,
             "description": f"Fixed asset - {asset.name}"},
            {"account_id": credit_id, "debit": 0, "credit": asset.purchase_cost,
             "description": f"Acquisition of {asset.name}"},
        ],
    )

fa_assets_bp = Blueprint("fa_assets", __name__, url_prefix="/fixed-assets/assets")


@fa_assets_bp.route("/")
@login_required
def list_assets():
    if not current_user.module_access("fixed_assets"):
        return render_template("access_denied.html")
    q = request.args.get("q", "").strip()
    status_filter = request.args.get("status", "")
    category_filter = request.args.get("category_id", "")
    query = FixedAsset.query.filter_by(is_active=True)
    if q:
        query = query.filter(FixedAsset.name.ilike(f"%{q}%") | FixedAsset.asset_code.ilike(f"%{q}%"))
    if status_filter:
        query = query.filter_by(status=status_filter)
    if category_filter:
        query = query.filter_by(category_id=int(category_filter))
    assets = query.order_by(FixedAsset.created_at.desc()).all()
    categories = AssetCategory.query.filter_by(is_active=True).all()
    return render_template("fixed_assets/assets/index.html",
                           assets=assets, categories=categories,
                           q=q, status_filter=status_filter, category_filter=category_filter)


@fa_assets_bp.route("/create", methods=["GET", "POST"])
@login_required
def create_asset():
    if not current_user.module_access("fixed_assets"):
        return render_template("access_denied.html")
    categories = AssetCategory.query.filter_by(is_active=True).all()
    accounts = ChartOfAccount.query.filter(
        ChartOfAccount.level >= ChartOfAccount.POSTING_LEVEL,
        ChartOfAccount.is_active == True,
    ).order_by(ChartOfAccount.code).all()
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if not name:
            flash("Asset name is required.", "error")
            return render_template("fixed_assets/assets/form.html", asset=None, categories=categories, accounts=accounts)
        category_id = int(request.form.get("category_id", 0))
        category = AssetCategory.query.get(category_id)
        purchase_cost = float(request.form.get("purchase_cost", 0))
        useful_life = int(request.form.get("useful_life", category.default_useful_life if category else 5))
        salvage_value = float(request.form.get("salvage_value", 0))
        last_asset = FixedAsset.query.order_by(FixedAsset.id.desc()).first()
        next_id = (last_asset.id + 1) if last_asset else 1
        fa_acct_id = request.form.get("fixed_asset_account_id", type=int) or posting_account("fixed_assets").id
        asset = FixedAsset(
            asset_code=f"FA-{next_id:04d}",
            name=name,
            description=request.form.get("description", ""),
            category_id=category_id,
            purchase_date=datetime.strptime(request.form["purchase_date"], "%Y-%m-%d").date(),
            purchase_cost=purchase_cost,
            useful_life=useful_life,
            depreciation_method=request.form.get("depreciation_method", "straight_line"),
            salvage_value=salvage_value,
            current_book_value=purchase_cost,
            status="active",
            location=request.form.get("location", ""),
            assigned_to=request.form.get("assigned_to", ""),
            vendor=request.form.get("vendor", ""),
            serial_number=request.form.get("serial_number", ""),
            fixed_asset_account_id=fa_acct_id,
            accum_dep_account_id=request.form.get("accum_dep_account_id", type=int) or None,
            dep_expense_account_id=request.form.get("dep_expense_account_id", type=int) or None,
            notes=request.form.get("notes", ""),
            acquisition_credit_account_id=request.form.get(
                "acquisition_credit_account_id", type=int) or None,
        )
        db.session.add(asset)
        db.session.flush()
        try:
            post_acquisition(asset, current_user.id)
        except Exception as e:
            db.session.rollback()
            flash(f"Asset not created — acquisition entry failed: {e}", "error")
            return render_template("fixed_assets/assets/form.html", asset=None,
                                   categories=categories, accounts=accounts)
        asset.recalculate()
        db.session.commit()
        flash(f"Asset '{name}' created and booked to the ledger.", "success")
        return redirect(url_for("fa_assets.list_assets"))
    return render_template("fixed_assets/assets/form.html", asset=None, categories=categories, accounts=accounts)


@fa_assets_bp.route("/<int:asset_id>")
@login_required
def view_asset(asset_id):
    if not current_user.module_access("fixed_assets"):
        return render_template("access_denied.html")
    asset = FixedAsset.query.get_or_404(asset_id)
    # Fresh query, not the relationship: its own order_by is appended to, so
    # .desc() through it silently yields ascending order.
    depreciation_entries = (AssetDepreciation.query
                            .filter_by(asset_id=asset.id)
                            .order_by(AssetDepreciation.entry_date.desc(),
                                      AssetDepreciation.id.desc()).all())
    accounts = ChartOfAccount.query.filter(
        ChartOfAccount.level >= ChartOfAccount.POSTING_LEVEL,
        ChartOfAccount.is_active == True,
    ).order_by(ChartOfAccount.code).all()
    return render_template("fixed_assets/assets/view.html", asset=asset,
                           depreciation_entries=depreciation_entries,
                           accounts=accounts)


@fa_assets_bp.route("/<int:asset_id>/edit", methods=["GET", "POST"])
@login_required
def edit_asset(asset_id):
    if not current_user.module_access("fixed_assets"):
        return render_template("access_denied.html")
    asset = FixedAsset.query.get_or_404(asset_id)
    categories = AssetCategory.query.filter_by(is_active=True).all()
    accounts = ChartOfAccount.query.filter(
        ChartOfAccount.level >= ChartOfAccount.POSTING_LEVEL,
        ChartOfAccount.is_active == True,
    ).order_by(ChartOfAccount.code).all()
    if request.method == "POST":
        prior_cost = asset.purchase_cost
        prior_date = asset.purchase_date
        prior_credit = asset.acquisition_credit_account_id
        asset.name = request.form.get("name", "").strip()
        asset.description = request.form.get("description", "")
        asset.category_id = int(request.form.get("category_id", 0))
        asset.purchase_date = datetime.strptime(request.form["purchase_date"], "%Y-%m-%d").date()
        asset.purchase_cost = float(request.form.get("purchase_cost", 0))
        asset.useful_life = int(request.form.get("useful_life", 5))
        asset.depreciation_method = request.form.get("depreciation_method", "straight_line")
        asset.salvage_value = float(request.form.get("salvage_value", 0))
        asset.status = request.form.get("status", "active")
        asset.fixed_asset_account_id = request.form.get("fixed_asset_account_id", type=int) or None
        asset.accum_dep_account_id = request.form.get("accum_dep_account_id", type=int) or None
        asset.dep_expense_account_id = request.form.get("dep_expense_account_id", type=int) or None
        asset.location = request.form.get("location", "")
        asset.assigned_to = request.form.get("assigned_to", "")
        asset.vendor = request.form.get("vendor", "")
        asset.serial_number = request.form.get("serial_number", "")
        asset.notes = request.form.get("notes", "")
        asset.acquisition_credit_account_id = request.form.get(
            "acquisition_credit_account_id", type=int) or None
        # Editing what the acquisition journal was built from must move the
        # journal too, or the GL keeps carrying the old cost forever.
        if (asset.purchase_cost != prior_cost or asset.purchase_date != prior_date
                or asset.acquisition_credit_account_id != prior_credit):
            try:
                reverse_journal_entry("FA-ACQ", asset.id, created_by=current_user.id)
                post_acquisition(asset, current_user.id)
            except Exception as e:
                db.session.rollback()
                flash(f"Could not restate the acquisition entry: {e}", "error")
                return render_template("fixed_assets/assets/form.html", asset=asset,
                                       categories=categories, accounts=accounts)
        asset.recalculate()
        db.session.commit()
        flash("Asset updated successfully.", "success")
        return redirect(url_for("fa_assets.view_asset", asset_id=asset.id))
    return render_template("fixed_assets/assets/form.html", asset=asset, categories=categories, accounts=accounts)


@fa_assets_bp.route("/<int:asset_id>/depreciate", methods=["POST"])
@login_required
def record_depreciation(asset_id):
    if not current_user.module_access("fixed_assets"):
        return jsonify({"ok": False, "error": "Access denied"}), 403
    asset = FixedAsset.query.get_or_404(asset_id)
    entry_date = datetime.strptime(request.form["entry_date"], "%Y-%m-%d").date()
    amount = float(request.form.get("amount", 0))
    if amount <= 0:
        flash("Depreciation amount must be positive.", "error")
        return redirect(url_for("fa_assets.view_asset", asset_id=asset.id))
    if amount > asset.remaining_depreciable:
        flash(f"Only {asset.remaining_depreciable:,.2f} remains depreciable before "
              f"the salvage value of {asset.salvage_value:,.2f}.", "error")
        return redirect(url_for("fa_assets.view_asset", asset_id=asset.id))
    # One shared implementation: this route used to duplicate the posting logic
    # and drifted from it (same voucher_id for every charge, no journal link).
    try:
        charged = post_asset_depreciation(asset, entry_date, current_user.id,
                                          amount=amount)
    except Exception as e:
        db.session.rollback()
        flash(f"Depreciation entry failed: {e}", "error")
        return redirect(url_for("fa_assets.view_asset", asset_id=asset.id))
    if not charged:
        flash("Nothing left to depreciate on this asset.", "error")
        return redirect(url_for("fa_assets.view_asset", asset_id=asset.id))
    db.session.commit()
    flash(f"Depreciation of {charged:,.2f} recorded for '{asset.name}'.", "success")
    return redirect(url_for("fa_assets.view_asset", asset_id=asset.id))


@fa_assets_bp.route("/<int:asset_id>/dispose", methods=["POST"])
@login_required
def dispose_asset(asset_id):
    if not current_user.module_access("fixed_assets"):
        return render_template("access_denied.html")
    asset = FixedAsset.query.get_or_404(asset_id)
    if not asset.fixed_asset_account_id:
        fa_acct, _ = create_fixed_asset_accounts(asset, asset.name)
        asset.fixed_asset_account_id = fa_acct.id
        db.session.flush()
    if not asset.accum_dep_account_id:
        _, accum_acct = create_fixed_asset_accounts(asset, asset.name)
        asset.accum_dep_account_id = accum_acct.id
        db.session.flush()
    if asset.status == "disposed":
        flash("This asset is already disposed.", "error")
        return redirect(url_for("fa_assets.view_asset", asset_id=asset.id))
    fa_acct_id = asset.fixed_asset_account_id
    accum_dep_acct_id = asset.accum_dep_account_id
    purchase_cost = asset.purchase_cost
    # Derived, not the cached column: a reversed depreciation charge must not
    # leave disposal writing off accumulated depreciation the GL no longer holds.
    accum_dep = asset.posted_depreciation
    net_book = purchase_cost - accum_dep

    proceeds = float(request.form.get("proceeds", 0) or 0)
    if proceeds < 0:
        flash("Proceeds cannot be negative.", "error")
        return redirect(url_for("fa_assets.view_asset", asset_id=asset.id))
    proceeds_acct_id = (request.form.get("proceeds_account_id", type=int)
                        or posting_account("cash").id)

    lines = [
        {"account_id": accum_dep_acct_id, "debit": accum_dep, "credit": 0,
         "description": f"Write-off accumulated depreciation - {asset.name}"},
        {"account_id": fa_acct_id, "debit": 0, "credit": purchase_cost,
         "description": f"Asset disposal - {asset.name}"},
    ]
    if proceeds:
        lines.append(
            {"account_id": proceeds_acct_id, "debit": proceeds, "credit": 0,
             "description": f"Proceeds on disposal - {asset.name}"})
    # Sale price against what the books still carry. Scrapping is just the
    # proceeds = 0 case, so there is no separate code path for it.
    result = round(proceeds - net_book, 2)
    if result:
        gain_loss_acct = posting_account("disposal_gain_loss")
        lines.append({
            "account_id": gain_loss_acct.id,
            "debit": abs(result) if result < 0 else 0,
            "credit": result if result > 0 else 0,
            "description": (f"{'Gain' if result > 0 else 'Loss'} on disposal - "
                            f"{asset.name}"),
        })
    asset.status = "disposed"
    asset.is_active = False
    db.session.flush()
    try:
        post_journal_entry(
            voucher_type="FA-DISP",
            voucher_id=asset.id,
            voucher_number=f"FA-DISP-{asset.asset_code}",
            description=f"Disposal of {asset.name}",
            entry_date=date.today(),
            created_by=current_user.id,
            lines=lines,
        )
    except Exception as e:
        db.session.rollback()
        flash(f"Disposal entry failed: {e}", "error")
        return redirect(url_for("fa_assets.view_asset", asset_id=asset.id))
    db.session.commit()
    outcome = ("" if not result else
               f" {'Gain' if result > 0 else 'Loss'} of {abs(result):,.2f} booked.")
    flash(f"Asset '{asset.name}' has been disposed.{outcome}", "success")
    return redirect(url_for("fa_assets.list_assets"))


@fa_assets_bp.route("/<int:asset_id>/undo-disposal", methods=["POST"])
@login_required
def undo_disposal(asset_id):
    """Un-post a disposal and put the asset back into service.

    Disposal was a one-way door: a mis-keyed disposal left the asset written out
    of the ledger with no way back. Un-posting restores the cost and the
    accumulated depreciation together, because both were derecognised by the
    same journal.
    """
    if not current_user.module_access("fixed_assets"):
        return render_template("access_denied.html")
    asset = FixedAsset.query.get_or_404(asset_id)
    if asset.status != "disposed":
        flash("This asset is not disposed.", "error")
        return redirect(url_for("fa_assets.view_asset", asset_id=asset.id))
    try:
        reverse_journal_entry("FA-DISP", asset.id, created_by=current_user.id)
    except Exception as e:
        db.session.rollback()
        flash(f"Could not reverse the disposal: {e}", "error")
        return redirect(url_for("fa_assets.view_asset", asset_id=asset.id))
    asset.status = "active"
    asset.is_active = True
    asset.recalculate()
    db.session.commit()
    flash(f"Disposal of '{asset.name}' reversed; book value is back to "
          f"{asset.current_book_value:,.2f}.", "success")
    return redirect(url_for("fa_assets.view_asset", asset_id=asset.id))


