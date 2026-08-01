"""Super admin portal — global (cross-company) administration.

All tables touched here (companies, company_memberships, global_limits,
users, roles) are GLOBAL: no tenancy scoping, no unscoped() needed.
"""
import random
import re
import string

from datetime import datetime

from flask import (Blueprint, abort, flash, redirect, render_template,
                   request, url_for)
from flask_login import current_user, login_required, login_user

from shared.extensions import db
from shared.models.base import Role, User
from shared.models.company import (Company, CompanyMembership,
                                   GlobalLimits)
from shared.permissions import MODULES

superadmin_bp = Blueprint("superadmin", __name__, url_prefix="/superadmin")

# Same rule the portal enforces, so a company address means one thing however
# it was created.
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,60}$")


def _require_super_admin():
    if not current_user.is_authenticated or not current_user.is_super_admin:
        abort(403)


@superadmin_bp.route("/login", methods=["GET", "POST"])
def login():
    """Separate door to the platform console. Same credentials as /auth/login
    — there is one user table — but it refuses anyone without the super admin
    flag and lands straight on the console instead of a company's books.

    A tenant who finds this URL and signs in correctly is still told the same
    thing as a wrong password: confirming "your password is right, you are just
    not a super admin" would turn this page into a super-admin oracle.
    """
    if current_user.is_authenticated and current_user.is_super_admin:
        return redirect(url_for("superadmin.index"))
    if request.method == "POST":
        login_id = (request.form.get("login") or "").strip()
        password = request.form.get("password", "")
        user = (User.query.filter(
            db.func.lower(User.login_id) == login_id.lower()).first()
            or User.query.filter_by(email=login_id.lower()).first())
        if (user and user.check_password(password)
                and user.is_active and user.is_super_admin):
            login_user(user)
            user.last_login = datetime.utcnow()
            db.session.commit()
            return redirect(url_for("superadmin.index"))
        flash("Those credentials do not open the super admin console.",
              "error")
    return render_template("superadmin/login.html")


def _random_password(length=10):
    alphabet = string.ascii_letters + string.digits
    return "".join(random.SystemRandom().choice(alphabet)
                   for _ in range(length))


def _next_employee_code():
    n = User.query.count() + 1
    while User.query.filter_by(employee_code=f"SA{n:04d}").first():
        n += 1
    return f"SA{n:04d}"


def _active_member_count(company_id):
    return CompanyMembership.query.filter_by(
        company_id=company_id, status=CompanyMembership.ACTIVE).count()


@superadmin_bp.route("/", methods=["GET", "POST"])
@login_required
def index():
    _require_super_admin()
    limits = GlobalLimits.get()
    if request.method == "POST":
        try:
            limits.max_companies_per_user = max(
                1, int(request.form.get("max_companies_per_user") or 3))
            limits.default_max_members = max(
                1, int(request.form.get("default_max_members") or 25))
        except (TypeError, ValueError):
            flash("Limits must be whole numbers.", "error")
            return redirect(url_for("superadmin.index"))
        limits.updated_by = current_user.id
        db.session.commit()
        flash("Global limits updated.", "success")
        return redirect(url_for("superadmin.index"))
    return render_template(
        "superadmin/index.html",
        limits=limits,
        company_count=Company.query.count(),
        user_count=User.query.count(),
        membership_count=CompanyMembership.query.count(),
    )


