"""Executive Reports — receivables and payables, derived from the ledger.

The whole module is a READ of the general ledger. It writes no journal, holds
no balances of its own, and stores nothing but the list of accounts to watch:

* Every figure is recomputed from posted ``JournalLine`` rows on each request.
  Edit a voucher, unapprove it, delete it, and the numbers here follow on the
  next page load — there is no cached total to go stale.
* Which side a party appears on is decided by the sign of its balance, not by
  the account's type. A trade debtor sitting on a credit balance (an advance,
  an over-receipt) belongs in Payables and shows there. When the next voucher
  flips the sign, the party moves on its own.
* Removing every selection, or the whole module, would leave the ledger and
  every other report identical.

"Party" means one posting account: the entity subledger accounts created per
customer, supplier or employee already carry the party's name, so an account
is the finest grain the ledger can answer at.
"""

from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP

from shared.extensions import db
from shared.models.ledger import ChartOfAccount, JournalEntry, JournalLine
from shared.models.exec_report import ExecAccountSelection

CENT = Decimal("0.01")
# Balances below this are treated as settled: a party whose debits and credits
# cancel is not owed anything and is not a row worth showing.
TOLERANCE = Decimal("0.01")

RECEIVABLE = "receivable"
PAYABLE = "payable"

SIDE_META = {
    RECEIVABLE: {
        "title": "Receivables",
        "noun": "receivable",
        "party_word": "owes us",
        "empty": "No selected account is carrying a debit balance.",
    },
    PAYABLE: {
        "title": "Payables",
        "noun": "payable",
        "party_word": "we owe",
        "empty": "No selected account is carrying a credit balance.",
    },
}


def _q(value):
    if value is None:
        return Decimal("0.00")
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


def _f(value):
    return float(_q(value))


def _eod(dt):
    """End of the given day, so an "as of" date includes that day's postings."""
    if dt is None:
        return None
    if isinstance(dt, datetime):
        return dt.replace(hour=23, minute=59, second=59, microsecond=999999)
    return datetime.combine(dt, datetime.max.time())


# ── Selected accounts ───────────────────────────────────────────────────────

def _children_index():
    """``{parent_id: [child accounts]}`` over the whole chart."""
    index = {}
    for acct in ChartOfAccount.query.all():
        index.setdefault(acct.parent_id, []).append(acct)
    return index


def selections():
    """The raw picks, newest chart order first."""
    rows = (ExecAccountSelection.query
            .join(ChartOfAccount,
                  ExecAccountSelection.account_id == ChartOfAccount.id)
            .order_by(ChartOfAccount.code).all())
    return rows


def scope(as_dict=False):
    """Every account currently in scope, expanded through the tree.

    ``{account_id: (root_account_id, root_name)}`` — the root being the picked
    ancestor, so a row can say which control account it came from.
    """
    picks = selections()
    if not picks:
        return {} if as_dict else set()

    kids = _children_index()
    out = {}
    for pick in picks:
        root = pick.account
        if root is None:
            continue
        label = f"{root.code} · {root.name}"
        stack = [root]
        first = True
        while stack:
            node = stack.pop()
            # The pick itself is always in scope; descendants only when the
            # selection says so.
            if first or pick.include_children:
                out[int(node.id)] = (int(root.id), label)
            if pick.include_children:
                stack.extend(kids.get(node.id, []))
            first = False
    return out if as_dict else set(out)


def _balances(account_ids, as_of=None):
    """``{account_id: (debit_total, credit_total)}`` from posted journal lines.

    Posted lines only — an unapproved voucher has not moved anything, and a
    reversal is itself a posting, so unapproving a voucher nets its party back
    to zero here without anything having to be recalculated.
    """
    if not account_ids:
        return {}
    q = (db.session.query(
            JournalLine.account_id,
            db.func.coalesce(db.func.sum(JournalLine.debit), 0).label("dr"),
            db.func.coalesce(db.func.sum(JournalLine.credit), 0).label("cr"))
         .join(JournalEntry, JournalLine.journal_entry_id == JournalEntry.id)
         .filter(JournalEntry.is_posted == True,   # noqa: E712
                 JournalLine.account_id.in_(list(account_ids))))
    if as_of:
        q = q.filter(JournalEntry.entry_date <= _eod(as_of))
    q = q.group_by(JournalLine.account_id)
    return {int(r.account_id): (_q(r.dr), _q(r.cr)) for r in q.all()}


# ── The two registers ───────────────────────────────────────────────────────

