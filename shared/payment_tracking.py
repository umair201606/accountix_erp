"""Invoice payment tracking — the engine behind the Invoicing > Tracking pages.

WHAT THIS IS NOT: a posting module. Nothing in this file writes, reverses or
touches a JournalEntry, a JournalLine or a ChartOfAccount balance. Money reaches
the ledger exactly once, when the CPV / CRV / BPV / BRV / JV that carries it is
approved. Assigning that money to an invoice is bookkeeping *about* documents,
not a second transaction — so the trial balance, the ledger and every financial
statement read the same before and after any allocation made here.

WHAT IT DOES:

* Reads approved accounting vouchers and picks out the lines that moved a
  customer's or a supplier's balance — those, and only those, are the receipts
  and payments the feed shows (a line hitting Rent or Salaries is not a
  settlement of anything and never appears).
* Tracks how much of each such line has been assigned to invoices, so a receipt
  can be split across several invoices and an invoice can be settled by several
  receipts.
* Derives each invoice's settled / outstanding figures and its age, and keeps
  the invoice's own ``paid_amount`` / ``payment_status`` columns in step so the
  rest of the app (invoice lists, FBR payload, dashboards) agrees with the
  tracker. Those two columns are document status, not ledger figures.

Party identification: an invoice posts its AR/AP side to the party's own level-5
subledger account (``shared/ledger_utils.party_account``), whose code is the
level-4 parent plus ``entity_id + ENTITY_ID_OFFSET``. That mapping is invertible,
which is what lets a voucher line be traced back to the customer or supplier it
settled. Invoices posted against an explicit override account contribute that
account to the same index.
"""

from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
import re

from shared.extensions import db
from shared.models.accounting_voucher import AccountingVoucher, AccountingVoucherLine
from shared.models.ledger import ChartOfAccount
from shared.models.payment_allocation import PaymentAllocation

CENT = Decimal("0.01")
TOLERANCE = Decimal("0.01")

# A receipt from a customer credits the customer; a payment to a supplier debits
# the supplier. Both can arrive on any voucher type that can carry a party line.
RECEIPT_VOUCHERS = ("CRV", "BRV", "JV")
PAYMENT_VOUCHERS = ("CPV", "BPV", "JV")

SALES, PURCHASE = "SI", "PI"
# A settlement that deliberately points at no invoice — an advance, a rounding
# difference, a receipt against something that was never invoiced. It is stored
# as an allocation whose doc_type is this and whose doc_id is 0, so it closes
# the line in every "how much is still loose?" query without ever reaching an
# invoice: allocated_by_doc() filters on SI/PI and can never see it.
NOT_APPLICABLE = "NA"
NA_DOC_ID = 0

DOC_FOR_PARTY = {"customer": SALES, "supplier": PURCHASE}
PARTY_FOR_DOC = {SALES: "customer", PURCHASE: "supplier"}
DOC_LABELS = {SALES: "Sales invoice", PURCHASE: "Purchase invoice"}

# How much of a receipt/payment has been accounted for. "assigned" and "forced"
# are both "fully dealt with", and the split between them is the point: the
# first was matched to invoices, the second was closed by a person who said no
# invoice applies. Keeping them apart is what stops a forced close from looking
# like a clean match in the feed.
ASSIGN_STATES = ("unassigned", "partial", "assigned", "forced", "review")
ASSIGN_STATE_LABELS = {
    "unassigned": "Unassigned",
    "partial": "Partially assigned",
    "assigned": "Assigned",
    "forced": "Force-closed (no invoice)",
    "review": "Needs re-assignment",
}
# What the feed shows when nobody has picked a filter: everything that still
# needs a decision, including lines whose voucher was edited under an existing
# assignment.
DEFAULT_ASSIGN_STATES = ("unassigned", "partial", "review")
PAY_STATES = ("unpaid", "partial", "paid")
# Ageing buckets, in days past due. Chosen to match the standard AR/AP ageing
# report so the tracker and the ledger reports bucket the same balance the same.
AGE_BUCKETS = [
    ("not_due", "Not due", None, None),
    ("d1_30", "1 – 30 days", 1, 30),
    ("d31_60", "31 – 60 days", 31, 60),
    ("d61_90", "61 – 90 days", 61, 90),
    ("d90_plus", "90+ days", 91, None),
]


def _q(value):
    """Money, rounded to cents. Accepts float / str / Decimal / None."""
    if value is None:
        return Decimal("0.00")
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


def _f(value):
    return float(_q(value))


# ── Party identification ────────────────────────────────────────────────────

def _entity_codes(kind, ids):
    from shared.coa import ENTITY_PARENT_CODES, ENTITY_ID_OFFSET
    parent = ENTITY_PARENT_CODES[kind]
    return {f"{parent}-{int(i) + ENTITY_ID_OFFSET:04d}": int(i) for i in ids}


