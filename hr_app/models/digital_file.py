from datetime import datetime, date
from ..extensions import db


class FileCategory(db.Model):
    __tablename__ = "file_categories"
    __table_args__ = (
        db.UniqueConstraint("company_id", "name",
                            name="uq_file_categories_name"),
    )
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)
    name = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text)


class DigitalFile(db.Model):
    __tablename__ = "digital_files"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    category_id = db.Column(db.Integer, db.ForeignKey("file_categories.id"))
    title = db.Column(db.String(200), nullable=False)
    filename = db.Column(db.String(255), nullable=False)
    original_name = db.Column(db.String(255), nullable=False)
    file_size = db.Column(db.Integer)
    mime_type = db.Column(db.String(100))
    notes = db.Column(db.Text)
    expiry_date = db.Column(db.Date)
    is_verified = db.Column(db.Boolean, default=False)
    verified_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    verified_at = db.Column(db.DateTime)
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship("User", foreign_keys=[user_id], backref="digital_files")
    category = db.relationship("FileCategory")
    verifier = db.relationship("User", foreign_keys=[verified_by])
