"""Order -> invoice linkage: balances, write-back, and over-invoicing control.

Sales_Purchase_Orders_Invoices_ERP_UI_Standard_v3 §4.2 (the picker caps each
line at its uninvoiced balance), §4.3 (what loading copies) and §4.4 (posting
writes back; deleting or crediting restores).

Sales and purchase sides are symmetrical, so both go through here — the only
difference is which model pair is involved.
"""

from inventory_app.extensions import db
from shared.tenancy import scoped_get


class OverInvoiceError(Exception):
    """A line would be billed past its order balance beyond the tolerance."""


def _models(side):
    if side == "sales":
        from inventory_app.models.sales_order import InvSalesOrder, InvSalesOrderItem
        return InvSalesOrder, InvSalesOrderItem
    from inventory_app.models.purchase_order import InvPurchaseOrder, InvPurchaseOrderItem
    return InvPurchaseOrder, InvPurchaseOrderItem


def _tolerance_pct():
    """Admin over-invoicing tolerance (§11.2). Absent settings mean no slack."""
    try:
        from shared.models.invoice_settings import InvoiceSettings
        return float(getattr(InvoiceSettings.get(), "over_invoice_tolerance_pct", 0) or 0)
    except Exception:
        return 0.0


def line_balance(order_item):
    """Uninvoiced quantity on an order line — never negative."""
    ordered = float(order_item.quantity or 0)
    invoiced = float(order_item.invoiced_qty or 0)
    return max(0.0, round(ordered - invoiced, 6))


def order_progress(order):
    """(ordered_value, uninvoiced_value, status) for the picker.

    Value is measured on the line's own net, so a partially invoiced order
    reports what is still worth billing rather than its original total.
    """
    ordered_value = uninvoiced_value = 0.0
    all_done = True
    any_invoiced = False
    for it in order.items:
        qty = float(it.quantity or 0)
        inv_qty = float(it.invoiced_qty or 0)
        # Unit net after the line's own discount, so the figure matches what an
        # invoice for the remaining quantity would actually come to.
        net = float(it.total_after_discount or 0) or (qty * float(it.unit_price or 0))
        unit_net = (net / qty) if qty else 0.0
        ordered_value += net
        uninvoiced_value += unit_net * line_balance(it)
        if inv_qty > 0:
            any_invoiced = True
        if line_balance(it) > 1e-6:
            all_done = False
    if all_done:
        status = "invoiced"
    elif any_invoiced:
        status = "partial"
    else:
        status = "open"
    return round(ordered_value, 2), round(uninvoiced_value, 2), status


def refresh_order_status(order):
    """Recompute and store the order's invoicing progress (§4.4)."""
    _, _, status = order_progress(order)
    order.fulfilment_status = status
    return status


def picker_payload(side, order):
    """One order as the create-from-invoice picker needs it (§4.2, Figure 2)."""
    ordered_value, uninvoiced_value, status = order_progress(order)
    number = getattr(order, "so_number", None) or getattr(order, "po_number", "")
    lines = []
    for it in order.items:
        product = getattr(it, "product", None)
        lines.append({
            "order_item_id": it.id,
            "product_id": it.product_id,
            "product_name": (it.description or (product.name if product else "")),
            "product_sku": product.sku if product else "",
            "unit": it.unit or "pcs",
            "ordered_qty": float(it.quantity or 0),
            "invoiced_qty": float(it.invoiced_qty or 0),
            "balance_qty": line_balance(it),
            "unit_price": float(it.unit_price or 0),
            # §4.3: the line's tax code copies with it. Discount does not —
            # an order carries none, so there is nothing to bring over and the
            # invoice sets its own.
            "sales_tax_pct": float(it.sales_tax_pct or 0),
        })
    return {
        "id": order.id,
        "number": number,
        "so_number": number,          # legacy key the older picker JS reads
        "po_number": number,
        "order_date": order.order_date.strftime("%Y-%m-%d") if order.order_date else "",
        "order_value": ordered_value,
        "uninvoiced_value": uninvoiced_value,
        "total_amount": float(order.total_amount or 0),
        "status": status,
        # §4.2: fully-invoiced orders are listed but greyed and unselectable.
        "selectable": status != "invoiced",
        # §4.3: the order's tax settings travel with the loaded share. Discount,
        # charges, withholding and further tax do not — an order carries none of
        # them, so the invoice decides them itself. The keys are still emitted
        # (zeroed) because the picker JS reads them when seeding a new invoice.
        "pools": {
            "tax_mode": order.tax_mode or "general",
            "global_sales_tax_pct": float(order.global_sales_tax_pct or 0),
            "discount_mode": "general",
            "global_discount_pct": 0.0,
            "global_discount_value": 0.0,
            "further_tax_pct": 0.0,
            "apply_further_tax": False,
            "withholding_tax_pct": 0.0,
            "apply_withholding_tax": False,
        },
        "items": lines,
    }


