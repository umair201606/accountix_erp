"""Settings → Rights is enforced: an unticked box refuses.

Only executive and invoice tracking used to check "view"; categories, the
chart of accounts, financial and inventory reports, stock, FBR, fixed assets,
bank reconciliation and the Excel imports checked nothing, and documents
checked some actions only. ``shared/rights_guard.py`` now names every
endpoint of those modules with the section and action it needs.

Writes are judged by what is stored, not by the status code: a refused write
and an accepted one both redirect.
"""
import io
import os
import tempfile

import pytest

_TMP_DB = os.path.join(tempfile.gettempdir(), "erp_rights_enforced.db")
if os.path.exists(_TMP_DB):
    os.remove(_TMP_DB)
os.environ["DATABASE_URL"] = (os.environ.get("TEST_DATABASE_URL")
                              or "sqlite:///" + _TMP_DB.replace("\\", "/"))

from app import app as flask_app  # noqa: E402
from shared.extensions import db  # noqa: E402

ACTIONS = ("view", "create", "edit", "approve", "delete")


@pytest.fixture(scope="module")
def world():
    flask_app.test_client().get("/")
    from shared.models.base import Role, User
    from shared.models.company import Company, CompanyMembership
    from shared.tenancy import set_current_company, unscoped
    from shared.company_setup import provision_company
    from inventory_app.models.product import InvProduct
    from inventory_app.models.customer import InvCustomer
    from inventory_app.models.supplier import InvSupplier
    from inventory_app.models.category import InvCategory
    from shared.models.accounting_voucher import AccountingVoucher

    with flask_app.app_context():
        admin = (User.query.filter_by(is_super_admin=True).order_by(User.id).first()
                 or User.query.order_by(User.id).first())
        emp_role = Role.query.filter_by(name=Role.EMPLOYEE).first()
        admin_role = Role.query.filter_by(name=Role.ADMIN).first()
        with unscoped():
            comp = Company(name="Rights Co", slug="rights-co-e2e", is_active=True,
                           created_by=admin.id)
            db.session.add(comp)
            db.session.flush()
            clerk = User(employee_code="RGT001", email="clerk-rights@example.com",
                         login_id="clerk-rights@example.com", full_name="Rights Clerk",
                         role_id=emp_role.id, is_active=True,
                         **{attr: True for attr in (
                             "has_inventory_access", "has_invoicing_access",
                             "has_finance_access", "has_accounting_access",
                             "has_fbr_access", "has_fixed_assets_access",
                             "has_executive_access", "has_hr_access")})
            clerk.set_password("clerk-pass")
            db.session.add(clerk)
            db.session.flush()
            db.session.add_all([
                CompanyMembership(company_id=comp.id, user_id=admin.id,
                                  role_id=admin_role.id, status=CompanyMembership.ACTIVE),
                CompanyMembership(company_id=comp.id, user_id=clerk.id,
                                  role_id=emp_role.id, status=CompanyMembership.ACTIVE),
            ])
            db.session.commit()
            ids = {"company": comp.id, "admin": admin.id, "clerk": clerk.id}
        set_current_company(ids["company"])
        provision_company(ids["company"])
        set_current_company(ids["company"])
        cat = InvCategory(name="Panels")
        prod = InvProduct(name="Panel 550W", sku="PNL550", current_stock=0, cost_price=0,
                          unit_price=0, is_active=True)
        cust = InvCustomer(name="Rights Customer")
        sup = InvSupplier(name="Rights Supplier")
        db.session.add_all([cat, prod, cust, sup])
        db.session.flush()
        jv = AccountingVoucher(voucher_type="JV", voucher_number="JV-RGT-1",
                               created_by=ids["admin"])
        cpv = AccountingVoucher(voucher_type="CPV", voucher_number="CPV-RGT-1",
                                created_by=ids["admin"])
        db.session.add_all([jv, cpv])
        db.session.commit()
        ids.update(category=cat.id, product=prod.id, customer=cust.id,
                   supplier=sup.id, jv=jv.id, cpv=cpv.id)
    return ids


