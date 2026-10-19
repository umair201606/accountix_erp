"""Documents carry an edit version.

Revision ID: 0013_edit_version
Revises: 0012_bank_reconciliation
Create Date: 2026-10-15

Sales/purchase invoices, orders and returns gain ``edit_version``: bumped on
every save and state change, sent back by the form with the version it
loaded, so a save from a stale copy is refused instead of silently
overwriting another user's edit. Existing rows start at 0. No data changes.
"""
from alembic import op
import sqlalchemy as sa

revision = "0013_edit_version"
down_revision = "0012_bank_reconciliation"
branch_labels = None
depends_on = None

TABLES = ("inv_invoices", "inv_purchase_invoices", "inv_sales_orders",
          "inv_purchase_orders", "inv_sales_returns", "inv_purchase_returns")


def upgrade():
    insp = sa.inspect(op.get_bind())
    names = insp.get_table_names()
    for table in TABLES:
        if table in names and "edit_version" not in {c["name"] for c in insp.get_columns(table)}:
            op.add_column(table, sa.Column("edit_version", sa.Integer, server_default="0"))


def downgrade():
    pass
