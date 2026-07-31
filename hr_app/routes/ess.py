import os
from datetime import datetime, date
from flask import Blueprint, render_template, request, jsonify, flash, redirect, url_for, send_file, current_app
from flask_login import login_required, current_user
from ..extensions import db
from shared.tenancy import scoped_get_404, get_member, current_company_id
from ..models.user import User
from ..models.change_request import ChangeRequest
from ..models.leave import LeaveRequest, LeaveQuota, LeaveType
from ..models.timesheet import TimesheetWeek, TimesheetEntry
from ..models.loan import LoanAdvanceRequest, LoanRepayment
from ..models.compensation import PayrollSlip
from ..models.performance import PerformanceReview, PerformanceGoal
from ..models.communication import Notification, NotificationRecipient

ess_bp = Blueprint("ess", __name__, url_prefix="/ess")


@ess_bp.route("/")
@login_required
def index():
    user = current_user
    quotas = LeaveQuota.query.filter_by(user_id=user.id).all()
    loan_requests = LoanAdvanceRequest.query.filter_by(user_id=user.id).order_by(LoanAdvanceRequest.created_at.desc()).all()
    slips = PayrollSlip.query.filter_by(user_id=user.id).order_by(PayrollSlip.created_at.desc()).limit(12).all()
    reviews = PerformanceReview.query.filter_by(user_id=user.id).order_by(PerformanceReview.created_at.desc()).all()
    return render_template("ess/index.html", user=user, quotas=quotas,
                           loan_requests=loan_requests, slips=slips, reviews=reviews)


@ess_bp.route("/update-profile", methods=["POST"])
@login_required
def update_profile():
    # Direct update for display name
    name = request.form.get("full_name", "").strip()
    if name and name != current_user.full_name:
        current_user.full_name = name
    # Change request fields
    fields = ["phone", "emergency_contact", "emergency_phone", "address"]
    for f in fields:
        val = request.form.get(f, "").strip()
        if val and getattr(current_user, f) != val:
            cr = ChangeRequest.query.filter_by(user_id=current_user.id, field_name=f, status="pending").first()
            if not cr:
                old = getattr(current_user, f) or ""
                db.session.add(ChangeRequest(user_id=current_user.id, field_name=f,
                                              old_value=str(old), new_value=val))
    sensitive = ["bank_name", "bank_account_title", "bank_account_number"]
    for f in sensitive:
        val = request.form.get(f, "").strip()
        if val and getattr(current_user, f) != val:
            cr = ChangeRequest.query.filter_by(user_id=current_user.id, field_name=f, status="pending").first()
            if not cr:
                old = getattr(current_user, f) or ""
                db.session.add(ChangeRequest(user_id=current_user.id, field_name=f,
                                              old_value=str(old), new_value=val))
    db.session.commit()
    flash("Profile updated.", "success")
    return redirect(url_for("ess.index"))


ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def _avatar_dir():
    """Per-company avatar folder: uploads/<company_id>/avatars/.

    Isolated per company so one tenant can never address another's images;
    the serve route falls back to the legacy flat uploads/avatars/ folder
    for pre-multi-company files.
    """
    cid = current_company_id()
    if cid is None:
        raise RuntimeError("No active company for avatar upload")
    d = os.path.join(current_app.config["UPLOAD_FOLDER"], str(cid), "avatars")
    os.makedirs(d, exist_ok=True)
    return d


def _avatar_candidates(filename):
    """(new per-company path, legacy path) for an avatar filename."""
    cid = current_company_id()
    if cid:
        yield os.path.join(current_app.config["UPLOAD_FOLDER"],
                           str(cid), "avatars", filename)
    yield os.path.join(current_app.config["UPLOAD_FOLDER"], "avatars", filename)


