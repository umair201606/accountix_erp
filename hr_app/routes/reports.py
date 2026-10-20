from datetime import datetime, date, timedelta
from flask import Blueprint, render_template, request, jsonify
from flask_login import login_required, current_user
from sqlalchemy import func, extract, case
from sqlalchemy import text
from ..extensions import db
from shared.tenancy import get_member
from ..models.user import User
from shared import report_export as rx
from ..models.attendance import Attendance
from ..models.leave import LeaveRequest, LeaveType, LeaveQuota
from ..models.timesheet import TimesheetWeek, TimesheetEntry
from ..models.compensation import PayrollRun, PayrollSlip
from ..models.pf import PFLedger, PFContribution
from ..models.holiday import OvertimeAccount
from ..models.loan import LoanAdvanceRequest, LoanRepayment

reports_bp = Blueprint("reports", __name__, url_prefix="/reports")


def _require_admin():
    if not current_user.is_admin() and not current_user.is_manager():
        return False
    return True


def _visible_ids():
    """None for an admin (the whole company); for a manager, the ids of
    their direct reports here. Managers used to run every report over the
    whole company — every employee's PF and loan balance, every leave
    reason — when they manage a team."""
    if current_user.is_admin():
        return None
    return {u.id for u in current_user.reports_of()}


def _get_scope_users(scope, user_id=None, department=None):
    if scope == "individual" and user_id:
        u = get_member(user_id)
        users = [u] if u is not None else []
    elif scope == "department" and department:
        # Department is per company (on the membership User.employees joins).
        from shared.models.company import CompanyMembership
        users = User.employees().filter(CompanyMembership.department == department,
                                        User.is_active.is_(True)).all()
    else:
        users = User.employees().filter(User.is_active.is_(True)).all()
    visible = _visible_ids()
    return users if visible is None else [u for u in users if u.id in visible]


# Report columns that read another section's data need that section's view
# right (HR staff only; an admin has every right).
COLUMN_RIGHTS = {"pf_balance": "pf", "loan_balance": "loans",
                 "leaves": "leaves", "sick_days": "leaves"}


@reports_bp.route("/")
@login_required
def index():
    if not _require_admin():
        return render_template("dashboard/index.html")
    employees = _get_scope_users("company")
    return render_template("reports/index.html", employees=employees,
                           today=date.today())


@reports_bp.route("/dashboard")
@login_required
def dashboard():
    today = date.today()
    if current_user.is_admin() or current_user.is_manager():
        total_employees = User.employees().filter(User.is_active.is_(True)).count()
        today_present = Attendance.query.filter(Attendance.date == today, Attendance.clock_in != None).count()
        pending_leaves = LeaveRequest.query.filter(LeaveRequest.status == "pending").count()
        running_payroll = PayrollRun.query.filter(
            PayrollRun.status == "unapproved", extract("month", PayrollRun.run_date) == today.month
        ).first()
        return jsonify({
            "total_employees": total_employees,
            "today_present": today_present,
            "today_absent": total_employees - today_present,
            "pending_leaves": pending_leaves,
            "active_payroll": running_payroll is not None,
        })
    my_att = Attendance.query.filter(Attendance.user_id == current_user.id, Attendance.date == today).first()
    my_pending = LeaveRequest.query.filter(
        LeaveRequest.user_id == current_user.id, LeaveRequest.status == "pending"
    ).count()
    return jsonify({
        "total_employees": "-",
        "today_present": "Clocked In" if my_att and my_att.clock_in and not my_att.clock_out else "Done" if my_att and my_att.clock_out else "Not Clocked",
        "today_absent": "-",
        "pending_leaves": my_pending,
        "active_payroll": False,
    })


@reports_bp.route("/attendance-chart")
@login_required
def attendance_chart():
    if not _require_admin():
        return jsonify({"error": "Access denied"}), 403
    year = request.args.get("year", datetime.now().year, type=int)
    monthly = db.session.query(
        extract("month", Attendance.date).label("month"),
        func.count(Attendance.id).label("total"),
        func.sum(case((Attendance.is_late == True, 1), else_=0)).label("late"),
        func.sum(case((Attendance.is_half_day == True, 1), else_=0)).label("half"),
    ).filter(extract("year", Attendance.date) == year).group_by("month").order_by("month").all()
    return jsonify([{
        "month": int(r.month), "total": int(r.total),
        "late": int(r.late), "half": int(r.half),
    } for r in monthly])


