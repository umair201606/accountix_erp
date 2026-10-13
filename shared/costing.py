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

    sum(layer.value_remaining) == ledger running_cost

Layer value is CARRIED, not recomputed from qty x unit_cost: every movement
of it is one of the same 2dp amounts the ledger moves in, so the two sides
cannot drift apart by rounding. StockLayer's docstring has the arithmetic.

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

TIME: DOCUMENT DATE, NOT KEYING ORDER
------------------------------------
Every ledger row carries ``txn_date`` -- the date of the document that moved
the stock. Layers are consumed in (txn_date, id) order. A document keyed in
order (the normal case) is costed on the fast path exactly as above. Two
things break the order, and both are handled by replaying the product's
history in date order (``_replay``):

    back-dating   a purchase dated last month, keyed after this month's sales.
                  In date order those sales could have drawn on it, so they
                  are re-costed as if it had been entered on time.
    reversal      unapproving an earlier document (to correct it, or because
                  it never happened). Later issues are re-costed from what is
                  left; re-approving the corrected document re-costs them
                  again around it.

Rows before the change are frozen -- nothing earlier can be affected by it.
Rows after it are re-costed. Every issue whose cost moves gets a
``StockCostAdjustment`` and a journal (shared/cost_adjustment.py) charging the
difference to the account the issue originally hit, so the inventory control
account keeps tying to the stock valuation and P&L carries the true cost.
A back-dated issue is refused if stock was not on hand AT ITS DATE, or if it
would leave a later issue uncovered.
"""

from datetime import date, datetime
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


# OUT rows whose cost is a fixed basis rather than drawn from the layers: a
# purchase return leaves at the cost it was bought at.
EXPLICIT_BASIS_TYPES = {"PRV"}


def as_txn_date(when):
    """Normalise a document date (date, datetime, 'YYYY-MM-DD', None=today)."""
    if when is None or when == "":
        return date.today()
    if isinstance(when, datetime):
        return when.date()
    if isinstance(when, date):
        return when
    return datetime.strptime(str(when)[:10], "%Y-%m-%d").date()


def _key(row):
    return (row.txn_date or date.min, row.id)


def _ordered_rows(product_id):
    return (StockLedger.query.filter_by(product_id=product_id)
            .order_by(StockLedger.txn_date.asc(), StockLedger.id.asc()).all())


def _is_backdated(product_id, txn_date):
    """True when the product already has movement dated AFTER ``txn_date``."""
    return (StockLedger.query
            .filter(StockLedger.product_id == product_id,
                    StockLedger.txn_date > txn_date)
            .first()) is not None


def on_hand_at(product_id, when):
    """Quantity on hand at the end of ``when`` (document-date view)."""
    d = as_txn_date(when)
    total = ZERO
    for r in (StockLedger.query
              .filter(StockLedger.product_id == product_id,
                      StockLedger.txn_date <= d).all()):
        q = _d(r.quantity)
        total += q if r.transaction_type == "IN" else -q
    return total


def value_at(product_id, when):
    """Stock value at the end of ``when``: posted IN cost less posted OUT cost
    of every movement dated on or before it."""
    d = as_txn_date(when)
    total = ZERO
    for r in (StockLedger.query
              .filter(StockLedger.product_id == product_id,
                      StockLedger.txn_date <= d).all()):
        c = _d(r.total_cost)
        total += c if r.transaction_type == "IN" else -c
    return total


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
    """Value of stock on hand per the layers.

    The carried figure, not qty x unit_cost: the product of a 4dp cost and a
    quantity is only ever approximately what was posted, and that is the
    approximation the invariant used to be checked against.

    Counts every layer still carrying a balance — including short layers
    (negative ``qty_remaining``) left by issues beyond stock on hand. Those
    are value the ledger moved that no positive layer holds, so excluding
    them would untie the pool from the ledger.
    """
    return sum((_d(l.value_remaining)
                for l in StockLayer.query
                .filter(StockLayer.product_id == product_id,
                        StockLayer.qty_remaining != 0)
                .all()), ZERO)


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
    total_value = sum((_d(l.value_remaining) for l in layers), ZERO)
    return _q(total_value / total_qty)


def _preferred_layer_ids(product_id, prefer):
    """Layer ids opened by the receipt ``prefer`` = (voucher_type, voucher_id).

    A purchase return sends back the goods of ONE invoice, so it must draw from
    that invoice's layers first — taking the oldest layer instead charged a
    landed cost to stock bought at a different price, and the difference
    landed on whatever layer was left (even driving its value negative).
    """
    if not prefer:
        return []
    vtype, vid = prefer
    rows = [r.id for r in StockLedger.query.filter_by(
        voucher_type=vtype, voucher_id=vid, product_id=product_id,
        transaction_type="IN").all()]
    if not rows:
        return []
    return [l.id for l in StockLayer.query.filter(
        StockLayer.source_ledger_id.in_(rows)).all()]


def _return_source(row):
    """(voucher_type, voucher_id) a purchase-return row sends goods back from."""
    if row.voucher_type != "PRV":
        return None
    try:
        from inventory_app.models.purchase_return import InvPurchaseReturn
    except Exception:
        return None
    ret = scoped_get(InvPurchaseReturn, row.voucher_id)
    return ("PI", ret.original_invoice_id) if ret and ret.original_invoice_id else None


def _plan_consumption(product_id, qty, prefer=None):
    """(plan, uncovered) for issuing ``qty`` — oldest layers first.

    ``plan`` is [(layer, take_qty, effective_cost)]; ``uncovered`` is what no
    layer could cover. Pure: decrements nothing.

    The cost is the layer's EFFECTIVE cost — carried value over remaining
    quantity, unrounded — so that consuming a layer whole draws exactly the
    value it carries. ``unit_cost`` is the 4dp rounding of it, and rounding is
    what used to leave a residue behind on every issue.
    """
    remaining = qty
    plan = []
    layers = _open_layers(product_id)
    first = set(_preferred_layer_ids(product_id, prefer))
    if first:
        layers = ([l for l in layers if l.id in first]
                  + [l for l in layers if l.id not in first])
    for layer in layers:
        if remaining <= 0:
            break
        take = min(_d(layer.qty_remaining), remaining)
        if take <= 0:
            continue
        plan.append((layer, take, layer.unit_cost_effective))
        remaining -= take
    return plan, remaining


def _withdraw_value(plan, posted_total):
    """Take exactly ``posted_total`` of value out of the layers in ``plan``,
    decrement their quantity, and return what each one gave up.

    Exactly, because ``posted_total`` is the amount the ledger row moves by.
    Matching it here is what makes ``sum(value_remaining) == running_cost``
    true by construction rather than true to within a rounding error that
    accumulates (StockLayer). Quantity and value move together, so a layer's
    units and its worth never come apart.

    Within that total: a layer drained of quantity gives up everything it
    carries. ``_open_layers`` only sees ``qty_remaining > 0``, so a fraction of
    a cent stranded on an emptied layer is value ``stock_value`` can no longer
    count, and it would read as a loss. A partial take gives up ``take`` times
    the layer's effective cost, which leaves the units still on the layer
    costing exactly what they did before.

    Whatever those raw figures miss the posted 2dp amount by settles on the
    last layer still holding stock — the only place it can sit and still be
    counted. A plan that drains every layer it touches has nowhere to put it
    and leaves it; that needs an issue to empty whole layers to the unit, and
    it is half a cent against the 0.01 ``assert_invariant`` allows.
    """
    values = []
    for layer, take, _cost in plan:
        if take >= _d(layer.qty_remaining):
            values.append(_d(layer.value_remaining))
        else:
            values.append(_q(take * layer.unit_cost_effective))

    residual = posted_total - sum(values, ZERO)
    if residual:
        for i in range(len(plan) - 1, -1, -1):
            layer, take, _cost = plan[i]
            if _d(layer.qty_remaining) - take > 0:
                values[i] += residual
                break

    for (layer, take, _cost), value in zip(plan, values):
        layer.qty_remaining = _d(layer.qty_remaining) - take
        layer.value_remaining = _d(layer.value_remaining) - value
    return values


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
        # current_stock is a denormalised copy kept for display; the
        # StockLedger (Numeric 16,4) is authoritative and is what every costing
        # decision reads. Both are NUMERIC(16,4) now (Quantity), so fractional
        # stock shows exactly instead of truncated.
        p.current_stock = on_hand(product_id)
        # Keep the legacy static field in step with the engine so any old
        # display code shows the current valuation cost.
        unit = current_unit_cost(product_id)
        if unit > 0:
            p.cost_price = float(unit)


def _write_row(product_id, voucher_type, voucher_id, voucher_number,
               transaction_type, qty, unit_cost, total_cost, notes, created_by,
               txn_date=None):
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
        txn_date=as_txn_date(txn_date),
        notes=notes,
        created_by=created_by,
    )
    db.session.add(row)
    db.session.flush()
    return row


def _cover_shorts(product_id, receipt_row, qty, value):
    """Net a receipt against short layers, oldest first.

    Returns the (qty, value) left over for the shelf. Each covered short gives
    up its carried value for the units it gets back, so the receipt's surplus
    carries what the ledger's running cost now says it is worth. The cover is
    recorded as a negative consumption on the receipt's row, which is what a
    reversal of the receipt gives back to reopen the short.
    """
    shorts = (StockLayer.query
              .filter(StockLayer.product_id == product_id,
                      StockLayer.qty_remaining < 0)
              .order_by(StockLayer.id.asc()).all())
    for s in shorts:
        if qty <= 0:
            break
        owed = -_d(s.qty_remaining)
        take = min(owed, qty)
        if take >= owed:
            moved = -_d(s.value_remaining)
        else:
            moved = _q(take * _d(s.value_remaining) / _d(s.qty_remaining))
        s.qty_remaining = _d(s.qty_remaining) + take
        s.value_remaining = _d(s.value_remaining) + moved
        db.session.add(LayerConsumption(
            layer_id=s.id, out_ledger_id=receipt_row.id,
            product_id=product_id, qty=-take, unit_cost=_d(s.unit_cost),
            total_cost=-moved,
        ))
        qty -= take
        value -= moved
    return qty, value


def record_in(product_id, voucher_type, voucher_id, voucher_number,
              qty, unit_cost, notes="", created_by=1, txn_date=None,
              total_cost=None):
    """Stock received at an actual acquisition cost (e.g. landed purchase cost).

    FIFO opens a new layer. Weighted average merges into the open layer and
    re-averages it, so exactly one layer stays open and its cost is the
    running average.

    ``txn_date`` is the document date. Dated before the product's latest
    movement, the receipt is slotted into the timeline and every later issue
    is re-costed around it (``_replay``).

    ``total_cost`` is the exact value the document posts to the inventory
    account (a purchase line's landed total). Given, the receipt carries that
    figure; otherwise it is qty x the 4dp unit cost, which drifts from the
    journal by a paisa whenever the landed cost does not divide evenly
    (288 x 15,349.6528 = 4,420,700.01 against a posted 4,420,700.00).
    """
    qty = _d(qty)
    if qty <= 0:
        raise ValueError(
            f"Cannot receive {qty} of product {product_id}: receipt quantity "
            "must be positive."
        )
    unit_cost = _q(unit_cost)
    total_cost = _q(qty * unit_cost, 2) if total_cost is None else _q(_d(total_cost), 2)
    txn_date = as_txn_date(txn_date)
    backdated = _is_backdated(product_id, txn_date)
    row = _write_row(product_id, voucher_type, voucher_id, voucher_number,
                     "IN", qty, unit_cost, total_cost, notes, created_by,
                     txn_date=txn_date)
    if backdated:
        _replay(product_id, _key(row), new_ids={row.id},
                trigger=voucher_number, created_by=created_by)
        return row

    _apply_receipt(product_id, row, qty, unit_cost, total_cost,
                   _settings().valuation_method, notes)
    db.session.flush()
    _sync_product_stock(product_id)
    return row


def _apply_receipt(product_id, row, qty, unit_cost, total_cost, method, notes):
    """Put a receipt's quantity and value onto the layers (no ledger write)."""
    # Fill any short first: those units were already issued, so they are not
    # stock on hand and must not sit on a positive layer.
    layer_qty, layer_value = _cover_shorts(product_id, row, qty, total_cost)

    is_fifo = method == "fifo"
    open_layers = _open_layers(product_id)
    if layer_qty <= 0:
        # Entirely absorbed by the short — nothing reaches the shelf. A value
        # left over (the short was charged at a different cost) is what the
        # ledger clamps away at zero quantity, so the layers drop it too.
        pass
    elif is_fifo or not open_layers:
        db.session.add(StockLayer(
            product_id=product_id, source_ledger_id=row.id,
            unit_cost=unit_cost, qty_original=layer_qty,
            qty_remaining=layer_qty,
            # The 2dp figure this receipt posted, not qty x the 4dp cost:
            # the layer must move in the same amount the ledger moved in.
            value_remaining=layer_value,
            method=method, notes=notes,
        ))
    else:
        # Weighted average: re-average the single open layer. Any extra open
        # layers (left by a FIFO period before the switch) are folded in too,
        # so the pool collapses back to one.
        qty, total_cost = layer_qty, layer_value
        total_qty = sum((_d(l.qty_remaining) for l in open_layers), ZERO) + qty
        total_value = sum((_d(l.value_remaining)
                           for l in open_layers), ZERO) + total_cost
        keep, rest = open_layers[0], open_layers[1:]
        keep.qty_remaining = total_qty
        keep.value_remaining = total_value
        # Still the 4dp BASIS an issue is charged at. Re-averaging it no longer
        # moves the pool's value, which is what used to drift the pool away
        # from the ledger a little on every single receipt.
        keep.unit_cost = _q(total_value / total_qty) if total_qty > 0 else ZERO
        keep.qty_original = _d(keep.qty_original) + qty
        for l in rest:
            # Folded into keep above — leaving value here would double-count it.
            l.qty_remaining = ZERO
            l.value_remaining = ZERO
    db.session.flush()


def record_out(product_id, voucher_type, voucher_id, voucher_number,
               qty, notes="", created_by=1, unit_cost=None, txn_date=None,
               prefer=None):
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

    txn_date = as_txn_date(txn_date)
    if _is_backdated(product_id, txn_date):
        # Cost it where it sits in time: replay the timeline with this issue
        # slotted in. The replay refuses it if the stock was not on hand at
        # its date, or if it would strip a later issue of its stock.
        if unit_cost is not None:
            unit = _q(unit_cost)
            total = _q(qty * unit, 2)
        else:
            unit = total = ZERO
        row = _write_row(product_id, voucher_type, voucher_id, voucher_number,
                         "OUT", qty, unit, total, notes, created_by,
                         txn_date=txn_date)
        _replay(product_id, _key(row), new_ids={row.id},
                explicit_ids={row.id} if unit_cost is not None else set(),
                trigger=voucher_number, created_by=created_by)
        return _q(row.unit_cost), _q(row.total_cost, 2)

    plan, uncovered = _plan_consumption(product_id, qty, prefer)
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
                     "OUT", qty, unit, total, notes, created_by,
                     txn_date=txn_date)
    _apply_issue(product_id, row, plan, uncovered, unit, total,
                 unit if unit_cost is not None else None,
                 _settings().valuation_method)
    db.session.flush()
    _sync_product_stock(product_id)
    return unit, total