@superadmin_bp.route("/companies/", methods=["GET", "POST"])
@login_required
def companies():
    _require_super_admin()
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        slug = request.form.get("slug", "").strip().lower()
        plan = request.form.get("plan_name", "").strip() or "free"
        email = request.form.get("admin_email", "").strip().lower()
        if not name or not slug or not email:
            flash("Company name, slug and admin email are required.", "error")
            return redirect(url_for("superadmin.companies"))
        if Company.query.filter_by(slug=slug).first():
            flash(f"A company with slug '{slug}' already exists.", "error")
            return redirect(url_for("superadmin.companies"))
        admin_role = Role.query.filter_by(name=Role.ADMIN).first()
        if admin_role is None:
            flash("Admin role is not seeded yet.", "error")
            return redirect(url_for("superadmin.companies"))
        admin = User.query.filter_by(email=email).first()
        created_user = False
        if admin is None:
            password = request.form.get("password", "").strip() or _random_password()
            admin = User(
                employee_code=_next_employee_code(),
                email=email,
                login_id=email,
                full_name=request.form.get("admin_name", "").strip()
                or email.split("@")[0],
                role_id=admin_role.id,
                is_active=True,
                has_hr_access=True,
                has_inventory_access=True,
                has_invoicing_access=True,
                has_finance_access=True,
                has_accounting_access=True,
                has_fbr_access=True,
                has_fixed_assets_access=True,
            )
            admin.set_password(password)
            db.session.add(admin)
            created_user = True
        company = Company(name=name, slug=slug, plan_name=plan,
                          is_active=True, created_by=current_user.id)
        db.session.add(company)
        db.session.flush()
        membership = CompanyMembership.query.filter_by(
            company_id=company.id, user_id=admin.id).first()
        if membership is None:
            db.session.add(CompanyMembership(
                company_id=company.id, user_id=admin.id,
                role_id=admin_role.id, status=CompanyMembership.ACTIVE,
                employee_code=admin.employee_code))
        else:
            membership.status = CompanyMembership.ACTIVE
            membership.role_id = admin_role.id
        db.session.commit()
        msg = (f"Company '{name}' created. Admin: {email}.")
        if created_user:
            msg += f" New user — password: {password}"
        flash(msg, "success")
        return redirect(url_for("superadmin.companies"))
    # `mine` marks companies this super admin created, so their own books are
    # visible in the platform-wide list without being mistaken for a tenant's.
    rows = [(c, _active_member_count(c.id), c.created_by == current_user.id)
            for c in Company.query.order_by(Company.name).all()]
    return render_template("superadmin/companies.html", companies=rows)


@superadmin_bp.route("/companies/<int:company_id>/", methods=["GET", "POST"])
@login_required
def company_edit(company_id):
    _require_super_admin()
    company = Company.query.get(company_id)
    if company is None:
        abort(404)
    if request.method == "POST":
        company.name = request.form.get("name", "").strip() or company.name
        company.plan_name = request.form.get("plan_name", "").strip() or "free"
        mm = request.form.get("max_members", "").strip()
        try:
            company.max_members = int(mm) if mm else None
        except (TypeError, ValueError):
            flash("Max members must be a whole number.", "error")
            return redirect(url_for("superadmin.company_edit",
                                    company_id=company.id))
        company.is_active = request.form.get("is_active") == "on"
        company.address = request.form.get("address", "") or None
        company.city = request.form.get("city", "") or None
        company.country = request.form.get("country", "") or "Pakistan"
        company.phone = request.form.get("phone", "") or None
        company.email = request.form.get("email", "") or None
        company.website = request.form.get("website", "") or None
        company.tax_id = request.form.get("tax_id", "") or None
        company.registration_number = \
            request.form.get("registration_number", "") or None
        company.logo_url = request.form.get("logo_url", "") or None
        company.currency = request.form.get("currency", "") or "PKR"
        company.currency_symbol = request.form.get("currency_symbol", "") or "Rs."
        # Module entitlement. Absent checkbox = off, so this form is the whole
        # truth for the company's modules every time it is saved.
        for key, col in Company.MODULE_COLUMNS.items():
            setattr(company, col, request.form.get(f"mod_{key}") == "on")
        db.session.commit()
        flash("Company updated.", "success")
        return redirect(url_for("superadmin.company_edit",
                                company_id=company.id))
    members = [(m, User.query.get(m.user_id))
               for m in CompanyMembership.query
               .filter_by(company_id=company.id)
               .order_by(CompanyMembership.status,
                         CompanyMembership.joined_at).all()]
    return render_template(
        "superadmin/company_edit.html",
        company=company,
        members=members,
        limits=GlobalLimits.get(),
        active_members=_active_member_count(company.id),
        modules=MODULES,
        blocked_status=CompanyMembership.BLOCKED,
    )


