from datetime import datetime, date
from ..extensions import db


class PerformanceReview(db.Model):
    __tablename__ = "performance_reviews"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    reviewer_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    review_period = db.Column(db.String(50), nullable=False)
    overall_score = db.Column(db.Float)
    productivity_rating = db.Column(db.Integer)
    quality_rating = db.Column(db.Integer)
    teamwork_rating = db.Column(db.Integer)
    punctuality_rating = db.Column(db.Integer)
    strengths = db.Column(db.Text)
    improvements = db.Column(db.Text)
    feedback = db.Column(db.Text)
    status = db.Column(db.String(20), default="unapproved")
    completed_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship("User", foreign_keys=[user_id], backref="performance_reviews")
    reviewer = db.relationship("User", foreign_keys=[reviewer_id])


class PerformanceGoal(db.Model):
    __tablename__ = "performance_goals"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text)
    target_date = db.Column(db.Date)
    status = db.Column(db.String(20), default="active")
    progress_pct = db.Column(db.Integer, default=0)
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship("User", foreign_keys=[user_id], backref="performance_goals")
    creator = db.relationship("User", foreign_keys=[created_by])


class TimesheetMergedReport(db.Model):
    __tablename__ = "timesheet_merged_reports"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    period_start = db.Column(db.Date, nullable=False)
    period_end = db.Column(db.Date, nullable=False)
    total_hours = db.Column(db.Float, default=0.0)
    overtime_hours = db.Column(db.Float, default=0.0)
    project_breakdown = db.Column(db.Text)
    generated_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship("User", backref="timesheet_merged_reports")