@reports_bp.route("/query", methods=["POST"])
@login_required
def query():
    if not _require_admin():
        return jsonify({"error": "Access denied"}), 403
    data = request.get_json()
    scope = data.get("scope", "company")
    user_id = data.get("user_id")
    if user_id is not None:
        user_id = int(user_id)
    department = data.get("department")
    date_from = datetime.strptime(data["date_from"], "%Y-%m-%d").date() if data.get("date_from") else None
    date_to = datetime.strptime(data["date_to"], "%Y-%m-%d").date() if data.get("date_to") else None
    columns = data.get("columns", ["employee", "total_hours", "leaves", "sick_days"])
    for c in columns:
        section = COLUMN_RIGHTS.get(c)
        if section and not current_user.can(section, "view"):
            return jsonify({"error": f"You do not have the {section.upper() if section == 'pf' else section.title()} "
                                     f"view right needed for that column."}), 403
    if scope == "individual" and user_id:
        if get_member(user_id) is None:
            return jsonify({"error": "That person is not a member of this company."}), 404
        visible = _visible_ids()
        if visible is not None and user_id not in visible:
            return jsonify({"error": "Not authorized for this employee"}), 403
    users = _get_scope_users(scope, user_id, department)
    results = []
    for u in users:
        row = {"employee": u.full_name, "department": u.department, "id": u.id}
        base = Attendance.query.filter(Attendance.user_id == u.id)
        if date_from:
            base = base.filter(Attendance.date >= date_from)
        if date_to:
            base = base.filter(Attendance.date <= date_to)
        if "total_hours" in columns:
            if db.engine.name == "sqlite":
                hours_expr = func.julianday(Attendance.clock_out) - func.julianday(Attendance.clock_in)
            else:
                hours_expr = func.extract("epoch", Attendance.clock_out - Attendance.clock_in) / 3600.0
            totals = base.with_entities(
                func.sum(case((Attendance.clock_out != None, hours_expr), else_=0))
            ).scalar() or 0
            row["total_hours"] = round(float(totals) * 24, 2)
        if "overtime" in columns:
            ot = db.session.query(func.sum(OvertimeAccount.overtime_hours)).filter(
                OvertimeAccount.user_id == u.id
            ).scalar() or 0
            row["overtime"] = round(float(ot), 2)
        if "leaves" in columns:
            lq = LeaveRequest.query.filter(LeaveRequest.user_id == u.id, LeaveRequest.status == "approved")
            if date_from:
                lq = lq.filter(LeaveRequest.start_date >= date_from)
            if date_to:
                lq = lq.filter(LeaveRequest.end_date <= date_to)
            row["leaves"] = sum(l.total_days for l in lq.all())
        if "sick_days" in columns:
            sq = LeaveRequest.query.join(LeaveType).filter(
                LeaveRequest.user_id == u.id, LeaveRequest.status == "approved",
                LeaveType.code == "SL"
            )
            if date_from:
                sq = sq.filter(LeaveRequest.start_date >= date_from)
            if date_to:
                sq = sq.filter(LeaveRequest.end_date <= date_to)
            row["sick_days"] = sum(l.total_days for l in sq.all())
        if "late_days" in columns:
            late = base.filter(Attendance.is_late == True).count()
            row["late_days"] = late
        if "present_days" in columns:
            present = base.filter(Attendance.clock_in != None).count()
            row["present_days"] = present
        if "pf_balance" in columns:
            pf = db.session.query(func.sum(PFLedger.credit) - func.sum(PFLedger.debit)).filter(
                PFLedger.user_id == u.id
            ).scalar() or 0
            row["pf_balance"] = round(float(pf), 2)
        if "loan_balance" in columns:
            loan = db.session.query(func.sum(LoanAdvanceRequest.remaining_amount)).filter(
                LoanAdvanceRequest.user_id == u.id, LoanAdvanceRequest.status == "approved"
            ).scalar() or 0
            row["loan_balance"] = round(float(loan), 2)
        results.append(row)
    return jsonify({"results": results})


COLUMN_TITLES = {"pf_balance": "PF Balance", "loan_balance": "Loan Balance",
                 "sick_days": "Sick Days", "late_days": "Late Days",
                 "present_days": "Present Days"}


