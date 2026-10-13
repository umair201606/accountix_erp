"""Payroll slips: store the taxable pay.

Revision ID: 0009_payslip_taxable_pay
Revises: 0008_capitalisation_drafts
Create Date: 2026-10-13

Withholding tax is now cumulative over the tax year, so each slip records the
pay that counted for tax. Older slips are left NULL and read as their gross
pay, which is what they were taxed on.
"""
from alembic import op
import sqlalchemy as sa

revision = "0009_payslip_taxable_pay"
down_revision = "0008_capitalisation_drafts"
branch_labels = None
depends_on = None


def upgrade():
    insp = sa.inspect(op.get_bind())
    if "payroll_slips" not in insp.get_table_names():
        return
    if "taxable_pay" not in {c["name"] for c in insp.get_columns("payroll_slips")}:
        op.add_column("payroll_slips", sa.Column("taxable_pay", sa.Numeric(20, 6)))


def downgrade():
    pass
