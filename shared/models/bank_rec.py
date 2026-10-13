"""Bank reconciliation: the cash book matched to the bank statement.

A reconciliation is one bank (or cash) account up to one statement date with
the statement's closing balance. Ticking a posted journal line on that
account marks it as cleared — it appears on the statement. The
reconciliation is complete when the cleared balance equals the statement
balance; what is left unticked is the reconciliation statement's unpresented
payments and deposits in transit.

Lines are referenced, never copied: a journal un-posted later simply drops
out of every total (and a completed reconciliation that loses a cleared line
shows the difference again).
"""
from datetime import datetime

from shared.extensions import db
from shared.models.money import Money


class BankReconciliation(db.Model):
    __tablename__ = "bank_reconciliations"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)
    account_id = db.Column(db.Integer, db.ForeignKey("chart_of_accounts.id"), nullable=False)
    statement_date = db.Column(db.Date, nullable=False)
    statement_balance = db.Column(Money, nullable=False, default=0)
    status = db.Column(db.String(20), default="draft")       # draft | completed
    notes = db.Column(db.Text, default="")
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    completed_at = db.Column(db.DateTime)

    account = db.relationship("ChartOfAccount")
    items = db.relationship("BankRecItem", backref="reconciliation", lazy="dynamic",
                            cascade="all, delete-orphan")


class BankRecItem(db.Model):
    """A journal line cleared by a reconciliation."""
    __tablename__ = "bank_rec_items"
    __table_args__ = (
        db.UniqueConstraint("reconciliation_id", "journal_line_id", name="uq_bank_rec_item"),
    )
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)
    reconciliation_id = db.Column(db.Integer, db.ForeignKey("bank_reconciliations.id"),
                                  nullable=False, index=True)
    journal_line_id = db.Column(db.Integer, db.ForeignKey("journal_lines.id"), nullable=False,
                                index=True)
