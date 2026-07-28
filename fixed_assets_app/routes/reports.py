from flask import Blueprint, render_template, request
from flask_login import login_required, current_user
from shared.extensions import db
from ..models.asset import FixedAsset, AssetCategory, AssetDepreciation
from datetime import date

fa_reports_bp = Blueprint("fa_reports", __name__, url_prefix="/fixed-assets/reports")


@fa_reports_bp.route("/")
@login_required
def index():
    if not current_user.module_access("fixed_assets"):
        return render_template("access_denied.html")
    assets = FixedAsset.query.filter_by(is_active=True)\
        .join(AssetCategory, AssetCategory.id == FixedAsset.category_id)\
        .order_by(AssetCategory.name, FixedAsset.name).all()
    total_cost = sum(a.purchase_cost for a in assets)
    total_dep = sum(a.accumulated_depreciation for a in assets)
    category_summary = {}
    for a in assets:
        cat_name = a.category_obj.name if a.category_obj else "Uncategorized"
        if cat_name not in category_summary:
            category_summary[cat_name] = {"count": 0, "cost": 0, "depreciation": 0}
        category_summary[cat_name]["count"] += 1
        category_summary[cat_name]["cost"] += a.purchase_cost
        category_summary[cat_name]["depreciation"] += a.accumulated_depreciation
    status_summary = {}
    for a in assets:
        s = a.status or "unknown"
        if s not in status_summary:
            status_summary[s] = {"count": 0, "cost": 0}
        status_summary[s]["count"] += 1
        status_summary[s]["cost"] += a.purchase_cost
    return render_template("fixed_assets/reports/index.html",
                           assets=assets,
                           total_cost=total_cost,
                           total_dep=total_dep,
                           net_book_value=total_cost - total_dep,
                           category_summary=category_summary,
                           status_summary=status_summary)
