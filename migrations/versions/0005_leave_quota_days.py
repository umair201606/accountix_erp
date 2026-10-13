"""Leave quotas: INTEGER -> NUMERIC days, balances recomputed from requests.

Revision ID: 0005_leave_quota_days
Revises: 0004_invoice_dates_timestamp
Create Date: 2026-10-12

Half-day leave adds 0.5 to a quota, which INTEGER columns rounded on
Postgres. ``used`` / ``pending`` were also nudged with += / -= by two copies
of the approval code (approving twice counted twice) and ``remaining`` was
only ever set by the seed. The app now derives all three from the requests
(hr_app/leave_flow.py); this brings existing rows to the same figures. No
screen edits used / pending by hand, so the requests are the whole truth.
"""
from alembic import op
import sqlalchemy as sa

revision = "0005_leave_quota_days"
down_revision = "0004_invoice_dates_timestamp"
branch_labels = None
depends_on = None

COLUMNS = ("total", "used", "pending", "remaining")


def upgrade():
    conn = op.get_bind()
    insp = sa.inspect(conn)
    if "leave_quotas" not in insp.get_table_names() or \
            "leave_requests" not in insp.get_table_names():
        return
    pg = conn.dialect.name == "postgresql"
    if pg:
        for column in COLUMNS:
            dtype = conn.execute(sa.text(
                "SELECT data_type FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = 'leave_quotas' "
                "AND column_name = :c"), {"c": column}).scalar()
            if dtype in ("integer", "bigint", "smallint"):
                conn.execute(sa.text(
                    f'ALTER TABLE leave_quotas ALTER COLUMN "{column}" TYPE NUMERIC(20, 6)'))
    year = ("EXTRACT(YEAR FROM r.start_date)" if pg
            else "CAST(strftime('%Y', r.start_date) AS INTEGER)")

    def days(status):
        return (f"COALESCE((SELECT SUM(r.total_days) FROM leave_requests r "
                f"WHERE r.user_id = leave_quotas.user_id "
                f"AND r.leave_type_id = leave_quotas.leave_type_id "
                f"AND (r.company_id = leave_quotas.company_id "
                f"     OR (r.company_id IS NULL AND leave_quotas.company_id IS NULL)) "
                f"AND r.status = '{status}' AND {year} = leave_quotas.year), 0)")
    conn.execute(sa.text(
        f"UPDATE leave_quotas SET used = {days('approved')}, pending = {days('pending')}"))
    conn.execute(sa.text(
        "UPDATE leave_quotas SET remaining = COALESCE(total, 0) - used - pending"))


def downgrade():
    pass
