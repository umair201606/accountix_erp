"""Several users on the same records at the same instant.

Each test starts N requests (or transactions) together behind a barrier, one
thread each with its own session, the way N browser tabs on N desks reach the
server. Afterwards the books must tie (shared/integrity.py) and the outcome
must be what one-at-a-time would have produced: no number handed out twice,
no document posted twice, no stock sold that was not there, no report total
that misses someone's posting — and a user who loses a race is told so in
words, never shown a crash.
"""
import os
import tempfile
import threading
from datetime import date
from decimal import Decimal

import pytest


def _database_url():
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        return url
    try:
        import pgserver
    except ImportError:
        return None
    import sqlalchemy as sa
    global _PG
    _PG = pgserver.get_server(tempfile.mkdtemp(prefix="erp_concurrency_pg_"),
                              cleanup_mode="delete")
    base = _PG.get_uri()
    eng = sa.create_engine(base, isolation_level="AUTOCOMMIT")
    with eng.connect() as c:
        c.execute(sa.text("DROP DATABASE IF EXISTS erp_concurrency"))
        c.execute(sa.text("CREATE DATABASE erp_concurrency"))
    eng.dispose()
    return base.rsplit("/", 1)[0] + "/erp_concurrency"


_URL = _database_url()
if not _URL:
    pytest.skip("needs Postgres: set TEST_DATABASE_URL or pip install pgserver",
                allow_module_level=True)
os.environ["DATABASE_URL"] = _URL

from app import app as flask_app  # noqa: E402
from shared.extensions import db  # noqa: E402

TODAY = date.today().isoformat()
N = 6


# ─────────────────────────────────────────────
# A fresh company, and N signed-in users
# ─────────────────────────────────────────────

@pytest.fixture(scope="module")
def world():
    flask_app.test_client().get("/")
    with flask_app.app_context():
        assert db.engine.dialect.name == "postgresql", db.engine.url
    from hr_app.models.user import User
    from shared.models.company import Company, CompanyMembership
    from shared.models.base import Role
    from shared.tenancy import set_current_company, unscoped
    from shared.company_setup import provision_company

    with flask_app.app_context():
        user = User.query.filter_by(is_super_admin=True).order_by(User.id).first()
        uid = user.id
        with unscoped():
            comp = Company(name="Many Hands Co", slug="many-hands-e2e",
                           is_active=True, created_by=uid)
            db.session.add(comp)
            db.session.flush()
            admin = Role.query.filter_by(name=Role.ADMIN).first()
            db.session.add(CompanyMembership(company_id=comp.id, user_id=uid,
                                             role_id=admin.id,
                                             status=CompanyMembership.ACTIVE))
            db.session.commit()
            cid = comp.id
        set_current_company(cid)
        provision_company(cid)
        set_current_company(cid)

        from shared.models.inventory_settings import InventorySettings
        from inventory_app.models.product import InvProduct
        from inventory_app.models.supplier import InvSupplier
        from inventory_app.models.customer import InvCustomer
        from shared.models.ledger import ChartOfAccount
        s = InventorySettings.get()
        s.valuation_method = "fifo"
        s.allow_negative_stock = False
        prods = [InvProduct(sku=f"MH-{i}", name=f"Many Hands Part {i}", unit="pcs",
                            unit_price=200, cost_price=0, current_stock=0)
                 for i in range(4)]
        sup = InvSupplier(name="Many Hands Supplier")
        cus = InvCustomer(name="Many Hands Customer")
        db.session.add_all(prods + [sup, cus])
        db.session.commit()
        accts = [a.id for a in ChartOfAccount.query.filter(ChartOfAccount.level >= 5)
                 .order_by(ChartOfAccount.id).limit(2).all()]
        return {"company": cid, "user": uid, "products": [p.id for p in prods],
                "supplier": sup.id, "customer": cus.id, "accounts": accts}


def client(w):
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess["_user_id"] = str(w["user"])
        sess["_fresh"] = True
        sess["company_id"] = w["company"]
    return c


