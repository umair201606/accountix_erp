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

from datetime import datetime, date
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
    parents = {int(a.parent_id) for a in accounts if a.parent_id is not None}
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
            # "Include sub-accounts" only means something on a parent — leaves
            # get no toggle at all, not a disabled one.
            "has_children": int(a.id) in parents,
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


# ── Aging ────────────────────────────────────────────────────────────────────
#
# Every open party balance is aged from its own posting layers by FIFO: on the
# receivable side debits build the stack and credits consume the oldest layer
# first; on the payable side credits build it and debits consume it. The layers
# still on the stack are what is actually owed, and their age in days drives
# the buckets, the weighted-average collection / payment period, and the oldest
# debt on each side.

# (key, label, min days, max days — None = unbounded)
BUCKET_DEFS = (
    ("current", "Current", 0, 30),
    ("d31", "31–60 days", 31, 60),
    ("d61", "61–90 days", 61, 90),
    ("d91", "90+ days", 91, None),
)


def _aging_side(parties):
    """Collapse the per-party FIFO stacks into one side's dashboard view."""
    total = _f(sum(Decimal(str(p["amount"])) for p in parties)) if parties else 0.0
    buckets = []
    for key, label, lo, hi in BUCKET_DEFS:
        amount = sum(
            amount for p in parties
            for days, amount in p["layers"]
            if lo <= days and (hi is None or days <= hi))
        buckets.append({
            "key": key,
            "label": label,
            "amount": _f(Decimal(str(amount))),
            "share": _f(Decimal(str(amount)) / Decimal(str(total)) * 100)
            if total else 0.0,
        })
    avg_days = (sum(p["avg_days"] * p["amount"] for p in parties) / total
                if total else 0.0)
    top = sorted(parties, key=lambda p: p["amount"], reverse=True)[:5]
    oldest = max(parties, key=lambda p: p["oldest_days"]) if parties else None
    return {
        "total": total,
        "parties": len(parties),
        "avg_days": float(avg_days),
        "oldest_days": oldest["oldest_days"] if oldest else 0,
        "oldest_name": oldest["name"] if oldest else "",
        "oldest_code": oldest["code"] if oldest else "",
        "oldest_account_id": oldest["account_id"] if oldest else None,
        "buckets": buckets,
        "top": [{
            "name": p["name"],
            "code": p["code"],
            "account_id": p["account_id"],
            "amount": p["amount"],
            "share": _f(Decimal(str(p["amount"])) / Decimal(str(total)) * 100)
            if total else 0.0,
        } for p in top],
    }


def _age_party(account_id, acct, rows, balance, as_of_day, side):
    """FIFO-age one account: returns the surviving layers, oldest day, and
    the weighted average age of what remains."""
    layers = []  # [entry_date, remaining amount] — oldest first
    add_idx, take_idx = (1, 2) if side == RECEIVABLE else (2, 1)
    for entry_date, dr, cr in rows:
        add = _q(dr if add_idx == 1 else cr)
        if add:
            layers.append([entry_date, add])
        take = _q(cr if take_idx == 2 else dr)
        while take and layers:
            if layers[0][1] <= take:
                take = _q(take - layers[0][1])
                layers.pop(0)
            else:
                layers[0][1] = _q(layers[0][1] - take)
                take = None
    if not layers:
        return None

    ages = []
    total = _q(balance)
    for entry_date, amount in layers:
        days = max((as_of_day - entry_date.date()).days, 0)
        ages.append((days, amount))
    avg_days = float(sum(d * float(a) for d, a in ages) / float(total))
    return {
        "account_id": account_id,
        "code": acct.code,
        "name": acct.name,
        "amount": _f(total),
        "avg_days": avg_days,
        "oldest_days": max(d for d, _ in ages),
        "layers": ages,
    }


