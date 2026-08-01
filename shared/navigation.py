"""Sidebar navigation registry — one definition per module, rendered by
``templates/layouts/app_shell.html``.

Before this existed each module hand-wrote its own sidebar markup, which is how
they drifted apart (different footers, different brand handling, and four of
five with a broken mobile toggle). Keeping the nav as data means the shell can
render every module identically and the common header/footer stay in one place.

An item is a dict:
    endpoint  url_for target (required)
    label     link text; may be a callable(ctx) for flow-dependent labels
    icon      HTML entity string
    active    how to decide the highlighted item (see ``_is_active``):
                {"exact": [...]} | {"prefix": "x."} | {"prefix": ..., "exclude": [...]}
              defaults to exact match on ``endpoint``
    gate      optional callable(user, ctx) -> bool; item hidden when False

A group is (name, icon, [items]) and renders as a collapsible section.
A bare item (not in a group) renders flat above the groups.
"""

# module_key -> presentation. brand/badge colours preserve each module's
# existing identity now that the markup is shared.
MODULE_META = {
    "hr": {
        "label": "Accountix ERP", "badge": "HR", "letter": "A",
        "brand": "linear-gradient(135deg,#0f5257,#1a7a7a)",
        "badge_bg": "#e0f2f1", "badge_fg": "#0f5257",
        "home": "dashboard",
    },
    "inventory": {
        "label": "Accountix Inventory", "badge": "INVENTORY", "letter": "I",
        "brand": "linear-gradient(135deg,#7c3aed,#8b5cf6)",
        "badge_bg": "#f3e8ff", "badge_fg": "#7c3aed",
        "home": "inv_auth.dashboard",
    },
    "invoicing": {
        "label": "Accountix Invoicing", "badge": "INVOICING", "letter": "V",
        "brand": "linear-gradient(135deg,#0d9488,#14b8a6)",
        "badge_bg": "#ccfbf1", "badge_fg": "#0d9488",
        "home": "invoicing.dashboard",
    },
    "finance": {
        "label": "Accountix Finance", "badge": "FINANCE", "letter": "F",
        "brand": "linear-gradient(135deg,#1d4ed8,#3b82f6)",
        "badge_bg": "#dbeafe", "badge_fg": "#1d4ed8",
        "home": "finance.dashboard",
    },
    "accounting": {
        "label": "Accountix Accounting", "badge": "ACCOUNTING", "letter": "A",
        "brand": "linear-gradient(135deg,#0f5257,#1a7a7a)",
        "badge_bg": "#e0f2f1", "badge_fg": "#0f5257",
        "home": "accounting.dashboard",
    },
    "fbr": {
        "label": "FBR Digital Invoicing", "badge": "FBR", "letter": "F",
        "brand": "linear-gradient(135deg,#b5790a,#d4952e)",
        "badge_bg": "#faf1de", "badge_fg": "#b5790a",
        "home": "fbr_dashboard.dashboard",
    },
    "settings": {
        "label": "Accountix Settings", "badge": "SETTINGS", "letter": "A",
        "brand": "linear-gradient(135deg,#475569,#64748b)",
        "badge_bg": "#f1f5f9", "badge_fg": "#475569",
        "home": "settings.index",
    },
    "fixed_assets": {
        "label": "Accountix Fixed Assets", "badge": "FA", "letter": "F",
        "brand": "linear-gradient(135deg,#b91c1c,#dc2626)",
        "badge_bg": "#fef2f2", "badge_fg": "#b91c1c",
        "home": "fa_auth.dashboard",
    },
}


def _admin(user, ctx):
    return user.is_admin()


def _admin_or_manager(user, ctx):
    return user.is_admin() or user.is_manager()


def _with_po(user, ctx):
    return ctx.get("purchase_flow") == "with_po"


def _with_so(user, ctx):
    return ctx.get("sales_flow") == "with_so"


