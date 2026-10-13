"""Stock that enters or leaves outside a purchase/sale — always with its journal.

Two paths used to change a product's stock figure without touching the stock
ledger or the general ledger at all:

    opening stock   typed on the product form (or a bulk import) — set the
                    display column only, so the goods had no cost layer and the
                    inventory account never saw them.
    adjust stock    the product page's +/- quantity box — same problem.

Both now go through the costing engine and post a balanced journal, so stock
quantity, stock value and the inventory account always move together.
"""

from datetime import datetime
from decimal import Decimal

from shared.extensions import db


def post_opening_stock(product, qty, unit_cost, when=None, created_by=1):
    """Bring opening stock on at ``unit_cost``:

        Dr Inventory (product's stock account)
            Cr Opening Balance Equity

    Dated ``when`` (the go-live / opening date) so it sits before every later
    movement; a later-dated purchase or sale is costed on top of it.
    """
    from shared import costing
    from shared.ledger_utils import (post_journal_entry, posting_account,
                                     create_entity_account)
    qty = Decimal(str(qty or 0))
    if qty <= 0:
        return None
    when = when or datetime.utcnow()
    number = f"OPEN-{product.sku or product.id}"[:50]
    row = costing.record_in(product.id, "OPENING", product.id, number,
                            qty=qty, unit_cost=unit_cost or 0,
                            notes="Opening stock", created_by=created_by,
                            txn_date=when)
    value = float(row.total_cost or 0)
    if value:
        stock_acct = create_entity_account("product", product.id,
                                           f"{product.name} ({product.sku})")
        post_journal_entry(
            voucher_type="OPENING", voucher_id=product.id, voucher_number=number,
            description=f"Opening stock — {product.name}",
            entry_date=when, created_by=created_by,
            lines=[
                {"account_id": stock_acct.id, "debit": value, "credit": 0,
                 "description": f"Opening stock {qty.normalize()} x {product.name}"},
                {"account_id": posting_account("suspense_equity").id,
                 "debit": 0, "credit": value,
                 "description": f"Opening balance — {product.name}"},
            ])
    return row


def quick_adjustment(product, qty, reason, when=None, created_by=1):
    """Post a stock adjustment voucher for a single product (+ found, - lost).

    The same document the Stock Adjustment screen creates, approved at once:
    stock moves through the costing engine at its valuation cost and the
    difference goes to Inventory Adjustment — so it can be reviewed, printed
    and unapproved like any other voucher.
    """
    from shared import costing
    from shared.ledger_utils import post_journal_entry, get_or_create_account
    from shared.models.stock_ledger import VoucherNumber
    from shared.models.vouchers import StockAdjustmentVoucher, StockAdjustmentItem
    qty = Decimal(str(qty or 0))
    if not qty:
        return None
    when = when or datetime.utcnow()
    system = costing.on_hand(product.id)
    v = StockAdjustmentVoucher(voucher_number=VoucherNumber.next("ADJ"), date=when,
                               reason=reason or "Quick adjustment from product page",
                               status="approved", created_by=created_by,
                               approved_by=created_by, approved_at=datetime.utcnow())
    db.session.add(v)
    db.session.flush()
    if qty > 0:
        unit = costing.current_unit_cost(product.id)
        row = costing.record_in(product.id, "ADJ", v.id, v.voucher_number, qty=qty,
                                unit_cost=unit, notes=v.reason,
                                created_by=created_by, txn_date=when)
        unit, total = row.unit_cost, row.total_cost
    else:
        unit, total = costing.record_out(product.id, "ADJ", v.id, v.voucher_number,
                                         qty=-qty, notes=v.reason,
                                         created_by=created_by, txn_date=when)
    db.session.add(StockAdjustmentItem(
        voucher_id=v.id, product_id=product.id, product_name=product.name,
        system_qty=system, physical_qty=system + qty, difference=qty,
        unit_cost=unit, total_cost=total))
    value = float(total or 0)
    if value:
        inv = get_or_create_account("1200", "Inventory", "asset").id
        adj = get_or_create_account("5900", "Inventory Adjustment", "expense").id
        dr, cr = (inv, adj) if qty > 0 else (adj, inv)
        post_journal_entry(
            voucher_type="ADJ", voucher_id=v.id, voucher_number=v.voucher_number,
            description=f"Stock Adjustment {v.voucher_number}: {v.reason}",
            entry_date=when, created_by=created_by,
            lines=[{"account_id": dr, "debit": value, "credit": 0,
                    "description": v.reason},
                   {"account_id": cr, "debit": 0, "credit": value,
                    "description": v.reason}])
    return v


def unjournaled_openings():
    """OPENING stock-ledger rows whose product has no posted OPENING journal.

    The boot routine that gave pre-costing-engine stock a cost layer posted
    no journal, assuming the ledger already held that stock — but the old
    product form / import / quick-adjust only changed the display figure, so
    the inventory account fell short of the stock valuation by exactly these.
    """
    from shared.models.ledger import JournalEntry
    from shared.models.stock_ledger import StockLedger
    journaled = {vid for (vid,) in db.session.query(JournalEntry.voucher_id).filter(
        JournalEntry.voucher_type == "OPENING", JournalEntry.is_posted == True)}  # noqa: E712
    return [r for r in StockLedger.query.filter_by(voucher_type="OPENING",
                                                   transaction_type="IN").all()
            if r.product_id not in journaled and float(r.total_cost or 0)]


def journal_opening_row(row, created_by=1):
    """Post Dr the product's stock account / Cr Opening Balance Equity for an
    existing OPENING stock-ledger row, on that row's own date."""
    from inventory_app.models.product import InvProduct
    from shared.ledger_utils import post_journal_entry, posting_account, create_entity_account
    from shared.tenancy import scoped_get
    product = scoped_get(InvProduct, row.product_id)
    if product is None:
        return None
    value = round(float(row.total_cost or 0), 2)
    stock_acct = create_entity_account("product", product.id, f"{product.name} ({product.sku})")
    return post_journal_entry(
        voucher_type="OPENING", voucher_id=product.id,
        voucher_number=(row.voucher_number or f"OPEN-{product.id:05d}")[:50],
        description=f"Opening stock — {product.name}",
        entry_date=row.txn_date or row.created_at, created_by=created_by,
        lines=[{"account_id": stock_acct.id, "debit": value, "credit": 0,
                "description": f"Opening stock {product.name}"},
               {"account_id": posting_account("suspense_equity").id, "debit": 0,
                "credit": value, "description": f"Opening balance — {product.name}"}])
