"""User portal: the landing page where a global user sees the companies he
created, the companies where other users assigned him a role, and the
invitations awaiting his acceptance. Clicking a company enters its books.

Global (non-scoped) queries only: companies, memberships, invitations and
limits are tenancy machinery tables.
"""
import re

from flask import (Blueprint, flash, redirect, render_template, request,
                   session, url_for)
from flask_login import current_user, login_required

from shared.extensions import db
from shared.models.base import Role
from shared.models.company import (Company, CompanyInvitation,
                                   CompanyMembership, GlobalLimits)
from shared.tenancy import set_current_company

portal_bp = Blueprint("portal", __name__, url_prefix="/portal")

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,60}$")


def should_redirect_to_portal():
    """Login landing decision: portal when the user has no active company,
    several companies, or pending invitations — otherwise the hub."""
    if not current_user.is_authenticated:
        return False
    active = current_user.active_companies()
    if len(active) != 1:
        return True
    pending = CompanyInvitation.query.filter_by(
        email=current_user.email,
        status=CompanyInvitation.SENT).count()
    return pending > 0


def _role_name(role_id):
    r = Role.query.get(role_id) if role_id else None
    return r.name if r else ""


def _membership_company(membership):
    return Company.query.get(membership.company_id)


@portal_bp.route("/")
@login_required
def index():
    owned = Company.query.filter_by(
        created_by=current_user.id).order_by(Company.name).all()
    memberships = CompanyMembership.query.filter_by(
        user_id=current_user.id).order_by(
        CompanyMembership.status).all()
    invites = CompanyInvitation.query.filter_by(
        email=current_user.email,
        status=CompanyInvitation.SENT).all()
    limits = GlobalLimits.get()
    return render_template(
        "portal/index.html",
        owned=owned,
        memberships=memberships,
        invites=invites,
        role_name=_role_name,
        membership_company=_membership_company,
        quota_total=limits.max_companies_per_user,
        quota_used=len(owned),
    )


@portal_bp.route("/create", methods=["POST"])
@login_required
def create():
    name = request.form.get("name", "").strip()
    slug = request.form.get("slug", "").strip().lower()
    if not name:
        flash("Company name is required.", "error")
        return redirect(url_for("portal.index"))
    if not slug or not SLUG_RE.match(slug):
        flash("Company address must be 2-62 characters: lowercase "
              "letters, digits and hyphens only.", "error")
        return redirect(url_for("portal.index"))
    if Company.query.filter_by(slug=slug).first():
        flash("That company address is already taken.", "error")
        return redirect(url_for("portal.index"))
    limits = GlobalLimits.get()
    if current_user.owns_company_count() >= limits.max_companies_per_user:
        flash(f"You have reached your company creation limit "
              f"({limits.max_companies_per_user}).", "error")
        return redirect(url_for("portal.index"))

    company = Company(name=name, slug=slug, is_active=True,
                      plan_name="free", created_by=current_user.id)
    db.session.add(company)
    db.session.flush()
    admin_role = Role.query.filter_by(name=Role.ADMIN).first()
    if admin_role:
        db.session.add(CompanyMembership(
            company_id=company.id, user_id=current_user.id,
            role_id=admin_role.id, status=CompanyMembership.ACTIVE))
    db.session.commit()

    from shared.company_setup import provision_company
    provision_company(company.id)

    set_current_company(company.id)
    session["company_id"] = company.id
    flash(f"Company '{company.name}' created — its books are ready.", "success")
    return redirect(url_for("dashboard.hub"))
