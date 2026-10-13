from datetime import datetime
from shared.extensions import db
from shared.models.money import Money


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
    transfer_amount = db.Column(Money, default=0)
    # Units taken out of stock by a capitalisation (stored so a draft can be
    # approved later with the quantity it was raised for).
    quantity = db.Column(Money, default=1)
    description = db.Column(db.Text, default="")
    # Date the transfer happens: its journal and stock movement post here.
    transfer_date = db.Column(db.Date)
    status = db.Column(db.String(20), default="unapproved")
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    approved_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    approved_at = db.Column(db.DateTime, nullable=True)

    # Both transfer lists print t.asset.name; with no relationship it was
    # always undefined and every row read "N/A".
    asset = db.relationship("FixedAsset", foreign_keys=[asset_id], lazy="joined")