def party_account_index():
    """``{account_id: (party_type, party_id, party_name)}`` for every account a
    customer or supplier settles through.

    Two sources, in this order of trust:

    1. The deterministic subledger code for each customer / supplier. This is
       where ``party_account()`` posts by default and covers the normal flow.
    2. ``party_account_id`` overrides actually used on approved invoices, for
       companies whose settings let an invoice name its own counterparty
       account. An account used by more than one party is dropped rather than
       guessed — an ambiguous account cannot identify a payer.
    """
    from inventory_app.models.customer import InvCustomer
    from inventory_app.models.supplier import InvSupplier
    from inventory_app.models.invoice import InvInvoice
    from inventory_app.models.purchase_invoice import InvPurchaseInvoice

    customers = {c.id: c.name for c in InvCustomer.query.all()}
    suppliers = {s.id: s.name for s in InvSupplier.query.all()}

    index = {}
    for kind, names in (("customer", customers), ("supplier", suppliers)):
        if not names:
            continue
        code_map = _entity_codes(kind, names.keys())
        rows = ChartOfAccount.query.filter(
            ChartOfAccount.code.in_(list(code_map.keys()))).all()
        for acct in rows:
            pid = code_map.get(acct.code)
            if pid is not None:
                index[acct.id] = (kind, pid, names.get(pid) or acct.name)

    # Override accounts. Collected per account so a shared one can be spotted
    # and discarded instead of attributing another party's money to whoever the
    # last query happened to return.
    seen = {}
    override_rows = [
        ("customer", customers,
         db.session.query(InvInvoice.party_account_id, InvInvoice.customer_id)
         .filter(InvInvoice.party_account_id.isnot(None)).distinct().all()),
        ("supplier", suppliers,
         db.session.query(InvPurchaseInvoice.party_account_id,
                          InvPurchaseInvoice.supplier_id)
         .filter(InvPurchaseInvoice.party_account_id.isnot(None)).distinct().all()),
    ]
    for kind, names, rows in override_rows:
        for account_id, party_id in rows:
            if account_id is None or party_id is None:
                continue
            key = (kind, int(party_id))
            seen.setdefault(int(account_id), set()).add(key)
    for account_id, keys in seen.items():
        if account_id in index or len(keys) != 1:
            continue
        kind, party_id = next(iter(keys))
        names = customers if kind == "customer" else suppliers
        index[account_id] = (kind, party_id, names.get(party_id) or "")
    return index


# ── Due dates and ageing ────────────────────────────────────────────────────

_TERM_DAYS = re.compile(r"(\d+)")


def terms_days(text):
    """Credit days read out of a free-text payment term ("Net 30", "30 days").

    Returns None when there is no number to read, which the callers treat as
    "due on issue" rather than inventing a grace period.
    """
    if not text:
        return None
    m = _TERM_DAYS.search(str(text))
    if not m:
        return None
    try:
        days = int(m.group(1))
    except ValueError:
        return None
    return days if 0 <= days <= 3650 else None


def _as_date(value):
    if value is None:
        return None
    return value.date() if isinstance(value, datetime) else value


def _fmt_date(value):
    return value.strftime("%d %b %Y") if value else ""


def effective_due_date(doc_type, inv, party):
    """When the invoice falls due.

    Sales invoices carry an explicit ``due_date``; purchase invoices have no
    such column, so the supplier's payment terms are applied to the invoice
    date. With neither, the document is due the day it was issued.
    """
    explicit = _as_date(getattr(inv, "due_date", None))
    if explicit:
        return explicit
    issued = _as_date(inv.invoice_date) or _as_date(inv.created_at)
    if issued is None:
        return None
    days = terms_days(getattr(party, "payment_terms", None) if party else None)
    return issued + timedelta(days=days) if days else issued


def overdue_days(due, as_of=None):
    """Days past due, 0 when not yet due (never negative)."""
    if due is None:
        return 0
    as_of = as_of or date.today()
    return max(0, (as_of - due).days)


def age_bucket(days):
    if days <= 0:
        return "not_due"
    for key, _label, lo, hi in AGE_BUCKETS:
        if lo is None:
            continue
        if days >= lo and (hi is None or days <= hi):
            return key
    return "d90_plus"


def bucket_label(key):
    for k, label, _lo, _hi in AGE_BUCKETS:
        if k == key:
            return label
    return key


# ── Allocation aggregates ───────────────────────────────────────────────────

def settlement_lines(line_ids=None):
    """``{line_id: (party_type, party_id)}`` for voucher lines that are, right
    now, an assignable receipt or payment.

    A line drops out of here the moment its voucher is edited away from being
    one: unapproved, the party line deleted, or its account repointed at a
    different customer. That is what makes the tracker self-correct.
    """
    index = party_account_index()
    if not index:
        return {}
    q = _line_query().filter(
        AccountingVoucherLine.account_id.in_(list(index.keys())))
    if line_ids is not None:
        if not line_ids:
            return {}
        q = q.filter(AccountingVoucherLine.id.in_([int(i) for i in line_ids]))
    out = {}
    for line, voucher in q.all():
        party = index.get(line.account_id)
        if party is None or _flow_of(party[0], line, voucher) is None:
            continue
        out[int(line.id)] = (party[0], party[1])
    return out


def _live_doc_ids(doc_type, doc_ids):
    """Of the given invoices, the ones that still exist and are still approved."""
    if not doc_ids:
        return set()
    model = _doc_model(doc_type)
    rows = _approved_filter(doc_type, model.query).filter(
        model.id.in_([int(d) for d in doc_ids])).all()
    return {int(i.id) for i in rows}


def classify_allocations(allocs):
    """Split allocations into the ones still backed on both sides and the rest.

    An allocation is a claim that *this money* settled *that invoice*. Either
    half can be edited out from under it after the fact — the voucher line can
    lose its party or its approval, the invoice can be unapproved or deleted.
    When that happens the claim is stale and must stop counting, so the invoice
    falls back to unpaid/partially paid and the money returns to the feed.
    """
    allocs = list(allocs)
    if not allocs:
        return [], []
    lines = settlement_lines({a.voucher_line_id for a in allocs})
    live_docs = {}
    for doc_type in (SALES, PURCHASE):
        live_docs[doc_type] = _live_doc_ids(
            doc_type, {a.doc_id for a in allocs if a.doc_type == doc_type})

    live, stale = [], []
    for a in allocs:
        party = lines.get(int(a.voucher_line_id))
        if party is None:
            stale.append((a, "The voucher no longer carries this "
                             f"{a.party_type} as an approved settlement."))
        elif party != (a.party_type, int(a.party_id)):
            stale.append((a, "The voucher line now belongs to a different "
                             "customer or supplier."))
        elif a.doc_type == NOT_APPLICABLE:
            live.append(a)          # points at no document by design
        elif int(a.doc_id) not in live_docs.get(a.doc_type, set()):
            stale.append((a, "The invoice is no longer approved, or was "
                             "deleted."))
        else:
            live.append(a)
    return live, stale


