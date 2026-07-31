from datetime import datetime
from ..extensions import db


class InvInvoice(db.Model):
    __tablename__ = "inv_invoices"
    __table_args__ = (
        db.UniqueConstraint("company_id", "invoice_number",
                            name="uq_inv_invoices_invoice_number"),
        db.UniqueConstraint("company_id", "voucher_number",
                            name="uq_inv_invoices_voucher_number"),
    )
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)
    invoice_number = db.Column(db.String(50), nullable=False)
    voucher_number = db.Column(db.String(50), nullable=False)
    sales_order_id = db.Column(db.Integer, db.ForeignKey("inv_sales_orders.id"))
    customer_id = db.Column(db.Integer, db.ForeignKey("inv_customers.id"), nullable=False)
    # Set when settings allow picking an arbitrary ledger account as the
    # counterparty; the AR posting then debits this instead of the customer.
    party_account_id = db.Column(db.Integer, db.ForeignKey("chart_of_accounts.id"))
    invoice_date = db.Column(db.DateTime, default=datetime.utcnow)
    due_date = db.Column(db.DateTime)
    voucher_status = db.Column(db.String(20), default="unapproved")
    payment_status = db.Column(db.String(20), default="unpaid")

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
    paid_amount = db.Column(db.Float, default=0)

    notes = db.Column(db.Text)
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    approved_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    approved_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    creator = db.relationship("User", foreign_keys=[created_by], backref="created_invoices")
    approver = db.relationship("User", foreign_keys=[approved_by])
    items = db.relationship("InvInvoiceItem", backref="invoice",
                            lazy="dynamic", cascade="all, delete-orphan")
    @property
    def charges_list(self):
        from .additional_charge import AdditionalCharge
        return AdditionalCharge.query.filter_by(doc_type="SI", doc_id=self.id).all()


class InvInvoiceItem(db.Model):
    __tablename__ = "inv_invoice_items"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)
    invoice_id = db.Column(db.Integer, db.ForeignKey("inv_invoices.id"), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey("inv_products.id"))
    description = db.Column(db.String(300))
    quantity = db.Column(db.Float, default=1)
    unit = db.Column(db.String(20), default="pcs")
    unit_price = db.Column(db.Float, default=0)

    # Per-line source order (§4.3). One invoice can draw on several orders, so
    # the link belongs on the line, not the header — this is what the Order ref
    # column shows and what the §4.4 write-back credits.
    source_order_id = db.Column(db.Integer, db.ForeignKey("inv_sales_orders.id"))
    source_order_item_id = db.Column(db.Integer, db.ForeignKey("inv_sales_order_items.id"))
    source_order_number = db.Column(db.String(50), default="")

    discount_pct = db.Column(db.Float, default=0)
    discount_amount = db.Column(db.Float, default=0)
    delivery = db.Column(db.Float, default=0)
    installation = db.Column(db.Float, default=0)
    sales_tax_pct = db.Column(db.Float, default=0)

    total_before_discount = db.Column(db.Float, default=0)
    total_after_discount = db.Column(db.Float, default=0)

    comments = db.Column(db.Text)

    product = db.relationship("InvProduct", backref="invoice_items")
