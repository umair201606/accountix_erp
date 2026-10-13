"""Payroll policy settings, PF contribution base, leaving date.

Revision ID: 0010_payroll_policy
Revises: 0009_payslip_taxable_pay
Create Date: 2026-10-13

* payroll_settings: overtime rate, EOBI, high-income surcharge, final
  settlement (gratuity, leave encashment) per company.
* pf_config.contribution_base: existing companies keep "gross" (what their
  PF has been calculated on); new configurations default to "basic".
* company_memberships.date_of_leaving: payroll pays up to it, then stops.
* payroll_runs.paid_at, pf_ledger.payroll_run_id; loans approved before
  in-system disbursement are marked disbursed so recovery continues.
"""
from alembic import op
import sqlalchemy as sa

revision = "0010_payroll_policy"
down_revision = "0009_payslip_taxable_pay"
branch_labels = None
depends_on = None


def _cols(insp, table):
    return {c["name"] for c in insp.get_columns(table)}


def upgrade():
    conn = op.get_bind()
    insp = sa.inspect(conn)
    names = insp.get_table_names()
    if "payroll_settings" not in names:
        op.create_table(
            "payroll_settings",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("company_id", sa.Integer, index=True),
            sa.Column("overtime_multiplier", sa.Numeric(20, 6)),
            sa.Column("overtime_hours_basis", sa.Numeric(20, 6)),
            sa.Column("eobi_enabled", sa.Boolean),
            sa.Column("eobi_wage_base", sa.Numeric(20, 6)),
            sa.Column("eobi_employee_pct", sa.Numeric(20, 6)),
            sa.Column("eobi_employer_pct", sa.Numeric(20, 6)),
            sa.Column("surcharge_threshold", sa.Numeric(20, 6)),
            sa.Column("surcharge_pct", sa.Numeric(20, 6)),
            sa.Column("gratuity_enabled", sa.Boolean),
            sa.Column("gratuity_basis", sa.String(10)),
            sa.Column("gratuity_days_per_year", sa.Numeric(20, 6)),
            sa.Column("leave_encashment_enabled", sa.Boolean),
        )
    else:
        have = _cols(insp, "payroll_settings")
        for name, typ in (("gratuity_enabled", sa.Boolean), ("gratuity_basis", sa.String(10)),
                          ("gratuity_days_per_year", sa.Numeric(20, 6)),
                          ("leave_encashment_enabled", sa.Boolean)):
            if name not in have:
                op.add_column("payroll_settings", sa.Column(name, typ))
    if "pf_config" in names and "contribution_base" not in _cols(insp, "pf_config"):
        op.add_column("pf_config", sa.Column("contribution_base", sa.String(10)))
        conn.execute(sa.text("UPDATE pf_config SET contribution_base = 'gross'"))
    if "company_memberships" in names and "date_of_leaving" not in _cols(insp, "company_memberships"):
        op.add_column("company_memberships", sa.Column("date_of_leaving", sa.Date))
    if "payroll_runs" in names and "paid_at" not in _cols(insp, "payroll_runs"):
        op.add_column("payroll_runs", sa.Column("paid_at", sa.DateTime))
    if "pf_ledger" in names and "payroll_run_id" not in _cols(insp, "pf_ledger"):
        op.add_column("pf_ledger", sa.Column("payroll_run_id", sa.Integer))
    if "loan_advance_requests" in names:
        # Staff loans are now paid out through the system and only recovered
        # once paid. Loans approved before that were paid outside it: treat
        # them as paid on approval so their instalments carry on.
        conn.execute(sa.text(
            "UPDATE loan_advance_requests SET disbursed_at = COALESCE(approved_at, created_at) "
            "WHERE status IN ('approved', 'paid') AND disbursed_at IS NULL"))


def downgrade():
    pass
