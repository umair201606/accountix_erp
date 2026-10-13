"""Accounting period locking.

A closed period is frozen: its reported numbers must never move again. That is
what makes a cost, once computed and conveyed, permanent — an employee charged
for a damaged product has an agreed receivable, and if a later edit could still
shift the period the charge sits in, "agreed" means nothing.

Two paths can move a closed period's numbers, and both are guarded here:

    posting     a new journal entry dated inside the period.
    un-posting  reverse_journal_entry flips is_posted=False on the ORIGINAL
                entry rather than writing a counter-entry, so un-approving a
                voucher silently rewrites the period it was posted in.

The rule is the one every serious ERP applies: corrections to a closed period
are not made in that period. They are posted as an adjusting entry in the
current open period, where they are visible.
"""

from datetime import date, datetime

from shared.models.company_settings import AccountingPeriod


class ClosedPeriodError(Exception):
    """Raised when an operation would change a closed period's numbers."""


def _as_date(when):
    if isinstance(when, datetime):
        return when.date()
    if isinstance(when, date):
        return when
    return date.today()


def period_for(when):
    """The accounting period covering ``when``, or None if none is defined."""
    d = _as_date(when)
    return (AccountingPeriod.query
            .filter(AccountingPeriod.start_date <= d,
                    AccountingPeriod.end_date >= d)
            .order_by(AccountingPeriod.start_date.desc())
            .first())


def current_open_period():
    """The open period covering today, if any — where corrections belong."""
    p = period_for(date.today())
    return p if (p is not None and not p.is_closed) else None


def is_closed(when):
    p = period_for(when)
    return p is not None and bool(p.is_closed)


def require_open_period(when, action="post to"):
    """Refuse if ``when`` falls in a closed period.

    A date no period covers is allowed: periods are created by the user, and
    refusing every date outside them would block posting on a fresh database
    that has only the current fiscal year seeded. Only an explicit close locks.
    """
    p = period_for(when)
    if p is None or not p.is_closed:
        return p
    closed_on = p.closed_at.strftime("%d %b %Y") if p.closed_at else "earlier"
    raise ClosedPeriodError(
        f"Cannot {action} {_as_date(when).strftime('%d %b %Y')}: the period "
        f"'{p.period_name}' ({p.start_date:%d %b %Y} – {p.end_date:%d %b %Y}) "
        f"was closed on {closed_on} and its figures are final. Post an "
        f"adjusting entry in the current open period instead, or reopen "
        f"'{p.period_name}' in Settings → Periods if closing it was a mistake."
    )


def close_checklist(period):
    """What is still open inside ``period`` — once it is closed none of it can
    be posted there any more. Returns readable lines (empty = nothing open).

    Drafts dated in the period, depreciation still due up to its end, and
    payroll months in it that were never run.
    """
    from datetime import datetime, time
    start = datetime.combine(period.start_date, time.min)
    end = datetime.combine(period.end_date, time.max)
    out = []

    def count(model, date_col, open_states):
        try:
            return model.query.filter(date_col >= start, date_col <= end,
                                      model.status.in_(open_states)).count()
        except Exception:  # module not installed for this company
            return 0

    from inventory_app.models.invoice import InvInvoice
    from inventory_app.models.purchase_invoice import InvPurchaseInvoice
    from inventory_app.models.sales_return import InvSalesReturn
    from inventory_app.models.purchase_return import InvPurchaseReturn
    from shared.models.accounting_voucher import AccountingVoucher
    from shared.models.vouchers import ConsumptionVoucher, ScrapVoucher, StockAdjustmentVoucher
    si = InvInvoice.query.filter(InvInvoice.invoice_date >= start, InvInvoice.invoice_date <= end,
                                 InvInvoice.voucher_status != "approved").count()
    for n, label in (
            (si, "unapproved sales invoice(s)"),
            (count(InvPurchaseInvoice, InvPurchaseInvoice.invoice_date, ("new", "unapproved")),
             "unapproved purchase invoice(s)"),
            (count(InvSalesReturn, InvSalesReturn.return_date, ("new", "unapproved")),
             "unapproved sales return(s)"),
            (count(InvPurchaseReturn, InvPurchaseReturn.return_date, ("new", "unapproved")),
             "unapproved purchase return(s)"),
            (count(AccountingVoucher, AccountingVoucher.voucher_date, ("unapproved", "draft")),
             "unapproved accounting voucher(s)"),
            (sum(count(m, m.date, ("unapproved",))
                 for m in (ConsumptionVoucher, ScrapVoucher, StockAdjustmentVoucher)),
             "unapproved stock voucher(s)")):
        if n:
            out.append(f"{n} {label}")
    try:
        from fixed_assets_app.models.asset import FixedAsset
        from fixed_assets_app.routes.depreciation import due_depreciation
        due = [a for a in FixedAsset.query.filter_by(status="active").all()
               if due_depreciation(a, period.end_date) > 0]
        if due:
            out.append(f"depreciation not yet posted to {period.end_date:%d %b %Y} on "
                       f"{len(due)} asset(s)")
    except Exception:
        pass
    try:
        from hr_app.models.compensation import PayrollProfile, PayrollRun
        if PayrollProfile.query.first() is not None:
            missing = []
            y, m = period.start_date.year, period.start_date.month
            while (y, m) <= (period.end_date.year, period.end_date.month):
                if PayrollRun.query.filter_by(month=m, year=y).first() is None:
                    missing.append(f"{m}/{y}")
                m += 1
                if m == 13:
                    y, m = y + 1, 1
            if missing:
                out.append("no payroll run for " + ", ".join(missing[:6])
                           + (" …" if len(missing) > 6 else ""))
    except Exception:
        pass
    return out
