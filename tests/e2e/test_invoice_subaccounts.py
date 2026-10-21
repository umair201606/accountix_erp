"""§12.2 Example B through the real routes: revenue and tax sub-accounts.

The setting maps are only worth having if approval actually posts to them. This
creates a fresh company, maps two product categories and two tax rates through
the Settings write route, then posts one two-rate invoice with a Combined
freight charge and a Per-item labour charge. The journal must credit each
mapped revenue account and each mapped tax sub-account with the figures in the
spec: 2,000 / 3,000 revenue and 335.15 / 501.17 output tax.

The invoice totals are the form's own (§10 Example B): net 1,800 / 2,850,
tax bases 1,861.94 / 2,948.06, tax 836.32, net payable 5,646.32.
"""
import os
import tempfile
import uuid
from contextlib import contextmanager
from datetime import date, timedelta
from decimal import Decimal

import pytest

_TMP_DB = os.path.join(tempfile.gettempdir(), "erp_invoice_subaccounts.db")
if os.path.exists(_TMP_DB):
    os.remove(_TMP_DB)
os.environ["DATABASE_URL"] = "sqlite:///" + _TMP_DB.replace("\\", "/")

from app import app as flask_app  # noqa: E402
from shared.extensions import db  # noqa: E402


def _sfx():
    return uuid.uuid4().hex[:10]


@pytest.fixture(scope="module")
def world():
    c = flask_app.test_client()
    c.get("/")  # lazy create_all + migrate + seed
    from shared.models.base import Role, User
    from shared.models.company import Company, CompanyMembership
    from shared.tenancy import set_current_company, unscoped
    from shared.company_setup import provision_company

    with flask_app.app_context():
        user = (User.query.filter_by(is_super_admin=True).order_by(User.id).first()
                or User.query.order_by(User.id).first())
        assert user is not None, "seed produced no users"
        uid = user.id
        with unscoped():
            comp = Company(name="Subaccount Co " + _sfx(),
                           slug="subaccounts-" + _sfx(), is_active=True,
                           created_by=uid)
            db.session.add(comp)
            db.session.flush()
            admin_role = Role.query.filter_by(name=Role.ADMIN).first()
            db.session.add(CompanyMembership(
                company_id=comp.id, user_id=uid, role_id=admin_role.id,
                status=CompanyMembership.ACTIVE))
            db.session.commit()
            cid = comp.id
        set_current_company(cid)
        provision_company(cid)
        set_current_company(cid)

        from shared.models.inventory_settings import InventorySettings
        from inventory_app.models.category import InvCategory
        from inventory_app.models.customer import InvCustomer
        from inventory_app.models.product import InvProduct

        inv_settings = InventorySettings.get()
        inv_settings.allow_negative_stock = True

        hardware = InvCategory(name="Hardware")
        fittings = InvCategory(name="Fittings")
        customer = InvCustomer(name="Subaccount Customer")
        db.session.add_all([hardware, fittings, customer])
        db.session.flush()
        p1 = InvProduct(sku="SA-1", name="Server", category_id=hardware.id,
                        unit_price=1000, cost_price=0, current_stock=0)
        p2 = InvProduct(sku="SA-2", name="Rack", category_id=fittings.id,
                        unit_price=3000, cost_price=0, current_stock=0)
        db.session.add_all([p1, p2])
        db.session.commit()
        ids = {
            "company": cid,
            "user": uid,
            "customer": customer.id,
            "hardware": hardware.id,
            "fittings": fittings.id,
            "p1": p1.id,
            "p2": p2.id,
        }

    with c.session_transaction() as sess:
        sess["_user_id"] = str(uid)
        sess["_fresh"] = True
        sess["company_id"] = cid
    ids["client"] = c
    return ids


@contextmanager
def company_ctx(world):
    from shared.tenancy import set_current_company
    with flask_app.app_context():
        set_current_company(world["company"])
        try:
            yield
        finally:
            db.session.remove()


