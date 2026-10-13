"""Small helpers every posting route shares: document dates and label splits.

Document date
    A document posts on ITS date — the invoice date, the voucher date — never
    on the moment it was approved. That is what puts a back-dated invoice in
    the period it belongs to, lets the period lock refuse one dated in a closed
    period, and orders stock movements for costing (shared/costing.py).

Label split
    A document whose lines carry different project labels must not post its
    pooled lines (revenue, COGS, discount, charges) all under the first line's
    label. Amounts are split by each label's share, cent-exact.
"""

from collections import OrderedDict
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP


class DocumentDateError(ValueError):
    """A document date that cannot be accepted."""


def parse_doc_date(value, fallback=None):
    """A form/JSON date ('YYYY-MM-DD') as a midnight datetime.

    Blank falls back to ``fallback`` (the document's existing date) and then
    to today. A malformed value raises DocumentDateError rather than silently
    posting on today's date.
    """
    if value in (None, ""):
        if fallback is not None:
            if isinstance(fallback, datetime):
                return datetime.combine(fallback.date(), datetime.min.time())
            if isinstance(fallback, date):
                return datetime.combine(fallback, datetime.min.time())
        from shared.clock import local_today
        return datetime.combine(local_today(), datetime.min.time())
    if isinstance(value, datetime):
        return datetime.combine(value.date(), datetime.min.time())
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time())
    try:
        return datetime.strptime(str(value).strip()[:10], "%Y-%m-%d")
    except ValueError:
        raise DocumentDateError(f"'{value}' is not a valid date (YYYY-MM-DD).")


def as_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    from shared.clock import local_today
    return local_today()


def label_weights(pairs):
    """Collapse [(label_id, weight)] into an ordered {label_id: weight}."""
    out = OrderedDict()
    for label_id, weight in pairs:
        out[label_id] = out.get(label_id, Decimal("0")) + Decimal(str(weight or 0))
    return out


def split_by_label(amount, weights):
    """Split ``amount`` across labels pro rata to ``weights`` {label: weight}.

    Returns [(label_id, amount)], cent-exact (the remainder lands on the last
    label). One label, or no positive weight, returns the whole amount under
    the first label.
    """
    amount = Decimal(str(amount or 0)).quantize(Decimal("0.01"), ROUND_HALF_UP)
    items = [(k, v) for k, v in weights.items()]
    if not items:
        return [(None, amount)]
    total = sum((v for _k, v in items if v > 0), Decimal("0"))
    if len(items) == 1 or total <= 0:
        return [(items[0][0], amount)]
    out, left = [], amount
    positive = [(k, v) for k, v in items if v > 0]
    for i, (k, v) in enumerate(positive):
        if i == len(positive) - 1:
            share = left
        else:
            share = (amount * v / total).quantize(Decimal("0.01"), ROUND_HALF_UP)
            left -= share
        if share:
            out.append((k, share))
    return out or [(items[0][0], amount)]
