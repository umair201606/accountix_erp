from flask import Blueprint, render_template, redirect, url_for, flash, request
from flask_login import login_required
from ..extensions import db
from shared.tenancy import scoped_get_404
from ..models.category import InvCategory

inv_cat_bp = Blueprint("inv_categories", __name__, url_prefix="/inventory/categories")


@inv_cat_bp.route("/")
@login_required
def list_categories():
    categories = InvCategory.query.order_by(InvCategory.name).all()
    return render_template("categories/list_inv.html", categories=categories)


@inv_cat_bp.route("/create", methods=["GET", "POST"])
@login_required
def create_category():
    if request.method == "POST":
        cat = InvCategory(
            name=request.form["name"],
            description=request.form.get("description", ""),
            parent_id=request.form.get("parent_id", type=int) or None,
        )
        db.session.add(cat)
        db.session.commit()
        flash("Category created", "success")
        return redirect(url_for("inv_categories.list_categories"))
    parents = InvCategory.query.filter_by(is_active=True).all()
    return render_template("categories/form_inv.html", category=None, parents=parents)


@inv_cat_bp.route("/edit/<int:id>", methods=["GET", "POST"])
@login_required
def edit_category(id):
    cat = scoped_get_404(InvCategory, id)
    if request.method == "POST":
        cat.name = request.form["name"]
        cat.description = request.form.get("description", "")
        cat.parent_id = request.form.get("parent_id", type=int) or None
        cat.is_active = request.form.get("is_active") == "on"
        db.session.commit()
        flash("Category updated", "success")
        return redirect(url_for("inv_categories.list_categories"))
    parents = InvCategory.query.filter(InvCategory.id != id, InvCategory.is_active == True).all()
    return render_template("categories/form_inv.html", category=cat, parents=parents)


@inv_cat_bp.route("/delete/<int:id>")
@login_required
def delete_category(id):
    cat = scoped_get_404(InvCategory, id)
    if cat.products.count() > 0:
        flash("Cannot delete category with products", "error")
    else:
        db.session.delete(cat)
        db.session.commit()
        flash("Category deleted", "success")
    return redirect(url_for("inv_categories.list_categories"))
