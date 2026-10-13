"""The company's wall clock.

The app stores naive datetimes and compares them with ``datetime.now()`` /
``date.today()``. That is the server's clock — UTC on Vercel — so in Lahore
(UTC+5) a 09:30 clock-in was recorded as 04:30 and never late, "today"
changed at 05:00 in the morning, and a document entered after midnight
defaulted to yesterday's date. ``Company.timezone`` (default Asia/Karachi)
existed but nothing read it.

These return naive values in the active company's zone, so they drop in for
``datetime.now()`` / ``date.today()`` wherever the answer is "what time is it
for the user". Timestamps that are audit records (``created_at``) stay UTC.
"""
from datetime import datetime, timedelta, timezone

from dateutil import tz

DEFAULT_ZONE = "Asia/Karachi"
_PKT = timezone(timedelta(hours=5))   # if the zone database is unavailable


def company_zone():
    name = None
    try:
        from shared.tenancy import current_company_id
        cid = current_company_id()
        if cid is not None:
            from flask import g
            cache = g.setdefault("_company_zone", {})
            if cid not in cache:
                from shared.models.company import Company
                c = Company.query.get(cid)
                cache[cid] = getattr(c, "timezone", None)
            name = cache[cid]
    except Exception:  # no app / request context
        name = None
    return tz.gettz(name or DEFAULT_ZONE) or tz.gettz(DEFAULT_ZONE) or _PKT


def local_now():
    """Naive datetime: now, in the active company's time zone."""
    return datetime.now(company_zone()).replace(tzinfo=None)


def local_today():
    return local_now().date()