def _account(world, code, name, kind, parent_code):
    from shared.models.ledger import ChartOfAccount
    with company_ctx(world):
        parent = ChartOfAccount.query.filter_by(code=parent_code).one()
        acct = ChartOfAccount(code=code, name=name, type=kind, level=5,
                              parent_id=parent.id, is_active=True)
        db.session.add(acct)
        db.session.commit()
        return acct.id


@pytest.fixture(scope="module")
def accounts(world):
    return {
        "hardware_rev": _account(world, "4-01-01-01-0101", "Hardware Sales",
                                  "revenue", "4-01-01-01"),
        "fittings_rev": _account(world, "4-01-01-01-0102", "Fittings Sales",
                                  "revenue", "4-01-01-01"),
        "tax18": _account(world, "2-01-03-01-0018", "Output Sales Tax 18%",
                           "liability", "2-01-03-01"),
        "tax17": _account(world, "2-01-03-01-0017", "Output Sales Tax 17%",
                           "liability", "2-01-03-01"),
        "freight": _account(world, "4-03-01-01-0002", "Freight Recovered",
                             "revenue", "4-03-01-01"),
        "labour": _account(world, "4-03-01-01-0003", "Labour Recovered",
                            "revenue", "4-03-01-01"),
        "unloading": _account(world, "5-04-01-01-0003", "Unloading",
                               "expense", "5-04-01-01"),
    }


def _save_settings(world, category_id=None, revenue_id=None,
                   rate=None, tax_id=None, **extra):
    data = {"default_sales_tax_pct": 0, "default_discount_pct": 0,
            "default_charges_mode": "general", "default_discount_mode": "general",
            "default_tax_mode": "general", "default_party_mode": "relevant",
            "withholding_base": "taxable"}
    if category_id and revenue_id:
        data["cra_new_category"] = category_id
        data["cra_new_account"] = revenue_id
    if rate is not None and tax_id:
        data["tra_new_rate"] = rate
        data["tra_new_account"] = tax_id
    data.update(extra)
    return world["client"].post("/settings/invoicing", data=data)


def test_mappings_are_saved_through_the_settings_route(world, accounts):
    r = _save_settings(world, world["hardware"], accounts["hardware_rev"])
    assert r.status_code in (302, 200)
    r = _save_settings(world, world["fittings"], accounts["fittings_rev"])
    assert r.status_code in (302, 200)
    r = _save_settings(world, rate=18, tax_id=accounts["tax18"])
    assert r.status_code in (302, 200)
    r = _save_settings(world, rate=17, tax_id=accounts["tax17"])
    assert r.status_code in (302, 200)

    from shared.models.invoice_settings import (CategoryRevenueAccount,
                                                TaxRateAccount)
    with company_ctx(world):
        cats = {m.category_id: m.account_id for m in CategoryRevenueAccount.query.all()}
        rates = {round(float(m.rate_pct), 2): m.account_id
                 for m in TaxRateAccount.query.all()}
    assert cats[world["hardware"]] == accounts["hardware_rev"]
    assert cats[world["fittings"]] == accounts["fittings_rev"]
    assert rates[18.0] == accounts["tax18"]
    assert rates[17.0] == accounts["tax17"]


def test_a_wrong_type_or_missing_account_is_refused(world, accounts):
    """The pickers only offer revenue/liability ledgers; the server must agree.
    An expense account id posted by hand must not become Revenue Hardware."""
    r = world["client"].post("/settings/invoicing", data={
        "default_sales_tax_pct": 0, "default_discount_pct": 0,
        "cra_new_category": world["hardware"],
        "cra_new_account": accounts["unloading"],
    }, follow_redirects=True)
    body = r.get_data(as_text=True).lower()
    assert r.status_code == 200
    assert "ignored" in body

    from shared.models.invoice_settings import CategoryRevenueAccount
    with company_ctx(world):
        m = CategoryRevenueAccount.query.filter_by(
            category_id=world["hardware"]).one()
    assert m.account_id == accounts["hardware_rev"]


