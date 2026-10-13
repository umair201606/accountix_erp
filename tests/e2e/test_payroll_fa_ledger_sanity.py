"""Sanity check: payroll and fixed assets, every flow, proven in the general ledger.

One fresh company, driven only through the real routes, in the order an
accountant would live it. After each step the exact accounts it should move
are asserted, and Books Integrity (trial balance, every journal balanced,
asset register = GL, payroll runs = salary journals, staff loans = loan
accounts, PF members' ledger = PF Payable) must pass.

Employee: basic 200,000 + house rent 40% (taxable) + medical 10% (exempt).
"""
import io
import json
import os
import tempfile
from datetime import date

import pytest

_TMP_DB = os.path.join(tempfile.gettempdir(), "erp_payroll_fa_sanity.db")
if os.path.exists(_TMP_DB):
    os.remove(_TMP_DB)
os.environ["DATABASE_URL"] = (os.environ.get("TEST_DATABASE_URL")
                              or "sqlite:///" + _TMP_DB.replace("\\", "/"))

from app import app as flask_app  # noqa: E402
from shared.extensions import db  # noqa: E402

BASIC = 200_000
CASH = "1-01-01-01-0001"


@pytest.fixture(scope="module")
def world():
    c = flask_app.test_client()
    c.get("/")
    from hr_app.models.user import User
    from shared.models.company import Company, CompanyMembership
    from shared.models.base import Role
    from shared.models.ledger import ChartOfAccount
    from shared.tenancy import set_current_company, unscoped
    from shared.company_setup import provision_company
    from fixed_assets_app.models.asset import AssetCategory
    from hr_app.models.compensation import PayrollProfile

    with flask_app.app_context():
        user = (User.query.filter_by(is_super_admin=True).order_by(User.id).first()
                or User.query.order_by(User.id).first())
        uid = user.id
        user.date_of_joining = date(2020, 1, 1)
        with unscoped():
            comp = Company(name="Sanity Co", slug="sanity-co-e2e", is_active=True, created_by=uid)
            db.session.add(comp)
            db.session.flush()
            db.session.add(CompanyMembership(
                company_id=comp.id, user_id=uid, employee_code="SAN-001",
                role_id=Role.query.filter_by(name=Role.ADMIN).first().id,
                status=CompanyMembership.ACTIVE))
            db.session.commit()
            cid = comp.id
        set_current_company(cid)
        provision_company(cid)
        set_current_company(cid)
        if not AssetCategory.query.first():
            AssetCategory.seed()
        db.session.add(PayrollProfile(user_id=uid, basic_salary=BASIC,
                                      effective_from=date(2020, 1, 1)))
        db.session.commit()
        cash = ChartOfAccount.query.filter_by(code=CASH).one().id
        cat = AssetCategory.query.order_by(AssetCategory.id).first().id
    with c.session_transaction() as sess:
        sess["_user_id"] = str(uid)
        sess["_fresh"] = True
        sess["company_id"] = cid
    return {"client": c, "company": cid, "user": uid, "cash": cash, "cat": cat}


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


def books_tie(w, step):
    from shared import integrity
    with Ctx(w):
        bad = integrity.failures()
    assert not bad, f"after {step}: " + "; ".join(
        f"{b['label']}: expected {b['expected']} got {b['actual']} ({b['detail']})" for b in bad)


def balance(w, name=None, code=None, account_id=None):
    """Net debit balance of one account (posted lines only)."""
    from shared.models.ledger import ChartOfAccount, JournalEntry, JournalLine
    with Ctx(w):
        if account_id is None:
            q = ChartOfAccount.query
            acct = q.filter_by(code=code).first() if code else q.filter_by(name=name).first()
            if acct is None:
                return 0.0
            account_id = acct.id
        dr, cr = (db.session.query(db.func.coalesce(db.func.sum(JournalLine.debit), 0),
                                   db.func.coalesce(db.func.sum(JournalLine.credit), 0))
                  .join(JournalEntry, JournalLine.journal_entry_id == JournalEntry.id)
                  .filter(JournalEntry.is_posted == True,  # noqa: E712
                          JournalLine.account_id == account_id).one())
        return round(float(dr) - float(cr), 2)


