from shared.extensions import db


class InvoiceSettings(db.Model):
    __tablename__ = "invoice_settings"
    id = db.Column(db.Integer, primary_key=True)
    default_sales_tax_pct = db.Column(db.Float, default=0)
    default_further_tax_pct = db.Column(db.Float, default=0)
    default_withholding_tax_pct = db.Column(db.Float, default=0)
    default_discount_pct = db.Column(db.Float, default=0)
    default_charges_mode = db.Column(db.String(20), default="general")
    default_discount_mode = db.Column(db.String(20), default="general")
    default_tax_mode = db.Column(db.String(20), default="general")
    show_discount_column = db.Column(db.Boolean, default=True)
    show_charges_column = db.Column(db.Boolean, default=True)
    show_tax_column = db.Column(db.Boolean, default=True)
    auto_add_line = db.Column(db.Boolean, default=True)
    require_approval = db.Column(db.Boolean, default=True)
    allow_partial_payment = db.Column(db.Boolean, default=True)
    default_party_mode = db.Column(db.String(10), default="relevant")

    # §11.2 — tax application defaults, tolerance, withholding base
    over_invoice_tolerance_pct = db.Column(db.Float, default=0)
    withholding_base = db.Column(db.String(10), default="taxable")  # taxable | gross

    # §11.3 — form-field visibility (hiding a control forces its behaviour)
    show_further_tax = db.Column(db.Boolean, default=True)
    show_withholding_tax = db.Column(db.Boolean, default=True)
    show_transport_block = db.Column(db.Boolean, default=True)
    create_from_orders_enabled = db.Column(db.Boolean, default=True)
    per_line_discount_enabled = db.Column(db.Boolean, default=True)
    per_line_tax_enabled = db.Column(db.Boolean, default=True)

    @classmethod
    def get(cls):
        s = cls.query.first()
        if not s:
            s = cls()
            db.session.add(s)
            db.session.commit()
        return s