def _apply_issue(product_id, row, plan, uncovered, unit, total, explicit_unit,
                 method):
    """Take an issue's quantity and posted value off the layers (no ledger
    write). ``explicit_unit`` is the fixed basis, or None when the cost was
    drawn from the layers themselves."""
    voucher_number = row.voucher_number
    # Draw the quantity down against real layers and record what was taken
    # from where, so every posted cost can be traced to its purchases.
    withdrawn = _withdraw_value(plan, total)
    for (layer, take, layer_cost), value in zip(plan, withdrawn):
        # An explicit basis (purchase return) posts its own cost; the
        # consumption row records the basis actually charged, and the value
        # actually taken off the layer — which is what a reversal gives back,
        # so the round trip is exact.
        charged = explicit_unit if explicit_unit is not None else layer_cost
        db.session.add(LayerConsumption(
            layer_id=layer.id, out_ledger_id=row.id, product_id=product_id,
            qty=take, unit_cost=_q(charged), total_cost=value,
        ))
    if uncovered > 0:
        # Beyond stock on hand: the plan drained every layer, so the part of
        # the posted cost no layer held had nowhere to go and used to be
        # dropped — the ledger kept it, the pool lost it, and every later
        # receipt built on a diverged base. Carry it as a short layer instead:
        # a negative balance the next _withdraw_value-style accounting still
        # counts, so the pool stays tied to the ledger by construction.
        # Reversing this issue restores the consumption below and zeroes the
        # short back out exactly. Recorded even at zero value: the short also
        # carries the missing QUANTITY, which the next receipt must fill
        # before anything reaches the shelf (_cover_shorts).
        short = total - sum(withdrawn, ZERO)
        short_layer = StockLayer(
            product_id=product_id, source_ledger_id=row.id,
            unit_cost=unit, qty_original=-uncovered,
            qty_remaining=-uncovered, value_remaining=-short,
            method=method,
            notes=(f"Short {uncovered} unit(s) issued beyond stock on "
                   f"{voucher_number}; covered by a future receipt"),
        )
        db.session.add(short_layer)
        db.session.flush()
        db.session.add(LayerConsumption(
            layer_id=short_layer.id, out_ledger_id=row.id,
            product_id=product_id, qty=uncovered, unit_cost=unit,
            total_cost=short,
        ))
    db.session.flush()


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
        total_value = sum((_d(l.value_remaining) for l in layers), ZERO)
        keep, rest = layers[0], layers[1:]
        keep.qty_remaining = total_qty
        keep.qty_original = total_qty
        keep.value_remaining = total_value
        keep.unit_cost = _q(total_value / total_qty) if total_qty > 0 else ZERO
        keep.method = new_method
        keep.is_revaluation = True
        keep.notes = (f"Revaluation: collapsed {len(layers)} layers on switch "
                      f"to {new_method} at book value {_q(total_value, 2)}")
        for l in rest:
            l.qty_remaining = ZERO
            l.value_remaining = ZERO
    db.session.flush()