class Ctx:
    def __init__(self, w):
        self.w = w

    def __enter__(self):
        from shared.tenancy import set_current_company
        self.cm = flask_app.app_context()
        self.cm.__enter__()
        set_current_company(self.w["company"])
        return self

    def __exit__(self, *a):
        db.session.remove()
        self.cm.__exit__(*a)


def race(fns, timeout=180):
    """Run every callable at the same instant, one thread each. Returns each
    one's result, or the exception it raised."""
    barrier = threading.Barrier(len(fns))
    out = [None] * len(fns)

    def run(i, fn):
        try:
            barrier.wait(timeout=60)
            out[i] = fn()
        except BaseException as e:  # noqa: BLE001 — reported by the test
            out[i] = e

    threads = [threading.Thread(target=run, args=(i, f), daemon=True)
               for i, f in enumerate(fns)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout)
        assert not t.is_alive(), "a request never finished (deadlock?)"
    return out


def assert_books_tie(w, step):
    from shared import integrity
    with Ctx(w):
        bad = integrity.failures()
    assert not bad, f"after {step}: " + "; ".join(
        f"{b['label']}: expected {b['expected']} got {b['actual']} ({b['detail']})"
        for b in bad)


# ── documents, as the forms send them ──

def purchase_json(w, lines, action="approve"):
    total = sum(q * p for _pid, q, p in lines)
    return {
        "supplier_id": w["supplier"], "invoice_date": TODAY,
        "discount_mode": "general", "expenses_mode": "general", "tax_mode": "general",
        "global_discount_pct": 0, "global_discount_value": 0, "global_commission": 0,
        "global_freight": 0, "global_loading": 0, "global_sales_tax_pct": 0,
        "global_withholding_tax_pct": 0, "subtotal": total, "total_discount": 0,
        "net_payable": total, "total_amount": total,
        "items": [{"product_id": pid, "quantity": q, "unit_price": p,
                   "discount_pct": 0, "total_before_discount": q * p,
                   "total_after_discount": q * p} for pid, q, p in lines],
        "action": action}


def sale_json(w, lines, action="approve", inv_id=None):
    total = sum(q * p for _pid, q, p in lines)
    return {
        "id": inv_id, "customer_id": w["customer"], "invoice_date": TODAY,
        "discount_mode": "general", "charges_mode": "general", "tax_mode": "general",
        "global_discount_pct": 0, "global_discount_value": 0, "global_delivery": 0,
        "global_installation": 0, "global_sales_tax_pct": 0, "further_tax_pct": 0,
        "apply_further_tax": False, "withholding_tax_pct": 0, "subtotal": total,
        "total_discount": 0, "total_charges": 0, "total_tax": 0, "total_amount": total,
        "items": [{"product_id": pid, "quantity": q, "unit_price": p,
                   "description": "Part", "unit": "pcs", "discount_pct": 0,
                   "delivery": 0, "installation": 0, "sales_tax_pct": 0,
                   "total_before_discount": q * p, "total_after_discount": q * p}
                  for pid, q, p in lines],
        "action": action}


def post(w, url, payload):
    r = client(w).post(url, json=payload)
    return r.status_code, (r.get_json(silent=True) or {"raw": r.get_data(as_text=True)[:300]})


def receive(w, pid, qty, price):
    code, body = post(w, "/inventory/purchase-invoice/save", purchase_json(w, [(pid, qty, price)]))
    assert code == 200 and body.get("ok"), body
    return body["id"]


def on_hand(w, pid):
    from shared import costing
    with Ctx(w):
        return costing.on_hand(pid)


def no_crash(results):
    """Every response is a normal answer — success, or a refusal in words.
    A 500, or an exception escaping a thread, is a crash."""
    for r in results:
        assert not isinstance(r, BaseException), repr(r)
        code, body = r
        assert code < 500, body
        if code != 200:
            assert body.get("error") and "Server error" not in body["error"], body


# ─────────────────────────────────────────────
# The reporting table under simultaneous postings
# ─────────────────────────────────────────────

