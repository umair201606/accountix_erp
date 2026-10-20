from datetime import datetime, date, timedelta
from io import BytesIO
from flask import Blueprint, render_template, request, jsonify, flash, redirect, url_for, send_file, abort
from flask_login import login_required, current_user
from ..extensions import db
from shared.tenancy import scoped_get_404, get_member, current_company_id
from ..models.digital_file import DigitalFile, FileCategory
from ..models.user import User
from ..models.attendance import Attendance
from ..models.timesheet import TimesheetWeek
from ..models.compensation import PayrollSlip
from ..models.performance import PerformanceReview
from ..models.communication import Notification, NotificationRecipient
from shared import file_store

df_bp = Blueprint("digital_files", __name__, url_prefix="/digital-files")


def _file_key(f):
    """Storage key of a DigitalFile's bytes inside its company.

    Same per-user layout the upload folder had (<user_id>/<file>), so the
    legacy-disk fallback in file_store finds files uploaded before the
    database store, and one user's files never collide with another's.
    """
    return f"{f.user_id}/{f.filename}"


@df_bp.route("/")
@login_required
def index():
    files = DigitalFile.query.filter_by(user_id=current_user.id).order_by(DigitalFile.uploaded_at.desc()).all()
    categories = FileCategory.query.all()
    now = date.today()
    expiring = [f for f in files if f.expiry_date and f.expiry_date <= now + timedelta(days=30) and f.expiry_date > now]
    return render_template("digital_files/index.html", files=files, categories=categories, expiring=expiring, now=now)


@df_bp.route("/upload", methods=["POST"])
@login_required
def upload():
    if "file" not in request.files:
        flash("No file selected.", "danger")
        return redirect(url_for("digital_files.index"))
    f = request.files["file"]
    if f.filename == "":
        flash("No file selected.", "danger")
        return redirect(url_for("digital_files.index"))
    allowed = {"pdf", "png", "jpg", "jpeg"}
    ext = f.filename.rsplit(".", 1)[-1].lower() if "." in f.filename else ""
    if ext not in allowed:
        flash("Only PDF, PNG, JPG files allowed.", "danger")
        return redirect(url_for("digital_files.index"))
    if current_company_id() is None:
        flash("Open a company before uploading files.", "danger")
        return redirect(url_for("digital_files.index"))
    import uuid
    from shared.tenancy import scoped_get
    unique = f"{uuid.uuid4().hex}.{ext}"
    # A category id from another company (or none at all) was stored as typed.
    category_id = request.form.get("category_id", type=int)
    if category_id and scoped_get(FileCategory, category_id) is None:
        category_id = None
    try:
        expiry = (datetime.strptime(request.form["expiry_date"], "%Y-%m-%d").date()
                  if request.form.get("expiry_date") else None)
    except ValueError:
        flash("Enter the expiry date as YYYY-MM-DD.", "danger")
        return redirect(url_for("digital_files.index"))
    data = f.read()
    df = DigitalFile(
        # Always the uploader's own file: nobody files documents into
        # another person's record from here.
        user_id=current_user.id,
        category_id=category_id,
        title=request.form.get("title") or f.filename,
        filename=unique,
        original_name=f.filename,
        file_size=len(data),
        mime_type=f.content_type,
        notes=request.form.get("notes", ""),
        expiry_date=expiry,
    )
    db.session.add(df)
    file_store.save(_file_key(df), data, f.content_type)
    db.session.commit()
    flash("File uploaded.", "success")
    return redirect(url_for("digital_files.index"))


@df_bp.route("/download/<int:fid>")
@login_required
def download(fid):
    f = scoped_get_404(DigitalFile, fid)
    if f.user_id != current_user.id and not current_user.is_admin():
        flash("Access denied.", "danger")
        return redirect(url_for("digital_files.index"))
    stored = file_store.read(_file_key(f), company_id=f.company_id)
    if stored is None:
        abort(404)
    data, content_type = stored
    return send_file(BytesIO(data), download_name=f.original_name,
                     mimetype=f.mime_type or content_type, as_attachment=True)


@df_bp.route("/delete/<int:fid>", methods=["POST"])
@login_required
def delete(fid):
    f = scoped_get_404(DigitalFile, fid)
    if f.user_id != current_user.id and not current_user.is_admin():
        flash("You can delete only your own documents.", "danger")
        return redirect(url_for("digital_files.index"))
    file_store.delete(_file_key(f), company_id=f.company_id)
    db.session.delete(f)
    db.session.commit()
    flash("File deleted.", "success")
    return redirect(url_for("digital_files.index"))


@df_bp.route("/admin")
@login_required
def admin():
    if not current_user.is_admin():
        flash("Access denied.", "danger")
        return redirect(url_for("dashboard"))
    files = DigitalFile.query.order_by(DigitalFile.uploaded_at.desc()).all()
    now = date.today()
    expired = [f for f in files if f.expiry_date and f.expiry_date < now]
    expiring = [f for f in files if f.expiry_date and f.expiry_date <= now + timedelta(days=30) and f.expiry_date >= now]
    return render_template("digital_files/admin.html", files=files, expired=expired, expiring=expiring, now=now)


@df_bp.route("/verify/<int:fid>", methods=["POST"])
@login_required
def verify(fid):
    if not current_user.is_admin():
        return jsonify({"error": "Access denied"}), 403
    f = scoped_get_404(DigitalFile, fid)
    f.is_verified = True
    f.verified_by = current_user.id
    f.verified_at = datetime.utcnow()
    db.session.commit()
    return jsonify({"success": True})


@df_bp.route("/check-expiry")
@login_required
def check_expiry():
    if not current_user.is_admin():
        return jsonify({"error": "Access denied"}), 403
    now = date.today()
    warning = now + timedelta(days=30)
    expiring = DigitalFile.query.filter(
        DigitalFile.expiry_date != None,
        DigitalFile.expiry_date <= warning,
        DigitalFile.expiry_date >= now
    ).all()
    expired = DigitalFile.query.filter(
        DigitalFile.expiry_date != None,
        DigitalFile.expiry_date < now
    ).all()
    return jsonify({
        "expiring_count": len(expiring),
        "expired_count": len(expired),
        "expiring": [{"id": f.id, "title": f.title, "user": f.user.full_name, "expiry": str(f.expiry_date)} for f in expiring],
        "expired": [{"id": f.id, "title": f.title, "user": f.user.full_name, "expiry": str(f.expiry_date)} for f in expired],
    })


@df_bp.route("/profile/<int:uid>")
@login_required
def profile(uid):
    if not current_user.is_admin() and current_user.id != uid:
        flash("Access denied.", "danger")
        return redirect(url_for("dashboard"))
    emp = get_member(uid) or abort(404)
    files = DigitalFile.query.filter_by(user_id=uid).order_by(DigitalFile.uploaded_at.desc()).all()
    attendance_count = Attendance.query.filter_by(user_id=uid).count()
    timesheet_count = TimesheetWeek.query.filter_by(user_id=uid, status="approved").count()
    slip_count = PayrollSlip.query.filter_by(user_id=uid).count()
    review_count = PerformanceReview.query.filter_by(user_id=uid).count()
    return render_template("digital_files/profile.html", emp=emp, files=files,
                           attendance_count=attendance_count, timesheet_count=timesheet_count,
                           slip_count=slip_count, review_count=review_count)
