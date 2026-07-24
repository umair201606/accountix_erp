from datetime import datetime
from ..extensions import db


class InvSalesOrder(db.Model):
    __tablename__ = "inv_sales_orders"
    id = db.Column(db.Integer, primary_key=True)
    so_number = db.Column(db.String(50), unique=True, nullable=False)
    customer_id = db.Column(db.Integer, db.ForeignKey("inv_customers.id"), nullable=False)
    party_account_id = db.Column(db.Integer, db.ForeignKey("chart_of_accounts.id"))
    order_date = db.Column(db.Date, default=datetime.utcnow)
    expected_date = db.Column(db.Date)
    status = db.Column(db.String(20), default="unapproved")
    discount_mode = db.Column(db.String(20), default="general")
    charges_mode = db.Column(db.String(20), default="general")
    tax_mode = db.Column(db.String(20), default="general")
    global_discount_pct = db.Column(db.Float, default=0)
    global_discount_value = db.Column(db.Float, default=0)
    global_delivery = db.Column(db.Float, default=0)
    global_installation = db.Column(db.Float, default=0)
    global_sales_tax_pct = db.Column(db.Float, default=0)
    further_tax_pct = db.Column(db.Float, default=0)
    apply_further_tax = db.Column(db.Boolean, default=False)
    withholding_tax_pct = db.Column(db.Float, default=0)
    apply_withholding_tax = db.Column(db.Boolean, default=False)
    subtotal = db.Column(db.Float, default=0)
    total_discount = db.Column(db.Float, default=0)
    total_charges = db.Column(db.Float, default=0)
    total_tax = db.Column(db.Float, default=0)
    total_further_tax = db.Column(db.Float, default=0)
    total_withholding_tax = db.Column(db.Float, default=0)
    total_amount = db.Column(db.Float, default=0)
    notes = db.Column(db.Text)
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    approved_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    approved_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    items = db.relationship("InvSalesOrderItem", backref="sales_order",
                            lazy="dynamic", cascade="all, delete-orphan")
    invoices = db.relationship("InvInvoice", backref="sales_order", lazy="dynamic")
    creator = db.relationship("User", backref="sales_orders", foreign_keys=[created_by])
    approver = db.relationship("User", backref="approved_sales_orders", foreign_keys=[approved_by])

    @property
    def charges_list(self):
        from inventory_app.models.additional_charge import AdditionalCharge
        return AdditionalCharge.query.filter_by(doc_type="SO", doc_id=self.id).all()


class InvSalesOrderItem(db.Model):
    __tablename__ = "inv_sales_order_items"
    id = db.Column(db.Integer, primary_key=True)
    so_id = db.Column(db.Integer, db.ForeignKey("inv_sales_orders.id"), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey("inv_products.id"), nullable=False)
    description = db.Column(db.String(200), default="")
    unit = db.Column(db.String(20), default="pcs")
    quantity = db.Column(db.Float, default=1)
    unit_price = db.Column(db.Float, default=0)
    discount_pct = db.Column(db.Float, default=0)
    discount_amount = db.Column(db.Float, default=0)
    delivery = db.Column(db.Float, default=0)
    installation = db.Column(db.Float, default=0)
    sales_tax_pct = db.Column(db.Float, default=0)
    total_before_discount = db.Column(db.Float, default=0)
    total_after_discount = db.Column(db.Float, default=0)
    total_price = db.Column(db.Float, default=0)
