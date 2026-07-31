from datetime import datetime
from ..extensions import db


class InvProduct(db.Model):
    __tablename__ = "inv_products"
    __table_args__ = (
        db.UniqueConstraint("company_id", "sku",
                            name="uq_inv_products_sku"),
    )
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)
    sku = db.Column(db.String(50), nullable=False)
    name = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text)
    category_id = db.Column(db.Integer, db.ForeignKey("inv_categories.id"))
    unit_price = db.Column(db.Float, default=0)
    cost_price = db.Column(db.Float, default=0)
    reorder_level = db.Column(db.Integer, default=0)
    current_stock = db.Column(db.Integer, default=0)
    unit = db.Column(db.String(20), default="pcs")
    hs_code = db.Column(db.String(50), default="")
    # Unit weight, for the "By weight" charge distribution (§6.2). The basis is
    # the line's total mass — this times the quantity — since freight and the
    # like scale with what is actually shipped, not with the unit.
    weight = db.Column(db.Float, default=0)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    movements = db.relationship("InvStockMovement", backref="product", lazy="dynamic")
    po_items = db.relationship("InvPurchaseOrderItem", backref="product", lazy="dynamic")
    so_items = db.relationship("InvSalesOrderItem", backref="product", lazy="dynamic")