class Ctx:
    def __init__(self, w):
        self.w = w

    def __enter__(self):
        from shared.tenancy import set_current_company
        self.cm = flask_app.app_context()
        self.cm.__enter__()
        set_current_company(self.w["company"])
        return self

    def __exit__(self, *a):
        db.session.remove()
        self.cm.__exit__(*a)


def client_for(w, who):
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(w[who])
        s["_fresh"] = True
        s["company_id"] = w["company"]
    return c


def set_rights(w, who="clerk", **sections):
    """Rights rows for ``who``: every section in the registry fully granted,
    except those given — ``products="view"`` grants only view, ``products=""``
    nothing. With no keywords, every row is removed (never configured)."""
    from shared.models.base import UserPermission
    from shared.permissions import all_section_keys
    with Ctx(w):
        UserPermission.query.filter_by(user_id=w[who]).delete()
        if sections:
            for key in all_section_keys():
                granted = sections.get(key, ",".join(ACTIONS)).split(",")
                db.session.add(UserPermission(
                    user_id=w[who], resource=key,
                    **{f"can_{a}": a in granted for a in ACTIONS}))
        db.session.commit()


def all_but(action):
    return ",".join(a for a in ACTIONS if a != action)


def flashes(c):
    with c.session_transaction() as s:
        return " ".join(m for _cat, m in s.get("_flashes", []))


def assert_refused(c, resp, action=None):
    """A page refusal redirects with the rights message; JSON answers 403."""
    if resp.status_code == 403 and resp.is_json:
        assert "rights" in resp.get_json()["error"]
        return
    assert resp.status_code == 302, (resp.status_code, resp.get_data(as_text=True)[:300])
    msg = flashes(c)
    assert "rights here" in msg, msg
    if action:
        assert f"'{action}'" in msg


def count(w, model_path, **filters):
    module, name = model_path.rsplit(".", 1)
    model = getattr(__import__(module, fromlist=[name]), name)
    with Ctx(w):
        return model.query.filter_by(**filters).count()


@pytest.fixture(autouse=True)
def _reset(world):
    set_rights(world)
    yield
    set_rights(world)


# ── The map covers every route ──────────────────────────────────────────────

def test_every_route_of_the_covered_modules_is_in_the_map():
    from shared import module_guard as mg
    from shared.rights_guard import COVERED_MODULES, OPEN, RIGHTS
    missing, endpoints = [], set()
    for rule in flask_app.url_map.iter_rules():
        ep = rule.endpoint
        endpoints.add(ep)
        bp = ep.split(".")[0] if "." in ep else None
        module = mg._BP_MODULE.get(bp) if bp else mg._EP_MODULE.get(ep)
        if module in COVERED_MODULES and ep not in RIGHTS and ep not in OPEN:
            missing.append(ep)
    assert not missing, f"routes with no rights entry: {sorted(missing)}"
    stale = sorted((set(RIGHTS) | OPEN) - endpoints)
    assert not stale, f"rights entries for routes that no longer exist: {stale}"


def test_every_section_named_in_the_map_is_in_the_grid():
    """A typo in a section key would make the check unrestricted forever."""
    from shared.permissions import all_section_keys
    from shared.rights_guard import RIGHTS, AnyOf, EMPTY_CALL, Call, requirements
    keys = set(all_section_keys())
    calls = [EMPTY_CALL, Call("POST", {"id": 1}, {}, {"status": "approved",
                                                      "action": "approve"},
                              {"id": 1, "action": "approve"}),
             Call("POST", {}, {}, {"status": "approved", "action": "approve"},
                  {"action": "approve"})]
    with flask_app.test_request_context():
        from shared.tenancy import set_current_company
        set_current_company(None)
        for ep in RIGHTS:
            if ep in ("accounting.voucher_preview", "accounting.voucher_export",
                      "accounting.approve_voucher", "accounting.unapprove_voucher",
                      "accounting.delete_voucher", "accounting.voucher_form"):
                continue            # look the voucher up; covered below
            for call in calls:
                for req in requirements(ep, call):
                    pairs = req if isinstance(req, AnyOf) else (req,)
                    for section, action in pairs:
                        assert section in keys, (ep, section)
                        assert action in ACTIONS, (ep, action)