def voucher_lines(w, vtype, vid):
    """{account name: net debit} for a voucher's posted journals."""
    from shared.models.ledger import ChartOfAccount, JournalEntry, JournalLine
    with Ctx(w):
        rows = (db.session.query(ChartOfAccount.name, JournalLine.debit, JournalLine.credit)
                .join(JournalLine, JournalLine.account_id == ChartOfAccount.id)
                .join(JournalEntry, JournalLine.journal_entry_id == JournalEntry.id)
                .filter(JournalEntry.voucher_type == vtype, JournalEntry.voucher_id == vid,
                        JournalEntry.is_posted == True).all())  # noqa: E712
    out = {}
    for name, d, c in rows:
        out[name] = round(out.get(name, 0) + float(d or 0) - float(c or 0), 2)
    return out


def run_of(w, month, year):
    from hr_app.models.compensation import PayrollRun
    with Ctx(w):
        r = PayrollRun.query.filter_by(month=month, year=year).first()
        return None if r is None else {"id": r.id, "net": float(r.total_net),
                                       "gross": float(r.total_gross), "paid": bool(r.paid_at)}


def flashed(resp):
    return resp.get_data(as_text=True)


# ── Payroll ────────────────────────────────────────────────────────────────

def test_p01_settings_and_salary_package(world):
    c = world["client"]
    c.post("/compensation/tax-settings", data={"action": "seed_defaults"})
    c.post("/compensation/tax-settings", data={
        "action": "policy", "overtime_multiplier": "1.5", "overtime_hours_basis": "160",
        "eobi_enabled": "1", "eobi_wage_base": "37000", "eobi_employee_pct": "1",
        "eobi_employer_pct": "5", "surcharge_threshold": "10000000", "surcharge_pct": "9"})
    c.post("/pf/config", data={"employee_pct": "5", "employer_pct": "5",
                               "contribution_base": "basic", "max_loan_pct": "50",
                               "interest_rate": "0", "min_months": "0"})
    r = c.post(f"/compensation/edit-profile/{world['user']}", data={
        "basic_salary": BASIC, "effective_from": "2020-01-01",
        "components_json": json.dumps([
            {"name": "House Rent", "type": "allowance", "method": "percent_of_basic",
             "value": 40, "is_taxable": True},
            {"name": "Medical", "type": "allowance", "method": "percent_of_basic",
             "value": 10, "is_taxable": False}])})
    assert r.status_code in (302, 303)
    books_tie(world, "setup")


def test_p02_staff_loan_is_paid_out_before_it_is_recovered(world):
    """Approval alone used to start deductions on money never handed over,
    and the recovery credited a receivable nothing had debited."""
    from hr_app.models.loan import LoanAdvanceRequest
    c = world["client"]
    with Ctx(world):
        loan = LoanAdvanceRequest(user_id=world["user"], request_type="loan", amount=120_000,
                                  purpose="Car", installment_months=12,
                                  monthly_installment=10_000, remaining_amount=120_000,
                                  status="pending")
        db.session.add(loan)
        db.session.commit()
        lid = loan.id
    c.post(f"/mss/approve-loan/{lid}", data={"action": "approve"})
    preview = c.post("/compensation/payroll-preview-json",
                     data={"month": 7, "year": 2025}).get_json()["employees"][0]
    assert preview["loan_deduction"] == 0, "deducted before the loan was paid out"
    page = c.get("/compensation/loans").get_data(as_text=True)
    assert "Pay out" in page
    r = c.post(f"/compensation/loans/{lid}/disburse",
               data={"account_id": world["cash"], "paid_on": "2025-07-05"})
    assert r.status_code in (302, 303)
    lines = voucher_lines(world, "LOAN-DSB", lid)
    assert lines.get("Main Cash") == -120_000
    assert sum(lines.values()) == 0 and len(lines) == 2
    world["loan"] = lid
    books_tie(world, "loan paid out")


