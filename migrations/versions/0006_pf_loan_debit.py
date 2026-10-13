"""PF loans: approved loans were CREDITED to the member's fund ledger.

Revision ID: 0006_pf_loan_debit
Revises: 0005_leave_quota_days
Create Date: 2026-10-12

Borrowing from the provident fund increased the member's balance by the
loan amount (hr_app/routes/pf.py posted credit=amount). It is now a debit;
this turns the existing rows round so balances, loan limits and settlements
read the same as new ones. Only the rows that route wrote are touched
(type "loan", description "PF loan approved", credit only).
"""
from alembic import op
import sqlalchemy as sa

revision = "0006_pf_loan_debit"
down_revision = "0005_leave_quota_days"
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    if "pf_ledger" not in sa.inspect(conn).get_table_names():
        return
    conn.execute(sa.text(
        "UPDATE pf_ledger SET debit = credit, credit = 0 "
        "WHERE transaction_type = 'loan' AND description = 'PF loan approved' "
        "AND COALESCE(credit, 0) > 0 AND COALESCE(debit, 0) = 0"))


def downgrade():
    pass
