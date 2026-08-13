"""13-Week Cash Flow engine: weeks, cash actuals, aged AR/AP forecast,
recurring-line expansion and the full matrix.

The contract these protect: every figure is recomputed from posted journals
and the user's forecast lines — nothing is stored; weeks fully elapsed carry
actual cash movement; and closing is always opening plus the running net, so
the matrix balances by construction.
"""

from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

import shared.tenancy  # noqa: F401  (registers the scoping listener)
from shared.extensions import db

import shared.models.base  # noqa: F401
import shared.models.company  # noqa: F401
import shared.models.company_settings  # noqa: F401
import shared.models.invoice_template  # noqa: F401 (FK target)
import shared.models.twcf  # noqa: F401
import hr_app.models.compensation  # noqa: F401  (HR payroll_runs table)
import shared.models.ledger  # noqa: F401


@pytest.fixture
def app():
    from flask import Flask
    application = Flask(__name__)
    application.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    application.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(application)
    shared.tenancy._reset_registry()
    with application.app_context():
        db.create_all()
        from shared.models.company import Company
        from shared.tenancy import set_current_company, unscoped
        with unscoped():
            default = Company(name="TWCF Unit Co", slug="twcf-unit",
                              is_active=True)
            db.session.add(default)
            db.session.commit()
        set_current_company(default.id)
        yield application
        db.session.remove()
        db.drop_all()


@pytest.fixture
def chart(app):
    """Cash + a customer and a supplier subledger account, like the seed."""
    from shared.models.ledger import ChartOfAccount

    cash = ChartOfAccount(code="1-01-01-01-0001", name="Cash",
                          type="asset", level=5, cash_flow_activity="cash")
    ar_parent = ChartOfAccount(code="1-01-02-01", name="Trade Debtors",
                               type="asset", level=4)
    ap_parent = ChartOfAccount(code="2-01-01-01", name="Trade Creditors",
                               type="liability", level=4)
    db.session.add_all([cash, ar_parent, ap_parent])
    db.session.commit()
    cust = ChartOfAccount(code="1-01-02-01-0101", name="Customer One",
                          type="asset", parent_id=ar_parent.id, level=5)
    supp = ChartOfAccount(code="2-01-01-01-0101", name="Supplier One",
                          type="liability", parent_id=ap_parent.id, level=5)
    db.session.add_all([cust, supp])
    db.session.commit()
    return {"cash": cash.id, "cust": cust.id, "supp": supp.id}


def _post(account_id, debit=0, credit=0, when=None, description="twcf test"):
    from shared.models.ledger import JournalEntry, JournalLine
    entry = JournalEntry(voucher_type="JV", voucher_id=0,
                         voucher_number="TWCF-JV",
                         description=description,
                         entry_date=when or datetime.utcnow(), created_by=1,
                         is_posted=True)
    db.session.add(entry)
    db.session.commit()
    db.session.add(JournalLine(journal_entry_id=entry.id,
                               account_id=account_id,
                               debit=Decimal(str(debit)),
                               credit=Decimal(str(credit))))
    db.session.commit()
    return entry


def _line(direction="out", category="payroll", description="Salaries",
          amount=1000, start_date=None, frequency="oneoff", day_of_week=0,
          day_of_month=1, month=1):
    from shared.models.twcf import TwcfLine
    line = TwcfLine(direction=direction, category=category,
                    description=description, amount=Decimal(str(amount)),
                    start_date=start_date or date.today(),
                    frequency=frequency, day_of_week=day_of_week,
                    day_of_month=day_of_month, month=month)
    db.session.add(line)
    db.session.commit()
    return line


def _clear(chart):
    from shared.models.ledger import JournalEntry, JournalLine
    from shared.models.twcf import TwcfLine
    JournalLine.query.delete()
    JournalEntry.query.delete()
    TwcfLine.query.delete()
    db.session.commit()


def test_weeks_from_builds_13_blocks():
    from shared.twcf_reports import weeks_from
    start = date(2026, 8, 3)  # a Monday
    weeks = weeks_from(start)
    assert len(weeks) == 13
    assert weeks[0]["start"] == start
    assert weeks[0]["end"] == start + timedelta(days=6)
    assert weeks[1]["start"] == start + timedelta(days=7)
    assert weeks[-1]["end"] == start + timedelta(days=90)


