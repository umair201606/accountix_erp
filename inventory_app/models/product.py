from datetime import datetime
from ..extensions import db


class InvProduct(db.Model):
    __tablename__ = "inv_products"
    id = db.Column(db.Integer, primary_key=True)
    sku = db.Column(db.String(50), unique=True, nullable=False)
    name = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text)
    category_id = db.Column(db.Integer, db.ForeignKey("inv_categories.id"))
    unit_price = db.Column(db.Float, default=0)
    cost_price = db.Column(db.Float, default=0)
    reorder_level = db.Column(db.Integer, default=0)
    current_stock = db.Column(db.Integer, default=0)
    unit = db.Column(db.String(20), default="pcs")
    hs_code = db.Column(db.String(50), default="")
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    movements = db.relationship("InvStockMovement", backref="product", lazy="dynamic")
    po_items = db.relationship("InvPurchaseOrderItem", backref="product", lazy="dynamic")
    so_items = db.relationship("InvSalesOrderItem", backref="product", lazy="dynamic")
