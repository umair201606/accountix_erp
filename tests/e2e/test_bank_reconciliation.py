"""Bank reconciliation: cash book matched to the statement, through the routes.

July cash book (Main Cash):
    01 Jul  receipt 100,000    on the statement
    05 Jul  payment  30,000    on the statement
    10 Jul  receipt  20,000    deposit in transit
    12 Jul  payment   5,000    cheque not yet presented
Statement 31 Jul closing balance 70,000.

Statement: books 85,000 + unpresented 5,000 - in transit 20,000 = 70,000.
"""
import os
import tempfile
from datetime import date

import pytest

_TMP_DB = os.path.join(tempfile.gettempdir(), "erp_bank_rec.db")
if os.path.exists(_TMP_DB):
    os.remove(_TMP_DB)
os.environ["DATABASE_URL"] = (os.environ.get("TEST_DATABASE_URL")
                              or "sqlite:///" + _TMP_DB.replace("\\", "/"))

from app import app as flask_app  # noqa: E402
from shared.extensions import db  # noqa: E402


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
    with flask_app.app_context():
        user = (User.query.filter_by(is_super_admin=True).order_by(User.id).first()
                or User.query.order_by(User.id).first())
        uid = user.id
        with unscoped():
            comp = Company(name="Bank Rec Co", slug="bank-rec-e2e", is_active=True, created_by=uid)
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
        cash = ChartOfAccount.query.filter_by(code="1-01-01-01-0001").one().id
        rev = (ChartOfAccount.query.filter(ChartOfAccount.type == "revenue",
                                           ChartOfAccount.level >= 5).first().id)
        exp = (ChartOfAccount.query.filter(ChartOfAccount.type == "expense",
                                           ChartOfAccount.level >= 5).first().id)
    with c.session_transaction() as sess:
        sess["_user_id"] = str(uid)
        sess["_fresh"] = True
        sess["company_id"] = cid
    w = {"client": c, "company": cid, "cash": cash, "rev": rev, "exp": exp}
    for day, amount, receipt in ((1, 100_000, True), (5, 30_000, False),
                                 (10, 20_000, True), (12, 5_000, False)):
        dr, cr = (cash, rev) if receipt else (exp, cash)
        r = c.post("/accounting/vouchers", data={
            "voucher_type": "JV", "voucher_date": f"2025-07-{day:02d}T10:00",
            "account_id[]": [str(dr), str(cr)], "description[]": [f"July {day}", f"July {day}"],
            "debit[]": [str(amount), "0"], "credit[]": ["0", str(amount)], "action": "approve"})
        assert r.status_code in (200, 302)
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


def _rec(w, rid):
    from shared.models.bank_rec import BankReconciliation
    from finance_app.routes.bank_rec import reconcile
    with Ctx(w):
        rec = db.session.get(BankReconciliation, rid)
        d = reconcile(rec)
        return rec.status, d, [(ln.id, float(ln.debit or 0) - float(ln.credit or 0))
                               for ln, _ in d["rows"]]


def test_july_reconciliation(world):
    c = world["client"]
    r = c.post("/accounting/bank-reconciliation/new", data={
        "account_id": world["cash"], "statement_date": "2025-07-31",
        "statement_balance": "70000"})
    assert r.status_code == 302
    rid = int(r.headers["Location"].rstrip("/").split("/")[-1])
    world["july"] = rid
    status, d, rows = _rec(world, rid)
    assert status == "draft" and len(rows) == 4 and d["book"] == 85_000
    on_statement = [lid for lid, net in rows if net in (100_000, -30_000)]
    # One line short: completing is refused and the difference shown.
    page = c.post(f"/accounting/bank-reconciliation/{rid}/save", data={
        "line_ids[]": on_statement[:1], "action": "complete"},
        follow_redirects=True).get_data(as_text=True)
    assert "Not complete" in page
    assert _rec(world, rid)[0] == "draft"
    c.post(f"/accounting/bank-reconciliation/{rid}/save", data={
        "line_ids[]": on_statement, "action": "complete"})
    status, d, _ = _rec(world, rid)
    assert status == "completed" and d["difference"] == 0
    assert d["unpresented"] == 5_000 and d["in_transit"] == 20_000
    assert d["bank_per_books"] == d["statement"] == 70_000
    x = c.get(f"/accounting/bank-reconciliation/{rid}?format=excel")
    assert x.status_code == 200 and x.data[:2] == b"PK"


def test_next_statement_carries_only_what_is_still_open(world):
    c = world["client"]
    r = c.post("/accounting/bank-reconciliation/new", data={
        "account_id": world["cash"], "statement_date": "2025-07-20",
        "statement_balance": "0"}, follow_redirects=True)
    assert "the next statement must be later" in r.get_data(as_text=True)
    r = c.post("/accounting/bank-reconciliation/new", data={
        "account_id": world["cash"], "statement_date": "2025-08-31",
        "statement_balance": "85000"})
    aid = int(r.headers["Location"].rstrip("/").split("/")[-1])
    status, d, rows = _rec(world, aid)
    assert sorted(net for _, net in rows) == [-5_000, 20_000]
    assert d["cleared_before"] == 70_000
    r = c.post("/accounting/bank-reconciliation/new", data={
        "account_id": world["cash"], "statement_date": "2025-09-30",
        "statement_balance": "0"}, follow_redirects=True)
    assert "already has a reconciliation in progress" in r.get_data(as_text=True)
    c.post(f"/accounting/bank-reconciliation/{aid}/save", data={
        "line_ids[]": [lid for lid, _ in rows], "action": "complete"})
    assert _rec(world, aid)[0] == "completed"
    # Only the latest completed reconciliation reopens.
    r = c.post(f"/accounting/bank-reconciliation/{world['july']}/reopen", follow_redirects=True)
    assert "Only the latest" in r.get_data(as_text=True)
    page = c.get("/accounting/bank-reconciliation").get_data(as_text=True)
    assert "Bank reconciliation" in page and "completed" in page
