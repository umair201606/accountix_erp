from datetime import datetime, date, timedelta
from flask import Blueprint, render_template, request, jsonify, flash, redirect, url_for
from flask_login import login_required, current_user
from sqlalchemy import extract
from ..extensions import db
from shared.tenancy import scoped_get, scoped_get_404
from ..models.leave import LeaveType, LeaveQuota, LeaveRequest, LeaveApproval
from ..models.holiday import CompanyHoliday, ApprovalWorkflow
from ..models.communication import Notification, NotificationRecipient
from .. import leave_flow

leave_bp = Blueprint("leave", __name__, url_prefix="/leaves")


def _get_working_days(start, end):
    holidays = {h.holiday_date for h in CompanyHoliday.query.filter(
        CompanyHoliday.holiday_date >= start, CompanyHoliday.holiday_date <= end, CompanyHoliday.is_active == True
    ).all()}
    days = 0
    d = start
    while d <= end:
        if d.weekday() < 5 and d not in holidays:
            days += 1
        d += timedelta(days=1)
    return days


def _create_notification(user_id, title, message, ntype="info", module="leaves", ref_id=None):
    notif = Notification(title=title, message=message, notification_type=ntype,
                         module=module, reference_id=ref_id, created_by=current_user.id)
    db.session.add(notif)
    db.session.flush()
    db.session.add(NotificationRecipient(notification_id=notif.id, user_id=user_id))


@leave_bp.route("/")
@login_required
def index():
    requests = LeaveRequest.query.filter_by(user_id=current_user.id).order_by(LeaveRequest.created_at.desc()).all()
    leave_types = LeaveType.query.all()
    for lt in leave_types:
        leave_flow.refresh(leave_flow.quota_for(current_user.id, lt, date.today().year))
    db.session.commit()
    quotas = LeaveQuota.query.filter_by(user_id=current_user.id, year=date.today().year).all()
    upcoming_holidays = CompanyHoliday.query.filter(
        CompanyHoliday.holiday_date >= date.today(), CompanyHoliday.is_active == True
    ).order_by(CompanyHoliday.holiday_date).limit(10).all()
    return render_template("leaves/index.html", requests=requests, quotas=quotas,
                           leave_types=leave_types, upcoming_holidays=upcoming_holidays)


@leave_bp.route("/calendar")
@login_required
def calendar():
    month = request.args.get("month", datetime.now().month, type=int)
    year = request.args.get("year", datetime.now().year, type=int)
    first_day = date(year, month, 1)
    if month == 12:
        last_day = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        last_day = date(year, month + 1, 1) - timedelta(days=1)
    company_leaves = LeaveRequest.query.filter(
        LeaveRequest.status == "approved",
        LeaveRequest.start_date <= last_day,
        LeaveRequest.end_date >= first_day
    ).all()
    holidays = CompanyHoliday.query.filter(
        CompanyHoliday.holiday_date >= first_day,
        CompanyHoliday.holiday_date <= last_day,
        CompanyHoliday.is_active == True
    ).all()
    if current_user.is_admin() or current_user.is_manager():
        entries = [{"date": lr.start_date, "title": f"{lr.user.full_name} - {lr.leave_type.name}",
                    "type": "leave"} for lr in company_leaves]
    else:
        team_ids = [current_user.id]
        if current_user.manager_id:
            from ..models.user import User
            from shared.models.company import CompanyMembership
            from shared.tenancy import current_company_id
            cid = current_company_id()
            if cid is not None:
                team = (User.query.join(CompanyMembership, db.and_(
                    CompanyMembership.user_id == User.id,
                    CompanyMembership.company_id == cid,
                    CompanyMembership.status == CompanyMembership.ACTIVE))
                    .filter(CompanyMembership.manager_id == current_user.manager_id).all())
            else:
                team = []
            team_ids = [u.id for u in team] + [current_user.id]
        team_leaves = [lr for lr in company_leaves if lr.user_id in team_ids]
        entries = [{"date": lr.start_date, "title": f"{lr.user.full_name} - Out of Office",
                    "type": "leave_anon"} for lr in team_leaves]
    for h in holidays:
        entries.append({"date": h.holiday_date, "title": f"&#127775; {h.name}", "type": "holiday"})
    return jsonify({"entries": entries, "month": month, "year": year})