def test_p03_payroll_run_journal_account_by_account(world):
    c = world["client"]
    p = c.post("/compensation/payroll-preview-json",
               data={"month": 7, "year": 2025}).get_json()["employees"][0]
    assert p["gross"] == BASIC * 1.5
    assert p["taxable"] == BASIC * 1.4
    assert p["pf_employee"] == BASIC * 0.05 and p["eobi_employee"] == 370
    assert p["loan_deduction"] == 10_000
    r = c.post("/compensation/run-payroll", data={"month": 7, "year": 2025})
    assert r.status_code in (302, 303) and "/compensation/run/" in r.headers["Location"]
    run = run_of(world, 7, 2025)
    lines = voucher_lines(world, "PRL", run["id"])
    assert lines["Salary Expense"] == run["gross"]
    assert lines["Salary Payable"] == -run["net"]
    assert lines["Income Tax Payable"] == -p["monthly_tax"]
    assert lines["PF Payable"] == -(BASIC * 0.10)          # employee 5% + employer 5%
    assert lines["PF Employer Expense"] == BASIC * 0.05
    assert lines["EOBI Payable"] == -(370 + 1850)
    assert lines["EOBI Employer Expense"] == 1850
    loan_line = [v for k, v in lines.items() if k.startswith("Loan #")]
    assert loan_line == [-10_000]
    assert round(sum(lines.values()), 2) == 0
    world["july"] = run["id"]
    books_tie(world, "July payroll")


def test_p04_salary_payment_clears_salary_payable(world):
    c = world["client"]
    rid = world["july"]
    run = run_of(world, 7, 2025)
    page = c.get(f"/compensation/run/{rid}").get_data(as_text=True)
    assert "Record payment" in page and "SAN-001" in page
    r = c.post(f"/compensation/run/{rid}/pay", data={"account_id": world["cash"],
                                                    "paid_on": "2025-08-01"})
    assert r.status_code in (302, 303)
    assert voucher_lines(world, "PRL-PAY", rid) == {"Salary Payable": run["net"],
                                                     "Main Cash": -run["net"]}
    assert balance(world, name="Salary Payable") == 0
    assert run_of(world, 7, 2025)["paid"]
    # A paid run cannot be replaced or deleted until the payment is undone.
    r = c.post(f"/compensation/run/{rid}/delete", headers={"Referer": f"/compensation/run/{rid}"},
               follow_redirects=True)
    assert "Undo the salary payment" in flashed(r)
    r = c.post("/compensation/run-payroll", data={"month": 7, "year": 2025, "replace_all": "1"},
               follow_redirects=True)
    assert "already paid" in flashed(r)
    assert run_of(world, 7, 2025)["id"] == rid
    books_tie(world, "salaries paid")


def test_p05_bank_sheet_and_slip_agree_with_the_ledger(world):
    import openpyxl
    c = world["client"]
    rid = world["july"]
    x = c.get(f"/compensation/export-bank-ledger/{rid}")
    ws = openpyxl.load_workbook(io.BytesIO(x.data)).active
    rows = list(ws.iter_rows(values_only=True))
    assert rows[1][0] == "SAN-001"
    assert rows[-1][4] == "Total" and rows[-1][5] == pytest.approx(run_of(world, 7, 2025)["net"])
    from hr_app.models.compensation import PayrollSlip
    with Ctx(world):
        sid = PayrollSlip.query.filter_by(payroll_run_id=rid).one().id
    assert "SAN-001" in c.get(f"/compensation/slip/{sid}").get_data(as_text=True)
    assert c.get(f"/compensation/slip/{sid}/pdf").data[:4] == b"%PDF"


