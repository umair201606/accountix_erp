from ..extensions import db


class IncomeTaxSlab(db.Model):
    __tablename__ = "income_tax_slabs"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)
    min_income = db.Column(db.Float, nullable=False, default=0)
    max_income = db.Column(db.Float, nullable=False, default=999999999)
    rate_pct = db.Column(db.Float, nullable=False, default=0)
    fixed_amount = db.Column(db.Float, nullable=False, default=0)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=db.func.now())

    @staticmethod
    def calculate_tax(annual_gross):
        slabs = IncomeTaxSlab.query.filter_by(is_active=True).order_by(IncomeTaxSlab.min_income).all()
        if not slabs:
            return round(annual_gross * 0.05, 2)
        for slab in reversed(slabs):
            if annual_gross > slab.min_income:
                tax = slab.fixed_amount + (annual_gross - slab.min_income) * slab.rate_pct / 100
                return round(tax, 2)
        return 0