@leave_bp.route("/team-calendar")
@login_required
def team_calendar():
    month = request.args.get("month", datetime.now().month, type=int)
    year = request.args.get("year", datetime.now().year, type=int)
    first_day = date(year, month, 1)
    if month == 12:
        last_day = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        last_day = date(year, month + 1, 1) - timedelta(days=1)
    approved = LeaveRequest.query.filter(
        LeaveRequest.status == "approved",
        LeaveRequest.start_date <= last_day,
        LeaveRequest.end_date >= first_day
    ).all()
    holidays = CompanyHoliday.query.filter(
        CompanyHoliday.holiday_date >= first_day,
        CompanyHoliday.holiday_date <= last_day,
        CompanyHoliday.is_active == True
    ).all()
    entries = []
    for lr in approved:
        entries.append({
            "id": lr.id, "user": lr.user.full_name, "user_id": lr.user_id,
            "date": lr.start_date.isoformat(), "end_date": lr.end_date.isoformat(),
            "type": lr.leave_type.name,
            "label": f"{lr.user.full_name} - {lr.leave_type.name}" if (current_user.is_admin() or current_user.is_manager()) else f"{lr.user.full_name} - Out of Office"
        })
    for h in holidays:
        entries.append({"id": -h.id, "title": h.name, "date": h.holiday_date.isoformat(),
                        "type": "holiday", "label": h.name})
    return jsonify(entries)


@leave_bp.route("/apply", methods=["GET", "POST"])
@login_required
def apply():
    leave_types = LeaveType.query.all()
    if request.method == "POST":
        def again(msg):
            flash(msg, "danger")
            return render_template("leaves/apply.html", leave_types=leave_types)
        leave_type_id = request.form.get("leave_type", type=int)
        try:
            start_date = datetime.strptime(request.form.get("start_date") or "", "%Y-%m-%d").date()
            end_date = datetime.strptime(request.form.get("end_date") or "", "%Y-%m-%d").date()
        except ValueError:
            return again("Pick a start and an end date.")
        reason = request.form.get("reason", "").strip()
        is_half_day = request.form.get("is_half_day") == "on"
        if start_date > end_date:
            return again("Start date must be before end date.")
        if is_half_day and start_date != end_date:
            return again("A half day starts and ends on the same date.")
        lt = scoped_get(LeaveType, leave_type_id)
        if not lt:
            return again("Invalid leave type.")
        if is_half_day:
            total_days = 0.5 if _get_working_days(start_date, end_date) else 0
        else:
            total_days = _get_working_days(start_date, end_date)
        if total_days < 0.5:
            return again("No working days in selected range (weekends/holidays excluded).")
        # A double-clicked Apply used to file two overlapping requests: the
        # second waits here, then finds the first.
        from shared.concurrency import serialize
        serialize("leave", current_user.id)
        clash = leave_flow.overlapping(current_user.id, start_date, end_date)
        if clash:
            return again(f"You already have a {clash.status} request for "
                         f"{clash.start_date:%d %b} – {clash.end_date:%d %b %Y}.")
        quota = leave_flow.quota_for(current_user.id, lt, start_date.year)
        leave_flow.refresh(quota)
        if quota.used + quota.pending + total_days > (quota.total or 0):
            return again(f"Insufficient leave balance: {quota.remaining:g} day(s) of "
                         f"{lt.name} left for {start_date.year}.")
        lr = LeaveRequest(
            user_id=current_user.id, leave_type_id=leave_type_id,
            start_date=start_date, end_date=end_date, total_days=total_days,
            reason=reason, is_half_day=is_half_day
        )
        db.session.add(lr)
        db.session.flush()
        workflow = ApprovalWorkflow.query.filter_by(leave_type_id=leave_type_id, is_active=True).first()
        approver = leave_flow.approver_for(current_user, right=("leaves", "approve"))
        if (workflow and workflow.auto_approve) or approver is None:
            # Auto-approved type, or nobody else to decide (the company's
            # only admin applying).
            lr.status = "approved"
        else:
            db.session.add(LeaveApproval(leave_request_id=lr.id, approver_id=approver, level=1))
            _create_notification(approver, "Leave Request",
                                 f"{current_user.full_name} requests {total_days:g} day(s) of {lt.name}")
        leave_flow.refresh(quota)
        db.session.commit()
        flash("Leave approved." if lr.status == "approved" else "Leave request submitted.", "success")
        return redirect(url_for("leave.index"))
    return render_template("leaves/apply.html", leave_types=leave_types)


