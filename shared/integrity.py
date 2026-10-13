"""Books integrity: do the general ledger and the sub-ledgers still agree?

Every document in the app posts twice — once to its own register (stock
ledger, asset register, the document itself) and once to the general ledger.
These checks prove the two halves still match, for the active company, after
any sequence of additions, back-dated postings, edits and reversals:

    trial_balance        total debits == total credits (posted lines)
    journals_balanced    every posted journal balances on its own
    inventory_vs_gl      inventory accounts == stock valuation (stock ledger)
    stock_layers         cost layers back every product's ledger value
    stock_quantity       product on-hand figure == stock ledger quantity
    fixed_assets_vs_gl   asset register cost / accumulated depreciation == GL
    documents_posted     every approved document has a posted journal, and
                         no unapproved one does

Each check returns a dict {key, label, ok, expected, actual, detail}. The
Executive "Books integrity" page renders them; the E2E suite asserts them
after every transaction type.
"""

from decimal import Decimal

from shared.extensions import db

ZERO = Decimal("0")
TOL = Decimal("0.01")


def _d(v):
    return Decimal(str(v or 0))


def _gl_balance(prefixes, extra_ids=()):
    """Net debit balance of every posted line on accounts under ``prefixes``."""
    from shared.models.ledger import ChartOfAccount, JournalEntry, JournalLine
    accts = ChartOfAccount.query.all()
    ids = {a.id for a in accts if any((a.code or "").startswith(p) for p in prefixes)}
    ids |= set(extra_ids)
    if not ids:
        return ZERO
    dr, cr = (db.session.query(
        db.func.coalesce(db.func.sum(JournalLine.debit), 0),
        db.func.coalesce(db.func.sum(JournalLine.credit), 0))
        .join(JournalEntry, JournalLine.journal_entry_id == JournalEntry.id)
        .filter(JournalEntry.is_posted == True,  # noqa: E712
                JournalLine.account_id.in_(ids)).one())
    return _d(dr) - _d(cr)


def _check(key, label, expected, actual, detail="", tol=TOL):
    ok = abs(_d(expected) - _d(actual)) <= tol
    return {"key": key, "label": label, "ok": ok,
            "expected": float(_d(expected)), "actual": float(_d(actual)),
            "detail": detail}


def trial_balance():
    from shared.models.ledger import JournalEntry, JournalLine
    dr, cr = (db.session.query(
        db.func.coalesce(db.func.sum(JournalLine.debit), 0),
        db.func.coalesce(db.func.sum(JournalLine.credit), 0))
        .join(JournalEntry, JournalLine.journal_entry_id == JournalEntry.id)
        .filter(JournalEntry.is_posted == True).one())  # noqa: E712
    return _check("trial_balance", "Trial balance (debits = credits)",
                  dr, cr, "Total posted debits vs credits")


def journals_balanced():
    from shared.models.ledger import JournalEntry, JournalLine
    rows = (db.session.query(
        JournalEntry.voucher_number,
        db.func.coalesce(db.func.sum(JournalLine.debit), 0),
        db.func.coalesce(db.func.sum(JournalLine.credit), 0))
        .join(JournalLine, JournalLine.journal_entry_id == JournalEntry.id)
        .filter(JournalEntry.is_posted == True)  # noqa: E712
        .group_by(JournalEntry.id, JournalEntry.voucher_number).all())
    bad = [n for n, d, c in rows if abs(_d(d) - _d(c)) > TOL]
    return {"key": "journals_balanced", "label": "Every journal balances",
            "ok": not bad, "expected": 0, "actual": len(bad),
            "detail": ("Unbalanced: " + ", ".join(bad[:10])) if bad else
                      f"{len(rows)} posted journals checked"}


def stock_valuation():
    """Value of all stock per the stock ledger (posted IN less posted OUT)."""
    from shared.models.stock_ledger import StockLedger
    rows = (db.session.query(StockLedger.transaction_type,
                             db.func.coalesce(db.func.sum(StockLedger.total_cost), 0))
            .group_by(StockLedger.transaction_type).all())
    total = ZERO
    for t, v in rows:
        total += _d(v) if t == "IN" else -_d(v)
    return total


def inventory_vs_gl():
    from shared.ledger_utils import posting_account
    try:
        extra = [posting_account("inventory").id]
    except Exception:
        extra = []
    gl = _gl_balance(("1-01-04",), extra)
    stock = stock_valuation()
    return _check("inventory_vs_gl", "Inventory account = stock valuation",
                  stock, gl,
                  "General-ledger inventory balance vs the stock ledger's value")