def _post_example_b(world, accounts):
    today = date.today()
    payload = {
        "customer_id": world["customer"],
        "invoice_date": today.isoformat(),
        "due_date": (today + timedelta(days=30)).isoformat(),
        "discount_mode": "individual",
        "charges_mode": "individual",
        "tax_mode": "individual",
        "global_discount_pct": 0,
        "global_discount_value": 0,
        "global_delivery": 0,
        "global_installation": 0,
        "global_sales_tax_pct": 0,
        "further_tax_pct": 0,
        "apply_further_tax": False,
        "withholding_tax_pct": 0,
        "apply_withholding_tax": False,
        "subtotal": 5000,
        "total_discount": 350,
        "total_charges": 160,
        "total_tax": 836.32,
        "total_further_tax": 0,
        "total_withholding_tax": 0,
        "total_amount": 5646.32,
        "notes": "Example B",
        "items": [
            {"product_id": world["p1"], "quantity": 2, "unit_price": 1000,
             "unit": "pcs", "description": "Server",
             "discount_pct": 10, "discount_amount": 200,
             "delivery": 0, "installation": 0, "sales_tax_pct": 18,
             "total_before_discount": 2000, "total_after_discount": 1800},
            {"product_id": world["p2"], "quantity": 1, "unit_price": 3000,
             "unit": "pcs", "description": "Rack",
             "discount_pct": 5, "discount_amount": 150,
             "delivery": 0, "installation": 0, "sales_tax_pct": 17,
             "total_before_discount": 3000, "total_after_discount": 2850},
        ],
        "charges": [
            {"charge_account_id": accounts["freight"], "description": "Freight",
             "amount": 100, "scope": "general", "distribution": "pro_rata_value",
             "treatment": "bill", "st_taxable": True, "wht_taxable": False},
            {"charge_account_id": accounts["labour"], "description": "Labour",
             "amount": 60, "scope": "individual", "distribution": "pro_rata_value",
             "treatment": "bill", "st_taxable": True, "wht_taxable": False},
            {"charge_account_id": accounts["unloading"], "description": "Unloading",
             "amount": 40, "scope": "general", "treatment": "expense",
             "st_taxable": False, "wht_taxable": False},
        ],
        "action": "approve",
    }
    r = world["client"].post("/inventory/invoices/save", json=payload)
    assert r.status_code == 200, r.get_data(as_text=True)[:1000]
    body = r.get_json() or {}
    assert body.get("ok") is not False, body
    return body["id"]


def _journal(world, invoice_id):
    from shared.models.ledger import JournalEntry, JournalLine
    with company_ctx(world):
        entry = JournalEntry.query.filter_by(
            voucher_type="SI", voucher_id=invoice_id).one()
        lines = JournalLine.query.filter_by(journal_entry_id=entry.id).all()
        return [(ln.account_id, round(float(ln.debit or 0), 2),
                 round(float(ln.credit or 0), 2)) for ln in lines]


def test_example_b_posts_each_category_and_rate_to_its_own_account(world, accounts):
    invoice_id = _post_example_b(world, accounts)
    lines = _journal(world, invoice_id)

    credits = {}
    debits = {}
    for account_id, debit, credit in lines:
        if credit:
            credits[account_id] = round(credits.get(account_id, 0) + credit, 2)
        if debit:
            debits[account_id] = round(debits.get(account_id, 0) + debit, 2)

    assert credits[accounts["hardware_rev"]] == 2000.00
    assert credits[accounts["fittings_rev"]] == 3000.00
    assert credits[accounts["tax18"]] == 335.15
    assert credits[accounts["tax17"]] == 501.17
    assert credits[accounts["freight"]] == 100.00
    assert credits[accounts["labour"]] == 60.00
    assert debits[accounts["unloading"]] == 40.00

    assert round(sum(d for _, d, _ in lines), 2) == 6036.32
    assert round(sum(c for _, _, c in lines), 2) == 6036.32
    assert round(sum(c for _, _, c in lines), 2) == round(
        sum(d for _, d, _ in lines), 2)


