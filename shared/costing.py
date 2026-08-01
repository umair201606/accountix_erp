"""Inventory costing engine.

Single source of truth for what a unit of stock COSTS at any point in time.
Every document that moves stock (purchase invoice, sales invoice, purchase
return, consumption, scrap, adjustment, stock take) must go through
``record_in`` / ``record_out`` so the StockLedger holds a complete history:

    IN  rows carry the actual acquisition cost (landed purchase cost).
    OUT rows carry the cost COMPUTED from the cost layers at issue time —
        never a price typed by the user and never the product's static
        cost_price.

That computed cost is what the calling voucher posts to the general ledger
(COGS, scrap loss, an employee's receivable account, ...), so receivables/
payables always reflect true historic cost.

HOW VALUATION WORKS
-------------------
Cost lives in StockLayer rows, not in a replay of the ledger. The two
valuation methods differ in exactly one place — what a RECEIPT does:

    FIFO              each receipt opens a new layer.
    weighted average  each receipt merges into the single open layer,
                      re-averaging its unit cost.

Issuing is identical under both: consume layers oldest-first at each layer's
own cost. Under weighted average there is only ever one open layer, so that
cost IS the running average.

Because issues decrement real layers rather than assuming a consumption
order, this invariant always holds:

    sum(layer.qty_remaining * layer.unit_cost) == ledger running_cost

WHY THAT MATTERS
----------------
Switching valuation method is a REVALUATION, not a re-interpretation of
history (this is what SAP's material ledger and NetSuite's cost
revaluation do):

    WA -> FIFO   the single open layer carries forward at book value;
                 later receipts open new layers.
    FIFO -> WA   remaining layers collapse into one at book value.

Both directions preserve book value exactly, so a method switch can never
change a cost that was already computed and posted. The old engine derived
FIFO layers by replaying OUT quantities against IN rows, which silently
assumed every past issue had consumed oldest-first; switching methods then
invented value (buy 10@10 + 10@20, sell 10 at avg 15, switch to FIFO ->
COGS 350 against purchases of 300). Layers make that unrepresentable.
"""

from decimal import Decimal

from shared.extensions import db
from shared.models.stock_ledger import StockLedger
from shared.models.stock_layer import StockLayer, LayerConsumption
from shared.models.inventory_settings import InventorySettings
from shared.tenancy import scoped_get

ZERO = Decimal("0")


class NegativeStockError(Exception):
    """Raised when an issue would drive stock below zero and the company has
    not enabled ``allow_negative_stock``.

    Costing stock that was never purchased means inventing value: the
    uncovered units have no acquisition cost to draw on, so any figure the
    engine posts to COGS (or an employee's receivable) is a guess that will
    not reconcile against the inventory control account. Refuse instead.
    """


class ConsumedLayerError(Exception):
    """Raised when reversing a receipt whose stock has already been issued.

    The issue drew its cost from this receipt's layer and posted that cost to
    the general ledger, where it is frozen. Withdrawing the receipt now would
    leave that posted cost backed by a purchase that no longer exists: the
    quantity would have to come from a later, differently-priced layer while
    the posted figure stayed put, and the difference — real money — would
    silently land nowhere.

    Reverse the dependent issues first, which returns the quantity to this
    layer and reverses their journal entries, then reverse the receipt. This
    is what SAP does when a goods receipt has downstream consumption.
    """

    def __init__(self, message, dependents=None):
        super().__init__(message)
        self.dependents = dependents or []


def _q(value, places=4):
    return Decimal(str(value or 0)).quantize(Decimal("0." + "0" * places))


def _d(value):
    return Decimal(str(value or 0))


def _settings():
    return InventorySettings.get()


def _open_layers(product_id):
    """Layers with stock left, oldest first — the consumption order."""
    return (StockLayer.query
            .filter(StockLayer.product_id == product_id,
                    StockLayer.qty_remaining > 0)
            .order_by(StockLayer.id.asc())
            .all())


