from datetime import datetime
from flask import Blueprint, render_template, redirect, url_for, request, flash, jsonify, abort
from flask_login import login_user, logout_user, login_required, current_user
from ..extensions import db, csrf
from shared.tenancy import get_member
from ..models.user import User, Role
from ..models.communication import Notification, NotificationRecipient

auth_bp = Blueprint("auth", __name__, url_prefix="/auth")


def _require_admin():
    if not current_user.is_admin():
        flash("Access denied.", "danger")
        return False
    return True


def _can_manage_users():
    """HR (managers with HR access) can view and add users; admin can do
    everything including rights, edits and deletion."""
    if current_user.is_admin():
        return True
    if current_user.is_manager() and current_user.has_hr_access:
        return True
    flash("Access denied.", "danger")
    return False


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.hub"))
    if request.method == "POST":
        # Users sign in with their User ID; the email still works as a
        # fallback because login_id defaults to the email.
        login_id = (request.form.get("login") or request.form.get("email") or "").strip()
        password = request.form.get("password", "")
        user = (User.query.filter(db.func.lower(User.login_id) == login_id.lower()).first()
                or User.query.filter_by(email=login_id.lower()).first())
        if user and user.check_password(password):
            if not user.is_active:
                flash("Your account has been deactivated.", "danger")
                return render_template("login.html")
            login_user(user)
            user.last_login = datetime.utcnow()
            db.session.commit()
            next_page = request.args.get("next")
            return redirect(next_page or url_for("dashboard.hub"))
        flash("Invalid email or password.", "danger")
    return render_template("login.html")


@auth_bp.route("/logout")
@login_required
def logout():
    logout_user()
    flash("You have been logged out.", "info")
    return redirect(url_for("auth.login"))

@auth_bp.route("/profile")
@login_required
def profile():
    return render_template("ess/profile.html", user=current_user)


@auth_bp.route("/api/notifications")
@login_required
def api_notifications():
    recipients = NotificationRecipient.query.filter_by(
        user_id=current_user.id, is_read=False
    ).order_by(NotificationRecipient.id.desc()).limit(20).all()
    return jsonify([{
        "id": r.id,
        "title": r.notification.title,
        "message": r.notification.message,
        "type": r.notification.notification_type,
        "module": r.notification.module,
        "created_at": r.notification.created_at.isoformat()
    } for r in recipients])


@auth_bp.route("/api/notifications/<int:nid>/read", methods=["POST"])
@csrf.exempt
@login_required
def mark_notification_read(nid):
    r = NotificationRecipient.query.filter_by(id=nid, user_id=current_user.id).first()
    if r:
        r.is_read = True
        r.read_at = datetime.utcnow()
        db.session.commit()
    return jsonify({"success": True})


@auth_bp.route("/api/notifications/read-all", methods=["POST"])
@csrf.exempt
@login_required
def mark_all_read():
    NotificationRecipient.query.filter_by(
        user_id=current_user.id, is_read=False
    ).update({"is_read": True, "read_at": datetime.utcnow()})
    db.session.commit()
    return jsonify({"success": True})


# ── Account Settings (self-service): User ID + email ──

@auth_bp.route("/account-settings", methods=["POST"])
@login_required
def account_settings():
    login_id = request.form.get("login_id", "").strip()
    email = request.form.get("email", "").strip().lower()
    if not login_id or not email:
        flash("User ID and email are both required.", "danger")
        return redirect(url_for("auth.profile"))
    if User.query.filter(db.func.lower(User.login_id) == login_id.lower(),
                         User.id != current_user.id).first():
        flash("That User ID is already taken.", "danger")
        return redirect(url_for("auth.profile"))
    if User.query.filter(User.email == email, User.id != current_user.id).first():
        flash("That email is already in use.", "danger")
        return redirect(url_for("auth.profile"))
    current_user.login_id = login_id
    current_user.email = email
    db.session.commit()
    flash("Account settings saved — sign in with your User ID from now on.", "success")
    return redirect(url_for("auth.profile"))


# ── Password Change (self-service) ──

