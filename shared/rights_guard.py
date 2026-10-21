"""Settings → Rights, enforced at the door of every module request.

The rights grid (view / create / edit / approve / delete per section) used to
be checked by hand in a handful of routes: executive and invoice tracking
checked "view", invoices and vouchers checked some actions, and everything
else — categories, the chart of accounts, every financial and inventory
report, stock, FBR, fixed assets, bank reconciliation, the Excel imports —
checked nothing. An admin could untick a box and nothing changed.

Here every endpoint of the covered modules is named once, with the section
and action it needs. ``install_rights_guard`` runs it as a before_request
after the module guard (entitlement first, then the person's rights).
``tests/e2e/test_rights_enforced.py`` fails when a route of a covered module
is in neither ``RIGHTS`` nor ``OPEN``, so a page added later cannot slip
through unguarded.

Semantics are ``User.can``'s: an admin passes everything, and a section with
no rights row is unrestricted (people whose rights were never configured keep
full access). The checks that routes already make stay where they are; the
map agrees with them and is stricter only where they left a gap (saving a NEW
document straight to approved also needs "create").

HR is not covered here: its pages are governed by role (admin / manager /
self-service) in the HR routes themselves.
"""
from collections import namedtuple
from urllib.parse import urlparse

from flask import flash, g, jsonify, redirect, request, url_for
from flask_login import current_user

from shared.permissions import VOUCHER_SECTION

V, C, E, A, D = "view", "create", "edit", "approve", "delete"

# Modules whose every endpoint must appear in RIGHTS or OPEN.
COVERED_MODULES = ("inventory", "invoicing", "finance", "accounting", "fbr",
                   "fixed_assets", "executive")


class AnyOf(tuple):
    """A requirement met by any one of its (section, action) pairs."""


# What a resolver sees of the request. The sidebar asks with an empty call
# (GET, no arguments) to decide whether to show a link.
Call = namedtuple("Call", "method view_args args form json")
EMPTY_CALL = Call("GET", {}, {}, {}, {})


def _use(section):
    """Lookups a document's form makes while it is open (product lists,
    party lists, orders to import): whoever may open the form may use them."""
    return AnyOf(((section, V), (section, C), (section, E)))


VOUCHER_SECTIONS = tuple(dict.fromkeys(VOUCHER_SECTION.values()))


def _any_voucher(*actions):
    return AnyOf(tuple((s, a) for s in VOUCHER_SECTIONS for a in actions))


# ── Resolvers: requirements that depend on the request ──────────────────────

def _doc_page(section, id_arg="id"):
    """The form page of a document that serves "new" (no id) and an existing
    one: opening a blank form needs "create", opening a saved one "view"."""
    def resolve(c):
        return [(section, V if c.view_args.get(id_arg) else C)]
    return resolve


def _json_save(section):
    """A document's JSON save (sales / purchase invoices, orders, returns).
    The route checks approve on approval and edit / create otherwise; a NEW
    document saved straight to approved also needs "create" here."""
    def resolve(c):
        approve = c.json.get("action") == "approve"
        if c.json.get("id"):
            return [(section, A if approve else E)]
        return [(section, C)] + ([(section, A)] if approve else [])
    return resolve


def _status_form(section, id_arg="id", field="status", approved="approved",
                 get_action=None):
    """A form endpoint whose POST saves and, with ``field == approved``,
    approves too (inventory vouchers, transfers, capitalisations). GET opens
    it: "create" for a blank one, ``get_action`` (default "view") for a saved
    one."""
    def resolve(c):
        has_id = c.view_args.get(id_arg) is not None
        if c.method != "POST":
            return [(section, (get_action or V) if has_id else C)]
        reqs = [(section, E if has_id else C)]
        if c.form.get(field) == approved:
            reqs.append((section, A))
        return reqs
    return resolve


def _by_method(get, post):
    """GET needs ``get``, POST needs ``post`` — (section, action) each."""
    def resolve(c):
        return [post if c.method == "POST" else get]
    return resolve


def _voucher_type(voucher_id):
    from shared.models.accounting_voucher import AccountingVoucher
    from shared.tenancy import scoped_get
    v = scoped_get(AccountingVoucher, voucher_id) if voucher_id else None
    return v.voucher_type if v is not None else None


def _voucher_section(vtype):
    return VOUCHER_SECTION.get(vtype or "", "journal_vouchers")


