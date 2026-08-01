from datetime import datetime, date, timedelta
from flask import Blueprint, render_template, request, jsonify, flash, redirect, url_for
from flask_login import login_required, current_user
from sqlalchemy import func
from ..extensions import db, csrf
from shared.tenancy import scoped_get, scoped_get_404
from ..models.timesheet import TimesheetWeek, TimesheetEntry, TimesheetApproval
from ..models.project import Project, WorkPackage, ProjectTask
from ..models.communication import Notification, NotificationRecipient
from ..models.performance import TimesheetMergedReport

timesheet_bp = Blueprint("timesheet", __name__, url_prefix="/timesheets")


def _week_range(d):
    start = d - timedelta(days=d.weekday())
    return start, start + timedelta(days=6)


@timesheet_bp.route("/")
@login_required
def index():
    today = date.today()
    week_start, week_end = _week_range(today)
    week = TimesheetWeek.query.filter_by(user_id=current_user.id, week_start=week_start).first()
    if not week:
        week = TimesheetWeek(user_id=current_user.id, week_start=week_start, week_end=week_end)
        db.session.add(week)
        db.session.commit()
    entries = TimesheetEntry.query.filter_by(week_id=week.id).order_by(TimesheetEntry.day).all()
    days = []
    for i in range(7):
        d = week_start + timedelta(days=i)
        day_entries = [e for e in entries if e.day == d]
        total = sum(e.hours for e in day_entries)
        days.append({"date": d, "entries": day_entries, "total": total})
    recent = TimesheetWeek.query.filter_by(user_id=current_user.id).order_by(TimesheetWeek.week_start.desc()).limit(12).all()
    projects = Project.query.filter_by(status="active").all()
    work_packages = WorkPackage.query.all()
    tasks = ProjectTask.query.all()
    return render_template("timesheets/index.html", week=week, days=days, recent=recent,
                           projects=projects, work_packages=work_packages, tasks=tasks)


@timesheet_bp.route("/add-entry", methods=["POST"])
@csrf.exempt
@login_required
def add_entry():
    week_id = request.form.get("week_id", type=int)
    day = datetime.strptime(request.form.get("day"), "%Y-%m-%d").date()
    project_id = request.form.get("project_id", type=int)
    task_name = request.form.get("task", "").strip()
    hours = request.form.get("hours", type=float)
    description = request.form.get("description", "").strip()
    if not task_name or not hours:
        return jsonify({"error": "Task and hours required"}), 400
    week = scoped_get_404(TimesheetWeek, week_id)
    if week.user_id != current_user.id:
        return jsonify({"error": "Not authorized"}), 403
    entry = TimesheetEntry(week_id=week_id, day=day, project=task_name,
                           task=task_name, hours=hours, description=description)
    db.session.add(entry)
    week.total_hours = (week.total_hours or 0) + hours
    db.session.commit()
    return jsonify({"success": True, "id": entry.id, "total_hours": week.total_hours})


@timesheet_bp.route("/delete-entry/<int:eid>", methods=["POST"])
@csrf.exempt
@login_required
def delete_entry(eid):
    entry = scoped_get_404(TimesheetEntry, eid)
    week = scoped_get(TimesheetWeek, entry.week_id)
    if week.user_id != current_user.id:
        return jsonify({"error": "Not authorized"}), 403
    week.total_hours = max(0, (week.total_hours or 0) - entry.hours)
    db.session.delete(entry)
    db.session.commit()
    return jsonify({"success": True})