def _live_allocations(**filters):
    q = PaymentAllocation.query.filter_by(**filters) if filters \
        else PaymentAllocation.query
    return classify_allocations(q.all())[0]


def allocated_by_line(line_ids=None):
    q = PaymentAllocation.query
    if line_ids is not None:
        if not line_ids:
            return {}
        q = q.filter(PaymentAllocation.voucher_line_id.in_(
            [int(i) for i in line_ids]))
    out = {}
    for a in classify_allocations(q.all())[0]:
        key = int(a.voucher_line_id)
        out[key] = _q(out.get(key, Decimal("0")) + _q(a.amount))
    return out


def forced_by_line(line_ids=None):
    """``{line_id: amount}`` closed without an invoice. Subset of
    ``allocated_by_line`` — a forced amount still counts as accounted for."""
    q = PaymentAllocation.query.filter(
        PaymentAllocation.doc_type == NOT_APPLICABLE)
    if line_ids is not None:
        if not line_ids:
            return {}
        q = q.filter(PaymentAllocation.voucher_line_id.in_(
            [int(i) for i in line_ids]))
    out = {}
    for a in classify_allocations(q.all())[0]:
        key = int(a.voucher_line_id)
        out[key] = _q(out.get(key, Decimal("0")) + _q(a.amount))
    return out


def forced_note_by_line(line_ids=None):
    """``{line_id: reason}`` — why each force-closed line was closed."""
    q = PaymentAllocation.query.filter_by(doc_type=NOT_APPLICABLE)
    if line_ids is not None:
        if not line_ids:
            return {}
        q = q.filter(PaymentAllocation.voucher_line_id.in_(list(line_ids)))
    return {int(a.voucher_line_id): (a.note or "") for a in q.all()}


def allocated_by_doc(doc_type, doc_ids=None):
    q = PaymentAllocation.query.filter(PaymentAllocation.doc_type == doc_type)
    if doc_ids is not None:
        if not doc_ids:
            return {}
        q = q.filter(PaymentAllocation.doc_id.in_([int(d) for d in doc_ids]))
    out = {}
    for a in classify_allocations(q.all())[0]:
        key = int(a.doc_id)
        out[key] = _q(out.get(key, Decimal("0")) + _q(a.amount))
    return out


def assign_state(amount, assigned, forced=Decimal("0")):
    """How a receipt/payment stands.

    "review" comes first because it is a contradiction, not a status: more is
    assigned than the line now carries, which only happens when the voucher was
    edited downwards after the fact. The old split is kept and shown so it can
    be re-cut against the new amount, rather than being silently discarded.

    ``forced`` then wins over "assigned" whenever any part of the line was
    closed without an invoice, including the common case of a real invoice
    match plus a forced remainder — the row is finished, but a person decided
    the rest, and the feed has to keep saying so.
    """
    forced = _q(forced)
    if assigned > amount + TOLERANCE:
        return "review"
    if forced > TOLERANCE:
        return "forced"
    if assigned <= TOLERANCE:
        return "unassigned"
    if assigned >= amount - TOLERANCE:
        return "assigned"
    return "partial"


def pay_state(total, settled):
    if settled <= TOLERANCE:
        return "unpaid"
    if settled >= total - TOLERANCE:
        return "paid"
    return "partial"


# ── Documents ───────────────────────────────────────────────────────────────

def _doc_model(doc_type):
    if doc_type == SALES:
        from inventory_app.models.invoice import InvInvoice
        return InvInvoice
    if doc_type == PURCHASE:
        from inventory_app.models.purchase_invoice import InvPurchaseInvoice
        return InvPurchaseInvoice
    # Explicit, because NOT_APPLICABLE rows point at no document at all and a
    # silent fall-through here would hand them the purchase-invoice table.
    raise ValueError(f"{doc_type!r} is not an invoice type")


def _approved_filter(doc_type, query):
    """Only posted invoices are settleable — an unapproved one owes nothing yet."""
    model = _doc_model(doc_type)
    if doc_type == SALES:
        return query.filter(model.voucher_status == "approved")
    return query.filter(model.status == "approved")


def _party_of(doc_type, inv):
    return inv.customer if doc_type == SALES else inv.supplier


def _party_id_of(doc_type, inv):
    return inv.customer_id if doc_type == SALES else inv.supplier_id


def _doc_dict(doc_type, inv, settled, as_of=None):
    party = _party_of(doc_type, inv)
    total = _q(inv.total_amount)
    settled = _q(settled)
    outstanding = _q(total - settled)
    due = effective_due_date(doc_type, inv, party)
    days = overdue_days(due, as_of) if outstanding > TOLERANCE else 0
    state = pay_state(total, settled)
    return {
        "doc_type": doc_type,
        "id": inv.id,
        "number": inv.invoice_number,
        "voucher_number": inv.voucher_number,
        "date": _as_date(inv.invoice_date) or _as_date(inv.created_at),
        "due_date": due,
        # Pre-formatted twins for the JSON callers: jsonify would render a date
        # as an HTTP timestamp, which is not what a compact table cell wants.
        "date_str": _fmt_date(_as_date(inv.invoice_date) or _as_date(inv.created_at)),
        "due_str": _fmt_date(due),
        "party_type": PARTY_FOR_DOC[doc_type],
        "party_id": _party_id_of(doc_type, inv),
        "party_name": party.name if party else "—",
        "total": _f(total),
        "settled": _f(settled),
        "outstanding": _f(outstanding),
        "progress": (float(settled / total * 100) if total > 0 else 0.0),
        "pay_state": state,
        "overdue_days": days,
        "age_bucket": age_bucket(days),
        "age_label": bucket_label(age_bucket(days)),
        "is_overdue": days > 0,
    }