def on_hand(product_id):
    qty, _cost, _avg = StockLedger.get_running_balance(product_id)
    return _d(qty)


def layers_remaining(product_id):
    """Remaining (unit_cost, qty) layers, oldest first."""
    return [(_d(l.unit_cost), _d(l.qty_remaining)) for l in _open_layers(product_id)]


# Kept for callers/tests written against the previous engine.
fifo_layers_remaining = layers_remaining


def stock_value(product_id):
    """Value of stock on hand per the layers."""
    return sum((_d(l.qty_remaining) * _d(l.unit_cost) for l in _open_layers(product_id)), ZERO)


def current_unit_cost(product_id):
    """Unit cost of stock on hand: total layer value / total layer qty."""
    layers = _open_layers(product_id)
    total_qty = sum((_d(l.qty_remaining) for l in layers), ZERO)
    if total_qty <= 0:
        # Nothing on hand — fall back to the most recent known cost so
        # vouchers over empty stock still carry a sensible value.
        last = (StockLayer.query
                .filter_by(product_id=product_id)
                .order_by(StockLayer.id.desc()).first())
        return _d(last.unit_cost) if last else ZERO
    total_value = sum((_d(l.qty_remaining) * _d(l.unit_cost) for l in layers), ZERO)
    return _q(total_value / total_qty)


def _plan_consumption(product_id, qty):
    """(plan, uncovered) for issuing ``qty`` — oldest layers first.

    ``plan`` is [(layer, take_qty, unit_cost)]; ``uncovered`` is what no
    layer could cover. Pure: decrements nothing.
    """
    remaining = qty
    plan = []
    for layer in _open_layers(product_id):
        if remaining <= 0:
            break
        take = min(_d(layer.qty_remaining), remaining)
        if take <= 0:
            continue
        plan.append((layer, take, _d(layer.unit_cost)))
        remaining -= take
    return plan, remaining


def cost_of_issue(product_id, qty):
    """(unit_cost, total_cost) that issuing ``qty`` units would carry NOW.

    Consumes layers oldest-first at each layer's own cost. Under weighted
    average there is a single open layer, so this returns the running
    average; under FIFO it returns the blended cost of the layers the issue
    would actually eat.

    Does not mutate anything — callers that intend to issue use
    ``record_out``, which plans and consumes in one step.
    """
    qty = _d(qty)
    if qty <= 0:
        return ZERO, ZERO
    plan, uncovered = _plan_consumption(product_id, qty)
    total = sum((take * cost for _l, take, cost in plan), ZERO)
    if uncovered > 0:
        # Only reachable when negative stock is allowed; the uncovered units
        # are costed at the last known cost so the ledger never books free
        # stock. record_out refuses this case unless it is enabled.
        total += uncovered * current_unit_cost(product_id)
    return _q(total / qty), _q(total, 2)


def _sync_product_stock(product_id):
    from inventory_app.models.product import InvProduct
    p = scoped_get(InvProduct, product_id)
    if p is not None:
        # current_stock is a legacy denormalised Integer column kept for
        # display; the StockLedger (Numeric 16,4) is authoritative and is what
        # every costing decision reads. Fractional stock therefore shows
        # truncated here — fixing that means widening the column, not lying
        # about the type by writing a float into an Integer on Postgres.
        p.current_stock = int(on_hand(product_id))
        # Keep the legacy static field in step with the engine so any old
        # display code shows the current valuation cost.
        unit = current_unit_cost(product_id)
        if unit > 0:
            p.cost_price = float(unit)


