from datetime import datetime, date, time, timedelta
from flask import Blueprint, render_template, request, jsonify, flash, redirect, url_for
from flask_login import login_required, current_user
from sqlalchemy import func, extract
from ..extensions import db, csrf
from shared.tenancy import scoped_get_404
from ..models.attendance import Attendance, AttendanceLog
from ..models.holiday import BreakLog, TimePolicy, AttendanceCorrection, OvertimeAccount
from ..models.communication import Notification, NotificationRecipient

attendance_bp = Blueprint("attendance", __name__, url_prefix="/attendance")


def _get_policy(user):
    policy = TimePolicy.query.filter_by(department=user.department, is_active=True).first()
    if not policy:
        policy = TimePolicy.query.filter_by(is_active=True).first()
    return policy or TimePolicy(
        shift_start=time(9, 0), shift_end=time(18, 0),
        grace_period_minutes=15, max_regular_hours=8,
        max_overtime_hours=4, max_consecutive_days=6
    )


def _check_consecutive_days(user_id, max_days):
    recent = Attendance.query.filter_by(user_id=user_id).order_by(Attendance.date.desc()).limit(max_days + 1).all()
    if len(recent) < max_days + 1:
        return False
    for i in range(max_days):
        expected = recent[0].date - timedelta(days=i)
        if not any(a.date == expected for a in recent):
            return False
    return True


def _check_policy_violations(user_id, policy):
    violations = []
    if _check_consecutive_days(user_id, policy.max_consecutive_days):
        violations.append(f"Working {policy.max_consecutive_days}+ consecutive days")
    return violations


@attendance_bp.route("/")
@login_required
def index():
    today = date.today()
    records = Attendance.query.filter_by(user_id=current_user.id).order_by(Attendance.date.desc()).limit(30).all()
    today_att = Attendance.query.filter_by(user_id=current_user.id, date=today).first()
    active_break = None
    if today_att and today_att.clock_in and not today_att.clock_out:
        active_break = BreakLog.query.filter_by(attendance_id=today_att.id, break_end=None).first()
    stats = db.session.query(
        func.count(Attendance.id).label("total"),
        func.sum(db.cast(Attendance.is_late, db.Integer)).label("late_days"),
        func.sum(db.cast(Attendance.is_half_day, db.Integer)).label("half_days"),
    ).filter(Attendance.user_id == current_user.id).first()
    overtime = db.session.query(func.sum(OvertimeAccount.overtime_hours)).filter(
        OvertimeAccount.user_id == current_user.id).scalar() or 0
    policy = _get_policy(current_user)
    violations = _check_policy_violations(current_user.id, policy)
    return render_template("attendance/index.html", records=records, today_att=today_att,
                           active_break=active_break, stats=stats, overtime=overtime,
                           policy=policy, violations=violations)


@attendance_bp.route("/clock-in", methods=["POST"])
@csrf.exempt
@login_required
def clock_in():
    today = date.today()
    existing = Attendance.query.filter_by(user_id=current_user.id, date=today).first()
    if existing and existing.clock_in:
        return jsonify({"error": "Already clocked in today"}), 400
    now = datetime.now()
    policy = _get_policy(current_user)
    shift_start_dt = datetime.combine(today, policy.shift_start)
    shift_end_dt = datetime.combine(today, policy.shift_end)
    grace_end = shift_start_dt + timedelta(minutes=policy.grace_period_minutes)
    is_late = now > grace_end
    is_half_day = now > datetime.combine(today, time(13, 0))
    att = Attendance(
        user_id=current_user.id, date=today, clock_in=now,
        ip_address=request.remote_addr,
        status="present" if not is_half_day else "half-day",
        is_late=is_late, is_half_day=is_half_day,
    )
    db.session.add(att)
    db.session.flush()
    db.session.add(AttendanceLog(attendance_id=att.id, event="clock_in", timestamp=now, ip_address=request.remote_addr))
    db.session.commit()
    msg = f"{'Late - ' if is_late else ''}{'Half Day - ' if is_half_day else ''}Clocked in at {now.strftime('%H:%M')}"
    return jsonify({"success": True, "message": msg, "time": now.strftime('%H:%M:%S')})


