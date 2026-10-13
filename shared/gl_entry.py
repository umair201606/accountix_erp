"""The accounting entry behind a document, for the "Accounting entry" panel.

Every posting goes through ``ledger_utils.post_journal_entry`` keyed by
``(voucher_type, voucher_id)``. A document's ledger effect is therefore:

- its own journal(s): ``voucher_type`` = the document's code (SI, PI, SR, PR,
  JV/CPV/CRV/BPV/BRV, CONS, SCRAP, ADJ, FA-ACQ, PRL, ...);
- any related codes the document also posts under (a purchase invoice's PMT
  payment journal);
- cost re-adjustments booked later against it (``CADJ-<code>``, see
  ``shared/cost_adjustment.py``) when a back-dated document re-costed it.

Unapproving a document marks its journal un-posted rather than deleting it
(``reverse_journal_entry``), so earlier versions are returned separately as
history: they had no effect on the books, but they show what changed.

Read-only; everything is tenant-scoped by the normal query hook.
"""
from decimal import Decimal

from shared.models.ledger import ChartOfAccount, JournalEntry, JournalLine

# Codes a document also posts under besides its own (keyed by its own code).
RELATED = {"PI": ["PMT"], "FA-ACQ": ["FA-DISP"], "PRL": ["PRL-PAY"]}

TITLES = {
    "SI": "Sales invoice", "PI": "Purchase invoice", "SR": "Sales return",
    "PR": "Purchase return", "PMT": "Payment", "JV": "Journal voucher",
    "CPV": "Cash payment", "CRV": "Cash receipt", "BPV": "Bank payment",
    "BRV": "Bank receipt", "CONS": "Consumption", "SCRAP": "Scrap",
    "ADJ": "Stock adjustment", "FA-ACQ": "Asset acquisition",
    "FA-DEP": "Depreciation", "FA-DISP": "Asset disposal",
    "FA-CAP": "Capitalisation", "FA-TRF": "Transfer to inventory",
    "PRL": "Payroll", "PRL-PAY": "Salary payment", "OPENING": "Opening stock",
    "LOAN-DSB": "Staff loan paid out", "PF-WD": "PF withdrawal", "PF-LOAN": "PF loan",
    "PF-SETTLE": "PF settlement", "PF-PROFIT": "PF profit", "PF-ADJ": "PF adjustment",
}


def _q(v):
    return Decimal(str(v or 0))


def _entry_dict(e, entry_lines, accounts, labels):
    lines = []
    dr = cr = Decimal("0")
    # Debits first, then credits — the order an accountant reads a journal in.
    for ln in sorted(entry_lines, key=lambda l: (_q(l.credit) > 0, l.id)):
        a = accounts.get(ln.account_id)
        d, c = _q(ln.debit), _q(ln.credit)
        dr += d
        cr += c
        lines.append({
            "account_id": ln.account_id,
            "code": a.code if a else "",
            "name": a.name if a else f"Account #{ln.account_id}",
            "type": a.type if a else "",
            "description": ln.description or "",
            "label": labels.get(getattr(ln, "label_id", None)),
            "debit": float(d), "credit": float(c),
        })
    vtype = e.voucher_type or ""
    base = vtype[5:] if vtype.startswith("CADJ-") else vtype
    return {
        "id": e.id,
        "voucher_type": vtype,
        "kind": ("cost_adjustment" if vtype.startswith("CADJ-")
                 else "related" if base != vtype else "main"),
        "title": ("Cost re-adjustment" if vtype.startswith("CADJ-")
                  else TITLES.get(vtype, vtype)),
        "number": e.voucher_number,
        "date": e.entry_date.strftime("%Y-%m-%d") if e.entry_date else None,
        "description": e.description or "",
        "posted": bool(e.is_posted),
        "lines": lines,
        "total_debit": float(dr), "total_credit": float(cr),
        "balanced": abs(dr - cr) < Decimal("0.005"),
    }


def entries_for(voucher_type, voucher_id, also=()):
    """{"posted": [...], "history": [...]} for one document.

    ``also``: extra (voucher_type, voucher_id) pairs that belong to the same
    document but are keyed by another record (an asset's depreciation runs)."""
    from sqlalchemy import and_, or_
    voucher_type = (voucher_type or "").strip().upper()
    codes = [voucher_type, f"CADJ-{voucher_type}"] + RELATED.get(voucher_type, [])
    conds = [and_(JournalEntry.voucher_type.in_(codes),
                  JournalEntry.voucher_id == int(voucher_id))]
    extra_types = set()
    for t, i in also:
        conds.append(and_(JournalEntry.voucher_type == t, JournalEntry.voucher_id == int(i)))
        extra_types.add(t)
    rows = (JournalEntry.query.filter(or_(*conds))
            .order_by(JournalEntry.entry_date, JournalEntry.id).all())
    if not rows:
        return {"posted": [], "history": []}
    ids = [r.id for r in rows]
    lines = JournalLine.query.filter(JournalLine.journal_entry_id.in_(ids)).all()
    by_entry = {}
    for ln in lines:
        by_entry.setdefault(ln.journal_entry_id, []).append(ln)
    accounts = {a.id: a for a in ChartOfAccount.query.filter(
        ChartOfAccount.id.in_({l.account_id for l in lines})).all()} if lines else {}
    labels = {}
    label_ids = {getattr(l, "label_id", None) for l in lines} - {None}
    if label_ids:
        from shared.models.project_label import ProjectLabel
        labels = {p.id: p.name for p in ProjectLabel.query.filter(
            ProjectLabel.id.in_(label_ids)).all()}
    out = {"posted": [], "history": []}
    for r in rows:
        d = _entry_dict(r, by_entry.get(r.id, []), accounts, labels)
        if r.voucher_type in RELATED.get(voucher_type, []) or r.voucher_type in extra_types:
            d["kind"] = "related"
        (out["posted"] if r.is_posted else out["history"]).append(d)
    return out
