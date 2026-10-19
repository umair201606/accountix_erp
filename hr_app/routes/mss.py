from datetime import datetime, date
from flask import Blueprint, render_template, request, jsonify, flash, redirect, url_for, abort
from flask_login import login_required, current_user
from ..extensions import db
from shared.tenancy import scoped_get, scoped_get_404, get_member
from ..models.user import User
from ..models.leave import LeaveRequest, LeaveApproval, LeaveQuota, LeaveType
from ..models.timesheet import TimesheetWeek, TimesheetApproval, TimesheetEntry
from ..models.loan import LoanAdvanceRequest, LoanRepayment
from ..models.compensation import SalaryRevision
from ..models.communication import Notification, NotificationRecipient

mss_bp = Blueprint("mss", __name__, url_prefix="/mss")


def _require_manager():
    if not current_user.is_manager() and not current_user.is_admin():
        flash("Access denied.", "danger")
        return False
    return True


def _notify(user_id, title, message, ntype="info", module="mss", ref_id=None):
    notif = Notification(title=title, message=message, notification_type=ntype,
                         module=module, reference_id=ref_id, created_by=current_user.id)
    db.session.add(notif)
    db.session.flush()
    db.session.add(NotificationRecipient(notification_id=notif.id, user_id=user_id))


@mss_bp.route("/")
@login_required
def index():
    if not _require_manager():
        return redirect(url_for("dashboard"))
    if current_user.is_admin():
        reports = User.employees().filter(User.is_active == True, User.id != current_user.id).order_by(User.full_name).all()
    else:
        reports = current_user.reports_of()
    pending_leaves = LeaveRequest.query.filter(
        LeaveRequest.user_id.in_([u.id for u in reports]),
        LeaveRequest.status == "pending"
    ).count()
    pending_ts = TimesheetApproval.query.filter_by(approver_id=current_user.id, status="pending").count()
    pending_loans = LoanAdvanceRequest.query.filter(
        LoanAdvanceRequest.user_id.in_([u.id for u in reports]),
        LoanAdvanceRequest.status == "pending"
    ).count()
    return render_template("mss/index.html", direct_reports=reports,
                           pending_leaves=pending_leaves, pending_ts=pending_ts, pending_loans=pending_loans)


@mss_bp.route("/approvals")
@login_required
def approvals():
    if not _require_manager():
        return redirect(url_for("dashboard"))
    leave_approvals = LeaveApproval.query.filter_by(approver_id=current_user.id, status="pending").all()
    ts_approvals = TimesheetApproval.query.filter_by(approver_id=current_user.id, status="pending").all()
    my_reports = [u.id for u in current_user.reports_of()]
    loan_approvals = LoanAdvanceRequest.query.filter(
        LoanAdvanceRequest.user_id.in_(my_reports),
        LoanAdvanceRequest.status == "pending"
    ).all()
    return render_template("mss/approvals.html", leave_approvals=leave_approvals,
                           ts_approvals=ts_approvals, loan_approvals=loan_approvals)


def _answer(ok, msg, status=200):
    """JSON for scripts (fetch with Accept: application/json), flash +
    redirect for the approval page's plain forms — they used to land on a
    bare {"success": true} page."""
    if request.is_json or request.accept_mimetypes.best == "application/json":
        return jsonify({"success": True} if ok else {"error": msg}), status
    flash(msg, "success" if ok else "danger")
    return redirect(url_for("mss.approvals"))


@mss_bp.route("/approve-leave/<int:aid>", methods=["POST"])
@login_required
def approve_leave(aid):
    from .. import leave_flow
    approval = scoped_get_404(LeaveApproval, aid)
    action = request.form.get("action")
    comment = request.form.get("comment", "")
    err = leave_flow.decide(approval, action, comment, current_user.id)
    if err:
        db.session.rollback()
        return _answer(False, err, 403 if approval.approver_id != current_user.id else 400)
    lr = approval.leave_request
    if action == "reject":
        _notify(lr.user_id, "Leave Rejected", f"Your {lr.leave_type.name} was rejected: {comment.strip()}", "danger")
    elif lr.status == "approved":
        _notify(lr.user_id, "Leave Approved", f"Your {lr.leave_type.name} has been approved.", "success")
    db.session.commit()
    return _answer(True, f"Leave request {action}d.")


@mss_bp.route("/approve-timesheet/<int:aid>", methods=["POST"])
@login_required
def approve_timesheet(aid):
    approval = scoped_get_404(TimesheetApproval, aid, for_update=True)
    if approval.approver_id != current_user.id:
        return _answer(False, "Not authorized", 403)
    action = request.form.get("action")
    comment = request.form.get("comment", "")
    if approval.status != "pending":
        return _answer(False, "This timesheet has already been decided.", 400)
    if action != "approve" and not comment.strip():
        return _answer(False, "Reason required for rejection", 400)
    if action == "approve":
        approval.status = "approved"
        approval.week.status = "approved"
        approval.week.approved_at = datetime.utcnow()
        approval.week.approved_by = current_user.id
    else:
        approval.status = "rejected"
        approval.week.status = "unapproved"
    approval.comment = comment
    db.session.commit()
    return _answer(True, f"Timesheet {'approved' if action == 'approve' else 'rejected'}.")