def test_opening_cash_is_actual_balance_at_window_start(app, chart):
    from shared.twcf_reports import opening_cash
    _post(chart["cash"], debit=5000, when=datetime(2026, 8, 1, 10, 0))
    _post(chart["cash"], debit=300, when=datetime(2026, 8, 5, 10, 0))
    assert opening_cash(date(2026, 8, 3)) == 5000.0


def test_actual_net_cash_summed_per_window(app, chart):
    from shared.twcf_reports import actual_net_cash
    _post(chart["cash"], debit=700, when=datetime(2026, 7, 21, 10, 0))
    _post(chart["cash"], credit=300, when=datetime(2026, 7, 24, 10, 0))
    assert actual_net_cash(date(2026, 7, 20), date(2026, 7, 26)) == 400.0
    assert actual_net_cash(date(2026, 7, 27), date(2026, 8, 2)) == 0.0


def test_collections_spread_by_age_bucket(app, chart):
    from shared.twcf_reports import collections_forecast
    # 1000 receivable 45 days before the window, 400 receivable 5 days before.
    as_of = date(2026, 8, 3)
    _post(chart["cust"], debit=1000, when=datetime(2026, 6, 19, 10, 0))
    _post(chart["cust"], debit=400, when=datetime(2026, 7, 29, 10, 0))
    cells = collections_forecast(as_of)
    assert len(cells) == 13
    # 400 (current) -> weeks 1-2 @ 200 each.
    assert cells[0] == pytest.approx(200.0, abs=0.01)
    assert cells[1] == pytest.approx(200.0, abs=0.01)
    # 1000 (31-60 days) -> weeks 3-5 @ 333.33 each.
    for i in (2, 3, 4):
        assert cells[i] == pytest.approx(333.33, abs=0.05)
    assert sum(cells) == pytest.approx(1400.0, abs=0.1)


def test_fifo_consumes_oldest_layer_first(app, chart):
    from shared.twcf_reports import collections_forecast
    as_of = date(2026, 8, 3)
    _post(chart["cust"], debit=1000, when=datetime(2026, 6, 19, 10, 0))
    _post(chart["cust"], debit=400, when=datetime(2026, 7, 29, 10, 0))
    # 400 collected on 31 Jul erodes the OLDEST layer (45d), not the new one.
    _post(chart["cust"], credit=400, when=datetime(2026, 7, 31, 10, 0))
    cells = collections_forecast(as_of)
    assert sum(cells) == pytest.approx(1000.0, abs=0.1)
    # Remaining: 600 @45d + 400 @5d.
    assert cells[0] == pytest.approx(200.0, abs=0.01)
    assert cells[2] == pytest.approx(200.0, abs=0.05)


def test_payments_mirror_receivables(app, chart):
    from shared.twcf_reports import payments_forecast
    as_of = date(2026, 8, 3)
    _post(chart["supp"], credit=900, when=datetime(2026, 7, 15, 10, 0))
    cells = payments_forecast(as_of)
    # 900 @ 19 days -> current bucket -> weeks 1-2 @ 450.
    assert cells[0] == pytest.approx(450.0, abs=0.01)
    assert cells[1] == pytest.approx(450.0, abs=0.01)
    assert sum(cells) == pytest.approx(900.0, abs=0.1)


def test_no_party_accounts_means_no_auto_forecast(app):
    from shared.twcf_reports import collections_forecast, payments_forecast
    as_of = date(2026, 8, 3)
    assert collections_forecast(as_of) == [0.0] * 13
    assert payments_forecast(as_of) == [0.0] * 13


def test_oneoff_line_lands_in_its_week(app, chart):
    from shared.twcf_reports import line_week_values, weeks_from
    start = date(2026, 8, 3)
    line = _line(direction="out", category="taxes", description="VAT",
                 amount=500, start_date=date(2026, 8, 12))
    values = line_week_values(line, weeks_from(start))
    assert values == {1: 500.0}  # week 2 (Aug 10-16)


def test_oneoff_outside_window_is_invisible(app, chart):
    from shared.twcf_reports import line_week_values, weeks_from
    start = date(2026, 8, 3)
    line = _line(direction="out", amount=500, start_date=date(2026, 11, 10))
    assert line_week_values(line, weeks_from(start)) == {}


