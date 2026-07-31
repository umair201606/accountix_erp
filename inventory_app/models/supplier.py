from datetime import datetime
from ..extensions import db


class InvSupplier(db.Model):
    __tablename__ = "inv_suppliers"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)
    name = db.Column(db.String(200), nullable=False)
    contact_person = db.Column(db.String(100))
    email = db.Column(db.String(120))
    phone = db.Column(db.String(20))
    mobile = db.Column(db.String(20))
    address = db.Column(db.Text)
    city = db.Column(db.String(50))
    tax_id = db.Column(db.String(50))
    payment_terms = db.Column(db.String(100))
    website = db.Column(db.String(200))
    notes = db.Column(db.Text)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    purchase_orders = db.relationship("InvPurchaseOrder", backref="supplier", lazy="dynamic")
