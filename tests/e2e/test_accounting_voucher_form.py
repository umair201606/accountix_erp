"""Accounting voucher form: only a literal "approve" action approves.

The form route used to treat ANY action value other than "save" as an
approval. A crafted POST with ``action=save_approve`` therefore flipped the
voucher to approved WITHOUT posting the journal — an approved voucher that
had never moved the books, with no approved_by/approved_at on record. These
tests pin the contract: approval is the single ``approve`` action, and it is
the only path that reaches the ledger.
"""
import os
import tempfile
from datetime import datetime, timedelta

import pytest

# Point at a throwaway DB before app import — importing app builds the engine.
_TMP_DB = os.path.join(tempfile.gettempdir(), "erp_voucher_form.db")
if os.path.exists(_TMP_DB):
    os.remove(_TMP_DB)
os.environ["DATABASE_URL"] = "sqlite:///" + _TMP_DB.replace("\\", "/")

from app import app as flask_app  # noqa: E402
from shared.extensions import db  # noqa: E402


@pytest.fixture(scope="module")
def client():
    c = flask_app.test_client()
    c.get("/")  # lazy create_all + migrate + seed
    from hr_app.models.user import User
    with flask_app.app_context():
        user = (User.query.filter(User.email.ilike("admin%")).first()
                or User.query.first())
        assert user is not None, "seed produced no users"
        uid = user.id
    with c.session_transaction() as sess:
        sess["_user_id"] = str(uid)
        sess["_fresh"] = True
    yield c


def _company_ctx():
    """App context with the first company active, for tenant-scoped reads."""
    from contextlib import contextmanager
    from shared.models.company import Company
    from shared.tenancy import set_current_company, unscoped

    @contextmanager
    def _ctx():
        with flask_app.app_context():
            with unscoped():
                company = Company.query.order_by(Company.id).first()
            set_current_company(company.id)
            yield

    return _ctx()


def _line_accounts():
    """A receivable account and a cash account from the seeded chart."""
    from shared.models.ledger import ChartOfAccount
    with _company_ctx():
        debtor = ChartOfAccount.query.filter_by(
            name="Trade Debtors — General").first()
        cash = ChartOfAccount.query.filter_by(name="Main Cash").first()
    assert debtor is not None and cash is not None
    return debtor, cash


def _post_jv(client, action):
    debtor, cash = _line_accounts()
    return client.post(
        "/accounting/vouchers",
        data={
            "voucher_type": "JV",
            "voucher_date": datetime.utcnow().strftime("%Y-%m-%dT%H:%M"),
            "notes": "voucher form action test",
            "account_id[]": [str(debtor.id), str(cash.id)],
            "description[]": ["receivable leg", "cash leg"],
            "debit[]": ["5000", "0"],
            "credit[]": ["0", "5000"],
            "action": action,
        },
        follow_redirects=True)


def _latest_voucher():
    from shared.models.accounting_voucher import AccountingVoucher
    with _company_ctx():
        return (AccountingVoucher.query
                .filter_by(voucher_type="JV")
                .order_by(AccountingVoucher.id.desc()).first())


def _journal_for(voucher):
    from shared.models.ledger import JournalEntry
    with _company_ctx():
        return (JournalEntry.query
                .filter_by(voucher_type=voucher.voucher_type,
                           voucher_id=voucher.id).first())


def test_unknown_action_saves_unapproved_and_posts_nothing(client):
    """The reported bug: action=save_approve used to silently approve."""
    resp = _post_jv(client, "save_approve")
    assert resp.status_code == 200
    voucher = _latest_voucher()
    assert voucher is not None
    assert voucher.status != "approved"
    assert voucher.approved_by is None
    assert voucher.approved_at is None
    assert _journal_for(voucher) is None, (
        "an unapproved voucher must not have reached the ledger")


def test_save_action_keeps_the_voucher_unapproved(client):
    resp = _post_jv(client, "save")
    assert resp.status_code == 200
    voucher = _latest_voucher()
    assert voucher.status == "unapproved"
    assert _journal_for(voucher) is None


