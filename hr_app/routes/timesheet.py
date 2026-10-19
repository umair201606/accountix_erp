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
from ..models.user import User
from shared.clock import local_today

timesheet_bp = Blueprint("timesheet", __name__, url_prefix="/timesheets")


def _week_range(d):
    start = d - timedelta(days=d.weekday())
    return start, start + timedelta(days=6)


@timesheet_bp.route("/")
@login_required
def index():
    today = local_today()
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
    try:
        day = datetime.strptime(request.form.get("day") or "", "%Y-%m-%d").date()
    except ValueError:
        return jsonify({"error": "Pick a day"}), 400
    project_id = request.form.get("project_id", type=int)
    task_name = request.form.get("task", "").strip()
    hours = request.form.get("hours", type=float)
    description = request.form.get("description", "").strip()
    if not task_name or not hours:
        return jsonify({"error": "Task and hours required"}), 400
    if not (0 < hours <= 24):
        return jsonify({"error": "Hours must be between 0 and 24"}), 400
    week = scoped_get_404(TimesheetWeek, week_id)
    if week.user_id != current_user.id:
        return jsonify({"error": "Not authorized"}), 403
    # A submitted or approved week is frozen: entries used to be addable and
    # deletable after the manager had signed it off.
    if week.status != "unapproved":
        return jsonify({"error": f"This week is {week.status}; it can no longer be changed"}), 400
    if not (week.week_start <= day <= week.week_end):
        return jsonify({"error": "That day is not in this week"}), 400
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
    if week.status != "unapproved":
        return jsonify({"error": f"This week is {week.status}; it can no longer be changed"}), 400
    week.total_hours = max(0, (week.total_hours or 0) - entry.hours)
    db.session.delete(entry)
    db.session.commit()
    return jsonify({"success": True})


@timesheet_bp.route("/submit/<int:wid>", methods=["POST"])
@login_required
def submit(wid):
    # Locked: a double-clicked Submit queued two approvals for one week.
    week = scoped_get_404(TimesheetWeek, wid, for_update=True)
    if week.user_id != current_user.id:
        return jsonify({"error": "Not authorized"}), 403
    # Submitting twice used to queue a second approval for the same week.
    if week.status != "unapproved":
        flash(f"This week is already {week.status}.", "danger")
        return redirect(url_for("timesheet.index"))
    if not TimesheetEntry.query.filter_by(week_id=wid).first():
        flash("Add at least one entry before submitting.", "danger")
        return redirect(url_for("timesheet.index"))
    from ..leave_flow import approver_for
    approver = approver_for(current_user)
    week.submitted_at = datetime.utcnow()
    if approver is None:
        # Nobody else to sign it off (the company's only admin).
        week.status = "approved"
        week.approved_at = datetime.utcnow()
        week.approved_by = current_user.id
    else:
        # A manager outside this company, or none, used to leave the week
        # submitted with no approver at all.
        week.status = "submitted"
        db.session.add(TimesheetApproval(week_id=wid, approver_id=approver))
        notif = Notification(title="Timesheet Submitted", message=f"{current_user.full_name} submitted timesheet",
                             notification_type="info", module="timesheets", reference_id=wid, created_by=current_user.id)
        db.session.add(notif)
        db.session.flush()
        db.session.add(NotificationRecipient(notification_id=notif.id, user_id=approver))
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
    approval = scoped_get_404(TimesheetApproval, aid, for_update=True)
    if approval.approver_id != current_user.id:
        return jsonify({"error": "Not authorized"}), 403
    action = request.form.get("action")
    comment = request.form.get("comment", "")
    if approval.status != "pending":
        flash("This timesheet has already been decided.", "danger")
        return redirect(url_for("timesheet.approvals"))
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
    try:
        period_start = datetime.strptime(request.form.get("period_start") or "", "%Y-%m-%d").date()
        period_end = datetime.strptime(request.form.get("period_end") or "", "%Y-%m-%d").date()
    except ValueError:
        return jsonify({"error": "Pick a period"}), 400
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
