from ..extensions import db
from shared.models.money import Money


class IncomeTaxSlab(db.Model):
    __tablename__ = "income_tax_slabs"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)
    min_income = db.Column(Money, nullable=False, default=0)
    max_income = db.Column(Money, nullable=False, default=999999999)
    rate_pct = db.Column(Money, nullable=False, default=0)
    fixed_amount = db.Column(Money, nullable=False, default=0)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=db.func.now())

    @staticmethod
    def calculate_tax(annual_gross):
        """Annual tax on annual taxable income from the company's slabs.

        No slabs means no tax is computed (the run screen says so). It used
        to fall back to a flat 5% that is in no tax law."""
        slabs = IncomeTaxSlab.query.filter_by(is_active=True).order_by(IncomeTaxSlab.min_income).all()
        income = float(annual_gross or 0)
        for slab in reversed(slabs):
            if income > float(slab.min_income or 0):
                tax = float(slab.fixed_amount or 0) + (income - float(slab.min_income or 0))                     * float(slab.rate_pct or 0) / 100
                return round(tax, 2)
        return 0.0
