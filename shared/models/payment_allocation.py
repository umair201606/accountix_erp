"""Payment/receipt -> invoice assignment records.

A row here says "this much of that voucher line belongs to that invoice". It is
a SUBLEDGER MATCHING record and nothing else:

* No journal entry is written, reversed or amended when an allocation is
  created or removed. The money already hit the ledger when the CPV/CRV/BPV/
  BRV/JV was approved (Dr Cash / Cr Customer, or Dr Supplier / Cr Cash), and
  posting anything here would double-count it.
* No chart-of-accounts balance, trial balance, ledger or financial statement
  figure changes because of this table. Deleting every row would leave the
  general ledger identical.

What it does drive is invoice-level tracking: how much of an invoice has been
settled, what is still outstanding, how old that balance is, and which receipts
paid it. ``shared/payment_tracking.py`` is the only module that writes it.
"""

from datetime import datetime
from decimal import Decimal

from shared.extensions import db


class PaymentAllocation(db.Model):
    __tablename__ = "payment_allocations"
    __table_args__ = (
        # One line settles a given invoice through exactly one row; assigning
        # again updates the amount instead of stacking a second row (which is
        # what made "assigned" totals drift in the first prototype).
        db.UniqueConstraint("company_id", "voucher_line_id", "doc_type", "doc_id",
                            name="uq_payment_allocations_line_doc"),
    )

    SALES = "SI"
    PURCHASE = "PI"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)

    # The money side: the accounting voucher and the specific line of it that
    # moved the party's balance. The line matters — one voucher can pay four
    # suppliers, and each of those amounts is assigned independently.
    voucher_id = db.Column(db.Integer, db.ForeignKey("accounting_vouchers.id"),
                           nullable=False, index=True)
    voucher_line_id = db.Column(db.Integer,
                                db.ForeignKey("accounting_voucher_lines.id"),
                                nullable=False, index=True)

    # The document side. Sales invoices and purchase invoices live in separate
    # tables, so the reference is (doc_type, doc_id) rather than a foreign key.
    doc_type = db.Column(db.String(4), nullable=False)   # "SI" | "PI"
    doc_id = db.Column(db.Integer, nullable=False, index=True)

    # Denormalised party, so the feed and the ageing queries do not have to
    # re-derive it from the account code on every read.
    party_type = db.Column(db.String(10), nullable=False)  # customer | supplier
    party_id = db.Column(db.Integer, nullable=False, index=True)

    amount = db.Column(db.Numeric(16, 4), nullable=False,
                       default=Decimal("0.0000"))
    note = db.Column(db.String(300), default="")

    created_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    voucher = db.relationship("AccountingVoucher")
    line = db.relationship("AccountingVoucherLine")
    creator = db.relationship("User", foreign_keys=[created_by])
