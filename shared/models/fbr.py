from datetime import datetime
from shared.extensions import db


class FBRConfig(db.Model):
    __tablename__ = "fbr_config"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)
    environment = db.Column(db.String(20), default="sandbox")
    access_token = db.Column(db.String(500), default="")
    token_expiry = db.Column(db.DateTime)

    seller_ntn = db.Column(db.String(50), default="")
    seller_strn = db.Column(db.String(50), default="")
    seller_cnic = db.Column(db.String(50), default="")
    seller_name = db.Column(db.String(200), default="")
    seller_business_name = db.Column(db.String(200), default="")
    seller_address = db.Column(db.Text, default="")
    seller_phone = db.Column(db.String(50), default="")
    seller_email = db.Column(db.String(200), default="")

    is_active = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    @classmethod
    def get(cls):
        c = cls.query.first()
        if not c:
            c = cls()
            db.session.add(c)
            db.session.commit()
        return c

    def is_sandbox(self):
        return self.environment == "sandbox"

    def base_url(self):
        if self.is_sandbox():
            return "https://sandbox.fbr.gov.pk/di/api"
        return "https://di.fbr.gov.pk/di/api"

    def __repr__(self):
        return f"<FBRConfig {'sandbox' if self.is_sandbox() else 'live'}>"


class FBRInvoiceRecord(db.Model):
    __tablename__ = "fbr_invoice_records"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)
    inv_invoice_id = db.Column(db.Integer, db.ForeignKey("inv_invoices.id"), nullable=False)

    fbr_invoice_number = db.Column(db.String(100), default="")
    fbr_submission_id = db.Column(db.String(100), default="")
    status = db.Column(db.String(20), default="pending")

    request_json = db.Column(db.Text, default="")
    response_json = db.Column(db.Text, default="")
    error_message = db.Column(db.Text, default="")

    invoice_type = db.Column(db.String(10), default="S")
    attempt_count = db.Column(db.Integer, default=0)

    submitted_at = db.Column(db.DateTime)
    submitted_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    invoice = db.relationship("InvInvoice", backref=db.backref("fbr_records", lazy="dynamic"))
    submitter = db.relationship("User", foreign_keys=[submitted_by])

    @classmethod
    def latest_for_invoice(cls, inv_invoice_id):
        return cls.query.filter_by(inv_invoice_id=inv_invoice_id)\
            .order_by(cls.id.desc()).first()
