from datetime import datetime
from ..extensions import db


class AdditionalCharge(db.Model):
    __tablename__ = "additional_charges"
    id = db.Column(db.Integer, primary_key=True)
    # Polymorphic owner — any document type (SI, SO, PI, PO)
    doc_type = db.Column(db.String(10), nullable=False)
    doc_id = db.Column(db.Integer, nullable=False)
    charge_account_id = db.Column(db.Integer, db.ForeignKey("chart_of_accounts.id"), nullable=False)
    description = db.Column(db.String(200))
    amount = db.Column(db.Float, default=0)
    scope = db.Column(db.String(20), default="general")  # general (Combined) | individual (Per-line)
    # How the charge flows (v3 spec §6.3): bill = separate billed charge added to
    # net payable; absorb = folded into item/inventory value (carriage inward on
    # purchases); expense = expense-only, not billed, excluded from payable.
    treatment = db.Column(db.String(10), default="bill")
    # Independent tax-base switches (§6.4). Further tax has no switch of its own —
    # it always follows the sales-tax base.
    st_taxable = db.Column(db.Boolean, default=True)    # enters sales-tax + further-tax base
    wht_taxable = db.Column(db.Boolean, default=False)  # enters withholding base
    extra_taxable = db.Column(db.Boolean, default=False)
    # Legacy columns kept so old rows still load; st_taxable supersedes `taxable`.
    taxable = db.Column(db.Boolean, default=True)
    tax_base = db.Column(db.String(30), default="after_discount")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    charge_account = db.relationship("ChartOfAccount")
