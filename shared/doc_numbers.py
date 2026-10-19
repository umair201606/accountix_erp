"""Document numbers two users saving at the same instant cannot share.

Sales/purchase invoices, orders and returns are numbered PREFIX-YYYYMM-NNNN.
NNNN used to be "the last document's id + 1", read before saving: two users
saving together read the same last id, built the same number, and the second
save hit the unique key — an error page, the whole document lost.

The number is now built from the new row's OWN id, which the database hands
out uniquely, so it is decided by the insert itself. Until the row has its
id it carries a placeholder (the columns are NOT NULL and unique per
company); ``number_from_id`` replaces it in the same transaction, so no
placeholder is ever committed.
"""
from uuid import uuid4

from shared.extensions import db


def placeholder():
    return "~" + uuid4().hex[:24]


def is_placeholder(value):
    return bool(value) and str(value).startswith("~")


def number_from_id(doc, prefix, *columns):
    """Number a new document PREFIX-YYYYMM-<its id> (YYYYMM by the company's
    clock) and write it to every column in ``columns``."""
    from shared.clock import local_now
    if doc.id is None:
        db.session.flush()
    number = f"{prefix}-{local_now():%Y%m}-{doc.id:04d}"
    for column in columns:
        setattr(doc, column, number)
    return number