# ── Per-module navigation ────────────────────────────────────────────────────
# flat: items rendered above the groups. groups: collapsible sections.
NAV = {
    "hr": {
        "flat": [
            {"endpoint": "dashboard", "icon": "&#9679;", "label": "Dashboard"},
        ],
        "groups": [
            ("Self Service", "&#128736;", [
                {"endpoint": "attendance.index", "icon": "&#9200;", "label": "Attendance",
                 "active": {"prefix": "attendance.",
                            "exclude": ["attendance.admin_view", "attendance.policies"]}},
                {"endpoint": "leave.index", "icon": "&#128197;", "label": "Leaves",
                 "active": {"prefix": "leave.",
                            "exclude": ["leave.holidays", "leave.workflows"]}},
                {"endpoint": "timesheet.index", "icon": "&#128203;", "label": "Timesheets",
                 "active": {"prefix": "timesheet.", "exclude": ["timesheet.projects"]}},
                {"endpoint": "ess.index", "icon": "&#128100;", "label": "My Profile",
                 "active": {"prefix": "ess."}},
                {"endpoint": "digital_files.index", "icon": "&#128193;", "label": "My Files",
                 "active": {"prefix": "digital_files.", "exclude": ["digital_files.admin"]}},
            ]),
            ("Financial", "&#128188;", [
                {"endpoint": "compensation.index", "icon": "&#128176;", "label": "Compensation",
                 "active": {"prefix": "compensation."}},
                {"endpoint": "pf.index", "icon": "&#128179;", "label": "Provident Fund",
                 "active": {"prefix": "pf.",
                            "exclude": ["pf.config", "pf.button_permissions"]}},
            ]),
        ],
        "flat_after": [
            {"endpoint": "workplace.index", "icon": "&#128187;", "label": "Workplace",
             "active": {"prefix": "workplace."}},
        ],
        "groups_after": [
            ("Management", "&#128202;", [
                {"endpoint": "mss.index", "icon": "&#128101;", "label": "My Team",
                 "active": {"exact": ["mss.index", "mss.team", "mss.evaluate",
                                      "mss.team_calendar", "mss.team_availability"]},
                 "gate": _admin_or_manager},
                {"endpoint": "mss.approvals", "icon": "&#9989;", "label": "Approvals",
                 "gate": _admin_or_manager},
                {"endpoint": "reports.index", "icon": "&#128200;", "label": "Reports",
                 "active": {"prefix": "reports."}, "gate": _admin_or_manager},
            ]),
            ("Administration", "&#128295;", [
                {"endpoint": "communications.index", "icon": "&#128231;", "label": "Notifications", "gate": _admin},
                {"endpoint": "attendance.admin_view", "icon": "&#128197;", "label": "All Attendance", "gate": _admin},
                {"endpoint": "attendance.policies", "icon": "&#9881;", "label": "Time Policies", "gate": _admin},
                {"endpoint": "leave.holidays", "icon": "&#127775;", "label": "Holidays", "gate": _admin},
                {"endpoint": "leave.workflows", "icon": "&#128295;", "label": "Workflows", "gate": _admin},
                {"endpoint": "timesheet.projects", "icon": "&#128194;", "label": "Projects", "gate": _admin},
                {"endpoint": "digital_files.admin", "icon": "&#128193;", "label": "All Files", "gate": _admin},
                {"endpoint": "pf.config", "icon": "&#9881;", "label": "PF Config", "gate": _admin},
                {"endpoint": "compensation.tax_settings", "icon": "&#9881;", "label": "Income Tax Slabs", "gate": _admin},
                {"endpoint": "auth.user_list", "icon": "&#128101;", "label": "Users",
                 "active": {"exact": ["auth.user_list", "auth.member_assign",
                                      "auth.user_edit"]},
                 "gate": _admin},
            ]),
        ],
    },
    "inventory": {
        "flat": [
            {"endpoint": "inv_auth.dashboard", "icon": "&#9679;", "label": "Dashboard"},
        ],
        "groups": [
            ("Inventory", "&#128230;", [
                {"endpoint": "inv_products.list_products", "icon": "&#9632;", "label": "Products",
                 "active": {"prefix": "inv_products"}},
                {"endpoint": "inv_categories.list_categories", "icon": "&#9632;", "label": "Categories",
                 "active": {"prefix": "inv_categories"}},
                {"endpoint": "inv_units.list_units", "icon": "&#9632;", "label": "Units",
                 "active": {"prefix": "inv_units"}},
                {"endpoint": "inv_stock.list_stock", "icon": "&#9632;", "label": "Stock Movements",
                 "active": {"prefix": "inv_stock"}},
            ]),
            ("Vouchers", "&#128196;", [
                {"endpoint": "inv_vouchers.consumption_list", "icon": "&#9632;", "label": "Consumption",
                 "active": {"exact": ["inv_vouchers.consumption_list", "inv_vouchers.consumption_form"]}},
                {"endpoint": "inv_vouchers.scrap_list", "icon": "&#9632;", "label": "Scrap",
                 "active": {"exact": ["inv_vouchers.scrap_list", "inv_vouchers.scrap_form"]}},
                {"endpoint": "inv_vouchers.adjustment_list", "icon": "&#9632;", "label": "Stock Adjustment",
                 "active": {"exact": ["inv_vouchers.adjustment_list", "inv_vouchers.adjustment_form"]}},
                {"endpoint": "inv_vouchers.stock_take_list", "icon": "&#9632;", "label": "Stock Taking",
                 "active": {"exact": ["inv_vouchers.stock_take_list", "inv_vouchers.stock_take_form"]}},
                {"endpoint": "inv_transfers.list_transfers", "icon": "&#9632;", "label": "Transfer to FA",
                 "active": {"prefix": "inv_transfers."}},
            ]),
            ("Reports", "&#128202;", [
                {"endpoint": "inv_reports.stock_ledger_report", "icon": "&#9632;", "label": "Stock Ledger"},
                {"endpoint": "inv_reports.valuation_report", "icon": "&#9632;", "label": "Stock Valuation"},
                {"endpoint": "inv_reports.low_stock_report", "icon": "&#9632;", "label": "Low Stock Alert"},
                {"endpoint": "finance.dashboard", "icon": "&#128176;", "label": "Financial Reports",
                 "active": {"prefix": "finance."}},
            ]),
        ],
    },
    "invoicing": {
        "flat": [
            {"endpoint": "invoicing.dashboard", "icon": "&#9679;", "label": "Dashboard"},
        ],
        "groups": [
            ("Procurement", "&#128230;", [
                {"endpoint": "inv_suppliers.list_suppliers", "icon": "&#9632;", "label": "Suppliers",
                 "active": {"prefix": "inv_suppliers"}},
                {"endpoint": "inv_purchases.list_purchases", "icon": "&#9632;", "label": "Purchase Orders",
                 "active": {"prefix": "inv_purchases"}, "gate": _with_po},
                {"endpoint": "inv_purchase_invoice.list_invoices", "icon": "&#9632;",
                 "label": lambda ctx: ("Invoice Against PO" if ctx.get("purchase_flow") == "with_po"
                                       else "Purchase Invoice"),
                 "active": {"prefix": "inv_purchase_invoice"}},
                {"endpoint": "inv_purchase_return.list_returns", "icon": "&#9632;", "label": "Purchase Return",
                 "active": {"prefix": "inv_purchase_return"}},
            ]),
            ("Sales", "&#128176;", [
                {"endpoint": "inv_customers.list_customers", "icon": "&#9632;", "label": "Customers",
                 "active": {"prefix": "inv_customers"}},
                {"endpoint": "inv_sales.list_sales", "icon": "&#9632;", "label": "Sales Orders",
                 "active": {"prefix": "inv_sales"}, "gate": _with_so},
                {"endpoint": "inv_invoices.list_invoices", "icon": "&#9632;",
                 "label": lambda ctx: ("Invoice Against SO" if ctx.get("sales_flow") == "with_so"
                                       else "Sales Invoice"),
                 "active": {"prefix": "inv_invoices"}},
                {"endpoint": "inv_sales_return.list_returns", "icon": "&#9632;",
                 "label": "Sales Return",
                 "active": {"prefix": "inv_sales_return"}},
            ]),
        ],
    },
    "fbr": {
        "flat": [
            {"endpoint": "fbr_dashboard.dashboard", "icon": "&#9679;", "label": "Dashboard"},
            {"endpoint": "fbr_settings.index", "icon": "&#9881;", "label": "Settings",
             "active": {"prefix": "fbr_settings."}, "gate": _admin},
        ],
    },
    "finance": {
        "flat": [
            {"endpoint": "finance.dashboard", "icon": "&#9679;", "label": "Dashboard"},
            {"endpoint": "finance.ledger", "icon": "&#128212;", "label": "General Ledger"},
            {"endpoint": "finance.trial_balance", "icon": "&#9878;", "label": "Trial Balance"},
            {"endpoint": "finance.profit_loss", "icon": "&#128200;", "label": "P&L Statement"},
            {"endpoint": "finance.balance_sheet", "icon": "&#128203;", "label": "Balance Sheet"},
            {"endpoint": "finance.socie", "icon": "&#128218;", "label": "SOCIE"},
            {"endpoint": "finance.cash_flow", "icon": "&#128181;", "label": "Cash Flow"},
        ],
    },
    "accounting": {
        "flat": [
            {"endpoint": "accounting.dashboard", "icon": "&#9679;", "label": "Dashboard"},
            {"endpoint": "accounting.voucher_form", "icon": "&#10133;", "label": "Create Voucher"},
            {"endpoint": "accounting.voucher_list", "icon": "&#128196;", "label": "View Vouchers"},
            {"endpoint": "coa.list_accounts", "icon": "&#128202;", "label": "Chart of Accounts (COA)",
             "active": {"prefix": "coa."}},
        ],
    },
    "settings": {"flat": [], "groups": []},
    "fixed_assets": {
        "flat": [
            {"endpoint": "fa_auth.dashboard", "icon": "&#9679;", "label": "Dashboard"},
        ],
        "groups": [
            ("Assets", "&#128230;", [
                {"endpoint": "fa_assets.list_assets", "icon": "&#9632;", "label": "All Assets",
                 "active": {"prefix": "fa_assets."}},
                {"endpoint": "fa_transfers.list_transfers", "icon": "&#9632;", "label": "Transfer to Stock",
                 "active": {"prefix": "fa_transfers."}},
                {"endpoint": "fa_dep.index", "icon": "&#9632;", "label": "Depreciation",
                 "active": {"prefix": "fa_dep."}},
                {"endpoint": "fa_categories.list_categories", "icon": "&#9632;", "label": "Categories",
                 "active": {"prefix": "fa_categories."}},
            ]),
            ("Reports", "&#128202;", [
                {"endpoint": "fa_reports.index", "icon": "&#9632;", "label": "Asset Reports",
                 "active": {"prefix": "fa_reports."}},
            ]),
        ],
    },
}