@reports_bp.route("/export-excel", methods=["POST"])
@login_required
def export_excel():
    """The custom report builder's result, as the shared report workbook
    (heading, frozen titles, filters, number formats, print setup)."""
    if not _require_admin():
        return jsonify({"error": "Access denied"}), 403
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "No data supplied for export."}), 400
    rows = data.get("rows", [])
    columns = data.get("columns", [])
    if not columns:
        return jsonify({"error": "No columns to export."}), 400
    fmt = (data.get("format") or "excel").lower()

    def _val(v):
        if isinstance(v, str):
            t = v.strip().replace(",", "")
            try:
                return float(t) if t else v
            except ValueError:
                return v
        return v

    headers = [COLUMN_TITLES.get(c, c.replace("_", " ").title()) for c in columns]
    body = [[_val(r.get(c, "")) for c in columns] for r in rows]
    period = ""
    if data.get("date_from") or data.get("date_to"):
        period = f"{data.get('date_from') or 'start'} to {data.get('date_to') or 'today'}"
    count_cols = {i: "0" for i, c in enumerate(columns)
                  if c.endswith("_days") or c == "leaves"}
    return rx.export_table(fmt if fmt in ("excel", "pdf", "csv") else "excel",
                           "HR Report", headers, body, period=period,
                           filters=[f"{len(body)} employees"], col_formats=count_cols,
                           file_period=date.today())


@reports_bp.route("/export-attendance")
@login_required
def export_attendance():
    if not _require_admin():
        return jsonify({"error": "Access denied"}), 403
    if not current_user.can("attendance", "view"):
        return jsonify({"error": "You do not have the Attendance view right."}), 403
    month = request.args.get("month", datetime.now().month, type=int)
    year = request.args.get("year", datetime.now().year, type=int)
    q = db.session.query(Attendance, User).join(User).filter(
        extract("month", Attendance.date) == month,
        extract("year", Attendance.date) == year,
    )
    visible = _visible_ids()
    if visible is not None:
        q = q.filter(Attendance.user_id.in_(visible or [0]))
    records = q.order_by(Attendance.date).all()
    rows = [[att.date, usr.full_name, usr.department or "",
             att.clock_in.strftime("%H:%M") if att.clock_in else "",
             att.clock_out.strftime("%H:%M") if att.clock_out else "",
             (att.status or "").replace("_", " ").title(),
             "Yes" if att.is_late else "No", "Yes" if att.is_half_day else "No"]
            for att, usr in records]
    late = sum(1 for r in rows if r[6] == "Yes")
    return rx.export_table(request.args.get("format", "csv"), "Attendance Report",
                           ["Date", "Employee", "Department", "Clock In", "Clock Out",
                            "Status", "Late", "Half Day"], rows,
                           period=date(year, month, 1).strftime("%B %Y"),
                           filters=[f"{len(rows)} records", f"{late} late"],
                           file_period=f"{year}-{month:02d}")


@reports_bp.route("/export-leaves")
@login_required
def export_leaves():
    if not _require_admin():
        return jsonify({"error": "Access denied"}), 403
    if not current_user.can("leaves", "view"):
        return jsonify({"error": "You do not have the Leaves view right."}), 403
    year = request.args.get("year", datetime.now().year, type=int)
    q = db.session.query(LeaveRequest, User, LeaveType).join(User).join(LeaveType).filter(
        extract("year", LeaveRequest.start_date) == year
    )
    visible = _visible_ids()
    if visible is not None:
        q = q.filter(LeaveRequest.user_id.in_(visible or [0]))
    records = q.order_by(LeaveRequest.start_date).all()
    rows = [[usr.full_name, lt.name, lr.start_date, lr.end_date,
             float(lr.total_days or 0), (lr.status or "").title(), lr.reason or ""]
            for lr, usr, lt in records]
    approved = sum(r[4] for r in rows if r[5] == "Approved")
    return rx.export_table(request.args.get("format", "csv"), "Leave Report",
                           ["Employee", "Type", "Start", "End", "Days", "Status",
                            "Reason"], rows, period=f"Calendar year {year}",
                           filters=[f"{len(rows)} requests",
                                    f"{approved:g} approved days"],
                           col_formats={4: "0.#"}, file_period=str(year))