def _accounting_voucher_form(c):
    """CPV / CRV / BPV / BRV / JV share one form; the section follows the
    voucher type (the saved voucher's, or the one being created)."""
    vid = c.view_args.get("id")
    if vid:
        vtype = _voucher_type(vid)
        if vtype is None:
            return []                      # the route answers "not found"
        section = _voucher_section(vtype)
        if c.method != "POST":
            return [(section, V)]
        reqs = [(section, E)]
    elif c.method == "POST":
        # The route's own defaults: no type is a CPV, an unknown one is
        # checked as a journal voucher.
        section = _voucher_section(c.form.get("voucher_type", "CPV"))
        reqs = [(section, C)]
    else:
        vtype = c.args.get("type")
        if vtype not in VOUCHER_SECTION:
            return [_any_voucher(C)]       # the type picker
        reqs = [(_voucher_section(vtype), C)]
        section = None
    if c.method == "POST" and c.form.get("action") == "approve":
        reqs.append((section, A))
    return reqs


def _accounting_voucher(action):
    def resolve(c):
        vtype = _voucher_type(c.view_args.get("id"))
        return [(_voucher_section(vtype), action)] if vtype else []
    return resolve


def _voucher_listing(c):
    """Lists and registers of accounting vouchers: one type needs that
    type's "view"; all types need any, and the route lists only the types
    the person may view (``viewable_voucher_types``)."""
    vtype = (c.args.get("vtype") or "").strip().upper()
    if vtype in VOUCHER_SECTION:
        return [(_voucher_section(vtype), V)]
    return [_any_voucher(V)]


# Journal type (the "Accounting entry" panel) -> the section of the document
# that posted it. Anyone who can read the general ledger can read any journal
# there, so financial-report viewers pass for every type.
_JOURNAL_SECTION = {
    "SI": "sales_invoices", "PI": "purchase_invoices", "PMT": "purchase_invoices",
    "SR": "sales_returns", "PR": "purchase_returns",
    "CONS": "consumption_vouchers", "SCRAP": "scrap_vouchers",
    "ADJ": "adjustment_vouchers", "OPENING": "products",
    "FA-ACQ": "fixed_assets_assets", "FA-DEP": "fixed_assets_assets",
    "FA-DISP": "fixed_assets_assets", "FA-CAP": "fixed_assets_assets",
    "FA-TRF": "fixed_assets_assets",
    "PRL": "payroll", "PRL-PAY": "payroll", "LOAN-DSB": "loans",
    **VOUCHER_SECTION,
}


def _journal_requirement(vtype):
    vtype = (vtype or "").strip().upper()
    if vtype.startswith("CADJ-"):
        vtype = vtype[5:]
    section = _JOURNAL_SECTION.get(vtype)
    if section is None and vtype.startswith("PF-"):
        section = "pf"
    if section is None:
        return AnyOf((("financial_reports", V),))
    return AnyOf(((section, V), ("financial_reports", V)))


def _gl_entry(c):
    reqs = [_journal_requirement(c.args.get("type"))]
    for part in (c.args.get("also") or "").split(",")[:200]:
        t, _, i = part.strip().rpartition(":")
        if t and i.isdigit():
            reqs.append(_journal_requirement(t))
    return reqs


def _register_doc(c):
    """Invoice Register: ``doc=purchase`` is the purchase side. The page
    with no document chosen yet (the sidebar's link) opens for either."""
    doc = (c.args.get("doc") or "").strip().lower()
    if not doc and c.args.get("view") != "1" and c.method == "GET" \
            and not c.view_args:
        return [AnyOf((("sales_invoices", V), ("purchase_invoices", V)))]
    return [("purchase_invoices" if doc == "purchase" else "sales_invoices", V)]


_INV_PREVIEW = {"CONS": "consumption_vouchers", "SCRAP": "scrap_vouchers",
                "ADJ": "adjustment_vouchers", "PI": "purchase_invoices",
                "PR": "purchase_returns"}


def _inventory_preview(c):
    section = _INV_PREVIEW.get((c.view_args.get("vtype") or "").upper())
    return [(section, V)] if section else []


def _bank_rec_save(c):
    reqs = [("bank_reconciliation", E)]
    if c.form.get("action") == "complete":
        reqs.append(("bank_reconciliation", A))
    return reqs


def _twcf(c):
    if c.method != "POST":
        return [("financial_reports", V)]
    action = c.form.get("action", "")
    return [("financial_reports", {"add": C, "delete": D}.get(action, E))]


