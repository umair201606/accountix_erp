from datetime import date, datetime
from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required, current_user
from shared.extensions import db
from shared.tenancy import scoped_get, scoped_get_404
from shared.models.ledger import ChartOfAccount
from shared.models.stock_ledger import VoucherNumber
from shared.models.asset_transfer import AssetTransfer
from shared.ledger_utils import (post_journal_entry, reverse_journal_entry,
                                 posting_account, create_fixed_asset_accounts,
                                 create_entity_account)

inv_transfers_bp = Blueprint("inv_transfers", __name__, url_prefix="/inventory/transfer-to-fa")


def _assets():
    from fixed_assets_app.models.asset import FixedAsset, AssetCategory
    return FixedAsset, AssetCategory


@inv_transfers_bp.route("/")
@login_required
def list_transfers():
    if not current_user.module_access("inventory"):
        return render_template("access_denied.html")
    transfers = AssetTransfer.query.filter_by(direction="from_inventory")\
        .order_by(AssetTransfer.created_at.desc()).all()
    return render_template("transfers/list.html", transfers=transfers)


@inv_transfers_bp.route("/create", methods=["GET", "POST"])
@login_required
def create_transfer():
    if not current_user.module_access("inventory"):
        return render_template("access_denied.html")
    products = []
    try:
        from inventory_app.models.product import InvProduct
        products = InvProduct.query.filter_by(is_active=True).order_by(InvProduct.name).all()
    except Exception:
        pass
    categories = []
    try:
        FixedAsset, AssetCategory = _assets()
        categories = AssetCategory.query.filter_by(is_active=True).all()
    except Exception:
        pass
    if request.method == "POST":
        product_id = request.form.get("product_id", type=int)
        if not product_id:
            flash("Select a product to transfer.", "error")
            return render_template("transfers/form.html", products=products, categories=categories)
        try:
            from inventory_app.models.product import InvProduct
            prod = scoped_get(InvProduct, product_id)
            if not prod:
                flash("Product not found.", "error")
                return render_template("transfers/form.html", products=products, categories=categories)
        except Exception as e:
            flash(f"Product error: {e}", "error")
            return render_template("transfers/form.html", products=products, categories=categories)
        name = request.form.get("name", "").strip() or prod.name
        purchase_cost = float(request.form.get("purchase_cost", 0) or (prod.cost_price or prod.unit_price or 0))
        category_id = int(request.form.get("category_id", 0))
        FixedAsset, AssetCategory = _assets()
        category = scoped_get(AssetCategory, category_id)
        useful_life = int(request.form.get("useful_life", category.default_useful_life if category else 5))
        voucher_number = VoucherNumber.next("INV-FA")
        asset = FixedAsset(
            asset_code=f"FA-{FixedAsset.query.count() + 1:04d}",
            name=name, description=request.form.get("description", ""),
            category_id=category_id,
            purchase_date=datetime.strptime(request.form["purchase_date"], "%Y-%m-%d").date(),
            purchase_cost=purchase_cost, useful_life=useful_life,
            depreciation_method=request.form.get("depreciation_method", "straight_line"),
            salvage_value=float(request.form.get("salvage_value", 0)),
            current_book_value=purchase_cost, status="active",
            location=request.form.get("location", ""),
            assigned_to=request.form.get("assigned_to", ""),
            vendor=request.form.get("vendor", ""),
            serial_number=request.form.get("serial_number", ""),
            notes=request.form.get("notes", ""),
        )
        db.session.add(asset)
        db.session.flush()
        transfer = AssetTransfer(
            voucher_number=voucher_number, direction="from_inventory",
            asset_id=asset.id, source_product_id=product_id,
            transfer_amount=purchase_cost,
            description=f"Transfer from inventory: {name}",
            status="unapproved", created_by=current_user.id,
        )
        db.session.add(transfer)
        db.session.flush()
        status = request.form.get("status", "unapproved")
        if status == "approved":
            fa_acct, accum_acct = create_fixed_asset_accounts(asset, name)
            asset.fixed_asset_account_id = fa_acct.id
            asset.accum_dep_account_id = accum_acct.id
            inv_acct = create_entity_account("product", prod.id, prod.name)
            lines = [
                {"account_id": fa_acct.id, "debit": purchase_cost, "credit": 0,
                 "description": f"Asset capitalised - {name}"},
                {"account_id": inv_acct.id, "debit": 0, "credit": purchase_cost,
                 "description": f"Transfer from inventory - {prod.name}"},
            ]
            try:
                post_journal_entry(
                    voucher_type="INV-FA", voucher_id=transfer.id,
                    voucher_number=voucher_number,
                    description=f"Transfer from inventory to FA: {name}",
                    entry_date=asset.purchase_date, created_by=current_user.id, lines=lines,
                )
            except Exception as e:
                db.session.rollback()
                flash(f"Posting failed: {e}", "error")
                return render_template("transfers/form.html", products=products, categories=categories)
            transfer.approved_by = current_user.id
            transfer.approved_at = datetime.utcnow()
            transfer.status = "approved"
        db.session.commit()
        flash(f"Transfer {voucher_number} {'approved and ' if status == 'approved' else ''}saved.", "success")
        return redirect(url_for("inv_transfers.list_transfers"))
    return render_template("transfers/form.html", products=products, categories=categories)


