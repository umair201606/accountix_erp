"""13-Week Cash Flow (TWCF) engine — the standard treasury liquidity report.

A TWCF is a matrix of 13 weekly columns (one per week, Mon–Sun): opening
cash, forecast receipts and disbursements, net cash flow, and a running
closing balance. Nothing here is stored — every figure is recomputed from
posted journal lines plus the user's ``TwcfLine`` forecast lines:

* ``opening_cash`` — actual cash balance at the moment the window starts.
* ``collections_forecast`` / ``payments_forecast`` — open customer/supplier
  balances (Trade Debtors / Trade Creditors subtrees) aged FIFO and spread
  across the weeks by age bucket: fresher debt is collected sooner.
* Weeks already elapsed show *actual* net cash (from the journals) and a
  variance row against the forecast — the standard TWCF discipline.
* User lines (one-off and recurring) add the obligations the ledger cannot
  see yet: payroll, rent, taxes, capex, loan repayments.
* ``build_matrix`` ties it together with totals, closing, and — when the
  company sets a cash floor — a covenant headroom line.
"""

from datetime import date, datetime, timedelta

from shared.extensions import db
from shared.models.ledger import ChartOfAccount, JournalEntry, JournalLine
from shared.models.twcf import (TWCF_IN, TWCF_OUT, TWCF_ONEOFF, TWCF_WEEKLY,
                                TWCF_MONTHLY, TWCF_QUARTERLY, TWCF_YEARLY,
                                TwcfLine, TWCF_USER_IN_CATEGORIES,
                                TWCF_USER_OUT_CATEGORIES)

WEEKS_IN_FORECAST = 13

# Age buckets (same labels as the executive aging report) mapped onto the
# 13-week horizon. A bucket spreads evenly across its weeks: 0-30 days of age
# means the money is expected within the next fortnight, 90+ days across the
# tail. Weights sum to 1 per bucket.
BUCKET_DEFS = (
    ("current", 0, 30, ((0, 0.5), (1, 0.5))),
    ("d31", 31, 60, ((2, 1 / 3), (3, 1 / 3), (4, 1 / 3))),
    ("d61", 61, 90, ((5, 1 / 3), (6, 1 / 3), (7, 1 / 3))),
    ("d91", 91, None, ((8, 0.2), (9, 0.2), (10, 0.2), (11, 0.2), (12, 0.2))),
)


def _eod(d):
    """Inclusive upper bound for an ``entry_date`` filter (DateTime column)."""
    if d is None:
        return None
    if isinstance(d, datetime):
        return d
    return datetime.combine(d, datetime.max.time())


def _f(v):
    return float(v or 0)


def weeks_from(start, n=WEEKS_IN_FORECAST):
    """The ``n`` 7-day blocks beginning at ``start`` (any weekday)."""
    out = []
    for i in range(n):
        ws = start + timedelta(days=7 * i)
        out.append({"start": ws, "end": ws + timedelta(days=6)})
    return out


def _cash_account_ids():
    """Every postable account whose effective activity is ``cash`` — the same
    population the cash-flow statement uses for its opening/closing cash."""
    return [a.id for a in ChartOfAccount.query.all()
            if (a.effective_cash_flow_activity() or "") == "cash"
            and a.level >= 5]


def opening_cash(as_of):
    """Actual cash and equivalents at the end of ``as_of`` (inclusive)."""
    ids = _cash_account_ids()
    if not ids:
        return 0.0
    q = (db.session.query(
            db.func.coalesce(db.func.sum(JournalLine.debit), 0),
            db.func.coalesce(db.func.sum(JournalLine.credit), 0))
         .join(JournalEntry, JournalLine.journal_entry_id == JournalEntry.id)
         .filter(JournalEntry.is_posted == True,   # noqa: E712
                 JournalLine.account_id.in_(ids)))
    if as_of:
        q = q.filter(JournalEntry.entry_date <= _eod(as_of))
    dr, cr = q.first()
    return _f(dr) - _f(cr)


