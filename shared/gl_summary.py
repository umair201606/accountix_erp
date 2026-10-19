"""Keeps ``gl_daily_balances`` (shared/models/gl_balance.py) in step with the
general ledger, inside the same transaction as every posting.

How it stays exact without trusting any caller, with any number of users
posting at once:

- ``before_flush`` notes every journal entry whose lines or posted state are
  about to change, and reads what those entries contribute to each
  (company, account, label, day) key in the database *before* the change.
- ``after_flush`` reads what they contribute *after* it, and adds the
  difference to each key with one atomic ``INSERT ... ON CONFLICT DO UPDATE
  SET debit = debit + ...``. Old keys matter: moving a line to another
  account or date, unposting, or deleting takes its amount off the key it
  left. A key no line reaches any more is removed.
- A bulk ``query.update()`` / ``query.delete()`` on the journal tables
  bypasses the flush, so ``do_orm_execute`` rebuilds the active company's
  summary after such a statement.

Why a difference and not a fresh sum: each request is its own transaction
and cannot see another's uncommitted lines. Two users posting to Cash today
used to each sum the lines *they* could see and rewrite the row — the second
writer either hit the unique key (an error page, posting lost) or deadlocked.
Adding a difference commutes: the database queues the second writer on the
row and applies both, and the total includes every posting. Keys are written
in sorted order so two postings never take the same rows in opposite order.

Writes go through the session's Core connection, so they neither re-enter
the ORM hooks nor touch the audit trail, and they commit or roll back
(savepoints included) with the posting itself.
"""
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import and_, bindparam, event, inspect, select, text

from shared.extensions import db
from shared.models.gl_balance import GLDailyBalance
from shared.models.ledger import JournalEntry, JournalLine

T = GLDailyBalance.__table__
JE = JournalEntry.__table__
JL = JournalLine.__table__
ZERO = Decimal("0")


def _day(v):
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, str) and v:
        return datetime.fromisoformat(v[:19]).date() if len(v) > 10 else date.fromisoformat(v)
    return None


def _contributions(conn, entry_ids):
    """What the entries' posted lines add to each (company, account, label,
    day) key right now: {key: [debit, credit, line_count]}."""
    out = defaultdict(lambda: [ZERO, ZERO, 0])
    ids = sorted(entry_ids)
    for i in range(0, len(ids), 500):
        rows = conn.execute(
            select(JL.c.company_id, JE.c.company_id, JL.c.account_id,
                   JL.c.label_id, JE.c.entry_date, JL.c.debit, JL.c.credit)
            .select_from(JL.join(JE, JL.c.journal_entry_id == JE.c.id))
            .where(JE.c.id.in_(ids[i:i + 500]),
                   JE.c.is_posted == True)).all()  # noqa: E712
        for lc, ec, acc, lab, d, dr, cr in rows:
            day = _day(d)
            company = lc if lc is not None else ec
            if day is None or acc is None or company is None:
                continue
            a = out[(company, acc, lab or 0, day)]
            a[0] += Decimal(str(dr or 0))
            a[1] += Decimal(str(cr or 0))
            a[2] += 1
    return out


def _insert(conn):
    if conn.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    return insert(T)


KEY = ("company_id", "account_id", "label_id", "day")


def apply_delta(conn, delta):
    """Add {key: [debit, credit, line_count]} to the summary, atomically.

    One INSERT ... ON CONFLICT DO UPDATE adds to the row if it exists and
    creates it if not; a concurrent posting to the same key waits on the row
    and then adds its own difference on top. Rows are written in key order
    so two transactions always lock shared keys in the same order.
    """
    rows = [{"company_id": k[0], "account_id": k[1], "label_id": k[2], "day": k[3],
             "debit": v[0], "credit": v[1], "line_count": v[2]}
            for k, v in sorted(delta.items()) if v[0] or v[1] or v[2]]
    if not rows:
        return
    ins = _insert(conn)
    conn.execute(ins.on_conflict_do_update(
        index_elements=list(KEY),
        set_={"debit": T.c.debit + ins.excluded.debit,
              "credit": T.c.credit + ins.excluded.credit,
              "line_count": T.c.line_count + ins.excluded.line_count}), rows)
    conn.execute(
        T.delete().where(and_(T.c.company_id == bindparam("k_c"), T.c.account_id == bindparam("k_a"),
                              T.c.label_id == bindparam("k_l"), T.c.day == bindparam("k_d"),
                              T.c.line_count <= 0)),
        [{"k_c": r["company_id"], "k_a": r["account_id"], "k_l": r["label_id"],
          "k_d": r["day"]} for r in rows])