def assert_invariant(product_id):
    """(ok, layer_value, running_cost) — layers must equal the ledger.

    Any drift means a cost was posted that the layers cannot back, which is
    exactly how an inventory control account silently stops tying to COGS.
    Used by the costing tests.

    Both sides now move in the same posted 2dp amounts, so this is normally
    equal outright rather than equal within the tolerance. The tolerance stays
    for the one case that cannot reach it: an issue that empties whole layers
    to the unit leaves the half-cent it rounded by (``_withdraw_value``).
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
    rows = _ordered_rows(product_id)
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


def original_receipt_cost(voucher_type, voucher_id, product_id):
    """Unit cost a product was RECEIVED at on a given voucher, or None.

    What a purchase return needs: goods going back to the supplier leave stock
    at the landed cost they came in at, so the inventory credit matches the
    inventory debit the purchase made — never a tax-inclusive return value.
    Quantity-weighted when the voucher received the product on several lines.
    """
    rows = (StockLedger.query
            .filter_by(voucher_type=voucher_type, voucher_id=voucher_id,
                       product_id=product_id, transaction_type="IN").all())
    qty = sum((_d(r.quantity) for r in rows), ZERO)
    if qty <= 0:
        return None
    return _q(sum((_d(r.total_cost) for r in rows), ZERO) / qty)


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
    # frozen. Refuse on quantity instead — and on value: a receipt whose
    # value has already left through issued stock cannot come back either,
    # even when enough *units* remain (withdrawing it would delete purchase
    # value that posted COGS is still drawn from).
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
        if not _settings().is_fifo():
            received = sum((_d(x.total_cost) for x in StockLedger.query.filter_by(
                product_id=r.product_id, transaction_type="IN").all()), ZERO)
            issued = sum((_d(x.total_cost) for x in StockLedger.query.filter_by(
                product_id=r.product_id, transaction_type="OUT").all()), ZERO)
            if issued > received - _d(r.total_cost):
                raise ConsumedLayerError(
                    f"Cannot reverse {voucher_type} #{voucher_id}: "
                    f"{issued} of product {r.product_id} has already been "
                    f"issued at a posted cost, but only "
                    f"{received - _d(r.total_cost)} of received value would "
                    f"survive this reversal. Reverse the issues first, or "
                    f"reverse with a cost variance."
                )

    # Re-sequence each product without these rows. Rows dated before the
    # reversed voucher cannot be affected and stay frozen; later issues are
    # re-costed from what is left, and any cost that moves is adjusted
    # through the general ledger (_replay -> shared.cost_adjustment).
    #
    # allow_variance (purchase-invoice unapprove, i.e. "correct an earlier
    # purchase"): an issue that drew on the withdrawn receipt keeps going --
    # it is carried as a short at its posted cost until the corrected
    # document is re-approved, and re-approving it (back-dated to its own
    # date) re-costs that issue at the corrected price.
    # This voucher's own past cost adjustments belong to it and go with it.
    from shared.cost_adjustment import reverse_adjustments
    reverse_adjustments(voucher_type, voucher_id, created_by)

    thresholds = {}
    for r in rows:
        k = _key(r)
        if r.product_id not in thresholds or k < thresholds[r.product_id]:
            thresholds[r.product_id] = k
    previously_short = {pid: _short_sources(pid) for pid in product_ids}
    for pid in product_ids:
        _wipe_layers(pid)
    for r in rows:
        db.session.delete(r)
    db.session.flush()

    allow_short = allow_variance or bool(_settings().allow_negative_stock)
    moved = {}
    for pid in product_ids:
        adjustments = _replay(pid, thresholds[pid], allow_short=allow_short,
                              previously_short=previously_short[pid],
                              trigger=f"reversal of {voucher_number}",
                              created_by=created_by)
        total = sum((new - old for _r, old, new in adjustments), ZERO)
        if total:
            moved[pid] = total
    return moved


# ─────────────────────────────────────────────
# Date-ordered replay
# ─────────────────────────────────────────────

# Receipts whose cost mirrors an issue and must follow it when it is re-costed.
IN_RECOST_TYPES = {"SRV"}


PENDING_SHORT = "[pending correction]"


def _short_sources(product_id):
    """Ledger rows that already carry a short the company accepted (issued
    beyond stock under 'allow negative stock'). Shorts opened only because an
    earlier receipt was withdrawn for correction are not accepted: the
    corrected receipt has to cover them again."""
    return {l.source_ledger_id for l in StockLayer.query.filter(
        StockLayer.product_id == product_id,
        StockLayer.qty_original < 0).all()
        if l.source_ledger_id and PENDING_SHORT not in (l.notes or "")}


def _fallback_cost(product_id):
    """Cost for units issued with nothing on hand at their date: the last
    known layer cost, else the first receipt the product ever had."""
    unit = current_unit_cost(product_id)
    if unit > 0:
        return unit
    first = (StockLedger.query
             .filter(StockLedger.product_id == product_id,
                     StockLedger.transaction_type == "IN",
                     StockLedger.quantity > 0,
                     StockLedger.unit_cost > 0)
             .order_by(StockLedger.txn_date.asc(), StockLedger.id.asc()).first())
    return _d(first.unit_cost) if first else ZERO


def _wipe_layers(product_id):
    LayerConsumption.query.filter(
        LayerConsumption.product_id == product_id).delete(synchronize_session=False)
    StockLayer.query.filter(
        StockLayer.product_id == product_id).delete(synchronize_session=False)
    db.session.flush()


def _collapse_pool(product_id):
    """Weighted average holds one pool; fold any extra positive layers in."""
    layers = _open_layers(product_id)
    if len(layers) <= 1:
        return
    total_qty = sum((_d(l.qty_remaining) for l in layers), ZERO)
    total_value = sum((_d(l.value_remaining) for l in layers), ZERO)
    keep, rest = layers[0], layers[1:]
    keep.qty_remaining = total_qty
    keep.value_remaining = total_value
    keep.unit_cost = _q(total_value / total_qty) if total_qty > 0 else ZERO
    for l in rest:
        l.qty_remaining = ZERO
        l.value_remaining = ZERO
    db.session.flush()


def _shift_value(product_id, amount):
    """Apply a value-only movement (legacy VAR rows) to the newest layer."""
    layers = _open_layers(product_id)
    if layers and amount:
        layers[-1].value_remaining = _d(layers[-1].value_remaining) + amount
        db.session.flush()


def _resolve_in_cost(row):
    """Re-derived unit cost of a receipt that mirrors an issue, or None.

    A sales return comes back at the cost its sale left at; when that sale is
    re-costed, the return must follow it or the round trip invents value.
    """
    if row.voucher_type != "SRV":
        return None
    try:
        from inventory_app.models.sales_return import InvSalesReturn
    except Exception:
        return None
    ret = scoped_get(InvSalesReturn, row.voucher_id)
    if ret is None or not ret.original_invoice_id:
        return None
    return original_issue_cost("SI", ret.original_invoice_id, row.product_id)


def _replay(product_id, threshold, new_ids=(), explicit_ids=(),
            allow_short=None, previously_short=None, trigger="",
            created_by=1):
    """Rebuild a product's layers from its ledger in (txn_date, id) order.

    Rows keyed before ``threshold`` are frozen: they re-apply at exactly the
    cost they posted. Rows from ``threshold`` on are re-costed from the layers
    as they stand at that point in time. ``new_ids`` are rows being posted
    right now -- they have no journal yet, so their cost is set, not adjusted.

    Returns [(row, old_total, new_total)] for every already-posted row whose
    cost moved; each one is journalled by shared.cost_adjustment.
    """
    settings = _settings()
    if allow_short is None:
        allow_short = bool(settings.allow_negative_stock)
    if previously_short is None:
        previously_short = _short_sources(product_id)
    new_ids = set(new_ids)
    explicit_ids = set(explicit_ids)

    _wipe_layers(product_id)
    adjustments = []
    for r in _ordered_rows(product_id):
        frozen = _key(r) < threshold and r.id not in new_ids
        method = r.valuation_method or settings.valuation_method
        if method != "fifo":
            _collapse_pool(product_id)
        qty = _d(r.quantity)

        if r.transaction_type == "IN":
            if qty <= 0:
                _shift_value(product_id, _d(r.total_cost))
                continue
            unit, total = _d(r.unit_cost), _d(r.total_cost)
            if (not frozen and r.id not in new_ids
                    and r.voucher_type in IN_RECOST_TYPES):
                basis = _resolve_in_cost(r)
                if basis is not None:
                    new_total = _q(qty * basis, 2)
                    if new_total != _q(total, 2):
                        adjustments.append((r, _q(total, 2), new_total))
                    unit, total = _q(basis), new_total
                    r.unit_cost, r.total_cost = unit, total
            _apply_receipt(product_id, r, qty, unit, total, method, r.notes)
            continue

        if qty <= 0:
            _shift_value(product_id, -_d(r.total_cost))
            continue
        plan, uncovered = _plan_consumption(product_id, qty, _return_source(r))
        if (uncovered > 0 and not allow_short and not frozen
                and (r.id in new_ids or r.id not in previously_short)):
            when = r.txn_date.strftime("%d %b %Y") if r.txn_date else "its date"
            if r.id in new_ids:
                msg = (f"Cannot issue {qty.normalize()} of product {product_id} "
                       f"on {when}: only {(qty - uncovered).normalize()} were "
                       f"on hand at that date.")
            else:
                msg = (f"Cannot post this change: {r.voucher_type} "
                       f"{r.voucher_number} dated {when} would be left "
                       f"{uncovered.normalize()} unit(s) short -- the stock it "
                       f"issued would no longer have been on hand.")
            raise NegativeStockError(
                msg + " Receive the stock first, or enable 'allow negative "
                "stock' in Inventory Settings.")
        explicit = (frozen or r.voucher_type in EXPLICIT_BASIS_TYPES
                    or r.id in explicit_ids)
        if explicit:
            unit, total = _d(r.unit_cost), _d(r.total_cost)
        else:
            cost = sum((take * c for _l, take, c in plan), ZERO)
            if uncovered > 0:
                fallback = (_d(r.unit_cost)
                            if r.id not in new_ids and _d(r.unit_cost) > 0
                            else _fallback_cost(product_id))
                cost += uncovered * fallback
            unit, total = _q(cost / qty), _q(cost, 2)
            if r.id not in new_ids and total != _q(_d(r.total_cost), 2):
                adjustments.append((r, _q(_d(r.total_cost), 2), total))
            r.unit_cost, r.total_cost = unit, total
        _apply_issue(product_id, r, plan, uncovered, unit, total,
                     unit if explicit else None, method)

    db.session.flush()
    if allow_short and not settings.allow_negative_stock:
        # Shorts opened only because a receipt was withdrawn for correction.
        for l in StockLayer.query.filter(StockLayer.product_id == product_id,
                                         StockLayer.qty_remaining < 0).all():
            if l.source_ledger_id not in previously_short:
                l.notes = f"{l.notes or ''} {PENDING_SHORT}".strip()
        db.session.flush()
    rebuild_running(product_id)
    if adjustments:
        from shared.cost_adjustment import post_adjustments
        post_adjustments(adjustments, trigger, created_by)
    return adjustments


def ensure_opening_balances(created_by=1):
    """Give products that pre-date the costing engine an opening cost layer.

    Any product holding stock with no ledger history gets one IN row at its
    static cost_price, so all future issues have a historic cost to draw on,
    and the matching journal (Dr stock / Cr Opening Balance Equity). It used
    to post no journal on the assumption that the ledger already carried the
    stock; the old product form, import and quick-adjust only changed the
    display figure, so the inventory account fell short of the valuation.
    Rows written that way before are listed by Books Integrity and repaired
    with tools/repair_opening_stock.py.
    """
    from inventory_app.models.product import InvProduct
    from shared.stock_entry import journal_opening_row
    for p in InvProduct.query.filter(InvProduct.current_stock > 0).all():
        exists = StockLedger.query.filter_by(product_id=p.id).first()
        if exists:
            continue
        row = record_in(p.id, "OPENING", p.id, f"OPEN-{p.id:05d}",
                        qty=p.current_stock, unit_cost=p.cost_price or 0,
                        notes="Opening balance (pre-costing-engine stock)",
                        created_by=created_by)
        if row is not None and float(row.total_cost or 0):
            journal_opening_row(row, created_by=created_by)


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
            value_remaining=cost, method=_settings().valuation_method,
            is_revaluation=True,
            notes="Opening layer at book value (pre-layer-engine stock)",
        ))
    db.session.flush()


def backfill_layer_values():
    """Seed ``value_remaining`` on layers that pre-date the carried column.

    Those rows come back from the ALTER TABLE holding 0, which would read as
    a warehouse full of free stock. The only reconstruction available is the
    old derivation, ``qty_remaining * unit_cost`` — the very product whose
    rounding the carried column exists to stop accumulating, so the result is
    a few cents out per product rather than exact.

    So the per-product total is then settled against the ledger's own
    running_cost, which closes the drift already on the books at the moment
    the column arrives. Only a small gap is settled: a rounding residue is
    what this is for, while a large one is a real break that a migration must
    not paper over — that is left standing for ``assert_invariant`` to report.

    Runs on every boot, so it settles ONLY products it just seeded. A product
    whose layers already carry their value is left alone even if it is a few
    cents out: that gap arrived after the migration, which makes it a leak,
    and quietly closing it on every restart is how a leak stays invisible.
    """
    layers = (StockLayer.query
              .filter(StockLayer.qty_remaining > 0)
              .order_by(StockLayer.product_id.asc(), StockLayer.id.asc())
              .all())
    by_product = {}
    seeded = set()
    for layer in layers:
        if _d(layer.value_remaining) == ZERO:
            layer.value_remaining = _q(_d(layer.qty_remaining) * _d(layer.unit_cost))
            seeded.add(layer.product_id)
        by_product.setdefault(layer.product_id, []).append(layer)

    for product_id in seeded:
        product_layers = by_product[product_id]
        _qty, running_cost, _avg = StockLedger.get_running_balance(product_id)
        gap = _d(running_cost) - sum((_d(l.value_remaining) for l in product_layers), ZERO)
        if gap == ZERO:
            continue
        if abs(gap) > Decimal("1.00"):
            print(f"MIGRATION stock_layers.value_remaining: product {product_id} "
                  f"is {gap} off its ledger — too large to be rounding, left "
                  f"as reconstructed for assert_invariant to report")
            continue
        product_layers[-1].value_remaining = _d(product_layers[-1].value_remaining) + gap
    db.session.flush()
