"""Sales / purchase order actions can no longer move stock outside the ledger.

"Deliver" on a sales order took quantity off the product's on-hand figure
(no stock-ledger row, no cost of sales, no journal) and created an empty
invoice shell; "Receive" on a purchase order added quantity with no cost
layer. Billing the order afterwards moved the stock a second time. Both now
send the user to bill the order, and Cancel is a POST.
"""
import os
import tempfile
from datetime import date

import pytest

_TMP_DB = os.path.join(tempfile.gettempdir(), "erp_order_actions.db")
if os.path.exists(_TMP_DB):
    os.remove(_TMP_DB)
os.environ["DATABASE_URL"] = (os.environ.get("TEST_DATABASE_URL")
                              or "sqlite:///" + _TMP_DB.replace("\\", "/"))

from app import app as flask_app  # noqa: E402
from shared.extensions import db  # noqa: E402


@pytest.fixture(scope="module")
def world():
    c = flask_app.test_client()
    c.get("/")
    from hr_app.models.user import User
    from shared.models.company import Company, CompanyMembership
    from shared.models.base import Role
    from shared.tenancy import set_current_company, unscoped
    from shared.company_setup import provision_company
    from shared.stock_entry import post_opening_stock
    from inventory_app.models.product import InvProduct
    from inventory_app.models.customer import InvCustomer
    from inventory_app.models.supplier import InvSupplier
    from inventory_app.models.sales_order import InvSalesOrder, InvSalesOrderItem
    from inventory_app.models.purchase_order import InvPurchaseOrder, InvPurchaseOrderItem

    with flask_app.app_context():
        user = (User.query.filter_by(is_super_admin=True).order_by(User.id).first()
                or User.query.order_by(User.id).first())
        uid = user.id
        with unscoped():
            comp = Company(name="Orders Co", slug="orders-co-e2e", is_active=True, created_by=uid)
            db.session.add(comp)
            db.session.flush()
            db.session.add(CompanyMembership(
                company_id=comp.id, user_id=uid,
                role_id=Role.query.filter_by(name=Role.ADMIN).first().id,
                status=CompanyMembership.ACTIVE))
            db.session.commit()
            cid = comp.id
        set_current_company(cid)
        provision_company(cid)
        set_current_company(cid)
        p = InvProduct(name="Inverter 5kW", sku="INV5", current_stock=0, cost_price=0,
                       unit_price=0, is_active=True)
        cust = InvCustomer(name="Order Customer")
        sup = InvSupplier(name="Order Supplier")
        db.session.add_all([p, cust, sup])
        db.session.flush()
        post_opening_stock(p, 10, 50_000, when=date(2025, 1, 1), created_by=uid)
        so = InvSalesOrder(so_number="SO-T1", customer_id=cust.id, status="approved",
                           total_amount=300_000)
        po = InvPurchaseOrder(po_number="PO-T1", supplier_id=sup.id, status="approved",
                              total_amount=250_000)
        db.session.add_all([so, po])
        db.session.flush()
        db.session.add(InvSalesOrderItem(so_id=so.id, product_id=p.id, quantity=3,
                                         unit_price=100_000))
        db.session.add(InvPurchaseOrderItem(po_id=po.id, product_id=p.id, quantity=5,
                                            unit_price=50_000))
        db.session.commit()
        ids = {"company": cid, "product": p.id, "so": so.id, "po": po.id, "customer": cust.id}
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


def stock(w):
    from inventory_app.models.product import InvProduct
    from shared.models.stock_ledger import StockLedger
    with Ctx(w):
        p = db.session.get(InvProduct, w["product"])
        return p.current_stock, StockLedger.query.filter_by(product_id=p.id).count()


def books_tie(w):
    from shared import integrity
    with Ctx(w):
        assert not integrity.failures()


def test_deliver_and_receive_do_not_touch_stock(world):
    before = stock(world)
    r = world["client"].get(f"/inventory/sales/deliver/{world['so']}")
    assert r.status_code == 302 and "/inventory/invoices" in r.headers["Location"]
    r = world["client"].get(f"/inventory/purchases/receive/{world['po']}")
    assert r.status_code == 302 and "/inventory/purchase-invoice" in r.headers["Location"]
    assert stock(world) == before == (10, 1)
    from inventory_app.models.invoice import InvInvoice
    with Ctx(world):
        assert InvInvoice.query.count() == 0, "an empty invoice shell was created"
    books_tie(world)


def test_cancel_is_post_only_and_refuses_closed_orders(world):
    from inventory_app.models.sales_order import InvSalesOrder
    c = world["client"]
    assert c.get(f"/inventory/sales/cancel/{world['so']}").status_code == 405
    assert c.get(f"/inventory/purchases/cancel/{world['po']}").status_code == 405
    c.post(f"/inventory/sales/cancel/{world['so']}")
    with Ctx(world):
        assert db.session.get(InvSalesOrder, world["so"]).status == "cancelled"
    r = c.post(f"/inventory/sales/cancel/{world['so']}", follow_redirects=True)
    assert "cannot be cancelled" in r.get_data(as_text=True)


