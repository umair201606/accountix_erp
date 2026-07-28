from datetime import date
from flask import Blueprint, render_template
from flask_login import login_required, current_user
from shared.extensions import db
from ..models.asset import FixedAsset, AssetCategory, AssetDepreciation

fa_auth_bp = Blueprint("fa_auth", __name__, url_prefix="/fixed-assets")


@fa_auth_bp.route("/")
@fa_auth_bp.route("/dashboard")
@login_required
def dashboard():
    if not current_user.module_access("fixed_assets"):
        return render_template("access_denied.html")
    total_assets = FixedAsset.query.filter_by(is_active=True).count()
    total_cost = db.session.query(db.func.sum(FixedAsset.purchase_cost)).filter_by(is_active=True).scalar() or 0
    total_depreciation = db.session.query(db.func.sum(FixedAsset.accumulated_depreciation)).filter_by(is_active=True).scalar() or 0
    active_count = FixedAsset.query.filter_by(status="active", is_active=True).count()
    disposed_count = FixedAsset.query.filter_by(status="disposed", is_active=True).count()
    cat_count = AssetCategory.query.filter_by(is_active=True).count()
    recent_assets = FixedAsset.query.filter_by(is_active=True).order_by(FixedAsset.created_at.desc()).limit(5).all()
    return render_template("fixed_assets/dashboard/index.html",
                           total_assets=total_assets,
                           total_cost=total_cost,
                           total_depreciation=total_depreciation,
                           net_book_value=total_cost - total_depreciation,
                           active_count=active_count,
                           disposed_count=disposed_count,
                           categories=cat_count,
                           recent_assets=recent_assets)