def invoice_rows(doc_type, party_id=None, pay_states=None, age_buckets=None,
                 overdue_only=False, search=None, date_from=None, date_to=None,
                 as_of=None):
    """Tracking rows for one invoice family, newest first.

    Settled amounts come from the allocation table, never from the cached
    ``paid_amount`` column, so a stale cache can never hide an outstanding
    balance.
    """
    model = _doc_model(doc_type)
    q = _approved_filter(doc_type, model.query)
    if party_id:
        q = q.filter((model.customer_id if doc_type == SALES
                      else model.supplier_id) == int(party_id))
    if date_from:
        q = q.filter(model.invoice_date >= date_from)
    if date_to:
        q = q.filter(model.invoice_date <= date_to)
    if search:
        q = q.filter(model.invoice_number.ilike(f"%{search.strip()}%"))
    invoices = q.order_by(model.invoice_date.desc(), model.id.desc()).all()

    settled_map = allocated_by_doc(doc_type, [i.id for i in invoices])
    rows = []
    for inv in invoices:
        row = _doc_dict(doc_type, inv, settled_map.get(inv.id, Decimal("0")), as_of)
        if pay_states and row["pay_state"] not in pay_states:
            continue
        if overdue_only and not row["is_overdue"]:
            continue
        if age_buckets and row["age_bucket"] not in age_buckets:
            continue
        rows.append(row)
    return rows


def invoice_row(doc_type, doc_id, as_of=None):
    from shared.tenancy import scoped_get
    inv = scoped_get(_doc_model(doc_type), doc_id)
    if inv is None:
        return None
    settled = allocated_by_doc(doc_type, [doc_id]).get(int(doc_id), Decimal("0"))
    return _doc_dict(doc_type, inv, settled, as_of)


def open_invoices(party_type, party_id, line_id=None, as_of=None):
    """Invoices of this party that still owe something — the assignable set.

    Oldest first: that is the order money is applied in unless the user says
    otherwise, and the order the "auto-assign" action follows. An invoice this
    line has already been assigned to stays in the list, carrying that amount,
    so the workspace can edit an existing split instead of only adding to it.
    """
    doc_type = DOC_FOR_PARTY[party_type]
    model = _doc_model(doc_type)
    q = _approved_filter(doc_type, model.query).filter(
        (model.customer_id if doc_type == SALES else model.supplier_id)
        == int(party_id))
    invoices = q.order_by(model.invoice_date.asc(), model.id.asc()).all()
    settled_map = allocated_by_doc(doc_type, [i.id for i in invoices])
    mine = {}
    if line_id:
        mine = {int(a.doc_id): _q(a.amount) for a in PaymentAllocation.query.filter_by(
            voucher_line_id=int(line_id), doc_type=doc_type).all()}
    rows = []
    for inv in invoices:
        settled = settled_map.get(inv.id, Decimal("0"))
        row = _doc_dict(doc_type, inv, settled, as_of)
        already = mine.get(inv.id, Decimal("0"))
        row["assigned_here"] = _f(already)
        # Headroom for THIS line: what is unpaid plus whatever this line has
        # already put on the invoice (that part is being re-decided, not added).
        row["assignable"] = _f(_q(row["outstanding"]) + already)
        if row["assignable"] > 0 or already > 0:
            rows.append(row)
    return rows


# ── Feed (payments and receipts) ────────────────────────────────────────────

def _line_query():
    return (db.session.query(AccountingVoucherLine, AccountingVoucher)
            .join(AccountingVoucher,
                  AccountingVoucherLine.voucher_id == AccountingVoucher.id)
            .filter(AccountingVoucher.status == "approved"))


def _flow_of(party_type, line, voucher):
    """"receipt" / "payment" for a party line, or None when the line is not a
    settlement at all.

    A customer credited is money in; a supplier debited is money out. The
    mirror cases (a customer debited, a supplier credited) are the invoice's own
    posting or a refund, not a payment the feed is about — they are skipped so
    the feed cannot offer an invoice's own AR entry as cash to assign.
    """
    debit, credit = _q(line.debit), _q(line.credit)
    if party_type == "customer" and credit > TOLERANCE \
            and voucher.voucher_type in RECEIPT_VOUCHERS:
        return "receipt"
    if party_type == "supplier" and debit > TOLERANCE \
            and voucher.voucher_type in PAYMENT_VOUCHERS:
        return "payment"
    return None


def _feed_dict(line, voucher, party, assigned, forced=Decimal("0"),
               forced_reason=""):
    party_type, party_id, party_name = party
    flow = _flow_of(party_type, line, voucher)
    amount = _q(line.credit if flow == "receipt" else line.debit)
    assigned = _q(assigned)
    forced = _q(forced)
    unassigned = _q(amount - assigned)
    return {
        "line_id": line.id,
        "voucher_id": voucher.id,
        "voucher_number": voucher.voucher_number,
        "voucher_type": voucher.voucher_type,
        "date": voucher.voucher_date,
        "date_str": _fmt_date(_as_date(voucher.voucher_date)),
        "flow": flow,
        "party_type": party_type,
        "party_id": party_id,
        "party_name": party_name,
        "doc_type": DOC_FOR_PARTY[party_type],
        "cash_account": (voucher.cash_bank_account.name
                         if voucher.cash_bank_account else ""),
        "description": line.description or "",
        "notes": voucher.notes or "",
        "amount": _f(amount),
        "assigned": _f(assigned),
        # Split of "assigned": what actually landed on invoices, and what a
        # person closed off without one.
        "to_invoices": _f(_q(assigned - forced)),
        "forced": _f(forced),
        "forced_reason": forced_reason or "",
        # Negative headroom means the voucher was edited below its own split;
        # both figures are surfaced so the workspace can show what to re-cut.
        "unassigned": _f(unassigned if unassigned > 0 else Decimal("0")),
        "over_assigned": _f(-unassigned if unassigned < 0 else Decimal("0")),
        "progress": (float(assigned / amount * 100) if amount > 0 else 0.0),
        "state": assign_state(amount, assigned, forced),
        "state_label": ASSIGN_STATE_LABELS.get(
            assign_state(amount, assigned, forced), ""),
    }


