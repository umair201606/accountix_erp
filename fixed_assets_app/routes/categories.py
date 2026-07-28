from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required, current_user
from shared.extensions import db
from ..models.asset import AssetCategory

fa_categories_bp = Blueprint("fa_categories", __name__, url_prefix="/fixed-assets/categories")


@fa_categories_bp.route("/")
@login_required
def list_categories():
    if not current_user.module_access("fixed_assets"):
        return render_template("access_denied.html")
    categories = AssetCategory.query.order_by(AssetCategory.name).all()
    return render_template("fixed_assets/categories/index.html", categories=categories)


@fa_categories_bp.route("/create", methods=["GET", "POST"])
@login_required
def create_category():
    if not current_user.module_access("fixed_assets"):
        return render_template("access_denied.html")
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if not name:
            flash("Category name is required.", "error")
            return render_template("fixed_assets/categories/form.html", category=None)
        existing = AssetCategory.query.filter_by(name=name).first()
        if existing:
            flash(f"Category '{name}' already exists.", "error")
            return render_template("fixed_assets/categories/form.html", category=None)
        category = AssetCategory(
            name=name,
            description=request.form.get("description", ""),
            default_useful_life=int(request.form.get("default_useful_life", 5)),
            default_depreciation_method=request.form.get("default_depreciation_method", "straight_line"),
            default_salvage_value_pct=float(request.form.get("default_salvage_value_pct", 0)),
        )
        db.session.add(category)
        db.session.commit()
        flash(f"Category '{name}' created.", "success")
        return redirect(url_for("fa_categories.list_categories"))
    return render_template("fixed_assets/categories/form.html", category=None)


@fa_categories_bp.route("/<int:category_id>/edit", methods=["GET", "POST"])
@login_required
def edit_category(category_id):
    if not current_user.module_access("fixed_assets"):
        return render_template("access_denied.html")
    category = AssetCategory.query.get_or_404(category_id)
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if not name:
            flash("Category name is required.", "error")
            return render_template("fixed_assets/categories/form.html", category=category)
        dup = AssetCategory.query.filter(AssetCategory.name == name, AssetCategory.id != category.id).first()
        if dup:
            flash(f"Category '{name}' already exists.", "error")
            return render_template("fixed_assets/categories/form.html", category=category)
        category.name = name
        category.description = request.form.get("description", "")
        category.default_useful_life = int(request.form.get("default_useful_life", 5))
        category.default_depreciation_method = request.form.get("default_depreciation_method", "straight_line")
        category.default_salvage_value_pct = float(request.form.get("default_salvage_value_pct", 0))
        category.is_active = request.form.get("is_active") == "1"
        db.session.commit()
        flash("Category updated.", "success")
        return redirect(url_for("fa_categories.list_categories"))
    return render_template("fixed_assets/categories/form.html", category=category)