@attendance_bp.route("/clock-out", methods=["POST"])
@csrf.exempt
@login_required
def clock_out():
    today = date.today()
    att = Attendance.query.filter_by(user_id=current_user.id, date=today).first()
    if not att or not att.clock_in:
        return jsonify({"error": "Not clocked in today"}), 400
    if att.clock_out:
        return jsonify({"error": "Already clocked out today"}), 400
    active_break = BreakLog.query.filter_by(attendance_id=att.id, break_end=None).first()
    if active_break:
        active_break.break_end = datetime.now()
        active_break.duration_minutes = int((active_break.break_end - active_break.break_start).total_seconds() / 60)
    now = datetime.now()
    att.clock_out = now
    db.session.add(AttendanceLog(attendance_id=att.id, event="clock_out", timestamp=now, ip_address=request.remote_addr))
    total_seconds = (now - att.clock_in).total_seconds()
    total_breaks = db.session.query(func.sum(BreakLog.duration_minutes)).filter(
        BreakLog.attendance_id == att.id, BreakLog.break_end != None).scalar() or 0
    net_seconds = total_seconds - (total_breaks * 60)
    hours = round(net_seconds / 3600, 2)
    regular = min(hours, 8.0)
    overtime = max(0, hours - 8.0)
    oa = OvertimeAccount.query.filter_by(user_id=current_user.id, date=today).first()
    if not oa:
        oa = OvertimeAccount(user_id=current_user.id, date=today)
        db.session.add(oa)
    oa.regular_hours = regular
    oa.overtime_hours = min(overtime, 4.0)
    oa.double_overtime = max(0, overtime - 4.0)
    db.session.commit()
    return jsonify({"success": True, "message": f"Clocked out. Net: {hours}h (Regular: {regular}h, OT: {overtime}h)", "time": now.strftime('%H:%M:%S')})


@attendance_bp.route("/break-start", methods=["POST"])
@csrf.exempt
@login_required
def break_start():
    today = date.today()
    att = Attendance.query.filter_by(user_id=current_user.id, date=today).first()
    if not att or not att.clock_in:
        return jsonify({"error": "Not clocked in"}), 400
    if att.clock_out:
        return jsonify({"error": "Already clocked out"}), 400
    active = BreakLog.query.filter_by(attendance_id=att.id, break_end=None).first()
    if active:
        return jsonify({"error": "Already on break"}), 400
    bl = BreakLog(attendance_id=att.id, break_start=datetime.now(),
                  break_type=request.form.get("type", "lunch"))
    db.session.add(bl)
    db.session.commit()
    return jsonify({"success": True, "message": "Break started"})


@attendance_bp.route("/break-end", methods=["POST"])
@csrf.exempt
@login_required
def break_end():
    today = date.today()
    att = Attendance.query.filter_by(user_id=current_user.id, date=today).first()
    if not att:
        return jsonify({"error": "Not clocked in"}), 400
    bl = BreakLog.query.filter_by(attendance_id=att.id, break_end=None).first()
    if not bl:
        return jsonify({"error": "No active break"}), 400
    bl.break_end = datetime.now()
    bl.duration_minutes = int((bl.break_end - bl.break_start).total_seconds() / 60)
    db.session.commit()
    return jsonify({"success": True, "message": f"Break ended ({bl.duration_minutes} min)"})


@attendance_bp.route("/live")
@login_required
def live_status():
    if not current_user.is_admin() and not current_user.is_manager():
        return jsonify({"error": "Access denied"}), 403
    today = date.today()
    from ..models.user import User
    users = User.employees().filter_by(is_active=True).all()
    statuses = []
    for u in users:
        att = Attendance.query.filter_by(user_id=u.id, date=today).first()
        if att and att.clock_in and not att.clock_out:
            on_break = BreakLog.query.filter_by(attendance_id=att.id, break_end=None).first()
            s = "on_break" if on_break else "online"
        elif att and att.clock_out:
            s = "completed"
        else:
            s = "absent"
        statuses.append({"id": u.id, "name": u.full_name, "department": u.department,
                         "status": s, "clock_in": att.clock_in.strftime('%H:%M') if att and att.clock_in else None})
    return jsonify(statuses)


