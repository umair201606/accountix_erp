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
from datetime import date, datetime

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
                                  status="approved", disbursed_at=datetime(2025, 1, 1))
        db.session.add(loan)
        db.session.commit()
    p = preview(world, 9, 2025)
    assert p["net"] == 0
    assert p["loan_deduction"] == pytest.approx(p["gross"] - p["monthly_tax"]
                                                - p["pf_employee"], abs=0.01)


def test_09_payroll_settings_page_validates_and_saves(world):
    c = world["client"]
    page = c.get("/compensation/tax-settings").get_data(as_text=True)
    assert "Payroll settings" in page and "Surcharge" in page
    r = c.post("/compensation/tax-settings", data={
        "action": "policy", "overtime_multiplier": "9", "overtime_hours_basis": "160",
        "eobi_wage_base": "37000", "eobi_employee_pct": "1", "eobi_employer_pct": "5",
        "surcharge_threshold": "10000000", "surcharge_pct": "9"},
        headers={"Referer": "/compensation/tax-settings"}, follow_redirects=True)
    assert "Overtime rate 1" in r.get_data(as_text=True)
    r = c.post("/compensation/tax-settings", data={
        "action": "policy", "overtime_multiplier": "2", "overtime_hours_basis": "208",
        "eobi_enabled": "1", "eobi_wage_base": "37000", "eobi_employee_pct": "1",
        "eobi_employer_pct": "5", "surcharge_threshold": "10000000", "surcharge_pct": "9"})
    assert r.status_code in (302, 303)
    with Ctx(world):
        from hr_app.models.tax import PayrollSettings
        s = PayrollSettings.get()
        assert float(s.overtime_multiplier) == 2 and s.eobi_enabled


def test_10_overtime_follows_the_policy(world):
    from hr_app.models.holiday import OvertimeAccount
    with Ctx(world):
        db.session.add(OvertimeAccount(user_id=world["user"], date=date(2025, 10, 6),
                                       regular_hours=8, overtime_hours=10))
        db.session.commit()
    p = preview(world, 10, 2025)
    assert p["ot_pay"] == pytest.approx(10 * BASIC / 208 * 2, abs=0.01)


def test_11_eobi_is_deducted_and_posted(world):
    """Employee 1% and employer 5% of the 37,000 wage base, both to EOBI Payable."""
    from hr_app.models.loan import LoanAdvanceRequest
    with Ctx(world):
        LoanAdvanceRequest.query.update({"status": "rejected"})   # test_08's loan
        db.session.commit()
    p = preview(world, 11, 2025)
    assert p["eobi_employee"] == 370 and p["eobi_employer"] == 1850
    assert p["deductions_breakdown"]["EOBI (employee)"] == 370
    r = world["client"].post("/compensation/run-payroll", data={"month": 11, "year": 2025})
    assert r.status_code in (302, 303)
    from shared.models.ledger import ChartOfAccount, JournalEntry, JournalLine
    from hr_app.models.compensation import PayrollRun
    with Ctx(world):
        run = PayrollRun.query.filter_by(month=11, year=2025).one()
        rows = (db.session.query(ChartOfAccount.name, JournalLine.debit, JournalLine.credit)
                .join(JournalLine, JournalLine.account_id == ChartOfAccount.id)
                .join(JournalEntry, JournalLine.journal_entry_id == JournalEntry.id)
                .filter(JournalEntry.voucher_type == "PRL", JournalEntry.voucher_id == run.id,
                        JournalEntry.is_posted == True).all())  # noqa: E712
    net = {}
    for name, dr, cr in rows:
        net[name] = net.get(name, 0) + float(cr or 0) - float(dr or 0)
    assert net["EOBI Payable"] == pytest.approx(370 + 1850)
    assert net["EOBI Employer Expense"] == pytest.approx(-1850)
    assert sum(net.values()) == pytest.approx(0, abs=0.01)
    from shared import integrity
    with Ctx(world):
        assert not integrity.failures()


def test_12_surcharge_on_high_income(world):
    with Ctx(world):
        from hr_app.payroll_engine import tax_on
        from hr_app.models.tax import IncomeTaxSlab
        base = IncomeTaxSlab.calculate_tax(12_000_000)
        assert tax_on(12_000_000) == pytest.approx(base * 1.09, abs=0.01)
        assert tax_on(9_000_000) == IncomeTaxSlab.calculate_tax(9_000_000)