def _ledger_net_by_product():
    """{product_id: (net qty, net posted cost)} in two grouped queries."""
    from shared.models.stock_ledger import StockLedger
    out = {}
    for pid, ttype, q, v in (db.session.query(
            StockLedger.product_id, StockLedger.transaction_type,
            db.func.coalesce(db.func.sum(StockLedger.quantity), 0),
            db.func.coalesce(db.func.sum(StockLedger.total_cost), 0))
            .group_by(StockLedger.product_id, StockLedger.transaction_type).all()):
        sign = 1 if ttype == "IN" else -1
        oq, ov = out.get(pid, (ZERO, ZERO))
        out[pid] = (oq + sign * _d(q), ov + sign * _d(v))
    return out


def stock_layers():
    # Grouped first — one query per table, not two per product, so the page
    # stays inside a serverless time limit on a remote database. Only the
    # products whose grouped figures disagree get the exact per-product check
    # (the ledger's running cost clamps to zero at zero quantity, which a
    # plain sum does not).
    from shared import costing
    from shared.models.stock_layer import StockLayer
    ledger = _ledger_net_by_product()
    layer_value = {pid: _d(v) for pid, v in db.session.query(
        StockLayer.product_id,
        db.func.coalesce(db.func.sum(StockLayer.value_remaining), 0))
        .filter(StockLayer.qty_remaining != 0)
        .group_by(StockLayer.product_id).all()}
    pids = list(ledger)
    bad = []
    for pid in pids:
        if abs(layer_value.get(pid, ZERO) - ledger[pid][1]) <= TOL:
            continue
        ok, layers, led = costing.assert_invariant(pid)
        if not ok:
            bad.append(f"#{pid} layers {layers} vs ledger {led}")
    return {"key": "stock_layers", "label": "Cost layers back the stock ledger",
            "ok": not bad, "expected": 0, "actual": len(bad),
            "detail": "; ".join(bad[:5]) if bad else f"{len(pids)} products checked"}


def stock_quantity():
    from inventory_app.models.product import InvProduct
    ledger = _ledger_net_by_product()
    bad = []
    products = InvProduct.query.all()
    for p in products:
        if p.id not in ledger:
            continue
        q = ledger[p.id][0]
        if int(q) != int(p.current_stock or 0):
            bad.append(f"{p.name}: shows {p.current_stock}, ledger {q.normalize()}")
    return {"key": "stock_quantity", "label": "On-hand quantity = stock ledger",
            "ok": not bad, "expected": 0, "actual": len(bad),
            "detail": "; ".join(bad[:5]) if bad else f"{len(products)} products checked"}


def fixed_assets_vs_gl():
    from fixed_assets_app.models.asset import FixedAsset
    live = [a for a in FixedAsset.query.all()
            if a.status not in ("disposed", "transferred", "inactive", "draft", "cancelled")]
    cost = sum((_d(a.purchase_cost) for a in live), ZERO)
    dep = sum((_d(a.posted_depreciation) for a in live), ZERO)
    gl_cost = _gl_balance(("1-02-01",))
    gl_dep = -_gl_balance(("1-02-02",))
    ok = abs(cost - gl_cost) <= TOL and abs(dep - gl_dep) <= TOL
    return {"key": "fixed_assets_vs_gl", "label": "Asset register = fixed-asset accounts",
            "ok": ok, "expected": float(cost - dep), "actual": float(gl_cost - gl_dep),
            "detail": (f"Cost {cost:,.2f} vs GL {gl_cost:,.2f}; accumulated "
                       f"depreciation {dep:,.2f} vs GL {gl_dep:,.2f}")}


# (model import path, class, status attr, approved value, journal voucher_type)
_DOCUMENTS = (
    ("inventory_app.models.invoice", "InvInvoice", "voucher_status", "approved", "SI", "invoice_number"),
    ("inventory_app.models.purchase_invoice", "InvPurchaseInvoice", "status", "approved", "PI", "invoice_number"),
    ("inventory_app.models.sales_return", "InvSalesReturn", "status", "approved", "SR", "return_number"),
    ("inventory_app.models.purchase_return", "InvPurchaseReturn", "status", "approved", "PR", "return_number"),
    ("shared.models.vouchers", "ConsumptionVoucher", "status", "approved", "CONS", "voucher_number"),
    ("shared.models.vouchers", "ScrapVoucher", "status", "approved", "SCRAP", "voucher_number"),
    ("shared.models.accounting_voucher", "AccountingVoucher", "status", "approved", None, "voucher_number"),
)


