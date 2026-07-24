from decimal import Decimal
from shared.extensions import db
from shared.models.ledger import JournalEntry, JournalLine, ChartOfAccount


# ── Canonical posting accounts ───────────────────────────────────────────────
# Postings must never hardcode a chart-of-accounts *code*: they resolve a
# semantic ROLE to the canonical level-5 operational account in the fixed
# segmented chart (shared/coa.py). Legacy alternates cover databases that have
# not run the startup migration yet (flat 1000-series or old 111-series).
# role -> (name, type, [legacy_alternate_codes]); canonical code from ROLE_CODES.
POSTING_ACCOUNTS = {
    "cash":              ("Main Cash", "asset", ["1000", "111"]),
    "ar":                ("Trade Debtors — General", "asset", ["1100", "112"]),
    "inventory":         ("Stock — General", "asset", ["1200", "113"]),
    "fixed_assets":      ("Fixed Assets — General", "asset", ["1300", "121"]),
    "input_tax":         ("Input Sales Tax", "asset", ["1400", "114"]),
    "ap":                ("Trade Creditors — General", "liability", ["2000", "211"]),
    "accrued":           ("Accrued Expenses — General", "liability", ["2100", "212"]),
    "loans":             ("Long-term Loans — General", "liability", ["2200", "221"]),
    "wht_payable":       ("WHT Payable", "liability", ["6400", "214"]),
    "sales_tax_payable": ("Output Sales Tax", "liability", ["6500", "213"]),
    "further_tax_payable": ("Further Tax Payable", "liability", []),
    "revenue":           ("Sales — General", "revenue", ["4000", "411"]),
    "cogs":              ("Cost of Goods Sold", "expense", ["5000", "511"]),
    "inventory_variance": ("Inventory Cost Variance", "expense", ["5900"]),
    "sales_returns":     ("Sales Returns — General", "revenue", ["4100"]),
}


def posting_account(role):
    """Resolve a semantic posting role to a ChartOfAccount, creating it if the
    canonical and all legacy codes are absent. Never returns None."""
    from shared.coa import ROLE_CODES
    name, type_, alts = POSTING_ACCOUNTS[role]
    code = ROLE_CODES[role]
    acct = ChartOfAccount.query.filter_by(code=code).first()
    if acct:
        return acct
    for alt in alts:
        acct = ChartOfAccount.query.filter_by(code=alt).first()
        if acct:
            return acct
    return get_or_create_account(code, name, type_)


def post_journal_entry(voucher_type, voucher_id, voucher_number, description,
                       lines, entry_date=None, created_by=1):
    from datetime import datetime
    # Defence in depth: a double-entry system must never persist an unbalanced
    # journal. Refuse rather than corrupt the ledger (this is what let an
    # unbalanced cash/bank voucher through before).
    total_debit = sum(Decimal(str(l.get("debit", 0) or 0)) for l in lines)
    total_credit = sum(Decimal(str(l.get("credit", 0) or 0)) for l in lines)
    if abs(total_debit - total_credit) > Decimal("0.01"):
        raise ValueError(
            f"Unbalanced journal for {voucher_number}: "
            f"debits {total_debit} != credits {total_credit}"
        )
    # Aggregating accounts (levels 1-4) only roll up child balances — a line
    # posted there would double-count against its children in every report.
    for l in lines:
        acct = ChartOfAccount.query.get(l["account_id"])
        if acct is None:
            raise ValueError(f"Journal for {voucher_number} references "
                             f"unknown account id {l['account_id']}")
        if not acct.is_postable:
            raise ValueError(
                f"Cannot post to aggregating account {acct.code} {acct.name} "
                f"(level {acct.level}); post to a level-5 operational account."
            )
    # A closed period's figures are final — see shared/periods.py. Checked
    # after the balance/postability checks so a malformed entry still reports
    # the more specific problem first.
    from shared.periods import require_open_period
    entry_date = entry_date or datetime.utcnow()
    require_open_period(entry_date, action=f"post {voucher_number} into")

    je = JournalEntry(
        voucher_type=voucher_type,
        voucher_id=voucher_id,
        voucher_number=voucher_number,
        description=description,
        entry_date=entry_date,
        created_by=created_by
    )
    db.session.add(je)
    db.session.flush()

    for line in lines:
        jl = JournalLine(
            journal_entry_id=je.id,
            account_id=line["account_id"],
            debit=Decimal(str(line.get("debit", 0))),
            credit=Decimal(str(line.get("credit", 0))),
            description=line.get("description", "")
        )
        db.session.add(jl)
    db.session.flush()
    return je


def reverse_journal_entry(voucher_type, voucher_id, created_by=1):
    """Un-post the active journal entries for a voucher (used on unapprove).

    Every balance/ledger query filters ``is_posted == True``, so flipping the
    flag removes the entry's effect and returns the affected account balances to
    zero. The rows are retained (not deleted) as an audit trail.

    The previous implementation ALSO created an equal-and-opposite reversal
    entry with ``is_posted=True``. Because the original was simultaneously set
    ``is_posted=False`` (i.e. excluded from every report), only the reversal was
    counted — which *inverted* each affected account's balance instead of
    cancelling it. Marking the original un-posted is sufficient and correct.

    ``created_by`` is accepted for call-site compatibility; it is unused now
    that no new entry is created.
    """
    from shared.periods import require_open_period
    entries = JournalEntry.query.filter_by(
        voucher_type=voucher_type, voucher_id=voucher_id, is_posted=True
    ).all()
    # Un-posting rewrites the period the entry SITS IN, not today's: flipping
    # is_posted removes it from every balance query retroactively. If that
    # period is closed, its figures are final and this is exactly the edit the
    # close exists to prevent.
    for entry in entries:
        require_open_period(entry.entry_date,
                            action=f"un-post {entry.voucher_number} from")
    for entry in entries:
        entry.is_posted = False
    db.session.flush()