def check_over_invoicing(side, invoice_items):
    """Refuse lines that would bill past the order balance (§4.4).

    ``invoice_items`` are the invoice's own line rows, each carrying
    ``source_order_item_id`` and ``quantity``. Quantities already written back
    by *this* invoice are excluded, so re-approving an edited invoice compares
    against the right baseline. Returns a list of human-readable errors.
    """
    _, OrderItem = _models(side)
    wanted = {}
    for it in invoice_items:
        oid = getattr(it, "source_order_item_id", None)
        if oid:
            wanted[oid] = wanted.get(oid, 0.0) + float(it.quantity or 0)
    errors = []
    # This runs mid-save, while a half-built invoice is pending in the session.
    # Autoflush would push that incomplete row to the database and fail on its
    # NOT NULL columns, so every read here — the settings lookup included — is
    # deliberately kept out of it.
    with db.session.no_autoflush:
        tol = _tolerance_pct()
        for oid, qty in wanted.items():
            oi = scoped_get(OrderItem, oid)
            if oi is None:
                continue
            balance = line_balance(oi)
            allowed = balance * (1 + tol / 100.0)
            if qty - allowed > 1e-6:
                name = getattr(oi, "description", "") or f"item #{oi.product_id}"
                errors.append(
                    f"{name}: billing {qty:g} exceeds the uninvoiced balance "
                    f"{balance:g}"
                    + (f" (tolerance {tol:g}%)" if tol else "")
                )
    return errors


def apply_writeback(side, invoice_items, sign=1):
    """Move invoiced quantities on the source orders and restage their status.

    ``sign=1`` on approve; ``sign=-1`` to release on unapprove, delete, or a
    credit note (§4.4 — "deleting a draft invoice, or posting a credit note
    against a posted one, restores the balances and reopens the orders").
    """
    Order, OrderItem = _models(side)
    touched = set()
    for it in invoice_items:
        oid = getattr(it, "source_order_item_id", None)
        if not oid:
            continue
        oi = scoped_get(OrderItem, oid)
        if oi is None:
            continue
        current = float(oi.invoiced_qty or 0)
        oi.invoiced_qty = max(0.0, round(current + sign * float(it.quantity or 0), 6))
        touched.add(getattr(oi, "so_id", None) or getattr(oi, "po_id", None))
    for order_id in touched:
        if not order_id:
            continue
        order = scoped_get(Order, order_id)
        if order is not None:
            refresh_order_status(order)
    db.session.flush()
    return touched


class _Release:
    """A minimal stand-in for an invoice item, for apply_writeback's benefit.

    A credit note releases a quantity against an *order line*, but its own rows
    do not carry that link, so the allocation below synthesises the two
    attributes apply_writeback reads.
    """

    __slots__ = ("source_order_item_id", "quantity")

    def __init__(self, order_item_id, quantity):
        self.source_order_item_id = order_item_id
        self.quantity = quantity


def tally_returned_quantities(return_items):
    """Total returned quantity per product across a credit note's rows.

    One product can appear on several rows, and the allocation below works per
    product, so the rows are summed first.
    """
    totals = {}
    for item in return_items:
        if not item.product_id:
            continue
        totals[item.product_id] = (totals.get(item.product_id, 0.0)
                                   + float(item.current_return_qty or 0))
    return totals


def allocate_return_to_order_lines(original_invoice_items, returned_by_product):
    """Spread returned quantities back over the invoice lines that billed them.

    A credit-note row names a product, not an invoice line, so a product billed
    on two lines of one invoice needs the returned quantity apportioned. Lines
    are filled in invoice order, each capped at what it actually billed, which
    keeps the total released equal to the total returned. Lines not drawn from
    an order are skipped — they never consumed a balance.

    Returns a list of ``_Release`` shims for :func:`apply_writeback`.
    """
    remaining = {pid: float(qty) for pid, qty in returned_by_product.items()
                 if float(qty or 0) > 0}
    releases = []
    for it in original_invoice_items:
        order_item_id = getattr(it, "source_order_item_id", None)
        if not order_item_id:
            continue
        left = remaining.get(it.product_id, 0.0)
        if left <= 0:
            continue
        take = min(left, float(it.quantity or 0))
        if take <= 0:
            continue
        remaining[it.product_id] = round(left - take, 6)
        releases.append(_Release(order_item_id, take))
    return releases


def apply_return_writeback(side, original_invoice_items, returned_by_product,
                           sign=-1):
    """Restore (or re-consume) order balances for a credit note (§4.4).

    ``sign=-1`` when the credit note is approved: the returned quantity stops
    counting as invoiced, so the order reopens and the quantity can be billed
    again. ``sign=+1`` when that approval is withdrawn — without the symmetric
    move the balance would stay inflated and the line could be over-billed.
    """
    releases = allocate_return_to_order_lines(original_invoice_items,
                                              returned_by_product)
    if not releases:
        return set()
    return apply_writeback(side, releases, sign=sign)