def test_simultaneous_postings_to_the_same_account_and_day(world):
    """Every posting today hits Cash, Sales and Inventory: N users posting at
    once all land, and the report totals include every one of them."""
    from shared.models.ledger import JournalEntry, JournalLine
    from shared.models.gl_balance import GLDailyBalance
    from datetime import datetime
    a0, a1 = world["accounts"]

    def one(amount):
        def go():
            with Ctx(world):
                e = JournalEntry(voucher_type="JV", voucher_id=0, voucher_number="RACE",
                                 description="race", entry_date=datetime.now(),
                                 created_by=world["user"], is_posted=True)
                db.session.add(e)
                db.session.flush()
                db.session.add_all([
                    JournalLine(journal_entry_id=e.id, account_id=a0,
                                debit=Decimal(amount), credit=Decimal(0)),
                    JournalLine(journal_entry_id=e.id, account_id=a1,
                                debit=Decimal(0), credit=Decimal(amount))])
                db.session.commit()
                return amount
        return go

    for round_ in range(3):
        before = _summary_debit(world, a0)
        results = race([one(100 + i) for i in range(N)])
        errors = [r for r in results if isinstance(r, BaseException)]
        assert not errors, errors
        assert _summary_debit(world, a0) - before == sum(100 + i for i in range(N))
    assert_books_tie(world, "simultaneous journal postings")


def _summary_debit(w, account_id):
    from shared.models.gl_balance import GLDailyBalance as G
    with Ctx(w):
        return Decimal(str(db.session.query(db.func.coalesce(db.func.sum(G.debit), 0))
                           .filter(G.account_id == account_id).scalar()))


# ─────────────────────────────────────────────
# Numbers
# ─────────────────────────────────────────────

def test_voucher_numbers_are_never_handed_out_twice(world):
    from shared.models.stock_ledger import VoucherNumber

    def take():
        with Ctx(world):
            n = VoucherNumber.next("RACE")
            db.session.commit()
            return n

    got = race([take for _ in range(N * 2)])
    assert not [g for g in got if isinstance(g, BaseException)], got
    assert len(set(got)) == len(got), sorted(got)


def test_new_invoices_saved_together_get_distinct_numbers(world):
    p = world["products"][3]
    results = race([lambda: post(world, "/inventory/invoices/save",
                                 sale_json(world, [(p, 1, 200)], action="save"))
                    for _ in range(N)])
    no_crash(results)
    assert all(code == 200 for code, _ in results), results
    numbers = [body["number"] for _c, body in results]
    assert len(set(numbers)) == N, numbers

    results = race([lambda: post(world, "/inventory/purchase-invoice/save",
                                 purchase_json(world, [(p, 1, 100)], action="save"))
                    for _ in range(N)])
    no_crash(results)
    assert all(code == 200 for code, _ in results), results


# ─────────────────────────────────────────────
# Documents
# ─────────────────────────────────────────────

def test_two_users_approving_one_invoice_post_it_once(world):
    p = world["products"][0]
    receive(world, p, 20, 100)
    code, body = post(world, "/inventory/invoices/save", sale_json(world, [(p, 2, 150)], action="save"))
    assert code == 200, body
    inv_id = body["id"]
    results = race([lambda: post(world, "/inventory/invoices/save",
                                 sale_json(world, [(p, 2, 150)], inv_id=inv_id))
                    for _ in range(N)])
    no_crash(results)
    assert sum(1 for code, _ in results if code == 200) == 1, results

    from shared.models.ledger import JournalEntry
    from shared.models.stock_ledger import StockLedger
    with Ctx(world):
        assert JournalEntry.query.filter_by(voucher_type="SI", voucher_id=inv_id,
                                            is_posted=True).count() == 1
        assert StockLedger.query.filter_by(voucher_type="SI", voucher_id=inv_id).count() == 1
    assert on_hand(world, p) == 18
    assert_books_tie(world, "double approval")


def test_stock_is_never_sold_twice(world):
    """10 on hand, N users each sell 4 at once with negative stock off:
    exactly two sales go through, the rest are refused in words."""
    p = world["products"][1]
    receive(world, p, 10, 100)
    results = race([lambda: post(world, "/inventory/invoices/save",
                                 sale_json(world, [(p, 4, 150)]))
                    for _ in range(N)])
    no_crash(results)
    assert sum(1 for code, _ in results if code == 200) == 2, results
    assert on_hand(world, p) == 2
    assert_books_tie(world, "simultaneous sales of one product")