def _landing(*sections):
    """A module's home page: open to anyone who may view something in it."""
    return AnyOf(tuple((s, V) for s in sections))


_INVENTORY_SECTIONS = ("products", "categories", "stock", "consumption_vouchers",
                       "scrap_vouchers", "adjustment_vouchers",
                       "stock_take_vouchers", "inventory_reports")
_STOCK_VOUCHERS = ("consumption_vouchers", "scrap_vouchers",
                   "adjustment_vouchers", "stock_take_vouchers")


def _crud(prefix, section, create=None, edit=None, delete=None, listing=None):
    """The usual master-data endpoints of one blueprint."""
    out = {}
    if listing:
        out[f"{prefix}.{listing}"] = (section, V)
    if create:
        out[f"{prefix}.{create}"] = (section, C)
    if edit:
        out[f"{prefix}.{edit}"] = (section, E)
    if delete:
        out[f"{prefix}.{delete}"] = (section, D)
    return out


def _imports(prefix, section):
    """Excel import of a master list: the template, the upload and the
    review/batch save all create records."""
    return {f"{prefix}.download_template": (section, C),
            f"{prefix}.upload_excel": (section, C),
            f"{prefix}.batch_editor": (section, C)}


# ── The map ─────────────────────────────────────────────────────────────────
# endpoint -> (section, action) | [requirements] | resolver(Call) -> [requirements]

