"""Accounting period locking: a closed period's figures are final.

The rule these protect: a cost that has been computed, posted and conveyed to
someone must stay put. Closing the period is what makes that permanent.
"""

from datetime import date, datetime, timedelta

import pytest

from shared.extensions import db
from shared.periods import (ClosedPeriodError, require_open_period, period_for,
                            is_closed, current_open_period)
from shared.models.company_settings import AccountingPeriod, FiscalYearRule
from shared.models.ledger import JournalEntry
from shared.ledger_utils import post_journal_entry, reverse_journal_entry


TODAY = date.today()


@pytest.fixture
def accounts(app):
    """Two postable level-5 accounts to move value between."""
    from shared.models.ledger import ChartOfAccount
    a = ChartOfAccount(code="50001", name="Scrap Loss", type="expense",
                       level=5, is_active=True)
    b = ChartOfAccount(code="12001", name="Stock", type="asset",
                       level=5, is_active=True)
    db.session.add_all([a, b])
    db.session.commit()
    return a, b


@pytest.fixture
def open_period(app):
    rule = FiscalYearRule.get()
    rule.start_month, rule.start_day = 1, 1
    rule.generate_periods()
    p = AccountingPeriod.query.filter(
        AccountingPeriod.start_date <= TODAY,
        AccountingPeriod.end_date >= TODAY
    ).first()
    return p


def _post(accounts, when=None, number="SCRAP-00001"):
    a, b = accounts
    return post_journal_entry(
        voucher_type="SCRAP", voucher_id=1, voucher_number=number,
        description="Damaged unit charged to employee",
        lines=[{"account_id": a.id, "debit": 100, "credit": 0},
               {"account_id": b.id, "debit": 0, "credit": 100}],
        entry_date=when or datetime.utcnow(), created_by=1)


# ─────────────────────────────────────────────
# The lock
# ─────────────────────────────────────────────

def test_posting_into_an_open_period_is_allowed(accounts, open_period):
    _post(accounts)
    assert JournalEntry.query.count() == 1


def test_posting_into_a_closed_period_is_refused(accounts, open_period):
    open_period.is_open, open_period.is_closed = False, True
    open_period.closed_at = datetime.utcnow()
    db.session.commit()

    with pytest.raises(ClosedPeriodError, match="figures are final"):
        _post(accounts)
    assert JournalEntry.query.count() == 0, "the refused entry must not persist"


def test_unposting_from_a_closed_period_is_refused(accounts, open_period):
    _post(accounts)
    open_period.is_open, open_period.is_closed = False, True
    open_period.closed_at = datetime.utcnow()
    db.session.commit()

    with pytest.raises(ClosedPeriodError, match="un-post"):
        reverse_journal_entry("SCRAP", 1)

    assert JournalEntry.query.filter_by(is_posted=True).count() == 1, \
        "the entry must stay posted in the closed period"


def test_unposting_from_an_open_period_still_works(accounts, open_period):
    _post(accounts)
    reverse_journal_entry("SCRAP", 1)
    db.session.commit()
    assert JournalEntry.query.filter_by(is_posted=True).count() == 0


def test_reopening_a_period_restores_posting(accounts, open_period):
    open_period.is_open, open_period.is_closed = False, True
    db.session.commit()
    with pytest.raises(ClosedPeriodError):
        _post(accounts)

    open_period.is_open, open_period.is_closed = True, False
    open_period.closed_at = None
    db.session.commit()

    _post(accounts)
    assert JournalEntry.query.count() == 1


# ─────────────────────────────────────────────
# Scope of the lock
# ─────────────────────────────────────────────

def test_a_date_no_period_covers_is_allowed(accounts, open_period):
    far_future = datetime(TODAY.year + 5, 6, 1)
    assert period_for(far_future) is None
    _post(accounts, when=far_future)
    assert JournalEntry.query.count() == 1


def test_closing_one_period_does_not_lock_another(accounts, open_period):
    prior = AccountingPeriod.query.filter(
        AccountingPeriod.start_date <= date(TODAY.year - 1, 6, 1),
        AccountingPeriod.end_date >= date(TODAY.year - 1, 6, 1)
    ).first()
    prior.is_open, prior.is_closed = False, True
    prior.closed_at = datetime.utcnow()
    db.session.commit()

    with pytest.raises(ClosedPeriodError):
        _post(accounts, when=datetime(TODAY.year - 1, 6, 1))
    _post(accounts)
    assert JournalEntry.query.count() == 1


def test_the_refusal_names_the_period_and_the_way_out(accounts, open_period):
    open_period.is_open, open_period.is_closed = False, True
    open_period.closed_at = datetime(TODAY.year, 3, 4)
    db.session.commit()

    with pytest.raises(ClosedPeriodError) as exc:
        _post(accounts)
    msg = str(exc.value)
    assert open_period.period_name in msg
    assert "04 Mar" in msg
    assert "adjusting entry" in msg and "reopen" in msg


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def test_is_closed_and_current_open_period(accounts, open_period):
    assert is_closed(datetime.utcnow()) is False
    assert current_open_period() is not None

    open_period.is_open, open_period.is_closed = False, True
    db.session.commit()

    assert is_closed(datetime.utcnow()) is True
    assert current_open_period() is None


def test_require_open_period_accepts_dates_and_datetimes(accounts, open_period):
    assert require_open_period(TODAY) is open_period
    assert require_open_period(datetime.utcnow()) is open_period


def test_period_boundaries_are_inclusive(accounts, open_period):
    assert period_for(open_period.start_date) is open_period
    assert period_for(open_period.end_date) is open_period
    prior = AccountingPeriod.query.filter(
        AccountingPeriod.end_date == open_period.start_date - timedelta(days=1)
    ).first()
    if prior:
        assert period_for(open_period.start_date - timedelta(days=1)) is prior
    nxt = AccountingPeriod.query.filter(
        AccountingPeriod.start_date == open_period.end_date + timedelta(days=1)
    ).first()
    if nxt:
        assert period_for(open_period.end_date + timedelta(days=1)) is nxt
