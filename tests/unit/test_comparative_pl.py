"""Comparative P&L figures, computed against real journal movements.

The screen and both exports read the same ``comp_amounts`` array, so a wrong
aggregation here shows up in all three at once. It did: every section total in
a comparative column was the whole period's net profit, which is how "Total
Sales" came to read -100,000 with a single sales account showing 0.00 above it.
"""

from datetime import date, datetime

import pytest

from shared.extensions import db


@pytest.fixture
def coa(app):
    """Two revenue accounts and one admin expense, tagged to P&L sections."""
    from shared.models.ledger import ChartOfAccount
    accounts = {
        "sales_a": ChartOfAccount(code="4-01-01-01-0001", name="Sales — General",
                                  type="revenue", level=5, pl_section="sales"),
        "sales_b": ChartOfAccount(code="4-01-01-01-0002", name="Sales — Export",
                                  type="revenue", level=5, pl_section="sales"),
        "admin": ChartOfAccount(code="5-01-01-01-0001", name="Office Rent",
                                type="expense", level=5, pl_section="admin"),
    }
    db.session.add_all(accounts.values())
    db.session.commit()
    return accounts


def _post(account, entry_date, debit=0.0, credit=0.0):
    from shared.models.ledger import JournalEntry, JournalLine
    e = JournalEntry(voucher_type="JV", voucher_id=1, voucher_number="JV-1",
                     entry_date=datetime.combine(entry_date, datetime.min.time()),
                     created_by=1, is_posted=True)
    db.session.add(e)
    db.session.flush()
    db.session.add(JournalLine(journal_entry_id=e.id, account_id=account.id,
                               debit=debit, credit=credit))
    db.session.commit()


def test_section_totals_stay_inside_their_section(app, coa):
    """Sales and admin move in the same period; the sales total must not see
    the expense, and the admin total must not see the revenue."""
    from finance_app.routes.reports import _pl_period_lookup

    _post(coa["sales_a"], date(2026, 9, 1), credit=630000)
    _post(coa["admin"], date(2026, 9, 2), debit=100000)

    accounts, totals, subtotals = _pl_period_lookup(date(2026, 7, 1), date(2027, 6, 30))

    assert totals["sales"] == 630000.0
    assert accounts["4-01-01-01-0001"] == 630000.0
    # "Administrative Expenses" is a negate section: it displays positive.
    assert totals["admin"] == 100000.0
    assert subtotals["net_sales"] == 630000.0
    assert subtotals["net_profit"] == 530000.0


def test_a_section_total_is_the_sum_of_its_own_accounts(app, coa):
    from finance_app.routes.reports import _pl_period_lookup

    _post(coa["sales_a"], date(2026, 9, 1), credit=630000)
    _post(coa["sales_b"], date(2026, 9, 3), credit=70000)

    accounts, totals, _ = _pl_period_lookup(date(2026, 7, 1), date(2027, 6, 30))
    assert totals["sales"] == 700000.0
    assert accounts["4-01-01-01-0001"] + accounts["4-01-01-01-0002"] == totals["sales"]


def test_a_period_with_no_movement_reports_zero_not_another_periods_figures(app, coa):
    """The comparative column for an empty year is what made the defect
    visible — it showed the *other* period's profit."""
    from finance_app.routes.reports import _pl_period_lookup

    _post(coa["sales_a"], date(2026, 9, 1), credit=630000)

    _, totals, subtotals = _pl_period_lookup(date(2025, 7, 1), date(2026, 6, 30))
    assert totals["sales"] == 0.0
    assert subtotals["net_profit"] == 0.0