def test_p06_pf_loan_paid_from_the_fund_and_recovered_by_payroll(world):
    from hr_app.models.pf import PFLoanRequest
    c = world["client"]
    with Ctx(world):
        pfl = PFLoanRequest(user_id=world["user"], amount=15_000, installment_months=5,
                            purpose="Fees", monthly_installment=3_000, remaining_amount=15_000,
                            status="pending")
        db.session.add(pfl)
        db.session.commit()
        pid = pfl.id
    r = c.post(f"/pf/approve-loan/{pid}", follow_redirects=True)
    assert "Choose the bank or cash account" in flashed(r)
    c.post(f"/pf/approve-loan/{pid}", data={"account_id": world["cash"], "paid_on": "2025-08-10"})
    assert voucher_lines(world, "PF-LOAN", pid) == {"PF Payable": 15_000, "Main Cash": -15_000}
    books_tie(world, "PF loan paid out")
    c.post("/compensation/run-payroll", data={"month": 8, "year": 2025})
    run = run_of(world, 8, 2025)
    p = voucher_lines(world, "PRL", run["id"])
    assert p["PF Payable"] == -(BASIC * 0.10 + 3_000)
    with Ctx(world):
        assert float(db.session.get(PFLoanRequest, pid).remaining_amount) == 12_000
    world["pfloan"], world["august"] = pid, run["id"]
    books_tie(world, "August payroll with PF loan recovery")


def test_p07_deleting_a_run_takes_everything_back_out(world):
    from hr_app.models.loan import LoanAdvanceRequest
    from hr_app.models.pf import PFLoanRequest, PFLedger
    c = world["client"]
    rid = world["august"]
    r = c.post(f"/compensation/run/{rid}/delete")
    assert r.status_code in (302, 303)
    assert run_of(world, 8, 2025) is None
    assert voucher_lines(world, "PRL", rid) == {}
    with Ctx(world):
        assert float(db.session.get(LoanAdvanceRequest, world["loan"]).remaining_amount) == 110_000
        assert float(db.session.get(PFLoanRequest, world["pfloan"]).remaining_amount) == 15_000
        assert PFLedger.query.filter_by(payroll_run_id=rid).count() == 0
    books_tie(world, "August run deleted")
    c.post("/compensation/run-payroll", data={"month": 8, "year": 2025})
    books_tie(world, "August re-run")


def test_p08_raise_mid_month_is_split_and_old_months_keep_the_old_rate(world):
    """MSS evaluation crashed for anyone with a salary package; raises had no
    date, so re-running an earlier month paid it at today's rate."""
    c = world["client"]
    r = c.post(f"/mss/evaluate/{world['user']}", data={
        "new_basic": "260000", "reason": "Promotion", "effective_from": "2025-09-16"})
    assert r.status_code in (302, 303)
    sep = c.post("/compensation/payroll-preview-json",
                 data={"month": 9, "year": 2025}).get_json()["employees"][0]
    assert sep["basic"] == pytest.approx((BASIC * 15 + 260_000 * 15) / 30, abs=0.01)
    jul = c.post("/compensation/payroll-preview-json",
                 data={"month": 7, "year": 2025}).get_json()["employees"][0]
    assert jul["basic"] == BASIC
    oct_ = c.post("/compensation/payroll-preview-json",
                  data={"month": 10, "year": 2025}).get_json()["employees"][0]
    assert oct_["basic"] == 260_000
    assert oct_["allowances_breakdown"]["House Rent"] == 104_000   # 40% follows the raise


def test_p09_pf_withdrawal_profit_and_settlement_move_pf_payable(world):
    from hr_app.models.pf import PFWithdrawalRequest
    c = world["client"]
    c.post("/pf/request-withdrawal", data={"amount": "5000", "reason": "Medical"})
    with Ctx(world):
        wid = PFWithdrawalRequest.query.order_by(PFWithdrawalRequest.id.desc()).first().id
    c.post(f"/pf/approve-withdrawal/{wid}", data={"account_id": world["cash"],
                                                 "paid_on": "2025-09-05"})
    assert voucher_lines(world, "PF-WD", wid) == {"PF Payable": 5_000, "Main Cash": -5_000}
    c.post("/pf/distribute-profit", data={"month": 9, "year": 2025, "total_profit": "2500"})
    assert balance(world, name="PF Profit Credited to Members") == 2_500
    books_tie(world, "PF withdrawal and profit")
    r = c.post(f"/pf/settle/{world['user']}", data={"account_id": world["cash"],
                                                   "paid_on": "2025-09-30"})
    assert r.status_code in (302, 303)
    assert balance(world, name="PF Payable") == 0           # the fund is fully paid out
    books_tie(world, "PF settled")


