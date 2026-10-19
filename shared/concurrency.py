"""What a user is told when someone else changed the same records first.

Many users share one set of books, each request in its own transaction. The
posting paths serialise the records they change (document rows through
``scoped_get(..., for_update=True)``, product stock through
``costing.lock_products``, number series and report totals through atomic
updates), so a second user normally just waits a moment and then sees the
first one's result. What can still reach the database as an error —
a deadlock between two long postings, a unique key two users raced for,
a row another user deleted — is answered in words here and rolled back
whole, never shown as a crash. Nothing is half-saved: the transaction that
lost is undone entirely.
"""
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm.exc import ObjectDeletedError, StaleDataError

CONFLICT = ("Another user changed the same records at the same moment, so "
            "nothing was saved. Reload the page and try again.")
DUPLICATE = ("This clashes with a record that already exists — a number, "
             "code or name that must be unique — so nothing was saved. "
             "Reload the page and try again.")
GONE = ("A record this refers to was removed by another user, so nothing "
        "was saved. Reload the page and try again.")

# Postgres SQLSTATEs a concurrent writer causes: serialization failure,
# deadlock detected, lock not available.
_CONCURRENT = {"40001", "40P01", "55P03"}


def conflict_message(exc):
    """Words for a database refusal caused by another user's change, or None
    when the error is something else (a real bug)."""
    if isinstance(exc, (StaleDataError, ObjectDeletedError)):
        return CONFLICT
    orig = getattr(exc, "orig", None)
    code = getattr(orig, "pgcode", None)
    text = str(orig or exc).lower()
    if isinstance(exc, OperationalError):
        if code in _CONCURRENT or "deadlock" in text or "database is locked" in text:
            return CONFLICT
        return None
    if isinstance(exc, IntegrityError):
        if code == "23505" or "unique" in text:
            return DUPLICATE
        if code == "23503" or "foreign key" in text:
            return GONE
    return None


def serialize(scope, *key):
    """Queue the requests that check-then-create the same thing — one draft
    reconciliation per bank account, one payroll run per month, one
    depreciation charge per asset and month — so the second one waits for the
    first to commit and its check then sees what the first created.

    A transaction-scoped advisory lock on (scope, key): released at commit or
    rollback, and it works through Neon's pooled connection. Postgres only;
    SQLite already lets one writer in at a time.
    """
    import zlib
    from sqlalchemy import text
    from shared.extensions import db
    conn = db.session.connection()
    if conn.dialect.name != "postgresql":
        return
    a = zlib.crc32(scope.encode()) & 0x7FFFFFFF
    b = zlib.crc32(repr(key).encode()) & 0x7FFFFFFF
    conn.execute(text("SELECT pg_advisory_xact_lock(:a, :b)"), {"a": a, "b": b})


def install_lock_timeout():
    """Every Postgres transaction waits at most DB_LOCK_TIMEOUT_MS (default
    15 s) for a row another user holds, then fails with a lock timeout that
    conflict_message() answers in words. Without it a request queued behind
    a stuck one hung until the serverless function was killed. SET LOCAL is
    scoped to the transaction, so it is safe through Neon's pooler."""
    import os
    from sqlalchemy import event
    from sqlalchemy.engine import Engine
    ms = int(os.environ.get("DB_LOCK_TIMEOUT_MS", "15000"))

    @event.listens_for(Engine, "begin")
    def _lock_timeout(conn):
        if conn.dialect.name == "postgresql" and ms > 0:
            conn.exec_driver_sql(f"SET LOCAL lock_timeout = {ms}")


def stale_edit(doc, sent, label):
    """Refuse a save made from a stale copy of ``doc``, else bump its version.

    The form sends the ``edit_version`` it loaded. If the document has been
    saved, approved or unapproved since — by anyone — the save would replace
    their edit with this copy, silently; it is refused instead, naming who
    changed it and when. A request that sends no version (an older page, a
    script) is not checked. Call with the document locked
    (``scoped_get(..., for_update=True)``) so two saves cannot both pass.
    Returns the refusal text, or None after bumping the version.
    """
    try:
        sent = None if sent in (None, "") else int(sent)
    except (TypeError, ValueError):
        sent = None
    current = int(doc.edit_version or 0)
    if sent is not None and sent != current:
        who = _last_change(doc)
        return (f"{label} was changed{who} after you opened it, so your changes were "
                "not saved. Reload it to see the latest version, then make your "
                "changes again.")
    bump_edit(doc)
    return None


def bump_edit(doc):
    """A save or state change: copies opened before it are now stale."""
    doc.edit_version = int(doc.edit_version or 0) + 1


def _last_change(doc):
    """' by <name> at <time>' from the audit trail, or ''."""
    try:
        from dateutil import tz
        from shared.audit import TRACKED
        from shared.clock import company_zone
        from shared.models.audit_log import AuditLog
        label = TRACKED.get(doc.__tablename__, (None, None))[1]
        row = (AuditLog.query.filter(AuditLog.entity_type == label,
                                     AuditLog.entity_id == doc.id,
                                     AuditLog.company_id == doc.company_id)
               .order_by(AuditLog.created_at.desc()).first()) if label else None
        if row is None:
            return ""
        when = row.created_at.replace(tzinfo=tz.UTC).astimezone(company_zone())
        return f" by {row.user_name or 'another user'} at {when:%H:%M on %d %b}"
    except Exception:
        return ""


def consistent_snapshot(view):
    """Run a report in one snapshot of the database.

    A report is several queries. Under Postgres's default isolation each
    query sees whatever had committed when it started, so a posting landing
    between two of them was in one figure and not the other: Books Integrity
    reported "Inventory account ≠ stock valuation" for a ledger that tied, and
    a balance sheet could be off for a moment while users were posting. The
    report runs in a REPEATABLE READ transaction instead — every query sees
    the books as of the same instant. (What the request read before the
    report — the signed-in user, the company — is committed first.)
    """
    from functools import wraps

    @wraps(view)
    def wrapper(*args, **kwargs):
        from shared.extensions import db
        if db.engine.dialect.name == "postgresql":
            db.session.commit()
            db.session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        return view(*args, **kwargs)
    return wrapper
