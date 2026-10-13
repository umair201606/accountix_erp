"""Payroll run: the journal it posts, and the page that shows it.

One fresh company, one employee, driven through the real routes. A profile
with a deduction-type component (EOBI) used to make the run 500: the slip
counted the component in its deductions, but the journal credited only
tax / PF / loan / custom / net, so it did not balance and
``post_journal_entry`` refused it. Custom deductions were credited to Salary
Payable, which overstated what the company owes its staff (the bank sheet
pays net). Both now go to Other Payroll Deductions Payable.

The run also has a detail page now (slips, totals, accounting entry button).
"""
import calendar
import os
import tempfile
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

_TMP_DB = os.path.join(tempfile.gettempdir(), "erp_payroll_run.db")
if os.path.exists(_TMP_DB):
    os.remove(_TMP_DB)
os.environ["DATABASE_URL"] = (os.environ.get("TEST_DATABASE_URL")
                              or "sqlite:///" + _TMP_DB.replace("\\", "/"))

from app import app as flask_app  # noqa: E402
from shared.extensions import db  # noqa: E402

PREV = date.today().replace(day=1) - timedelta(days=1)
M, Y = PREV.month, PREV.year
BEFORE = (PREV.replace(day=1) - timedelta(days=1))
M0, Y0 = BEFORE.month, BEFORE.year


@pytest.fixture(scope="module")
def world():
    c = flask_app.test_client()
    c.get("/")
    from hr_app.models.user import User
    from hr_app.models.compensation import PayrollProfile, PayrollComponent
    from shared.models.company import Company, CompanyMembership
    from shared.models.base import Role
    from shared.tenancy import set_current_company, unscoped
    from shared.company_setup import provision_company

    with flask_app.app_context():
        user = (User.query.filter_by(is_super_admin=True).order_by(User.id).first()
                or User.query.order_by(User.id).first())
        uid = user.id
        user.date_of_joining = date(2000, 1, 1)
        with unscoped():
            comp = Company(name="Payroll Co", slug="payroll-co-e2e",
                           is_active=True, created_by=uid)
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
        pp = PayrollProfile(user_id=uid, basic_salary=50000,
                            effective_from=date(2000, 1, 1))
        db.session.add(pp)
        db.session.flush()
        db.session.add_all([
            PayrollComponent(profile_id=pp.id, name="Conveyance",
                             type="allowance", value=5000),
            PayrollComponent(profile_id=pp.id, name="EOBI",
                             type="deduction", value=1000),
        ])
        db.session.commit()
        ids = {"company": cid, "user": uid, "name": user.full_name}

    with c.session_transaction() as sess:
        sess["_user_id"] = str(uid)
        sess["_fresh"] = True
        sess["company_id"] = cid
    ids["client"] = c
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


def assert_books_tie(w, step):
    from shared import integrity
    with Ctx(w):
        bad = integrity.failures()
    assert not bad, f"after {step}: " + "; ".join(
        f"{b['label']}: expected {b['expected']} got {b['actual']} ({b['detail']})"
        for b in bad)


def run_for(w, month, year):
    from hr_app.models.compensation import PayrollRun
    with Ctx(w):
        r = PayrollRun.query.filter_by(month=month, year=year).first()
        return None if r is None else {
            "id": r.id, "gross": Decimal(str(r.total_gross)),
            "net": Decimal(str(r.total_net)),
            "ded": Decimal(str(r.total_deductions))}


def credits(w, run_id):
    """{account name: net credit} across the run's posted PRL journals."""
    from shared.models.ledger import ChartOfAccount, JournalEntry, JournalLine
    out = {}
    with Ctx(w):
        rows = (db.session.query(ChartOfAccount.name, JournalLine.debit, JournalLine.credit)
                .join(JournalLine, JournalLine.account_id == ChartOfAccount.id)
                .join(JournalEntry, JournalLine.journal_entry_id == JournalEntry.id)
                .filter(JournalEntry.voucher_type == "PRL",
                        JournalEntry.voucher_id == run_id,
                        JournalEntry.is_posted == True)  # noqa: E712
                .all())
    for name, dr, cr in rows:
        out[name] = out.get(name, Decimal("0")) + Decimal(str(cr or 0)) - Decimal(str(dr or 0))
    return out