def _is_active(item, endpoint):
    spec = item.get("active")
    if not endpoint:
        return False
    if not spec:
        return endpoint == item["endpoint"]
    if endpoint in spec.get("exclude", []):
        return False
    if "exact" in spec:
        return endpoint in spec["exact"]
    if "prefix" in spec:
        return endpoint.startswith(spec["prefix"])
    return endpoint == item["endpoint"]


def _resolve(items, user, ctx, endpoint):
    out = []
    for item in items or []:
        gate = item.get("gate")
        if gate and not gate(user, ctx):
            continue
        label = item["label"]
        out.append({
            "endpoint": item["endpoint"],
            "icon": item.get("icon", "&#9632;"),
            "label": label(ctx) if callable(label) else label,
            "active": _is_active(item, endpoint),
        })
    return out


def build_nav(module_key, user, endpoint, ctx=None):
    """Nav for a module, already gated and with the active item marked.

    Returns a list of blocks: {"kind": "flat"|"group", ...} in render order.
    """
    ctx = ctx or {}
    spec = NAV.get(module_key) or {}
    blocks = []

    def add_flat(items):
        resolved = _resolve(items, user, ctx, endpoint)
        if resolved:
            blocks.append({"kind": "flat", "items": resolved})

    def add_groups(groups):
        for name, icon, items in groups or []:
            resolved = _resolve(items, user, ctx, endpoint)
            if not resolved:
                continue
            blocks.append({
                "kind": "group", "name": name, "icon": icon, "items": resolved,
                "slug": name.lower().replace(" ", "-"),
                "expanded": any(i["active"] for i in resolved),
            })

    add_flat(spec.get("flat"))
    add_groups(spec.get("groups"))
    add_flat(spec.get("flat_after"))
    add_groups(spec.get("groups_after"))
    return blocks


def accessible_modules(user):
    """Modules the user may switch to — drives the sidebar module switcher."""
    from shared.permissions import MODULES
    out = []
    for key, label, _flag, _sections in MODULES:
        if user.module_access(key):
            meta = MODULE_META.get(key, {})
            out.append({"key": key, "label": label,
                        "letter": meta.get("letter", label[:1]),
                        "brand": meta.get("brand", ""),
                        "home": meta.get("home", "dashboard.hub")})
    return out