# ── Never configured = unrestricted; admins bypass ──────────────────────────

PAGES = ["/inventory/products/", "/inventory/categories/", "/inventory/units/",
         "/inventory/stock/", "/inventory/reports/low-stock",
         "/inventory/customers/", "/inventory/suppliers/",
         "/accounting/coa/", "/accounting/bank-reconciliation",
         "/accounting/vouchers/list", "/finance/trial-balance", "/fbr/",
         "/fixed-assets/assets/", "/fixed-assets/categories/",
         "/fixed-assets/reports/"]


def test_a_person_whose_rights_were_never_configured_keeps_full_access(world):
    c = client_for(world, "clerk")
    for url in PAGES:
        resp = c.get(url)
        assert resp.status_code == 200, (url, resp.status_code, resp.headers.get("Location"))


def test_admins_pass_whatever_their_rows_say(world):
    set_rights(world, who="admin", products="", chart_of_accounts="",
               financial_reports="", fixed_assets_assets="")
    try:
        c = client_for(world, "admin")
        for url in ("/inventory/products/", "/accounting/coa/",
                    "/finance/trial-balance", "/fixed-assets/assets/"):
            assert c.get(url).status_code == 200, url
    finally:
        set_rights(world, who="admin")


# ── View ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("section,url", [
    ("products", "/inventory/products/"),
    ("products", "/inventory/units/"),
    ("categories", "/inventory/categories/"),
    ("stock", "/inventory/stock/"),
    ("stock", "/inventory/transfer-to-fa/"),
    ("inventory_reports", "/inventory/reports/valuation"),
    ("inventory_reports", "/inventory/vouchers/product-ledger/list"),
    ("customers", "/inventory/customers/"),
    ("suppliers", "/inventory/suppliers/"),
    ("sales_invoices", "/inventory/invoices/list"),
    ("purchase_invoices", "/inventory/purchase-invoice/list"),
    ("chart_of_accounts", "/accounting/coa/"),
    ("bank_reconciliation", "/accounting/bank-reconciliation"),
    ("financial_reports", "/finance/trial-balance"),
    ("financial_reports", "/finance/ledger"),
    ("financial_reports", "/finance/trial-balance?view=1&format=excel"),
    ("fbr_dashboard", "/fbr/"),
    ("fixed_assets_assets", "/fixed-assets/assets/"),
    ("fixed_assets_categories", "/fixed-assets/categories/"),
    ("fixed_assets_reports", "/fixed-assets/reports/movement"),
    ("executive_reports", "/executive/receivables"),
])
def test_view_off_refuses_the_page(world, section, url):
    set_rights(world, **{section: all_but("view")})
    c = client_for(world, "clerk")
    assert_refused(c, c.get(url), "view")


def test_view_off_refuses_json_with_403(world):
    set_rights(world, inventory_reports=all_but("view"), sales_invoices=all_but("view"))
    c = client_for(world, "clerk")
    for url in ("/inventory/reports/api/stock-ledger-json",
                "/invoicing/registers/data?doc=sales&view=1"):
        resp = c.get(url)
        assert resp.status_code == 403 and resp.is_json, (url, resp.status_code)
        assert "rights" in resp.get_json()["error"]


def test_a_refusal_never_loops(world):
    """Nothing in Inventory may be viewed: the person lands on the hub, in a
    bounded number of hops, instead of bouncing between denied pages."""
    from shared.rights_guard import _INVENTORY_SECTIONS
    set_rights(world, **{s: "" for s in _INVENTORY_SECTIONS})
    c = client_for(world, "clerk")
    resp = c.get("/inventory/products/", headers={"Referer": "http://localhost/inventory/"})
    hops = 0
    while resp.status_code == 302 and hops < 6:
        resp = c.get(resp.headers["Location"])
        hops += 1
    assert resp.status_code == 200 and hops <= 3, hops


