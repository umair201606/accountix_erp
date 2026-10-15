from datetime import datetime
from flask import Blueprint, render_template, request, jsonify, flash, redirect, url_for
from flask_login import login_required, current_user
from ..extensions import db
from ..models.communication import Notification, NotificationRecipient, EmailLog
from ..models.user import User

comm_bp = Blueprint("communications", __name__, url_prefix="/communications")


@comm_bp.route("/")
@login_required
def index():
    if not current_user.is_admin():
        my_notifs = NotificationRecipient.query.filter_by(user_id=current_user.id).order_by(
            NotificationRecipient.id.desc()).limit(50).all()
        return render_template("communications/index.html", notifications=my_notifs)
    all_notifs = Notification.query.order_by(Notification.created_at.desc()).limit(50).all()
    email_logs = EmailLog.query.order_by(EmailLog.sent_at.desc()).limit(20).all()
    return render_template("communications/admin.html", notifications=all_notifs, email_logs=email_logs)


@comm_bp.route("/send-notification", methods=["POST"])
@login_required
def send_notification():
    if not current_user.is_admin():
        return jsonify({"error": "Access denied"}), 403
    title = request.form.get("title", "").strip()
    message = request.form.get("message", "").strip()
    ntype = request.form.get("type", "info")
    target = request.form.get("target", "all")
    if not title or not message:
        flash("Title and message required.", "danger")
        return redirect(url_for("communications.index"))
    notif = Notification(title=title, message=message, notification_type=ntype,
                         module="communications", created_by=current_user.id)
    db.session.add(notif)
    db.session.flush()
    if target == "all":
        users = User.employees().filter(User.is_active.is_(True)).all()
    elif target == "role":
        # Roles belong to the company membership (the user row's role is a
        # global default): filter on the role held in THIS company.
        from shared.models.base import Role
        from shared.models.company import CompanyMembership
        role = Role.query.filter_by(name=request.form.get("role")).first()
        users = (User.employees().filter(User.is_active.is_(True),
                                         CompanyMembership.role_id == role.id).all()
                 if role else [])
    else:
        users = []
    for u in users:
        db.session.add(NotificationRecipient(notification_id=notif.id, user_id=u.id))
    db.session.commit()
    flash(f"Notification sent to {len(users)} users.", "success")
    return redirect(url_for("communications.index"))


@comm_bp.route("/send-email", methods=["POST"])
@login_required
def send_email():
    if not current_user.is_admin():
        return jsonify({"error": "Access denied"}), 403
    recipient = request.form.get("recipient", "").strip()
    subject = request.form.get("subject", "").strip()
    body = request.form.get("body", "").strip()
    if not recipient or not subject:
        flash("Recipient and subject required.", "danger")
        return redirect(url_for("communications.index"))
    # No mail server is configured, so nothing is sent: the log says so
    # instead of recording the message as "sent".
    log = EmailLog(recipient=recipient, subject=subject, body=body, module="manual",
                   status="not_sent")
    db.session.add(log)
    db.session.commit()
    flash("Email logged (SMTP not configured for live send).", "info")
    return redirect(url_for("communications.index"))
