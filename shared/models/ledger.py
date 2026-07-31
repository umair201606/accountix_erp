from datetime import datetime
from decimal import Decimal
from shared.extensions import db


class ChartOfAccount(db.Model):
    """Five-level segmented chart: 1 / 1-01 / 1-01-01 / 1-01-01-01 / 1-01-01-01-0001.

    Levels 1-4 are aggregating accounts: they only roll up child balances and
    must never carry journal lines. Level 5 is the operational level — the
    only level journal entries may post to (enforced in post_journal_entry).
    """
    __tablename__ = "chart_of_accounts"
    POSTING_LEVEL = 5

    __table_args__ = (
        db.UniqueConstraint("company_id", "code",
                            name="uq_chart_of_accounts_code"),
    )

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)
    code = db.Column(db.String(30), nullable=False)
    name = db.Column(db.String(200), nullable=False)
    type = db.Column(db.String(50), nullable=False)
    parent_id = db.Column(db.Integer, db.ForeignKey("chart_of_accounts.id"), nullable=True)
    level = db.Column(db.Integer, nullable=False, default=5)
    is_fixed = db.Column(db.Boolean, default=False)
    is_active = db.Column(db.Boolean, default=True)
    description = db.Column(db.Text)
    # Cash-flow statement activity: operating / investing / financing, or
    # "cash" for cash-and-equivalent accounts (the statement's target figure).
    # Null inherits from the nearest tagged ancestor.
    cash_flow_activity = db.Column(db.String(20))
    # P&L section: sales, sales_returns, other_income, cost_of_sales, admin,
    # selling_distribution, other_operating, finance_cost, income_tax.
    # Null inherits from the nearest tagged ancestor.
    pl_section = db.Column(db.String(30))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    parent = db.relationship("ChartOfAccount", remote_side=[id], backref="children")

    @property
    def is_postable(self):
        return self.level >= self.POSTING_LEVEL

    def is_leaf(self):
        return ChartOfAccount.query.filter_by(parent_id=self.id).count() == 0

    def can_delete(self):
        return not self.is_fixed and self.is_leaf()

    def display_path(self):
        parts = []
        a = self
        while a:
            parts.append(a.name)
            a = a.parent
        return " > ".join(reversed(parts))

    def _inherited(self, attr):
        a = self
        while a:
            val = getattr(a, attr)
            if val:
                return val
            a = a.parent
        return None

    def effective_cash_flow_activity(self):
        return self._inherited("cash_flow_activity")

    def effective_pl_section(self):
        return self._inherited("pl_section")


class JournalEntry(db.Model):
    __tablename__ = "journal_entries"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)
    voucher_type = db.Column(db.String(50), nullable=False)
    voucher_id = db.Column(db.Integer, nullable=False)
    voucher_number = db.Column(db.String(50), nullable=False, index=True)
    description = db.Column(db.Text)
    entry_date = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    is_posted = db.Column(db.Boolean, default=True)

    lines = db.relationship("JournalLine", backref="entry", lazy="dynamic",
                            cascade="all, delete-orphan")


class JournalLine(db.Model):
    __tablename__ = "journal_lines"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)
    journal_entry_id = db.Column(db.Integer, db.ForeignKey("journal_entries.id"), nullable=False)
    account_id = db.Column(db.Integer, db.ForeignKey("chart_of_accounts.id"), nullable=False)
    debit = db.Column(db.Numeric(16, 4), default=Decimal("0.0000"))
    credit = db.Column(db.Numeric(16, 4), default=Decimal("0.0000"))
    description = db.Column(db.Text)

    account = db.relationship("ChartOfAccount")