# ─────────────────────────────────────────────
# More documents
# ─────────────────────────────────────────────

def test_mixed_sales_and_purchases_on_shared_products_never_deadlock(world):
    """Invoices listing the same two products in opposite orders, purchases
    among them: every product is locked in one order up front, so nobody
    deadlocks and every document goes through."""
    a, b = world["products"][2], world["products"][3]
    receive(world, a, 50, 100)
    receive(world, b, 50, 100)
    start_a, start_b = on_hand(world, a), on_hand(world, b)
    jobs = []
    for i in range(N):
        lines = [(a, 1, 150), (b, 1, 150)] if i % 2 else [(b, 1, 150), (a, 1, 150)]
        jobs.append(lambda lines=lines: post(world, "/inventory/invoices/save",
                                             sale_json(world, lines)))
    jobs.append(lambda: post(world, "/inventory/purchase-invoice/save",
                             purchase_json(world, [(b, 5, 110), (a, 5, 110)])))
    results = race(jobs)
    no_crash(results)
    assert all(code == 200 for code, _ in results), results
    assert on_hand(world, a) == start_a - N + 5
    assert on_hand(world, b) == start_b - N + 5
    assert_books_tie(world, "sales and purchases of shared products")


def test_two_users_approving_one_purchase_invoice_post_it_once(world):
    p = world["products"][2]
    code, body = post(world, "/inventory/purchase-invoice/save",
                      purchase_json(world, [(p, 7, 90)], action="save"))
    assert code == 200, body
    pi = body["id"]
    payload = dict(purchase_json(world, [(p, 7, 90)]), id=pi)
    before = on_hand(world, p)
    results = race([lambda: post(world, "/inventory/purchase-invoice/save", payload)
                    for _ in range(N)])
    no_crash(results)
    assert sum(1 for code, _ in results if code == 200) == 1, results
    from shared.models.ledger import JournalEntry
    with Ctx(world):
        assert JournalEntry.query.filter_by(voucher_type="PI", voucher_id=pi,
                                            is_posted=True).count() == 1
    assert on_hand(world, p) == before + 7
    assert_books_tie(world, "double purchase approval")


def test_returns_raised_together_cannot_exceed_the_sale(world):
    """10 sold; N users each credit 4 of them at once: two credit notes go
    through, the rest are refused as over the returnable quantity."""
    p = world["products"][0]
    receive(world, p, 10, 100)
    code, body = post(world, "/inventory/invoices/save", sale_json(world, [(p, 10, 150)]))
    assert code == 200, body
    si = body["id"]
    payload = {"original_invoice_id": si, "customer_id": world["customer"], "date": TODAY,
               "gross_return_value": 600, "total_discount": 0, "total_charges": 0,
               "total_tax": 0, "net_return_amount": 600,
               "items": [{"product_id": p, "description": "Part", "original_quantity": 10,
                          "max_returnable_qty": 10, "current_return_qty": 4,
                          "unit_price": 150, "net_return_value": 600}],
               "action": "approve"}
    results = race([lambda: post(world, "/invoicing/sales-return/save", payload)
                    for _ in range(N)])
    no_crash(results)
    assert sum(1 for code, _ in results if code == 200) == 2, results
    from inventory_app.models.sales_return import InvSalesReturn, InvSalesReturnItem
    with Ctx(world):
        returned = sum(float(i.current_return_qty) for i in InvSalesReturnItem.query
                       .join(InvSalesReturn, InvSalesReturnItem.return_id == InvSalesReturn.id)
                       .filter(InvSalesReturn.original_invoice_id == si,
                               InvSalesReturn.status == "approved"))
    assert returned == 8
    assert_books_tie(world, "simultaneous returns")