@mss_bp.route("/approve-loan/<int:lid>", methods=["POST"])
@login_required
def approve_loan(lid):
    loan = scoped_get_404(LoanAdvanceRequest, lid, for_update=True)
    if loan.user.manager_id != current_user.id and not current_user.is_admin():
        return _answer(False, "Not authorized", 403)
    action = request.form.get("action")
    if action not in ("approve", "reject"):
        return _answer(False, "Unknown action.", 400)
    # Re-deciding re-opened paid or rejected loans for payroll deductions.
    if loan.status != "pending":
        return _answer(False, "This loan request has already been decided.", 400)
    if action == "approve":
        loan.status = "approved"
        loan.approved_by = current_user.id
        loan.approved_at = datetime.utcnow()
        # Loans created before ledger integration get their account on approval.
        from shared.ledger_utils import create_entity_account
        create_entity_account("loan", loan.id,
                              f"{loan.request_type.title()} #{loan.id} - {loan.user.full_name}")
        loan.queue_for_payroll = True
        _notify(loan.user_id, "Loan Approved", f"Your loan of Rs.{loan.amount:,.0f} has been approved.", "success")
    else:
        loan.status = "rejected"
        _notify(loan.user_id, "Loan Rejected", f"Your loan request was rejected.", "danger")
    db.session.commit()
    return _answer(True, f"Loan request {action}d.")


@mss_bp.route("/bulk-approve-timesheets", methods=["POST"])
@login_required
def bulk_approve_timesheets():
    ids = (request.get_json(silent=True) or {}).get("ids", [])
    done = 0
    for tid in sorted({int(t) for t in ids if str(t).isdigit()}):
        approval = scoped_get(TimesheetApproval, tid, for_update=True)
        if approval and approval.approver_id == current_user.id and approval.status == "pending":
            approval.status = "approved"
            approval.week.status = "approved"
            approval.week.approved_at = datetime.utcnow()
            approval.week.approved_by = current_user.id
            done += 1
    db.session.commit()
    return jsonify({"success": True, "count": done})


@mss_bp.route("/team")
@login_required
def team():
    if not _require_manager():
        return redirect(url_for("dashboard"))
    if current_user.is_admin():
        reports = User.employees().filter(User.is_active == True, User.id != current_user.id).order_by(User.full_name).all()
    else:
        reports = current_user.reports_of()
    return render_template("mss/team.html", members=reports)


@mss_bp.route("/team-availability")
@login_required
def team_availability():
    if not _require_manager():
        return jsonify({"error": "Access denied"}), 403
    today = date.today()
    reports = current_user.reports_of()
    statuses = []
    for u in reports:
        from ..models.attendance import Attendance
        att = Attendance.query.filter_by(user_id=u.id, date=today).first()
        if att and att.clock_in and not att.clock_out:
            s = "on_duty"
        elif att and att.clock_out:
            s = "completed"
        else:
            leave = LeaveRequest.query.filter(
                LeaveRequest.user_id == u.id, LeaveRequest.status == "approved",
                LeaveRequest.start_date <= today, LeaveRequest.end_date >= today
            ).first()
            s = "on_leave" if leave else "absent"
        statuses.append({"id": u.id, "name": u.full_name, "designation": u.designation, "status": s})
    return jsonify(statuses)


@mss_bp.route("/team-calendar")
@login_required
def team_calendar():
    if not _require_manager():
        return jsonify({"error": "Access denied"}), 403
    month = request.args.get("month", datetime.now().month, type=int)
    year = request.args.get("year", datetime.now().year, type=int)
    from datetime import timedelta
    first_day = date(year, month, 1)
    if month == 12:
        last_day = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        last_day = date(year, month + 1, 1) - timedelta(days=1)
    reports = current_user.reports_of()
    report_ids = [u.id for u in reports]
    leaves = LeaveRequest.query.filter(
        LeaveRequest.user_id.in_(report_ids),
        LeaveRequest.status == "approved",
        LeaveRequest.start_date <= last_day,
        LeaveRequest.end_date >= first_day
    ).all()
    return jsonify([{
        "id": lr.id, "user": lr.user.full_name, "type": lr.leave_type.name,
        "start": lr.start_date.isoformat(), "end": lr.end_date.isoformat()
    } for lr in leaves])


@mss_bp.route("/evaluate/<int:uid>", methods=["GET", "POST"])
@login_required
def evaluate(uid):
    if not _require_manager():
        return redirect(url_for("dashboard"))
    emp = get_member(uid) or abort(404)
    if emp.manager_id != current_user.id and not current_user.is_admin():
        flash("Not your direct report.", "danger")
        return redirect(url_for("mss.index"))
    if request.method == "POST":
        from shared.clock import local_today
        from shared.forms import form_date
        from ..models.compensation import PayrollProfile
        new_basic = request.form.get("new_basic", type=float)
        reason = request.form.get("reason", "").strip()
        effective = form_date("effective_from", default=False) or local_today()
        # payroll_profile is a list backref (one profile per company), so
        # ".basic_salary" on it raised and every evaluation of an employee
        # with a salary package was a 500. Look the profile up in this company.
        profile = PayrollProfile.query.filter_by(user_id=emp.id).first()
        if new_basic and new_basic > 0:
            old_basic = float(profile.basic_salary or 0) if profile else 0
            db.session.add(SalaryRevision(
                user_id=emp.id, previous_basic=old_basic, new_basic=new_basic,
                reason=reason, approved_by=current_user.id, effective_from=effective))
            # Payroll reads the dated revision; the profile shows the rate in
            # force today.
            if profile and effective <= local_today():
                profile.basic_salary = new_basic
            flash(f"Salary revision recorded, effective {effective:%d %b %Y}.", "success")
            db.session.commit()
        else:
            flash("Enter a new basic salary above zero.", "error")
        return redirect(url_for("mss.index"))
    reviews = emp.performance_reviews
    from shared.clock import local_today
    from ..models.compensation import PayrollProfile
    return render_template("mss/evaluate.html", employee=emp, reviews=reviews,
                           profile=PayrollProfile.query.filter_by(user_id=emp.id).first(),
                           today=local_today())
