"""Stock quantities keep their decimals.

Revision ID: 0011_stock_quantity_decimal
Revises: 0010_payroll_policy
Create Date: 2026-10-13

inv_products.current_stock and inv_stock_movements.quantity were INTEGER
while every document line and the stock ledger are NUMERIC: 2.5 kg on hand
showed as 2, and Postgres rounded fractional movement rows. Both become
NUMERIC(16, 4) (Postgres; SQLite keeps whatever is stored), then the on-hand
figure is recomputed from the stock ledger for every product that has one.
"""
from alembic import op
import sqlalchemy as sa

revision = "0011_stock_quantity_decimal"
down_revision = "0010_payroll_policy"
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    names = sa.inspect(conn).get_table_names()
    if conn.dialect.name == "postgresql":
        for table, column in (("inv_products", "current_stock"),
                              ("inv_stock_movements", "quantity")):
            if table in names:
                conn.execute(sa.text(
                    f"ALTER TABLE {table} ALTER COLUMN {column} TYPE NUMERIC(16, 4) "
                    f"USING {column}::numeric"))
    if "inv_products" in names and "stock_ledger" in names:
        conn.execute(sa.text(
            "UPDATE inv_products SET current_stock = ("
            " SELECT COALESCE(SUM(CASE WHEN s.transaction_type = 'IN' THEN s.quantity"
            "                      ELSE -s.quantity END), 0)"
            " FROM stock_ledger s WHERE s.product_id = inv_products.id)"
            " WHERE EXISTS (SELECT 1 FROM stock_ledger s WHERE s.product_id = inv_products.id)"))


def downgrade():
    pass