RIGHTS = {
    # Inventory ───────────────────────────────────────────────────────────
    "inv_auth.dashboard": _landing(*_INVENTORY_SECTIONS),
    **_crud("inv_categories", "categories", listing="list_categories",
            create="create_category", edit="edit_category", delete="delete_category"),
    **_crud("inv_products", "products", listing="list_products",
            create="create_product", edit="edit_product", delete="delete_product"),
    **_imports("inv_products", "products"),
    # The route already asks for approve on stock adjustments (it posts one).
    "inv_products.adjust_stock": ("adjustment_vouchers", A),
    # Units are part of the product master: no section of their own.
    **_crud("inv_units", "products", listing="list_units",
            create="create_unit", edit="edit_unit", delete="delete_unit"),
    "inv_units.quick_create": ("products", C),
    **_imports("inv_units", "products"),
    "inv_stock.list_stock": ("stock", V),
    "inv_stock.list_movements": ("stock", V),
    "inv_reports.low_stock_report": ("inventory_reports", V),
    "inv_reports.stock_ledger_report": ("inventory_reports", V),
    "inv_reports.stock_ledger_json": ("inventory_reports", V),
    "inv_reports.valuation_report": ("inventory_reports", V),
    "inv_vouchers.product_ledger": ("inventory_reports", V),
    "inv_vouchers.product_ledger_list": ("inventory_reports", V),
    # Stock to fixed assets: a stock movement out of inventory.
    "inv_transfers.list_transfers": ("stock", V),
    "inv_transfers.create_transfer": _status_form("stock"),
    "inv_transfers.edit_transfer": _status_form("stock", get_action=E),
    "inv_transfers.unapprove_transfer": ("stock", A),
    "inv_transfers.delete_transfer": ("stock", D),
    "inv_vouchers.voucher_preview": _inventory_preview,
    "inv_vouchers.api_products": AnyOf(tuple((s, a) for s in _STOCK_VOUCHERS for a in (V, C, E))),
    "inv_vouchers.api_product_stock": AnyOf(tuple((s, a) for s in _STOCK_VOUCHERS for a in (V, C, E))),
    **{f"inv_vouchers.{kind}_list": (section, V) for kind, section in (
        ("consumption", "consumption_vouchers"), ("scrap", "scrap_vouchers"),
        ("adjustment", "adjustment_vouchers"), ("stock_take", "stock_take_vouchers"))},
    **{f"inv_vouchers.{kind}_form": _status_form(section) for kind, section in (
        ("consumption", "consumption_vouchers"), ("scrap", "scrap_vouchers"),
        ("adjustment", "adjustment_vouchers"), ("stock_take", "stock_take_vouchers"))},
    **{f"inv_vouchers.{kind}_delete": (section, D) for kind, section in (
        ("consumption", "consumption_vouchers"), ("scrap", "scrap_vouchers"),
        ("adjustment", "adjustment_vouchers"), ("stock_take", "stock_take_vouchers"))},
    **{f"inv_vouchers.{kind}_unapprove": (section, A) for kind, section in (
        ("consumption", "consumption_vouchers"), ("scrap", "scrap_vouchers"),
        ("adjustment", "adjustment_vouchers"))},

    # Invoicing ───────────────────────────────────────────────────────────
    # The dashboard is the sales and purchase performance band.
    "invoicing.dashboard": _landing("sales_invoices", "purchase_invoices"),
    **_crud("inv_customers", "customers", listing="list_customers",
            create="create_customer", edit="edit_customer", delete="delete_customer"),
    **_imports("inv_customers", "customers"),
    **_crud("inv_suppliers", "suppliers", listing="list_suppliers",
            create="create_supplier", edit="edit_supplier", delete="delete_supplier"),
    **_imports("inv_suppliers", "suppliers"),
    # Sales invoices
    "inv_invoices.invoice_form": _doc_page("sales_invoices"),
    "inv_invoices.list_invoices": ("sales_invoices", V),
    "inv_invoices.save_invoice": _json_save("sales_invoices"),
    "inv_invoices.unapprove_invoice": ("sales_invoices", A),
    "inv_invoices.delete_invoice": ("sales_invoices", D),
    "inv_invoices.pay_invoice": ("sales_invoices", A),        # retired; refuses
    "inv_invoices.api_products": _use("sales_invoices"),
    "inv_invoices.api_customers": _use("sales_invoices"),
    "inv_invoices.api_orders_for_customer": _use("sales_invoices"),
    "inv_invoices.api_charge_accounts": _use("sales_invoices"),
    # Purchase invoices
    "inv_purchase_invoice.invoice_form": _doc_page("purchase_invoices"),
    "inv_purchase_invoice.list_invoices": ("purchase_invoices", V),
    "inv_purchase_invoice.save_invoice": _json_save("purchase_invoices"),
    "inv_purchase_invoice.unapprove_invoice": ("purchase_invoices", A),
    "inv_purchase_invoice.delete_invoice": ("purchase_invoices", D),
    "inv_purchase_invoice.pay_invoice": ("purchase_invoices", A),  # retired
    "inv_purchase_invoice.api_products": _use("purchase_invoices"),
    "inv_purchase_invoice.api_suppliers": _use("purchase_invoices"),
    "inv_purchase_invoice.api_orders_for_supplier": _use("purchase_invoices"),
    "inv_purchase_invoice.api_pi_charge_accounts": _use("purchase_invoices"),
    # "+ New product" from inside the purchase invoice creates a product.
    "inv_purchase_invoice.api_new_product": ("products", C),
    # Sales orders
    "inv_sales.sale_form": _doc_page("sales_orders"),
    "inv_sales.list_sales": ("sales_orders", V),
    "inv_sales.save_sale": _json_save("sales_orders"),
    "inv_sales.unapprove_sale": ("sales_orders", A),
    "inv_sales.delete_sale": ("sales_orders", D),
    "inv_sales.cancel_sale": ("sales_orders", E),            # as the route checks
    "inv_sales.deliver_sale": ("sales_orders", V),           # retired: sends to billing
    "inv_sales.api_products": _use("sales_orders"),
    "inv_sales.api_customers": _use("sales_orders"),
    "inv_sales.api_orders_for_customer": _use("sales_orders"),
    "inv_sales.api_charge_accounts": _use("sales_orders"),
    # Purchase orders
    "inv_purchases.purchase_form": _doc_page("purchase_orders"),
    "inv_purchases.list_purchases": ("purchase_orders", V),
    "inv_purchases.save_purchase": _json_save("purchase_orders"),
    "inv_purchases.unapprove_purchase": ("purchase_orders", A),
    "inv_purchases.delete_purchase": ("purchase_orders", D),
    "inv_purchases.cancel_purchase": ("purchase_orders", E),
    "inv_purchases.receive_purchase": ("purchase_orders", V),  # retired
    "inv_purchases.api_products": _use("purchase_orders"),
    "inv_purchases.api_suppliers": _use("purchase_orders"),
    "inv_purchases.api_orders_for_supplier": _use("purchase_orders"),
    "inv_purchases.api_charge_accounts": _use("purchase_orders"),
    # Returns
    "inv_sales_return.return_form": _doc_page("sales_returns"),
    "inv_sales_return.list_returns": ("sales_returns", V),
    "inv_sales_return.save_return": _json_save("sales_returns"),
    "inv_sales_return.unapprove_return": ("sales_returns", A),
    "inv_sales_return.api_invoices": _use("sales_returns"),
    "inv_sales_return.api_invoice_detail": _use("sales_returns"),
    "inv_purchase_return.return_form": _doc_page("purchase_returns"),
    "inv_purchase_return.list_returns": ("purchase_returns", V),
    "inv_purchase_return.save_return": _json_save("purchase_returns"),
    "inv_purchase_return.unapprove_return": ("purchase_returns", A),
    "inv_purchase_return.api_invoices": _use("purchase_returns"),
    "inv_purchase_return.api_invoice_detail": _use("purchase_returns"),
    # Invoice Register
    "inv_registers.index": _register_doc,
    "inv_registers.data": _register_doc,
    "inv_registers.documents": _register_doc,
    "inv_registers.documents_export": _register_doc,
    "inv_registers.export": _register_doc,
    # Invoice tracking: "view" reads, "edit" assigns (the routes say the same).
    **{f"inv_tracking.{ep}": ("payment_tracking", V) for ep in (
        "feed", "sales", "purchases", "invoice_history", "assign_workspace",
        "api_feed_item", "api_open_invoices", "api_auto_split")},
    **{f"inv_tracking.{ep}": ("payment_tracking", E) for ep in (
        "api_assign", "api_auto_match", "api_apply_here", "api_force_close",
        "api_release_force", "api_clear_stale", "api_unassign")},

    # Finance ─────────────────────────────────────────────────────────────
    **{f"finance.{ep}": ("financial_reports", V) for ep in (
        "dashboard", "trial_balance", "balance_sheet", "profit_loss", "label_pl",
        "socie", "cash_flow", "ledger")},
    "finance.twcf": _twcf,

    # Accounting ──────────────────────────────────────────────────────────
    # The landing lists only the voucher types the person may view.
    "accounting.dashboard": AnyOf(tuple((s, V) for s in VOUCHER_SECTIONS)
                                  + (("chart_of_accounts", V), ("bank_reconciliation", V))),
    "accounting.voucher_form": _accounting_voucher_form,
    "accounting.voucher_list": _voucher_listing,
    "accounting.voucher_preview": _accounting_voucher(V),
    "accounting.voucher_export": _accounting_voucher(V),
    "accounting.approve_voucher": _accounting_voucher(A),
    "accounting.unapprove_voucher": _accounting_voucher(A),
    "accounting.delete_voucher": _accounting_voucher(D),
    "accounting.voucher_register": _voucher_listing,
    "accounting.voucher_register_data": _voucher_listing,
    "accounting.voucher_register_documents": _voucher_listing,
    "accounting.voucher_register_export": _voucher_listing,
    "accounting.gl_entry": _gl_entry,
    # The voucher form's "balance now -> after" line.
    "accounting.api_account_balance": AnyOf(_any_voucher(V, C, E)
                                            + (("bank_reconciliation", V),)),
    "accounting.bank_rec_list": ("bank_reconciliation", V),
    "accounting.bank_rec_view": ("bank_reconciliation", V),
    "accounting.bank_rec_new": ("bank_reconciliation", C),
    "accounting.bank_rec_save": _bank_rec_save,
    "accounting.bank_rec_reopen": ("bank_reconciliation", A),
    "accounting.bank_rec_delete": ("bank_reconciliation", D),
    "coa.list_accounts": ("chart_of_accounts", V),
    "coa.add_account": ("chart_of_accounts", C),
    "coa.edit_account": ("chart_of_accounts", E),
    "coa.delete_account": ("chart_of_accounts", D),
    # The account form's parent / next-code lookups.
    "coa.api_children": _use("chart_of_accounts"),
    "coa.api_level_accounts": _use("chart_of_accounts"),
    "coa.api_next_code": _use("chart_of_accounts"),

    # FBR ─────────────────────────────────────────────────────────────────
    "fbr_dashboard.dashboard": ("fbr_dashboard", V),
    "fbr_dashboard.view_record": ("fbr_dashboard", V),
    "fbr_dashboard.check_status": ("fbr_dashboard", V),
    "fbr_dashboard.validate_invoice": ("fbr_dashboard", E),
    # Reporting an invoice to FBR cannot be taken back: approve.
    "fbr_dashboard.submit_invoice": ("fbr_dashboard", A),
    "fbr_dashboard.retry_submission": ("fbr_dashboard", A),
    "fbr_settings.index": _by_method(("fbr_settings", V), ("fbr_settings", E)),
    "fbr_settings.test_connection": ("fbr_settings", E),

    # Fixed assets ────────────────────────────────────────────────────────
    "fa_auth.dashboard": ("fixed_assets_dashboard", V),
    "fa_assets.list_assets": ("fixed_assets_assets", V),
    "fa_assets.view_asset": ("fixed_assets_assets", V),
    "fa_assets.create_asset": ("fixed_assets_assets", C),
    "fa_assets.edit_asset": ("fixed_assets_assets", E),
    # Charges, disposals and their reversal post journals: approve.
    "fa_assets.record_depreciation": ("fixed_assets_assets", A),
    "fa_assets.dispose_asset": ("fixed_assets_assets", A),
    "fa_assets.undo_disposal": ("fixed_assets_assets", A),
    "fa_dep.index": ("fixed_assets_assets", V),
    "fa_dep.post_depreciation": ("fixed_assets_assets", A),
    "fa_dep.reverse_entry": ("fixed_assets_assets", A),
    "fa_categories.list_categories": ("fixed_assets_categories", V),
    "fa_categories.create_category": ("fixed_assets_categories", C),
    "fa_categories.edit_category": ("fixed_assets_categories", E),
    "fa_reports.index": ("fixed_assets_reports", V),
    "fa_reports.movement_schedule": ("fixed_assets_reports", V),
    "fa_transfers.list_transfers": ("fixed_assets_assets", V),
    "fa_transfers.capitalise_from_stock": _status_form("fixed_assets_assets"),
    "fa_transfers.create_transfer": _status_form("fixed_assets_assets"),
    "fa_transfers.edit_transfer": _status_form("fixed_assets_assets", get_action=E),
    "fa_transfers.approve_capitalisation_route": ("fixed_assets_assets", A),
    "fa_transfers.unapprove_capitalisation": ("fixed_assets_assets", A),
    "fa_transfers.unapprove_transfer": ("fixed_assets_assets", A),
    "fa_transfers.delete_transfer": ("fixed_assets_assets", D),

    # Executive ───────────────────────────────────────────────────────────
    **{f"executive.{ep}": ("executive_reports", V) for ep in (
        "dashboard", "integrity", "party", "payables", "receivables")},
    "executive.settings": _by_method(("executive_reports", V), ("executive_reports", E)),
}

