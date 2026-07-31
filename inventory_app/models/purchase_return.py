from datetime import datetime
from ..extensions import db


class InvPurchaseReturn(db.Model):
    __tablename__ = "inv_purchase_returns"
    __table_args__ = (
        db.UniqueConstraint("company_id", "return_number",
                            name="uq_inv_purchase_returns_return_number"),
    )
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)
    return_number = db.Column(db.String(50), nullable=False)
    original_invoice_id = db.Column(db.Integer, db.ForeignKey("inv_purchase_invoices.id"), nullable=False)
    supplier_id = db.Column(db.Integer, db.ForeignKey("inv_suppliers.id"), nullable=False)
    return_date = db.Column(db.DateTime, default=datetime.utcnow)
    notes = db.Column(db.Text)

    gross_return_value = db.Column(db.Float, default=0)
    total_discount = db.Column(db.Float, default=0)
    total_expenses = db.Column(db.Float, default=0)
    total_tax = db.Column(db.Float, default=0)
    net_return_amount = db.Column(db.Float, default=0)

    reverse_expenses = db.Column(db.Boolean, default=True)

    status = db.Column(db.String(20), default="new")
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    approved_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    approved_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    original_invoice = db.relationship("InvPurchaseInvoice", foreign_keys=[original_invoice_id])
    supplier = db.relationship("InvSupplier", foreign_keys=[supplier_id])
    creator = db.relationship("User", foreign_keys=[created_by])
    approver = db.relationship("User", foreign_keys=[approved_by])
    items = db.relationship("InvPurchaseReturnItem", backref="return_doc",
                            lazy="dynamic", cascade="all, delete-orphan")


class InvPurchaseReturnItem(db.Model):
    __tablename__ = "inv_purchase_return_items"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)
    return_id = db.Column(db.Integer, db.ForeignKey("inv_purchase_returns.id"), nullable=False)
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
    commission = db.Column(db.Float, default=0)
    freight = db.Column(db.Float, default=0)
    loading_unloading = db.Column(db.Float, default=0)
    sales_tax_pct = db.Column(db.Float, default=0)
    withholding_tax_pct = db.Column(db.Float, default=0)

    total_before_discount = db.Column(db.Float, default=0)
    total_after_discount = db.Column(db.Float, default=0)
    proportional_discount = db.Column(db.Float, default=0)
    proportional_sales_tax = db.Column(db.Float, default=0)
    proportional_withholding_tax = db.Column(db.Float, default=0)
    proportional_commission = db.Column(db.Float, default=0)
    proportional_freight = db.Column(db.Float, default=0)
    proportional_loading = db.Column(db.Float, default=0)
    net_return_value = db.Column(db.Float, default=0)

    product = db.relationship("InvProduct", foreign_keys=[product_id])