def _write_row(product_id, voucher_type, voucher_id, voucher_number,
               transaction_type, qty, unit_cost, total_cost, notes, created_by):
    prev_qty, prev_cost, _prev_avg = StockLedger.get_running_balance(product_id)
    prev_qty, prev_cost = _d(prev_qty), _d(prev_cost)
    if transaction_type == "IN":
        new_qty = prev_qty + qty
        new_cost = prev_cost + total_cost
    else:
        new_qty = prev_qty - qty
        new_cost = prev_cost - total_cost
    if new_qty == 0:
        new_cost = ZERO
    new_avg = _q(new_cost / new_qty) if new_qty > 0 else ZERO
    row = StockLedger(
        product_id=product_id,
        voucher_type=voucher_type,
        voucher_id=voucher_id,
        voucher_number=voucher_number,
        transaction_type=transaction_type,
        quantity=qty,
        unit_cost=_q(unit_cost),
        total_cost=_q(total_cost, 2),
        running_qty=new_qty,
        running_cost=new_cost,
        running_avg=new_avg,
        valuation_method=_settings().valuation_method,
        notes=notes,
        created_by=created_by,
    )
    db.session.add(row)
    db.session.flush()
    return row


def record_in(product_id, voucher_type, voucher_id, voucher_number,
              qty, unit_cost, notes="", created_by=1):
    """Stock received at an actual acquisition cost (e.g. landed purchase cost).

    FIFO opens a new layer. Weighted average merges into the open layer and
    re-averages it, so exactly one layer stays open and its cost is the
    running average.
    """
    qty = _d(qty)
    unit_cost = _q(unit_cost)
    total_cost = _q(qty * unit_cost, 2)
    row = _write_row(product_id, voucher_type, voucher_id, voucher_number,
                     "IN", qty, unit_cost, total_cost, notes, created_by)

    settings = _settings()
    open_layers = _open_layers(product_id)
    if settings.is_fifo() or not open_layers:
        db.session.add(StockLayer(
            product_id=product_id, source_ledger_id=row.id,
            unit_cost=unit_cost, qty_original=qty, qty_remaining=qty,
            method=settings.valuation_method, notes=notes,
        ))
    else:
        # Weighted average: re-average the single open layer. Any extra open
        # layers (left by a FIFO period before the switch) are folded in too,
        # so the pool collapses back to one.
        total_qty = sum((_d(l.qty_remaining) for l in open_layers), ZERO) + qty
        total_value = sum((_d(l.qty_remaining) * _d(l.unit_cost)
                           for l in open_layers), ZERO) + total_cost
        keep, rest = open_layers[0], open_layers[1:]
        keep.qty_remaining = total_qty
        keep.unit_cost = _q(total_value / total_qty) if total_qty > 0 else ZERO
        keep.qty_original = _d(keep.qty_original) + qty
        for l in rest:
            l.qty_remaining = ZERO
    db.session.flush()
    _sync_product_stock(product_id)
    return row


def record_out(product_id, voucher_type, voucher_id, voucher_number,
               qty, notes="", created_by=1, unit_cost=None):
    """Stock issued; cost computed from the layers unless an explicit cost
    basis is passed (purchase returns use the original invoice cost).

    Returns (unit_cost, total_cost) so the caller can post the same value to
    the general ledger. That value is frozen once posted: nothing in this
    module ever rewrites a row's unit_cost/total_cost.

    Raises NegativeStockError if the issue is not covered by stock on hand
    and ``allow_negative_stock`` is off.
    """
    qty = _d(qty)
    if qty <= 0:
        return ZERO, ZERO

    plan, uncovered = _plan_consumption(product_id, qty)
    if uncovered > 0 and not _settings().allow_negative_stock:
        raise NegativeStockError(
            f"Cannot issue {qty} of product {product_id}: only "
            f"{on_hand(product_id)} on hand. The uncovered {uncovered} "
            f"unit(s) have no acquisition cost, so any value posted for them "
            f"would be invented. Receive the stock first, or enable "
            f"'allow negative stock' in Inventory Settings."
        )

    if unit_cost is None:
        unit, total = cost_of_issue(product_id, qty)
    else:
        unit = _q(unit_cost)
        total = _q(qty * unit, 2)

    row = _write_row(product_id, voucher_type, voucher_id, voucher_number,
                     "OUT", qty, unit, total, notes, created_by)

    # Draw the quantity down against real layers and record what was taken
    # from where, so every posted cost can be traced to its purchases.
    for layer, take, layer_cost in plan:
        layer.qty_remaining = _d(layer.qty_remaining) - take
        # An explicit basis (purchase return) posts its own cost; the
        # consumption row records the basis actually charged.
        charged = unit if unit_cost is not None else layer_cost
        db.session.add(LayerConsumption(
            layer_id=layer.id, out_ledger_id=row.id, product_id=product_id,
            qty=take, unit_cost=charged, total_cost=_q(take * charged, 2),
        ))
    db.session.flush()
    _sync_product_stock(product_id)
    return unit, total