@superadmin_bp.route("/companies/<int:company_id>/members/<int:membership_id>"
                     "/block", methods=["POST"])
@login_required
def member_block(company_id, membership_id):
    """Block or unblock one person's access to one company.

    The membership is kept — role, employee code and history survive — and
    only its status flips. Every gate in the app filters on ACTIVE, so a
    blocked member simply stops being able to enter.
    """
    _require_super_admin()
    m = CompanyMembership.query.get(membership_id)
    if m is None or m.company_id != company_id:
        abort(404)
    user = User.query.get(m.user_id)
    if m.status == CompanyMembership.BLOCKED:
        m.status = CompanyMembership.ACTIVE
        msg = "unblocked"
    elif m.status == CompanyMembership.ACTIVE:
        m.status = CompanyMembership.BLOCKED
        msg = "blocked"
    else:
        flash("Only an active or blocked membership can be toggled.", "error")
        return redirect(url_for("superadmin.company_edit",
                                company_id=company_id))
    db.session.commit()
    flash(f"{user.full_name if user else 'Member'} {msg}.", "success")
    return redirect(url_for("superadmin.company_edit", company_id=company_id))


@superadmin_bp.route("/my-companies/", methods=["GET", "POST"])
@login_required
def my_companies():
    """The super admin's own books — the same flow every other user gets,
    in super-admin chrome. Creating here provisions the company exactly as
    the portal does; entering one goes through the shared company_switch."""
    _require_super_admin()
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        slug = request.form.get("slug", "").strip().lower()
        if not name or not SLUG_RE.match(slug or ""):
            flash("Name is required, and the address must be 2-62 characters: "
                  "lowercase letters, digits and hyphens.", "error")
            return redirect(url_for("superadmin.my_companies"))
        if Company.query.filter_by(slug=slug).first():
            flash("That company address is already taken.", "error")
            return redirect(url_for("superadmin.my_companies"))
        admin_role = Role.query.filter_by(name=Role.ADMIN).first()
        if admin_role is None:
            flash("Admin role is not seeded yet.", "error")
            return redirect(url_for("superadmin.my_companies"))
        company = Company(name=name, slug=slug, is_active=True,
                          plan_name="free", created_by=current_user.id)
        db.session.add(company)
        db.session.flush()
        db.session.add(CompanyMembership(
            company_id=company.id, user_id=current_user.id,
            role_id=admin_role.id, status=CompanyMembership.ACTIVE))
        db.session.commit()

        from shared.company_setup import provision_company
        provision_company(company.id)
        flash(f"Company '{company.name}' created — its books are ready.",
              "success")
        return redirect(url_for("superadmin.my_companies"))

    mine = (CompanyMembership.query
            .filter_by(user_id=current_user.id,
                       status=CompanyMembership.ACTIVE).all())
    rows = []
    for m in mine:
        c = Company.query.get(m.company_id)
        if c is not None:
            rows.append((c, m, c.created_by == current_user.id))
    rows.sort(key=lambda r: r[0].name)
    return render_template("superadmin/my_companies.html", rows=rows)