def actual_net_cash(from_date, to_date):
    """Posted net cash movement (in − out) over a date window."""
    ids = _cash_account_ids()
    if not ids:
        return 0.0
    q = (db.session.query(
            db.func.coalesce(db.func.sum(JournalLine.debit), 0),
            db.func.coalesce(db.func.sum(JournalLine.credit), 0))
         .join(JournalEntry, JournalLine.journal_entry_id == JournalEntry.id)
         .filter(JournalEntry.is_posted == True,   # noqa: E712
                 JournalEntry.entry_date >= from_date,
                 JournalEntry.entry_date <= _eod(to_date),
                 JournalLine.account_id.in_(ids)))
    dr, cr = q.first()
    return _f(dr) - _f(cr)


def _subtree_ids(root):
    """Ids of ``root`` and every descendant (tenant-scoped query walk)."""
    ids = [root.id]
    frontier = [root.id]
    while frontier:
        kids = [c.id for c in ChartOfAccount.query.filter(
            ChartOfAccount.parent_id.in_(frontier)).all()]
        ids.extend(kids)
        frontier = kids
    return ids


def _party_scope():
    """Accounts whose balances represent trade AR/AP.

    The canonical scope is the Trade Debtors / Trade Creditors subtrees
    (``ENTITY_PARENT_CODES`` — every customer and supplier subledger account
    lives under one of them). If a company deleted one of the controls, fall
    back to whatever the executive module watches; if that is empty too,
    there is nothing to forecast.
    """
    from shared.coa import ENTITY_PARENT_CODES
    from shared.models.exec_report import ExecAccountSelection

    ids = set()
    for code in (ENTITY_PARENT_CODES["customer"], ENTITY_PARENT_CODES["supplier"]):
        root = ChartOfAccount.query.filter_by(code=code).first()
        if root is not None:
            ids.update(_subtree_ids(root))
    if ids:
        return ids
    return {int(s.account_id) for s in ExecAccountSelection.query.all()}


def _aged_parties(side, as_of):
    """FIFO-age every in-scope party on one side.

    Returns ``[{account_id, name, layers: [(days, amount), ...]}]`` — layers
    are the surviving FIFO stack, so a recent payment erodes the oldest layer
    first, exactly like the executive aging report.
    """
    account_ids = _party_scope()
    if not account_ids:
        return []
    q = (db.session.query(JournalLine, JournalEntry)
         .join(JournalEntry, JournalLine.journal_entry_id == JournalEntry.id)
         .filter(JournalEntry.is_posted == True,   # noqa: E712
                 JournalLine.account_id.in_(list(account_ids))))
    if as_of:
        q = q.filter(JournalEntry.entry_date <= _eod(as_of))
    postings = {}
    for line, entry in q.order_by(JournalEntry.entry_date,
                                  JournalEntry.id).all():
        postings.setdefault(int(line.account_id), []).append(
            (entry.entry_date, line.debit, line.credit))

    accounts = {int(a.id): a for a in ChartOfAccount.query.filter(
        ChartOfAccount.id.in_(list(postings.keys()))).all()}
    as_of_day = as_of.date() if isinstance(as_of, datetime) else (
        as_of if isinstance(as_of, date) else datetime.utcnow().date())

    want_receivable = side == "receivable"
    out = []
    for account_id, rows in postings.items():
        acct = accounts.get(account_id)
        if acct is None:
            continue
        net = sum(_f(dr) - _f(cr) for _, dr, cr in rows)
        if abs(net) < 0.005:
            continue
        is_receivable = net > 0
        if is_receivable != want_receivable:
            continue
        # FIFO: debits build the stack, credits consume the oldest layer
        # (receivables), or the mirror for payables.
        add_idx, take_idx = (1, 2) if want_receivable else (2, 1)
        layers = []
        for entry_date, dr, cr in rows:
            add = _f(dr if add_idx == 1 else cr)
            if add:
                layers.append([entry_date, add])
            take = _f(cr if take_idx == 2 else dr)
            while take and layers:
                if layers[0][1] <= take:
                    take -= layers[0][1]
                    layers.pop(0)
                else:
                    layers[0][1] -= take
                    take = 0.0
        if not layers:
            continue
        aged = []
        for entry_date, amount in layers:
            days = max((as_of_day - entry_date.date()).days, 0)
            aged.append((days, amount))
        out.append({
            "account_id": account_id,
            "name": acct.name,
            "layers": aged,
            "amount": abs(net),
        })
    out.sort(key=lambda p: p["amount"], reverse=True)
    return out


