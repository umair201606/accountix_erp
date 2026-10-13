"""Payroll arithmetic through the real routes: proration, taxable income,
cumulative withholding tax, loan cap and percentage components.

One fresh company, one employee (basic 150,000). Tax year 2026 slabs are
loaded through the tax settings page, so every expected figure below is
plain slab arithmetic:

    annual taxable 1,800,000 -> 6,000 + 11% x 600,000 = 72,000 (6,000/month)
"""
import json
import os
import tempfile
from datetime import date

import pytest

_TMP_DB = os.path.join(tempfile.gettempdir(), "erp_payroll_engine.db")
if os.path.exists(_TMP_DB):
    os.remove(_TMP_DB)
os.environ["DATABASE_URL"] = (os.environ.get("TEST_DATABASE_URL")
                              or "sqlite:///" + _TMP_DB.replace("\\", "/"))

from app import app as flask_app  # noqa: E402
from shared.extensions import db  # noqa: E402

BASIC = 150_000


@pytest.fixture(scope="module")
def world():
    c = flask_app.test_client()
    c.get("/")
    from hr_app.models.user import User
    from hr_app.models.compensation import PayrollProfile
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
            comp = Company(name="Payroll Engine Co", slug="payroll-engine-e2e",
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
        db.session.add(PayrollProfile(user_id=uid, basic_salary=BASIC,
                                      effective_from=date(2000, 1, 1)))
        db.session.commit()
    with c.session_transaction() as sess:
        sess["_user_id"] = str(uid)
        sess["_fresh"] = True
        sess["company_id"] = cid
    return {"client": c, "company": cid, "user": uid}


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


def preview(w, month, year, adjustments=None):
    r = w["client"].post("/compensation/payroll-preview-json", data={
        "month": month, "year": year, "adjustments": json.dumps(adjustments or {})})
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    return [e for e in r.get_json()["employees"] if e["user_id"] == w["user"]][0]


def set_components(w, comps):
    r = w["client"].post(f"/compensation/edit-profile/{w['user']}", data={
        "basic_salary": BASIC, "effective_from": "2000-01-01",
        "components_json": json.dumps(comps)})
    assert r.status_code in (302, 303)


def test_01_no_slabs_means_no_tax(world):
    """It used to invent a flat 5%."""
    p = preview(world, 3, 2025)
    assert p["monthly_tax"] == 0
    page = world["client"].get("/compensation/run-payroll").get_data(as_text=True)
    assert "No income tax slabs" in page


def test_02_tax_year_2026_slabs_and_monthly_tax(world):
    r = world["client"].post("/compensation/tax-settings", data={"action": "seed_defaults"})
    assert r.status_code in (302, 303)
    with Ctx(world):
        from hr_app.models.tax import IncomeTaxSlab
        assert IncomeTaxSlab.calculate_tax(1_800_000) == 72_000
        assert IncomeTaxSlab.calculate_tax(600_000) == 0
        assert IncomeTaxSlab.calculate_tax(5_000_000) == 616_000 + 0.35 * 900_000
    # July: a full tax year ahead at 150,000 a month.
    assert preview(world, 7, 2025)["monthly_tax"] == 6_000


def test_03_exempt_allowance_is_not_taxed_and_percent_components(world):
    set_components(world, [
        {"name": "House Rent", "type": "allowance", "method": "percent_of_basic",
         "value": 40, "is_taxable": True},
        {"name": "Medical", "type": "allowance", "method": "fixed",
         "value": 15_000, "is_taxable": False},
    ])
    p = preview(world, 7, 2025)
    assert p["allowances_breakdown"]["House Rent"] == 60_000
    assert p["gross"] == BASIC + 60_000 + 15_000
    assert p["taxable"] == BASIC + 60_000
    # 210,000 x 12 = 2,520,000 -> 116,000 + 23% x 320,000 = 189,600
    assert p["monthly_tax"] == pytest.approx(189_600 / 12, abs=0.01)
    set_components(world, [])


def test_04_bad_component_is_refused(world):
    r = world["client"].post(f"/compensation/edit-profile/{world['user']}", data={
        "basic_salary": BASIC, "effective_from": "2000-01-01",
        "components_json": json.dumps([{"name": "Odd", "type": "allowance",
                                        "method": "percent_of_basic", "value": 250}])},
        follow_redirects=True)
    assert "at most 100" in r.get_data(as_text=True)


def test_05_bonus_is_taxed_once_not_annualised(world):
    """Gross x12 taxed a bonus month as if the bonus came every month."""
    base = preview(world, 7, 2025)["monthly_tax"]
    with_bonus = preview(world, 7, 2025, {str(world["user"]): {"bonus": "300000"}})
    # 1,800,000 + 300,000 = 2,100,000 -> 6,000 + 11% x 900,000 = 105,000;
    # the bonus month carries the whole extra 33,000.
    assert with_bonus["monthly_tax"] == pytest.approx(base + 33_000, abs=0.01)
    assert with_bonus["taxable"] == BASIC + 300_000


def test_06_withholding_is_cumulative_over_the_tax_year(world):
    """A bonus paid in July is in August's year-to-date: August withholds the
    regular share only and the year still adds up to the annual tax."""
    r = world["client"].post("/compensation/run-payroll", data={
        "month": 7, "year": 2025,
        "adjustments": json.dumps({str(world["user"]): {"bonus": "300000"}})})
    assert r.status_code in (302, 303)
    with Ctx(world):
        from hr_app.models.compensation import PayrollSlip
        slip = PayrollSlip.query.filter_by(user_id=world["user"]).one()
        assert float(slip.taxable_pay) == BASIC + 300_000
        july_tax = json.loads(slip.components_json)["deductions"]["Income Tax"]
    aug = preview(world, 8, 2025)
    # projected 450,000 + 150,000 x 11 = 2,100,000 -> 105,000; 11 months left
    assert aug["monthly_tax"] == pytest.approx((105_000 - july_tax) / 11, abs=0.01)
    assert july_tax + aug["monthly_tax"] * 11 == pytest.approx(105_000, abs=0.5)


def test_07_unpaid_leave_and_joiners_prorate_by_calendar_days(world):
    from hr_app.models.leave import LeaveRequest, LeaveType
    with Ctx(world):
        lwp = LeaveType(name="Leave without pay", code="LWP", is_paid=False)
        db.session.add(lwp)
        db.session.flush()
        db.session.add(LeaveRequest(user_id=world["user"], leave_type_id=lwp.id,
                                    start_date=date(2025, 3, 10), end_date=date(2025, 3, 12),
                                    total_days=3, reason="personal", status="approved"))
        db.session.commit()
    p = preview(world, 3, 2025)                       # March: 31 days
    assert p["unpaid_days"] == 3
    assert p["basic"] == pytest.approx(round(BASIC * 28 / 31, 2), abs=0.01)
    with Ctx(world):
        from hr_app.models.user import User
        User.query.get(world["user"]).date_of_joining = date(2025, 4, 21)
        db.session.commit()
    p = preview(world, 4, 2025)                       # April: 30 days, 10 worked
    assert p["basic"] == pytest.approx(BASIC * 10 / 30, abs=0.01)
    assert preview_or_none(world, 3, 2025) is None    # not joined yet
    with Ctx(world):
        User.query.get(world["user"]).date_of_joining = date(2000, 1, 1)
        db.session.commit()


def preview_or_none(w, month, year):
    r = w["client"].post("/compensation/payroll-preview-json",
                         data={"month": month, "year": year})
    rows = [e for e in r.get_json()["employees"] if e["user_id"] == w["user"]]
    return rows[0] if rows else None


def test_08_loan_instalment_never_drives_net_below_zero(world):
    from hr_app.models.loan import LoanAdvanceRequest
    with Ctx(world):
        loan = LoanAdvanceRequest(user_id=world["user"], request_type="loan",
                                  amount=1_000_000, installment_months=1,
                                  monthly_installment=1_000_000,
                                  remaining_amount=1_000_000, purpose="house",
                                  status="approved")
        db.session.add(loan)
        db.session.commit()
    p = preview(world, 9, 2025)
    assert p["net"] == 0
    assert p["loan_deduction"] == pytest.approx(p["gross"] - p["monthly_tax"]
                                                - p["pf_employee"], abs=0.01)
