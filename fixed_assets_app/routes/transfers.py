from datetime import date, datetime
from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required, current_user
from shared.extensions import db
from shared.models.ledger import ChartOfAccount
from shared.models.stock_ledger import VoucherNumber
from shared.models.asset_transfer import AssetTransfer
from shared.ledger_utils import (post_journal_entry, reverse_journal_entry,
                                 posting_account, create_fixed_asset_accounts,
                                 create_entity_account)
from ..models.asset import FixedAsset, AssetCategory
from .depreciation import post_asset_depreciation, due_depreciation

fa_transfers_bp = Blueprint("fa_transfers", __name__, url_prefix="/fixed-assets/transfers")


@fa_transfers_bp.route("/")
@login_required
def list_transfers():
    if not current_user.module_access("fixed_assets"):
        return render_template("access_denied.html")
    transfers = AssetTransfer.query.filter_by(direction="to_inventory")\
        .order_by(AssetTransfer.created_at.desc()).all()
    return render_template("fixed_assets/transfers/list.html", transfers=transfers)


@fa_transfers_bp.route("/create", methods=["GET", "POST"])
@login_required
def create_transfer():
    if not current_user.module_access("fixed_assets"):
        return render_template("access_denied.html")
    asset_id = request.args.get("asset_id", type=int)
    asset = FixedAsset.query.get(asset_id) if asset_id else None
    if not asset or asset.status != "active":
        flash("Select an active asset to transfer.", "error")
        return redirect(url_for("fa_assets.list_assets"))
    products = []
    try:
        from inventory_app.models.product import InvProduct
        products = InvProduct.query.filter_by(is_active=True).order_by(InvProduct.name).all()
    except Exception:
        pass
    if request.method == "POST":
        product_id = request.form.get("product_id", type=int)
        new_product_name = request.form.get("new_product_name", "").strip()
        stock_account_id = None
        stock_description = asset.name
        if product_id:
            try:
                from inventory_app.models.product import InvProduct
                prod = InvProduct.query.get(product_id)
                if prod:
                    acct = create_entity_account("product", prod.id, prod.name)
                    stock_account_id = acct.id
                    stock_description = prod.name
            except Exception:
                pass
        elif new_product_name:
            try:
                from inventory_app.models.product import InvProduct
                prod = InvProduct(name=new_product_name, sku=f"FA-{asset.asset_code}",
                                  unit_price=0, cost_price=0, current_stock=0, is_active=True)
                db.session.add(prod)
                db.session.flush()
                acct = create_entity_account("product", prod.id, prod.name)
                stock_account_id = acct.id
                stock_description = prod.name
                product_id = prod.id
            except Exception as e:
                flash(f"Could not create product: {e}", "error")
                return render_template("fixed_assets/transfers/form.html", asset=asset, products=products)
        else:
            stock_acct = posting_account("inventory")
            stock_account_id = stock_acct.id
        purchase_cost = asset.purchase_cost
        accum_dep = asset.accumulated_depreciation
        net_book = purchase_cost - accum_dep
        voucher_number = VoucherNumber.next("FA-TRF")
        transfer = AssetTransfer(
            voucher_number=voucher_number,
            direction="to_inventory",
            asset_id=asset.id,
            product_id=product_id,
            new_product_name=new_product_name,
            transfer_amount=net_book,
            description=request.form.get("description", f"Transfer to inventory: {stock_description}"),
            status="unapproved",
            created_by=current_user.id,
        )
        db.session.add(transfer)
        db.session.flush()
        status = request.form.get("status", "unapproved")
        if status == "approved":
            if not asset.fixed_asset_account_id:
                fa_acct, _ = create_fixed_asset_accounts(asset, asset.name)
                asset.fixed_asset_account_id = fa_acct.id
            if not asset.accum_dep_account_id:
                _, accum_acct = create_fixed_asset_accounts(asset, asset.name)
                asset.accum_dep_account_id = accum_acct.id
            fa_acct_id = asset.fixed_asset_account_id
            accum_dep_acct_id = asset.accum_dep_account_id
            try:
                post_asset_depreciation(asset, date.today(), current_user.id)
            except Exception:
                pass
            purchase_cost = asset.purchase_cost
            accum_dep = asset.accumulated_depreciation
            net_book = purchase_cost - accum_dep
            lines = [
                {"account_id": accum_dep_acct_id, "debit": accum_dep, "credit": 0,
                 "description": f"Derecognise accum dep - {asset.name}"},
                {"account_id": stock_account_id, "debit": net_book, "credit": 0,
                 "description": f"Transfer to stock - {stock_description}"},
                {"account_id": fa_acct_id, "debit": 0, "credit": purchase_cost,
                 "description": f"Derecognise asset - {asset.name}"},
            ]
            try:
                post_journal_entry(
                    voucher_type="FA-TRF", voucher_id=transfer.id,
                    voucher_number=voucher_number,
                    description=f"Transfer to inventory: {asset.name} at BV {net_book:,.0f}",
                    entry_date=date.today(), created_by=current_user.id, lines=lines,
                )
            except Exception as e:
                db.session.rollback()
                flash(f"Posting failed: {e}", "error")
                return render_template("fixed_assets/transfers/form.html", asset=asset, products=products)
            asset.status = "transferred"
            asset.is_active = False
            transfer.approved_by = current_user.id
            transfer.approved_at = datetime.utcnow()
            transfer.status = "approved"
        db.session.commit()
        flash(f"Transfer {voucher_number} {'approved and ' if status == 'approved' else ''}saved.", "success")
        return redirect(url_for("fa_transfers.list_transfers"))
    return render_template("fixed_assets/transfers/form.html", asset=asset, products=products)


