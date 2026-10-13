"""Payroll arithmetic that has to be right: proration, taxable income and
withholding tax.

Kept out of the route so the preview, the run and the tests share one
calculation.

* **Components** are a fixed amount or a percentage of basic.
* **Proration** is by calendar days: a joiner is paid from the joining date,
  and approved leave of an unpaid leave type is not paid. It reduces the
  earnings (and so the salary expense), not a deduction line. The old code
  assumed a 30-day month and ignored unpaid leave.
* **Taxable income** is basic + the components marked taxable + overtime and
  bonus. Non-taxable allowances (e.g. medical within the exemption) were
  taxed anyway: the flag was stored and never read.
* **Withholding** follows the cumulative method an employer uses under
  section 149: tax on the projected income for the tax year (July–June) —
  what was paid so far plus the current regular pay for the months left —
  less the tax already withheld, spread over those months. A one-off (bonus,
  overtime) is taxed in the month it is paid at the extra tax it causes. The
  old code taxed the month's gross ×12, so a bonus month was taxed as if the
  bonus were paid every month, and nothing corrected over- or under-
  deduction across the year.
"""
import calendar
import json
from datetime import date

from .extensions import db

TAX_YEAR_START_MONTH = 7          # Pakistan: 1 July – 30 June

# Salaried individuals, tax year 2026 (Finance Act 2025):
# (from, to, rate %, tax on the amount up to "from").
SLABS_TY2026 = [
    (0, 600_000, 0, 0),
    (600_000, 1_200_000, 1, 0),
    (1_200_000, 2_200_000, 11, 6_000),
    (2_200_000, 3_200_000, 23, 116_000),
    (3_200_000, 4_100_000, 30, 346_000),
    (4_100_000, 999_999_999, 35, 616_000),
]


def component_amount(component, basic):
    """A component's full-month amount."""
    value = float(component.value or 0)
    if (component.calculation_method or "fixed") in ("percentage", "percent_of_basic"):
        return round(float(basic or 0) * value / 100, 2)
    return round(value, 2)


def month_bounds(month, year):
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


def unpaid_leave_days(user_id, month, year):
    """Days of approved unpaid leave falling in the month (half days count .5)."""
    from .models.leave import LeaveRequest, LeaveType
    first, last = month_bounds(month, year)
    rows = (LeaveRequest.query.join(LeaveType, LeaveRequest.leave_type_id == LeaveType.id)
            .filter(LeaveRequest.user_id == user_id, LeaveRequest.status == "approved",
                    LeaveType.is_paid == False,  # noqa: E712
                    LeaveRequest.start_date <= last, LeaveRequest.end_date >= first)
            .all())
    days = 0.0
    for lr in rows:
        span = (lr.end_date - lr.start_date).days + 1
        inside = (min(lr.end_date, last) - max(lr.start_date, first)).days + 1
        if span <= 0 or inside <= 0:
            continue
        # total_days may exclude weekends/holidays: take the same share of it.
        days += float(lr.total_days or span) * inside / span
    return round(days, 2)


def paid_ratio(user, month, year):
    """(ratio of the month that is paid, days in month, unpaid leave days),
    or None when the employee had not joined by the month's end."""
    first, last = month_bounds(month, year)
    dim = last.day
    joined = getattr(user, "date_of_joining", None)
    if joined and joined > last:
        return None
    employed = dim - (joined.day - 1) if joined and joined >= first else dim
    unpaid = min(unpaid_leave_days(user.id, month, year), employed)
    return max(0.0, (employed - unpaid) / dim), dim, unpaid


def tax_on(annual_income):
    from .models.tax import IncomeTaxSlab
    return IncomeTaxSlab.calculate_tax(max(0.0, float(annual_income)))


def tax_year_months(month, year):
    """(start (month, year) of the tax year, months left including this one)."""
    start_year = year if month >= TAX_YEAR_START_MONTH else year - 1
    elapsed = (month - TAX_YEAR_START_MONTH) % 12
    return (TAX_YEAR_START_MONTH, start_year), 12 - elapsed


def year_to_date(user_id, month, year):
    """Taxable pay and income tax on this employee's earlier slips in the tax year."""
    from .models.compensation import PayrollRun, PayrollSlip
    (sm, sy), _ = tax_year_months(month, year)
    key = year * 12 + month
    start_key = sy * 12 + sm
    rows = (db.session.query(PayrollSlip, PayrollRun.month, PayrollRun.year)
            .join(PayrollRun, PayrollSlip.payroll_run_id == PayrollRun.id)
            .filter(PayrollSlip.user_id == user_id).all())
    taxable = tax = 0.0
    for slip, m, y in rows:
        k = y * 12 + m
        if not start_key <= k < key:
            continue
        taxable += float(slip.taxable_pay if slip.taxable_pay is not None else slip.gross_pay or 0)
        try:
            comps = json.loads(slip.components_json or "{}")
            tax += float((comps.get("deductions") or {}).get("Income Tax", 0) or 0)
        except (ValueError, TypeError, AttributeError):
            pass
    return round(taxable, 2), round(tax, 2)


def withholding(user_id, month, year, regular_taxable, one_off_taxable=0.0,
                full_month_taxable=None):
    """Income tax to withhold this month (cumulative method).

    ``regular_taxable`` is this month's regular taxable pay (prorated for a
    joiner or unpaid leave); the months still to come are projected at the
    full-month figure."""
    ytd_taxable, ytd_tax = year_to_date(user_id, month, year)
    _, left = tax_year_months(month, year)
    full = float(regular_taxable if full_month_taxable is None else full_month_taxable)
    projected = ytd_taxable + float(regular_taxable) + full * (left - 1)
    base_tax = tax_on(projected)
    regular_part = max(0.0, (base_tax - ytd_tax) / left)
    one_off_part = tax_on(projected + float(one_off_taxable)) - base_tax if one_off_taxable else 0.0
    return round(regular_part + max(0.0, one_off_part), 2)
