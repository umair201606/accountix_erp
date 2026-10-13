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


def leaving_date(user_id):
    """The employee's last day in the active company, if HR has set one."""
    from shared.models.company import CompanyMembership
    from shared.tenancy import current_company_id
    cid = current_company_id()
    if cid is None:
        return None
    m = CompanyMembership.query.filter_by(company_id=cid, user_id=user_id).first()
    return getattr(m, "date_of_leaving", None)


def paid_ratio(user, month, year):
    """(ratio of the month that is paid, days in month, unpaid leave days),
    or None when the employee was not employed at all in the month (not
    joined yet, or left before it began)."""
    first, last = month_bounds(month, year)
    dim = last.day
    joined = getattr(user, "date_of_joining", None)
    left = leaving_date(user.id)
    if (joined and joined > last) or (left and left < first):
        return None
    start = joined if joined and joined > first else first
    end = left if left and left < last else last
    employed = (end - start).days + 1
    unpaid = min(unpaid_leave_days(user.id, month, year), employed)
    return max(0.0, (employed - unpaid) / dim), dim, unpaid


def tax_on(annual_income):
    """Annual tax from the slabs, plus the surcharge on high salaried income."""
    from .models.tax import IncomeTaxSlab, PayrollSettings
    income = max(0.0, float(annual_income))
    tax = IncomeTaxSlab.calculate_tax(income)
    s = PayrollSettings.get()
    if s.surcharge_pct and income > float(s.surcharge_threshold or 0):
        tax = tax * (1 + float(s.surcharge_pct) / 100)
    return round(tax, 2)


def overtime_pay(hours, full_basic):
    from .models.tax import PayrollSettings
    s = PayrollSettings.get()
    basis = float(s.overtime_hours_basis or 160) or 160
    return round(float(hours or 0) * (float(full_basic or 0) / basis)
                 * float(s.overtime_multiplier or 1.5), 2)


def eobi(ratio=1.0):
    """(employee share, employer share) for the month, or (0, 0) when EOBI is off.
    Prorated with the paid part of the month."""
    from .models.tax import PayrollSettings
    s = PayrollSettings.get()
    if not s.eobi_enabled:
        return 0.0, 0.0
    base = float(s.eobi_wage_base or 0) * min(1.0, ratio)
    return (round(base * float(s.eobi_employee_pct or 0) / 100, 2),
            round(base * float(s.eobi_employer_pct or 0) / 100, 2))


def pf_base(pf_config, basic, gross):
    return basic if (getattr(pf_config, "contribution_base", None) or "gross") == "basic" else gross


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


def in_payroll(user, month, year):
    """Active staff, plus a leaver deactivated before their final month was run."""
    if user.is_active:
        return True
    left = leaving_date(user.id)
    return bool(left and left >= month_bounds(month, year)[0])


def service_years(joined, left):
    """Completed years of service, a final part year over six months counting
    as a full year (Standing Orders 12(6) practice)."""
    if not joined or not left or left < joined:
        return 0
    months = (left.year - joined.year) * 12 + (left.month - joined.month)
    if left.day < joined.day:
        months -= 1
    years, rest = divmod(max(0, months), 12)
    return years + (1 if rest > 6 else 0)


def final_settlement(user, month, year, full_basic, full_gross):
    """{"Gratuity": x, "Leave encashment": y} when the employee's last working
    day falls in this month and the company pays them; else {}."""
    from .models.tax import PayrollSettings
    left = leaving_date(user.id)
    first, last = month_bounds(month, year)
    if not left or not (first <= left <= last):
        return {}
    s = PayrollSettings.get()
    out = {}
    if s.gratuity_enabled:
        base = full_gross if (s.gratuity_basis or "basic") == "gross" else full_basic
        years = service_years(getattr(user, "date_of_joining", None), left)
        amount = round(float(base) / 30 * float(s.gratuity_days_per_year or 30) * years, 2)
        if amount:
            out["Gratuity"] = amount
    if s.leave_encashment_enabled:
        from .models.leave import LeaveQuota, LeaveType
        days = sum(float(q.remaining or 0) for q in
                   LeaveQuota.query.join(LeaveType, LeaveQuota.leave_type_id == LeaveType.id)
                   .filter(LeaveQuota.user_id == user.id, LeaveQuota.year == left.year,
                           LeaveType.is_paid == True,  # noqa: E712
                           LeaveType.carry_forward == True).all())  # noqa: E712
        amount = round(float(full_basic) / 30 * max(0.0, days), 2)
        if amount:
            out["Leave encashment"] = amount
    return out


def basic_on(pp, day, revisions=None):
    """Basic salary in force on ``day``: the latest salary revision effective
    by then; before the first revision, what it replaced; with no revisions,
    the profile's basic."""
    from .models.compensation import SalaryRevision
    if revisions is None:
        revisions = (SalaryRevision.query.filter_by(user_id=pp.user_id)
                     .order_by(SalaryRevision.effective_from, SalaryRevision.id).all())
    if not revisions:
        return float(pp.basic_salary or 0)
    current = None
    for r in revisions:
        if r.effective_from and r.effective_from <= day:
            current = float(r.new_basic or 0)
    if current is not None:
        return current
    first = revisions[0]
    return float(first.previous_basic if first.previous_basic is not None
                 else pp.basic_salary or 0)


def basic_for_month(pp, month, year):
    """Full-month basic for the month, day-weighted when a revision takes
    effect inside it (a raise on the 16th pays half the month at each rate).
    Re-running an earlier month uses the rate of that month, not today's."""
    from datetime import timedelta
    from .models.compensation import SalaryRevision
    revisions = (SalaryRevision.query.filter_by(user_id=pp.user_id)
                 .order_by(SalaryRevision.effective_from, SalaryRevision.id).all())
    first, last = month_bounds(month, year)
    if not revisions:
        return float(pp.basic_salary or 0)
    total = 0.0
    d = first
    while d <= last:
        total += basic_on(pp, d, revisions)
        d += timedelta(days=1)
    return round(total / last.day, 2)
