from datetime import datetime

from shared.extensions import db


class StockLayer(db.Model):
    """A parcel of stock on hand at a known unit cost.

    Layers are the source of truth for what stock COSTS. They are never
    derived by replaying the ledger — ``qty_remaining`` is decremented as
    stock is issued, so the pool always reflects what actually happened
    rather than what a valuation method assumes happened.

    The two valuation methods differ in exactly one place: what a RECEIPT
    does.

        FIFO             each receipt opens a new layer.
        weighted average each receipt merges into the single open layer,
                         re-averaging its unit cost.

    Issuing is identical under both: consume layers oldest-first at each
    layer's own cost. Under weighted average there is only ever one open
    layer, so that cost IS the running average.

    This keeps the invariant that makes a method switch safe:

        sum(qty_remaining * unit_cost) == StockLedger.running_cost

    which ``assert_invariant`` checks and the costing tests enforce.
    """

    __tablename__ = "stock_layers"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)
    product_id = db.Column(db.Integer, nullable=False, index=True)
    # The IN row that opened this layer. Null for layers created by a
    # revaluation (a method switch collapses/carries value, it receives none).
    source_ledger_id = db.Column(db.Integer, db.ForeignKey("stock_ledger.id"), nullable=True)
    unit_cost = db.Column(db.Numeric(16, 4), nullable=False)
    qty_original = db.Column(db.Numeric(16, 4), nullable=False)
    qty_remaining = db.Column(db.Numeric(16, 4), nullable=False, index=True)
    # Which method opened it — audit only; consumption never reads this.
    method = db.Column(db.String(20), nullable=False, default="weighted_average")
    is_revaluation = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    notes = db.Column(db.Text)

    @property
    def value_remaining(self):
        from decimal import Decimal
        return Decimal(str(self.qty_remaining)) * Decimal(str(self.unit_cost))


class LayerConsumption(db.Model):
    """Which OUT row consumed which layer, and at what cost.

    One row per (issue, layer) pair — an issue spanning three FIFO layers
    writes three rows. This is the audit trail behind every posted cost:
    given a COGS or receivable figure, these rows show exactly which
    purchases backed it.

    Kept append-only. Reversing an issue writes a compensating row rather
    than deleting, so the history of what was posted survives.
    """

    __tablename__ = "layer_consumptions"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)
    layer_id = db.Column(db.Integer, db.ForeignKey("stock_layers.id"), nullable=False, index=True)
    out_ledger_id = db.Column(db.Integer, db.ForeignKey("stock_ledger.id"), nullable=False, index=True)
    product_id = db.Column(db.Integer, nullable=False, index=True)
    qty = db.Column(db.Numeric(16, 4), nullable=False)
    unit_cost = db.Column(db.Numeric(16, 4), nullable=False)
    total_cost = db.Column(db.Numeric(16, 4), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    layer = db.relationship("StockLayer", backref="consumptions")
