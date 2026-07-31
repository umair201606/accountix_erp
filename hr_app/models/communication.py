from datetime import datetime
from ..extensions import db


class Notification(db.Model):
    __tablename__ = "notifications"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)
    title = db.Column(db.String(200), nullable=False)
    message = db.Column(db.Text, nullable=False)
    notification_type = db.Column(db.String(50), default="info")
    module = db.Column(db.String(50))
    reference_id = db.Column(db.Integer)
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    creator = db.relationship("User", backref="created_notifications")
    recipients = db.relationship("NotificationRecipient", backref="notification", lazy="dynamic", cascade="all, delete-orphan")


class NotificationRecipient(db.Model):
    __tablename__ = "notification_recipients"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)
    notification_id = db.Column(db.Integer, db.ForeignKey("notifications.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    is_read = db.Column(db.Boolean, default=False)
    read_at = db.Column(db.DateTime)

    user = db.relationship("User")


class EmailLog(db.Model):
    __tablename__ = "email_logs"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)
    recipient = db.Column(db.String(120), nullable=False)
    subject = db.Column(db.String(200), nullable=False)
    body = db.Column(db.Text)
    module = db.Column(db.String(50))
    status = db.Column(db.String(20), default="sent")
    error_message = db.Column(db.Text)
    sent_at = db.Column(db.DateTime, default=datetime.utcnow)