@attendance_bp.route("/admin")
@login_required
def admin_view():
    if not current_user.is_admin():
        flash("Access denied.", "danger")
        return redirect(url_for("dashboard"))
    month = request.args.get("month", datetime.now().month, type=int)
    year = request.args.get("year", datetime.now().year, type=int)
    records = Attendance.query.filter(
        extract("month", Attendance.date) == month,
        extract("year", Attendance.date) == year,
    ).order_by(Attendance.date.desc()).all()
    corrections = AttendanceCorrection.query.order_by(AttendanceCorrection.corrected_at.desc()).limit(20).all()
    return render_template("attendance/admin.html", records=records, corrections=corrections, month=month, year=year)


@attendance_bp.route("/correct", methods=["POST"])
@login_required
def correct():
    if not current_user.is_admin() and not current_user.is_manager():
        return jsonify({"error": "Access denied"}), 403
    att_id = request.form.get("attendance_id", type=int)
    field = request.form.get("field")
    new_value = request.form.get("new_value")
    reason = request.form.get("reason", "").strip()
    if not att_id or not field or not new_value or not reason:
        return jsonify({"error": "All fields required"}), 400
    att = scoped_get_404(Attendance, att_id)
    old_value = str(getattr(att, field, ""))
    setattr(att, field, new_value)
    db.session.add(AttendanceCorrection(
        attendance_id=att_id, corrected_by=current_user.id,
        field=field, old_value=old_value, new_value=new_value, reason=reason
    ))
    db.session.commit()
    return jsonify({"success": True, "message": "Attendance corrected"})


@attendance_bp.route("/policies", methods=["GET", "POST"])
@login_required
def policies():
    if not current_user.is_admin():
        flash("Access denied.", "danger")
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        p = TimePolicy(
            name=request.form["name"], department=request.form.get("department", ""),
            shift_start=datetime.strptime(request.form["shift_start"], "%H:%M").time(),
            shift_end=datetime.strptime(request.form["shift_end"], "%H:%M").time(),
            grace_period_minutes=int(request.form.get("grace", 15)),
            max_regular_hours=float(request.form.get("max_reg", 8)),
            max_overtime_hours=float(request.form.get("max_ot", 4)),
            max_consecutive_days=int(request.form.get("max_days", 6)),
            require_break=request.form.get("require_break") == "on",
            min_break_minutes=int(request.form.get("min_break", 30)),
        )
        db.session.add(p)
        db.session.commit()
        flash("Time policy created.", "success")
        return redirect(url_for("attendance.policies"))
    policies = TimePolicy.query.all()
    return render_template("attendance/policies.html", policies=policies)


@attendance_bp.route("/api/logs")
@login_required
def api_logs():
    records = Attendance.query.filter_by(user_id=current_user.id).order_by(Attendance.date.desc()).limit(60).all()
    return jsonify([{
        "id": r.id, "date": r.date.isoformat(),
        "clock_in": r.clock_in.isoformat() if r.clock_in else None,
        "clock_out": r.clock_out.isoformat() if r.clock_out else None,
        "status": r.status, "is_late": r.is_late, "is_half_day": r.is_half_day
    } for r in records])


@attendance_bp.route("/overview")
@login_required
def overview():
    period = request.args.get("period", "monthly")
    today = date.today()
    if period == "daily":
        records = Attendance.query.filter_by(user_id=current_user.id, date=today).all()
    elif period == "weekly":
        start = today - timedelta(days=today.weekday())
        records = Attendance.query.filter(
            Attendance.user_id == current_user.id,
            Attendance.date >= start, Attendance.date < start + timedelta(days=7)
        ).all()
    elif period == "yearly":
        records = Attendance.query.filter(
            Attendance.user_id == current_user.id,
            extract("year", Attendance.date) == today.year
        ).order_by(Attendance.date.desc()).all()
    else:
        records = Attendance.query.filter(
            Attendance.user_id == current_user.id,
            extract("month", Attendance.date) == today.month,
            extract("year", Attendance.date) == today.year
        ).order_by(Attendance.date.desc()).all()
    ot_records = OvertimeAccount.query.filter_by(user_id=current_user.id).order_by(OvertimeAccount.date.desc()).limit(30).all()
    return render_template("attendance/overview.html", records=records, ot_records=ot_records, period=period)
