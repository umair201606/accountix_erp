from io import BytesIO
from datetime import datetime, date
from flask import Blueprint, render_template, request, jsonify, flash, redirect, url_for, send_file
from flask_login import login_required, current_user
from ..extensions import db
from werkzeug.utils import secure_filename
from shared import file_store
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
    from .. import leave_flow
    year = date.today().year
    for lt in LeaveType.query.all():
        leave_flow.refresh(leave_flow.quota_for(user.id, lt, year))
    db.session.commit()
    quotas = LeaveQuota.query.filter_by(user_id=user.id, year=year).all()
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


def _avatar_key(filename):
    """Storage key of an avatar inside the active company (file_store keeps
    it in the database; legacy uploads/<company_id>/avatars/ and the flat
    uploads/avatars/ folder are still read for pre-existing pictures)."""
    return f"avatars/{filename}"


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
    if current_company_id() is None:
        flash("Open a company before uploading a picture.", "danger")
        return redirect(url_for("ess.index"))
    file_store.save(_avatar_key(filename), file.read(), file.mimetype)
    if current_user.profile_image and current_user.profile_image != filename:
        file_store.delete(_avatar_key(current_user.profile_image))
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
    cr = scoped_get_404(ChangeRequest, cid, for_update=True)
    action = request.form.get("action")
    notes = request.form.get("notes", "")
    if cr.status != "pending":
        flash("This change request has already been reviewed.", "danger")
        return redirect(url_for("ess.change_requests"))
    if action == "approve":
        user = get_member(cr.user_id)
        if user is None:
            flash("That person is no longer a member of this company.", "danger")
            return redirect(url_for("ess.change_requests"))
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
        # A zero instalment count divided by zero (500); a negative amount
        # was accepted as a request.
        try:
            amount = round(float(request.form.get("amount") or 0), 2)
            months = int(request.form.get("installments") or 12)
        except ValueError:
            amount, months = 0, 0
        purpose = (request.form.get("purpose") or "").strip()
        rtype = request.form.get("type", "loan")
        if amount <= 0 or not (1 <= months <= 120) or not purpose                 or rtype not in ("loan", "advance"):
            flash("Enter an amount above zero, 1–120 instalments and a purpose.", "danger")
            return redirect(url_for("ess.loans"))
        loan = LoanAdvanceRequest(
            user_id=current_user.id,
            request_type=rtype,
            amount=amount,
            purpose=purpose,
            installment_months=months,
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
    stored = file_store.read(_avatar_key(secure_filename(filename)))
    if stored is None:
        return "", 404
    data, content_type = stored
    return send_file(BytesIO(data), mimetype=content_type or "image/png")


@ess_bp.route("/performance")
@login_required
def performance():
    reviews = PerformanceReview.query.filter_by(user_id=current_user.id).order_by(PerformanceReview.created_at.desc()).all()
    goals = PerformanceGoal.query.filter_by(user_id=current_user.id).order_by(PerformanceGoal.created_at.desc()).all()
    return render_template("ess/performance.html", reviews=reviews, goals=goals)