# Endpoints of covered modules that need no section right, and why.
OPEN = {
    # The inventory app's own sign-in door and sign-out (legacy URLs).
    "inv_auth.login",
    "inv_auth.logout",
    # "Is this name / SKU taken?" while typing — answers yes or no only.
    "inv_products.check_duplicate",
    "inv_units.check_duplicate",
    "inv_customers.check_duplicate",
    "inv_suppliers.check_duplicate",
    # Account pickers (code + name) used by invoice, order and voucher forms.
    "accounting.api_accounts",
    "accounting.api_cash_bank_accounts",
}

# Endpoints that answer JSON even when the browser asks for anything.
JSON_ENDPOINTS = {
    "inv_invoices.save_invoice", "inv_invoices.unapprove_invoice",
    "inv_invoices.delete_invoice",
    "inv_purchase_invoice.save_invoice", "inv_purchase_invoice.unapprove_invoice",
    "inv_purchase_invoice.delete_invoice",
    "inv_sales.save_sale", "inv_sales.unapprove_sale", "inv_sales.delete_sale",
    "inv_purchases.save_purchase", "inv_purchases.unapprove_purchase",
    "inv_purchases.delete_purchase",
    "inv_sales_return.save_return", "inv_sales_return.unapprove_return",
    "inv_purchase_return.save_return", "inv_purchase_return.unapprove_return",
    "inv_registers.data", "accounting.voucher_register_data", "accounting.gl_entry",
    "inv_reports.stock_ledger_json", "inv_units.quick_create",
}