def rebuild(conn, company_id=None):
    """Recompute the whole summary (one company, or all) from journal_lines."""
    dialect = conn.dialect.name
    day = "CAST(e.entry_date AS DATE)" if dialect == "postgresql" else "date(e.entry_date)"
    comp = "COALESCE(jl.company_id, e.company_id)"
    where = "e.is_posted = :posted"
    params = {"posted": True}
    if company_id is not None:
        where += f" AND {comp} = :cid"
        params["cid"] = company_id
        conn.execute(T.delete().where(T.c.company_id == company_id))
    else:
        conn.execute(T.delete())
    conn.execute(text(
        "INSERT INTO gl_daily_balances (company_id, account_id, label_id, day, debit, credit, line_count) "
        f"SELECT {comp}, jl.account_id, COALESCE(jl.label_id, 0), {day}, "
        "COALESCE(SUM(jl.debit), 0), COALESCE(SUM(jl.credit), 0), COUNT(*) "
        "FROM journal_lines jl JOIN journal_entries e ON jl.journal_entry_id = e.id "
        f"WHERE {where} AND {comp} IS NOT NULL "
        f"GROUP BY {comp}, jl.account_id, COALESCE(jl.label_id, 0), {day} "
        # A posting committed between the DELETE and this SELECT has already
        # added its own row; the SELECT sees its lines, so overwrite it.
        "ON CONFLICT (company_id, account_id, label_id, day) DO UPDATE SET "
        "debit = excluded.debit, credit = excluded.credit, "
        "line_count = excluded.line_count"), params)


# ── session hooks ───────────────────────────────────────────────────────

def _touched_entry_ids(session):
    """Ids of existing entries the pending flush changes (including the one
    a moved line leaves), and new rows whose entry id the flush assigns."""
    ids, new = set(), []
    for o in list(session.new) + list(session.dirty) + list(session.deleted):
        if isinstance(o, JournalLine):
            if o.journal_entry_id:
                ids.add(o.journal_entry_id)
            else:
                new.append(o)
            if o in session.dirty:
                left = inspect(o).attrs.journal_entry_id.history.deleted
                ids.update(i for i in left if i)
        elif isinstance(o, JournalEntry):
            if o.id:
                ids.add(o.id)
            else:
                new.append(o)
    return ids, new


@event.listens_for(db.session, "before_flush")
def _gl_before_flush(session, flush_context, instances):
    ids, new = _touched_entry_ids(session)
    if not ids and not new:
        return
    session.info["gl_summary"] = {
        "ids": ids, "new": new,
        "before": _contributions(session.connection(), ids) if ids else {}}


@event.listens_for(db.session, "after_flush")
def _gl_after_flush(session, flush_context):
    pend = session.info.pop("gl_summary", None)
    if not pend:
        return
    ids = set(pend["ids"])
    for o in pend["new"]:
        eid = o.journal_entry_id if isinstance(o, JournalLine) else o.id
        if eid:
            ids.add(eid)
    conn = session.connection()
    after = _contributions(conn, ids) if ids else {}
    delta = defaultdict(lambda: [ZERO, ZERO, 0])
    for k, v in after.items():
        d = delta[k]
        d[0] += v[0]
        d[1] += v[1]
        d[2] += v[2]
    for k, v in pend["before"].items():
        d = delta[k]
        d[0] -= v[0]
        d[1] -= v[1]
        d[2] -= v[2]
    apply_delta(conn, delta)


@event.listens_for(db.session, "after_rollback")
def _gl_after_rollback(session):
    session.info.pop("gl_summary", None)


@event.listens_for(db.session, "do_orm_execute")
def _gl_bulk(state):
    if not (state.is_update or state.is_delete):
        return None
    mapper = state.bind_arguments.get("mapper")
    if mapper is None or mapper.class_ not in (JournalLine, JournalEntry):
        return None
    result = state.invoke_statement()
    from shared.tenancy import current_company_id
    rebuild(state.session.connection(), company_id=current_company_id())
    return result
