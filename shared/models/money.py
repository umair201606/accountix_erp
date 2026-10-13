"""Column type for amounts, prices, quantities and rates on documents.

Stored as NUMERIC(20, 6): on Postgres the database keeps the exact decimal
(no binary-float noise such as 0.30000000000000004 written into an invoice
total) and SUM()/arithmetic in SQL is exact. Six places is finer than the
general ledger's four (journal lines, stock layers are NUMERIC(16, 4)), so
nothing posted from a document changes.

Python still receives a float — every route and template does float
arithmetic on these attributes, and handing them Decimal would raise
TypeError on the first ``decimal + float``. Values are rounded to 6 places
by the database on write.
"""
from sqlalchemy.types import Numeric, TypeDecorator

PRECISION = 20
SCALE = 6


class Money(TypeDecorator):
    # asdecimal=False on the impl too: expressions built from these columns
    # (price * qty, SUM(...)) take the impl's type and must also come back
    # as float, not Decimal.
    impl = Numeric(PRECISION, SCALE, asdecimal=False)
    cache_ok = True

    def process_result_value(self, value, dialect):
        # SQLite's NUMERIC affinity hands back an int for whole numbers.
        return None if value is None else float(value)


class Quantity(TypeDecorator):
    """Stock quantities: NUMERIC(16, 4) like the stock ledger.

    The product's on-hand figure was an INTEGER, so 2.5 kg on hand showed as
    2 (and Postgres rounds a fractional value written to an integer column)
    while invoices, vouchers and the ledger kept the exact quantity. Whole
    numbers come back as int so lists still read "10", not "10.0".
    """
    impl = Numeric(16, 4, asdecimal=False)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return None if value is None else round(float(value), 4)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        v = round(float(value), 4)
        return int(v) if v.is_integer() else v
