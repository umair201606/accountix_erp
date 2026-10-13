from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required, current_user
from shared.extensions import db
from shared.tenancy import scoped_get, scoped_get_404
from shared.models.asset_transfer import AssetTransfer
from shared.ledger_utils import reverse_journal_entry
from shared.forms import form_date, form_float, form_int

inv_transfers_bp = Blueprint("inv_transfers", __name__, url_prefix="/inventory/transfer-to-fa")

# Capitalisations raised here are the same vouchers the Fixed Assets module
# raises ("to_fixed_asset"); "from_inventory" is the direction older rows used.
_DIRECTIONS = ("to_fixed_asset", "from_inventory")


def _assets():
    from fixed_assets_app.models.asset import FixedAsset, AssetCategory
    return FixedAsset, AssetCategory


def _form_lists():
    from inventory_app.models.product import InvProduct
    _, AssetCategory = _assets()
    return (InvProduct.query.filter_by(is_active=True).order_by(InvProduct.name).all(),
            AssetCategory.query.filter_by(is_active=True).all())


@inv_transfers_bp.route("/")
@login_required
def list_transfers():
    if not current_user.module_access("inventory"):
        return render_template("access_denied.html")
    # Filtered on "from_inventory" only, while this module creates
    # "to_fixed_asset" vouchers: the list never showed what was raised here.
    transfers = AssetTransfer.query.filter(AssetTransfer.direction.in_(_DIRECTIONS))\
        .order_by(AssetTransfer.created_at.desc()).all()
    return render_template("transfers/list.html", transfers=transfers)


@inv_transfers_bp.route("/create", methods=["GET", "POST"])
@login_required
def create_transfer():
    if not current_user.module_access("inventory"):
        return render_template("access_denied.html")
    products, categories = _form_lists()
    if request.method == "POST":
        from inventory_app.models.product import InvProduct
        from fixed_assets_app.routes.transfers import (new_capitalisation,
                                                       approve_capitalisation)
        _, AssetCategory = _assets()
        prod = scoped_get(InvProduct, request.form.get("product_id", type=int) or 0)
        category = scoped_get(AssetCategory, form_int("category_id", 0))
        if prod is None or category is None:
            flash("Select a product and an asset category.", "error")
            return render_template("transfers/form.html", products=products, categories=categories)
        qty = form_float("quantity", 1)
        salvage = form_float("salvage_value", 0)
        if qty <= 0 or salvage < 0:
            flash("Quantity must be positive and salvage value cannot be negative.", "error")
            return render_template("transfers/form.html", products=products, categories=categories)
        name = request.form.get("name", "").strip() or prod.name
        # The asset's cost is what the costing engine says the stock cost, set
        # on approval. A typed-in price used to be stored as the cost of a
        # draft asset that the depreciation run then charged.
        transfer = new_capitalisation(
            prod, qty, name, category, form_date("purchase_date"), current_user.id,
            voucher_prefix="INV-FA",
            useful_life=form_int("useful_life", category.default_useful_life or 5),
            depreciation_method=request.form.get("depreciation_method"),
            salvage_value=salvage,
            description=request.form.get("description", ""),
            location=request.form.get("location", ""),
            serial_number=request.form.get("serial_number", ""),
            notes=request.form.get("notes", ""))
        transfer.description = f"Transfer from inventory: {name}"
        approved = request.form.get("status", "unapproved") == "approved"
        if approved:
            try:
                approve_capitalisation(transfer, current_user.id, qty)
            except Exception as e:
                db.session.rollback()
                flash(f"Posting failed: {e}", "error")
                return render_template("transfers/form.html", products=products, categories=categories)
        db.session.commit()
        flash(f"Transfer {transfer.voucher_number} {'approved and ' if approved else ''}saved.", "success")
        return redirect(url_for("inv_transfers.list_transfers"))
    return render_template("transfers/form.html", products=products, categories=categories)