def revalue_for_method_change(new_method, created_by=1):
    """Switch valuation method as a REVALUATION — prospective only.

    Called when the method changes in Inventory Settings. For every product
    holding stock, the open layers are collapsed/carried at their CURRENT
    book value:

        WA -> FIFO   one open layer already; it carries forward untouched
                     and later receipts open new layers.
        FIFO -> WA   remaining layers collapse into one at
                     total_value / total_qty, which IS book value.

    Book value is identical before and after, so no cost that was already
    computed and posted can change. Ledger rows are never touched — a row's
    unit_cost/total_cost records what the method in force at the time
    charged, and stays that way forever.
    """
    products = [r[0] for r in db.session.query(StockLayer.product_id)
                .filter(StockLayer.qty_remaining > 0).distinct().all()]
    for product_id in products:
        layers = _open_layers(product_id)
        if len(layers) <= 1:
            # Already a single pool — nothing to collapse. Book value is
            # whatever that layer holds, and it carries forward as-is.
            continue
        if new_method == "fifo":
            # FIFO keeps distinct layers; the existing ones are already
            # distinct and correctly costed. Nothing to do.
            continue
        total_qty = sum((_d(l.qty_remaining) for l in layers), ZERO)
        total_value = sum((_d(l.qty_remaining) * _d(l.unit_cost) for l in layers), ZERO)
        keep, rest = layers[0], layers[1:]
        keep.qty_remaining = total_qty
        keep.qty_original = total_qty
        keep.unit_cost = _q(total_value / total_qty) if total_qty > 0 else ZERO
        keep.method = new_method
        keep.is_revaluation = True
        keep.notes = (f"Revaluation: collapsed {len(layers)} layers on switch "
                      f"to {new_method} at book value {_q(total_value, 2)}")
        for l in rest:
            l.qty_remaining = ZERO
    db.session.flush()


def assert_invariant(product_id):
    """(ok, layer_value, running_cost) — layers must equal the ledger.

    Any drift means a cost was posted that the layers cannot back, which is
    exactly how an inventory control account silently stops tying to COGS.
    Used by the costing tests.
    """
    _qty, running_cost, _avg = StockLedger.get_running_balance(product_id)
    layer_value = stock_value(product_id)
    return (abs(layer_value - _d(running_cost)) <= Decimal("0.01"),
            layer_value, _d(running_cost))


def rebuild_running(product_id):
    """Recompute the running qty/cost/avg columns by replaying the ledger.

    Only the running_* columns are touched. A row's unit_cost/total_cost is
    what was posted to the general ledger and never changes.
    """
    rows = StockLedger.query.filter_by(product_id=product_id).order_by(StockLedger.id.asc()).all()
    qty = cost = ZERO
    for r in rows:
        rqty = _d(r.quantity)
        rtotal = _d(r.total_cost)
        if r.transaction_type == "IN":
            qty += rqty
            cost += rtotal
        else:
            qty -= rqty
            cost -= rtotal
        if qty == 0:
            cost = ZERO
        r.running_qty = qty
        r.running_cost = cost
        r.running_avg = _q(cost / qty) if qty > 0 else ZERO
    db.session.flush()
    _sync_product_stock(product_id)


