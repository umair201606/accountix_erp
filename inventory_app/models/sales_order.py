from datetime import datetime
from ..extensions import db


class InvSalesOrder(db.Model):
    __tablename__ = "inv_sales_orders"
    __table_args__ = (
        db.UniqueConstraint("company_id", "so_number",
                            name="uq_inv_sales_orders_so_number"),
    )
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)
    so_number = db.Column(db.String(50), nullable=False)
    customer_id = db.Column(db.Integer, db.ForeignKey("inv_customers.id"), nullable=False)
    party_account_id = db.Column(db.Integer, db.ForeignKey("chart_of_accounts.id"))
    order_date = db.Column(db.Date, default=datetime.utcnow)
    expected_date = db.Column(db.Date)
    status = db.Column(db.String(20), default="unapproved")
    # Approval status (above) and invoicing progress (below) are independent:
    # an approved order is still "open" until something is invoiced against it.
    # open | partial | invoiced — maintained by shared/order_linkage.py (§4.4).
    fulfilment_status = db.Column(db.String(20), default="open")
    tax_mode = db.Column(db.String(20), default="general")
    global_sales_tax_pct = db.Column(db.Float, default=0)
    subtotal = db.Column(db.Float, default=0)
    total_tax = db.Column(db.Float, default=0)
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



class InvSalesOrderItem(db.Model):
    __tablename__ = "inv_sales_order_items"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)
    so_id = db.Column(db.Integer, db.ForeignKey("inv_sales_orders.id"), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey("inv_products.id"), nullable=False)
    description = db.Column(db.String(200), default="")
    unit = db.Column(db.String(20), default="pcs")
    quantity = db.Column(db.Float, default=1)
    # How much of `quantity` has been billed. The uninvoiced balance is
    # quantity - invoiced_qty; the order picker caps "this invoice" at it (§4.2)
    # and save refuses to exceed it beyond the admin tolerance (§4.4, §11.2).
    invoiced_qty = db.Column(db.Float, default=0)
    unit_price = db.Column(db.Float, default=0)
    sales_tax_pct = db.Column(db.Float, default=0)
    total_before_discount = db.Column(db.Float, default=0)
    total_after_discount = db.Column(db.Float, default=0)
    total_price = db.Column(db.Float, default=0)