def test_01_component_deduction_posts_a_balanced_journal(world):
    r = world["client"].post("/compensation/run-payroll",
                             data={"month": M, "year": Y})
    assert r.status_code in (302, 303), f"run failed: {r.status_code}"
    run = run_for(world, M, Y)
    assert run is not None, "payroll run was not saved"
    assert run["gross"] == Decimal("55000"), run
    cr = credits(world, run["id"])
    assert cr.get("Salary Payable") == run["net"], cr
    assert cr.get("Other Payroll Deductions Payable") == Decimal("1000"), cr
    assert sum(cr.values()) == 0, f"journal does not balance: {cr}"
    assert_books_tie(world, "payroll with a component deduction")


def test_02_custom_deduction_is_not_owed_to_staff(world):
    import json
    adj = {str(world["user"]): {"custom_deduction": "750"}}
    r = world["client"].post("/compensation/run-payroll", data={
        "month": M0, "year": Y0, "adjustments": json.dumps(adj)})
    assert r.status_code in (302, 303)
    run = run_for(world, M0, Y0)
    cr = credits(world, run["id"])
    # Salary Payable is exactly what the bank sheet pays: the net.
    assert cr.get("Salary Payable") == run["net"], cr
    assert cr.get("Other Payroll Deductions Payable") == Decimal("1750"), cr
    assert run["gross"] - run["ded"] == run["net"]
    assert_books_tie(world, "payroll with a custom deduction")


def test_03_run_detail_page(world):
    run = run_for(world, M, Y)
    r = world["client"].get(f"/compensation/run/{run['id']}")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert world["name"] in html
    assert calendar.month_name[M] in html and str(Y) in html
    assert 'data-type="PRL"' in html and f'data-id="{run["id"]}"' in html
    assert "55,000" in html
    assert "/compensation/slip/" in html
    assert f"/compensation/export-bank-ledger/{run['id']}" in html


def test_04_history_links_to_the_run(world):
    run = run_for(world, M, Y)
    html = world["client"].get("/compensation/").get_data(as_text=True)
    assert f"/compensation/run/{run['id']}" in html


def test_05_accounting_entry_panel_shows_the_payroll_journal(world):
    run = run_for(world, M, Y)
    body = world["client"].get(f"/accounting/gl-entry?type=PRL&id={run['id']}").get_json()
    assert body["ok"] and body["posted"], body
    main = body["posted"][0]
    assert main["title"] == "Payroll" and main["balanced"]
    names = {l["name"] for l in main["lines"]}
    assert {"Salary Expense", "Salary Payable",
            "Other Payroll Deductions Payable"} <= names


def test_06_unknown_run_is_404(world):
    assert world["client"].get("/compensation/run/999999").status_code == 404


def test_07_slip_shows_this_company_not_a_hard_coded_one(world):
    from hr_app.models.compensation import PayrollSlip
    run = run_for(world, M, Y)
    with Ctx(world):
        sid = PayrollSlip.query.filter_by(payroll_run_id=run["id"]).first().id
    html = world["client"].get(f"/compensation/slip/{sid}").get_data(as_text=True)
    assert "Payroll Co" in html
    assert "Solarkon" not in html


def test_08_loan_instalment_reduces_the_loan_account(world):
    """Repayments used to be credited to a clearing liability, so the
    employee's loan receivable never came down."""
    from hr_app.models.loan import LoanAdvanceRequest
    from shared.ledger_utils import create_entity_account
    with Ctx(world):
        loan = LoanAdvanceRequest(user_id=world["user"], request_type="loan",
                                  amount=12000, purpose="bike", installment_months=12,
                                  monthly_installment=1000, remaining_amount=12000,
                                  status="approved",
                                  # paid out before payroll era: only paid-out loans are recovered
                                  disbursed_at=datetime(2000, 1, 1))
        db.session.add(loan)
        db.session.flush()
        acct_name = create_entity_account("loan", loan.id, f"Loan #{loan.id} - {world['name']}").name
        db.session.commit()
        lid = loan.id
    third = BEFORE.replace(day=1) - timedelta(days=1)
    r = world["client"].post("/compensation/run-payroll",
                             data={"month": third.month, "year": third.year})
    assert r.status_code in (302, 303)
    run = run_for(world, third.month, third.year)
    cr = credits(world, run["id"])
    assert cr.get(acct_name) == Decimal("1000"), cr
    assert "Loan Deductions Clearing" not in cr, cr
    assert sum(cr.values()) == 0
    with Ctx(world):
        assert float(db.session.get(LoanAdvanceRequest, lid).remaining_amount) == 11000
    assert_books_tie(world, "payroll with a loan instalment")


