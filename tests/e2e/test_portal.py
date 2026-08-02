"""E2E coverage for the user company portal (/portal/).

Follows the harness pattern of test_multicompany.py: a throwaway sqlite DB
seeded through the app's own ``_seed_all_data`` (first request), setup rows
written directly with ``db.session``, flows driven through the Flask test
client.

Covers:
  - login landing: multi-company / zero-company / pending-invite users go to
    the portal; single-company users go straight to the hub
  - the portal lists companies the user created and companies where other
    users assigned them a role, plus pending invitations
  - a global user creates a company from the portal; the company's books are
    provisioned (chart, settings, periods, voucher numbering) and the
    session switches into it
  - creation quotas, slug uniqueness and validation
  - invitation accept/decline from the portal returns to the portal
  - the Super Admin portal is linked for super admins only
"""
import os
import tempfile
import uuid

import pytest

_TMP_DB = os.path.join(tempfile.gettempdir(), "erp_portal_e2e.db")
os.environ.setdefault("DATABASE_URL", "sqlite:///" + _TMP_DB.replace("\\", "/"))

from app import app as flask_app  # noqa: E402


def _suffix():
    return uuid.uuid4().hex[:10]


def _uid(email_like):
    from shared.models.base import User
    with flask_app.app_context():
        u = User.query.filter(User.email.ilike(email_like)).first()
        assert u is not None, f"seed produced no user matching {email_like}"
        return u.id


