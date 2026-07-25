from datetime import datetime
from ..extensions import db


class InvPurchaseOrder(db.Model):
    __tablename__ = "inv_purchase_orders"
    id = db.Column(db.Integer, primary_key=True)
    po_number = db.Column(db.String(50), unique=True, nullable=False)
    supplier_id = db.Column(db.Integer, db.ForeignKey("inv_suppliers.id"), nullable=False)
    party_account_id = db.Column(db.Integer, db.ForeignKey("chart_of_accounts.id"))
    order_date = db.Column(db.Date, default=datetime.utcnow)
    expected_date = db.Column(db.Date)
    status = db.Column(db.String(20), default="unapproved")
    # Approval status (above) and invoicing progress (below) are independent.
    # open | partial | invoiced — maintained by shared/order_linkage.py (§4.4).
    fulfilment_status = db.Column(db.String(20), default="open")
    tax_mode = db.Column(db.String(20), default="general")
    global_sales_tax_pct = db.Column(db.Float, default=0)
    subtotal = db.Column(db.Float, default=0)
    total_tax = db.Column(db.Float, default=0)
    total_amount = db.Column(db.Float, default=0)
    notes = db.Column(db.Text)
    driver_name = db.Column(db.String(100), default="")
    driver_contact = db.Column(db.String(50), default="")
    vehicle_number = db.Column(db.String(50), default="")
    gate_pass = db.Column(db.String(50), default="")
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    approved_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    approved_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    items = db.relationship("InvPurchaseOrderItem", backref="purchase_order",
                            lazy="dynamic", cascade="all, delete-orphan")
    creator = db.relationship("User", backref="purchase_orders", foreign_keys=[created_by])
    approver = db.relationship("User", backref="approved_purchase_orders", foreign_keys=[approved_by])



class InvPurchaseOrderItem(db.Model):
    __tablename__ = "inv_purchase_order_items"
    id = db.Column(db.Integer, primary_key=True)
    po_id = db.Column(db.Integer, db.ForeignKey("inv_purchase_orders.id"), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey("inv_products.id"), nullable=False)
    description = db.Column(db.String(200), default="")
    unit = db.Column(db.String(20), default="pcs")
    quantity = db.Column(db.Float, default=1)
    # Billed so far. On the purchase side §4.2 caps loading at
    # received-not-yet-invoiced for 3-way PO/receipt/invoice matching.
    invoiced_qty = db.Column(db.Float, default=0)
    unit_price = db.Column(db.Float, default=0)
    sales_tax_pct = db.Column(db.Float, default=0)
    total_before_discount = db.Column(db.Float, default=0)
    total_after_discount = db.Column(db.Float, default=0)
    total_price = db.Column(db.Float, default=0)