def documents_posted():
    import importlib
    from shared.models.ledger import JournalEntry
    problems = []
    checked = 0
    # Every posted (voucher_type, voucher_id) in ONE query, instead of one
    # query per document.
    posted_keys = set(db.session.query(JournalEntry.voucher_type,
                                       JournalEntry.voucher_id)
                      .filter(JournalEntry.is_posted == True).distinct().all())  # noqa: E712
    for mod, cls, attr, approved, vtype, num in _DOCUMENTS:
        try:
            model = getattr(importlib.import_module(mod), cls)
        except Exception:
            continue
        for doc in model.query.all():
            checked += 1
            vt = vtype or getattr(doc, "voucher_type", None)
            if not vt:
                continue
            posted = (vt, doc.id) in posted_keys
            is_approved = getattr(doc, attr, None) == approved
            if is_approved and not posted and _has_value(doc):
                problems.append(f"{getattr(doc, num, doc.id)} approved but not posted")
            if posted and not is_approved:
                problems.append(f"{getattr(doc, num, doc.id)} posted but not approved")
    return {"key": "documents_posted", "label": "Approved documents = posted journals",
            "ok": not problems, "expected": 0, "actual": len(problems),
            "detail": "; ".join(problems[:6]) if problems else f"{checked} documents checked"}


def _has_value(doc):
    for attr in ("total_amount", "net_payable", "net_return_amount"):
        v = getattr(doc, attr, None)
        if v is not None:
            return abs(float(v or 0)) > 0.005
    items = getattr(doc, "items", None)
    if items is not None:
        try:
            return any(abs(float(getattr(i, "total_cost", 0) or 0)) > 0.005 for i in items)
        except Exception:
            return True
    return True


def reporting_table():
    """The reporting table every report reads (gl_daily_balances) agrees with
    the posted journal lines, account by account and label by label."""
    from shared.models.gl_balance import GLDailyBalance as G
    from shared.models.ledger import JournalEntry, JournalLine
    src = {(a, l or 0): (_d(dr), _d(cr)) for a, l, dr, cr in (
        db.session.query(JournalLine.account_id, JournalLine.label_id,
                         db.func.coalesce(db.func.sum(JournalLine.debit), 0),
                         db.func.coalesce(db.func.sum(JournalLine.credit), 0))
        .join(JournalEntry, JournalLine.journal_entry_id == JournalEntry.id)
        .filter(JournalEntry.is_posted == True)  # noqa: E712
        .group_by(JournalLine.account_id, JournalLine.label_id).all())}
    summ = {(a, l): (_d(dr), _d(cr)) for a, l, dr, cr in (
        db.session.query(G.account_id, G.label_id,
                         db.func.coalesce(db.func.sum(G.debit), 0),
                         db.func.coalesce(db.func.sum(G.credit), 0))
        .group_by(G.account_id, G.label_id).all())}
    zero = (_d(0), _d(0))
    bad = [k for k in set(src) | set(summ)
           if abs(src.get(k, zero)[0] - summ.get(k, zero)[0]) > TOL
           or abs(src.get(k, zero)[1] - summ.get(k, zero)[1]) > TOL]
    return _check("reporting_table", "Reporting table matches the ledger",
                  0, len(bad),
                  "Accounts whose report totals differ from their journal lines"
                  + (f" — run tools/rebuild_gl_summary.py ({len(bad)} differ)" if bad else ""),
                  tol=0)


CHECKS = (trial_balance, journals_balanced, reporting_table, inventory_vs_gl, stock_layers,
          stock_quantity, fixed_assets_vs_gl, documents_posted)


def run_all():
    out = []
    for fn in CHECKS:
        try:
            out.append(fn())
        except Exception as e:  # a check that cannot run is itself a finding
            # On Postgres a failed statement aborts the transaction; clear it
            # or every later check fails with "current transaction is aborted".
            db.session.rollback()
            out.append({"key": fn.__name__, "label": fn.__name__.replace("_", " "),
                        "ok": False, "expected": 0, "actual": 0,
                        "detail": f"Could not run: {e}"})
    return out


def failures():
    return [c for c in run_all() if not c["ok"]]
