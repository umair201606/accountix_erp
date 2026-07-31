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
from shared import costing
from ..models.asset import FixedAsset, AssetCategory
from .depreciation import post_asset_depreciation, due_depreciation


def _capitalise_from_stock(transfer, product, qty, created_by):
    """Approve an inventory->fixed-asset transfer: issue stock, capitalise it.

    The costing engine decides what the stock cost (FIFO layers or the running
    average) and returns that figure, so the asset is capitalised at exactly the
    value the credit takes out of inventory — the GL and the stock valuation
    cannot disagree.

    No FA-ACQ is posted for these assets: this journal IS the acquisition.
    """
    asset = scoped_get(FixedAsset, transfer.asset_id)
    unit_cost, total_cost = costing.record_out(
        product_id=product.id, voucher_type="FA-CAP", voucher_id=transfer.id,
        voucher_number=transfer.voucher_number, qty=qty,
        notes=f"Capitalised as fixed asset {asset.asset_code}",
        created_by=created_by,
    )
    total_cost = float(total_cost)
    if not asset.fixed_asset_account_id:
        fa_acct, _ = create_fixed_asset_accounts(asset, asset.name)
        asset.fixed_asset_account_id = fa_acct.id
        db.session.flush()
    stock_account = create_entity_account("product", product.id, product.name)
    post_journal_entry(
        voucher_type="FA-CAP", voucher_id=transfer.id,
        voucher_number=transfer.voucher_number,
        description=f"Capitalise {product.name} as fixed asset {asset.asset_code}",
        entry_date=date.today(), created_by=created_by,
        lines=[
            {"account_id": asset.fixed_asset_account_id,
             "debit": total_cost, "credit": 0,
             "description": f"Fixed asset - {asset.name}"},
            {"account_id": stock_account.id, "debit": 0, "credit": total_cost,
             "description": f"Transfer from stock - {product.name}"},
        ],
    )
    asset.purchase_cost = total_cost
    asset.status = "active"
    asset.is_active = True
    asset.recalculate()
    transfer.transfer_amount = total_cost
    transfer.approved_by = created_by
    transfer.approved_at = datetime.utcnow()
    transfer.status = "approved"
    return asset


def _approve_transfer(transfer, asset, stock_account_id, stock_description,
                      created_by):
    """Post an approved asset->inventory transfer: journal AND stock.

    Both halves are keyed ``("FA-TRF", transfer.id)`` so unapproving reverses
    them together. The journal alone used to be posted, which debited the stock
    account in the GL while the costing engine knew nothing about it — no layer,
    no quantity, and the inventory control account permanently adrift from the
    stock valuation.
    """
    post_asset_depreciation(asset, date.today(), created_by)
    db.session.flush()
    purchase_cost = asset.purchase_cost
    accum_dep = asset.posted_depreciation
    net_book = purchase_cost - accum_dep

    if not asset.fixed_asset_account_id or not asset.accum_dep_account_id:
        fa_acct, accum_acct = create_fixed_asset_accounts(asset, asset.name)
        asset.fixed_asset_account_id = asset.fixed_asset_account_id or fa_acct.id
        asset.accum_dep_account_id = asset.accum_dep_account_id or accum_acct.id
        db.session.flush()

    lines = [
        {"account_id": asset.accum_dep_account_id, "debit": accum_dep, "credit": 0,
         "description": f"Derecognise accum dep - {asset.name}"},
        {"account_id": stock_account_id, "debit": net_book, "credit": 0,
         "description": f"Transfer to stock - {stock_description}"},
        {"account_id": asset.fixed_asset_account_id, "debit": 0, "credit": purchase_cost,
         "description": f"Derecognise asset - {asset.name}"},
    ]
    post_journal_entry(
        voucher_type="FA-TRF", voucher_id=transfer.id,
        voucher_number=transfer.voucher_number,
        description=f"Transfer to inventory: {asset.name} at BV {net_book:,.0f}",
        entry_date=date.today(), created_by=created_by, lines=lines,
    )
    # The asset becomes one unit of stock carried at its net book value, so the
    # layer the costing engine opens is worth exactly what the journal debited.
    if transfer.product_id:
        costing.record_in(
            product_id=transfer.product_id, voucher_type="FA-TRF",
            voucher_id=transfer.id, voucher_number=transfer.voucher_number,
            qty=1, unit_cost=net_book,
            notes=f"Transferred from fixed asset {asset.asset_code}",
            created_by=created_by,
        )
    transfer.transfer_amount = net_book
    asset.status = "transferred"
    asset.is_active = False
    asset.recalculate()
    transfer.approved_by = created_by
    transfer.approved_at = datetime.utcnow()
    transfer.status = "approved"


