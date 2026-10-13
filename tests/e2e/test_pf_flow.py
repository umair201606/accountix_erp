"""Provident fund: every movement leaves the member's balance right.

Before: approving a PF loan CREDITED the fund (borrowing raised the balance);
withdrawals / loans could be approved again and again, and for more than
the balance; 0 instalments divided by zero; settlement paid contributions
minus loans, ignoring withdrawals and profit, so the ledger never closed and
could be settled twice; profit shares were taken over a base that included
negative balances; the employee page showed the whole fund's profit as the
member's own; the approve buttons landed on a bare JSON page.
"""
import importlib.util
import os
import tempfile
import uuid
from datetime import date

import pytest

_TMP_DB = os.path.join(tempfile.gettempdir(), "erp_pf_flow.db")
if os.path.exists(_TMP_DB):
    os.remove(_TMP_DB)
os.environ["DATABASE_URL"] = (os.environ.get("TEST_DATABASE_URL")
                              or "sqlite:///" + _TMP_DB.replace("\\", "/"))

from app import app as flask_app  # noqa: E402
from shared.extensions import db  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(scope="module")
def world():
    flask_app.test_client().get("/")
    from shared.models.base import Role, User
    from shared.models.company import Company, CompanyMembership
    from shared.tenancy import set_current_company, unscoped
    from hr_app.models.pf import PFLedger, ProvidentFundConfig

    def mk(name):
        r = Role.query.filter_by(name=Role.EMPLOYEE).first()
        tag = uuid.uuid4().hex[:8]
        u = User(employee_code=f"PF{tag}".upper(), email=f"{tag}@pf.test",
                 login_id=f"{tag}@pf.test", full_name=name, role_id=r.id,
                 is_active=True, has_hr_access=True, date_of_joining=date(2015, 1, 1))
        u.set_password("pw12345")
        db.session.add(u)
        db.session.flush()
        return u

    with flask_app.app_context():
        admin = User.query.filter_by(is_super_admin=True).order_by(User.id).first()
        with unscoped():
            comp = Company(name="Fund Co", slug="fund-co-e2e", is_active=True,
                           created_by=admin.id)
            db.session.add(comp)
            db.session.flush()
            roles = {r.name: r.id for r in Role.query.all()}
            a, b, c = mk("Asma Saver"), mk("Bilal Saver"), mk("Chand Overdrawn")
            for u, role in ((admin, Role.ADMIN), (a, Role.EMPLOYEE),
                            (b, Role.EMPLOYEE), (c, Role.EMPLOYEE)):
                db.session.add(CompanyMembership(company_id=comp.id, user_id=u.id,
                                                 role_id=roles[role],
                                                 status=CompanyMembership.ACTIVE))
            db.session.commit()
            cid = comp.id
        set_current_company(cid)
        db.session.add(ProvidentFundConfig(employee_contribution_pct=5,
                                           employer_contribution_pct=5,
                                           max_loan_percentage=50, interest_rate=0,
                                           min_service_months_for_loan=0))
        for u, amt, side in ((a, 10000, "credit"), (b, 5000, "credit"), (c, 100, "debit")):
            db.session.add(PFLedger(user_id=u.id, transaction_date=date(2026, 1, 31),
                                    transaction_type="contribution", description="seed",
                                    credit=amt if side == "credit" else 0,
                                    debit=amt if side == "debit" else 0))
        db.session.commit()
        w = {"company": cid, "admin": admin.id, "a": a.id, "b": b.id, "c": c.id}

    def client(uid):
        cl = flask_app.test_client()
        with cl.session_transaction() as s:
            s["_user_id"] = str(uid)
            s["_fresh"] = True
            s["company_id"] = cid
        return cl
    w["cl"] = {k: client(w[k]) for k in ("admin", "a")}
    return w


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


def bal(w, who):
    from hr_app.models.pf import PFLedger
    with Ctx(w):
        rows = PFLedger.query.filter_by(user_id=w[who]).all()
        return round(sum(float(r.credit or 0) - float(r.debit or 0) for r in rows), 2)


def latest(w, model):
    with Ctx(w):
        r = model.query.order_by(model.id.desc()).first()
        return None if r is None else (r.id, r.status)


def test_01_withdrawal_over_the_balance_is_refused(world):
    from hr_app.models.pf import PFWithdrawalRequest
    world["cl"]["a"].post("/pf/request-withdrawal", data={"amount": 20000, "reason": "car"})
    assert latest(world, PFWithdrawalRequest) is None


