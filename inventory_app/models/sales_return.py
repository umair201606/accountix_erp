from datetime import datetime
from ..extensions import db


class InvSalesReturn(db.Model):
    """Goods coming back from a customer against an approved sales invoice.

    The mirror of InvPurchaseReturn. The value side is posted to the contra
    revenue account (Sales Returns) rather than debited against Revenue, which
    is why the fixed chart carries 4-02 and the P&L has a "Less: Sales Returns
    & Discounts" line between Sales and Net Sales.
    """

    __tablename__ = "inv_sales_returns"
    __table_args__ = (
        db.UniqueConstraint("company_id", "return_number",
                            name="uq_inv_sales_returns_return_number"),
    )
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)
    return_number = db.Column(db.String(50), nullable=False)
    original_invoice_id = db.Column(db.Integer, db.ForeignKey("inv_invoices.id"), nullable=False)
    customer_id = db.Column(db.Integer, db.ForeignKey("inv_customers.id"), nullable=False)
    # Mirrors the invoice: when settings allow an arbitrary counterparty, the
    # credit goes here instead of to the customer's own subledger.
    party_account_id = db.Column(db.Integer, db.ForeignKey("chart_of_accounts.id"))
    return_date = db.Column(db.DateTime, default=datetime.utcnow)
    notes = db.Column(db.Text)

    gross_return_value = db.Column(db.Float, default=0)
    total_discount = db.Column(db.Float, default=0)
    total_charges = db.Column(db.Float, default=0)
    total_tax = db.Column(db.Float, default=0)
    net_return_amount = db.Column(db.Float, default=0)
    # Cost of the returned goods at the basis they were sold at — the value
    # that goes back onto Inventory and comes off COGS.
    total_cost_returned = db.Column(db.Float, default=0)

    reverse_charges = db.Column(db.Boolean, default=True)

    status = db.Column(db.String(20), default="new")
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    approved_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    approved_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    original_invoice = db.relationship("InvInvoice", foreign_keys=[original_invoice_id])
    customer = db.relationship("InvCustomer", foreign_keys=[customer_id])
    creator = db.relationship("User", foreign_keys=[created_by])
    approver = db.relationship("User", foreign_keys=[approved_by])
    items = db.relationship("InvSalesReturnItem", backref="return_doc",
                            lazy="dynamic", cascade="all, delete-orphan")


class InvSalesReturnItem(db.Model):
    __tablename__ = "inv_sales_return_items"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)
    return_id = db.Column(db.Integer, db.ForeignKey("inv_sales_returns.id"), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey("inv_products.id"))
    description = db.Column(db.String(300))
    original_quantity = db.Column(db.Float, default=0)
    previously_returned_qty = db.Column(db.Float, default=0)
    max_returnable_qty = db.Column(db.Float, default=0)
    current_return_qty = db.Column(db.Float, default=0)
    unit = db.Column(db.String(20), default="pcs")
    unit_price = db.Column(db.Float, default=0)

    discount_pct = db.Column(db.Float, default=0)
    discount_amount = db.Column(db.Float, default=0)
    delivery = db.Column(db.Float, default=0)
    installation = db.Column(db.Float, default=0)
    sales_tax_pct = db.Column(db.Float, default=0)

    total_before_discount = db.Column(db.Float, default=0)
    total_after_discount = db.Column(db.Float, default=0)
    proportional_discount = db.Column(db.Float, default=0)
    proportional_sales_tax = db.Column(db.Float, default=0)
    proportional_delivery = db.Column(db.Float, default=0)
    proportional_installation = db.Column(db.Float, default=0)
    net_return_value = db.Column(db.Float, default=0)

    # Unit cost this stock was ISSUED at on the original invoice — what it
    # re-enters stock at. Recorded per line so the cost side of the return can
    # be traced back to the sale it reverses, and so a later change of
    # valuation method cannot re-price a return that is already posted.
    cost_basis = db.Column(db.Float, default=0)
    total_cost_returned = db.Column(db.Float, default=0)

    product = db.relationship("InvProduct", foreign_keys=[product_id])