def _side_forecast(side, as_of, n_weeks):
    """Per-week forecast for one side: every aged layer spread onto the
    horizon by its bucket. Returns ``n_weeks`` floats (index = week offset)."""
    cells = [0.0] * n_weeks
    for party in _aged_parties(side, as_of):
        for days, amount in party["layers"]:
            for _, lo, hi, spread in BUCKET_DEFS:
                if lo <= days and (hi is None or days <= hi):
                    for week_idx, weight in spread:
                        cells[week_idx] += amount * weight
                    break
    return [round(v, 2) for v in cells]


def collections_forecast(as_of, n_weeks=WEEKS_IN_FORECAST):
    """Expected customer collections per week, from the aged receivables."""
    return _side_forecast("receivable", as_of, n_weeks)


def payments_forecast(as_of, n_weeks=WEEKS_IN_FORECAST):
    """Expected supplier payments per week, from the aged payables."""
    return _side_forecast("payable", as_of, n_weeks)


# ── Recurring-line expansion ────────────────────────────────────────────────

def _month_occurrence(year, month, day_of_month):
    """``day_of_month`` clamped to the last day of that month."""
    last = (date(year + (1 if month == 12 else 0),
                 1 if month == 12 else month + 1, 1)
            - timedelta(days=1)).day
    return date(year, month, min(day_of_month, last))


def _line_occurrences(line, window_end):
    """All occurrence dates of one line from its start up to ``window_end``."""
    start = line.start_date
    if line.frequency == TWCF_ONEOFF:
        return [start]
    if line.frequency == TWCF_WEEKLY:
        # Keep the weekday of the first occurrence; step in whole weeks.
        offset = (line.day_of_week - start.weekday()) % 7
        first = start + timedelta(days=offset)
        out, cur = [], first
        while cur <= window_end:
            out.append(cur)
            cur += timedelta(days=7)
        return out
    if line.frequency == TWCF_MONTHLY:
        out = []
        y, m = start.year, start.month
        while date(y, m, 1) <= window_end:
            occ = _month_occurrence(y, m, line.day_of_month)
            if occ >= start:
                out.append(occ)
            m += 1
            if m > 12:
                m, y = 1, y + 1
        return out
    if line.frequency == TWCF_QUARTERLY:
        out = []
        y, m = start.year, start.month
        while date(y, m, 1) <= window_end:
            occ = _month_occurrence(y, m, line.day_of_month)
            if occ >= start:
                out.append(occ)
            m += 3
            while m > 12:
                m -= 12
                y += 1
        return out
    if line.frequency == TWCF_YEARLY:
        out = []
        y = start.year
        while date(y, 1, 1) <= window_end:
            occ = _month_occurrence(y, line.month or 1, line.day_of_month)
            if occ >= start:
                out.append(occ)
            y += 1
        return out
    return []


def line_week_values(line, weeks):
    """One line's per-week amounts over the window (occurrences bucketed
    into their containing week). Returns {week_index: amount}."""
    window_end = weeks[-1]["end"]
    values = {}
    for occ in _line_occurrences(line, window_end):
        for idx, wk in enumerate(weeks):
            if wk["start"] <= occ <= wk["end"]:
                values[idx] = values.get(idx, 0.0) + _f(line.amount)
                break
    return values


# ── The matrix ──────────────────────────────────────────────────────────────

def _category_rows(lines, weeks, direction, categories):
    """Per-week sums of user lines, one row per category label."""
    rows = []
    for cat in categories:
        values = [0.0] * len(weeks)
        for line in lines:
            if line.direction != direction or line.category != cat:
                continue
            for idx, amount in line_week_values(line, weeks).items():
                values[idx] += amount
        rows.append({"key": f"{direction}_{cat}", "label": cat,
                     "values": values})
    return rows