@fa_transfers_bp.route("/<int:id>/edit", methods=["GET", "POST"])
@login_required
def edit_transfer(id):
    if not current_user.module_access("fixed_assets"):
        return render_template("access_denied.html")
    transfer = AssetTransfer.query.get_or_404(id)
    if transfer.status == "approved":
        flash("Cannot edit an approved transfer.", "error")
        return redirect(url_for("fa_transfers.list_transfers"))
    asset = FixedAsset.query.get(transfer.asset_id)
    products = []
    try:
        from inventory_app.models.product import InvProduct
        products = InvProduct.query.filter_by(is_active=True).order_by(InvProduct.name).all()
    except Exception:
        pass
    if request.method == "POST":
        transfer.description = request.form.get("description", "")
        transfer.new_product_name = request.form.get("new_product_name", "").strip()
        product_id = request.form.get("product_id", type=int)
        transfer.product_id = product_id
        status = request.form.get("status", "unapproved")
        if status == "approved":
            stock_account_id = None
            stock_description = asset.name if asset else ""
            if product_id:
                try:
                    from inventory_app.models.product import InvProduct
                    prod = InvProduct.query.get(product_id)
                    if prod:
                        acct = create_entity_account("product", prod.id, prod.name)
                        stock_account_id = acct.id
                        stock_description = prod.name
                except Exception:
                    pass
            elif transfer.new_product_name:
                try:
                    from inventory_app.models.product import InvProduct
                    prod = InvProduct(name=transfer.new_product_name, sku=f"FA-{asset.asset_code}",
                                      unit_price=0, cost_price=0, current_stock=0, is_active=True)
                    db.session.add(prod)
                    db.session.flush()
                    acct = create_entity_account("product", prod.id, prod.name)
                    stock_account_id = acct.id
                    stock_description = prod.name
                    transfer.product_id = prod.id
                except Exception as e:
                    flash(f"Could not create product: {e}", "error")
                    return render_template("fixed_assets/transfers/form.html", asset=asset, products=products)
            else:
                stock_acct = posting_account("inventory")
                stock_account_id = stock_acct.id
            try:
                post_asset_depreciation(asset, date.today(), current_user.id)
            except Exception:
                pass
            purchase_cost = asset.purchase_cost
            accum_dep = asset.accumulated_depreciation
            net_book = purchase_cost - accum_dep
            if not asset.fixed_asset_account_id:
                fa_acct, _ = create_fixed_asset_accounts(asset, asset.name)
                asset.fixed_asset_account_id = fa_acct.id
            if not asset.accum_dep_account_id:
                _, accum_acct = create_fixed_asset_accounts(asset, asset.name)
                asset.accum_dep_account_id = accum_acct.id
            lines = [
                {"account_id": asset.accum_dep_account_id, "debit": accum_dep, "credit": 0,
                 "description": f"Derecognise accum dep - {asset.name}"},
                {"account_id": stock_account_id, "debit": net_book, "credit": 0,
                 "description": f"Transfer to stock - {stock_description}"},
                {"account_id": asset.fixed_asset_account_id, "debit": 0, "credit": purchase_cost,
                 "description": f"Derecognise asset - {asset.name}"},
            ]
            try:
                post_journal_entry(
                    voucher_type="FA-TRF", voucher_id=transfer.id,
                    voucher_number=transfer.voucher_number,
                    description=f"Transfer to inventory: {asset.name} at BV {net_book:,.0f}",
                    entry_date=date.today(), created_by=current_user.id, lines=lines,
                )
            except Exception as e:
                db.session.rollback()
                flash(f"Posting failed: {e}", "error")
                return render_template("fixed_assets/transfers/form.html", asset=asset, products=products)
            asset.status = "transferred"
            asset.is_active = False
            transfer.approved_by = current_user.id
            transfer.approved_at = datetime.utcnow()
            transfer.status = "approved"
        db.session.commit()
        flash(f"Transfer {transfer.voucher_number} updated.", "success")
        return redirect(url_for("fa_transfers.list_transfers"))
    return render_template("fixed_assets/transfers/form.html", asset=asset, products=products, transfer=transfer)


@fa_transfers_bp.route("/<int:id>/unapprove")
@login_required
def unapprove_transfer(id):
    if not current_user.module_access("fixed_assets"):
        return render_template("access_denied.html")
    transfer = AssetTransfer.query.get_or_404(id)
    if transfer.status != "approved":
        flash("Transfer is not approved.", "error")
        return redirect(url_for("fa_transfers.list_transfers"))
    asset = FixedAsset.query.get(transfer.asset_id)
    if asset:
        asset.status = "active"
        asset.is_active = True
    try:
        reverse_journal_entry("FA-TRF", transfer.id, created_by=current_user.id)
    except Exception as e:
        flash(f"Reversal failed: {e}", "error")
        return redirect(url_for("fa_transfers.list_transfers"))
    transfer.status = "unapproved"
    transfer.approved_by = None
    transfer.approved_at = None
    db.session.commit()
    flash(f"{transfer.voucher_number} unapproved and reversed.", "success")
    return redirect(url_for("fa_transfers.list_transfers"))


@fa_transfers_bp.route("/<int:id>/delete")
@login_required
def delete_transfer(id):
    if not current_user.module_access("fixed_assets"):
        return render_template("access_denied.html")
    transfer = AssetTransfer.query.get_or_404(id)
    if transfer.status == "approved":
        flash("Cannot delete an approved transfer. Unapprove it first.", "error")
        return redirect(url_for("fa_transfers.list_transfers"))
    db.session.delete(transfer)
    db.session.commit()
    flash(f"Transfer {transfer.voucher_number} deleted.", "success")
    return redirect(url_for("fa_transfers.list_transfers"))
