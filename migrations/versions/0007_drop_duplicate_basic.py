"""Payroll profiles: drop the "Basic Salary" allowance that doubled basic pay.

Revision ID: 0007_drop_duplicate_basic
Revises: 0006_pf_loan_debit
Create Date: 2026-10-12

Creating a profile added an allowance component named "Basic Salary" at
100% of the profile's basic. Gross pay is basic + allowances, so every such
profile paid its basic twice. Only exact duplicates are removed: type
allowance, that name, value equal to the profile's basic. Slips already
issued are history and are not touched.
"""
from alembic import op
import sqlalchemy as sa

revision = "0007_drop_duplicate_basic"
down_revision = "0006_pf_loan_debit"
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    names = sa.inspect(conn).get_table_names()
    if "payroll_components" not in names or "payroll_profiles" not in names:
        return
    conn.execute(sa.text(
        "DELETE FROM payroll_components WHERE type = 'allowance' AND name = 'Basic Salary' "
        "AND EXISTS (SELECT 1 FROM payroll_profiles p WHERE p.id = payroll_components.profile_id "
        "AND ABS(COALESCE(p.basic_salary, 0) - COALESCE(payroll_components.value, 0)) < 0.005)"))


def downgrade():
    pass
