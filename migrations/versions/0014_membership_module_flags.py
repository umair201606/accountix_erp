"""Module access per company.

Revision ID: 0014_membership_module_flags
Revises: 0013_edit_version
Create Date: 2026-10-20

``company_memberships.module_flags`` (JSON text, NULL) holds which modules a
person may open in that company. The ``users.has_*_access`` flags are shared
by every company the person belongs to, so one company's admin switching a
module off for them switched it off everywhere. NULL falls back to those
flags, so existing members keep exactly what they have. No data changes.
"""
from alembic import op
import sqlalchemy as sa

revision = "0014_membership_module_flags"
down_revision = "0013_edit_version"
branch_labels = None
depends_on = None


def upgrade():
    insp = sa.inspect(op.get_bind())
    if "company_memberships" in insp.get_table_names() and "module_flags" not in {
            c["name"] for c in insp.get_columns("company_memberships")}:
        op.add_column("company_memberships", sa.Column("module_flags", sa.Text, nullable=True))


def downgrade():
    pass
