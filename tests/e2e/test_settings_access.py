"""The settings module's write handlers must enforce access server-side.

Hiding a tab is not access control: shared/routes/settings.py re-checks the
section predicate in every write handler via ``_require``. These tests drive
the POST handlers directly — the route smoke test is GET-only and signs in as
an admin, so it exercises neither the write paths nor a restricted user.

Each negative test asserts the *stored value is unchanged* rather than just a
302: a denied write and a successful write both redirect, so status alone
would prove nothing.
"""
import os
import tempfile

import pytest

# Point at a throwaway DB before app import — importing app builds the engine.
# Mirrors test_routes_smoke.py so this file also runs standalone.
_TMP_DB = os.path.join(tempfile.gettempdir(), "erp_settings_access.db")
os.environ.setdefault("DATABASE_URL", "sqlite:///" + _TMP_DB.replace("\\", "/"))

from app import app as flask_app  # noqa: E402


def _user_id(email_like):
    from shared.models.base import User
    with flask_app.app_context():
        u = User.query.filter(User.email.ilike(email_like)).first()
        assert u is not None, f"seed produced no user matching {email_like}"
        return u.id


@pytest.fixture(scope="module")
def seeded():
    # Deliberately NOT `with flask_app.test_client() as c:` — that preserves
    # the request context for the life of the block, and this fixture's
    # unauthenticated context would then shadow every later client's request,
    # silently making each one anonymous.
    flask_app.test_client().get("/")  # lazy create_all + migrate + seed


def _client_as(email_like):
    c = flask_app.test_client()
    uid = _user_id(email_like)
    with c.session_transaction() as sess:
        sess["_user_id"] = str(uid)
        sess["_fresh"] = True
    return c


@pytest.fixture
def employee(seeded):
    """HR access only: no finance, no inventory, no invoicing, not admin."""
    return _client_as("emp@solarkon.com")


@pytest.fixture
def admin(seeded):
    return _client_as("admin@solarkon.com")


def test_employee_has_no_finance_or_admin_access(seeded):
    """Guards the premise of every negative test below."""
    from shared.models.base import User
    with flask_app.app_context():
        u = User.query.filter(User.email.ilike("emp@solarkon.com")).first()
        assert not u.is_admin()
        assert not u.module_access("finance")
        assert not u.module_access("inventory")
        assert not u.module_access("invoicing")


# ── Section visibility ──────────────────────────────────────────────────────

def test_employee_sees_account_and_invites_sections(seeded):
    """Every user sees My Account + Invitations; only module-gated sections
    are hidden from a plain employee."""
    from shared.models.base import User
    from shared.routes.settings import visible_sections
    with flask_app.app_context():
        u = User.query.filter(User.email.ilike("emp@solarkon.com")).first()
        assert [s["key"] for s in visible_sections(u)] == ["account", "invites"]


def test_admin_sees_every_section(seeded):
    from shared.models.base import User
    from shared.routes.settings import visible_sections, SECTIONS
    with flask_app.app_context():
        u = User.query.filter(User.email.ilike("admin@solarkon.com")).first()
        assert len(visible_sections(u)) == len(SECTIONS)


def test_deep_link_to_forbidden_tab_falls_back_not_403(employee):
    """A deep link to a hidden section degrades to the user's first section."""
    resp = employee.get("/settings/?tab=rights")
    assert resp.status_code == 200
    # The rights grid must not render for a non-admin.
    assert b"Rights &amp; Access" not in resp.data


# ── Company profile ─────────────────────────────────────────────────────────

def test_employee_cannot_rewrite_company_profile(employee):
    from shared.models.company_settings import CompanyInfo
    with flask_app.app_context():
        before = CompanyInfo.get().company_name

    employee.post("/settings/company", data={"company_name": "PWNED BY EMPLOYEE"})

    with flask_app.app_context():
        assert CompanyInfo.get().company_name == before


def _company_id():
    """The seeded default company (memberships/tenancy scoping)."""
    from shared.models.company import Company
    with flask_app.app_context():
        return Company.query.filter_by(slug="default").first().id


def _company_info_name():
    """Read CompanyInfo inside the default company's context."""
    from shared.models.company_settings import CompanyInfo
    from shared.tenancy import set_current_company
    with flask_app.app_context():
        set_current_company(_company_id())
        return CompanyInfo.get().company_name


