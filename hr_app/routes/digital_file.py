import os
from datetime import datetime, date, timedelta
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
from ..config import Config

df_bp = Blueprint("digital_files", __name__, url_prefix="/digital-files")


def _ensure_upload_dir():
    """Per-company, per-user upload directory: uploads/<company_id>/<user_id>/.

    Files live under the company's own subfolder so one company's admins can
    never address another company's files from disk.
    """
    cid = current_company_id()
    if cid is None:
        raise RuntimeError("No active company for file upload")
    d = os.path.join(Config.UPLOAD_FOLDER, str(cid), str(current_user.id))
    os.makedirs(d, exist_ok=True)
    return d


def _resolve_path(f):
    """Absolute path of a DigitalFile's blob.

    Looks in the per-company folder first (uploads/<company_id>/<user_id>/);
    falls back to the legacy flat folder (uploads/) so files uploaded before
    multi-company keep working.
    """
    cid = f.company_id
    if cid:
        p = os.path.join(Config.UPLOAD_FOLDER, str(cid),
                         str(f.user_id), f.filename)
        if os.path.exists(p):
            return p
    return os.path.join(Config.UPLOAD_FOLDER, f.filename)


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
    upload_dir = _ensure_upload_dir()
    import uuid
    unique = f"{uuid.uuid4().hex}.{ext}"
    f.save(os.path.join(upload_dir, unique))
    df = DigitalFile(
        user_id=current_user.id,
        category_id=request.form.get("category_id", type=int),
        title=request.form.get("title", f.filename),
        filename=unique,
        original_name=f.filename,
        file_size=os.path.getsize(os.path.join(upload_dir, unique)),
        mime_type=f.content_type,
        notes=request.form.get("notes", ""),
        expiry_date=datetime.strptime(request.form["expiry_date"], "%Y-%m-%d").date() if request.form.get("expiry_date") else None,
    )
    db.session.add(df)
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
    path = _resolve_path(f)
    if not os.path.exists(path):
        abort(404)
    return send_file(path, download_name=f.original_name)


@df_bp.route("/delete/<int:fid>", methods=["POST"])
@login_required
def delete(fid):
    f = scoped_get_404(DigitalFile, fid)
    if f.user_id != current_user.id and not current_user.is_admin():
        return jsonify({"error": "Access denied"}), 403
    fpath = _resolve_path(f)
    if os.path.exists(fpath):
        os.remove(fpath)
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
