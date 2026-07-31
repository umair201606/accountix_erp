from datetime import datetime, date
from ..extensions import db


class PayrollProfile(db.Model):
    __tablename__ = "payroll_profiles"
    __table_args__ = (
        db.UniqueConstraint("company_id", "user_id",
                            name="uq_payroll_profiles_user_id"),
    )
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    basic_salary = db.Column(db.Float, default=0.0)
    effective_from = db.Column(db.Date, nullable=False)
    payment_method = db.Column(db.String(50), default="bank_transfer")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = db.relationship("User", backref="payroll_profile")
    components = db.relationship("PayrollComponent", backref="profile", lazy="dynamic", cascade="all, delete-orphan")


class PayrollComponent(db.Model):
    __tablename__ = "payroll_components"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)
    profile_id = db.Column(db.Integer, db.ForeignKey("payroll_profiles.id"), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    type = db.Column(db.String(20), nullable=False)
    calculation_method = db.Column(db.String(30), default="fixed")
    value = db.Column(db.Float, default=0.0)
    is_taxable = db.Column(db.Boolean, default=True)


class PayrollRun(db.Model):
    __tablename__ = "payroll_runs"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)
    month = db.Column(db.Integer, nullable=False)
    year = db.Column(db.Integer, nullable=False)
    run_date = db.Column(db.DateTime, default=datetime.utcnow)
    status = db.Column(db.String(20), default="unapproved")
    total_gross = db.Column(db.Float, default=0.0)
    total_deductions = db.Column(db.Float, default=0.0)
    total_net = db.Column(db.Float, default=0.0)
    employee_count = db.Column(db.Integer, default=0)
    processed_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    approved_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    approved_at = db.Column(db.DateTime)
    notes = db.Column(db.Text)

    processor = db.relationship("User", foreign_keys=[processed_by])
    approver = db.relationship("User", foreign_keys=[approved_by])
    slips = db.relationship("PayrollSlip", backref="payroll_run", lazy="dynamic")

    __table_args__ = (db.UniqueConstraint("company_id", "month", "year",
                                          name="uq_payroll_run_company"),)


class PayrollSlip(db.Model):
    __tablename__ = "payroll_slips"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)
    payroll_run_id = db.Column(db.Integer, db.ForeignKey("payroll_runs.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    basic_salary = db.Column(db.Float, default=0.0)
    allowances = db.Column(db.Float, default=0.0)
    deductions = db.Column(db.Float, default=0.0)
    gross_pay = db.Column(db.Float, default=0.0)
    total_deductions = db.Column(db.Float, default=0.0)
    net_pay = db.Column(db.Float, default=0.0)
    components_json = db.Column(db.Text)
    pdf_filename = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship("User", backref="payroll_slips")

    __table_args__ = (db.UniqueConstraint("company_id", "payroll_run_id",
                                          "user_id",
                                          name="uq_payroll_slip_company"),)


class SalaryRevision(db.Model):
    __tablename__ = "salary_revisions"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    previous_basic = db.Column(db.Float)
    new_basic = db.Column(db.Float, nullable=False)
    reason = db.Column(db.Text)
    approved_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    effective_from = db.Column(db.Date, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship("User", foreign_keys=[user_id], backref="salary_revisions")
    approver = db.relationship("User", foreign_keys=[approved_by])