# ── Fixed assets ───────────────────────────────────────────────────────────

def _expense_account(w):
    from shared.models.ledger import ChartOfAccount
    with Ctx(w):
        return ChartOfAccount.query.filter(ChartOfAccount.type == "expense",
                                           ChartOfAccount.level >= 5).first().id


def test_f01_asset_accounts_must_be_the_right_kind(world):
    c = world["client"]
    r = c.post("/fixed-assets/assets/create", data={
        "name": "Wrong Account Van", "category_id": world["cat"], "purchase_date": "2025-07-01",
        "purchase_cost": "600000", "useful_life": "5", "salvage_value": "0",
        "depreciation_method": "straight_line",
        "fixed_asset_account_id": _expense_account(world)},
        headers={"Referer": "/fixed-assets/assets/create"}, follow_redirects=True)
    assert "cannot be used here" in flashed(r)
    page = c.get("/fixed-assets/assets/create").get_data(as_text=True)
    assert "Main Cash" in page          # offered as the credit (paid from) account


def test_f02_acquisition_depreciation_impairment_disposal(world):
    from fixed_assets_app.models.asset import FixedAsset
    c = world["client"]
    c.post("/fixed-assets/assets/create", data={
        "name": "Delivery Truck", "category_id": world["cat"], "purchase_date": "2025-07-01",
        "purchase_cost": "600000", "useful_life": "5", "salvage_value": "0",
        "depreciation_method": "straight_line", "acquisition_credit_account_id": world["cash"]})
    with Ctx(world):
        a = FixedAsset.query.filter_by(name="Delivery Truck").one()
        aid = a.id
    assert voucher_lines(world, "FA-ACQ", aid)["Main Cash"] == -600_000
    c.post("/fixed-assets/depreciation/post", data={"asset_ids[]": [aid],
                                                     "entry_date": "2025-10-01"})
    assert balance(world, name="Depreciation Expense") == 30_000      # 3 months x 10,000
    c.post(f"/fixed-assets/assets/{aid}/depreciate", data={
        "entry_date": "2025-10-15", "amount": "50000", "kind": "impairment",
        "notes": "Accident damage"})
    assert balance(world, name="Impairment Loss on Fixed Assets") == 50_000
    books_tie(world, "acquisition, depreciation, impairment")
    r = c.post(f"/fixed-assets/assets/{aid}/dispose", data={
        "disposal_date": "2025-10-31", "proceeds": "480000",
        "proceeds_account_id": _expense_account(world)},
        headers={"Referer": f"/fixed-assets/assets/{aid}"}, follow_redirects=True)
    assert "cannot be used here" in flashed(r)
    c.post(f"/fixed-assets/assets/{aid}/dispose", data={
        "disposal_date": "2025-10-31", "proceeds": "480000",
        "proceeds_account_id": world["cash"]})
    # NBV 600,000 - 30,000 - 50,000 = 520,000; sold for 480,000: loss 40,000.
    assert balance(world, name="Loss/(Gain) on Disposal of Fixed Assets") == 40_000
    world["truck"] = aid
    books_tie(world, "disposal")


def test_f03_dashboard_register_movement_and_ledger_panel(world):
    from fixed_assets_app.movement import movement
    c = world["client"]
    dash = c.get("/fixed-assets/dashboard").get_data(as_text=True)
    assert "Disposed" in dash
    with Ctx(world):
        m = movement(date(2025, 7, 1), date(2025, 12, 31))
    t = m["total"]
    assert t["additions"] == 600_000 and t["cost_disposals"] == 600_000
    assert t["charge"] == 80_000 and t["dep_disposals"] == 80_000
    assert t["nbv_close"] == 0
    aid = world["truck"]
    panel = c.get(f"/accounting/gl-entry?type=FA-ACQ&id={aid}&also=FA-DISP:{aid}").get_json()
    numbers = {e["number"] for e in panel["posted"]}
    assert any(n.startswith("FA-DISP") for n in numbers), numbers
    view = c.get(f"/fixed-assets/assets/{aid}").get_data(as_text=True)
    assert f"FA-DISP:{aid}" in view