def test_order_lists_offer_bill_not_deliver(world):
    page = world["client"].get("/inventory/purchases/list").get_data(as_text=True)
    assert ">Receive<" not in page and ">Bill<" in page


@pytest.mark.parametrize("url", [
    "/inventory/categories/delete/1", "/inventory/products/delete/1",
    "/inventory/vouchers/consumption/1/delete", "/inventory/vouchers/consumption/1/unapprove",
    "/inventory/vouchers/scrap/1/delete", "/inventory/vouchers/scrap/1/unapprove",
    "/inventory/vouchers/adjustment/1/delete", "/inventory/vouchers/adjustment/1/unapprove",
    "/inventory/vouchers/stock-take/1/delete", "/settings/templates/delete/1",
])
def test_state_changes_refuse_get(world, url):
    """A followed link (or a link preview fetching it) must not delete a record
    or un-post a voucher's journals."""
    assert world["client"].get(url).status_code == 405


def test_fractional_stock_is_kept_on_the_product(world):
    """current_stock was INTEGER: 2.5 kg on hand showed as 2 while the stock
    ledger held 2.5 (and Books Integrity compared the truncated figures)."""
    from inventory_app.models.product import InvProduct
    from shared.stock_entry import post_opening_stock
    with Ctx(world):
        cable = InvProduct(name="Copper cable (m)", sku="CBL", unit="m", current_stock=0,
                           cost_price=0, unit_price=0, is_active=True)
        db.session.add(cable)
        db.session.flush()
        post_opening_stock(cable, 12.75, 400, when=date(2025, 1, 1), created_by=1)
        db.session.commit()
        cid = cable.id
    with Ctx(world):
        assert db.session.get(InvProduct, cid).current_stock == 12.75
        whole = db.session.get(InvProduct, world["product"]).current_stock
        assert whole == 10 and isinstance(whole, int)        # lists still read "10"
    page = world["client"].get("/inventory/products/").get_data(as_text=True)
    assert "12.75" in page
    books_tie(world)


def _sale(w, action, price=80_000, qty=1, **row):
    item = {"product_id": w["product"], "quantity": qty, "unit_price": price,
            "description": "Order actions test", "unit": "pcs", "discount_pct": 0,
            "delivery": 0, "installation": 0, "sales_tax_pct": 0,
            "total_before_discount": qty * price, "total_after_discount": qty * price}
    item.update(row)
    return w["client"].post("/inventory/invoices/save", json={
        "customer_id": w["customer"], "invoice_date": "2025-03-01",
        "discount_mode": "general", "charges_mode": "general", "tax_mode": "general",
        "global_discount_pct": 0, "global_discount_value": 0, "global_delivery": 0,
        "global_installation": 0, "global_sales_tax_pct": 0, "further_tax_pct": 0,
        "apply_further_tax": False, "withholding_tax_pct": 0, "subtotal": qty * price,
        "total_discount": 0, "total_charges": 0, "total_tax": 0, "total_amount": qty * price,
        "items": [item], "action": action}).get_json()


def test_invoice_with_impossible_figures_is_not_approved(world):
    body = _sale(world, "approve", price=-500)
    assert not body["ok"] and "unit price" in body["error"]
    body = _sale(world, "approve", discount_pct=120)
    assert not body["ok"] and "discount" in body["error"]


def test_invoice_reported_to_fbr_cannot_be_unapproved(world):
    """The correction for a reported invoice is a credit note, not an edit."""
    from shared.models.fbr import FBRInvoiceRecord
    body = _sale(world, "approve")
    assert body["ok"], body
    with Ctx(world):
        db.session.add(FBRInvoiceRecord(inv_invoice_id=body["id"], status="success",
                                        fbr_invoice_number="FBR-0001"))
        db.session.commit()
    r = world["client"].post(f"/inventory/invoices/unapprove/{body['id']}").get_json()
    assert not r["ok"] and "reported to FBR as FBR-0001" in r["error"]
    books_tie(world)


def test_opening_stock_without_a_journal_is_found_and_repaired(world):
    """The boot routine for pre-costing-engine stock wrote OPENING layers with
    no journal; Books Integrity names them and the repair posts them."""
    from inventory_app.models.product import InvProduct
    from shared import costing, integrity
    from shared.stock_entry import unjournaled_openings, journal_opening_row
    with Ctx(world):
        legacy = InvProduct(name="Legacy panel", sku="LEG", current_stock=0, cost_price=0,
                            unit_price=0, is_active=True)
        db.session.add(legacy)
        db.session.flush()
        costing.record_in(legacy.id, "OPENING", legacy.id, f"OPEN-{legacy.id:05d}",
                          qty=4, unit_cost=25_000, notes="legacy", created_by=1)
        db.session.commit()
        rows = unjournaled_openings()
        assert [r.product_id for r in rows] == [legacy.id]
        check = integrity.inventory_vs_gl()
        assert not check["ok"] and "repair_opening_stock" in check["detail"]
        journal_opening_row(rows[0])
        db.session.commit()
        assert unjournaled_openings() == []
    books_tie(world)