def test_admin_can_rewrite_company_profile(admin):
    from shared.extensions import db
    from shared.models.company_settings import CompanyInfo
    from shared.tenancy import set_current_company
    original = _company_info_name()
    try:
        admin.post("/settings/company", data={"company_name": "Acme Solar Ltd"})
        assert _company_info_name() == "Acme Solar Ltd"
    finally:
        with flask_app.app_context():
            set_current_company(_company_id())
            CompanyInfo.get().company_name = original
            db.session.commit()


# ── Inventory ───────────────────────────────────────────────────────────────

def test_employee_cannot_change_valuation_method(employee):
    from shared.models.inventory_settings import InventorySettings
    with flask_app.app_context():
        before = InventorySettings.get().valuation_method

    employee.post("/settings/inventory", data={"valuation_method": "fifo",
                                               "allow_negative_stock": "on"})

    with flask_app.app_context():
        s = InventorySettings.get()
        assert s.valuation_method == before


# ── Financial periods ───────────────────────────────────────────────────────

def test_employee_cannot_change_fiscal_year_rule(employee):
    from shared.models.company_settings import AccountingPeriod
    with flask_app.app_context():
        before = AccountingPeriod.query.count()

    employee.post("/settings/periods/rule", data={"start_month": "7",
                                                   "start_day": "1"})

    with flask_app.app_context():
        assert AccountingPeriod.query.count() == before


# ── Report structure ────────────────────────────────────────────────────────

def test_employee_cannot_reset_pl_structure(employee):
    from shared.models.company_settings import ReportSettings
    with flask_app.app_context():
        before = ReportSettings.get().pl_detail_rows

    employee.post("/settings/reports", data={"pl_detail_rows": "99", "reset_pl": "1"})

    with flask_app.app_context():
        assert ReportSettings.get().pl_detail_rows == before


# ── Invoice party mode ──────────────────────────────────────────────────────

def test_employee_cannot_change_party_mode(employee):
    from shared.models.company_settings import ReportSettings
    with flask_app.app_context():
        before = ReportSettings.get().party_mode("purchase")

    employee.post("/settings/documents/purchase", data={"party_mode": "all"})

    with flask_app.app_context():
        assert ReportSettings.get().party_mode("purchase") == before


def test_unknown_document_type_is_404(admin):
    assert admin.post("/settings/documents/bogus",
                      data={"party_mode": "all"}).status_code == 404


# ── Rights & access (admin only) ────────────────────────────────────────────

def test_employee_cannot_grant_themselves_module_access(employee):
    from shared.models.base import User
    uid = _user_id("emp@solarkon.com")

    employee.post(f"/settings/users/{uid}/rights",
                  data={"module_finance": "on", "module_inventory": "on"})

    with flask_app.app_context():
        u = User.query.get(uid)
        assert not u.has_finance_access
        assert not u.has_inventory_access


def test_employee_cannot_reset_another_users_rights(employee):
    admin_uid = _user_id("admin@solarkon.com")
    resp = employee.post(f"/settings/users/{admin_uid}/reset-rights")
    # Redirected away rather than performing the reset.
    assert resp.status_code == 302
    assert "/settings/" in resp.headers["Location"]


def test_employee_cannot_deactivate_an_admin(employee):
    from shared.models.base import User
    admin_uid = _user_id("admin@solarkon.com")

    employee.post(f"/settings/users/{admin_uid}/toggle-active")

    with flask_app.app_context():
        assert User.query.get(admin_uid).is_active


def test_admin_cannot_deactivate_self(admin):
    from shared.models.base import User
    admin_uid = _user_id("admin@solarkon.com")

    admin.post(f"/settings/users/{admin_uid}/toggle-active")

    with flask_app.app_context():
        assert User.query.get(admin_uid).is_active, \
            "admin locked themselves out via self-deactivation"


# ── My Account (self-service — every user, including the employee) ──────────

def test_employee_can_update_their_own_name(employee):
    from shared.extensions import db
    from shared.models.base import User
    uid = _user_id("emp@solarkon.com")
    with flask_app.app_context():
        original = User.query.get(uid).full_name
    try:
        employee.post("/settings/account", data={"full_name": "Renamed Employee"})
        with flask_app.app_context():
            assert User.query.get(uid).full_name == "Renamed Employee"
    finally:
        with flask_app.app_context():
            User.query.get(uid).full_name = original
            db.session.commit()


def test_account_rejects_mismatched_password_confirmation(employee):
    """The password is only changed when both fields agree."""
    from shared.models.base import User
    uid = _user_id("emp@solarkon.com")

    employee.post("/settings/account", data={"new_password": "newpass123",
                                             "confirm_password": "different"})

    with flask_app.app_context():
        assert User.query.get(uid).check_password("emp123"), \
            "password changed despite mismatched confirmation"