def _resync_pool(product_id):
    """Re-point the weighted-average pool at the ledger balance.

    Only meaningful under weighted average, where the pool IS the ledger
    balance: one layer holding running_qty at running_cost/running_qty.

    Needed because a WA receipt merges into the open layer instead of opening
    its own, so reversing that receipt has no layer to withdraw — the ledger
    would drop the value while the layer kept it (reversing a merged 10 @ 20
    left a layer worth 300 against a ledger of 100). FIFO layers are withdrawn
    precisely by source_ledger_id and must not be touched here.
    """
    if _settings().is_fifo():
        return
    layers = _open_layers(product_id)
    if not layers:
        return
    qty, cost, _avg = StockLedger.get_running_balance(product_id)
    qty, cost = _d(qty), _d(cost)
    keep, rest = layers[0], layers[1:]
    for l in rest:
        l.qty_remaining = ZERO
    if qty <= 0:
        keep.qty_remaining = ZERO
    else:
        keep.qty_remaining = qty
        keep.unit_cost = _q(cost / qty)
    db.session.flush()


def original_issue_cost(voucher_type, voucher_id, product_id):
    """Unit cost a product was ISSUED at on a given voucher, or None.

    What a sales return needs: goods coming back from a customer must re-enter
    stock at the cost they left at, not at today's valuation. Selling a unit at
    a cost of 10 and taking it back at a current average of 18 would invent 8 of
    inventory value out of a round trip that changed nothing, and the COGS
    reversal would not match the COGS that was posted.

    Reads the frozen unit_cost off the issue's own ledger row, so it stays
    correct however long ago the sale was and whatever the valuation method has
    done since.
    """
    row = (StockLedger.query
           .filter_by(voucher_type=voucher_type, voucher_id=voucher_id,
                      product_id=product_id, transaction_type="OUT")
           .order_by(StockLedger.id.asc()).first())
    return _d(row.unit_cost) if row is not None else None


def _unaccounted_value(product_id):
    """Value received, less value expensed out, less value still on the shelf.

    Zero when the books tie. Non-zero only after a retroactive change, where it
    is the money a posted cost can no longer account for. Reads the frozen row
    costs directly rather than running_cost, which is clamped to zero whenever
    quantity reaches zero.
    """
    rows = StockLedger.query.filter_by(product_id=product_id).all()
    received = sum((_d(r.total_cost) for r in rows if r.transaction_type == "IN"), ZERO)
    issued = sum((_d(r.total_cost) for r in rows if r.transaction_type == "OUT"), ZERO)
    return received - issued - stock_value(product_id)


def _reconcile_to_variance(product_id, voucher_number, created_by=1):
    """Realign the layers with the ledger after a retroactive change; return
    the value that no longer has a home.

    Withdrawing a receipt whose stock was already issued leaves two
    inconsistencies:

      quantity  the issue drew from a layer that no longer exists, so the
                layers hold MORE units than the ledger says are on hand. The
                surplus is re-drawn from the surviving layers, oldest first —
                a quantity move only. No posted cost is touched.

      value     the issue's cost stays frozen at what it charged (correct: it
                was posted, and conveyed), but the stock that actually left is
                worth what the surviving layers cost. The gap is real money.

    That gap is returned as the variance. A value-only ledger row (quantity 0,
    carrying just the cost) records it against the product so the ledger's
    running_cost lands back on the layers' value, and the caller posts the
    matching journal entry.
    """
    layer_qty = sum((_d(l.qty_remaining) for l in _open_layers(product_id)), ZERO)
    ledger_qty = on_hand(product_id)

    # Quantity: re-draw the surplus from surviving layers, oldest first.
    surplus = layer_qty - ledger_qty
    if surplus > 0:
        for layer in _open_layers(product_id):
            if surplus <= 0:
                break
            take = min(_d(layer.qty_remaining), surplus)
            layer.qty_remaining = _d(layer.qty_remaining) - take
            surplus -= take
        db.session.flush()

    # Value: what was received, less what was expensed out, less what is still
    # on the shelf. Anything left over has no home.
    #
    # Deliberately NOT running_cost - stock_value: _write_row zeroes
    # running_cost whenever quantity hits zero, which is exactly the case a
    # reversal creates, so that reading would report no variance at the moment
    # one certainly exists. This arithmetic survives the clamp. It also stays
    # correct under weighted average, where _resync_pool has already folded any
    # gap into the surviving units' average and there is genuinely nothing left
    # to write off.
    variance = _unaccounted_value(product_id)
    if abs(variance) <= Decimal("0.01"):
        return ZERO

    _write_row(product_id, "VAR", 0, voucher_number,
               "OUT" if variance > 0 else "IN", ZERO, ZERO, abs(variance),
               f"Cost variance on reversal of {voucher_number}: value with no "
               f"remaining purchase to back it", created_by)
    db.session.flush()
    _sync_product_stock(product_id)
    return variance


