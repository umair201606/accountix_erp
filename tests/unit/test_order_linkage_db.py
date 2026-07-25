"""Order write-back against a real database (§4.4).

tests/unit/test_order_linkage.py covers the allocation arithmetic as a pure
function. This file covers what only a session can show: that invoiced_qty and
the order's fulfilment_status actually move, and that billing then crediting
round-trips instead of drifting.

Like tests/unit/conftest.py, this builds a minimal app rather than the real
factory — only the tables the linkage touches are registered.
"""

import pytest
from flask import Flask

from shared.extensions import db


@pytest.fixture
def linkage_app():
    application = Flask(__name__)
    application.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    application.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(application)

    # Imported for their side effect: registering tables on the metadata that
    # create_all() reads.
    import shared.models.ledger  # noqa: F401  (chart_of_accounts: FK target)
    import inventory_app.models.product  # noqa: F401
    import inventory_app.models.customer  # noqa: F401
    import inventory_app.models.sales_order  # noqa: F401
    import inventory_app.models.invoice  # noqa: F401

    with application.app_context():
        db.create_all()
        yield application
        db.session.remove()
        db.drop_all()


@pytest.fixture
def billed_order(linkage_app):
    """An order for 10 units, fully billed by one invoice line.

    Returns (order_item, [invoice_item]) — the shapes the write-back takes.
    """
    from inventory_app.models.product import InvProduct
    from inventory_app.models.customer import InvCustomer
    from inventory_app.models.sales_order import InvSalesOrder, InvSalesOrderItem
    from inventory_app.models.invoice import InvInvoice, InvInvoiceItem
    from shared.order_linkage import apply_writeback

    cust = InvCustomer(name="Test Co")
    prod = InvProduct(name="Widget", sku="W-1", current_stock=0, cost_price=0)
    db.session.add_all([cust, prod])
    db.session.flush()

    order = InvSalesOrder(so_number="SO-1", customer_id=cust.id, status="approved")
    db.session.add(order)
    db.session.flush()
    oi = InvSalesOrderItem(so_id=order.id, product_id=prod.id, quantity=10,
                           unit_price=100, total_after_discount=1000)
    db.session.add(oi)
    db.session.flush()

    inv = InvInvoice(invoice_number="SI-1", voucher_number="SIV-1",
                     customer_id=cust.id, voucher_status="approved")
    db.session.add(inv)
    db.session.flush()
    ii = InvInvoiceItem(invoice_id=inv.id, product_id=prod.id, quantity=10,
                        unit_price=100, source_order_id=order.id,
                        source_order_item_id=oi.id)
    db.session.add(ii)
    db.session.flush()

    apply_writeback("sales", [ii], sign=1)
    return oi, [ii]


def test_billing_the_whole_order_closes_it(billed_order):
    oi, _ = billed_order
    assert float(oi.invoiced_qty) == 10.0
    assert oi.sales_order.fulfilment_status == "invoiced"


def test_a_partial_credit_note_reopens_the_order(billed_order):
    """The point of §4.4: crediting 4 of 10 has to give those 4 back to the order
    so they can be billed again, and drop it out of "invoiced"."""
    from shared.order_linkage import apply_return_writeback
    oi, inv_items = billed_order

    apply_return_writeback("sales", inv_items, {oi.product_id: 4}, sign=-1)

    assert float(oi.invoiced_qty) == 6.0
    assert oi.sales_order.fulfilment_status == "partial"


def test_a_full_credit_note_returns_the_order_to_open(billed_order):
    from shared.order_linkage import apply_return_writeback
    oi, inv_items = billed_order

    apply_return_writeback("sales", inv_items, {oi.product_id: 10}, sign=-1)

    assert float(oi.invoiced_qty) == 0.0
    assert oi.sales_order.fulfilment_status == "open"


def test_withdrawing_a_credit_note_re_consumes_the_balance(billed_order):
    """Without the symmetric move the balance stays inflated after an unapprove,
    and the same quantity could be billed a second time."""
    from shared.order_linkage import apply_return_writeback
    oi, inv_items = billed_order

    apply_return_writeback("sales", inv_items, {oi.product_id: 4}, sign=-1)
    apply_return_writeback("sales", inv_items, {oi.product_id: 4}, sign=1)

    assert float(oi.invoiced_qty) == 10.0
    assert oi.sales_order.fulfilment_status == "invoiced"


def test_a_credit_note_cannot_drive_the_balance_below_zero(billed_order):
    from shared.order_linkage import apply_return_writeback
    oi, inv_items = billed_order

    apply_return_writeback("sales", inv_items, {oi.product_id: 10}, sign=-1)
    apply_return_writeback("sales", inv_items, {oi.product_id: 5}, sign=-1)

    assert float(oi.invoiced_qty) == 0.0


def test_crediting_a_line_with_no_order_leaves_the_order_alone(billed_order):
    """A product returned against a hand-typed line must not release a balance
    that line never consumed."""
    from shared.order_linkage import apply_return_writeback
    oi, inv_items = billed_order
    inv_items[0].source_order_item_id = None

    apply_return_writeback("sales", inv_items, {oi.product_id: 4}, sign=-1)

    assert float(oi.invoiced_qty) == 10.0
