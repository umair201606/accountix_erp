from datetime import datetime, date
from ..extensions import db


class LoanAdvanceRequest(db.Model):
    __tablename__ = "loan_advance_requests"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    request_type = db.Column(db.String(20), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    purpose = db.Column(db.Text, nullable=False)
    installment_months = db.Column(db.Integer, default=12)
    monthly_installment = db.Column(db.Float)
    remaining_amount = db.Column(db.Float)
    status = db.Column(db.String(20), default="pending")
    approved_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    approved_at = db.Column(db.DateTime)
    disbursed_at = db.Column(db.DateTime)
    queue_for_payroll = db.Column(db.Boolean, default=False)
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship("User", foreign_keys=[user_id], backref="loan_requests")
    approver = db.relationship("User", foreign_keys=[approved_by])


class LoanRepayment(db.Model):
    __tablename__ = "loan_repayments"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)
    loan_id = db.Column(db.Integer, db.ForeignKey("loan_advance_requests.id"), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    paid_at = db.Column(db.DateTime, default=datetime.utcnow)
    payroll_run_id = db.Column(db.Integer, db.ForeignKey("payroll_runs.id"))
    notes = db.Column(db.Text)

    loan = db.relationship("LoanAdvanceRequest", backref="repayments")
    payroll_run = db.relationship("PayrollRun")