def consumers_of_voucher(voucher_type, voucher_id):
    """Vouchers that drew cost from this one's layers: [(type, number, qty)].

    Empty means the receipt's stock is untouched and it can be reversed
    cleanly.
    """
    rows = (StockLedger.query
            .filter_by(voucher_type=voucher_type, voucher_id=voucher_id,
                       transaction_type="IN").all())
    if not rows:
        return []
    layer_ids = [l.id for l in StockLayer.query.filter(
        StockLayer.source_ledger_id.in_([r.id for r in rows])).all()]
    if not layer_ids:
        return []
    out = []
    for c in LayerConsumption.query.filter(
            LayerConsumption.layer_id.in_(layer_ids)).all():
        ledger_row = scoped_get(StockLedger, c.out_ledger_id)
        if ledger_row is not None:
            out.append((ledger_row.voucher_type, ledger_row.voucher_number, _d(c.qty)))
    return out


def reverse_voucher_stock(voucher_type, voucher_id, allow_variance=False,
                          created_by=1):
    """Remove a voucher's stock rows and give back what they consumed.

    Layer quantities consumed by a reversed issue are restored, and layers
    opened by a reversed receipt are withdrawn, so the pool matches the
    surviving rows.

    If the voucher received stock that has since been issued, the issue's cost
    was posted from a purchase that is about to disappear:

        allow_variance=False  refuse (ConsumedLayerError). Safe default: the
                              caller reverses the dependent issues first.
        allow_variance=True   proceed, and book the difference to Inventory
                              Cost Variance rather than restating the frozen
                              cost or letting inventory drift from COGS.

    Returns {product_id: variance} for whatever had to be written off.
    """
    rows = StockLedger.query.filter_by(voucher_type=voucher_type, voucher_id=voucher_id).all()
    if not rows:
        return {}
    product_ids = {r.product_id for r in rows}
    row_ids = [r.id for r in rows]
    voucher_number = rows[0].voucher_number

    dependents = consumers_of_voucher(voucher_type, voucher_id)
    if dependents and not allow_variance:
        listed = ", ".join(f"{t} {n} ({q})" for t, n, q in dependents[:5])
        more = f" and {len(dependents) - 5} more" if len(dependents) > 5 else ""
        raise ConsumedLayerError(
            f"Cannot reverse {voucher_type} #{voucher_id}: its stock has "
            f"already been issued by {listed}{more}. Those issues posted a "
            f"cost drawn from this receipt, and that posted cost cannot "
            f"change. Reverse them first, then reverse this receipt.",
            dependents=dependents,
        )

    # Under weighted average a receipt has no layer of its own — it merged into
    # the pool — so the consumption check above cannot see it. Stock is
    # fungible there, but withdrawing more than is still on hand necessarily
    # takes back units that were already issued at a cost now posted and
    # frozen. Refuse on quantity instead.
    for r in rows:
        if r.transaction_type != "IN" or allow_variance:
            continue
        available = on_hand(r.product_id)
        if _d(r.quantity) > available:
            raise ConsumedLayerError(
                f"Cannot reverse {voucher_type} #{voucher_id}: it received "
                f"{_d(r.quantity)} unit(s) of product {r.product_id} but only "
                f"{available} remain on hand, so part of it has already been "
                f"issued at a cost that is now posted and cannot change. "
                f"Reverse the issues that consumed it first."
            )

    # Give back quantity this voucher's issues took out of the layers.
    consumptions = LayerConsumption.query.filter(
        LayerConsumption.out_ledger_id.in_(row_ids)).all()
    for c in consumptions:
        layer = scoped_get(StockLayer, c.layer_id)
        if layer is not None:
            layer.qty_remaining = _d(layer.qty_remaining) + _d(c.qty)
        db.session.delete(c)
    db.session.flush()

    # Withdraw layers this voucher's receipts opened, along with any
    # consumption that drew from them — with allow_variance those exist, and
    # _reconcile_to_variance re-draws their quantity from the surviving layers.
    #
    # FIFO only. Under weighted average the open layer is a SHARED pool that
    # later receipts merged into, and source_ledger_id still names whichever
    # receipt happened to open it — so deleting "this voucher's layer" would
    # throw away every other receipt's value with it (reversing the first of
    # two receipts destroyed the second's 200 as well). There, the pool is left
    # alone and _resync_pool re-points it at the surviving ledger.
    doomed = (StockLayer.query.filter(StockLayer.source_ledger_id.in_(row_ids)).all()
              if _settings().is_fifo() else [])
    if doomed:
        LayerConsumption.query.filter(
            LayerConsumption.layer_id.in_([l.id for l in doomed])
        ).delete(synchronize_session=False)
        db.session.flush()
    for layer in doomed:
        db.session.delete(layer)
    db.session.flush()

    for r in rows:
        db.session.delete(r)
    db.session.flush()

    variances = {}
    for pid in product_ids:
        rebuild_running(pid)
        _resync_pool(pid)
        if allow_variance:
            v = _reconcile_to_variance(pid, voucher_number, created_by)
            if v:
                variances[pid] = v
    return variances