def feed_rows(assign_states=None, flow=None, party_type=None, party_id=None,
              date_from=None, date_to=None, search=None, limit=None):
    """Approved receipts from customers and payments to suppliers, newest first.

    ``assign_states`` filters on how much of each one has been assigned; the
    pages default it to the two states that still need attention.
    """
    index = party_account_index()
    if not index:
        return []
    account_ids = [aid for aid, (kind, _pid, _n) in index.items()
                   if party_type is None or kind == party_type]
    if party_id:
        account_ids = [aid for aid in account_ids
                       if index[aid][1] == int(party_id)]
    if not account_ids:
        return []

    q = _line_query().filter(AccountingVoucherLine.account_id.in_(account_ids))
    if date_from:
        q = q.filter(AccountingVoucher.voucher_date >= date_from)
    if date_to:
        q = q.filter(AccountingVoucher.voucher_date <= date_to)
    if search:
        term = f"%{search.strip()}%"
        q = q.filter(db.or_(AccountingVoucher.voucher_number.ilike(term),
                            AccountingVoucherLine.description.ilike(term),
                            AccountingVoucher.notes.ilike(term)))
    pairs = q.order_by(AccountingVoucher.voucher_date.desc(),
                       AccountingVoucher.id.desc(),
                       AccountingVoucherLine.line_no.asc()).all()

    line_ids = [l.id for l, _v in pairs]
    assigned_map = allocated_by_line(line_ids)
    forced_map = forced_by_line(line_ids)
    reason_map = forced_note_by_line(line_ids)
    rows = []
    for line, voucher in pairs:
        party = index.get(line.account_id)
        if party is None:
            continue
        row_flow = _flow_of(party[0], line, voucher)
        if row_flow is None or (flow and row_flow != flow):
            continue
        row = _feed_dict(line, voucher, party,
                         assigned_map.get(line.id, Decimal("0")),
                         forced_map.get(line.id, Decimal("0")),
                         reason_map.get(line.id, ""))
        if assign_states and row["state"] not in assign_states:
            continue
        rows.append(row)
        if limit and len(rows) >= limit:
            break
    return rows


def feed_row(line_id):
    """One feed item, or None when the line is not an assignable settlement."""
    pair = _line_query().filter(AccountingVoucherLine.id == int(line_id)).first()
    if pair is None:
        return None
    line, voucher = pair
    party = party_account_index().get(line.account_id)
    if party is None or _flow_of(party[0], line, voucher) is None:
        return None
    assigned = allocated_by_line([line.id]).get(int(line_id), Decimal("0"))
    forced = forced_by_line([line.id]).get(int(line_id), Decimal("0"))
    reason = forced_note_by_line([line.id]).get(int(line_id), "")
    return _feed_dict(line, voucher, party, assigned, forced, reason)


def feed_summary(rows=None):
    """Tiles for the feed header, computed over the whole feed (not the page)."""
    all_rows = feed_rows() if rows is None else rows
    out = {
        "receipts_open": 0, "receipts_open_value": 0.0,
        "payments_open": 0, "payments_open_value": 0.0,
        "unassigned_count": 0, "partial_count": 0, "assigned_count": 0,
        "forced_count": 0, "forced_value": 0.0,
        "review_count": 0, "review_value": 0.0,
        "unassigned_value": 0.0,
    }
    for r in all_rows:
        out[f"{r['state']}_count"] += 1
        out["forced_value"] += r["forced"]
        out["review_value"] += r["over_assigned"]
        # Both settled states are finished work; only the rest is still a queue.
        if r["state"] not in ("assigned", "forced"):
            out["unassigned_value"] += r["unassigned"]
            if r["flow"] == "receipt":
                out["receipts_open"] += 1
                out["receipts_open_value"] += r["unassigned"]
            else:
                out["payments_open"] += 1
                out["payments_open_value"] += r["unassigned"]
    return out


def draft_settlement_count():
    """Unapproved vouchers carrying a party line.

    They are deliberately absent from the feed — money that has not been posted
    cannot settle an invoice — but the count is worth showing so a stack of
    unapproved receipts does not look like "no receipts".
    """
    index = party_account_index()
    if not index:
        return 0
    return (db.session.query(db.func.count(db.distinct(AccountingVoucher.id)))
            .join(AccountingVoucherLine,
                  AccountingVoucherLine.voucher_id == AccountingVoucher.id)
            .filter(AccountingVoucher.status != "approved",
                    AccountingVoucherLine.account_id.in_(list(index.keys())))
            .scalar()) or 0


def outstanding_summary(doc_type, as_of=None):
    """Totals for the tracking pages' tiles: outstanding and overdue value."""
    rows = invoice_rows(doc_type, as_of=as_of)
    total = sum(r["outstanding"] for r in rows)
    overdue = sum(r["outstanding"] for r in rows if r["is_overdue"])
    buckets = {key: 0.0 for key, _l, _lo, _hi in AGE_BUCKETS}
    for r in rows:
        if r["outstanding"] > 0:
            buckets[r["age_bucket"]] += r["outstanding"]
    return {
        "count": len(rows),
        "open_count": sum(1 for r in rows if r["pay_state"] != "paid"),
        "outstanding": total,
        "overdue": overdue,
        "overdue_count": sum(1 for r in rows if r["is_overdue"]),
        "buckets": buckets,
    }


# ── History ─────────────────────────────────────────────────────────────────

