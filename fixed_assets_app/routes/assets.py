from datetime import date, datetime
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from flask_login import login_required, current_user
from shared.extensions import db
from shared.models.ledger import ChartOfAccount
from shared.ledger_utils import (post_journal_entry, posting_account,
                                 create_fixed_asset_accounts)
from ..models.asset import FixedAsset, AssetCategory, AssetDepreciation

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
        )
        db.session.add(asset)
        db.session.commit()
        flash(f"Asset '{name}' created successfully.", "success")
        return redirect(url_for("fa_assets.list_assets"))
    return render_template("fixed_assets/assets/form.html", asset=None, categories=categories, accounts=accounts)


@fa_assets_bp.route("/<int:asset_id>")
@login_required
def view_asset(asset_id):
    if not current_user.module_access("fixed_assets"):
        return render_template("access_denied.html")
    asset = FixedAsset.query.get_or_404(asset_id)
    depreciation_entries = asset.depreciation_entries.order_by(AssetDepreciation.entry_date.desc()).all()
    return render_template("fixed_assets/assets/view.html",
                           asset=asset, depreciation_entries=depreciation_entries)


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
    if not asset.accum_dep_account_id:
        _, accum_acct = create_fixed_asset_accounts(asset, asset.name)
        asset.accum_dep_account_id = accum_acct.id
        db.session.flush()
    dep_expense_acct_id = asset.dep_expense_account_id or posting_account("depreciation_expense").id
    accum_dep_acct_id = asset.accum_dep_account_id
    new_accumulated = asset.accumulated_depreciation + amount
    entry = AssetDepreciation(
        asset_id=asset.id,
        entry_date=entry_date,
        amount=amount,
        accumulated_after=new_accumulated,
        net_book_value_after=asset.purchase_cost - new_accumulated,
        notes=request.form.get("notes", ""),
    )
    asset.accumulated_depreciation = new_accumulated
    asset.current_book_value = asset.purchase_cost - new_accumulated
    if asset.current_book_value <= 0:
        asset.current_book_value = 0
    db.session.add(entry)
    db.session.flush()
    try:
        post_journal_entry(
            voucher_type="FA-DEP",
            voucher_id=asset.id,
            voucher_number=f"FA-DEP-{asset.asset_code}-{entry.id}",
            description=f"Depreciation for {asset.name} - {entry_date}",
            entry_date=entry_date,
            created_by=current_user.id,
            lines=[
                {"account_id": dep_expense_acct_id, "debit": amount, "credit": 0,
                 "description": f"Depreciation expense - {asset.name}"},
                {"account_id": accum_dep_acct_id, "debit": 0, "credit": amount,
                 "description": f"Accumulated depreciation - {asset.name}"},
            ],
        )
    except Exception as e:
        db.session.rollback()
        flash(f"Depreciation entry failed: {e}", "error")
        return redirect(url_for("fa_assets.view_asset", asset_id=asset.id))
    db.session.commit()
    flash(f"Depreciation of {amount:,.2f} recorded for '{asset.name}'.", "success")
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
    fa_acct_id = asset.fixed_asset_account_id
    accum_dep_acct_id = asset.accum_dep_account_id
    purchase_cost = asset.purchase_cost
    accum_dep = asset.accumulated_depreciation
    net_book = purchase_cost - accum_dep
    lines = [
        {"account_id": accum_dep_acct_id, "debit": accum_dep, "credit": 0,
         "description": f"Write-off accumulated depreciation - {asset.name}"},
        {"account_id": fa_acct_id, "debit": 0, "credit": purchase_cost,
         "description": f"Asset disposal - {asset.name}"},
    ]
    if net_book > 0:
        loss_acct = posting_account("depreciation_expense")
        lines.append(
            {"account_id": loss_acct.id, "debit": net_book, "credit": 0,
             "description": f"Loss on disposal - {asset.name}"},
        )
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
    flash(f"Asset '{asset.name}' has been disposed.", "success")
    return redirect(url_for("fa_assets.list_assets"))