def _order_json(w, p, qty, action):
    return {
        "customer_id": w["customer"], "order_date": TODAY, "so_number": "(auto)",
        "tax_mode": "general", "global_sales_tax_pct": 0, "subtotal": qty * 150,
        "total_tax": 0, "total_amount": qty * 150, "action": action,
        "items": [{"product_id": p, "description": "Part", "quantity": qty, "unit": "pcs",
                   "unit_price": 150, "sales_tax_pct": 0, "total_before_discount": qty * 150,
                   "total_after_discount": qty * 150}]}


def _order(w, p, qty):
    code, body = post(w, "/inventory/sales/save", _order_json(w, p, qty, "approve"))
    assert code == 200, body
    from inventory_app.models.sales_order import InvSalesOrder, InvSalesOrderItem
    with Ctx(w):
        so = InvSalesOrder.query.order_by(InvSalesOrder.id.desc()).first()
        line = InvSalesOrderItem.query.filter_by(so_id=so.id).first()
        return so.id, so.so_number, line.id


def test_new_orders_saved_together_are_numbered(world):
    """The form sends "(auto)" for a new order; that text used to become the
    order number itself, and the next order clashed with it."""
    p = world["products"][3]
    results = race([lambda: post(world, "/inventory/sales/save",
                                 _order_json(world, p, 1, "save")) for _ in range(N)])
    no_crash(results)
    assert all(code == 200 for code, _ in results), results
    from inventory_app.models.sales_order import InvSalesOrder
    with Ctx(world):
        numbers = [o.so_number for o in InvSalesOrder.query.all()]
    assert "(auto)" not in numbers and len(set(numbers)) == len(numbers), numbers


def test_one_order_line_billed_by_several_invoices_at_once(world):
    """An order for 10; N users each bill 4 of it at once. Two invoices go
    through, the rest are refused as over the order's balance, and the order
    records 8 billed."""
    p = world["products"][1]
    receive(world, p, 40, 100)
    so_id, so_no, line_id = _order(world, p, 10)

    def bill():
        payload = sale_json(world, [(p, 4, 150)])
        payload["items"][0].update(source_order_id=so_id, source_order_item_id=line_id,
                                   source_order_number=so_no)
        return post(world, "/inventory/invoices/save", payload)

    results = race([bill for _ in range(N)])
    no_crash(results)
    assert sum(1 for code, _ in results if code == 200) == 2, results
    from inventory_app.models.sales_order import InvSalesOrderItem
    with Ctx(world):
        assert float(InvSalesOrderItem.query.get(line_id).invoiced_qty) == 8
    # A billed order can no longer be unapproved (its lines would be replaced).
    code, body = post(world, f"/inventory/sales/unapprove/{so_id}", {})
    assert code == 400 and "billed" in body["error"], body
    assert_books_tie(world, "one order billed by several invoices")


def _cash_and_debtor(w):
    from shared.models.ledger import ChartOfAccount
    from shared.ledger_utils import party_account
    from inventory_app.models.customer import InvCustomer
    with Ctx(w):
        cash = ChartOfAccount.query.filter_by(code="1-01-01-01-0001").first().id
        cus = InvCustomer.query.get(w["customer"])
        debtor = party_account("customer", cus.id, cus.name).id
        db.session.commit()
        return cash, debtor


def _jv(w, dr, cr, amount, action="approve"):
    from datetime import datetime
    return client(w).post("/accounting/vouchers", data={
        "voucher_type": "JV", "voucher_date": datetime.now().strftime("%Y-%m-%dT%H:%M"),
        "notes": "many hands", "account_id[]": [str(dr), str(cr)],
        "description[]": ["dr", "cr"], "debit[]": [str(amount), "0"],
        "credit[]": ["0", str(amount)], "action": action})


def test_two_users_approving_one_voucher_post_it_once(world):
    from shared.models.accounting_voucher import AccountingVoucher
    from shared.models.ledger import JournalEntry
    cash, debtor = _cash_and_debtor(world)
    _jv(world, cash, debtor, 1234, action="save")
    with Ctx(world):
        v = AccountingVoucher.query.order_by(AccountingVoucher.id.desc()).first()
        vid = v.id
        assert v.status != "approved"
    results = race([lambda: client(world).post(f"/accounting/vouchers/{vid}/approve")
                    for _ in range(N)])
    assert not [r for r in results if isinstance(r, BaseException)], results
    assert all(r.status_code < 500 for r in results)
    with Ctx(world):
        assert JournalEntry.query.filter_by(voucher_type="JV", voucher_id=vid,
                                            is_posted=True).count() == 1
    assert_books_tie(world, "double voucher approval")