def test_02_withdrawal_approves_once(world):
    from hr_app.models.pf import PFWithdrawalRequest
    world["cl"]["a"].post("/pf/request-withdrawal", data={"amount": 3000, "reason": "fees"})
    wid, status = latest(world, PFWithdrawalRequest)
    assert status == "pending"
    r = world["cl"]["admin"].post(f"/pf/approve-withdrawal/{wid}")
    assert r.status_code == 302, "plain form must redirect, not show JSON"
    assert bal(world, "a") == 7000
    world["cl"]["admin"].post(f"/pf/approve-withdrawal/{wid}")
    assert bal(world, "a") == 7000, "approving twice debited twice"


def test_03_loan_debits_the_fund(world):
    from hr_app.models.pf import PFLoanRequest
    c = world["cl"]["a"]
    c.post("/pf/request-loan", data={"amount": 2000, "installments": 0, "purpose": "x"})
    assert latest(world, PFLoanRequest) is None, "0 instalments must be refused, not 500"
    c.post("/pf/request-loan", data={"amount": 2000, "installments": 10, "purpose": "roof"})
    lid, status = latest(world, PFLoanRequest)
    assert status == "pending"
    world["cl"]["admin"].post(f"/pf/approve-loan/{lid}")
    assert bal(world, "a") == 5000, "a loan out of the fund must reduce the balance"
    world["cl"]["admin"].post(f"/pf/approve-loan/{lid}")
    assert bal(world, "a") == 5000


def test_04_reject_leaves_the_balance(world):
    from hr_app.models.pf import PFWithdrawalRequest
    world["cl"]["a"].post("/pf/request-withdrawal", data={"amount": 1000, "reason": "trip"})
    wid, _ = latest(world, PFWithdrawalRequest)
    world["cl"]["admin"].post(f"/pf/approve-withdrawal/{wid}", data={"action": "reject"})
    assert latest(world, PFWithdrawalRequest)[1] == "rejected"
    assert bal(world, "a") == 5000


def test_05_profit_shares_add_up_and_skip_negative_balances(world):
    before = {k: bal(world, k) for k in ("a", "b", "c")}
    r = world["cl"]["admin"].post("/pf/distribute-profit",
                                  data={"month": 6, "year": 2026, "total_profit": 1000})
    assert r.status_code == 302
    gained = {k: round(bal(world, k) - before[k], 2) for k in before}
    assert gained["c"] == 0
    assert round(gained["a"] + gained["b"], 2) == 1000
    assert gained["a"] == 500 and gained["b"] == 500     # 5,000 each


def test_06_employee_page_shows_the_balance_after_profit(world):
    html = world["cl"]["a"].get("/pf/").get_data(as_text=True)
    assert "5,500" in html          # 5,000 + the 500 profit share


def test_07_settlement_closes_the_ledger_once(world):
    from hr_app.models.holiday import PFSettlement
    world["cl"]["admin"].post(f"/pf/settle/{world['a']}")
    assert bal(world, "a") == 0, "settlement must pay out the whole ledger balance"
    with Ctx(world):
        s = PFSettlement.query.filter_by(user_id=world["a"]).one()
        assert float(s.net_settlement) == 5500
    world["cl"]["admin"].post(f"/pf/settle/{world['a']}")
    with Ctx(world):
        assert PFSettlement.query.filter_by(user_id=world["a"]).count() == 1


def test_08_settling_a_non_member_is_refused(world):
    r = world["cl"]["admin"].post("/pf/settle/999999", headers={"Accept": "application/json"})
    assert r.status_code == 404


def test_09_migration_turns_old_loan_credits_into_debits(world):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from hr_app.models.pf import PFLedger
    with Ctx(world):
        db.session.add(PFLedger(user_id=world["b"], transaction_date=date(2026, 2, 1),
                                transaction_type="loan", description="PF loan approved",
                                credit=700, debit=0))
        db.session.commit()
    before = bal(world, "b")
    spec = importlib.util.spec_from_file_location(
        "m0006", os.path.join(ROOT, "migrations", "versions", "0006_pf_loan_debit.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    with flask_app.app_context():
        with db.engine.begin() as conn:
            with Operations.context(MigrationContext.configure(conn)):
                mod.upgrade()
    assert bal(world, "b") == round(before - 1400, 2)
