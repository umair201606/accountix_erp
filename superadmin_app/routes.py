"""Super admin portal — global (cross-company) administration.

All tables touched here (companies, company_memberships, global_limits,
users, roles) are GLOBAL: no tenancy scoping, no unscoped() needed.
"""
import random
import string

from flask import (Blueprint, abort, flash, redirect, render_template,
                   request, url_for)
from flask_login import current_user, login_required

from shared.extensions import db
from shared.models.base import Role, User
from shared.models.company import (Company, CompanyMembership,
                                   GlobalLimits)

superadmin_bp = Blueprint("superadmin", __name__, url_prefix="/superadmin")


def _require_super_admin():
    if not current_user.is_authenticated or not current_user.is_super_admin:
        abort(403)


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
    rows = [(c, _active_member_count(c.id))
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
    )


@superadmin_bp.route("/users/")
@login_required
def users():
    _require_super_admin()
    rows = [(u, u.memberships.filter_by(
        status=CompanyMembership.ACTIVE).count())
        for u in User.query.order_by(User.email).all()]
    return render_template("superadmin/users.html", users=rows)


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