def aging(as_of=None):
    """Age every open balance in scope, one side against the other.

    Returns ``{receivable: …, payable: …}``, each a side summary: total,
    party count, weighted-average days, oldest debt (days + party), the four
    buckets, and the five largest parties with their share of the total.
    """
    in_scope = scope(as_dict=True)
    if not in_scope:
        return {RECEIVABLE: _aging_side([]), PAYABLE: _aging_side([])}
    as_of_day = as_of.date() if isinstance(as_of, datetime) else (
        as_of if isinstance(as_of, date) else datetime.utcnow().date())

    q = (db.session.query(JournalLine, JournalEntry)
         .join(JournalEntry, JournalLine.journal_entry_id == JournalEntry.id)
         .filter(JournalEntry.is_posted == True,   # noqa: E712
                 JournalLine.account_id.in_(list(in_scope.keys()))))
    if as_of:
        q = q.filter(JournalEntry.entry_date <= _eod(as_of))
    postings = {}
    for line, entry in q.order_by(JournalEntry.entry_date,
                                  JournalEntry.id).all():
        postings.setdefault(int(line.account_id), []).append(
            (entry.entry_date, line.debit, line.credit))

    accounts = {int(a.id): a for a in ChartOfAccount.query.filter(
        ChartOfAccount.id.in_(list(postings.keys()))).all()}
    receivable, payable = [], []
    for account_id, rows in postings.items():
        net = _q(sum(dr - cr for _, dr, cr in rows))
        if abs(net) < TOLERANCE:
            continue
        acct = accounts.get(account_id)
        if acct is None:
            continue
        side = RECEIVABLE if net > 0 else PAYABLE
        party = _age_party(account_id, acct, rows, abs(net), as_of_day, side)
        if party:
            (receivable if side == RECEIVABLE else payable).append(party)
    return {
        RECEIVABLE: _aging_side(receivable),
        PAYABLE: _aging_side(payable),
    }


# ── Liquidity snapshot ───────────────────────────────────────────────────────
#
# Company-wide (the whole chart, not just the exec scope) and so available
# even before any account is picked. Current Assets / Current Liabilities are
# located by their standard names at level 2, inventory by the Inventories
# subtree, and every figure is a plain balance — accumulated depreciation is a
# credit on an asset, which nets itself out with no special handling.

def _subtree_ids(kids_index, node_id):
    out, stack = [], [node_id]
    while stack:
        nid = stack.pop()
        out.append(nid)
        stack.extend(int(child.id) for child in kids_index.get(nid, []))
    return out


def _named_child(kids_index, accounts, parent_id, name):
    for child in kids_index.get(parent_id, []):
        acct = accounts.get(int(child.id))
        if acct is not None and acct.name == name:
            return acct
    return None


def liquidity(as_of=None):
    """Net assets plus current / quick ratios, derived from the chart.

    Returns ``None``-safe fields: the ratios are ``None`` when current
    liabilities are zero (no comparison is meaningful), net assets when the
    chart has no level-1 asset and liability roots.
    """
    assets_root = ChartOfAccount.query.filter(
        ChartOfAccount.type == "asset",
        ChartOfAccount.level == 1).first()
    liab_root = ChartOfAccount.query.filter(
        ChartOfAccount.type == "liability",
        ChartOfAccount.level == 1).first()
    if assets_root is None or liab_root is None:
        return {
            "net_assets": None, "total_assets": 0.0, "total_liabilities": 0.0,
            "current_assets": 0.0, "current_liabilities": 0.0,
            "inventory": 0.0, "current_ratio": None, "quick_ratio": None,
        }

    kids = _children_index()
    all_ids = (_subtree_ids(kids, int(assets_root.id))
               + _subtree_ids(kids, int(liab_root.id)))
    balances = _balances(all_ids, as_of)
    accounts = {int(a.id): a for a in ChartOfAccount.query.filter(
        ChartOfAccount.id.in_(all_ids)).all()}

    def _net(nid):
        dr, cr = balances.get(nid, (Decimal("0.00"), Decimal("0.00")))
        return _q(dr - cr)

    total_assets = _f(sum(_net(nid) for nid, acct in accounts.items()
                          if acct.type == "asset"))
    total_liabilities = _f(-sum(_net(nid) for nid, acct in accounts.items()
                                if acct.type == "liability"))

    current = {}
    for root, ttype, cname in (
            (assets_root, "asset", "Current Assets"),
            (liab_root, "liability", "Current Liabilities")):
        node = _named_child(kids, accounts, int(root.id), cname)
        nids = _subtree_ids(kids, int(node.id)) if node else []
        current[ttype] = _f(sum(_net(nid) for nid in nids))

    inventory = 0.0
    inv_node = next((a for a in accounts.values()
                     if a.name == "Inventories"), None)
    if inv_node is not None:
        nids = _subtree_ids(kids, int(inv_node.id))
        inventory = _f(sum(_net(nid) for nid in nids))

    ca = current["asset"]
    cl = current["liability"]
    # A negative "liability" balance is really an asset (overpaid, drifted
    # payroll) — a ratio against it would be a meaningless negative number, so
    # the dashboard shows "—" instead.
    ratios = cl > 0
    return {
        "net_assets": _f(total_assets - total_liabilities),
        "total_assets": total_assets,
        "total_liabilities": total_liabilities,
        "current_assets": ca,
        "current_liabilities": cl,
        "inventory": inventory,
        "current_ratio": _f(ca / cl) if ratios else None,
        "quick_ratio": _f((ca - inventory) / cl) if ratios else None,
    }
