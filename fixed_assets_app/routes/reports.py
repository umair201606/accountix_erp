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
    # The register is the assets in service; depreciation is derived from the
    # posted charges (the cached column was summed before, and could lag a
    # reversal). The status summary covers every asset ever booked.
    everything = (FixedAsset.query
                  .filter(FixedAsset.status.notin_(("draft", "cancelled")))
                  .join(AssetCategory, AssetCategory.id == FixedAsset.category_id)
                  .order_by(AssetCategory.name, FixedAsset.name).all())
    assets = [a for a in everything if a.status == "active"]
    dep_of = {a.id: float(a.posted_depreciation or 0) for a in assets}
    total_cost = sum(float(a.purchase_cost or 0) for a in assets)
    total_dep = sum(dep_of.values())
    category_summary = {}
    for a in assets:
        cat_name = a.category_obj.name if a.category_obj else "Uncategorized"
        if cat_name not in category_summary:
            category_summary[cat_name] = {"count": 0, "cost": 0, "depreciation": 0}
        category_summary[cat_name]["count"] += 1
        category_summary[cat_name]["cost"] += float(a.purchase_cost or 0)
        category_summary[cat_name]["depreciation"] += dep_of[a.id]
    status_summary = {}
    for a in everything:
        s = a.status or "unknown"
        if s not in status_summary:
            status_summary[s] = {"count": 0, "cost": 0}
        status_summary[s]["count"] += 1
        status_summary[s]["cost"] += float(a.purchase_cost or 0)
    fmt = request.args.get("format")
    if fmt:
        return _export_register(fmt, assets, category_summary)
    return render_template("fixed_assets/reports/index.html",
                           assets=assets,
                           total_cost=total_cost,
                           total_dep=total_dep,
                           net_book_value=total_cost - total_dep,
                           category_summary=category_summary,
                           status_summary=status_summary)


@fa_reports_bp.route("/movement")
@login_required
def movement_schedule():
    """Cost and depreciation roll-forward by category for a period."""
    if not current_user.module_access("fixed_assets"):
        return render_template("access_denied.html")
    from shared.clock import local_today
    from shared.periods import period_for
    from ..movement import movement
    today = local_today()
    period = period_for(today)
    default_from = period.start_date if period else today.replace(month=1, day=1)
    try:
        date_from = date.fromisoformat(request.args.get("from") or str(default_from))
        date_to = date.fromisoformat(request.args.get("to") or str(today))
    except ValueError:
        date_from, date_to = default_from, today
    if date_to < date_from:
        date_from, date_to = date_to, date_from
    data = movement(date_from, date_to)
    fmt = request.args.get("format")
    if fmt:
        from shared import report_export as rx
        headers = ["Category", "Cost b/f", "Additions", "Disposals", "Cost c/f",
                   "Dep. b/f", "Charge for period", "On disposals", "Dep. c/f",
                   "NBV b/f", "NBV c/f"]
        keys = ("cost_open", "additions", "cost_disposals", "cost_close", "dep_open",
                "charge", "dep_disposals", "dep_close", "nbv_open", "nbv_close")
        rows = [[r["category"]] + [r[k] for k in keys] for r in data["rows"]]
        rows.append(["Total"] + [data["total"][k] for k in keys])
        return rx.export_table(fmt, "Fixed Asset Movement Schedule", headers, rows,
                               period=f"{date_from:%d %b %Y} to {date_to:%d %b %Y}",
                               row_kinds=["plain"] * (len(rows) - 1) + ["grand"],
                               file_period=date_to, landscape_mode=True)
    return render_template("fixed_assets/reports/movement.html", data=data,
                           date_from=date_from, date_to=date_to)


def _export_register(fmt, assets, category_summary):
    """Fixed asset register: every active asset with cost, depreciation
    posted to date and net book value; Excel adds the category summary."""
    from shared import report_export as rx
    headers = ["Code", "Asset", "Category", "Purchase date", "Location",
               "Assigned to", "Serial no.", "Method", "Life (yrs)", "Cost",
               "Salvage", "Acc. depreciation", "Net book value", "Status"]
    rows = [[a.asset_code, a.name, a.category_obj.name if a.category_obj else "",
             a.purchase_date, a.location or "", a.assigned_to or "",
             a.serial_number or "", (a.depreciation_method or "").replace("_", " ").title(),
             a.useful_life, float(a.purchase_cost or 0), float(a.salvage_value or 0),
             float(a.posted_depreciation or 0), float(a.net_book_value or 0),
             (a.status or "").title()] for a in assets]
    rows.append(["", f"Total ({len(assets)} assets)", "", "", "", "", "", "", "",
                 sum(r[9] for r in rows), sum(r[10] for r in rows),
                 sum(r[11] for r in rows), sum(r[12] for r in rows), ""])
    kinds = ["plain"] * len(assets) + ["grand"]
    from shared.clock import local_today
    today = local_today()
    title = "Fixed Asset Register"
    period = f"Assets in service as at {today:%d %b %Y}"
    if fmt in ("excel", "xlsx"):
        summary = [[name, d["count"], float(d["cost"]), float(d["depreciation"]),
                    float(d["cost"] - d["depreciation"])]
                   for name, d in category_summary.items()]
        buf = rx.build_excel(title, headers, rows, period=period, row_kinds=kinds,
                             col_formats={8: "0"},
                             extra_sheets=[("By category",
                                            ["Category", "Assets", "Cost",
                                             "Acc. depreciation", "Net book value"],
                                            summary)])
        return rx.send_export(buf, "excel", title, today)
    return rx.export_table(fmt, title, headers, rows, period=period,
                           row_kinds=kinds, col_formats={8: "0"},
                           file_period=today)
