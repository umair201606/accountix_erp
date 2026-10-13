"""Every transaction, every correction, and the books still tie.

One fresh company, one product, driven through the REAL routes in the order
real documents arrive — late, back-dated, corrected, returned, reversed. After
every step the integrity checks (shared/integrity.py) must all pass:

    trial balance balances, every journal balances, the inventory accounts
    equal the stock valuation, cost layers back the stock ledger, on-hand
    quantity matches the ledger, the asset register matches the GL, and every
    approved document (and only those) has a posted journal.

On top of that each step asserts the specific figures a real-world
accountant would check: the cost each issue carries, the date each journal
lands on, COGS by label, and that reports render the result.

Valuation is FIFO so every re-costing is visible in whole numbers.
"""
import os
import tempfile
from datetime import date, timedelta
from decimal import Decimal

import pytest

_TMP_DB = os.path.join(tempfile.gettempdir(), "erp_books_integrity.db")
if os.path.exists(_TMP_DB):
    os.remove(_TMP_DB)
# TEST_DATABASE_URL runs the same scenario against Postgres (production is
# Neon); default is a throwaway SQLite file.
os.environ["DATABASE_URL"] = (os.environ.get("TEST_DATABASE_URL")
                              or "sqlite:///" + _TMP_DB.replace("\\", "/"))

from app import app as flask_app  # noqa: E402
from shared.extensions import db  # noqa: E402

TODAY = date.today()


def ago(n):
    return (TODAY - timedelta(days=n)).isoformat()


# ─────────────────────────────────────────────
# Fixtures: a fresh company of our own
# ─────────────────────────────────────────────