@leave_bp.route("/approvals")
@login_required
def approvals():
    if not current_user.is_admin() and not current_user.is_manager():
        flash("Access denied.", "danger")
        return redirect(url_for("dashboard"))
    pending = LeaveApproval.query.filter_by(approver_id=current_user.id, status="pending").all()
    return render_template("leaves/approvals.html", approvals=pending)


@leave_bp.route("/review/<int:aid>", methods=["POST"])
@login_required
def review(aid):
    approval = scoped_get_404(LeaveApproval, aid)
    action = request.form.get("action")
    comment = request.form.get("comment", "")
    err = leave_flow.decide(approval, action, comment, current_user.id)
    if err:
        db.session.rollback()
        flash(err, "danger")
        return redirect(url_for("leave.approvals"))
    lr = approval.leave_request
    if action == "reject":
        _create_notification(lr.user_id, "Leave Rejected",
                             f"Your {lr.leave_type.name} request has been rejected. Reason: {comment.strip()}",
                             "danger", "leaves", lr.id)
    elif lr.status == "approved":
        _create_notification(lr.user_id, "Leave Approved",
                             f"Your {lr.leave_type.name} request ({lr.total_days:g} day(s)) has been approved.",
                             "success", "leaves", lr.id)
    db.session.commit()
    flash(f"Leave request {action}d.", "success")
    return redirect(url_for("leave.approvals"))


@leave_bp.route("/holidays", methods=["GET", "POST"])
@login_required
def holidays():
    if not current_user.is_admin():
        flash("Access denied.", "danger")
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        h = CompanyHoliday(
            name=request.form["name"],
            holiday_date=datetime.strptime(request.form["holiday_date"], "%Y-%m-%d").date(),
            is_recurring=request.form.get("is_recurring") == "on",
            department=request.form.get("department", ""),
        )
        db.session.add(h)
        db.session.commit()
        flash("Holiday added.", "success")
        return redirect(url_for("leave.holidays"))
    all_holidays = CompanyHoliday.query.order_by(CompanyHoliday.holiday_date.desc()).all()
    return render_template("leaves/holidays.html", holidays=all_holidays)


@leave_bp.route("/workflows", methods=["GET", "POST"])
@login_required
def workflows():
    if not current_user.is_admin():
        flash("Access denied.", "danger")
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        wf = ApprovalWorkflow(
            name=request.form["name"], module="leaves",
            leave_type_id=request.form.get("leave_type_id", type=int),
            approval_levels=int(request.form.get("approval_levels", 1)),
            auto_approve=request.form.get("auto_approve") == "on",
            notify_admins=request.form.get("notify_admins") == "on",
        )
        db.session.add(wf)
        db.session.commit()
        flash("Approval workflow created.", "success")
        return redirect(url_for("leave.workflows"))
    workflows = ApprovalWorkflow.query.all()
    leave_types = LeaveType.query.all()
    return render_template("leaves/workflows.html", workflows=workflows, leave_types=leave_types)