@superadmin_bp.route("/users/", methods=["GET", "POST"])
@login_required
def users():
    """Manage Users — every account on the platform, and the only place one
    is created. HR assigns existing members; Settings invites registered
    emails; neither can bring a new person into existence."""
    _require_super_admin()
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        name = request.form.get("full_name", "").strip()
        password = request.form.get("password", "").strip()
        if not email or not password:
            flash("Email and password are required.", "error")
            return redirect(url_for("superadmin.users"))
        if User.query.filter_by(email=email).first():
            flash(f"A user with {email} already exists.", "error")
            return redirect(url_for("superadmin.users"))
        emp_role = Role.query.filter_by(name=Role.EMPLOYEE).first()
        if emp_role is None:
            flash("Roles are not seeded yet.", "error")
            return redirect(url_for("superadmin.users"))
        # users.employee_code is globally unique and NOT NULL, so an account
        # needs one before it belongs anywhere. This is a placeholder; the
        # real per-company code is set by HR's Assign Member.
        u = User(employee_code=_next_employee_code(), email=email,
                 login_id=email, full_name=name or email.split("@")[0],
                 role_id=emp_role.id, is_active=True, has_hr_access=True)
        u.set_password(password)
        db.session.add(u)
        db.session.commit()
        flash(f"User {u.full_name} created. Invite them into a company from "
              f"that company's Settings.", "success")
        return redirect(url_for("superadmin.user_detail", user_id=u.id))

    rows = [(u, u.memberships.filter_by(
        status=CompanyMembership.ACTIVE).count())
        for u in User.query.order_by(User.email).all()]
    return render_template("superadmin/users.html", users=rows,
                           limits=GlobalLimits.get())


@superadmin_bp.route("/users/<int:user_id>/", methods=["GET", "POST"])
@login_required
def user_detail(user_id):
    """One user: their password, quotas, active state and companies."""
    _require_super_admin()
    user = User.query.get(user_id)
    if user is None:
        abort(404)
    if request.method == "POST":
        action = request.form.get("action", "profile")
        if action == "password":
            pw = request.form.get("new_password", "").strip()
            if len(pw) < 4:
                flash("Password must be at least 4 characters.", "error")
            else:
                user.set_password(pw)
                db.session.commit()
                flash(f"Password reset for {user.full_name}.", "success")
        elif action == "active":
            if user.id == current_user.id:
                flash("You cannot deactivate your own account.", "error")
            else:
                user.is_active = not user.is_active
                db.session.commit()
                flash(f"{user.full_name} is now "
                      f"{'active' if user.is_active else 'deactivated'}.",
                      "success")
        else:
            # Blank means "use the global default", so empty string -> NULL
            # rather than 0, which would forbid everything.
            for field in ("max_companies_owned", "max_companies_joined"):
                raw = request.form.get(field, "").strip()
                if not raw:
                    setattr(user, field, None)
                    continue
                try:
                    setattr(user, field, max(0, int(raw)))
                except (TypeError, ValueError):
                    flash("Limits must be whole numbers.", "error")
                    return redirect(url_for("superadmin.user_detail",
                                            user_id=user.id))
            user.full_name = request.form.get(
                "full_name", "").strip() or user.full_name
            db.session.commit()
            flash("User updated.", "success")
        return redirect(url_for("superadmin.user_detail", user_id=user.id))

    memberships = [(m, Company.query.get(m.company_id))
                   for m in user.memberships.order_by(
                       CompanyMembership.status).all()]
    limits = GlobalLimits.get()
    return render_template(
        "superadmin/user_detail.html",
        user=user, memberships=memberships, limits=limits,
        owned=Company.query.filter_by(created_by=user.id).count(),
        effective_owned=limits.company_limit_for(user),
        effective_joined=limits.join_limit_for(user))


@superadmin_bp.route("/users/<int:user_id>/toggle-super", methods=["POST"])
@login_required
def toggle_super(user_id):
    _require_super_admin()
    user = User.query.get(user_id)
    if user is None:
        abort(404)
    if user.id == current_user.id and user.is_super_admin:
        flash("You cannot remove your own super admin rights.", "error")
        return redirect(url_for("superadmin.users"))
    user.is_super_admin = not user.is_super_admin
    db.session.commit()
    flash(f"{user.full_name or user.email} is "
          f"{'now' if user.is_super_admin else 'no longer'} a super admin.",
          "success")
    return redirect(url_for("superadmin.users"))
