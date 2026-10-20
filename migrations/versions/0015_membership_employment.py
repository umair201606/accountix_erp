"""Manager, designation and department per company.

Revision ID: 0015_membership_employment
Revises: 0014_membership_module_flags
Create Date: 2026-10-20

``company_memberships`` gains ``manager_id``, ``designation`` and
``department``. They lived only on the user row, which every company the
person belongs to shares, so one company's HR choosing someone's manager made
that person their leave / loan / timesheet approver in every company.

Each existing membership is filled from its user row: designation and
department as they are; the manager only when that manager is an active
member of the same company (anyone else could never approve there anyway).
The user row keeps its values as the account-wide default. Re-running is
harmless: only memberships whose three fields are all empty are filled.
"""
from alembic import op
import sqlalchemy as sa

revision = "0015_membership_employment"
down_revision = "0014_membership_module_flags"
branch_labels = None
depends_on = None

COLUMNS = (("manager_id", sa.Integer), ("designation", sa.String(100)),
           ("department", sa.String(100)))


def upgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "company_memberships" not in insp.get_table_names():
        return
    have = {c["name"] for c in insp.get_columns("company_memberships")}
    for name, type_ in COLUMNS:
        if name not in have:
            op.add_column("company_memberships", sa.Column(name, type_, nullable=True))
    if "ix_company_memberships_manager_id" not in {
            i["name"] for i in insp.get_indexes("company_memberships")}:
        op.create_index("ix_company_memberships_manager_id",
                        "company_memberships", ["manager_id"])

    # Correlated subqueries: the same SQL on SQLite and Postgres.
    bind.execute(sa.text("""
        UPDATE company_memberships
           SET designation = (SELECT u.designation FROM users u
                               WHERE u.id = company_memberships.user_id),
               department  = (SELECT u.department FROM users u
                               WHERE u.id = company_memberships.user_id),
               manager_id  = (SELECT u.manager_id FROM users u
                               WHERE u.id = company_memberships.user_id
                                 AND u.manager_id <> company_memberships.user_id
                                 AND EXISTS (
                                     SELECT 1 FROM company_memberships m2
                                      WHERE m2.company_id = company_memberships.company_id
                                        AND m2.user_id = u.manager_id
                                        AND m2.status = 'active'))
         WHERE manager_id IS NULL AND designation IS NULL AND department IS NULL
    """))


def downgrade():
    pass