def test_weekly_line_repeats_every_monday(app, chart):
    from shared.twcf_reports import line_week_values, weeks_from
    start = date(2026, 8, 3)  # Monday
    line = _line(direction="out", category="rent", amount=250,
                 start_date=start, frequency="weekly", day_of_week=0)
    values = line_week_values(line, weeks_from(start))
    assert len(values) == 13
    assert all(v == 250.0 for v in values.values())


def test_monthly_line_clamps_day_to_month_length(app, chart):
    from shared.twcf_reports import line_week_values, weeks_from
    start = date(2026, 8, 3)  # window ends 1 Nov 2026
    # Monthly on the 31st: occurrences clamp to month length.
    line = _line(direction="out", amount=1000, start_date=date(2026, 8, 31),
                 frequency="monthly", day_of_month=31)
    values = line_week_values(line, weeks_from(start))
    assert len(values) == 3  # Aug 31, Sep 30, Oct 31 — all inside the window
    assert all(v == 1000.0 for v in values.values())


def test_quarterly_and_yearly_lines(app, chart):
    from shared.twcf_reports import line_week_values, weeks_from
    from shared.twcf_reports import _line_occurrences
    # Occurrence stepping, tested directly over a long horizon: quarterly
    # lands on Jan/Apr/Jul/Oct, yearly on the same date each January.
    q = _line(direction="out", category="debt", amount=3000,
              start_date=date(2026, 1, 1), frequency="quarterly",
              day_of_month=1)
    y = _line(direction="in", category="other_in", amount=50000,
              start_date=date(2026, 1, 1), frequency="yearly",
              day_of_month=1, month=1)
    qv = _line_occurrences(q, date(2026, 12, 31))
    yv = _line_occurrences(y, date(2026, 12, 31))
    assert qv == [date(2026, 1, 1), date(2026, 4, 1),
                  date(2026, 7, 1), date(2026, 10, 1)]
    assert yv == [date(2026, 1, 1)]  # next yearly is Jan 2027
    # Inside a real window they bucket into their weeks.
    start = date(2026, 1, 5)  # window ends 5 Apr 2026
    assert line_week_values(q, weeks_from(start)) == {12: 3000.0}


def test_build_matrix_balances_and_flows_into_closing(app, chart):
    from shared import twcf_reports as tw
    start = date(2026, 8, 3)
    _post(chart["cash"], debit=10000, when=datetime(2026, 8, 1, 10, 0))
    _post(chart["cust"], debit=1400, when=datetime(2026, 7, 29, 10, 0))
    _post(chart["supp"], credit=700, when=datetime(2026, 7, 15, 10, 0))
    _line(direction="out", category="payroll", description="Salaries",
          amount=200, start_date=start, frequency="weekly", day_of_week=0)

    m = tw.build_matrix(start, today=date(2026, 8, 3))
    assert m["opening"] == 10000.0
    rows = {r["key"]: r for r in m["rows"]}
    assert rows["in_collections"]["values"][0] == 700.0  # 1400 current/2
    assert rows["out_suppliers"]["values"][0] == 350.0
    assert rows["out_payroll"]["values"][0] == 200.0
    net = rows["net"]["values"]
    closing = rows["closing"]["values"]
    run = m["opening"]
    for i in range(13):
        run += net[i]
        assert closing[i] == pytest.approx(run, abs=0.01)
    assert rows["closing"]["total"] == closing[-1]
    assert m["has_past_weeks"] is False


def test_build_matrix_past_weeks_use_actuals_with_variance(app, chart):
    from shared import twcf_reports as tw
    start = date(2026, 8, 3)
    today = date(2026, 8, 17)  # week 1 fully elapsed, week 2 running
    _post(chart["cash"], debit=10000, when=datetime(2026, 8, 1, 10, 0))
    # Week 1 (Aug 3-9): forecast would be 0, actual movement +500.
    _post(chart["cash"], debit=500, when=datetime(2026, 8, 5, 10, 0))
    _post(chart["cust"], debit=1400, when=datetime(2026, 7, 29, 10, 0))

    m = tw.build_matrix(start, today=today)
    rows = {r["key"]: r for r in m["rows"]}
    assert m["has_past_weeks"] is True
    assert rows["net"]["values"][0] == 500.0  # actual, not 700 forecast
    assert rows["variance"]["values"][0] == pytest.approx(-200.0, abs=0.01)
    # Week 2 (Aug 10-16) is also fully elapsed by Aug 17: actual 0 vs
    # forecast 700 (collections) -> variance -700.
    assert rows["variance"]["values"][1] == pytest.approx(-700.0, abs=0.01)
    assert rows["net"]["values"][1] == 0.0
    # A future week carries no variance yet.
    assert rows["variance"]["values"][2] is None
    assert rows["closing"]["values"][0] == pytest.approx(10500.0, abs=0.01)