@inv_transfers_bp.route("/<int:id>/edit", methods=["GET", "POST"])
@login_required
def edit_transfer(id):
    if not current_user.module_access("inventory"):
        return render_template("access_denied.html")
    transfer = scoped_get_404(AssetTransfer, id)
    if transfer.status == "approved":
        flash("Cannot edit an approved transfer.", "error")
        return redirect(url_for("inv_transfers.list_transfers"))
    FixedAsset, AssetCategory = _assets()
    asset = scoped_get(FixedAsset, transfer.asset_id)
    products = []
    try:
        from inventory_app.models.product import InvProduct
        products = InvProduct.query.filter_by(is_active=True).order_by(InvProduct.name).all()
    except Exception:
        pass
    categories = AssetCategory.query.filter_by(is_active=True).all() if AssetCategory else []
    if request.method == "POST":
        transfer.description = request.form.get("description", "")
        status = request.form.get("status", "unapproved")
        if status == "approved":
            product_id = request.form.get("product_id", type=int) or transfer.source_product_id
            try:
                from inventory_app.models.product import InvProduct
                prod = scoped_get(InvProduct, product_id)
            except Exception:
                prod = None
            name = asset.name if asset else ""
            purchase_cost = asset.purchase_cost if asset else 0
            if not asset.fixed_asset_account_id:
                fa_acct, accum_acct = create_fixed_asset_accounts(asset, name)
                asset.fixed_asset_account_id = fa_acct.id
                asset.accum_dep_account_id = accum_acct.id
            fa_acct_id = asset.fixed_asset_account_id
            inv_acct = create_entity_account("product", prod.id, prod.name) if prod else posting_account("inventory")
            lines = [
                {"account_id": fa_acct_id, "debit": purchase_cost, "credit": 0,
                 "description": f"Asset capitalised - {name}"},
                {"account_id": inv_acct.id, "debit": 0, "credit": purchase_cost,
                 "description": f"Transfer from inventory - {prod.name if prod else ''}"},
            ]
            try:
                post_journal_entry(
                    voucher_type="INV-FA", voucher_id=transfer.id,
                    voucher_number=transfer.voucher_number,
                    description=f"Transfer from inventory: {name}",
                    entry_date=date.today(), created_by=current_user.id, lines=lines,
                )
            except Exception as e:
                db.session.rollback()
                flash(f"Posting failed: {e}", "error")
                return render_template("transfers/form.html", products=products, categories=categories)
            transfer.approved_by = current_user.id
            transfer.approved_at = datetime.utcnow()
            transfer.status = "approved"
        db.session.commit()
        flash(f"Transfer {transfer.voucher_number} updated.", "success")
        return redirect(url_for("inv_transfers.list_transfers"))
    return render_template("transfers/form.html", products=products, categories=categories, transfer=transfer, asset=asset)


@inv_transfers_bp.route("/<int:id>/unapprove")
@login_required
def unapprove_transfer(id):
    if not current_user.module_access("inventory"):
        return render_template("access_denied.html")
    transfer = scoped_get_404(AssetTransfer, id)
    if transfer.status != "approved":
        flash("Transfer is not approved.", "error")
        return redirect(url_for("inv_transfers.list_transfers"))
    FixedAsset, _ = _assets()
    asset = scoped_get(FixedAsset, transfer.asset_id)
    if asset:
        db.session.delete(asset)
    try:
        reverse_journal_entry("INV-FA", transfer.id, created_by=current_user.id)
    except Exception as e:
        flash(f"Reversal failed: {e}", "error")
        return redirect(url_for("inv_transfers.list_transfers"))
    transfer.status = "unapproved"
    transfer.approved_by = None
    transfer.approved_at = None
    db.session.commit()
    flash(f"{transfer.voucher_number} unapproved and reversed.", "success")
    return redirect(url_for("inv_transfers.list_transfers"))


@inv_transfers_bp.route("/<int:id>/delete")
@login_required
def delete_transfer(id):
    if not current_user.module_access("inventory"):
        return render_template("access_denied.html")
    transfer = scoped_get_404(AssetTransfer, id)
    if transfer.status == "approved":
        flash("Cannot delete an approved transfer. Unapprove it first.", "error")
        return redirect(url_for("inv_transfers.list_transfers"))
    FixedAsset, _ = _assets()
    asset = scoped_get(FixedAsset, transfer.asset_id)
    if asset:
        db.session.delete(asset)
    db.session.delete(transfer)
    db.session.commit()
    flash(f"Transfer {transfer.voucher_number} deleted.", "success")
    return redirect(url_for("inv_transfers.list_transfers"))
