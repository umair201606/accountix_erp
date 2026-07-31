from datetime import datetime, date
from ..extensions import db


class LeaveType(db.Model):
    __tablename__ = "leave_types"
    __table_args__ = (
        db.UniqueConstraint("company_id", "name",
                            name="uq_leave_types_name"),
        db.UniqueConstraint("company_id", "code",
                            name="uq_leave_types_code"),
    )
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)
    name = db.Column(db.String(50), nullable=False)
    code = db.Column(db.String(10), nullable=False)
    description = db.Column(db.Text)
    default_quota = db.Column(db.Integer, default=0)
    is_paid = db.Column(db.Boolean, default=True)
    requires_approval = db.Column(db.Boolean, default=True)
    carry_forward = db.Column(db.Boolean, default=False)
    max_carry_forward = db.Column(db.Integer, default=0)


class LeaveQuota(db.Model):
    __tablename__ = "leave_quotas"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    leave_type_id = db.Column(db.Integer, db.ForeignKey("leave_types.id"), nullable=False)
    year = db.Column(db.Integer, nullable=False)
    total = db.Column(db.Integer, default=0)
    used = db.Column(db.Integer, default=0)
    pending = db.Column(db.Integer, default=0)
    remaining = db.Column(db.Integer, default=0)

    user = db.relationship("User", backref="leave_quotas")
    leave_type = db.relationship("LeaveType")

    __table_args__ = (
        db.UniqueConstraint("company_id", "user_id", "leave_type_id", "year",
                            name="uq_leave_quota_company"),
    )


class LeaveRequest(db.Model):
    __tablename__ = "leave_requests"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    leave_type_id = db.Column(db.Integer, db.ForeignKey("leave_types.id"), nullable=False)
    start_date = db.Column(db.Date, nullable=False)
    end_date = db.Column(db.Date, nullable=False)
    total_days = db.Column(db.Float, nullable=False)
    reason = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(20), default="pending")
    is_half_day = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = db.relationship("User", foreign_keys=[user_id], backref="leave_requests")
    leave_type = db.relationship("LeaveType")
    approvals = db.relationship("LeaveApproval", backref="leave_request", lazy="dynamic")


class LeaveApproval(db.Model):
    __tablename__ = "leave_approvals"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)
    leave_request_id = db.Column(db.Integer, db.ForeignKey("leave_requests.id"), nullable=False)
    approver_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    level = db.Column(db.Integer, default=1)
    status = db.Column(db.String(20), default="pending")
    comment = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    approver = db.relationship("User")
