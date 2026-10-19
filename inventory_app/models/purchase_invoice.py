from datetime import datetime
from ..extensions import db
from shared.models.money import Money


class InvPurchaseInvoice(db.Model):
    __tablename__ = "inv_purchase_invoices"
    __table_args__ = (
        db.UniqueConstraint("company_id", "invoice_number",
                            name="uq_inv_purchase_invoices_invoice_number"),
        db.UniqueConstraint("company_id", "voucher_number",
                            name="uq_inv_purchase_invoices_voucher_number"),
    )
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)
    invoice_number = db.Column(db.String(50), nullable=False)
    voucher_number = db.Column(db.String(50), nullable=False)
    invoice_date = db.Column(db.DateTime, default=datetime.utcnow)
    supplier_id = db.Column(db.Integer, db.ForeignKey("inv_suppliers.id"), nullable=False)
    # Set when settings allow picking an arbitrary ledger account as the
    # counterparty; the AP posting then credits this instead of the supplier.
    party_account_id = db.Column(db.Integer, db.ForeignKey("chart_of_accounts.id"))
    # The PARTY label tags the AP line at posting (the supplier's ledger
    # line). Item labels live per line on InvPurchaseInvoiceItem and fall
    # back to the party label, then the company default, when left unpicked.
    label_id = db.Column(db.Integer, db.ForeignKey("project_labels.id"))
    driver_name = db.Column(db.String(100))
    driver_contact = db.Column(db.String(50))
    vehicle_number = db.Column(db.String(50))
    gate_pass = db.Column(db.String(50))

    discount_mode = db.Column(db.String(20), default="general")
    expenses_mode = db.Column(db.String(20), default="general")
    tax_mode = db.Column(db.String(20), default="general")

    global_discount_pct = db.Column(Money, default=0)
    global_discount_value = db.Column(Money, default=0)
    global_commission = db.Column(Money, default=0)
    global_freight = db.Column(Money, default=0)
    global_loading = db.Column(Money, default=0)
    global_sales_tax_pct = db.Column(Money, default=0)
    global_withholding_tax_pct = db.Column(Money, default=0)
    # Further tax exists on sales only (§8); the column is kept so old rows and
    # the shared mapper still load, but a purchase never sets it.
    further_tax_pct = db.Column(Money, default=0)
    apply_further_tax = db.Column(db.Boolean, default=False)
    apply_withholding_tax = db.Column(db.Boolean, default=False)

    subtotal = db.Column(Money, default=0)
    total_discount = db.Column(Money, default=0)
    total_expenses = db.Column(Money, default=0)
    total_tax = db.Column(Money, default=0)
    total_further_tax = db.Column(Money, default=0)
    total_withholding_tax = db.Column(Money, default=0)
    net_payable = db.Column(Money, default=0)
    total_amount = db.Column(Money, default=0)
    paid_amount = db.Column(Money, default=0)
    payment_status = db.Column(db.String(20), default="unpaid")
    purchase_order_id = db.Column(db.Integer, db.ForeignKey("inv_purchase_orders.id"))

    notes = db.Column(db.Text)
    status = db.Column(db.String(20), default="new")
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    approved_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    approved_at = db.Column(db.DateTime)
    # Bumped on every save and state change. The form sends the version it
    # loaded; a save from a stale copy is refused instead of silently
    # overwriting another user's edit (shared/concurrency.py).
    edit_version = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    supplier = db.relationship("InvSupplier", backref="purchase_invoices")
    creator = db.relationship("User", foreign_keys=[created_by], backref="created_purchase_invoices")
    approver = db.relationship("User", foreign_keys=[approved_by])
    items = db.relationship("InvPurchaseInvoiceItem", backref="invoice",
                            lazy="dynamic", cascade="all, delete-orphan")

    @property
    def charges_list(self):
        from .additional_charge import AdditionalCharge
        return AdditionalCharge.query.filter_by(doc_type="PI", doc_id=self.id).all()


class InvPurchaseInvoiceItem(db.Model):
    __tablename__ = "inv_purchase_invoice_items"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)
    invoice_id = db.Column(db.Integer, db.ForeignKey("inv_purchase_invoices.id"), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey("inv_products.id"))
    description = db.Column(db.String(300))
    quantity = db.Column(Money, default=1)
    unit = db.Column(db.String(20), default="pcs")
    unit_price = db.Column(Money, default=0)

    # Per-line project label. Falls back to the party label, then the company
    # default, when saved without a pick. The first item's label governs the
    # pooled economic lines (inventory/charges) at posting.
    label_id = db.Column(db.Integer, db.ForeignKey("project_labels.id"))

    # Per-line source purchase order (§4.3) — see the sales-side note.
    source_order_id = db.Column(db.Integer, db.ForeignKey("inv_purchase_orders.id"))
    source_order_item_id = db.Column(db.Integer, db.ForeignKey("inv_purchase_order_items.id"))
    source_order_number = db.Column(db.String(50), default="")

    discount_pct = db.Column(Money, default=0)
    discount_amount = db.Column(Money, default=0)
    commission = db.Column(Money, default=0)
    freight = db.Column(Money, default=0)
    loading_unloading = db.Column(Money, default=0)
    sales_tax_pct = db.Column(Money, default=0)
    withholding_tax_pct = db.Column(Money, default=0)

    total_before_discount = db.Column(Money, default=0)
    total_after_discount = db.Column(Money, default=0)

    comments = db.Column(db.Text)

    product = db.relationship("InvProduct", backref="purchase_invoice_items")
