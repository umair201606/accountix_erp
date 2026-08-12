"""E2E coverage for the multi-company (tenancy) features.

Follows the harness pattern of test_settings_access.py: a throwaway sqlite
DB seeded through the app's own ``_seed_all_data`` (first request), with
requests driven through Flask's test client and setup rows (users, companies,
memberships, products) created directly with ``db.session`` inside the test.

Covers:
  - the company-less user 500 regression (inject_notifications must not trip
    the fail-closed tenancy hook when the user has no active company)
  - the super admin portal (create company + admin, 403 for non-super-admins,
    toggle-super, global limits)
  - the company switcher (session switch, refusal for foreign companies)
  - the invite -> accept -> remove lifecycle (quota, membership, session
    hand-off, removal makes the user company-less)
  - cross-company isolation: one tenant can never read another tenant's
    scoped rows (product list + PK lookup)
"""
import os
import tempfile
import uuid

import pytest

# Point at a throwaway DB before app import — importing app builds the engine.
# Mirrors test_routes_smoke.py / test_settings_access.py so this file also
# runs standalone.
_TMP_DB = os.path.join(tempfile.gettempdir(), "erp_multicompany_e2e.db")
os.environ.setdefault("DATABASE_URL", "sqlite:///" + _TMP_DB.replace("\\", "/"))

from app import app as flask_app  # noqa: E402


def _suffix():
    return uuid.uuid4().hex[:10]


# ── Setup helpers (direct DB writes inside the test's app context) ──────────

def _uid(email_like):
    from shared.models.base import User
    with flask_app.app_context():
        u = User.query.filter(User.email.ilike(email_like)).first()
        assert u is not None, f"seed produced no user matching {email_like}"
        return u.id


def _default_company_id():
    from shared.models.company import Company
    with flask_app.app_context():
        c = Company.query.filter_by(slug="default").first()
        assert c is not None, "no seeded default company"
        return c.id


def _login(email, password):
    """Log in through the real /auth/login POST and follow the redirect to
    the hub, which is what establishes session["company_id"] via
    _set_company_context (before_request runs pre-login on the POST)."""
    c = flask_app.test_client()
    c.post("/auth/login", data={"login": email, "password": password},
           follow_redirects=True)
    with c.session_transaction() as sess:
        assert sess.get("_user_id"), f"login failed for {email}"
    return c


def _login_super_admin(email, password):
    """Super admins are refused by the regular form; use their own door."""
    c = flask_app.test_client()
    c.post("/superadmin/login", data={"login": email, "password": password},
           follow_redirects=True)
    with c.session_transaction() as sess:
        assert sess.get("_user_id"), f"super admin login failed for {email}"
    return c


def _create_user(email, full_name=None, password="pw12345", role="employee"):
    """Create a user row directly. Returns the user id. No membership is
    created — callers decide which companies (if any) the user belongs to."""
    from shared.extensions import db
    from shared.models.base import Role, User
    with flask_app.app_context():
        r = Role.query.filter_by(name=role).first()
        u = User(
            employee_code=f"MC{_suffix()}".upper(),
            email=email,
            login_id=email,
            full_name=full_name or email.split("@")[0],
            role_id=r.id,
            is_active=True,
            has_hr_access=True,
            has_inventory_access=True,
            has_invoicing_access=True,
            has_finance_access=True,
            has_accounting_access=True,
            has_fbr_access=True,
            has_fixed_assets_access=True,
        )
        u.set_password(password)
        db.session.add(u)
        db.session.commit()
        return u.id


def _create_company(name, slug, max_members=None, created_by=None):
    from shared.extensions import db
    from shared.models.company import Company
    with flask_app.app_context():
        c = Company(name=name, slug=slug, is_active=True,
                    max_members=max_members, created_by=created_by)
        db.session.add(c)
        db.session.commit()
        return c.id


def _add_membership(company_id, user_id, role="employee", status="active"):
    from shared.extensions import db
    from shared.models.base import Role
    from shared.models.company import CompanyMembership
    with flask_app.app_context():
        r = Role.query.filter_by(name=role).first()
        m = CompanyMembership(company_id=company_id, user_id=user_id,
                              role_id=r.id, status=status)
        db.session.add(m)
        db.session.commit()
        return m.id


@pytest.fixture(scope="module")
def seeded():
    # The temp DB file is shared across runs (and with the other test-client
    # modules in this suite), so wipe it before seeding: state leaked from an
    # earlier aborted run (e.g. members accepted past the company quota) must
    # not decide the outcome of this run.
    with flask_app.app_context():
        from shared.extensions import db
        db.drop_all()
        flask_app._db_initialized = False
    flask_app.test_client().get("/")  # lazy create_all + migrate + seed


# ── Defect regression: company-less user must not 500 ───────────────────────