def history_for_doc(doc_type, doc_id):
    """Every payment/receipt assigned to one invoice, oldest first, with the
    running balance after each one — the invoice's settlement story."""
    from shared.tenancy import scoped_get
    inv = scoped_get(_doc_model(doc_type), doc_id)
    if inv is None:
        return []
    allocs = (PaymentAllocation.query
              .filter_by(doc_type=doc_type, doc_id=int(doc_id))
              .join(AccountingVoucher,
                    PaymentAllocation.voucher_id == AccountingVoucher.id)
              .order_by(AccountingVoucher.voucher_date.asc(),
                        PaymentAllocation.id.asc()).all())
    total = _q(inv.total_amount)
    running = total
    out = []
    for a in allocs:
        amount = _q(a.amount)
        running = _q(running - amount)
        v = a.voucher
        line = a.line
        out.append({
            "id": a.id,
            "voucher_id": a.voucher_id,
            "line_id": a.voucher_line_id,
            "voucher_number": v.voucher_number if v else "",
            "voucher_type": v.voucher_type if v else "",
            "date": v.voucher_date if v else a.created_at,
            "cash_account": (v.cash_bank_account.name
                             if v and v.cash_bank_account else ""),
            "description": (line.description if line else "") or "",
            "amount": _f(amount),
            "balance_after": _f(running),
            "note": a.note or "",
            "by": a.creator.full_name if a.creator else "",
            "created_at": a.created_at,
        })
    return out


def history_for_line(line_id):
    """The invoices one receipt/payment was split across."""
    allocs = PaymentAllocation.query.filter_by(
        voucher_line_id=int(line_id)).order_by(PaymentAllocation.id.asc()).all()
    out = []
    for a in allocs:
        forced = a.doc_type == NOT_APPLICABLE
        row = None if forced else invoice_row(a.doc_type, a.doc_id)
        out.append({
            "id": a.id,
            "doc_type": a.doc_type,
            "doc_id": a.doc_id,
            "forced": forced,
            "number": ("Not applicable to any invoice" if forced
                       else (row["number"] if row else f"#{a.doc_id}")),
            "party_name": row["party_name"] if row else "",
            "total": row["total"] if row else 0.0,
            "outstanding": row["outstanding"] if row else 0.0,
            "amount": _f(a.amount),
            "note": a.note or "",
            "by": a.creator.full_name if a.creator else "",
            "created_at": a.created_at,
        })
    return out


# ── Writes ──────────────────────────────────────────────────────────────────

class AllocationError(Exception):
    """A refused assignment. The message is shown to the user verbatim."""


def recompute_doc(doc_type, doc_id):
    """Re-derive an invoice's ``paid_amount`` / ``payment_status`` from its
    allocations.

    These two columns are a cache of this table — every read path in the
    tracker computes from the allocations themselves. Keeping them in step is
    what makes the invoice list, the dashboards and the FBR payload agree with
    the tracker. Neither column is a ledger figure and no journal is involved.
    """
    from shared.tenancy import scoped_get
    inv = scoped_get(_doc_model(doc_type), doc_id)
    if inv is None:
        return None
    settled = allocated_by_doc(doc_type, [int(doc_id)]).get(int(doc_id),
                                                           Decimal("0"))
    inv.paid_amount = _f(settled)
    inv.payment_status = pay_state(_q(inv.total_amount), _q(settled))
    return inv


def allocate(line_id, entries, user_id=None, note=""):
    """Assign one receipt/payment to one or more invoices.

    ``entries`` is a list of ``{"doc_id": int, "amount": number}`` (doc_type is
    implied by the party — a customer's receipt can only settle sales invoices).
    An amount of zero removes an existing assignment, so the same call can
    re-split a receipt.

    Every refusal raises AllocationError and leaves nothing written: the whole
    split is validated before the first row is touched, because a half-applied
    split is exactly the state that makes an invoice's settled figure wrong.
    """
    row = feed_row(line_id)
    if row is None:
        raise AllocationError(
            "That payment is not an approved customer receipt or supplier "
            "payment, so it cannot be assigned to invoices.")
    doc_type = row["doc_type"]
    party_type, party_id = row["party_type"], row["party_id"]

    existing = {int(a.doc_id): a for a in PaymentAllocation.query.filter_by(
        voucher_line_id=int(line_id), doc_type=doc_type).all()}

    cleaned = {}
    for entry in entries or []:
        try:
            doc_id = int(entry.get("doc_id"))
            amount = _q(entry.get("amount") or 0)
        except (TypeError, ValueError):
            raise AllocationError("Invalid invoice or amount in the assignment.")
        if amount < 0:
            raise AllocationError("An assigned amount cannot be negative.")
        cleaned[doc_id] = cleaned.get(doc_id, Decimal("0")) + amount
    if not cleaned:
        raise AllocationError("Select at least one invoice to assign this to.")

    from shared.tenancy import scoped_get
    model = _doc_model(doc_type)
    invoices = {}
    for doc_id, amount in cleaned.items():
        inv = scoped_get(model, doc_id)
        if inv is None:
            raise AllocationError(f"Invoice #{doc_id} was not found.")
        approved = (inv.voucher_status if doc_type == SALES else inv.status)
        if approved != "approved":
            raise AllocationError(
                f"{inv.invoice_number} is not approved yet, so nothing can be "
                "assigned to it.")
        if _party_id_of(doc_type, inv) != party_id:
            raise AllocationError(
                f"{inv.invoice_number} belongs to a different "
                f"{party_type} than this {row['flow']}.")
        invoices[doc_id] = inv

    # Per-invoice headroom: what it still owes, plus what this same line had
    # already put on it (that amount is being replaced, not added to).
    settled_map = allocated_by_doc(doc_type, list(cleaned.keys()))
    for doc_id, amount in cleaned.items():
        inv = invoices[doc_id]
        settled = settled_map.get(doc_id, Decimal("0"))
        mine = _q(existing[doc_id].amount) if doc_id in existing else Decimal("0")
        headroom = _q(_q(inv.total_amount) - settled + mine)
        if amount > headroom + TOLERANCE:
            raise AllocationError(
                f"{inv.invoice_number} only has {headroom:,.2f} outstanding — "
                f"{amount:,.2f} cannot be assigned to it.")

    # Line headroom: the whole split must fit inside the money that moved.
    # Anything already force-closed on this line is spent too — leaving it out
    # would let a forced close and an assignment together exceed the receipt.
    total_assigned = sum(cleaned.values())
    other_lines = sum(_q(a.amount) for did, a in existing.items()
                      if did not in cleaned)
    forced = forced_by_line([int(line_id)]).get(int(line_id), Decimal("0"))
    capacity = _q(row["amount"])
    if total_assigned + other_lines + forced > capacity + TOLERANCE:
        detail = (f" ({forced:,.2f} of it is already closed as not applicable)"
                  if forced > TOLERANCE else "")
        raise AllocationError(
            f"This {row['flow']} is {capacity:,.2f}{detail}; assigning "
            f"{total_assigned + other_lines + forced:,.2f} would exceed it.")

    touched = set()
    for doc_id, amount in cleaned.items():
        alloc = existing.get(doc_id)
        if amount <= 0:
            if alloc is not None:
                db.session.delete(alloc)
                touched.add(doc_id)
            continue
        if alloc is None:
            alloc = PaymentAllocation(
                voucher_id=row["voucher_id"], voucher_line_id=int(line_id),
                doc_type=doc_type, doc_id=doc_id,
                party_type=party_type, party_id=party_id,
                created_by=user_id)
            db.session.add(alloc)
        alloc.amount = amount
        if note:
            alloc.note = note[:300]
        touched.add(doc_id)

    db.session.flush()
    for doc_id in touched:
        recompute_doc(doc_type, doc_id)
    db.session.commit()
    return feed_row(line_id)


