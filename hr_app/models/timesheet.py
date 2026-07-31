from datetime import datetime, date
from ..extensions import db


class TimesheetWeek(db.Model):
    __tablename__ = "timesheet_weeks"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    week_start = db.Column(db.Date, nullable=False)
    week_end = db.Column(db.Date, nullable=False)
    status = db.Column(db.String(20), default="unapproved")
    total_hours = db.Column(db.Float, default=0.0)
    submitted_at = db.Column(db.DateTime)
    approved_at = db.Column(db.DateTime)
    approved_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship("User", foreign_keys=[user_id], backref="timesheet_weeks")
    approver = db.relationship("User", foreign_keys=[approved_by])
    entries = db.relationship("TimesheetEntry", backref="week", lazy="dynamic", cascade="all, delete-orphan")

    __table_args__ = (db.UniqueConstraint("company_id", "user_id",
                                          "week_start",
                                          name="uq_ts_week_company"),)


class TimesheetEntry(db.Model):
    __tablename__ = "timesheet_entries"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)
    week_id = db.Column(db.Integer, db.ForeignKey("timesheet_weeks.id"), nullable=False)
    day = db.Column(db.Date, nullable=False)
    project = db.Column(db.String(200))
    task = db.Column(db.String(200), nullable=False)
    hours = db.Column(db.Float, nullable=False)
    description = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class TimesheetApproval(db.Model):
    __tablename__ = "timesheet_approvals"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)
    week_id = db.Column(db.Integer, db.ForeignKey("timesheet_weeks.id"), nullable=False)
    approver_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    status = db.Column(db.String(20), default="pending")
    comment = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    approver = db.relationship("User")
