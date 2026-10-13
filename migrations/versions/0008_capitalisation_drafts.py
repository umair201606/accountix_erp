"""Stock capitalisations: keep the requested quantity, park drafts.

Revision ID: 0008_capitalisation_drafts
Revises: 0007_drop_duplicate_basic
Create Date: 2026-10-13

A capitalisation saved without approving created its fixed asset as
"active" straight away (from Inventory, at a typed-in cost with no
acquisition journal), so the depreciation run charged an asset the ledger
did not hold. The quantity asked for was not stored, so the draft could not
be approved later either. Drafts are now status "draft" (not in service, not
in the register) until the transfer is approved.

Only assets whose sole link is an unapproved capitalisation and that carry no
depreciation rows are moved.
"""
from alembic import op
import sqlalchemy as sa

revision = "0008_capitalisation_drafts"
down_revision = "0007_drop_duplicate_basic"
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    insp = sa.inspect(conn)
    names = insp.get_table_names()
    if "asset_transfers" not in names:
        return
    cols = {c["name"] for c in insp.get_columns("asset_transfers")}
    if "quantity" not in cols:
        op.add_column("asset_transfers",
                      sa.Column("quantity", sa.Numeric(20, 6), server_default="1"))
    if "fixed_assets" not in names:
        return
    dep = ("AND NOT EXISTS (SELECT 1 FROM asset_depreciation d WHERE d.asset_id = fixed_assets.id) "
           if "asset_depreciation" in names else "")
    conn.execute(sa.text(
        "UPDATE fixed_assets SET status = 'draft', is_active = :f, purchase_cost = 0, "
        "current_book_value = 0, accumulated_depreciation = 0 "
        "WHERE status IN ('active', 'inactive') "
        "AND EXISTS (SELECT 1 FROM asset_transfers t WHERE t.asset_id = fixed_assets.id "
        "AND t.direction IN ('to_fixed_asset', 'from_inventory') AND t.status = 'unapproved') "
        "AND NOT EXISTS (SELECT 1 FROM asset_transfers t WHERE t.asset_id = fixed_assets.id "
        "AND t.status = 'approved') "
        "AND NOT EXISTS (SELECT 1 FROM journal_entries j WHERE j.voucher_type = 'FA-ACQ' "
        "AND j.voucher_id = fixed_assets.id AND j.is_posted = :t) " + dep),
        {"f": False, "t": True})


def downgrade():
    pass
