from datetime import datetime
from shared.extensions import db


class ChangeRequest(db.Model):
    __tablename__ = "change_requests"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    field_name = db.Column(db.String(100), nullable=False)
    old_value = db.Column(db.Text)
    new_value = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(20), default="pending")
    reviewed_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    review_notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    reviewed_at = db.Column(db.DateTime)

    user = db.relationship("User", foreign_keys=[user_id], backref="change_requests")
    reviewer = db.relationship("User", foreign_keys=[reviewed_by])