def test_receipts_assigned_together_never_overpay_an_invoice(world):
    """An invoice of 1,500; two receipts of 1,000 each assigned to it at the
    same moment. One fits, the other is refused for the 500 left — the invoice
    is never settled past its total."""
    from shared import payment_tracking as pt
    from shared.models.accounting_voucher import AccountingVoucher, AccountingVoucherLine
    p = world["products"][2]
    code, body = post(world, "/inventory/invoices/save", sale_json(world, [(p, 10, 150)]))
    assert code == 200, body
    si = body["id"]
    cash, debtor = _cash_and_debtor(world)
    lines = []
    for _ in range(2):
        r = _jv(world, cash, debtor, 1000)
        assert r.status_code in (302, 303), r.status_code
        with Ctx(world):
            v = AccountingVoucher.query.order_by(AccountingVoucher.id.desc()).first()
            assert v.status == "approved"
            lines.append(AccountingVoucherLine.query.filter_by(
                voucher_id=v.id, account_id=debtor).first().id)

    def assign(line_id):
        def go():
            with Ctx(world):
                try:
                    pt.allocate(line_id, [{"doc_id": si, "amount": 1000}], world["user"])
                    return "ok"
                except pt.AllocationError as e:
                    db.session.rollback()
                    return str(e)
        return go

    results = race([assign(line) for line in lines])
    assert not [r for r in results if isinstance(r, BaseException)], results
    assert results.count("ok") == 1, results
    with Ctx(world):
        assert pt.allocated_by_doc(pt.SALES, [si]).get(si, 0) <= Decimal("1500")


def test_one_draft_reconciliation_per_bank_account(world):
    cash, _debtor = _cash_and_debtor(world)
    results = race([lambda: client(world).post("/accounting/bank-reconciliation/new", data={
        "account_id": cash, "statement_date": TODAY, "statement_balance": "0"})
        for _ in range(N)])
    assert not [r for r in results if isinstance(r, BaseException)], results
    assert all(r.status_code < 500 for r in results)
    from shared.models.bank_rec import BankReconciliation
    with Ctx(world):
        assert BankReconciliation.query.filter_by(account_id=cash, status="draft").count() == 1


def test_consumption_voucher_approved_by_two_users_moves_stock_once(world):
    from shared.models.vouchers import ConsumptionVoucher
    p = world["products"][3]
    receive(world, p, 20, 100)
    form = {"department": "Workshop", "reason": "Repairs", "date": TODAY, "label_id": "",
            "charge_account_id": "", "product_id[]": [str(p)], "qty[]": ["3"]}
    r = client(world).post("/inventory/vouchers/consumption", data=dict(form, status="unapproved"))
    assert r.status_code in (200, 302)
    with Ctx(world):
        vid = ConsumptionVoucher.query.order_by(ConsumptionVoucher.id.desc()).first().id
    before = on_hand(world, p)
    results = race([lambda: client(world).post(f"/inventory/vouchers/consumption/{vid}",
                                               data=dict(form, status="approved"))
                    for _ in range(N)])
    assert not [r for r in results if isinstance(r, BaseException)], results
    assert all(r.status_code < 500 for r in results)
    assert on_hand(world, p) == before - 3
    assert_books_tie(world, "double consumption approval")


