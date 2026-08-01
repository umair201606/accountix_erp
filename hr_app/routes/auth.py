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
    from shared.routes.portal import should_redirect_to_portal
    if current_user.is_authenticated:
        return redirect(url_for("portal.index")
                        if should_redirect_to_portal()
                        else url_for("dashboard.hub"))
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
            if next_page:
                return redirect(next_page)
            return redirect(url_for("portal.index")
                            if should_redirect_to_portal()
                            else url_for("dashboard.hub"))
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


# HR does not mint logins. A person becomes a user of this company by being
# invited in Settings and accepting — that is the only path that binds an
# account to a company, and the only one where the person consents. HR's job
# starts after that: turning an existing member into an employee record.
#
# The old /users/add created a global User with no CompanyMembership at all,
# so the person vanished from the very list they were added to and could not
# reach any books. It is gone rather than patched.


def _unassigned_members():
    """Active members of this company who are not yet employees here.

    Membership carries the employee code (uq_membership_company_emp_code), so
    "assigned" means that code is set — a person can be EMP001 in one company
    and something else in another without the two colliding.
    """
    from shared.models.company import CompanyMembership
    from shared.tenancy import current_company_id
    cid = current_company_id()
    if cid is None:
        return []
    admin_role = Role.query.filter_by(name=Role.ADMIN).first()
    q = (db.session.query(User, CompanyMembership)
         .join(CompanyMembership, db.and_(
             CompanyMembership.user_id == User.id,
             CompanyMembership.company_id == cid,
             CompanyMembership.status == CompanyMembership.ACTIVE))
         .filter(db.or_(CompanyMembership.employee_code.is_(None),
                        CompanyMembership.employee_code == "")))
    if admin_role:
        q = q.filter(CompanyMembership.role_id != admin_role.id)
    return q.order_by(User.full_name).all()


@auth_bp.route("/members/assign", methods=["GET", "POST"])
@login_required
def member_assign():
    """Assign an existing company member their employee details."""
    if not _can_manage_users():
        return redirect(url_for("dashboard"))
    from shared.models.company import CompanyMembership
    from shared.tenancy import current_company_id, get_member

    def _page():
        return render_template(
            "auth/member_assign.html",
            pending=_unassigned_members(),
            managers=User.employees().filter(
                User.role_obj.has(name=Role.MANAGER)).all())

    if request.method == "POST":
        uid = request.form.get("user_id", type=int)
        emp_code = request.form.get("employee_code", "").strip()
        u = get_member(uid) if uid else None
        if u is None:
            flash("That person is not a member of this company.", "danger")
            return _page()
        if not emp_code:
            flash("Employee code is required.", "danger")
            return _page()
        cid = current_company_id()
        m = CompanyMembership.query.filter_by(
            company_id=cid, user_id=u.id,
            status=CompanyMembership.ACTIVE).first()
        if m is None:
            flash("That person is not an active member of this company.",
                  "danger")
            return _page()
        # Unique per company, not globally — the same code may be in use by a
        # different person in a different company.
        clash = CompanyMembership.query.filter(
            CompanyMembership.company_id == cid,
            CompanyMembership.employee_code == emp_code,
            CompanyMembership.user_id != u.id).first()
        if clash:
            flash(f"Employee code {emp_code} is already used in this company.",
                  "danger")
            return _page()

        m.employee_code = emp_code
        u.designation = request.form.get("designation", "").strip()
        u.department = request.form.get("department", "").strip()
        u.phone = request.form.get("phone", "").strip() or u.phone
        u.manager_id = request.form.get("manager_id", type=int) or None
        if not u.employee_code:
            # Legacy global column, still read by payroll and attendance.
            u.employee_code = emp_code
        if not u.date_of_joining:
            u.date_of_joining = datetime.utcnow().date()
        db.session.flush()
        from shared.ledger_utils import create_entity_account
        create_entity_account("employee", u.id,
                              f"{u.full_name} ({emp_code})")
        db.session.commit()
        flash(f"{u.full_name} assigned as {emp_code} — employee ledger "
              f"account added.", "success")
        return redirect(url_for("auth.user_list"))

    return _page()


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
