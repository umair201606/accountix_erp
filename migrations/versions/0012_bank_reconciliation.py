"""Bank reconciliation tables.

Revision ID: 0012_bank_reconciliation
Revises: 0011_stock_quantity_decimal
Create Date: 2026-10-13

bank_reconciliations: one bank/cash account up to one statement date.
bank_rec_items: the posted journal lines it marks as cleared.
"""
from alembic import op
import sqlalchemy as sa

revision = "0012_bank_reconciliation"
down_revision = "0011_stock_quantity_decimal"
branch_labels = None
depends_on = None


def upgrade():
    names = sa.inspect(op.get_bind()).get_table_names()
    if "bank_reconciliations" not in names:
        op.create_table(
            "bank_reconciliations",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("company_id", sa.Integer, index=True),
            sa.Column("account_id", sa.Integer, sa.ForeignKey("chart_of_accounts.id"),
                      nullable=False),
            sa.Column("statement_date", sa.Date, nullable=False),
            sa.Column("statement_balance", sa.Numeric(20, 6), nullable=False),
            sa.Column("status", sa.String(20)),
            sa.Column("notes", sa.Text),
            sa.Column("created_by", sa.Integer, sa.ForeignKey("users.id")),
            sa.Column("created_at", sa.DateTime),
            sa.Column("completed_at", sa.DateTime),
        )
    if "bank_rec_items" not in names:
        op.create_table(
            "bank_rec_items",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("company_id", sa.Integer, index=True),
            sa.Column("reconciliation_id", sa.Integer,
                      sa.ForeignKey("bank_reconciliations.id"), nullable=False, index=True),
            sa.Column("journal_line_id", sa.Integer, sa.ForeignKey("journal_lines.id"),
                      nullable=False, index=True),
            sa.UniqueConstraint("reconciliation_id", "journal_line_id", name="uq_bank_rec_item"),
        )


def downgrade():
    pass