def test_approve_action_is_the_only_path_to_the_ledger(client):
    resp = _post_jv(client, "approve")
    assert resp.status_code == 200
    voucher = _latest_voucher()
    assert voucher.status == "approved"
    assert voucher.approved_by is not None
    assert voucher.approved_at is not None
    entry = _journal_for(voucher)
    assert entry is not None and entry.is_posted
    assert entry.entry_date.date() == datetime.utcnow().date()


def test_refused_approval_keeps_the_draft(client):
    """Approving into a closed period is refused — and the voucher the user
    typed must survive as an unapproved draft. The refusal used to render
    the form without a commit, so teardown rolled the whole voucher away."""
    from datetime import date
    from shared.models.company_settings import AccountingPeriod
    with _company_ctx():
        db.session.add(AccountingPeriod(
            fiscal_year="2001", period_name="FY 2001 (closed)",
            start_date=date(2001, 1, 1), end_date=date(2001, 12, 31),
            is_open=False, is_closed=True, closed_at=datetime(2002, 1, 15)))
        db.session.commit()
    debtor, cash = _line_accounts()
    resp = client.post(
        "/accounting/vouchers",
        data={
            "voucher_type": "JV",
            "voucher_date": "2001-06-01T10:00",
            "notes": "closed period draft",
            "account_id[]": [str(debtor.id), str(cash.id)],
            "description[]": ["receivable leg", "cash leg"],
            "debit[]": ["700", "0"],
            "credit[]": ["0", "700"],
            "action": "approve",
        },
        follow_redirects=True)
    assert resp.status_code == 200
    assert b"could not approve" in resp.data
    voucher = _latest_voucher()
    assert voucher.notes == "closed period draft", "the draft was lost"
    assert voucher.status == "unapproved"
    assert voucher.approved_by is None
    assert _journal_for(voucher) is None
    from shared.models.accounting_voucher import AccountingVoucherLine
    with _company_ctx():
        assert AccountingVoucherLine.query.filter_by(
            voucher_id=voucher.id).count() == 2


def test_negative_or_two_sided_lines_are_refused(client):
    """A negative amount printed as a negative debit in every ledger and, on
    a cash/bank voucher, shrank the money line; a JV line could carry both
    a debit and a credit."""
    debtor, cash = _line_accounts()
    before = _latest_voucher()
    for debits, credits, message in ((["-5000", "0"], ["0", "-5000"], "cannot be negative"),
                                     (["5000", "100"], ["100", "5000"], "not both")):
        resp = client.post("/accounting/vouchers", data={
            "voucher_type": "JV",
            "voucher_date": datetime.utcnow().strftime("%Y-%m-%dT%H:%M"),
            "account_id[]": [str(debtor.id), str(cash.id)],
            "description[]": ["a", "b"], "debit[]": debits, "credit[]": credits,
            "action": "approve"}, follow_redirects=True)
        assert message in resp.get_data(as_text=True)
    after = _latest_voucher()
    assert (after.id if after else None) == (before.id if before else None)


def test_closing_a_period_lists_what_is_still_open(client):
    """A period closed over a draft voucher left it impossible to post; the
    close now lists open items and asks again."""
    from datetime import date
    from shared.models.company_settings import AccountingPeriod
    debtor, cash = _line_accounts()
    with _company_ctx():
        p = AccountingPeriod(fiscal_year="2003", period_name="FY 2003",
                             start_date=date(2003, 1, 1), end_date=date(2003, 12, 31),
                             is_open=True, is_closed=False)
        db.session.add(p)
        db.session.commit()
        pid = p.id
    client.post("/accounting/vouchers", data={
        "voucher_type": "JV", "voucher_date": "2003-05-01T10:00", "notes": "draft in 2003",
        "account_id[]": [str(debtor.id), str(cash.id)], "description[]": ["a", "b"],
        "debit[]": ["300", "0"], "credit[]": ["0", "300"], "action": "save"})
    page = client.post(f"/settings/periods/{pid}/close", follow_redirects=True).get_data(as_text=True)
    assert "Still open in FY 2003" in page and "unapproved accounting voucher" in page
    with _company_ctx():
        assert not db.session.get(AccountingPeriod, pid).is_closed
    client.post(f"/settings/periods/{pid}/close", data={"force": "1"})
    with _company_ctx():
        assert db.session.get(AccountingPeriod, pid).is_closed