def test_a_refused_page_returns_to_where_the_person_was(world):
    set_rights(world, products="view")
    c = client_for(world, "clerk")
    resp = c.get("/inventory/products/create",
                 headers={"Referer": "http://localhost/inventory/products/"})
    assert_refused(c, resp, "create")
    assert resp.headers["Location"].endswith("/inventory/products/")


# ── Create: forms, Excel upload, batch save ─────────────────────────────────

def _xlsx(rows):
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


@pytest.mark.parametrize("section,base,model,create_form,upload_rows,batch_row", [
    ("products", "/inventory/products", "inventory_app.models.product.InvProduct",
     {"sku": "NEW1", "name": "New product"},
     [["SKU", "Name"], ["UP1", "Uploaded product"]],
     {"sku": "BT1", "name": "Batch product"}),
    ("customers", "/inventory/customers", "inventory_app.models.customer.InvCustomer",
     {"name": "New customer"}, [["Name"], ["Uploaded customer"]],
     {"name": "Batch customer"}),
    ("suppliers", "/inventory/suppliers", "inventory_app.models.supplier.InvSupplier",
     {"name": "New supplier"}, [["Name"], ["Uploaded supplier"]],
     {"name": "Batch supplier"}),
    ("products", "/inventory/units", "inventory_app.models.unit.InvUnit",
     {"name": "Bundle", "abbreviation": "bdl"}, [["Name", "Abbreviation"], ["Crate", "crt"]],
     {"name": "Pallet", "abbreviation": "plt"}),
])
def test_create_off_refuses_form_upload_and_batch(world, section, base, model,
                                                  create_form, upload_rows, batch_row):
    import json
    set_rights(world, **{section: all_but("create")})
    c = client_for(world, "clerk")
    before = count(world, model)
    assert_refused(c, c.get(f"{base}/create"), "create")
    assert_refused(c, c.post(f"{base}/create", data=create_form), "create")
    assert_refused(c, c.get(f"{base}/template"), "create")
    assert_refused(c, c.post(f"{base}/upload-excel",
                             data={"file": (_xlsx(upload_rows), "rows.xlsx")},
                             content_type="multipart/form-data"), "create")
    assert_refused(c, c.post(f"{base}/batch", data={"data": json.dumps([batch_row])}), "create")
    assert count(world, model) == before


def test_quick_unit_and_new_product_from_an_invoice_need_create(world):
    set_rights(world, products=all_but("create"))
    c = client_for(world, "clerk")
    before = count(world, "inventory_app.models.unit.InvUnit")
    resp = c.post("/inventory/units/quick-create", data={"name": "Roll", "abbreviation": "rl"})
    assert resp.status_code == 403 and resp.is_json
    resp = c.post("/inventory/purchase-invoice/api/new-product",
                  json={"name": "Quick product", "sku": "QP1"})
    assert resp.status_code == 403 and resp.is_json
    assert count(world, "inventory_app.models.unit.InvUnit") == before
    assert count(world, "inventory_app.models.product.InvProduct", sku="QP1") == 0


# ── Edit / delete ───────────────────────────────────────────────────────────

def test_edit_and_delete_off_leave_the_record_alone(world):
    from inventory_app.models.customer import InvCustomer
    from inventory_app.models.category import InvCategory
    set_rights(world, customers=all_but("edit"), categories=all_but("delete"))
    c = client_for(world, "clerk")
    assert_refused(c, c.post(f"/inventory/customers/edit/{world['customer']}",
                             data={"name": "Renamed"}), "edit")
    assert_refused(c, c.post(f"/inventory/categories/delete/{world['category']}"), "delete")
    with Ctx(world):
        assert db.session.get(InvCustomer, world["customer"]).name == "Rights Customer"
        assert db.session.get(InvCategory, world["category"]) is not None