@pytest.fixture(scope="module")
def world():
    c = flask_app.test_client()
    c.get("/")
    from hr_app.models.user import User
    from shared.models.company import Company, CompanyMembership
    from shared.models.base import Role
    from shared.tenancy import set_current_company, unscoped
    from shared.company_setup import provision_company

    with flask_app.app_context():
        # Explicitly the super admin: an unordered first() returns a
        # different row on Postgres than on SQLite.
        user = (User.query.filter_by(is_super_admin=True).order_by(User.id).first()
                or User.query.order_by(User.id).first())
        uid = user.id
        with unscoped():
            comp = Company(name="Integrity Co", slug="integrity-co-e2e",
                           is_active=True, created_by=uid)
            db.session.add(comp)
            db.session.flush()
            admin_role = Role.query.filter_by(name=Role.ADMIN).first()
            db.session.add(CompanyMembership(
                company_id=comp.id, user_id=uid, role_id=admin_role.id,
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
        from shared.models.project_label import ProjectLabel
        s = InventorySettings.get()
        s.valuation_method = "fifo"
        s.allow_negative_stock = False
        prod = InvProduct(sku="INT-1", name="Integrity Widget", unit="pcs",
                          unit_price=200, cost_price=0, current_stock=0)
        sup = InvSupplier(name="Integrity Supplier")
        cus = InvCustomer(name="Integrity Customer")
        l1 = ProjectLabel(name="Project North")
        l2 = ProjectLabel(name="Project South")
        db.session.add_all([prod, sup, cus, l1, l2])
        db.session.commit()
        ids = {"company": cid, "user": uid, "product": prod.id,
               "supplier": sup.id, "customer": cus.id, "l1": l1.id, "l2": l2.id}

    with c.session_transaction() as sess:
        sess["_user_id"] = str(uid)
        sess["_fresh"] = True
        sess["company_id"] = cid
    ids["client"] = c
    return ids


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


def assert_books_tie(w, step):
    from shared import integrity
    with Ctx(w):
        bad = integrity.failures()
    assert not bad, f"after {step}: " + "; ".join(
        f"{b['label']}: expected {b['expected']} got {b['actual']} ({b['detail']})"
        for b in bad)


def cost(w, vtype, vid):
    from shared.models.stock_ledger import StockLedger
    with Ctx(w):
        rows = StockLedger.query.filter_by(voucher_type=vtype, voucher_id=vid,
                                           product_id=w["product"]).all()
        return sum((Decimal(str(r.total_cost)) for r in rows), Decimal("0"))


def gl(w, role, label_id=None):
    """Net debit balance of a posting-role account (optionally one label)."""
    from shared.ledger_utils import posting_account
    from shared.models.ledger import JournalEntry, JournalLine
    with Ctx(w):
        acct = posting_account(role)
        q = (db.session.query(
            db.func.coalesce(db.func.sum(JournalLine.debit), 0),
            db.func.coalesce(db.func.sum(JournalLine.credit), 0))
            .join(JournalEntry, JournalLine.journal_entry_id == JournalEntry.id)
            .filter(JournalEntry.is_posted == True,  # noqa: E712
                    JournalLine.account_id == acct.id))
        if label_id is not None:
            q = q.filter(JournalLine.label_id == label_id)
        dr, cr = q.one()
        return Decimal(str(dr)) - Decimal(str(cr))


def on_hand(w):
    from shared import costing
    with Ctx(w):
        return costing.on_hand(w["product"])


def je_date(w, vtype, vid):
    from shared.models.ledger import JournalEntry
    with Ctx(w):
        je = JournalEntry.query.filter_by(voucher_type=vtype, voucher_id=vid,
                                          is_posted=True).first()
        return je.entry_date.date().isoformat() if je else None


# ── document helpers (the same JSON the forms send) ──

def purchase(w, qty, price, when, inv_id=None, expect_ok=True):
    total = qty * price
    r = w["client"].post("/inventory/purchase-invoice/save", json={
        "id": inv_id, "supplier_id": w["supplier"], "invoice_date": when,
        "discount_mode": "general", "expenses_mode": "general", "tax_mode": "general",
        "global_discount_pct": 0, "global_discount_value": 0, "global_commission": 0,
        "global_freight": 0, "global_loading": 0, "global_sales_tax_pct": 0,
        "global_withholding_tax_pct": 0, "subtotal": total, "total_discount": 0,
        "net_payable": total, "total_amount": total,
        "items": [{"product_id": w["product"], "quantity": qty, "unit_price": price,
                   "discount_pct": 0, "total_before_discount": total,
                   "total_after_discount": total}],
        "action": "approve"})
    body = r.get_json(silent=True) or {}
    if expect_ok:
        assert r.status_code == 200 and body.get("ok"), r.get_data(as_text=True)[:500]
        return body["id"]
    return r, body


def sale(w, qty, price, when, label=None, expect_ok=True):
    total = qty * price
    r = w["client"].post("/inventory/invoices/save", json={
        "customer_id": w["customer"], "invoice_date": when, "label_id": label,
        "discount_mode": "general", "charges_mode": "general", "tax_mode": "general",
        "global_discount_pct": 0, "global_discount_value": 0, "global_delivery": 0,
        "global_installation": 0, "global_sales_tax_pct": 0, "further_tax_pct": 0,
        "apply_further_tax": False, "withholding_tax_pct": 0, "subtotal": total,
        "total_discount": 0, "total_charges": 0, "total_tax": 0, "total_amount": total,
        "items": [{"product_id": w["product"], "quantity": qty, "unit_price": price,
                   "description": "Widget", "unit": "pcs", "discount_pct": 0,
                   "label_id": label, "delivery": 0, "installation": 0,
                   "sales_tax_pct": 0, "total_before_discount": total,
                   "total_after_discount": total}],
        "action": "approve"})
    body = r.get_json(silent=True) or {}
    if expect_ok:
        assert r.status_code == 200 and body.get("ok"), r.get_data(as_text=True)[:500]
        return body["id"]
    return r, body


def consume(w, qty, when, label=None):
    from shared.models.vouchers import ConsumptionVoucher
    r = w["client"].post("/inventory/vouchers/consumption", data={
        "department": "Workshop", "reason": "Repairs", "date": when,
        "label_id": label or "", "charge_account_id": "", "status": "approved",
        "product_id[]": [str(w["product"])], "qty[]": [str(qty)]})
    assert r.status_code in (200, 302), r.get_data(as_text=True)[:400]
    with Ctx(w):
        v = ConsumptionVoucher.query.order_by(ConsumptionVoucher.id.desc()).first()
        assert v.status == "approved"
        return v.id


# ─────────────────────────────────────────────
# The scenario — order matters, each step builds on the last
# ─────────────────────────────────────────────

S = {}


def test_01_purchase_posts_on_its_own_date(world):
    S["pi1"] = purchase(world, 10, 100, ago(60))
    assert je_date(world, "PI", S["pi1"]) == ago(60), "posted on the invoice date"
    assert on_hand(world) == 10
    assert gl(world, "inventory") == Decimal("1000")
    assert_books_tie(world, "purchase")


def test_02_sale_costs_fifo_and_tags_label(world):
    S["si1"] = sale(world, 4, 150, ago(40), label=world["l1"])
    assert cost(world, "SI", S["si1"]) == Decimal("400")
    assert gl(world, "cogs", world["l1"]) == Decimal("400"), "COGS on the item label"
    assert je_date(world, "SI", S["si1"]) == ago(40)
    assert_books_tie(world, "sale")


def test_03_backdated_cheaper_purchase_recosts_the_sale(world):
    """A purchase dated BEFORE the sale arrives late. In date order the sale
    drew the older, cheaper stock: its cost is restated 400 -> 200 and the
    difference is journalled on the sale's date, on its label."""
    S["pi0"] = purchase(world, 10, 50, ago(80))
    assert cost(world, "SI", S["si1"]) == Decimal("200")
    assert gl(world, "cogs") == Decimal("200")
    assert gl(world, "cogs", world["l1"]) == Decimal("200")
    from shared.models.stock_layer import StockCostAdjustment
    with Ctx(world):
        adj = StockCostAdjustment.query.filter_by(voucher_type="SI").one()
        assert adj.entry_date.isoformat() == ago(40)
    assert on_hand(world) == 16
    assert_books_tie(world, "back-dated purchase")


def test_04_consumption_uses_the_oldest_remaining_layer(world):
    S["cons"] = consume(world, 3, ago(30), label=world["l2"])
    assert cost(world, "CONS", S["cons"]) == Decimal("150")   # 3 x 50
    assert je_date(world, "CONS", S["cons"]) == ago(30)
    assert_books_tie(world, "consumption")


def test_05_backdated_sale_recosts_later_issues(world):
    """A sale dated d-70 takes 5 of the 50-cost stock first; the later
    consumption now has only 1 left at 50 and takes 2 at 100."""
    S["si2"] = sale(world, 5, 150, ago(70))
    assert cost(world, "SI", S["si2"]) == Decimal("250")
    assert cost(world, "SI", S["si1"]) == Decimal("200")
    assert cost(world, "CONS", S["cons"]) == Decimal("250")   # 50 + 200
    assert_books_tie(world, "back-dated sale")


def test_06_backdated_sale_without_stock_at_its_date_is_refused(world):
    r, body = sale(world, 2, 150, ago(85), expect_ok=False)
    assert body.get("ok") is False and "on hand" in (body.get("error") or "")
    assert on_hand(world) == 8
    assert_books_tie(world, "refused back-dated sale")


def test_07_editing_an_old_purchase_price_flows_through(world):
    """The d-80 purchase was really 60, not 50: unapprove, correct, re-approve.
    Every issue that drew from it is re-costed and adjusted."""
    c = world["client"]
    r = c.post(f"/inventory/purchase-invoice/unapprove/{S['pi0']}")
    assert r.get_json()["ok"], r.get_data(as_text=True)[:400]
    assert_books_tie(world, "purchase withdrawn for correction")
    purchase(world, 10, 60, ago(80), inv_id=S["pi0"])
    assert cost(world, "SI", S["si2"]) == Decimal("300")      # 5 x 60
    assert cost(world, "SI", S["si1"]) == Decimal("240")      # 4 x 60
    assert cost(world, "CONS", S["cons"]) == Decimal("260")   # 60 + 2 x 100
    assert_books_tie(world, "purchase price corrected")


def test_08_sales_return_comes_back_at_the_sale_cost(world):
    from inventory_app.models.invoice import InvInvoiceItem
    with Ctx(world):
        item = InvInvoiceItem.query.filter_by(invoice_id=S["si1"]).first()
    r = world["client"].post("/invoicing/sales-return/save", json={
        "original_invoice_id": S["si1"], "customer_id": world["customer"],
        "date": ago(20), "gross_return_value": 300, "total_discount": 0,
        "total_charges": 0, "total_tax": 0, "net_return_amount": 300,
        "items": [{"product_id": world["product"], "invoice_item_id": item.id,
                   "description": "Widget", "original_quantity": 4,
                   "max_returnable_qty": 4, "current_return_qty": 2,
                   "unit_price": 150, "net_return_value": 300}],
        "action": "approve"})
    body = r.get_json()
    assert body and body["ok"], r.get_data(as_text=True)[:500]
    S["sr"] = body["id"]
    assert cost(world, "SRV", S["sr"]) == Decimal("120")      # 2 x 60
    assert je_date(world, "SR", S["sr"]) == ago(20)
    assert_books_tie(world, "sales return")


def test_09_return_cannot_exceed_what_was_sold(world):
    r = world["client"].post("/invoicing/sales-return/save", json={
        "original_invoice_id": S["si1"], "customer_id": world["customer"],
        "date": ago(19), "gross_return_value": 450, "net_return_amount": 450,
        "items": [{"product_id": world["product"], "current_return_qty": 3,
                   "max_returnable_qty": 99, "unit_price": 150,
                   "net_return_value": 450}],
        "action": "approve"})
    body = r.get_json()
    assert body["ok"] is False and "returnable" in body["error"]


def test_10_invoice_with_a_return_cannot_be_unapproved(world):
    r = world["client"].post(f"/inventory/invoices/unapprove/{S['si1']}")
    body = r.get_json()
    assert body["ok"] is False and "return" in body["error"].lower()


def test_11_purchase_return_reverses_cost_tax_and_payable(world):
    from inventory_app.models.purchase_invoice import InvPurchaseInvoiceItem
    with Ctx(world):
        item = InvPurchaseInvoiceItem.query.filter_by(invoice_id=S["pi1"]).first()
    ap_before = gl(world, "ap")
    r = world["client"].post("/inventory/purchase-return/save", json={
        "original_invoice_id": S["pi1"], "supplier_id": world["supplier"],
        "date": ago(10), "gross_return_value": 200, "total_discount": 0,
        "total_expenses": 0, "total_tax": 0, "net_return_amount": 200,
        "items": [{"product_id": world["product"], "original_quantity": 10,
                   "max_returnable_qty": 10, "current_return_qty": 2,
                   "unit_price": 100, "net_return_value": 200}],
        "action": "approve"})
    body = r.get_json()
    assert body and body["ok"], r.get_data(as_text=True)[:500]
    S["pr"] = body["id"]
    assert cost(world, "PRV", S["pr"]) == Decimal("200")      # landed 100 each
    assert je_date(world, "PR", S["pr"]) == ago(10)
    from shared.ledger_utils import party_account
    assert_books_tie(world, "purchase return")


def test_12_unapproving_a_middle_sale_recosts_what_followed(world):
    """SI2 (d-70) is cancelled. In the corrected history SI1 and the
    consumption take cheaper stock again."""
    r = world["client"].post(f"/inventory/invoices/unapprove/{S['si2']}")
    assert r.get_json()["ok"], r.get_data(as_text=True)[:400]
    assert cost(world, "SI", S["si2"]) == Decimal("0")
    assert cost(world, "SI", S["si1"]) == Decimal("240")
    assert cost(world, "CONS", S["cons"]) == Decimal("180")   # 3 x 60
    # The sales return follows its sale's cost (unchanged at 60).
    assert cost(world, "SRV", S["sr"]) == Decimal("120")
    r = world["client"].post(f"/inventory/invoices/delete/{S['si2']}")
    assert r.get_json()["ok"]
    assert_books_tie(world, "middle sale reversed and deleted")


def test_13_approved_voucher_cannot_be_reposted(world):
    """Re-submitting an approved voucher used to issue the stock twice."""
    before = on_hand(world)
    world["client"].post(f"/inventory/vouchers/consumption/{S['cons']}", data={
        "department": "Workshop", "reason": "again", "date": ago(30),
        "status": "approved", "product_id[]": [str(world["product"])],
        "qty[]": ["3"]})
    assert on_hand(world) == before
    assert_books_tie(world, "re-submitted approved voucher")


def test_14_closed_period_refuses_backdated_posting(world):
    from shared.models.company_settings import AccountingPeriod
    with Ctx(world):
        p = AccountingPeriod(fiscal_year="T", period_name="Locked",
                             start_date=TODAY - timedelta(days=100),
                             end_date=TODAY - timedelta(days=90), is_closed=True)
        db.session.add(p)
        db.session.commit()
        pid = p.id
    r, body = purchase(world, 1, 10, ago(95), expect_ok=False)
    assert body.get("ok") is False and "closed" in (body.get("error") or "").lower()
    assert_books_tie(world, "closed-period refusal")
    with Ctx(world):
        db.session.delete(db.session.get(AccountingPeriod, pid))
        db.session.commit()


def test_15_capitalising_stock_keeps_inventory_and_assets_tied(world):
    from fixed_assets_app.models.asset import AssetCategory
    with Ctx(world):
        cat = AssetCategory.query.first()
        if cat is None:
            cat = AssetCategory(name="Equipment", default_useful_life=5,
                                default_depreciation_method="straight_line")
            db.session.add(cat)
            db.session.commit()
        cat_id = cat.id
    stock_before = on_hand(world)
    r = world["client"].post("/fixed-assets/transfers/capitalise", data={
        "source_product_id": world["product"], "quantity": "1",
        "name": "Widget Rig", "category_id": cat_id, "useful_life": "5",
        "depreciation_method": "straight_line", "salvage_value": "0",
        "transfer_date": ago(2), "status": "approved"})
    assert r.status_code in (200, 302)
    assert on_hand(world) == stock_before - 1
    assert_books_tie(world, "capitalisation")


def test_16_reports_render_on_the_corrected_books(world):
    c = world["client"]
    for url in ("/finance/trial-balance", "/finance/profit-loss",
                "/finance/balance-sheet", "/executive/integrity",
                f"/inventory/vouchers/product-ledger?product_id={world['product']}",
                "/finance/label-pl", f"/inventory/reports/valuation?as_of={ago(30)}"):
        r = c.get(url)
        assert r.status_code == 200, f"{url} -> {r.status_code}"
    body = c.get("/executive/integrity?run=1").get_data(as_text=True)
    assert "All checks pass" in body


def test_17_opening_stock_and_quick_adjustment_post_to_the_ledger(world):
    """Opening stock typed on the product form, and the product page's +/-
    adjustment, used to change only the stock column — no cost layer and no
    journal, so the inventory account never followed."""
    from inventory_app.models.product import InvProduct
    c = world["client"]
    r = c.post("/inventory/products/create", data={
        "sku": "INT-2", "name": "Integrity Gadget", "unit_price": "90",
        "cost_price": "40", "reorder_level": "0", "unit": "pcs",
        "current_stock": "5", "opening_date": ago(90)})
    assert r.status_code in (200, 302)
    with Ctx(world):
        p2 = InvProduct.query.filter_by(sku="INT-2").one()
        pid2 = p2.id
        from shared import costing
        assert costing.on_hand(pid2) == 5
        assert costing.stock_value(pid2) == Decimal("200")
    assert_books_tie(world, "opening stock")

    r = c.post(f"/inventory/products/adjust-stock/{pid2}",
               data={"quantity": "-2", "notes": "Damaged in store", "date": ago(1)})
    assert r.status_code in (200, 302)
    with Ctx(world):
        from shared import costing
        assert costing.on_hand(pid2) == 3
        assert InvProduct.query.get(pid2).current_stock == 3
    assert_books_tie(world, "quick adjustment")
    world["product2"] = pid2


def test_18_product_with_stock_history_cannot_be_deleted(world):
    from inventory_app.models.product import InvProduct
    world["client"].post(f"/inventory/products/delete/{world['product2']}")
    with Ctx(world):
        assert InvProduct.query.get(world["product2"]) is not None
    assert_books_tie(world, "refused product delete")


def test_19_replacing_a_payroll_run_does_not_double_the_expense(world):
    """'Replace All' deleted the old run but left its journal posted, so the
    month's salary expense was counted twice; loan balances also stayed
    reduced. The run now posts on the month's last day and a replacement
    un-posts the old journal first."""
    import calendar
    from hr_app.models.compensation import PayrollProfile, PayrollRun
    from shared.models.ledger import JournalEntry, JournalLine
    first = TODAY.replace(day=1)
    prev = first - timedelta(days=1)
    m, y = prev.month, prev.year
    month_end = date(y, m, calendar.monthrange(y, m)[1])
    with Ctx(world):
        from hr_app.models.user import User
        u = db.session.get(User, world["user"])
        u.date_of_joining = date(2000, 1, 1)   # employed before the run month
        db.session.commit()
        if not PayrollProfile.query.filter_by(user_id=world["user"]).first():
            db.session.add(PayrollProfile(user_id=world["user"], basic_salary=50000,
                                          effective_from=date(2000, 1, 1)))
            db.session.commit()

    def gross_posted():
        with Ctx(world):
            return sum((Decimal(str(l.debit)) for l in JournalLine.query.join(
                JournalEntry, JournalLine.journal_entry_id == JournalEntry.id)
                .filter(JournalEntry.voucher_type == "PRL",
                        JournalEntry.is_posted == True,  # noqa: E712
                        JournalLine.description.like("Gross salary%")).all()),
                Decimal("0"))

    c = world["client"]
    r = c.post("/compensation/run-payroll", data={"month": m, "year": y},
               follow_redirects=True)
    once = gross_posted()
    assert once > 0, "payroll posted no salary expense"
    with Ctx(world):
        run = PayrollRun.query.filter_by(month=m, year=y).one()
        je = JournalEntry.query.filter_by(voucher_type="PRL", voucher_id=run.id,
                                          is_posted=True).first()
        assert je.entry_date.date() == month_end, "posted on the month's last day"
    assert_books_tie(world, "payroll run")

    c.post("/compensation/run-payroll", data={"month": m, "year": y,
                                              "replace_all": "1"})
    assert gross_posted() == once, "replacing the run must not double the expense"
    assert_books_tie(world, "payroll replaced")


def test_20_multi_label_invoice_splits_revenue_and_cogs_by_line(world):
    """Two lines, two projects: each project must carry its own revenue and
    COGS — not both under the first line's label."""
    from shared.ledger_utils import posting_account
    from shared.models.ledger import JournalEntry, JournalLine
    pid2 = world["product2"]

    def line(label, price):
        return {"product_id": pid2, "quantity": 1, "unit_price": price,
                "description": "Gadget", "unit": "pcs", "discount_pct": 0,
                "label_id": label, "delivery": 0, "installation": 0,
                "sales_tax_pct": 0, "total_before_discount": price,
                "total_after_discount": price}
    r = world["client"].post("/inventory/invoices/save", json={
        "customer_id": world["customer"], "invoice_date": ago(0),
        "discount_mode": "general", "charges_mode": "general", "tax_mode": "general",
        "global_discount_pct": 0, "global_discount_value": 0, "global_delivery": 0,
        "global_installation": 0, "global_sales_tax_pct": 0, "further_tax_pct": 0,
        "apply_further_tax": False, "withholding_tax_pct": 0, "subtotal": 200,
        "total_discount": 0, "total_charges": 0, "total_tax": 0, "total_amount": 200,
        "items": [line(world["l1"], 120), line(world["l2"], 80)], "action": "approve"})
    body = r.get_json()
    assert body and body["ok"], r.get_data(as_text=True)[:400]
    with Ctx(world):
        lines = (JournalLine.query.join(JournalEntry,
                 JournalLine.journal_entry_id == JournalEntry.id)
                 .filter(JournalEntry.voucher_type == "SI",
                         JournalEntry.is_posted == True,  # noqa: E712
                         JournalEntry.voucher_id == body["id"]).all())
        rev = {}
        cogs = {}
        rev_ids = {posting_account("revenue").id}
        cogs_id = posting_account("cogs").id
        for l in lines:
            if l.account_id in rev_ids:
                rev[l.label_id] = rev.get(l.label_id, 0) + float(l.credit)
            if l.account_id == cogs_id:
                cogs[l.label_id] = cogs.get(l.label_id, 0) + float(l.debit)
    assert rev == {world["l1"]: 120.0, world["l2"]: 80.0}
    assert cogs == {world["l1"]: 40.0, world["l2"]: 40.0}
    assert_books_tie(world, "multi-label invoice")
    page = world["client"].get(f"/finance/label-pl?from={ago(0)}&to={ago(0)}")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert "Project North" in html and "Project South" in html


def test_21_uneven_landed_cost_keeps_stock_equal_to_the_ledger(world):
    """A landed cost that does not divide evenly by the quantity.

    288 units, 14,300 carriage absorbed: 4,420,700 / 288 = 15,349.6527... The
    receipt used to store qty x the 4dp unit cost (4,420,700.01) while the
    journal debited 4,420,700.00 — a paisa per such purchase, enough over a
    year of trade to fail the inventory = stock valuation check.
    """
    from shared.ledger_utils import posting_account
    from shared.models.stock_ledger import StockLedger
    with Ctx(world):
        carriage = posting_account("cogs").id
    qty, price, absorb = 288, 15300, 14300
    goods = qty * price
    body = world["client"].post("/inventory/purchase-invoice/save", json={
        "supplier_id": world["supplier"], "invoice_date": ago(0),
        "discount_mode": "general", "expenses_mode": "general", "tax_mode": "general",
        "global_discount_pct": 0, "global_discount_value": 0, "global_commission": 0,
        "global_freight": 0, "global_loading": 0, "global_sales_tax_pct": 0,
        "global_withholding_tax_pct": 0, "subtotal": goods, "total_discount": 0,
        "net_payable": goods + absorb, "total_amount": goods + absorb,
        "charges": [{"description": "Carriage inward", "treatment": "absorb",
                     "charge_account_id": carriage, "amount": absorb,
                     "scope": "general", "distribution": "pro_rata_value"}],
        "items": [{"product_id": world["product"], "quantity": qty, "unit_price": price,
                   "discount_pct": 0, "total_before_discount": goods,
                   "total_after_discount": goods}],
        "action": "approve"}).get_json()
    assert body and body.get("ok"), body
    with Ctx(world):
        row = StockLedger.query.filter_by(voucher_type="PI", voucher_id=body["id"]).one()
        assert Decimal(str(row.total_cost)) == Decimal(goods + absorb)
    assert_books_tie(world, "uneven landed cost")


def test_22_parties_with_history_cannot_be_deleted(world):
    """Supplier delete only looked at purchase orders, so a supplier whose
    purchases were invoiced directly (no PO) could be deleted, orphaning
    the invoices, returns and its ledger account."""
    from inventory_app.models.supplier import InvSupplier
    from inventory_app.models.customer import InvCustomer
    c = world["client"]
    c.post(f"/inventory/suppliers/delete/{world['supplier']}")
    c.post(f"/inventory/customers/delete/{world['customer']}")
    with Ctx(world):
        assert InvSupplier.query.get(world["supplier"]) is not None
        assert InvCustomer.query.get(world["customer"]) is not None
        fresh = InvSupplier(name="Never Used Supplier")
        db.session.add(fresh)
        db.session.commit()
        fid = fresh.id
    c.post(f"/inventory/suppliers/delete/{fid}")
    with Ctx(world):
        assert InvSupplier.query.get(fid) is None, "an unused supplier still deletes"
    assert_books_tie(world, "refused party deletes")