def test_09_loan_request_input_is_validated(world):
    from hr_app.models.loan import LoanAdvanceRequest
    with Ctx(world):
        before = LoanAdvanceRequest.query.count()
    c = world["client"]
    for data in ({"amount": "5000", "installments": "0", "purpose": "x"},
                 {"amount": "-5000", "installments": "6", "purpose": "x"},
                 {"amount": "abc", "installments": "6", "purpose": "x"}):
        r = c.post("/ess/loans", data={"type": "loan", **data})
        assert r.status_code == 302, data
    with Ctx(world):
        assert LoanAdvanceRequest.query.count() == before


def test_10_new_profile_does_not_pay_basic_twice(world):
    """create-profile added a "Basic Salary" allowance at 100% of basic; gross
    is basic + allowances, so the basic was paid twice."""
    import uuid
    from shared.models.base import Role, User
    from shared.models.company import CompanyMembership
    from hr_app.models.compensation import PayrollProfile
    with Ctx(world):
        tag = uuid.uuid4().hex[:8]
        u = User(employee_code=f"PR{tag}".upper(), email=f"{tag}@pay.test",
                 login_id=f"{tag}@pay.test", full_name="Nida New", is_active=True,
                 role_id=Role.query.filter_by(name=Role.EMPLOYEE).first().id,
                 date_of_joining=date(2020, 1, 1))
        u.set_password("pw12345")
        db.session.add(u)
        db.session.flush()
        db.session.add(CompanyMembership(company_id=world["company"], user_id=u.id,
                                         role_id=u.role_id, status=CompanyMembership.ACTIVE))
        db.session.commit()
        nid = u.id
    world["client"].post("/compensation/create-profile", data={
        "user_id": nid, "basic_salary": 40000, "effective_from": "2020-01-01"})
    with Ctx(world):
        pp = PayrollProfile.query.filter_by(user_id=nid).one()
        names = [c.name for c in pp.components]
        assert "Basic Salary" not in names, names
    body = world["client"].post("/compensation/payroll-preview-json",
                                data={"month": M, "year": Y}).get_json()
    mine = [e for e in body["employees"] if e["user_id"] == nid][0]
    assert mine["gross"] == 40000 * 1.45, mine   # + 25% rent, 10% medical, 10% conveyance


def test_11_migration_drops_duplicate_basic_components(world):
    import importlib.util
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from hr_app.models.compensation import PayrollComponent, PayrollProfile
    with Ctx(world):
        pp = PayrollProfile.query.filter_by(user_id=world["user"]).one()
        db.session.add_all([
            PayrollComponent(profile_id=pp.id, name="Basic Salary", type="allowance",
                             value=pp.basic_salary),
            PayrollComponent(profile_id=pp.id, name="Basic Salary", type="allowance",
                             value=123)])           # not a duplicate: kept
        db.session.commit()
        pid = pp.id
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    spec = importlib.util.spec_from_file_location(
        "m0007", os.path.join(root, "migrations", "versions", "0007_drop_duplicate_basic.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    with flask_app.app_context():
        with db.engine.begin() as conn:
            with Operations.context(MigrationContext.configure(conn)):
                mod.upgrade()
    with Ctx(world):
        left = [float(c.value) for c in PayrollComponent.query.filter_by(
            profile_id=pid, name="Basic Salary")]
        assert left == [123.0]


def test_12_preview_json_with_an_active_loan(world):
    """The run screen's recalculation returned loan model objects (500)."""
    r = world["client"].post("/compensation/payroll-preview-json", data={"month": M, "year": Y})
    assert r.status_code == 200
    me = [e for e in r.get_json()["employees"] if e["user_id"] == world["user"]][0]
    assert me["active_loans"] and me["active_loans"][0]["remaining_amount"] == 11000