@ess_bp.route("/upload-picture", methods=["POST"])
@login_required
def upload_picture():
    file = request.files.get("profile_picture")
    if not file or file.filename == "":
        flash("No file selected.", "danger")
        return redirect(url_for("ess.index"))
    if not allowed_file(file.filename):
        flash("Allowed image types: PNG, JPG, JPEG, GIF, WebP.", "danger")
        return redirect(url_for("ess.index"))
    ext = file.filename.rsplit(".", 1)[1].lower()
    filename = f"avatar_{current_user.id}_{int(datetime.utcnow().timestamp())}.{ext}"
    upload_dir = _avatar_dir()
    file.save(os.path.join(upload_dir, filename))
    # Delete old avatar from disk (per-company folder, then legacy folder)
    if current_user.profile_image:
        for old in _avatar_candidates(current_user.profile_image):
            if os.path.exists(old):
                os.remove(old)
    current_user.profile_image = filename
    db.session.commit()
    flash("Profile picture updated.", "success")
    return redirect(url_for("ess.index"))


@ess_bp.route("/change-requests")
@login_required
def change_requests():
    if not current_user.is_admin():
        flash("Access denied.", "danger")
        return redirect(url_for("dashboard"))
    pending = ChangeRequest.query.filter_by(status="pending").order_by(ChangeRequest.created_at.desc()).all()
    return render_template("ess/change_requests.html", requests=pending)


@ess_bp.route("/review-change/<int:cid>", methods=["POST"])
@login_required
def review_change(cid):
    if not current_user.is_admin():
        return jsonify({"error": "Access denied"}), 403
    cr = scoped_get_404(ChangeRequest, cid)
    action = request.form.get("action")
    notes = request.form.get("notes", "")
    if action == "approve":
        user = get_member(cr.user_id)
        setattr(user, cr.field_name, cr.new_value)
        cr.status = "approved"
    else:
        cr.status = "rejected"
    cr.reviewed_by = current_user.id
    cr.review_notes = notes
    cr.reviewed_at = datetime.utcnow()
    db.session.commit()
    flash(f"Change request {action}d.", "success")
    return redirect(url_for("ess.change_requests"))


@ess_bp.route("/loans", methods=["GET", "POST"])
@login_required
def loans():
    if request.method == "POST":
        loan = LoanAdvanceRequest(
            user_id=current_user.id,
            request_type=request.form.get("type", "loan"),
            amount=float(request.form["amount"]),
            purpose=request.form["purpose"],
            installment_months=int(request.form.get("installments", 12)),
        )
        loan.monthly_installment = round(loan.amount / loan.installment_months, 2)
        loan.remaining_amount = loan.amount
        db.session.add(loan)
        db.session.flush()
        from shared.ledger_utils import create_entity_account
        create_entity_account("loan", loan.id,
                              f"{loan.request_type.title()} #{loan.id} - {current_user.full_name}")
        if current_user.manager_id:
            notif = Notification(title="Loan Request", message=f"{current_user.full_name} requests a loan of Rs.{loan.amount:,.0f}",
                                 notification_type="info", module="loans", reference_id=loan.id, created_by=current_user.id)
            db.session.add(notif)
            db.session.flush()
            db.session.add(NotificationRecipient(notification_id=notif.id, user_id=current_user.manager_id))
        db.session.commit()
        flash("Loan request submitted.", "success")
        return redirect(url_for("ess.index"))
    loans = LoanAdvanceRequest.query.filter_by(user_id=current_user.id).order_by(LoanAdvanceRequest.created_at.desc()).all()
    return render_template("ess/loans.html", loans=loans)


@ess_bp.route("/slips")
@login_required
def slips():
    slips = PayrollSlip.query.filter_by(user_id=current_user.id).order_by(PayrollSlip.created_at.desc()).all()
    return render_template("ess/slips.html", slips=slips)


@ess_bp.route("/avatar/<filename>")
@login_required
def avatar(filename):
    for path in _avatar_candidates(filename):
        if os.path.isfile(path):
            try:
                return send_file(path)
            except Exception:
                return "", 404
    return "", 404


@ess_bp.route("/performance")
@login_required
def performance():
    reviews = PerformanceReview.query.filter_by(user_id=current_user.id).order_by(PerformanceReview.created_at.desc()).all()
    goals = PerformanceGoal.query.filter_by(user_id=current_user.id).order_by(PerformanceGoal.created_at.desc()).all()
    return render_template("ess/performance.html", reviews=reviews, goals=goals)