def party_rows(side=None, as_of=None, search=None, group_id=None):
    """One row per account in scope that is carrying a balance.

    ``side`` filters to receivable / payable; omit it for both. Accounts that
    net to nothing are left out entirely — they are settled, not a debt.
    """
    in_scope = scope(as_dict=True)
    if not in_scope:
        return []
    balances = _balances(in_scope.keys(), as_of)
    if not balances:
        return []

    accounts = {int(a.id): a for a in ChartOfAccount.query.filter(
        ChartOfAccount.id.in_(list(balances.keys()))).all()}

    term = (search or "").strip().lower()
    rows = []
    for account_id, (dr, cr) in balances.items():
        acct = accounts.get(account_id)
        if acct is None:
            continue
        net = _q(dr - cr)
        if abs(net) < TOLERANCE:
            continue
        # The rule the whole module turns on: the sign decides the side, not
        # the account's type.
        row_side = RECEIVABLE if net > 0 else PAYABLE
        if side and row_side != side:
            continue
        root_id, root_label = in_scope.get(account_id, (None, ""))
        if group_id and root_id != int(group_id):
            continue
        if term and term not in acct.name.lower() and term not in acct.code.lower():
            continue
        rows.append({
            "account_id": account_id,
            "code": acct.code,
            "name": acct.name,
            "type": acct.type,
            "group_id": root_id,
            "group": root_label,
            "debit": _f(dr),
            "credit": _f(cr),
            "amount": _f(abs(net)),
            "side": row_side,
        })
    rows.sort(key=lambda r: r["amount"], reverse=True)
    return rows


def totals(rows):
    """The two figures the page leads with: how much, and across how many."""
    return {
        "amount": _f(sum(Decimal(str(r["amount"])) for r in rows)) if rows else 0.0,
        "parties": len(rows),
    }


def both_sides(as_of=None):
    """Totals for each side in one pass — for the module dashboard."""
    rows = party_rows(as_of=as_of)
    return {
        RECEIVABLE: totals([r for r in rows if r["side"] == RECEIVABLE]),
        PAYABLE: totals([r for r in rows if r["side"] == PAYABLE]),
    }


def groups():
    """The picked control accounts, for the register's group filter."""
    out = []
    for pick in selections():
        if pick.account is None:
            continue
        out.append({"id": int(pick.account.id),
                    "label": f"{pick.account.code} · {pick.account.name}"})
    return out


def account_ledger(account_id, as_of=None, limit=200):
    """The postings behind one party's balance, newest first.

    This is the "why" behind a row — and because it reads the same journal the
    balance came from, an edited or reversed voucher shows here too.
    """
    q = (db.session.query(JournalLine, JournalEntry)
         .join(JournalEntry, JournalLine.journal_entry_id == JournalEntry.id)
         .filter(JournalEntry.is_posted == True,   # noqa: E712
                 JournalLine.account_id == int(account_id)))
    if as_of:
        q = q.filter(JournalEntry.entry_date <= _eod(as_of))
    pairs = q.order_by(JournalEntry.entry_date.desc(),
                       JournalEntry.id.desc()).limit(limit).all()
    out = []
    for line, entry in pairs:
        out.append({
            "date": entry.entry_date,
            "voucher_type": entry.voucher_type,
            "voucher_number": entry.voucher_number,
            "description": line.description or entry.description or "",
            "debit": _f(line.debit),
            "credit": _f(line.credit),
        })
    return out


# ── Settings ────────────────────────────────────────────────────────────────

def selectable_accounts():
    """The whole chart, ordered for a tree picker, with selection state."""
    picked = {int(s.account_id): s for s in ExecAccountSelection.query.all()}
    accounts = ChartOfAccount.query.order_by(ChartOfAccount.code).all()
    out = []
    for a in accounts:
        sel = picked.get(int(a.id))
        out.append({
            "id": int(a.id),
            "code": a.code,
            "name": a.name,
            "type": a.type,
            "level": a.level or 5,
            "parent_id": a.parent_id,
            "selected": sel is not None,
            "include_children": bool(sel.include_children) if sel else True,
        })
    return out


def set_selection(account_ids, include_children_ids=None, user_id=None):
    """Replace the whole selection with the given accounts.

    Replace rather than merge: the settings screen posts the full picture, and
    a merge would make unticking an account impossible.
    """
    wanted = {int(i) for i in (account_ids or [])}
    deep = {int(i) for i in (include_children_ids or [])}

    existing = {int(s.account_id): s for s in ExecAccountSelection.query.all()}
    for account_id, row in existing.items():
        if account_id not in wanted:
            db.session.delete(row)
        else:
            row.include_children = account_id in deep
    for account_id in wanted - set(existing):
        db.session.add(ExecAccountSelection(
            account_id=account_id,
            include_children=account_id in deep,
            created_by=user_id))
    db.session.commit()
    return len(wanted)
