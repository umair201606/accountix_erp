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
                tax = (float(slab.fixed_amount or 0)
                       + (income - float(slab.min_income or 0)) * float(slab.rate_pct or 0) / 100)
                return round(tax, 2)
        return 0.0


class PayrollSettings(db.Model):
    """Company payroll policy that differs between employers.

    One row per company (created on first read). Defaults follow common
    Pakistani practice; every figure is editable on Tax & payroll settings.
    """
    __tablename__ = "payroll_settings"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)
    # Overtime pay per hour = basic / overtime_hours_basis x multiplier.
    # Factories Act s.47 sets double rate for factory workers.
    overtime_multiplier = db.Column(Money, default=1.5)
    overtime_hours_basis = db.Column(Money, default=160)
    # EOBI: contributions on a statutory wage base (the minimum wage).
    eobi_enabled = db.Column(db.Boolean, default=False)
    eobi_wage_base = db.Column(Money, default=37000)
    eobi_employee_pct = db.Column(Money, default=1)
    eobi_employer_pct = db.Column(Money, default=5)
    # Surcharge on the income tax of salaried individuals above a threshold
    # (Finance Act 2025: 9% of the tax where taxable income exceeds 10m).
    surcharge_threshold = db.Column(Money, default=10_000_000)
    surcharge_pct = db.Column(Money, default=9)
    # Final settlement, paid in the leaver's last payroll month.
    # Gratuity: days' wages per year of service (a part year over six months
    # counts as a year — Standing Orders 12(6) practice), on basic or gross.
    gratuity_enabled = db.Column(db.Boolean, default=False)
    gratuity_basis = db.Column(db.String(10), default="basic")
    gratuity_days_per_year = db.Column(Money, default=30)
    # Unused days of paid leave types that carry forward, at basic / 30 a day.
    leave_encashment_enabled = db.Column(db.Boolean, default=False)

    @staticmethod
    def get():
        s = PayrollSettings.query.order_by(PayrollSettings.id).first()
        if s is None:
            # First use: queued per company so two requests create one row.
            from shared.concurrency import serialize
            from shared.tenancy import current_company_id
            serialize("first-use", "payroll_settings", current_company_id())
            s = PayrollSettings.query.order_by(PayrollSettings.id).first()
        if s is None:
            s = PayrollSettings(overtime_multiplier=1.5, overtime_hours_basis=160,
                                eobi_enabled=False, eobi_wage_base=37000,
                                eobi_employee_pct=1, eobi_employer_pct=5,
                                surcharge_threshold=10_000_000, surcharge_pct=9,
                                gratuity_enabled=False, gratuity_basis="basic",
                                gratuity_days_per_year=30, leave_encashment_enabled=False)
            db.session.add(s)
            # Only this row: the getter can run mid-save, and a full flush
            # pushed the caller's half-built objects early.
            db.session.flush(objects=[s])
        return s