@timesheet_bp.route("/submit/<int:wid>", methods=["POST"])
@login_required
def submit(wid):
    week = scoped_get_404(TimesheetWeek, wid)
    if week.user_id != current_user.id:
        return jsonify({"error": "Not authorized"}), 403
    week.status = "submitted"
    week.submitted_at = datetime.utcnow()
    if current_user.manager_id:
        approval = TimesheetApproval(week_id=wid, approver_id=current_user.manager_id)
        db.session.add(approval)
        notif = Notification(title="Timesheet Submitted", message=f"{current_user.full_name} submitted timesheet",
                             notification_type="info", module="timesheets", reference_id=wid, created_by=current_user.id)
        db.session.add(notif)
        db.session.flush()
        db.session.add(NotificationRecipient(notification_id=notif.id, user_id=current_user.manager_id))
    db.session.commit()
    flash("Timesheet submitted for approval.", "success")
    return redirect(url_for("timesheet.index"))


@timesheet_bp.route("/approvals")
@login_required
def approvals():
    if not current_user.is_manager() and not current_user.is_admin():
        flash("Access denied.", "danger")
        return redirect(url_for("dashboard"))
    pending = TimesheetApproval.query.filter_by(approver_id=current_user.id, status="pending").all()
    return render_template("timesheets/approvals.html", approvals=pending)


@timesheet_bp.route("/review/<int:aid>", methods=["POST"])
@login_required
def review(aid):
    approval = scoped_get_404(TimesheetApproval, aid)
    if approval.approver_id != current_user.id:
        return jsonify({"error": "Not authorized"}), 403
    action = request.form.get("action")
    comment = request.form.get("comment", "")
    if action == "approve":
        approval.status = "approved"
        approval.week.status = "approved"
        approval.week.approved_at = datetime.utcnow()
        approval.week.approved_by = current_user.id
    else:
        if not comment:
            flash("Please provide a reason for rejection.", "danger")
            return redirect(url_for("timesheet.approvals"))
        approval.status = "rejected"
        approval.week.status = "unapproved"
    approval.comment = comment
    db.session.commit()
    flash(f"Timesheet {action}d.", "success")
    return redirect(url_for("timesheet.approvals"))


@timesheet_bp.route("/projects")
@login_required
def projects():
    if not current_user.is_admin():
        flash("Access denied.", "danger")
        return redirect(url_for("dashboard"))
    all_projects = Project.query.order_by(Project.created_at.desc()).all()
    return render_template("timesheets/projects.html", projects=all_projects)


@timesheet_bp.route("/projects/create", methods=["POST"])
@login_required
def create_project():
    if not current_user.is_admin():
        return jsonify({"error": "Access denied"}), 403
    p = Project(
        name=request.form["name"],
        code=request.form.get("code", ""),
        description=request.form.get("description", ""),
        department=request.form.get("department", ""),
        project_manager_id=request.form.get("pm_id", type=int),
        start_date=datetime.strptime(request.form["start_date"], "%Y-%m-%d").date() if request.form.get("start_date") else None,
        status=request.form.get("status", "active"),
    )
    db.session.add(p)
    db.session.commit()
    flash("Project created.", "success")
    return redirect(url_for("timesheet.projects"))


@timesheet_bp.route("/merge-report", methods=["POST"])
@login_required
def merge_report():
    if not current_user.is_admin():
        return jsonify({"error": "Access denied"}), 403
    period_start = datetime.strptime(request.form["period_start"], "%Y-%m-%d").date()
    period_end = datetime.strptime(request.form["period_end"], "%Y-%m-%d").date()
    users = User.employees().filter(User.is_active.is_(True)).all()
    results = []
    for u in users:
        total = db.session.query(func.sum(TimesheetEntry.hours)).join(TimesheetWeek).filter(
            TimesheetWeek.user_id == u.id, TimesheetWeek.status == "approved",
            TimesheetEntry.day >= period_start, TimesheetEntry.day <= period_end
        ).scalar() or 0
        if total > 0:
            results.append({"user": u.full_name, "total_hours": round(float(total), 2)})
            report = TimesheetMergedReport(user_id=u.id, period_start=period_start, period_end=period_end,
                                            total_hours=round(float(total), 2))
            db.session.add(report)
    db.session.commit()
    return jsonify({"results": results})