def post_variance_journal(variances, voucher_number, created_by=1):
    """Book the value a reversal left with no purchase to back it.

    ``variances`` is {product_id: amount} from
    ``costing.reverse_voucher_stock(..., allow_variance=True)``. A positive
    amount is value the books still carry but no stock backs, so it is expensed
    to Inventory Cost Variance and taken off Inventory.

    This is what lets a consumed receipt be withdrawn at all: the cost its issue
    posted stays frozen (it was reported, and conveyed to someone), while the
    difference lands in one visible account instead of quietly untying the
    inventory control account from COGS.
    """
    total = sum(Decimal(str(v)) for v in variances.values())
    if not total:
        return None
    variance_acct = posting_account("inventory_variance")
    inventory_acct = posting_account("inventory")
    amount = abs(total)
    write_off = total > 0
    return post_journal_entry(
        voucher_type="VAR", voucher_id=0,
        voucher_number=f"VAR-{voucher_number}",
        description=(f"Inventory cost variance on reversal of {voucher_number}: "
                     f"value with no remaining purchase to back it"),
        lines=[
            {"account_id": variance_acct.id if write_off else inventory_acct.id,
             "debit": amount, "credit": 0,
             "description": f"Reversal of {voucher_number}"},
            {"account_id": inventory_acct.id if write_off else variance_acct.id,
             "debit": 0, "credit": amount,
             "description": f"Reversal of {voucher_number}"},
        ],
        created_by=created_by,
    )


# ── Per-entity level-5 subledger accounts ───────────────────────────────────
# Every supplier / customer / product / employee / loan gets its own level-5
# operational account under the matching level-4 control account, so postings
# hit the entity's own ledger and its name shows up in reports.


def create_entity_account(kind, entity_id, name):
    """Create (or fetch) the level-5 ledger account for a business entity.

    Idempotent: keyed on a deterministic code — the level-4 parent's code plus
    a segment of ``entity_id + 100`` (0001-0099 is reserved for seeded
    defaults). If the entity was renamed, the account name is kept in sync so
    ledgers always show the current name.
    """
    from shared.coa import ENTITY_PARENT_CODES, ENTITY_ID_OFFSET
    parent = ChartOfAccount.query.filter_by(
        code=ENTITY_PARENT_CODES[kind]).first()
    if parent is None:
        from shared.coa import seed_fixed_tree
        seed_fixed_tree(levels=(1, 2, 3, 4))
        parent = ChartOfAccount.query.filter_by(
            code=ENTITY_PARENT_CODES[kind]).first()
    code = f"{parent.code}-{int(entity_id) + ENTITY_ID_OFFSET:04d}"
    acct = ChartOfAccount.query.filter_by(code=code).first()
    if not acct:
        acct = ChartOfAccount(code=code, name=name, type=parent.type,
                              parent_id=parent.id, level=5)
        db.session.add(acct)
        db.session.flush()
    elif acct.name != name:
        acct.name = name
    return acct


def party_account(kind, entity_id, name, override_account_id=None):
    """The ledger account an invoice's AR/AP side posts to.

    Priority: an explicit override (settings may allow any postable account as
    the counterparty) -> the entity's own subledger account -> the role's
    "General" control account. Posting to the entity account is what makes
    per-customer / per-supplier ledgers show real balances.
    """
    if override_account_id:
        acct = ChartOfAccount.query.get(override_account_id)
        if acct is not None and acct.is_postable:
            return acct
    if entity_id:
        return create_entity_account(kind, entity_id, name or f"{kind} #{entity_id}")
    return posting_account("ar" if kind == "customer" else "ap")


def get_account_by_code(code):
    return ChartOfAccount.query.filter_by(code=code).first()


def get_or_create_account(code, name, type_, parent_code=None):
    """Fetch an operational account by code, creating it under a sensible
    level-4 parent if absent.

    Legacy flat codes (``5700``, ``2121``, ...) from older call sites are
    translated to their canonical slot in the fixed segmented chart, so both
    pre- and post-migration databases resolve to the same account.
    """
    from shared.coa import (LEGACY_CODE_MAP, CATCHALL_PARENT_CODES,
                            is_segmented, parent_code_of, next_child_code,
                            seed_fixed_tree)
    code = str(code)
    acct = ChartOfAccount.query.filter_by(code=code).first()
    if acct:
        return acct
    mapped = LEGACY_CODE_MAP.get(code)
    if mapped:
        acct = ChartOfAccount.query.filter_by(code=mapped).first()
        if acct:
            return acct
        code = mapped
    parent = None
    if is_segmented(code):
        parent = ChartOfAccount.query.filter_by(code=parent_code_of(code)).first()
    if parent is None and parent_code:
        legacy_parent = ChartOfAccount.query.filter_by(code=str(parent_code)).first()
        if legacy_parent is not None and legacy_parent.level == 4:
            parent = legacy_parent
    if parent is None:
        # Fall back to the type's catch-all level-4 head in the fixed tree.
        seed_fixed_tree(levels=(1, 2, 3, 4))
        parent = ChartOfAccount.query.filter_by(
            code=CATCHALL_PARENT_CODES.get(type_, "5-04-01-01")).first()
        code = next_child_code(parent)
    acct = ChartOfAccount(code=code, name=name, type=type_,
                          parent_id=parent.id, level=5)
    db.session.add(acct)
    db.session.flush()
    return acct