@auth_bp.route("/change-password", methods=["GET", "POST"])
@login_required
def change_password():
    if request.method == "POST":
        current_pw = request.form.get("current_password", "")
        new_pw = request.form.get("new_password", "")
        confirm_pw = request.form.get("confirm_password", "")
        if not current_user.check_password(current_pw):
            flash("Current password is incorrect.", "danger")
            return redirect(url_for("auth.change_password"))
        if len(new_pw) < 4:
            flash("New password must be at least 4 characters.", "danger")
            return redirect(url_for("auth.change_password"))
        if new_pw != confirm_pw:
            flash("New passwords do not match.", "danger")
            return redirect(url_for("auth.change_password"))
        current_user.set_password(new_pw)
        db.session.commit()
        flash("Password changed successfully.", "success")
        return redirect(url_for("dashboard.hub"))
    return render_template("auth/change_password.html")


# ── Admin: User Management ──

@auth_bp.route("/users")
@login_required
def user_list():
    if not _can_manage_users():
        return redirect(url_for("dashboard"))
    users = User.employees().order_by(User.full_name).all()
    roles = Role.query.all()
    return render_template("auth/user_list.html", users=users, roles=roles)


@auth_bp.route("/users/add", methods=["GET", "POST"])
@login_required
def user_add():
    if not _can_manage_users():
        return redirect(url_for("dashboard"))
    roles = Role.query.all()
    managers = User.query.filter(User.role_obj.has(name=Role.MANAGER)).all()
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        emp_code = request.form.get("employee_code", "").strip()
        full_name = request.form.get("full_name", "").strip()
        password = request.form.get("password", "")
        role_id = request.form.get("role_id", type=int)
        manager_id = request.form.get("manager_id", type=int)
        designation = request.form.get("designation", "").strip()
        department = request.form.get("department", "").strip()
        phone = request.form.get("phone", "").strip()
        if not all([email, emp_code, full_name, password, role_id]):
            flash("Email, Employee Code, Name, Password, and Role are required.", "danger")
            return render_template("auth/user_form.html", roles=roles, managers=managers, user=None)
        if User.query.filter_by(email=email).first():
            flash("Email already exists.", "danger")
            return render_template("auth/user_form.html", roles=roles, managers=managers, user=None)
        if User.query.filter_by(employee_code=emp_code).first():
            flash("Employee code already exists.", "danger")
            return render_template("auth/user_form.html", roles=roles, managers=managers, user=None)
        admin_role = Role.query.filter_by(name=Role.ADMIN).first()
        if not current_user.is_admin() and admin_role and role_id == admin_role.id:
            flash("Only admin can create admin accounts.", "danger")
            return render_template("auth/user_form.html", roles=roles, managers=managers, user=None)
        # New users always get HR (self-service) access so they can log in and
        # land on a working hub — without this the hub showed "access denied".
        # Further module access & rights are granted by admin in Settings.
        u = User(employee_code=emp_code, email=email, login_id=email,
                 full_name=full_name,
                 role_id=role_id, manager_id=manager_id or None,
                 designation=designation, department=department, phone=phone,
                 date_of_joining=datetime.utcnow().date(), is_active=True,
                 has_hr_access=True)
        u.set_password(password)
        db.session.add(u)
        db.session.flush()
        from shared.ledger_utils import create_entity_account
        create_entity_account("employee", u.id, f"{full_name} ({emp_code})")
        db.session.commit()
        flash(f"User {full_name} created — employee ledger account added. "
              f"Admin can set module access & rights in Settings.", "success")
        return redirect(url_for("auth.user_list"))
    return render_template("auth/user_form.html", roles=roles, managers=managers, user=None)