fa_transfers_bp = Blueprint("fa_transfers", __name__, url_prefix="/fixed-assets/transfers")


@fa_transfers_bp.route("/")
@login_required
def list_transfers():
    if not current_user.module_access("fixed_assets"):
        return render_template("access_denied.html")
    transfers = AssetTransfer.query.order_by(AssetTransfer.created_at.desc()).all()
    return render_template("fixed_assets/transfers/list.html", transfers=transfers)


@fa_transfers_bp.route("/capitalise", methods=["GET", "POST"])
@login_required
def capitalise_from_stock():
    """Take an item out of stock and capitalise it as a fixed asset.

    The counterpart to create_transfer: the ``direction`` column and
    ``source_product_id`` existed for this from the start but nothing ever
    wrote them.
    """
    if not current_user.module_access("fixed_assets"):
        return render_template("access_denied.html")
    from inventory_app.models.product import InvProduct
    products = InvProduct.query.filter_by(is_active=True).order_by(InvProduct.name).all()
    categories = AssetCategory.query.filter_by(is_active=True).order_by(AssetCategory.name).all()
    if request.method == "POST":
        product_id = request.form.get("source_product_id", type=int)
        product = scoped_get(InvProduct, product_id) if product_id else None
        qty = request.form.get("quantity", type=float) or 1
        name = request.form.get("name", "").strip()
        category_id = request.form.get("category_id", type=int)
        category = scoped_get(AssetCategory, category_id) if category_id else None
        if not product or not name or not category:
            flash("Pick a stock item, an asset name and a category.", "error")
            return render_template("fixed_assets/transfers/capitalise.html",
                                   products=products, categories=categories)
        if qty <= 0:
            flash("Quantity must be positive.", "error")
            return render_template("fixed_assets/transfers/capitalise.html",
                                   products=products, categories=categories)
        last = FixedAsset.query.order_by(FixedAsset.id.desc()).first()
        asset = FixedAsset(
            asset_code=f"FA-{(last.id + 1) if last else 1:04d}",
            name=name, category_id=category.id, purchase_date=date.today(),
            purchase_cost=0, useful_life=int(request.form.get(
                "useful_life", type=int) or category.default_useful_life),
            depreciation_method=request.form.get(
                "depreciation_method", category.default_depreciation_method),
            salvage_value=float(request.form.get("salvage_value", 0) or 0),
            current_book_value=0, status="active",
            location=request.form.get("location", ""),
            notes=request.form.get("description", ""),
        )
        db.session.add(asset)
        db.session.flush()
        transfer = AssetTransfer(
            voucher_number=VoucherNumber.next("FA-CAP"),
            direction="to_fixed_asset", asset_id=asset.id,
            source_product_id=product.id, product_id=product.id,
            description=request.form.get(
                "description", f"Capitalise {product.name} as {name}"),
            status="unapproved", created_by=current_user.id,
        )
        db.session.add(transfer)
        db.session.flush()
        if request.form.get("status", "unapproved") == "approved":
            try:
                _capitalise_from_stock(transfer, product, qty, current_user.id)
            except Exception as e:
                db.session.rollback()
                flash(f"Capitalisation failed: {e}", "error")
                return render_template("fixed_assets/transfers/capitalise.html",
                                       products=products, categories=categories)
        db.session.commit()
        flash(f"{transfer.voucher_number} saved.", "success")
        return redirect(url_for("fa_transfers.list_transfers"))
    return render_template("fixed_assets/transfers/capitalise.html",
                           products=products, categories=categories)