def build_matrix(start, today=None):
    """Assemble the full 13-week matrix.

    ``today`` is injectable for tests; defaults to the real date.
    """
    today = today or datetime.utcnow().date()
    if isinstance(today, datetime):
        today = today.date()
    weeks = weeks_from(start)
    as_of = start - timedelta(days=1)

    lines = TwcfLine.query.order_by(TwcfLine.start_date).all()

    opening = opening_cash(as_of)
    coll = collections_forecast(as_of, len(weeks))
    pay = payments_forecast(as_of, len(weeks))

    in_rows = [{"key": "in_collections", "label": "Collections from customers",
                "values": coll, "auto": True}]
    in_rows += _category_rows(lines, weeks, TWCF_IN, TWCF_USER_IN_CATEGORIES)
    out_rows = [{"key": "out_suppliers", "label": "Payments to suppliers",
                 "values": pay, "auto": True}]
    out_rows += _category_rows(lines, weeks, TWCF_OUT, TWCF_USER_OUT_CATEGORIES)

    total_in = [sum(r["values"][i] for r in in_rows) for i in range(len(weeks))]
    total_out = [sum(r["values"][i] for r in out_rows) for i in range(len(weeks))]
    forecast_net = [total_in[i] - total_out[i] for i in range(len(weeks))]

    # Weeks fully elapsed use actual net; the variance row shows the gap.
    actuals = []
    for wk in weeks:
        if wk["end"] < today:
            actuals.append(actual_net_cash(wk["start"], wk["end"]))
        else:
            actuals.append(None)
    net = [a if a is not None else forecast_net[i]
           for i, a in enumerate(actuals)]
    variance = [None if a is None else round(a - forecast_net[i], 2)
                for i, a in enumerate(actuals)]

    closing = []
    run = opening
    for i in range(len(weeks)):
        run += net[i]
        closing.append(round(run, 2))

    from shared.models.company_settings import ReportSettings
    settings = ReportSettings.get()
    floor = _f(settings.twcf_cash_floor) if settings.twcf_cash_floor else None

    rows = [
        {"key": "opening", "kind": "opening",
         "label": "Opening cash & equivalents",
         "values": [opening] + [None] * (len(weeks) - 1),
         "total": opening},
        {"key": "sec_in", "kind": "section", "label": "RECEIPTS"},
        *[{**r, "kind": "account"} for r in in_rows],
        {"key": "total_in", "kind": "total", "label": "Total receipts",
         "values": total_in, "total": round(sum(total_in), 2)},
        {"key": "sec_out", "kind": "section", "label": "DISBURSEMENTS"},
        *[{**r, "kind": "account"} for r in out_rows],
        {"key": "total_out", "kind": "total", "label": "Total disbursements",
         "values": total_out, "total": round(sum(total_out), 2)},
        {"key": "net", "kind": "net", "label": "Net cash flow",
         "values": net, "total": round(sum(net), 2)},
        {"key": "variance", "kind": "variance",
         "label": "Variance (actual vs forecast)",
         "values": variance, "total": None},
        {"key": "closing", "kind": "grand", "label": "Closing cash & equivalents",
         "values": closing, "total": closing[-1]},
    ]
    if floor is not None:
        rows.append({"key": "floor", "kind": "plain",
                     "label": "Minimum cash (floor)",
                     "values": [floor] * len(weeks), "total": None})
        rows.append({"key": "headroom", "kind": "headroom",
                     "label": "Headroom over floor",
                     "values": [round(c - floor, 2) for c in closing],
                     "total": round(closing[-1] - floor, 2)})

    return {
        "weeks": [{"start": wk["start"], "end": wk["end"],
                   "is_past": wk["end"] < today}
                  for wk in weeks],
        "rows": rows,
        "opening": opening,
        "total_in": round(sum(total_in), 2),
        "total_out": round(sum(total_out), 2),
        "net": round(sum(net), 2),
        "closing": closing,
        "has_past_weeks": any(a is not None for a in actuals),
        "floor": floor,
        "has_scope": bool(_party_scope()),
    }