def test_companyless_user_renders_pages_without_500(seeded):
    """A logged-in user with ZERO memberships has no active company; every
    page must still render (inject_notifications skips the tenant-scoped
    query instead of tripping NoActiveCompanyError)."""
    email = f"noless_{_suffix()}@example.com"
    _create_user(email)
    c = _login(email, "pw12345")

    with c.session_transaction() as sess:
        assert "company_id" not in sess
    # The hub belongs to a company, so with none it sends the user to pick
    # one. What this test guards is that nothing 500s on the way.
    resp = c.get("/dashboard/")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/portal/")
    assert c.get("/dashboard/", follow_redirects=True).status_code == 200
    assert c.get("/settings/").status_code == 200


# ── Super admin portal ──────────────────────────────────────────────────────

def test_superadmin_portal_access_and_company_creation(seeded):
    from shared.models.base import User
    from shared.models.company import Company, GlobalLimits

    # Super admin (seeded) can open the portal.
    super_ = _login_super_admin("admin@gmail.com", "admin123")
    assert super_.get("/superadmin/").status_code == 200

    # Create a company with a brand-new admin user.
    slug = f"acme-{_suffix()}"
    admin_email = f"acmeadmin_{_suffix()}@example.com"
    admin_pw = "AcmePass1!"
    resp = super_.post("/superadmin/companies/", data={
        "name": "Acme Corp", "slug": slug, "plan_name": "free",
        "admin_email": admin_email, "admin_name": "Acme Admin",
        "password": admin_pw,
    })
    assert resp.status_code == 302

    with flask_app.app_context():
        acme = Company.query.filter_by(slug=slug).first()
        assert acme is not None, "company was not created via the portal"
        assert acme.name == "Acme Corp"
        assert acme.created_by is not None, "creator-lock needs created_by"
        acme_id = acme.id
        admin_u = User.query.filter_by(email=admin_email).first()
        assert admin_u is not None and admin_u.check_password(admin_pw)

    # The portal's own company-edit page opens for the super admin.
    assert super_.get(f"/superadmin/companies/{acme_id}/").status_code == 200

    # A plain company admin is denied the portal (403, not a redirect).
    acme_admin = _login(admin_email, admin_pw)
    assert acme_admin.get("/superadmin/").status_code == 403
    assert acme_admin.get("/superadmin/companies/").status_code == 403
    assert acme_admin.get("/superadmin/users/").status_code == 403

    # The company list shows the new company.
    resp = super_.get("/superadmin/companies/")
    assert resp.status_code == 200
    assert b"Acme Corp" in resp.data

    # toggle-super on a normal (seeded) user — on and back off.
    john_uid = _uid("john.doe@solarkon.com")
    super_.post(f"/superadmin/users/{john_uid}/toggle-super")
    with flask_app.app_context():
        assert User.query.get(john_uid).is_super_admin is True
    super_.post(f"/superadmin/users/{john_uid}/toggle-super")
    with flask_app.app_context():
        assert User.query.get(john_uid).is_super_admin is False

    # Global limits update.
    with flask_app.app_context():
        limits = GlobalLimits.get()
        original = (limits.max_companies_per_user, limits.default_max_members)
    try:
        super_.post("/superadmin/", data={
            "max_companies_per_user": "5", "default_max_members": "50"})
        with flask_app.app_context():
            assert GlobalLimits.get().max_companies_per_user == 5
            assert GlobalLimits.get().default_max_members == 50
    finally:
        with flask_app.app_context():
            limits = GlobalLimits.get()
            limits.max_companies_per_user = original[0]
            limits.default_max_members = original[1]
            from shared.extensions import db
            db.session.commit()


# ── Company switcher ────────────────────────────────────────────────────────

def test_company_switch_sets_session_and_refuses_foreign_company(seeded):
    acme_id = _create_company("Switch Co", f"switch-{_suffix()}")
    admin_email = f"swadmin_{_suffix()}@example.com"
    admin_uid = _create_user(admin_email, role="admin")
    _add_membership(acme_id, admin_uid, role="admin")

    c = _login(admin_email, "pw12345")
    with c.session_transaction() as sess:
        assert sess.get("company_id") == acme_id, \
            "login should land in the user's only active company"

    # Switch explicitly, with a next target.
    resp = c.get(f"/company/switch/{acme_id}?next=/settings/")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/settings/")
    with c.session_transaction() as sess:
        assert sess.get("company_id") == acme_id
    assert c.get("/settings/").status_code == 200

    # Switching to a company the user has NO membership in is refused and
    # leaves the session company untouched.
    default_id = _default_company_id()
    assert default_id != acme_id
    resp = c.get(f"/company/switch/{default_id}")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/dashboard/"), \
        "refused switch must bounce to the hub"
    with c.session_transaction() as sess:
        assert sess.get("company_id") == acme_id, \
            "refused switch must not change the active company"


# ── Invite -> accept -> remove lifecycle ────────────────────────────────────