def test_build_matrix_floor_and_headroom(app, chart):
    from shared import twcf_reports as tw
    from shared.models.company_settings import ReportSettings
    start = date(2026, 8, 3)
    _post(chart["cash"], debit=10000, when=datetime(2026, 8, 1, 10, 0))
    s = ReportSettings.get()
    s.twcf_cash_floor = Decimal("5000")
    db.session.commit()
    m = tw.build_matrix(start, today=date(2026, 8, 3))
    keys = [r["key"] for r in m["rows"]]
    assert "floor" in keys and "headroom" in keys
    rows = {r["key"]: r for r in m["rows"]}
    assert rows["floor"]["values"][0] == 5000.0
    assert rows["headroom"]["values"][0] == pytest.approx(5000.0, abs=0.01)
    s.twcf_cash_floor = None
    db.session.commit()
    m2 = tw.build_matrix(start, today=date(2026, 8, 3))
    assert [r["key"] for r in m2["rows"]].count("headroom") == 0


def test_build_matrix_auto_rows_respect_category_sum(app, chart):
    from shared import twcf_reports as tw
    start = date(2026, 8, 3)
    _post(chart["cust"], debit=800, when=datetime(2026, 7, 20, 10, 0))
    _line(direction="in", category="other_in", description="Loan",
          amount=300, start_date=date(2026, 8, 10))
    m = tw.build_matrix(start, today=date(2026, 8, 3))
    rows = {r["key"]: r for r in m["rows"]}
    # Week 2: 400 (collections, current bucket -> weeks 1-2) + 300 (loan).
    assert rows["total_in"]["values"][1] == pytest.approx(700.0, abs=0.01)
    assert rows["total_in"]["total"] == pytest.approx(1100.0, abs=0.01)
def test_auto_payroll_row_uses_latest_approved_hr_run(app, chart):
    from hr_app.models.compensation import PayrollRun
    from shared import twcf_reports as tw
    start = date(2026, 8, 3)  # window ends 1 Nov 2026
    # Two approved runs (Jun pay 20th, Jul pay 15th) and one unapproved.
    db.session.add(PayrollRun(month=6, year=2026,
                              run_date=datetime(2026, 6, 20, 10, 0),
                              status="approved", total_net=11000.0))
    db.session.add(PayrollRun(month=7, year=2026,
                              run_date=datetime(2026, 7, 15, 10, 0),
                              status="approved", total_net=12500.0))
    db.session.add(PayrollRun(month=8, year=2026,
                              run_date=datetime(2026, 8, 12, 10, 0),
                              status="unapproved", total_net=99999.0))
    db.session.commit()

    m = tw.build_matrix(start, today=date(2026, 8, 3))
    assert m["has_payroll_auto"] is True
    rows = {r["key"]: r for r in m["rows"]}
    auto = rows["out_payroll_auto"]
    assert auto["auto"] is True and auto["source"] == "payroll"
    # Latest approved run = Jul, pay date the 15th -> monthly on the 15th:
    # Aug 15 (week 2), Sep 15 (week 7), Oct 15 (week 11), all inside window.
    assert auto["values"] == [0.0, 12500.0, 0.0, 0.0, 0.0, 0.0, 12500.0,
                              0.0, 0.0, 0.0, 12500.0, 0.0, 0.0]
    assert sum(auto["values"]) == 37500.0
    # The unapproved run (99,999) must never leak into the forecast.
    assert 99999.0 not in auto["values"]


def test_no_payroll_runs_means_no_auto_payroll_row(app, chart):
    from shared import twcf_reports as tw
    m = tw.build_matrix(date(2026, 8, 3), today=date(2026, 8, 3))
    assert m["has_payroll_auto"] is False
    assert "out_payroll_auto" not in {r["key"] for r in m["rows"]}
