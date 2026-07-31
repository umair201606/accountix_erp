from datetime import datetime
from ..extensions import db


class InvStockMovement(db.Model):
    __tablename__ = "inv_stock_movements"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)
    product_id = db.Column(db.Integer, db.ForeignKey("inv_products.id"), nullable=False)
    type = db.Column(db.String(20), nullable=False)
    quantity = db.Column(db.Integer, nullable=False)
    reference_type = db.Column(db.String(50))
    reference_id = db.Column(db.Integer)
    notes = db.Column(db.Text)
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    creator = db.relationship("User", backref="stock_movements")