@auth_bp.route("/users/<int:uid>/edit", methods=["GET", "POST"])
@login_required
def user_edit(uid):
    if not _can_manage_users():
        return redirect(url_for("dashboard"))
    u = get_member(uid) or abort(404)
    if u.is_admin() and not current_user.is_admin():
        flash("Admin accounts are managed from ERP hub Settings.", "danger")
        return redirect(url_for("auth.user_list"))
    roles = Role.query.all()
    managers = User.query.filter(User.role_obj.has(name=Role.MANAGER), User.id != uid).all()
    if request.method == "POST":
        u.email = request.form.get("email", u.email).strip().lower()
        u.full_name = request.form.get("full_name", u.full_name).strip()
        new_role_id = int(request.form.get("role_id", u.role_id))
        admin_role = Role.query.filter_by(name=Role.ADMIN).first()
        # HR must never be able to promote anyone to admin.
        if not current_user.is_admin() and admin_role and new_role_id == admin_role.id:
            flash("Only admin can assign the admin role.", "danger")
            return redirect(url_for("auth.user_edit", uid=uid))
        u.role_id = new_role_id
        u.manager_id = request.form.get("manager_id", type=int) or None
        u.designation = request.form.get("designation", "").strip()
        u.department = request.form.get("department", "").strip()
        u.phone = request.form.get("phone", "").strip()
        u.is_active = request.form.get("is_active") == "1"
        password = request.form.get("password", "")
        if password:
            if len(password) < 4:
                flash("Password must be at least 4 characters.", "danger")
                return render_template("auth/user_form.html", roles=roles, managers=managers, user=u)
            u.set_password(password)
        db.session.commit()
        flash(f"User {u.full_name} updated.", "success")
        return redirect(url_for("auth.user_list"))
    return render_template("auth/user_form.html", roles=roles, managers=managers, user=u)


@auth_bp.route("/users/<int:uid>/delete", methods=["POST"])
@login_required
def user_delete(uid):
    if not _can_manage_users():
        return redirect(url_for("dashboard"))
    u = get_member(uid) or abort(404)
    if u.id == current_user.id:
        flash("Cannot delete yourself.", "danger")
        return redirect(url_for("auth.user_list"))
    if u.is_admin():
        flash("Cannot delete admin users.", "danger")
        return redirect(url_for("auth.user_list"))
    u.is_active = False
    db.session.commit()
    flash(f"User {u.full_name} deactivated.", "success")
    return redirect(url_for("auth.user_list"))


@auth_bp.route("/users/<int:uid>/hr-rights", methods=["GET", "POST"])
@login_required
def user_hr_rights(uid):
    """HR-scoped rights: HR managers may grant/restrict HR sections only.
    Rights for other modules are assigned exclusively by admin in Settings."""
    if not _can_manage_users():
        return redirect(url_for("dashboard"))
    u = get_member(uid) or abort(404)
    if u.is_admin():
        flash("Admin accounts are managed from ERP hub Settings.", "danger")
        return redirect(url_for("auth.user_list"))
    from shared.permissions import MODULES, ACTIONS
    from shared.models.base import UserPermission
    hr_sections = next(sections for key, _l, _f, sections in MODULES if key == "hr")

    if request.method == "POST":
        hr_keys = [r for r, _ in hr_sections]
        UserPermission.query.filter(
            UserPermission.user_id == u.id,
            UserPermission.resource.in_(hr_keys)).delete(synchronize_session=False)
        for resource, _label in hr_sections:
            db.session.add(UserPermission(
                user_id=u.id, resource=resource,
                **{f"can_{a}": request.form.get(f"perm_{resource}_{a}") == "on"
                   for a in ACTIONS}))
        db.session.commit()
        flash(f"HR rights saved for {u.full_name}.", "success")
        return redirect(url_for("auth.user_list"))

    perms = {p.resource: p for p in UserPermission.query.filter_by(user_id=u.id).all()}
    is_configured = any(r in perms for r, _ in hr_sections)
    return render_template("auth/user_hr_rights.html",
                           u=u, sections=hr_sections, actions=ACTIONS,
                           perms=perms, is_configured=is_configured)


@auth_bp.route("/users/<int:uid>/reset-password", methods=["POST"])
@login_required
def user_reset_password(uid):
    if not _require_admin():
        return redirect(url_for("dashboard"))
    u = get_member(uid) or abort(404)
    new_pw = request.form.get("new_password", "")
    if len(new_pw) < 4:
        flash("Password must be at least 4 characters.", "danger")
        return redirect(url_for("auth.user_list"))
    u.set_password(new_pw)
    db.session.commit()
    flash(f"Password reset for {u.full_name}.", "success")
    return redirect(url_for("auth.user_list"))