def test_categories_need_create_and_edit(world):
    set_rights(world, categories="view")
    c = client_for(world, "clerk")
    before = count(world, "inventory_app.models.category.InvCategory")
    assert_refused(c, c.post("/inventory/categories/create", data={"name": "Inverters"}), "create")
    assert_refused(c, c.post(f"/inventory/categories/edit/{world['category']}",
                             data={"name": "Renamed"}), "edit")
    assert count(world, "inventory_app.models.category.InvCategory") == before
    assert count(world, "inventory_app.models.category.InvCategory", name="Panels") == 1


# ── Chart of accounts ───────────────────────────────────────────────────────

def test_chart_of_accounts_add_edit_delete_follow_the_grid(world):
    from shared.models.ledger import ChartOfAccount
    with Ctx(world):
        acct = ChartOfAccount.query.filter_by(level=5).order_by(ChartOfAccount.id).first()
        acct_id, name, total = acct.id, acct.name, ChartOfAccount.query.count()
    set_rights(world, chart_of_accounts="view")
    c = client_for(world, "clerk")
    assert c.get("/accounting/coa/").status_code == 200
    assert_refused(c, c.get("/accounting/coa/add"), "create")
    assert_refused(c, c.post("/accounting/coa/add", data={"name": "Sneaky", "parent_id": acct_id}),
                   "create")
    assert_refused(c, c.post(f"/accounting/coa/{acct_id}/edit", data={"name": "Renamed"}), "edit")
    assert_refused(c, c.post(f"/accounting/coa/{acct_id}/delete"), "delete")
    resp = c.get(f"/accounting/coa/api/next-code?parent_id={acct_id}")
    assert resp.status_code != 403            # view is enough for the lookups
    with Ctx(world):
        assert ChartOfAccount.query.count() == total
        assert db.session.get(ChartOfAccount, acct_id).name == name


# ── Bank reconciliation ─────────────────────────────────────────────────────

def test_bank_reconciliation_follows_its_own_section(world):
    from shared.models.bank_rec import BankReconciliation
    set_rights(world, bank_reconciliation="view")
    c = client_for(world, "clerk")
    assert c.get("/accounting/bank-reconciliation").status_code == 200
    before = count(world, "shared.models.bank_rec.BankReconciliation")
    assert_refused(c, c.post("/accounting/bank-reconciliation/new",
                             data={"account_id": "1", "statement_date": "2026-01-31",
                                   "statement_balance": "0"}), "create")
    assert count(world, "shared.models.bank_rec.BankReconciliation") == before
    assert BankReconciliation  # imported for the model path above


# ── Accounting vouchers: the type decides the section ───────────────────────

def test_voucher_types_are_separate_sections(world):
    set_rights(world, journal_vouchers="", cash_payment_vouchers="view")
    c = client_for(world, "clerk")
    body = c.get("/accounting/vouchers/list").get_data(as_text=True)
    assert "CPV-RGT-1" in body and "JV-RGT-1" not in body
    assert c.get(f"/accounting/vouchers/{world['cpv']}").status_code == 200
    assert_refused(c, c.get(f"/accounting/vouchers/{world['jv']}"), "view")
    assert_refused(c, c.get("/accounting/vouchers/list?vtype=JV"), "view")
    assert_refused(c, c.get("/accounting/vouchers?type=CPV"), "create")
    body = c.get("/accounting/").get_data(as_text=True)
    assert "CPV-RGT-1" in body and "JV-RGT-1" not in body


def test_creating_a_voucher_straight_to_approved_needs_approve(world):
    from shared.models.accounting_voucher import AccountingVoucher
    set_rights(world, journal_vouchers=all_but("approve"))
    c = client_for(world, "clerk")
    before = count(world, "shared.models.accounting_voucher.AccountingVoucher")
    assert_refused(c, c.post("/accounting/vouchers", data={
        "voucher_type": "JV", "action": "approve", "voucher_date": "2026-01-15"}), "approve")
    assert_refused(c, c.post(f"/accounting/vouchers/{world['jv']}", data={
        "action": "approve", "voucher_date": "2026-01-15"}), "approve")
    assert count(world, "shared.models.accounting_voucher.AccountingVoucher") == before
    with Ctx(world):
        assert db.session.get(AccountingVoucher, world["jv"]).status == "unapproved"


