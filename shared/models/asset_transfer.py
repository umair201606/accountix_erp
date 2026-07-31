from datetime import datetime
from shared.extensions import db


class AssetTransfer(db.Model):
    __tablename__ = "asset_transfers"
    __table_args__ = (
        db.UniqueConstraint("company_id", "voucher_number",
                            name="uq_asset_transfers_voucher_number"),
    )
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)
    voucher_number = db.Column(db.String(50), nullable=False)
    direction = db.Column(db.String(20), nullable=False)
    asset_id = db.Column(db.Integer, db.ForeignKey("fixed_assets.id"), nullable=False)
    product_id = db.Column(db.Integer, nullable=True)
    new_product_name = db.Column(db.String(200), default="")
    source_product_id = db.Column(db.Integer, nullable=True)
    transfer_amount = db.Column(db.Float, default=0)
    description = db.Column(db.Text, default="")
    status = db.Column(db.String(20), default="unapproved")
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    approved_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    approved_at = db.Column(db.DateTime, nullable=True)