# ── Checking ────────────────────────────────────────────────────────────────

def _cache():
    """Per-request: whether the person is admin here, and their rights rows."""
    cache = getattr(g, "_rights_cache", None)
    if cache is None or cache.get("user") != current_user.id:
        from shared.models.base import UserPermission
        admin = current_user.is_admin()
        rows = {} if admin else {
            p.resource: p for p in UserPermission.query.filter_by(user_id=current_user.id)}
        cache = {"user": current_user.id, "admin": admin, "rows": rows}
        g._rights_cache = cache
    return cache


def can(section, action):
    """``User.can`` from one cached read of the person's rights."""
    cache = _cache()
    if cache["admin"]:
        return True
    row = cache["rows"].get(section)
    if row is None:
        return True
    return bool(getattr(row, f"can_{action}", False))


def _met(req):
    if isinstance(req, AnyOf):
        return any(can(s, a) for s, a in req)
    return can(*req)


def requirements(endpoint, call=EMPTY_CALL):
    """What an endpoint needs, as a list; [] when it needs nothing (or is
    not in the map)."""
    spec = RIGHTS.get(endpoint)
    if spec is None:
        return []
    if callable(spec):
        return list(spec(call) or [])
    if isinstance(spec, AnyOf):
        return [spec]
    if isinstance(spec, list):
        return spec
    return [spec]