def test_unmapped_system_still_posts_one_revenue_and_one_tax_credit(world):
    """The fallback is the safety property: a company that maps nothing must
    keep the old single-credit posting. Uses a fresh company with no maps."""
    from shared.models.base import Role, User
    from shared.models.company import Company, CompanyMembership
    from shared.tenancy import set_current_company, unscoped
    from shared.company_setup import provision_company
    from inventory_app.models.category import InvCategory
    from inventory_app.models.customer import InvCustomer
    from inventory_app.models.product import InvProduct

    c = flask_app.test_client()
    with flask_app.app_context():
        user = User.query.filter_by(is_super_admin=True).order_by(User.id).first()
        uid = user.id
        with unscoped():
            comp = Company(name="Unmapped Co " + _sfx(), slug="unmapped-" + _sfx(),
                           is_active=True, created_by=uid)
            db.session.add(comp)
            db.session.flush()
            db.session.add(CompanyMembership(
                company_id=comp.id, user_id=uid,
                role_id=Role.query.filter_by(name=Role.ADMIN).first().id,
                status=CompanyMembership.ACTIVE))
            db.session.commit()
            cid = comp.id
        set_current_company(cid)
        provision_company(cid)
        set_current_company(cid)
        from shared.models.inventory_settings import InventorySettings
        InventorySettings.get().allow_negative_stock = True
        cat = InvCategory(name="General")
        cust = InvCustomer(name="Plain Customer")
        db.session.add_all([cat, cust])
        db.session.flush()
        prod = InvProduct(sku="UN-1", name="Widget", category_id=cat.id,
                          unit_price=100, cost_price=0, current_stock=0)
        db.session.add(prod)
        db.session.commit()
        pid, cust_id = prod.id, cust.id
        # Keep the demo product/category for this company only.
        from shared.tenancy import set_current_company as _set
        _set(cid)

    with c.session_transaction() as sess:
        sess["_user_id"] = str(uid)
        sess["_fresh"] = True
        sess["company_id"] = cid

    payload = {
        "customer_id": cust_id, "invoice_date": date.today().isoformat(),
        "due_date": (date.today() + timedelta(days=10)).isoformat(),
        "discount_mode": "general", "charges_mode": "general",
        "tax_mode": "individual", "global_sales_tax_pct": 0,
        "subtotal": 100, "total_discount": 0, "total_charges": 0,
        "total_tax": 18, "total_amount": 118, "further_tax_pct": 0,
        "apply_further_tax": False, "withholding_tax_pct": 0,
        "apply_withholding_tax": False,
        "items": [{"product_id": pid, "quantity": 1, "unit_price": 100,
                   "unit": "pcs", "description": "Widget", "sales_tax_pct": 18,
                   "total_before_discount": 100, "total_after_discount": 100}],
        "charges": [], "action": "approve",
    }
    r = c.post("/inventory/invoices/save", json=payload)
    assert r.status_code == 200, r.get_data(as_text=True)[:600]
    invoice_id = (r.get_json() or {})["id"]

    from shared.models.ledger import JournalEntry, JournalLine
    with flask_app.app_context():
        set_current_company(cid)
        entry = JournalEntry.query.filter_by(
            voucher_type="SI", voucher_id=invoice_id).one()
        lines = JournalLine.query.filter_by(journal_entry_id=entry.id).all()
        credits = [round(float(ln.credit or 0), 2) for ln in lines
                   if float(ln.credit or 0)]
    # Revenue 100 and output tax 18, each a single credit line (plus no COGS).
    assert sorted(credits) == [18.0, 100.0]
