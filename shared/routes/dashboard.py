from flask import Blueprint, render_template, redirect, url_for
from flask_login import login_required, current_user

dashboard_bp = Blueprint("dashboard", __name__, url_prefix="/dashboard")


@dashboard_bp.route("/")
@login_required
def hub():
    # The hub is a company's front door: every tile behind it opens that
    # company's books, and module entitlement is a property of the company.
    # With none active there is nothing to show, so send the user to pick
    # one rather than render a hub belonging to nobody.
    from shared.tenancy import current_company_id
    if current_company_id() is None:
        return redirect(url_for("portal.index"))
    is_admin = current_user.is_admin()
    has_hr = current_user.module_access("hr")
    has_inv = current_user.module_access("inventory")
    has_invoicing = current_user.module_access("invoicing")
    has_finance = current_user.module_access("finance")
    has_accounting = current_user.module_access("accounting")
    has_fbr = current_user.module_access("fbr")
    has_fixed_assets = current_user.module_access("fixed_assets")
    has_executive = current_user.module_access("executive")
    if not (has_hr or has_inv or has_invoicing or has_finance or has_accounting
            or has_fbr or has_fixed_assets or has_executive):
        return render_template("dashboard/access_denied.html")

    # Every tile is data (shared/navigation.py MODULE_META supplies the brand
    # identity reused by the app shell), so the hub stays in sync with the
    # modules that actually exist instead of a pile of copy-pasted cards.
    from shared.navigation import MODULE_META
    modules = [
        {
            "key": "hr", "title": "People & HR", "endpoint": "dashboard",
            "desc": "Employees, attendance, leave, payroll, PF & more",
            "icon": "people",
        },
        {
            "key": "inventory", "title": "Inventory", "endpoint": "inv_auth.dashboard",
            "desc": "Products, stock movements, vouchers & reports",
            "icon": "box",
        },
        {
            "key": "invoicing", "title": "Invoicing", "endpoint": "invoicing.dashboard",
            "desc": "Customers, suppliers, invoices, returns & tracking",
            "icon": "receipt",
        },
        {
            "key": "finance", "title": "Finance", "endpoint": "finance.dashboard",
            "desc": "Ledger, trial balance, P&L, balance sheet, cash flow",
            "icon": "chart",
        },
        {
            "key": "accounting", "title": "Accounting", "endpoint": "accounting.dashboard",
            "desc": "Vouchers, chart of accounts & period books",
            "icon": "calc",
        },
        {
            "key": "fbr", "title": "FBR Digital Invoicing", "endpoint": "fbr_dashboard.dashboard",
            "desc": "DI submissions, validation & compliance",
            "icon": "shield",
        },
        {
            "key": "fixed_assets", "title": "Fixed Assets", "endpoint": "fa_auth.dashboard",
            "desc": "Assets, categories, depreciation & reports",
            "icon": "building",
        },
        {
            "key": "executive", "title": "Executive Reports", "endpoint": "executive.dashboard",
            "desc": "Receivables & payables, live from the ledger",
            "icon": "pulse",
        },
    ]
    flags = {
        "hr": has_hr, "inventory": has_inv, "invoicing": has_invoicing,
        "finance": has_finance, "accounting": has_accounting, "fbr": has_fbr,
        "fixed_assets": has_fixed_assets, "executive": has_executive,
    }
    visible = []
    for m in modules:
        if not flags[m["key"]]:
            continue
        meta = MODULE_META.get(m["key"], {})
        m["brand"] = meta.get("brand", "linear-gradient(135deg,#475569,#64748b)")
        m["letter"] = meta.get("letter", "?")
        m["badge_fg"] = meta.get("badge_fg", "#475569")
        m["badge_bg"] = meta.get("badge_bg", "#f1f5f9")
        visible.append(m)
    return render_template("dashboard/hub.html",
                           modules=visible, has_settings=is_admin)
