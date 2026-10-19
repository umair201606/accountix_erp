from datetime import datetime
from decimal import Decimal
from shared.extensions import db


class StockLedger(db.Model):
    __tablename__ = "stock_ledger"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)
    product_id = db.Column(db.Integer, nullable=False, index=True)
    voucher_type = db.Column(db.String(50), nullable=False)
    voucher_id = db.Column(db.Integer, nullable=False)
    voucher_number = db.Column(db.String(50), nullable=False, index=True)
    transaction_type = db.Column(db.String(10), nullable=False)
    quantity = db.Column(db.Numeric(16, 4), nullable=False)
    unit_cost = db.Column(db.Numeric(16, 4), nullable=False)
    total_cost = db.Column(db.Numeric(16, 4), nullable=False)
    running_qty = db.Column(db.Numeric(16, 4), nullable=False)
    running_cost = db.Column(db.Numeric(16, 4), nullable=False)
    running_avg = db.Column(db.Numeric(16, 4), nullable=False)
    # Method in force when this row was priced. Audit only — cost comes from
    # the layers, never from re-interpreting history under today's method.
    valuation_method = db.Column(db.String(20))
    # The DOCUMENT date the movement belongs to (invoice date, voucher date),
    # not the moment it was keyed in. Cost layers are consumed in
    # (txn_date, id) order, so a back-dated document is costed where it
    # belongs in time and every later issue is re-costed around it
    # (shared/costing.py: _replay).
    txn_date = db.Column(db.Date, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    notes = db.Column(db.Text)

    @classmethod
    def get_running_balance(cls, product_id):
        # Latest by document date: a back-dated row has the highest id but
        # sits in the middle of the timeline.
        last = (cls.query.filter_by(product_id=product_id)
                .order_by(cls.txn_date.desc(), cls.id.desc()).first())
        if last:
            return last.running_qty, last.running_cost, last.running_avg
        return Decimal("0.0000"), Decimal("0.0000"), Decimal("0.0000")


class VoucherNumber(db.Model):
    __tablename__ = "voucher_numbers"
    __table_args__ = (
        db.UniqueConstraint("company_id", "prefix",
                            name="uq_voucher_numbers_prefix"),
    )
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)
    prefix = db.Column(db.String(20), nullable=False)
    next_number = db.Column(db.Integer, nullable=False, default=1)

    @classmethod
    def next(cls, prefix):
        """Allocate the next number for ``prefix`` in the ACTIVE company.

        One atomic ``UPDATE ... SET next_number = next_number + 1 RETURNING``
        on the company's row: two users saving at the same instant queue on
        that row and get consecutive numbers. (Reading the counter and writing
        it back from Python handed both of them the same number.) A prefix's
        first use creates its row with ``ON CONFLICT DO NOTHING``, so two
        first uses at once cannot fail either.

        Nothing is committed here. The number belongs to the caller's
        transaction: it is kept if the document is saved and given back if
        the save fails, so the series has no gaps — and committing here used
        to commit the caller's half-built document along with the counter.
        The row stays locked until the caller commits; documents of one
        series are therefore numbered strictly in the order they are saved.
        """
        from sqlalchemy import update
        from shared.tenancy import NoActiveCompanyError, current_company_id
        cid = current_company_id()
        if cid is None:
            raise NoActiveCompanyError(f"VoucherNumber.next({prefix!r}) with no active company")
        T = cls.__table__
        conn = db.session.connection()
        if conn.dialect.name == "postgresql":
            from sqlalchemy.dialects.postgresql import insert
        else:
            from sqlalchemy.dialects.sqlite import insert
        conn.execute(insert(T).values(company_id=cid, prefix=prefix, next_number=1)
                     .on_conflict_do_nothing(index_elements=["company_id", "prefix"]))
        taken = conn.execute(
            update(T).where(T.c.company_id == cid, T.c.prefix == prefix)
            .values(next_number=T.c.next_number + 1)
            .returning(T.c.next_number)).scalar_one()
        # A VoucherNumber row already loaded in this session (settings page,
        # an earlier allocation) must not write its stale value back.
        for obj in list(db.session.identity_map.values()):
            if isinstance(obj, cls) and obj.prefix == prefix:
                db.session.expire(obj)
        num = taken - 1
        return f"{prefix}-{num:05d}"
