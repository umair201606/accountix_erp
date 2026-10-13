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
    # Disposal sets is_active False, so "disposed AND is_active" was always 0;
    # and the totals summed the cached columns. Counts are by status, money
    # from the assets in service, depreciation derived from posted charges.
    in_service = FixedAsset.query.filter_by(status="active").all()
    total_cost = sum(float(a.purchase_cost or 0) for a in in_service)
    total_depreciation = sum(float(a.posted_depreciation or 0) for a in in_service)
    counts = dict(db.session.query(FixedAsset.status, db.func.count(FixedAsset.id))
                  .group_by(FixedAsset.status).all())
    cat_count = AssetCategory.query.filter_by(is_active=True).count()
    recent_assets = (FixedAsset.query.filter(FixedAsset.status.notin_(("draft", "cancelled")))
                     .order_by(FixedAsset.created_at.desc()).limit(5).all())
    return render_template("fixed_assets/dashboard/index.html",
                           total_assets=len(in_service),
                           total_cost=total_cost,
                           total_depreciation=total_depreciation,
                           net_book_value=total_cost - total_depreciation,
                           active_count=counts.get("active", 0),
                           disposed_count=counts.get("disposed", 0),
                           categories=cat_count,
                           recent_assets=recent_assets)