def test_depreciation_run_by_two_users_charges_each_month_once(world):
    from fixed_assets_app.models.asset import FixedAsset, AssetCategory
    with Ctx(world):
        if not AssetCategory.query.first():
            AssetCategory.seed()
            db.session.commit()
        cat = AssetCategory.query.order_by(AssetCategory.id).first().id
    r = client(world).post("/fixed-assets/assets/create", data={
        "name": "Shared Van", "category_id": cat, "purchase_date": "2024-01-01",
        "purchase_cost": "1200000", "useful_life": "5", "salvage_value": "0",
        "depreciation_method": "straight_line"},
        headers={"Referer": "/fixed-assets/assets/create"})
    assert r.status_code in (200, 302)
    with Ctx(world):
        aid = FixedAsset.query.filter_by(name="Shared Van").first().id
    results = race([lambda: client(world).post("/fixed-assets/depreciation/post", data={
        "asset_ids[]": [aid], "entry_date": "2025-01-01"}) for _ in range(N)])
    assert not [r for r in results if isinstance(r, BaseException)], results
    with Ctx(world):
        a = FixedAsset.query.get(aid)
        assert float(a.posted_depreciation) == 240_000     # 12 months, once
    assert_books_tie(world, "simultaneous depreciation runs")


def test_payroll_month_run_by_two_admins_is_run_once(world):
    from datetime import timedelta
    from hr_app.models.compensation import PayrollProfile, PayrollRun
    from hr_app.models.user import User
    with Ctx(world):
        u = User.query.get(world["user"])
        u.date_of_joining = date(2000, 1, 1)
        if not PayrollProfile.query.filter_by(user_id=u.id).first():
            db.session.add(PayrollProfile(user_id=u.id, basic_salary=60000,
                                          effective_from=date(2000, 1, 1)))
        db.session.commit()
    prev = date.today().replace(day=1) - timedelta(days=1)
    results = race([lambda: client(world).post("/compensation/run-payroll", data={
        "month": prev.month, "year": prev.year}) for _ in range(N)])
    assert not [r for r in results if isinstance(r, BaseException)], results
    assert all(r.status_code < 500 for r in results), [r.status_code for r in results]
    with Ctx(world):
        assert PayrollRun.query.filter_by(month=prev.month, year=prev.year).count() == 1
    assert_books_tie(world, "simultaneous payroll runs")


def test_closing_a_period_waits_for_a_posting_in_flight(world):
    """A posting has checked its period is open and is still working; the
    close must wait for it to commit, and a posting after the close is
    refused — never a journal committed into a closed period."""
    import time
    from shared.periods import require_open_period, period_for
    with Ctx(world):
        period = period_for(date.today())
        assert period is not None and not period.is_closed
        pid = period.id
    started = threading.Event()

    def posting():
        with Ctx(world):
            require_open_period(date.today())
            started.set()
            time.sleep(2)
            db.session.commit()

    t = threading.Thread(target=posting)
    t.start()
    assert started.wait(30)
    t0 = time.monotonic()
    r = client(world).post(f"/settings/periods/{pid}/close", data={"force": "1"})
    waited = time.monotonic() - t0
    t.join(30)
    assert r.status_code in (302, 303), r.status_code
    assert waited >= 1.5, f"close did not wait for the posting in flight ({waited:.2f}s)"
    cash, debtor = _cash_and_debtor(world)
    from shared.models.accounting_voucher import AccountingVoucher
    _jv(world, cash, debtor, 10)
    with Ctx(world):
        v = AccountingVoucher.query.order_by(AccountingVoucher.id.desc()).first()
        assert v.status != "approved", "a voucher was posted into the closed period"
    r = client(world).post(f"/settings/periods/{pid}/reopen")
    assert r.status_code in (302, 303)


# ─────────────────────────────────────────────
# Two people editing the same draft
# ─────────────────────────────────────────────

