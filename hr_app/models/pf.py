from datetime import datetime, date
from ..extensions import db


class ProvidentFundConfig(db.Model):
    __tablename__ = "pf_config"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)
    employee_contribution_pct = db.Column(db.Float, default=5.0)
    employer_contribution_pct = db.Column(db.Float, default=5.0)
    max_loan_percentage = db.Column(db.Float, default=50.0)
    interest_rate = db.Column(db.Float, default=0.0)
    min_service_months_for_loan = db.Column(db.Integer, default=12)
    is_active = db.Column(db.Boolean, default=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class PFContribution(db.Model):
    __tablename__ = "pf_contributions"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    month = db.Column(db.Integer, nullable=False)
    year = db.Column(db.Integer, nullable=False)
    employee_amount = db.Column(db.Float, default=0.0)
    employer_amount = db.Column(db.Float, default=0.0)
    total_amount = db.Column(db.Float, default=0.0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship("User", backref="pf_contributions")

    __table_args__ = (db.UniqueConstraint("company_id", "user_id", "month",
                                          "year",
                                          name="uq_pf_contribution_company"),)


class PFLedger(db.Model):
    __tablename__ = "pf_ledger"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    transaction_date = db.Column(db.Date, default=date.today, nullable=False)
    transaction_type = db.Column(db.String(30), nullable=False)
    description = db.Column(db.Text)
    debit = db.Column(db.Float, default=0.0)
    credit = db.Column(db.Float, default=0.0)
    balance = db.Column(db.Float, default=0.0)
    reference_id = db.Column(db.Integer)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship("User", backref="pf_ledger")


class PFWithdrawalRequest(db.Model):
    __tablename__ = "pf_withdrawal_requests"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    reason = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(20), default="pending")
    approved_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    approved_at = db.Column(db.DateTime)
    disbursed_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship("User", foreign_keys=[user_id], backref="pf_withdrawals")
    approver = db.relationship("User", foreign_keys=[approved_by])


class PFLoanRequest(db.Model):
    __tablename__ = "pf_loan_requests"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    installment_months = db.Column(db.Integer, default=12)
    purpose = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(20), default="pending")
    approved_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    approved_at = db.Column(db.DateTime)
    monthly_installment = db.Column(db.Float)
    remaining_amount = db.Column(db.Float)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship("User", foreign_keys=[user_id], backref="pf_loans")
    approver = db.relationship("User", foreign_keys=[approved_by])
