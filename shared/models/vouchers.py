from datetime import datetime
from shared.extensions import db


class ConsumptionVoucher(db.Model):
    __tablename__ = "consumption_vouchers"
    __table_args__ = (
        db.UniqueConstraint("company_id", "voucher_number",
                            name="uq_consumption_vouchers_voucher_number"),
    )
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)
    voucher_number = db.Column(db.String(50), nullable=False)
    date = db.Column(db.DateTime, default=datetime.utcnow)
    department = db.Column(db.String(100))
    reason = db.Column(db.Text)
    # Ledger account the consumed value is charged (debited) to — any level-4
    # account: an expense, an employee's account, a project, ...
    charge_account_id = db.Column(db.Integer, db.ForeignKey("chart_of_accounts.id"))
    status = db.Column(db.String(20), default="unapproved")
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    approved_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    approved_at = db.Column(db.DateTime)

    items = db.relationship("ConsumptionItem", backref="voucher", lazy="dynamic",
                            cascade="all, delete-orphan")


class ConsumptionItem(db.Model):
    __tablename__ = "consumption_items"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)
    voucher_id = db.Column(db.Integer, db.ForeignKey("consumption_vouchers.id"), nullable=False)
    product_id = db.Column(db.Integer, nullable=False)
    product_name = db.Column(db.String(200))
    quantity = db.Column(db.Numeric(16, 4), nullable=False)
    unit_cost = db.Column(db.Numeric(16, 4), default=0)
    total_cost = db.Column(db.Numeric(16, 4), default=0)


class ScrapVoucher(db.Model):
    __tablename__ = "scrap_vouchers"
    __table_args__ = (
        db.UniqueConstraint("company_id", "voucher_number",
                            name="uq_scrap_vouchers_voucher_number"),
    )
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)
    voucher_number = db.Column(db.String(50), nullable=False)
    date = db.Column(db.DateTime, default=datetime.utcnow)
    reason = db.Column(db.Text)
    # Account debited with the scrapped value — scrap loss by default, or a
    # receivable (employee, insurer) when someone is charged for the loss.
    charge_account_id = db.Column(db.Integer, db.ForeignKey("chart_of_accounts.id"))
    status = db.Column(db.String(20), default="unapproved")
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    approved_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    approved_at = db.Column(db.DateTime)

    items = db.relationship("ScrapItem", backref="voucher", lazy="dynamic",
                            cascade="all, delete-orphan")


class ScrapItem(db.Model):
    __tablename__ = "scrap_items"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)
    voucher_id = db.Column(db.Integer, db.ForeignKey("scrap_vouchers.id"), nullable=False)
    product_id = db.Column(db.Integer, nullable=False)
    product_name = db.Column(db.String(200))
    quantity = db.Column(db.Numeric(16, 4), nullable=False)
    unit_cost = db.Column(db.Numeric(16, 4), default=0)
    total_cost = db.Column(db.Numeric(16, 4), default=0)


class StockAdjustmentVoucher(db.Model):
    __tablename__ = "stock_adjustment_vouchers"
    __table_args__ = (
        db.UniqueConstraint("company_id", "voucher_number",
                            name="uq_stock_adjustment_vouchers_voucher_number"),
    )
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)
    voucher_number = db.Column(db.String(50), nullable=False)
    date = db.Column(db.DateTime, default=datetime.utcnow)
    reason = db.Column(db.Text)
    status = db.Column(db.String(20), default="unapproved")
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    approved_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    approved_at = db.Column(db.DateTime)

    items = db.relationship("StockAdjustmentItem", backref="voucher", lazy="dynamic",
                            cascade="all, delete-orphan")


class StockAdjustmentItem(db.Model):
    __tablename__ = "stock_adjustment_items"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)
    voucher_id = db.Column(db.Integer, db.ForeignKey("stock_adjustment_vouchers.id"), nullable=False)
    product_id = db.Column(db.Integer, nullable=False)
    product_name = db.Column(db.String(200))
    system_qty = db.Column(db.Numeric(16, 4), default=0)
    physical_qty = db.Column(db.Numeric(16, 4), default=0)
    difference = db.Column(db.Numeric(16, 4), default=0)
    unit_cost = db.Column(db.Numeric(16, 4), default=0)
    total_cost = db.Column(db.Numeric(16, 4), default=0)


class StockTake(db.Model):
    __tablename__ = "stock_takes"
    __table_args__ = (
        db.UniqueConstraint("company_id", "reference",
                            name="uq_stock_takes_reference"),
    )
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)
    reference = db.Column(db.String(50), nullable=False)
    date = db.Column(db.DateTime, default=datetime.utcnow)
    location = db.Column(db.String(100))
    status = db.Column(db.String(20), default="in_progress")
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    approved_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    approved_at = db.Column(db.DateTime)
    adjustment_voucher_id = db.Column(db.Integer, db.ForeignKey("stock_adjustment_vouchers.id"))

    items = db.relationship("StockTakeItem", backref="take", lazy="dynamic",
                            cascade="all, delete-orphan")
    adjustment_voucher = db.relationship("StockAdjustmentVoucher")


class StockTakeItem(db.Model):
    __tablename__ = "stock_take_items"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)
    stock_take_id = db.Column(db.Integer, db.ForeignKey("stock_takes.id"), nullable=False)
    product_id = db.Column(db.Integer, nullable=False)
    product_name = db.Column(db.String(200))
    system_qty = db.Column(db.Numeric(16, 4), default=0)
    physical_qty = db.Column(db.Numeric(16, 4), default=0)
    difference = db.Column(db.Numeric(16, 4), default=0)
    unit_cost = db.Column(db.Numeric(16, 4), default=0)