def test_invite_accept_remove_lifecycle(seeded):
    from shared.models.base import User
    from shared.models.company import CompanyInvitation, CompanyMembership

    default_id = _default_company_id()
    admin = _login_super_admin("admin@gmail.com", "admin123")

    # 1. Inviting a user who is ALREADY a member is refused (spec).
    with flask_app.app_context():
        before = CompanyInvitation.query.filter_by(
            company_id=default_id, email="john.doe@solarkon.com").count()
    admin.post("/settings/invite", data={
        "email": "john.doe@solarkon.com", "role": "employee"})
    with flask_app.app_context():
        after = CompanyInvitation.query.filter_by(
            company_id=default_id, email="john.doe@solarkon.com").count()
        assert after == before, "already-a-member invite must not create a row"

    # 2. A brand-new registered user can be invited.
    inv_email = f"invitee_{_suffix()}@example.com"
    inv_name = f"Invitee {_suffix()[:4]}"
    inv_uid = _create_user(inv_email, full_name=inv_name)
    admin.post("/settings/invite", data={
        "email": inv_email, "role": "employee"})
    with flask_app.app_context():
        inv = CompanyInvitation.query.filter_by(
            company_id=default_id, email=inv_email,
            status=CompanyInvitation.SENT).first()
        assert inv is not None, "invitation was not sent"
        inv_id = inv.id

    # 3. The invitee sees the invitation and accepts it.
    c = _login(inv_email, "pw12345")
    resp = c.get("/settings/invitations/")
    assert resp.status_code == 200
    assert b"Accept" in resp.data
    assert b"Default Company" in resp.data

    resp = c.post(f"/settings/invitations/{inv_id}/accept")
    assert resp.status_code == 302
    with c.session_transaction() as sess:
        assert sess.get("company_id") == default_id, \
            "accepting must switch the session to the inviting company"
    with flask_app.app_context():
        m = CompanyMembership.query.filter_by(
            company_id=default_id, user_id=inv_uid).first()
        assert m is not None and m.status == CompanyMembership.ACTIVE
        assert m.role_name() == "employee"
        membership_id = m.id

    # 4. The admin's members page shows the new member.
    resp = admin.get("/settings/members/")
    assert resp.status_code == 200
    assert inv_name.encode() in resp.data

    # 5. Admin removes the member; the membership becomes removed.
    resp = admin.post(f"/settings/members/{membership_id}/remove")
    assert resp.status_code == 302
    with flask_app.app_context():
        m = CompanyMembership.query.get(membership_id)
        assert m.status == CompanyMembership.REMOVED

    # 6. The removed member is company-less again: no switcher entries, no
    #    active company, and pages still render (no 500).
    c2 = _login(inv_email, "pw12345")
    with c2.session_transaction() as sess:
        assert "company_id" not in sess
    with flask_app.app_context():
        u = User.query.get(inv_uid)
        assert u.active_companies() == []
    assert c2.get("/dashboard/", follow_redirects=True).status_code == 200


# ── Cross-company isolation (the core tenancy guarantee) ────────────────────

def test_cross_company_product_isolation(seeded):
    """Company B (Acme) must never see company A's (Default) scoped rows,
    even through a direct PK lookup, and A must still see them afterwards."""
    from shared.extensions import db
    from shared.tenancy import unscoped
    from inventory_app.models.product import InvProduct

    default_id = _default_company_id()
    acme_id = _create_company("Isolation Co", f"iso-{_suffix()}")
    acme_admin_email = f"isoadmin_{_suffix()}@example.com"
    acme_admin_uid = _create_user(acme_admin_email, role="admin")
    _add_membership(acme_id, acme_admin_uid, role="admin")

    # Company A's own product. company_id is set explicitly, so the write is
    # unscoped (test setup); a commit then expires the row and any later
    # attribute reload must not trip the fail-closed tenancy hook.
    with flask_app.app_context(), unscoped():
        p = InvProduct(
            sku=f"ISO-PROD-{_suffix()}", name="ISO Isolation Product",
            company_id=default_id, unit_price=10, cost_price=5,
            current_stock=0, reorder_level=0, unit="pcs", is_active=True)
        db.session.add(p)
        db.session.commit()
        p_id = p.id

    # Company B admin: switch to B, list products, try the edit page of A's
    # product id.
    b_admin = _login(acme_admin_email, "pw12345")
    assert b_admin.get(f"/company/switch/{acme_id}").status_code == 302
    resp = b_admin.get("/inventory/products/")
    assert resp.status_code == 200
    assert b"ISO-PROD" not in resp.data, \
        "company B product list must not contain company A's SKU"
    resp = b_admin.get(f"/inventory/products/edit/{p_id}")
    assert resp.status_code == 404, \
        "company B must not resolve company A's product id"

    # Company A admin: the same product is still visible in A.
    a_admin = _login_super_admin("admin@gmail.com", "admin123")
    resp = a_admin.get("/inventory/products/")
    assert resp.status_code == 200
    assert b"ISO-PROD" in resp.data, \
        "company A must still see its own product after B's checks"