def ensure_opening_balances(created_by=1):
    """Give products that pre-date the costing engine an opening cost layer.

    Any product holding stock with no ledger history gets one IN row at its
    static cost_price, so all future issues have a historic cost to draw on.
    No journal entry is posted — the general ledger already carried these
    balances under the old flows.
    """
    from inventory_app.models.product import InvProduct
    for p in InvProduct.query.filter(InvProduct.current_stock > 0).all():
        exists = StockLedger.query.filter_by(product_id=p.id).first()
        if exists:
            continue
        record_in(p.id, "OPENING", p.id, f"OPEN-{p.id:05d}",
                  qty=p.current_stock, unit_cost=p.cost_price or 0,
                  notes="Opening balance (pre-costing-engine stock)",
                  created_by=created_by)


def backfill_layers(created_by=1):
    """Give products with ledger history but no layers an opening layer.

    Bridges stock that the replay-based engine tracked before layers
    existed: one layer per product at current book value, so the invariant
    (layer value == running_cost) holds from here on. Idempotent.
    """
    product_ids = [r[0] for r in db.session.query(StockLedger.product_id).distinct().all()]
    for pid in product_ids:
        if StockLayer.query.filter_by(product_id=pid).first():
            continue
        qty, cost, _avg = StockLedger.get_running_balance(pid)
        qty, cost = _d(qty), _d(cost)
        if qty <= 0:
            continue
        db.session.add(StockLayer(
            product_id=pid, source_ledger_id=None,
            unit_cost=_q(cost / qty), qty_original=qty, qty_remaining=qty,
            method=_settings().valuation_method, is_revaluation=True,
            notes="Opening layer at book value (pre-layer-engine stock)",
        ))
    db.session.flush()