def stale_allocations():
    """Assignments that no longer hold, with the reason each one broke.

    Read-only: nothing is deleted. The row stays so the history of what was
    once matched survives an accidental voucher edit, and ``reconcile()`` only
    has to refresh the invoices those rows used to settle.
    """
    _live, stale = classify_allocations(PaymentAllocation.query.all())
    if not stale:
        return []

    # Both sides are looked up leniently: the whole reason a row is stale may
    # be that the voucher or the invoice is gone, so neither can be assumed.
    vouchers = {}
    if stale:
        for v in AccountingVoucher.query.filter(AccountingVoucher.id.in_(
                {int(a.voucher_id) for a, _r in stale})).all():
            vouchers[int(v.id)] = v
    docs = {}
    for doc_type in (SALES, PURCHASE):
        ids = {int(a.doc_id) for a, _r in stale if a.doc_type == doc_type}
        if not ids:
            continue
        model = _doc_model(doc_type)
        for inv in model.query.filter(model.id.in_(ids)).all():
            docs[(doc_type, int(inv.id))] = inv
    names = {}
    for kind, party_id, name in party_account_index().values():
        names[(kind, int(party_id))] = name

    out = []
    for a, reason in stale:
        v = vouchers.get(int(a.voucher_id))
        inv = docs.get((a.doc_type, int(a.doc_id)))
        out.append({
            "id": a.id,
            "voucher_id": a.voucher_id,
            "voucher_number": v.voucher_number if v else "(deleted voucher)",
            "voucher_type": v.voucher_type if v else "",
            "voucher_status": v.status if v else "deleted",
            "date": v.voucher_date if v else a.created_at,
            "line_id": a.voucher_line_id,
            "doc_type": a.doc_type,
            "doc_id": a.doc_id,
            "doc_number": ("Not applicable to any invoice"
                           if a.doc_type == NOT_APPLICABLE
                           else (inv.invoice_number if inv
                                 else f"#{a.doc_id} (deleted)")),
            "party_type": a.party_type,
            "party_id": a.party_id,
            "party_name": names.get((a.party_type, int(a.party_id)),
                                    f"{a.party_type} #{a.party_id}"),
            "amount": _f(a.amount),
            "reason": reason,
            "by": a.creator.full_name if a.creator else "",
        })
    out.sort(key=lambda r: r["id"])
    return out


def clear_stale_allocations(alloc_ids=None):
    """Delete broken assignments, then refresh the invoices they pointed at.

    Deliberately a separate, explicit action rather than something the read
    path does: a stale row is usually the trace of an accidental voucher edit,
    and keeping it until someone looks is what lets them see what was matched
    before deciding. Removes no money and writes no journal.
    """
    stale = classify_allocations(PaymentAllocation.query.all())[1]
    rows = [a for a, _reason in stale]
    if alloc_ids is not None:
        wanted = {int(i) for i in alloc_ids}
        rows = [a for a in rows if int(a.id) in wanted]
    if not rows:
        return 0

    touched = {(a.doc_type, int(a.doc_id)) for a in rows}
    for a in rows:
        db.session.delete(a)
    db.session.flush()
    for doc_type, doc_id in touched:
        if doc_type in (SALES, PURCHASE):
            recompute_doc(doc_type, doc_id)
    db.session.commit()
    return len(rows)


def reconcile(doc_type=None, doc_ids=None):
    """Re-derive the cached ``paid_amount`` / ``payment_status`` on invoices.

    Every figure the tracker *displays* is derived live, so it is already right
    the instant a voucher or an invoice is edited. These two columns are the
    exception — they are stored on the invoice for the invoice list, the
    dashboards and the FBR payload to read cheaply, so they need a nudge when
    the thing they cache changes underneath them.

    Call it after a voucher is saved, approved, unapproved or deleted. With no
    arguments it sweeps every invoice that any allocation points at, which is
    the safe thing to do when you do not know what an edit touched. It writes
    no journal — like everything else here.
    """
    targets = {SALES: set(), PURCHASE: set()}
    if doc_type and doc_ids is not None:
        targets[doc_type] = {int(d) for d in doc_ids}
    else:
        for a in PaymentAllocation.query.all():
            if a.doc_type in targets:
                targets[a.doc_type].add(int(a.doc_id))

    changed = 0
    for dt, ids in targets.items():
        for doc_id in ids:
            inv = recompute_doc(dt, doc_id)
            if inv is not None and db.session.is_modified(inv):
                changed += 1
    if changed:
        db.session.commit()
    return changed