def first_unmet(endpoint, call=EMPTY_CALL):
    for req in requirements(endpoint, call):
        if not _met(req):
            return req
    return None


def allowed(endpoint, call=EMPTY_CALL):
    """Whether the signed-in person may open ``endpoint`` — the sidebar asks
    with the empty call (a plain GET of the link)."""
    if not getattr(current_user, "is_authenticated", False):
        return True
    from shared.tenancy import current_company_id
    if current_company_id() is None or endpoint not in RIGHTS:
        return True
    try:
        return first_unmet(endpoint, call) is None
    except Exception:               # a lookup that needs the request; let it through
        return True


def viewable_voucher_types():
    """Accounting voucher types the person may view (lists, registers and the
    accounting dashboard show only these)."""
    return [t for t, s in VOUCHER_SECTION.items() if can(s, V)]


def _current_call():
    json_body = {}
    if request.method == "POST" and request.endpoint in RIGHTS:
        data = request.get_json(force=True, silent=True)
        json_body = data if isinstance(data, dict) else {}
    return Call(request.method, request.view_args or {}, request.args,
                request.form if request.method == "POST" else {}, json_body)


def _wants_json():
    ep = request.endpoint or ""
    name = ep.rsplit(".", 1)[-1]
    if ep in JSON_ENDPOINTS or name.startswith("api_"):
        return True
    if request.is_json or request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return True
    best = request.accept_mimetypes.best
    return best == "application/json"


def _safe_referrer():
    ref = request.referrer
    if not ref:
        return None
    parsed = urlparse(ref)
    if parsed.netloc and parsed.netloc != request.host:
        return None
    if parsed.path == request.path and request.method != "POST":
        return None                 # the page refusing itself: no loop
    return ref


def _landing_url(module_key):
    """Somewhere in the module the person may open; the hub if nowhere."""
    from shared.navigation import MODULE_META, NAV
    here = request.endpoint
    candidates = []
    home = MODULE_META.get(module_key, {}).get("home")
    if home:
        candidates.append(home)
    spec = NAV.get(module_key) or {}
    for key in ("flat", "flat_after"):
        candidates += [i["endpoint"] for i in spec.get(key) or []]
    for key in ("groups", "groups_after"):
        for _name, _icon, items in spec.get(key) or []:
            candidates += [i["endpoint"] for i in items]
    for ep in candidates:
        if ep != here and allowed(ep):
            try:
                return url_for(ep)
            except Exception:
                continue
    return url_for("dashboard.hub")


def refusal(section, action):
    """Answer for a refused request: JSON 403 for scripts, else a flash and
    a redirect to somewhere the person may be."""
    from shared.module_guard import module_for_request
    if _wants_json():
        return jsonify({"ok": False,
                        "error": f"You don't have '{action}' rights for this section. "
                                 f"Ask an administrator to grant access in Settings."}), 403
    flash(f"No '{action}' rights here. Ask an admin in Settings.", "error")
    return redirect(_safe_referrer() or _landing_url(module_for_request()))


def install_rights_guard(app):
    """Refuse a request whose section right is not granted. Must come after
    the company context and the module guard."""
    @app.before_request
    def _enforce_section_rights():
        if not current_user.is_authenticated:
            return None
        endpoint = request.endpoint
        if endpoint not in RIGHTS:
            return None
        from shared.tenancy import current_company_id
        if current_company_id() is None:
            return None             # the route itself answers "open a company"
        unmet = first_unmet(endpoint, _current_call())
        if unmet is None:
            return None
        section, action = unmet[0] if isinstance(unmet, AnyOf) else unmet
        return refusal(section, action)
