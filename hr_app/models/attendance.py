from datetime import datetime, date, time
from ..extensions import db


class Attendance(db.Model):
    __tablename__ = "attendance"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    date = db.Column(db.Date, nullable=False, default=date.today)
    clock_in = db.Column(db.DateTime)
    clock_out = db.Column(db.DateTime)
    ip_address = db.Column(db.String(50))
    geo_lat = db.Column(db.Float)
    geo_lng = db.Column(db.Float)
    status = db.Column(db.String(20), default="present")
    is_late = db.Column(db.Boolean, default=False)
    is_half_day = db.Column(db.Boolean, default=False)
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship("User", backref="attendances")

    __table_args__ = (db.UniqueConstraint("company_id", "user_id", "date",
                                          name="uq_attendance_company_date"),)


class AttendanceLog(db.Model):
    __tablename__ = "attendance_logs"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)
    attendance_id = db.Column(db.Integer, db.ForeignKey("attendance.id"), nullable=False)
    event = db.Column(db.String(20), nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    ip_address = db.Column(db.String(50))
    geo_lat = db.Column(db.Float)
    geo_lng = db.Column(db.Float)

    attendance = db.relationship("Attendance", backref="logs")