def test_13_pf_on_basic_or_gross(world):
    from hr_app.models.pf import ProvidentFundConfig
    set_components(world, [{"name": "Conveyance", "type": "allowance", "method": "fixed",
                            "value": 10_000, "is_taxable": True}])
    with Ctx(world):
        db.session.add(ProvidentFundConfig(employee_contribution_pct=5,
                                           employer_contribution_pct=5))
        db.session.commit()
    p = preview(world, 12, 2025)
    assert p["pf_employee"] == BASIC * 0.05            # new configs: basic
    world["client"].post("/pf/config", data={
        "employee_pct": "5", "employer_pct": "5", "contribution_base": "gross",
        "max_loan_pct": "50", "interest_rate": "0", "min_months": "12"})
    p = preview(world, 12, 2025)
    assert p["pf_employee"] == pytest.approx(p["gross"] * 0.05, abs=0.01)
    set_components(world, [])


def test_14_leaving_date_prorates_and_then_stops(world):
    c = world["client"]
    with Ctx(world):
        from hr_app.models.user import User
        u = User.query.get(world["user"])
        form = {"email": u.email, "full_name": u.full_name, "role_id": u.role_id,
                "is_active": "1", "date_of_joining": "2000-01-01",
                "date_of_leaving": "2026-01-10"}
        from shared.models.company import CompanyMembership
        form["role_id"] = CompanyMembership.query.filter_by(
            company_id=world["company"], user_id=world["user"]).one().role_id
    r = c.post(f"/auth/users/{world['user']}/edit", data=form)
    assert r.status_code in (302, 303)
    p = preview(world, 1, 2026)
    assert p["basic"] == pytest.approx(BASIC * 10 / 31, abs=0.01)
    assert preview_or_none(world, 2, 2026) is None
    form["date_of_leaving"] = ""
    c.post(f"/auth/users/{world['user']}/edit", data=form)
    assert preview_or_none(world, 2, 2026) is not None


def test_15_final_settlement_gratuity_and_leave_encashment(world):
    """Joined 1 Jan 2000, leaves 15 Mar 2026: 26 years 2 months -> 26 years.
    Gratuity 30 days of basic a year = 150,000 x 26; 10 unused days of
    carry-forward leave at basic / 30."""
    from hr_app.models.leave import LeaveQuota, LeaveType
    from shared.models.company import CompanyMembership
    c = world["client"]
    r = c.post("/compensation/tax-settings", data={
        "action": "policy", "overtime_multiplier": "2", "overtime_hours_basis": "208",
        "eobi_wage_base": "37000", "eobi_employee_pct": "1", "eobi_employer_pct": "5",
        "surcharge_threshold": "10000000", "surcharge_pct": "9",
        "gratuity_enabled": "1", "gratuity_days_per_year": "30", "gratuity_basis": "basic",
        "leave_encashment_enabled": "1"})
    assert r.status_code in (302, 303)
    with Ctx(world):
        al = LeaveType(name="Annual leave", code="AL", is_paid=True, carry_forward=True)
        db.session.add(al)
        db.session.flush()
        db.session.add(LeaveQuota(user_id=world["user"], leave_type_id=al.id, year=2026,
                                  total=14, used=4, pending=0, remaining=10))
        CompanyMembership.query.filter_by(company_id=world["company"],
                                          user_id=world["user"]).one().date_of_leaving = date(2026, 3, 15)
        db.session.commit()
    p = preview(world, 3, 2026)
    assert p["settlement"] == {"Gratuity": BASIC * 26, "Leave encashment": BASIC / 30 * 10}
    assert p["basic"] == pytest.approx(BASIC * 15 / 31, abs=0.01)
    assert preview(world, 2, 2026)["settlement"] == {}
    r = c.post("/compensation/run-payroll", data={"month": 3, "year": 2026})
    assert r.status_code in (302, 303)
    from shared.models.ledger import ChartOfAccount, JournalEntry, JournalLine
    from hr_app.models.compensation import PayrollRun
    with Ctx(world):
        run = PayrollRun.query.filter_by(month=3, year=2026).one()
        rows = (db.session.query(ChartOfAccount.name, JournalLine.debit, JournalLine.credit)
                .join(JournalLine, JournalLine.account_id == ChartOfAccount.id)
                .join(JournalEntry, JournalLine.journal_entry_id == JournalEntry.id)
                .filter(JournalEntry.voucher_type == "PRL", JournalEntry.voucher_id == run.id,
                        JournalEntry.is_posted == True).all())  # noqa: E712
        gross = float(run.total_gross)
    dr = {}
    for name, d, cr in rows:
        dr[name] = dr.get(name, 0) + float(d or 0) - float(cr or 0)
    assert dr["Gratuity Expense"] == pytest.approx(BASIC * 26)
    assert dr["Leave Encashment Expense"] == pytest.approx(50_000)
    assert dr["Salary Expense"] == pytest.approx(gross - BASIC * 26 - 50_000, abs=0.01)
    assert sum(dr.values()) == pytest.approx(0, abs=0.01)
    from shared import integrity
    with Ctx(world):
        assert not integrity.failures()
        CompanyMembership.query.filter_by(company_id=world["company"],
                                          user_id=world["user"]).one().date_of_leaving = None
        db.session.commit()