@inv_transfers_bp.route("/<int:id>/edit", methods=["GET", "POST"])
@login_required
def edit_transfer(id):
    if not current_user.module_access("inventory"):
        return render_template("access_denied.html")
    transfer = scoped_get_404(AssetTransfer, id)
    if transfer.direction not in _DIRECTIONS:
        flash("That transfer belongs to Fixed Assets.", "error")
        return redirect(url_for("inv_transfers.list_transfers"))
    if transfer.status == "approved":
        flash("Cannot edit an approved transfer.", "error")
        return redirect(url_for("inv_transfers.list_transfers"))
    FixedAsset, _ = _assets()
    asset = scoped_get(FixedAsset, transfer.asset_id)
    products, categories = _form_lists()
    if request.method == "POST":
        transfer.description = request.form.get("description", "") or transfer.description
        if request.form.get("status", "unapproved") == "approved":
            from inventory_app.models.product import InvProduct
            from fixed_assets_app.routes.transfers import approve_capitalisation
            product_id = request.form.get("product_id", type=int) or transfer.source_product_id
            prod = scoped_get(InvProduct, product_id) if product_id else None
            if prod is None or asset is None:
                flash("Select the stock item to capitalise.", "error")
                return redirect(url_for("inv_transfers.edit_transfer", id=transfer.id))
            transfer.product_id = transfer.source_product_id = prod.id
            transfer.direction = "to_fixed_asset"
            try:
                approve_capitalisation(transfer, current_user.id,
                                       form_float("quantity", 0) or None)
            except Exception as e:
                db.session.rollback()
                flash(f"Posting failed: {e}", "error")
                return redirect(url_for("inv_transfers.edit_transfer", id=transfer.id))
        db.session.commit()
        flash(f"Transfer {transfer.voucher_number} updated.", "success")
        return redirect(url_for("inv_transfers.list_transfers"))
    return render_template("transfers/form.html", products=products, categories=categories,
                           transfer=transfer, asset=asset)


@inv_transfers_bp.route("/<int:id>/unapprove", methods=["POST"])
@login_required
def unapprove_transfer(id):
    if not current_user.module_access("inventory"):
        return render_template("access_denied.html")
    transfer = scoped_get_404(AssetTransfer, id)
    if transfer.status != "approved" or transfer.direction not in _DIRECTIONS:
        flash("Transfer is not an approved capitalisation.", "error")
        return redirect(url_for("inv_transfers.list_transfers"))
    FixedAsset, _ = _assets()
    asset = scoped_get(FixedAsset, transfer.asset_id)
    if asset and asset.live_depreciation_query().count():
        flash("Reverse this asset's depreciation charges first.", "error")
        return redirect(url_for("inv_transfers.list_transfers"))
    if asset and asset.status != "active":
        flash("The asset is no longer in service (disposed or transferred); "
              "reverse that first.", "error")
        return redirect(url_for("inv_transfers.list_transfers"))
    try:
        # Journal first (refuses a closed period), then the stock comes back
        # to the shelf; "INV-FA" covers transfers approved before they moved
        # stock through the costing engine.
        reverse_journal_entry("FA-CAP", transfer.id, created_by=current_user.id)
        reverse_journal_entry("INV-FA", transfer.id, created_by=current_user.id)
        from shared import costing
        costing.reverse_voucher_stock("FA-CAP", transfer.id,
                                      created_by=current_user.id)
    except Exception as e:
        db.session.rollback()
        flash(f"Reversal failed: {e}", "error")
        return redirect(url_for("inv_transfers.list_transfers"))
    if asset:
        # Kept as a draft: the voucher can be approved again.
        asset.status = "draft"
        asset.is_active = False
        asset.purchase_cost = 0
        asset.recalculate()
    transfer.status = "unapproved"
    transfer.approved_by = None
    transfer.approved_at = None
    db.session.commit()
    flash(f"{transfer.voucher_number} unapproved and reversed.", "success")
    return redirect(url_for("inv_transfers.list_transfers"))


@inv_transfers_bp.route("/<int:id>/delete", methods=["POST"])
@login_required
def delete_transfer(id):
    if not current_user.module_access("inventory"):
        return render_template("access_denied.html")
    transfer = scoped_get_404(AssetTransfer, id)
    if transfer.direction not in _DIRECTIONS:
        flash("That transfer belongs to Fixed Assets.", "error")
        return redirect(url_for("inv_transfers.list_transfers"))
    if transfer.status == "approved":
        flash("Cannot delete an approved transfer. Unapprove it first.", "error")
        return redirect(url_for("inv_transfers.list_transfers"))
    from fixed_assets_app.routes.transfers import discard_draft_asset
    number = transfer.voucher_number
    discard_draft_asset(transfer)
    if transfer in db.session:
        db.session.delete(transfer)
    db.session.commit()
    flash(f"Transfer {number} deleted.", "success")
    return redirect(url_for("inv_transfers.list_transfers"))
