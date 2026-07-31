from datetime import datetime
from decimal import Decimal
from shared.extensions import db


class AccountingVoucher(db.Model):
    __tablename__ = "accounting_vouchers"
    __table_args__ = (
        db.UniqueConstraint("company_id", "voucher_number",
                            name="uq_accounting_vouchers_voucher_number"),
    )
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)
    voucher_type = db.Column(db.String(10), nullable=False)
    voucher_number = db.Column(db.String(50), nullable=False)
    voucher_date = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    cash_bank_account_id = db.Column(db.Integer, db.ForeignKey("chart_of_accounts.id"), nullable=True)
    notes = db.Column(db.Text)
    status = db.Column(db.String(20), default="unapproved")
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    approved_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    approved_at = db.Column(db.DateTime, nullable=True)

    cash_bank_account = db.relationship("ChartOfAccount", foreign_keys=[cash_bank_account_id])
    creator = db.relationship("User", foreign_keys=[created_by])
    approver = db.relationship("User", foreign_keys=[approved_by])
    lines = db.relationship("AccountingVoucherLine", backref="voucher",
                            lazy="dynamic", cascade="all, delete-orphan",
                            order_by="AccountingVoucherLine.line_no")


class AccountingVoucherLine(db.Model):
    __tablename__ = "accounting_voucher_lines"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)
    voucher_id = db.Column(db.Integer, db.ForeignKey("accounting_vouchers.id"), nullable=False)
    line_no = db.Column(db.Integer, nullable=False)
    account_id = db.Column(db.Integer, db.ForeignKey("chart_of_accounts.id"), nullable=False)
    description = db.Column(db.String(300), default="")
    debit = db.Column(db.Numeric(16, 4), default=Decimal("0.0000"))
    credit = db.Column(db.Numeric(16, 4), default=Decimal("0.0000"))

    account = db.relationship("ChartOfAccount")
