from datetime import datetime, date
from ..extensions import db


class CompanyHoliday(db.Model):
    __tablename__ = "company_holidays"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)
    name = db.Column(db.String(200), nullable=False)
    holiday_date = db.Column(db.Date, nullable=False)
    is_recurring = db.Column(db.Boolean, default=False)
    department = db.Column(db.String(100))
    location = db.Column(db.String(100))
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (db.UniqueConstraint("company_id", "holiday_date",
                                          "department",
                                          name="uq_holiday_date_dept_company"),)


class ApprovalWorkflow(db.Model):
    __tablename__ = "approval_workflows"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)
    name = db.Column(db.String(100), nullable=False)
    module = db.Column(db.String(50), nullable=False)
    leave_type_id = db.Column(db.Integer, db.ForeignKey("leave_types.id"))
    requires_approval = db.Column(db.Boolean, default=True)
    approval_levels = db.Column(db.Integer, default=1)
    auto_approve = db.Column(db.Boolean, default=False)
    auto_approve_role = db.Column(db.String(50))
    notify_admins = db.Column(db.Boolean, default=True)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class OvertimeAccount(db.Model):
    __tablename__ = "overtime_accounts"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    date = db.Column(db.Date, nullable=False)
    regular_hours = db.Column(db.Float, default=0.0)
    overtime_hours = db.Column(db.Float, default=0.0)
    double_overtime = db.Column(db.Float, default=0.0)
    approved = db.Column(db.Boolean, default=False)
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship("User", backref="overtime_accounts")

    __table_args__ = (db.UniqueConstraint("company_id", "user_id", "date",
                                          name="uq_overtime_date_company"),)


class TimePolicy(db.Model):
    __tablename__ = "time_policies"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)
    name = db.Column(db.String(100), nullable=False)
    department = db.Column(db.String(100))
    shift_start = db.Column(db.Time, nullable=False)
    shift_end = db.Column(db.Time, nullable=False)
    grace_period_minutes = db.Column(db.Integer, default=15)
    max_regular_hours = db.Column(db.Float, default=8.0)
    max_overtime_hours = db.Column(db.Float, default=4.0)
    max_consecutive_days = db.Column(db.Integer, default=6)
    require_break = db.Column(db.Boolean, default=True)
    min_break_minutes = db.Column(db.Integer, default=30)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class AttendanceCorrection(db.Model):
    __tablename__ = "attendance_corrections"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)
    attendance_id = db.Column(db.Integer, db.ForeignKey("attendance.id"), nullable=False)
    corrected_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    field = db.Column(db.String(50), nullable=False)
    old_value = db.Column(db.String(255))
    new_value = db.Column(db.String(255), nullable=False)
    reason = db.Column(db.Text, nullable=False)
    corrected_at = db.Column(db.DateTime, default=datetime.utcnow)

    attendance = db.relationship("Attendance", backref="corrections")
    corrector = db.relationship("User", foreign_keys=[corrected_by])


class BreakLog(db.Model):
    __tablename__ = "break_logs"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)
    attendance_id = db.Column(db.Integer, db.ForeignKey("attendance.id"), nullable=False)
    break_start = db.Column(db.DateTime, nullable=False)
    break_end = db.Column(db.DateTime)
    break_type = db.Column(db.String(30), default="lunch")
    duration_minutes = db.Column(db.Integer)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    attendance = db.relationship("Attendance", backref="breaks")


class PayrollAuditLog(db.Model):
    __tablename__ = "payroll_audit_logs"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)
    payroll_run_id = db.Column(db.Integer, db.ForeignKey("payroll_runs.id"))
    action = db.Column(db.String(50), nullable=False)
    performed_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    details = db.Column(db.Text)
    ip_address = db.Column(db.String(50))
    performed_at = db.Column(db.DateTime, default=datetime.utcnow)

    payroll_run = db.relationship("PayrollRun", backref="audit_logs")
    performer = db.relationship("User", foreign_keys=[performed_by])


class ButtonPermission(db.Model):
    __tablename__ = "button_permissions"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)
    role_id = db.Column(db.Integer, db.ForeignKey("roles.id"), nullable=False)
    button_code = db.Column(db.String(100), nullable=False)
    is_granted = db.Column(db.Boolean, default=False)
    module = db.Column(db.String(50), nullable=False)
    description = db.Column(db.String(200))

    role = db.relationship("Role", backref="button_permissions")

    __table_args__ = (db.UniqueConstraint("company_id", "role_id",
                                          "button_code",
                                          name="uq_button_perm_company"),)


class PFProfitDistribution(db.Model):
    __tablename__ = "pf_profit_distributions"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)
    month = db.Column(db.Integer, nullable=False)
    year = db.Column(db.Integer, nullable=False)
    total_profit = db.Column(db.Float, nullable=False)
    distributed_at = db.Column(db.DateTime, default=datetime.utcnow)
    distributed_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    status = db.Column(db.String(20), default="completed")

    distributor = db.relationship("User", foreign_keys=[distributed_by])


class PFSettlement(db.Model):
    __tablename__ = "pf_settlements"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    total_employee_contrib = db.Column(db.Float, default=0.0)
    total_employer_contrib = db.Column(db.Float, default=0.0)
    total_profit_distributed = db.Column(db.Float, default=0.0)
    outstanding_loan = db.Column(db.Float, default=0.0)
    net_settlement = db.Column(db.Float, default=0.0)
    status = db.Column(db.String(20), default="pending")
    settled_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    settled_at = db.Column(db.DateTime)
    pdf_statement = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship("User", foreign_keys=[user_id], backref="pf_settlements")
    settler = db.relationship("User", foreign_keys=[settled_by])