def test_the_accounting_entry_panel_follows_the_document(world):
    set_rights(world, sales_invoices="", financial_reports="")
    c = client_for(world, "clerk")
    resp = c.get("/accounting/gl-entry?type=SI&id=1")
    assert resp.status_code == 403 and resp.is_json
    set_rights(world, sales_invoices="", financial_reports="view")
    c = client_for(world, "clerk")
    assert c.get("/accounting/gl-entry?type=SI&id=1").status_code == 200


# ── Documents: a new one saved straight to approved needs create ────────────

def test_sales_invoice_save_and_approve_needs_create_and_approve(world):
    set_rights(world, sales_invoices=all_but("approve"))
    c = client_for(world, "clerk")
    resp = c.post("/inventory/invoices/save", json={"action": "approve",
                                                     "customer_id": world["customer"]})
    assert resp.status_code == 403 and "approve" in resp.get_json()["error"]
    set_rights(world, sales_invoices=all_but("create"))
    c = client_for(world, "clerk")
    resp = c.post("/inventory/invoices/save", json={"action": "approve",
                                                     "customer_id": world["customer"]})
    assert resp.status_code == 403 and "create" in resp.get_json()["error"]
    assert count(world, "inventory_app.models.invoice.InvInvoice") == 0


def test_inventory_voucher_approved_on_save_needs_approve(world):
    set_rights(world, consumption_vouchers=all_but("approve"))
    c = client_for(world, "clerk")
    before = count(world, "shared.models.vouchers.ConsumptionVoucher")
    assert_refused(c, c.post("/inventory/vouchers/consumption",
                             data={"status": "approved", "date": "2026-01-15"}), "approve")
    assert count(world, "shared.models.vouchers.ConsumptionVoucher") == before


# ── FBR and fixed assets ────────────────────────────────────────────────────

def test_fbr_submission_needs_approve(world):
    set_rights(world, fbr_dashboard="view")
    c = client_for(world, "clerk")
    assert c.get("/fbr/").status_code == 200
    assert_refused(c, c.post("/fbr/submit/1"), "approve")
    assert_refused(c, c.post("/fbr/validate/1"), "edit")


def test_fixed_assets_create_and_post_follow_the_grid(world):
    set_rights(world, fixed_assets_assets="view")
    c = client_for(world, "clerk")
    assert c.get("/fixed-assets/assets/").status_code == 200
    before = count(world, "fixed_assets_app.models.asset.FixedAsset")
    assert_refused(c, c.post("/fixed-assets/assets/create", data={"name": "Van"}), "create")
    assert_refused(c, c.post("/fixed-assets/depreciation/post"), "approve")
    assert count(world, "fixed_assets_app.models.asset.FixedAsset") == before


# ── Sidebar ─────────────────────────────────────────────────────────────────

def _nav_endpoints(world, module_key):
    from flask_login import login_user
    from shared.models.base import User
    from shared.navigation import build_nav
    from shared.tenancy import set_current_company
    with flask_app.test_request_context("/"):
        set_current_company(world["company"])
        user = db.session.get(User, world["clerk"])
        login_user(user)
        blocks = build_nav(module_key, user, "x.y")
        out = set()
        for b in blocks:
            out |= {i["endpoint"] for i in b["items"]}
        db.session.remove()
        return out


def test_sidebar_hides_what_cannot_be_opened(world):
    assert "inv_products.list_products" in _nav_endpoints(world, "inventory")
    set_rights(world, products="", inventory_reports="", journal_vouchers="",
               cash_payment_vouchers="", cash_receipt_vouchers="",
               bank_payment_vouchers="", bank_receipt_vouchers="")
    inv = _nav_endpoints(world, "inventory")
    assert "inv_products.list_products" not in inv
    assert "inv_units.list_units" not in inv
    assert "inv_reports.valuation_report" not in inv
    assert "inv_categories.list_categories" in inv
    acc = _nav_endpoints(world, "accounting")
    assert "accounting.voucher_form" not in acc and "accounting.voucher_list" not in acc
    assert "coa.list_accounts" in acc