@fa_transfers_bp.route("/<int:id>/unapprove-capitalisation")
@login_required
def unapprove_capitalisation(id):
    if not current_user.module_access("fixed_assets"):
        return render_template("access_denied.html")
    transfer = scoped_get_404(AssetTransfer, id)
    if transfer.status != "approved" or transfer.direction != "to_fixed_asset":
        flash("Not an approved capitalisation.", "error")
        return redirect(url_for("fa_transfers.list_transfers"))
    asset = scoped_get(FixedAsset, transfer.asset_id)
    if asset and asset.live_depreciation_query().count():
        flash("Reverse this asset's depreciation charges first.", "error")
        return redirect(url_for("fa_transfers.list_transfers"))
    try:
        costing.reverse_voucher_stock("FA-CAP", transfer.id,
                                      created_by=current_user.id)
        reverse_journal_entry("FA-CAP", transfer.id, created_by=current_user.id)
    except Exception as e:
        db.session.rollback()
        flash(f"Reversal failed: {e}", "error")
        return redirect(url_for("fa_transfers.list_transfers"))
    if asset:
        asset.status = "inactive"
        asset.is_active = False
        asset.purchase_cost = 0
        asset.recalculate()
    transfer.status = "unapproved"
    transfer.approved_by = None
    transfer.approved_at = None
    db.session.commit()
    flash(f"{transfer.voucher_number} unapproved; stock returned.", "success")
    return redirect(url_for("fa_transfers.list_transfers"))


@fa_transfers_bp.route("/create", methods=["GET", "POST"])
@login_required
def create_transfer():
    if not current_user.module_access("fixed_assets"):
        return render_template("access_denied.html")
    asset_id = request.args.get("asset_id", type=int)
    asset = scoped_get(FixedAsset, asset_id) if asset_id else None
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
                prod = scoped_get(InvProduct, product_id)
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
            try:
                _approve_transfer(transfer, asset, stock_account_id,
                                  stock_description, current_user.id)
            except Exception as e:
                db.session.rollback()
                flash(f"Posting failed: {e}", "error")
                return render_template("fixed_assets/transfers/form.html", asset=asset, products=products)
        db.session.commit()
        flash(f"Transfer {voucher_number} {'approved and ' if status == 'approved' else ''}saved.", "success")
        return redirect(url_for("fa_transfers.list_transfers"))
    return render_template("fixed_assets/transfers/form.html", asset=asset, products=products)


@fa_transfers_bp.route("/<int:id>/edit", methods=["GET", "POST"])
@login_required
def edit_transfer(id):
    if not current_user.module_access("fixed_assets"):
        return render_template("access_denied.html")
    transfer = scoped_get_404(AssetTransfer, id)
    if transfer.status == "approved":
        flash("Cannot edit an approved transfer.", "error")
        return redirect(url_for("fa_transfers.list_transfers"))
    asset = scoped_get(FixedAsset, transfer.asset_id)
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
                    prod = scoped_get(InvProduct, product_id)
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
                _approve_transfer(transfer, asset, stock_account_id,
                                  stock_description, current_user.id)
            except Exception as e:
                db.session.rollback()
                flash(f"Posting failed: {e}", "error")
                return render_template("fixed_assets/transfers/form.html", asset=asset, products=products)
        db.session.commit()
        flash(f"Transfer {transfer.voucher_number} updated.", "success")
        return redirect(url_for("fa_transfers.list_transfers"))
    return render_template("fixed_assets/transfers/form.html", asset=asset, products=products, transfer=transfer)


@fa_transfers_bp.route("/<int:id>/unapprove")
@login_required
def unapprove_transfer(id):
    if not current_user.module_access("fixed_assets"):
        return render_template("access_denied.html")
    transfer = scoped_get_404(AssetTransfer, id)
    if transfer.status != "approved":
        flash("Transfer is not approved.", "error")
        return redirect(url_for("fa_transfers.list_transfers"))
    asset = scoped_get(FixedAsset, transfer.asset_id)
    # Withdraw the stock first: if that transferred unit has already been sold
    # or consumed, the costing engine refuses (ConsumedLayerError) and nothing
    # else has been touched yet, so the voucher stays consistently approved.
    try:
        costing.reverse_voucher_stock("FA-TRF", transfer.id,
                                      created_by=current_user.id)
        reverse_journal_entry("FA-TRF", transfer.id, created_by=current_user.id)
    except Exception as e:
        db.session.rollback()
        flash(f"Reversal failed: {e}", "error")
        return redirect(url_for("fa_transfers.list_transfers"))
    if asset:
        asset.status = "active"
        asset.is_active = True
        asset.recalculate()
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
    transfer = scoped_get_404(AssetTransfer, id)
    if transfer.status == "approved":
        flash("Cannot delete an approved transfer. Unapprove it first.", "error")
        return redirect(url_for("fa_transfers.list_transfers"))
    db.session.delete(transfer)
    db.session.commit()
    flash(f"Transfer {transfer.voucher_number} deleted.", "success")
    return redirect(url_for("fa_transfers.list_transfers"))
