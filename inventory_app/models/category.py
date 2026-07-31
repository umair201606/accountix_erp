from datetime import datetime
from ..extensions import db


class InvCategory(db.Model):
    __tablename__ = "inv_categories"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)
    name = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text)
    parent_id = db.Column(db.Integer, db.ForeignKey("inv_categories.id"))
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    parent = db.relationship("InvCategory", remote_side=[id], backref="children")
    products = db.relationship("InvProduct", backref="category", lazy="dynamic")
