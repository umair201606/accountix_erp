"""Registry of modules -> sections used by the admin Settings rights UI.

Each section key is the `resource` stored in UserPermission and checked by
User.can(resource, action). Keep keys stable — they are referenced from route
enforcement across the apps.

Settings itself has no section here: each tab is gated on the matching module
access flag by shared/routes/settings.py, not on a UserPermission resource.
"""

ACTIONS = ["view", "create", "edit", "approve", "delete"]

# module_key, module label, User module-access flag attr, [(section_key, label), ...]
MODULES = [
    ("hr", "HR Management", "has_hr_access", [
        ("employees", "Employees / Users"),
        ("attendance", "Attendance"),
        ("leaves", "Leaves"),
        ("payroll", "Payroll & Compensation"),
        ("loans", "Loans & Advances"),
        ("pf", "Provident Fund"),
    ]),
    ("inventory", "Inventory", "has_inventory_access", [
        ("products", "Products"),
        ("categories", "Categories"),
        ("stock", "Stock Movements"),
        ("consumption_vouchers", "Consumption Vouchers"),
        ("scrap_vouchers", "Scrap Vouchers"),
        ("adjustment_vouchers", "Stock Adjustment Vouchers"),
        ("stock_take_vouchers", "Stock Taking Vouchers"),
        ("inventory_reports", "Inventory Reports"),
    ]),
    ("invoicing", "Invoicing", "has_invoicing_access", [
        ("suppliers", "Suppliers"),
        ("customers", "Customers"),
        ("purchase_orders", "Purchase Orders"),
        ("sales_orders", "Sales Orders"),
        ("purchase_invoices", "Procurement"),
        ("purchase_returns", "Purchase Returns"),
        ("sales_invoices", "Sales"),
        ("sales_returns", "Sales Returns"),
    ]),
    ("finance", "Finance", "has_finance_access", [
        ("financial_reports", "Financial Reports"),
    ]),
    ("accounting", "Accounting", "has_accounting_access", [
        ("cash_payment_vouchers", "Cash Payment Vouchers (CPV)"),
        ("cash_receipt_vouchers", "Cash Receipt Vouchers (CRV)"),
        ("bank_payment_vouchers", "Bank Payment Vouchers (BPV)"),
        ("bank_receipt_vouchers", "Bank Receipt Vouchers (BRV)"),
        ("journal_vouchers", "Journal Vouchers (JV)"),
        ("chart_of_accounts", "Chart of Accounts"),
    ]),
    ("fbr", "FBR Digital Invoicing", "has_fbr_access", [
        ("fbr_dashboard", "Dashboard"),
        ("fbr_settings", "Settings"),
    ]),
]

# Accounting voucher type -> section key (used to enforce per-voucher rights)
VOUCHER_SECTION = {
    "CPV": "cash_payment_vouchers",
    "CRV": "cash_receipt_vouchers",
    "BPV": "bank_payment_vouchers",
    "BRV": "bank_receipt_vouchers",
    "JV": "journal_vouchers",
}


def all_section_keys():
    return [key for _, _, _, sections in MODULES for key, _ in sections]


def deny_json(resource, action):
    """For JSON endpoints: None if current user may act, else a 403 response."""
    from flask import jsonify
    from flask_login import current_user
    if current_user.can(resource, action):
        return None
    return jsonify({"ok": False,
                    "error": f"You don't have '{action}' rights for this section. "
                             f"Ask an administrator to grant access in Settings."}), 403


def deny_page(resource, action):
    """For page endpoints: None if allowed, else flashes and returns True."""
    from flask import flash
    from flask_login import current_user
    if current_user.can(resource, action):
        return None
    flash(f"You don't have '{action}' rights for this section. "
          f"Ask an administrator to grant access in Settings.", "error")
    return True
