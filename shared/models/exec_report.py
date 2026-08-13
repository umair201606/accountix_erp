"""Which ledger accounts the Executive Reports module watches.

A row here says "include this account in the receivables/payables view". It
carries no amounts and no side: which side an account lands on is decided by
its balance at read time, never stored (see ``shared/executive_reports.py``).

Selecting a parent selects everything beneath it, so a company can either
watch a whole control account ("Trade Debtors") or pick individual children
inside it. Nothing here posts, and removing every row would leave the general
ledger identical.
"""

from datetime import datetime

from shared.extensions import db


class ExecAccountSelection(db.Model):
    __tablename__ = "exec_account_selections"
    __table_args__ = (
        # One row per account per company; re-selecting is a no-op rather than
        # a second row that would double the account in the totals.
        db.UniqueConstraint("company_id", "account_id",
                            name="uq_exec_selection_company_account"),
    )

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)

    account_id = db.Column(db.Integer, db.ForeignKey("chart_of_accounts.id"),
                           nullable=False, index=True)

    # True  -> this account and every descendant is in scope.
    # False -> only this exact account, so a parent can be watched while one
    #          of its children is deliberately excluded.
    include_children = db.Column(db.Boolean, default=True, nullable=False)

    created_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    account = db.relationship("ChartOfAccount")