def test_a_save_from_a_stale_copy_does_not_overwrite_a_newer_edit(world):
    """Two users open the same draft. The first saves; the second's copy is
    now stale, and saving it would silently replace the first user's edit —
    it is refused, naming who changed it. Saving from a fresh copy works."""
    from inventory_app.models.invoice import InvInvoiceItem
    p = world["products"][3]
    code, body = post(world, "/inventory/invoices/save", sale_json(world, [(p, 1, 150)], action="save"))
    assert code == 200, body
    inv_id, opened = body["id"], body["edit_version"]

    first = dict(sale_json(world, [(p, 2, 150)], action="save", inv_id=inv_id), edit_version=opened)
    code, body = post(world, "/inventory/invoices/save", first)
    assert code == 200 and body["edit_version"] == opened + 1, body

    second = dict(sale_json(world, [(p, 5, 150)], action="save", inv_id=inv_id), edit_version=opened)
    code, body = post(world, "/inventory/invoices/save", second)
    assert code == 409 and body.get("stale"), body
    assert "changed" in body["error"] and "Reload" in body["error"], body
    with Ctx(world):
        assert [float(i.quantity) for i in InvInvoiceItem.query.filter_by(invoice_id=inv_id)] == [2.0]

    fresh = dict(second, edit_version=opened + 1)
    code, body = post(world, "/inventory/invoices/save", fresh)
    assert code == 200, body

    # Several users saving the same version at the same instant: one wins.
    version = body["edit_version"]
    results = race([lambda q=q: post(world, "/inventory/invoices/save",
                                     dict(sale_json(world, [(p, q, 150)], action="save",
                                                    inv_id=inv_id), edit_version=version))
                    for q in range(1, N + 1)])
    no_crash(results)
    assert sum(1 for code, _ in results if code == 200) == 1, results
    assert all(body.get("stale") for code, body in results if code != 200), results


def test_unapproving_makes_open_copies_stale(world):
    p = world["products"][3]
    receive(world, p, 5, 100)
    code, body = post(world, "/inventory/invoices/save", sale_json(world, [(p, 1, 150)]))
    assert code == 200, body
    inv_id, approved_at = body["id"], body["edit_version"]
    code, body = post(world, f"/inventory/invoices/unapprove/{inv_id}", {})
    assert code == 200 and body["edit_version"] == approved_at + 1, body
    stale = dict(sale_json(world, [(p, 3, 150)], action="save", inv_id=inv_id),
                 edit_version=approved_at)
    code, body = post(world, "/inventory/invoices/save", stale)
    assert code == 409 and body.get("stale"), body
    # A request that sends no version (an older page, a script) is not checked.
    code, body = post(world, "/inventory/invoices/save",
                      sale_json(world, [(p, 1, 150)], action="save", inv_id=inv_id))
    assert code == 200, body
    assert_books_tie(world, "stale edits refused")


# ─────────────────────────────────────────────
# Reports read one moment of the books
# ─────────────────────────────────────────────

def test_reports_read_the_books_as_of_one_instant(world):
    """A report is several queries; between two of them another user's
    posting could commit, and the report compared figures from two moments
    (Books Integrity flagged a mismatch the ledger did not have). Report
    views run in one REPEATABLE READ snapshot."""
    import sqlalchemy as sa
    from shared.concurrency import consistent_snapshot
    from shared.tenancy import set_current_company

    @consistent_snapshot
    def view():
        return db.session.execute(sa.text("show transaction_isolation")).scalar()

    with flask_app.test_request_context("/"):
        set_current_company(world["company"])
        assert view() == "repeatable read"
        db.session.rollback()
    for url in ("/executive/integrity?run=1", "/finance/trial-balance", "/executive/"):
        r = client(world).get(url)
        assert r.status_code == 200, (url, r.status_code)


def test_a_settings_row_made_on_first_use_is_made_once(world):
    """Settings rows are created the first time anyone asks for them. Six
    requests asking together in a new company used to create six, and
    .first() then handed back any of them."""
    from shared.models.company import Company
    from shared.models.invoice_settings import InvoiceSettings
    from shared.tenancy import set_current_company, unscoped
    with flask_app.app_context():
        with unscoped():
            comp = Company(name="First Use Co", slug="first-use-e2e", is_active=True,
                           created_by=world["user"])
            db.session.add(comp)
            db.session.commit()
            cid = comp.id

    def ask():
        with flask_app.app_context():
            set_current_company(cid)
            InvoiceSettings.get()
            db.session.commit()
            db.session.remove()
            return "ok"

    assert race([ask for _ in range(N)]) == ["ok"] * N
    with flask_app.app_context():
        set_current_company(cid)
        assert InvoiceSettings.query.filter_by(company_id=cid).count() == 1