def _create_user(email, full_name=None, password="pw12345", role="employee"):
    from shared.extensions import db
    from shared.models.base import Role, User
    with flask_app.app_context():
        r = Role.query.filter_by(name=role).first()
        u = User(
            employee_code=f"PT{_suffix()}".upper(),
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


def _create_company(name, slug, created_by=None):
    from shared.extensions import db
    from shared.models.company import Company
    with flask_app.app_context():
        c = Company(name=name, slug=slug, is_active=True, created_by=created_by)
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


def _login(email, password):
    """Log in without following the redirect, so tests can assert the
    landing target."""
    c = flask_app.test_client()
    resp = c.post("/auth/login", data={"login": email, "password": password})
    assert resp.status_code == 302, f"login failed for {email}"
    with c.session_transaction() as sess:
        assert sess.get("_user_id"), f"login failed for {email}"
    return c, resp


@pytest.fixture(scope="module")
def seeded():
    with flask_app.app_context():
        from shared.extensions import db
        db.drop_all()
        flask_app._db_initialized = False
    flask_app.test_client().get("/")  # lazy create_all + migrate + seed


# ── Login landing ───────────────────────────────────────────────────────────

def test_login_lands_on_portal_for_multi_company_user(seeded):
    """A user with several companies (one created, one assigned) lands on
    the portal after login; a single-company user goes straight to the hub."""
    email = f"multi_{_suffix()}@example.com"
    uid = _create_user(email)
    owned_id = _create_company("Mine Co", f"mine-{_suffix()}", created_by=uid)
    other_id = _create_company("Theirs Co", f"theirs-{_suffix()}")
    _add_membership(owned_id, uid, role="admin")
    _add_membership(other_id, uid, role="manager")

    c, resp = _login(email, "pw12345")
    assert resp.headers["Location"].endswith("/portal/"), \
        "multi-company user must land on the portal"

    page = c.get("/portal/")
    assert page.status_code == 200
    assert b"Mine Co" in page.data
    assert b"Theirs Co" in page.data


def test_login_lands_on_portal_for_companyless_user(seeded):
    email = f"zero_{_suffix()}@example.com"
    _create_user(email)
    c, resp = _login(email, "pw12345")
    assert resp.headers["Location"].endswith("/portal/"), \
        "company-less user must land on the portal (no company to enter)"
    assert c.get("/portal/").status_code == 200


@pytest.mark.parametrize("target", [
    "https://evil.example/phishing",
    "//evil.example/phishing",
    "/\\evil.example/phishing",
])
def test_login_rejects_unsafe_next_redirect(seeded, target):
    email = f"redirect_{_suffix()}@example.com"
    _create_user(email)

    c = flask_app.test_client()
    resp = c.post(
        "/auth/login",
        data={"login": email, "password": "pw12345"},
        query_string={"next": target},
    )

    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/portal/")
    assert "evil.example" not in resp.headers["Location"]


def test_login_allows_local_next_redirect(seeded):
    email = f"local_redirect_{_suffix()}@example.com"
    _create_user(email)

    c = flask_app.test_client()
    resp = c.post(
        "/auth/login?next=/portal/",
        data={"login": email, "password": "pw12345"},
    )

    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/portal/")


def test_login_lands_on_portal_even_with_one_company(seeded):
    """No shortcut for single-company users. Skipping the picker made login
    mean two different things depending on a count the user cannot see, and
    dropped them into a company's books without ever naming it."""
    c, resp = _login("emp@solarkon.com", "emp123")
    assert resp.headers["Location"].endswith("/portal/")
    assert c.get("/portal/").status_code == 200


def test_default_company_is_owned_by_the_seeded_admin(seeded):
    """The admin's own company was filed under "companies where I have a
    role" because the seed left created_by NULL — as if somebody else had
    made the one company they actually run."""
    from shared.models.base import User
    from shared.models.company import Company
    with flask_app.app_context():
        sysadmin = User.query.filter_by(employee_code="SYSADMIN").first()
        assert sysadmin is not None, "seed produced no SYSADMIN"
        default = Company.query.filter_by(slug="default").first()
        assert default.created_by == sysadmin.id, \
            "the default company must record its admin as its creator"

    c, _ = _login("admin@gmail.com", "admin123")
    page = c.get("/portal/")
    assert page.status_code == 200
    body = page.data.decode()
    created_at = body.index("Companies I created")
    role_at = body.index("Companies where I have a role")
    assert body.index("Default Company") < role_at, \
        "Default Company must be listed under the companies the admin created"
    assert created_at < body.index("Default Company")


def test_login_lands_on_portal_when_invite_pending(seeded):
    email = f"invited_{_suffix()}@example.com"
    uid = _create_user(email)
    default_id = _default_company_id()
    _add_membership(default_id, uid, role="employee")
    from shared.extensions import db
    from shared.models.base import Role
    from shared.models.company import CompanyInvitation
    with flask_app.app_context():
        r = Role.query.filter_by(name="manager").first()
        inv = CompanyInvitation(company_id=default_id, email=email,
                                role_id=r.id, status="sent")
        db.session.add(inv)
        db.session.commit()

    c, resp = _login(email, "pw12345")
    assert resp.headers["Location"].endswith("/portal/"), \
        "pending invitation must route login to the portal"


# ── Portal content ──────────────────────────────────────────────────────────

def test_portal_shows_created_assigned_and_invitations(seeded):
    email = f"view_{_suffix()}@example.com"
    uid = _create_user(email)
    owned_id = _create_company("Owned View Co", f"oview-{_suffix()}",
                               created_by=uid)
    _add_membership(owned_id, uid, role="admin")
    other_id = _create_company("Assigned View Co", f"aview-{_suffix()}")
    _add_membership(other_id, uid, role="manager")

    from shared.extensions import db
    from shared.models.base import Role
    from shared.models.company import CompanyInvitation
    with flask_app.app_context():
        r = Role.query.filter_by(name="employee").first()
        inv = CompanyInvitation(company_id=other_id, email=email,
                                role_id=r.id, status="sent")
        db.session.add(inv)
        db.session.commit()

    c, _ = _login(email, "pw12345")
    page = c.get("/portal/")
    assert page.status_code == 200
    assert b"Owned View Co" in page.data
    assert b"Assigned View Co" in page.data
    assert b"Accept" in page.data and b"Decline" in page.data
    assert b"Open books" in page.data


# ── Creation + provisioning ─────────────────────────────────────────────────

def test_user_creates_company_and_books_are_provisioned(seeded):
    from shared.models.company import Company
    email = f"creator_{_suffix()}@example.com"
    uid = _create_user(email)
    slug = f"brandnew-{_suffix()}"

    c, _ = _login(email, "pw12345")
    resp = c.post("/portal/create", data={"name": "Brand New Co", "slug": slug})
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/dashboard/")

    with flask_app.app_context():
        company = Company.query.filter_by(slug=slug).first()
        assert company is not None, "company was not created from the portal"
        assert company.created_by == uid, "creator must be recorded"
        new_id = company.id

        from shared.models.company import CompanyMembership
        m = CompanyMembership.query.filter_by(
            company_id=new_id, user_id=uid).first()
        assert m is not None and m.status == CompanyMembership.ACTIVE
        from shared.models.base import Role
        assert m.role_id == Role.query.filter_by(name=Role.ADMIN).first().id, \
            "creator must be the company admin"

        # The session must switch into the new company.
        with c.session_transaction() as sess:
            assert sess.get("company_id") == new_id
        assert c.get("/dashboard/").status_code == 200

        # Books provisioned, scoped to the new company.
        from shared.tenancy import set_current_company
        set_current_company(new_id)
        from shared.models.ledger import ChartOfAccount
        assert ChartOfAccount.query.count() > 50, \
            "fixed chart must be seeded for the new company"
        from shared.models.stock_ledger import VoucherNumber
        assert VoucherNumber.query.count() == 14, \
            "voucher numbering must be provisioned"
        from shared.models.company_settings import AccountingPeriod
        assert AccountingPeriod.query.count() >= 1, \
            "fiscal periods must exist for the new company"
        from shared.models.inventory_settings import InventorySettings
        assert InventorySettings.query.first() is not None
        from shared.models.invoice_template import InvoiceTemplate
        assert InvoiceTemplate.query.count() >= 4, \
            "default invoice templates must be provisioned"


def test_creation_enforces_quota_slug_and_uniqueness(seeded):
    from shared.extensions import db
    from shared.models.company import GlobalLimits
    email = f"q_{_suffix()}@example.com"
    _create_user(email)
    c, _ = _login(email, "pw12345")

    with flask_app.app_context():
        limits = GlobalLimits.get()
        original = limits.max_companies_per_user
        limits.max_companies_per_user = 0
        db.session.commit()

    try:
        resp = c.post("/portal/create",
                      data={"name": "No Quota Co", "slug": f"nq-{_suffix()}"})
        assert resp.status_code == 302
        assert resp.headers["Location"].endswith("/portal/"), \
            "quota-blocked creation must return to the portal"
    finally:
        with flask_app.app_context():
            limits = GlobalLimits.get()
            limits.max_companies_per_user = original
            db.session.commit()

    # Invalid slug rejected.
    resp = c.post("/portal/create", data={"name": "Bad Slug", "slug": "UPPER!"})
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/portal/")

    # Duplicate slug rejected.
    slug = f"dup-{_suffix()}"
    resp = c.post("/portal/create", data={"name": "First Co", "slug": slug})
    assert resp.status_code == 302
    resp = c.post("/portal/create", data={"name": "Second Co", "slug": slug})
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/portal/")

    # Blank name rejected.
    resp = c.post("/portal/create", data={"name": "", "slug": slug})
    assert resp.status_code == 302


# ── Invitations on the portal ───────────────────────────────────────────────

def test_invitation_accept_and_decline_return_to_portal(seeded):
    from shared.extensions import db
    from shared.models.base import Role
    from shared.models.company import CompanyInvitation
    email = f"pinv_{_suffix()}@example.com"
    uid = _create_user(email)
    company_id = _create_company("Inviter Co", f"invc-{_suffix()}")

    c, _ = _login(email, "pw12345")
    page = c.get("/portal/")
    assert b"Inviter Co" not in page.data

    # Super admin (or another admin) invites the user.
    with flask_app.app_context():
        r = Role.query.filter_by(name="employee").first()
        inv = CompanyInvitation(company_id=company_id, email=email,
                                role_id=r.id, status="sent")
        db.session.add(inv)
        db.session.commit()
        inv_id = inv.id

    page = c.get("/portal/")
    assert b"Inviter Co" in page.data, "pending invite must appear on portal"

    # Accept: session switches into the company, then returns to the portal.
    resp = c.post(f"/settings/invitations/{inv_id}/accept",
                  data={"next": "/portal/"})
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/portal/")
    with c.session_transaction() as sess:
        assert sess.get("company_id") == company_id
    page = c.get("/portal/")
    assert b"Inviter Co" in page.data

    # Decline: another invite, portal keeps the user in place.
    with flask_app.app_context():
        r = Role.query.filter_by(name="employee").first()
        inv2 = CompanyInvitation(company_id=company_id, email=email,
                                 role_id=r.id, status="sent")
        db.session.add(inv2)
        db.session.commit()
        inv2_id = inv2.id
    resp = c.post(f"/settings/invitations/{inv2_id}/decline",
                  data={"next": "/portal/"})
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/portal/")
    with flask_app.app_context():
        assert CompanyInvitation.query.get(inv2_id).status == "revoked"


# ── Super admin link ────────────────────────────────────────────────────────

def test_super_admin_portal_link_only_for_super_admins(seeded):
    """Asserted on the href, not the link text: the label is copy and has
    already been reworded once, but the destination is the actual contract."""
    super_c, _ = _login("admin@gmail.com", "admin123")
    page = super_c.get("/portal/")
    assert page.status_code == 200
    assert b'href="/superadmin/"' in page.data

    emp_c, _ = _login("emp@solarkon.com", "emp123")
    page = emp_c.get("/portal/")
    assert page.status_code == 200
    assert b'href="/superadmin/"' not in page.data


# ── Landing page ────────────────────────────────────────────────────────────

def test_landing_page_is_public(seeded):
    """"/" serves the marketing page to anonymous visitors, with the two
    sign-in doors on it."""
    page = flask_app.test_client().get("/")
    assert page.status_code == 200
    assert b"/auth/login" in page.data
    assert b"/superadmin/login" in page.data


def test_landing_redirects_signed_in_users(seeded):
    """A signed-in user never sees marketing copy — "/" sends them where
    login would have, which is the portal."""
    c, _ = _login("emp@solarkon.com", "emp123")
    resp = c.get("/")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/portal/")

    email = f"landing_{_suffix()}@example.com"
    _create_user(email)
    c2, _ = _login(email, "pw12345")
    resp = c2.get("/")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/portal/")


# ── Super admin console door ────────────────────────────────────────────────

def test_superadmin_login_admits_super_admins(seeded):
    c = flask_app.test_client()
    assert c.get("/superadmin/login").status_code == 200
    resp = c.post("/superadmin/login",
                  data={"login": "admin@gmail.com", "password": "admin123"})
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/superadmin/")
    assert c.get("/superadmin/").status_code == 200


def test_superadmin_login_refuses_non_super_admins(seeded):
    """Correct credentials for an ordinary user must not open the console,
    and must not sign them in either — otherwise this page would quietly
    become a second, unguarded staff login."""
    c = flask_app.test_client()
    resp = c.post("/superadmin/login",
                  data={"login": "emp@solarkon.com", "password": "emp123"})
    assert resp.status_code == 200, "must re-render, not redirect in"
    with c.session_transaction() as sess:
        assert not sess.get("_user_id"), \
            "a non-super-admin was logged in by the console door"


def _default_company_id():
    from shared.models.company import Company
    with flask_app.app_context():
        c = Company.query.filter_by(slug="default").first()
        assert c is not None, "no seeded default company"
        return c.id