def reconcile_voucher(voucher_id):
    """Refresh the invoices touched by one voucher's assignments.

    The cheap hook for a voucher save/approve/delete: only the invoices that
    voucher was assigned to can have changed.
    """
    allocs = PaymentAllocation.query.filter_by(voucher_id=int(voucher_id)).all()
    if not allocs:
        return 0
    changed = 0
    for dt in (SALES, PURCHASE):
        ids = {int(a.doc_id) for a in allocs if a.doc_type == dt}
        if ids:
            changed += reconcile(dt, ids)
    return changed


def sync_invoice(doc_type, doc_id):
    """Refresh one invoice's settled cache after the invoice itself changed.

    Editing an invoice's total, or unapproving it, changes what its existing
    assignments mean: a 3,000 invoice fully settled by a 3,000 receipt becomes
    partially paid the moment someone raises it to 4,000. Safe to call on every
    invoice save. Writes no journal.
    """
    try:
        inv = recompute_doc(doc_type, doc_id)
        if inv is not None:
            db.session.commit()
        return inv
    except Exception:                                    # pragma: no cover
        db.session.rollback()
        return None


def drop_allocations_for_doc(doc_type, doc_id):
    """Remove assignments pointing at an invoice about to be deleted.

    The money itself is untouched — it goes back to the feed as unassigned,
    which is exactly right: it was still received, it just no longer settles
    anything.
    """
    try:
        allocs = PaymentAllocation.query.filter_by(
            doc_type=doc_type, doc_id=int(doc_id)).all()
        for a in allocs:
            db.session.delete(a)
        if allocs:
            db.session.flush()
        return len(allocs)
    except Exception:                                    # pragma: no cover
        db.session.rollback()
        return 0


def force_close(line_id, amount=None, reason="", user_id=None):
    """Close a receipt/payment (or the rest of one) against no invoice at all.

    For the money that genuinely has no document to match: a customer advance,
    a rounding difference, a payment for something never invoiced. Without this
    the feed can only ever grow, because those items can never reach "assigned"
    and would sit in the unassigned queue forever.

    ``amount`` defaults to everything still loose on the line. A reason is
    required — this is a human overriding the matching rule, and the feed shows
    who said so and why. Like every other write here it touches no ledger.
    """
    row = feed_row(line_id)
    if row is None:
        raise AllocationError(
            "That payment is not an approved customer receipt or supplier "
            "payment, so it cannot be closed.")
    reason = (reason or "").strip()
    if not reason:
        raise AllocationError(
            "Give a reason for closing this without an invoice.")

    loose = _q(row["unassigned"])
    amount = loose if amount in (None, "") else _q(amount)
    if amount <= 0:
        raise AllocationError("The amount to close must be more than zero.")
    if amount > loose + TOLERANCE:
        raise AllocationError(
            f"Only {loose:,.2f} of this {row['flow']} is still unassigned; "
            f"{amount:,.2f} cannot be closed.")

    alloc = PaymentAllocation.query.filter_by(
        voucher_line_id=int(line_id), doc_type=NOT_APPLICABLE).first()
    if alloc is None:
        alloc = PaymentAllocation(
            voucher_id=row["voucher_id"], voucher_line_id=int(line_id),
            doc_type=NOT_APPLICABLE, doc_id=NA_DOC_ID,
            party_type=row["party_type"], party_id=row["party_id"],
            created_by=user_id)
        db.session.add(alloc)
    else:
        # Re-closing tops up the existing row rather than stacking a second
        # one, for the same reason invoice assignment does.
        amount = _q(amount + _q(alloc.amount))
    alloc.amount = amount
    alloc.note = reason[:300]
    db.session.commit()
    return feed_row(line_id)


def release_force(line_id):
    """Undo a forced close: the money goes back to the unassigned queue."""
    alloc = PaymentAllocation.query.filter_by(
        voucher_line_id=int(line_id), doc_type=NOT_APPLICABLE).first()
    if alloc is None:
        raise AllocationError("This payment has not been force-closed.")
    db.session.delete(alloc)
    db.session.commit()
    return feed_row(line_id)


def unallocate(alloc_id):
    """Remove one assignment. The money stays where the ledger put it; only the
    link to the invoice goes away."""
    from shared.tenancy import scoped_get
    alloc = scoped_get(PaymentAllocation, alloc_id)
    if alloc is None:
        raise AllocationError("That assignment no longer exists.")
    doc_type, doc_id, line_id = alloc.doc_type, alloc.doc_id, alloc.voucher_line_id
    db.session.delete(alloc)
    db.session.flush()
    # A forced close has no invoice to re-derive; only the line goes back to
    # being unassigned.
    if doc_type != NOT_APPLICABLE:
        recompute_doc(doc_type, doc_id)
    db.session.commit()
    return {"doc_type": doc_type, "doc_id": doc_id, "line_id": line_id}


def auto_split(line_id, as_of=None):
    """Oldest-first proposal for a receipt: fill each open invoice in turn.

    Returned, never saved — the user confirms (and may edit) the split.
    """
    row = feed_row(line_id)
    if row is None:
        return []
    remaining = _q(row["unassigned"])
    out = []
    for inv in open_invoices(row["party_type"], row["party_id"], line_id, as_of):
        if remaining <= 0:
            break
        take = min(remaining, _q(inv["assignable"]))
        if take <= 0:
            continue
        out.append({"doc_id": inv["id"], "number": inv["number"],
                    "amount": _f(take)})
        remaining = _q(remaining - take)
    return out
