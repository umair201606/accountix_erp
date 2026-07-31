from datetime import datetime
from ..extensions import db


class InvUnit(db.Model):
    __tablename__ = "inv_units"
    __table_args__ = (
        db.UniqueConstraint("company_id", "name",
                            name="uq_inv_units_name"),
        db.UniqueConstraint("company_id", "abbreviation",
                            name="uq_inv_units_abbreviation"),
    )
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)
    name = db.Column(db.String(100), nullable=False)
    abbreviation = db.Column(db.String(20), nullable=False)
    explanation = db.Column(db.Text)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __str__(self):
        return self.abbreviation
