"""E2E coverage for the super admin console and the account rules it owns.

Same harness as test_portal.py: a throwaway sqlite DB seeded through the
app's own ``_seed_all_data`` on first request, setup rows written directly
with ``db.session``, flows driven through the Flask test client.

Covers:
  - Manage Users: the only place an account is created; duplicate email
    refused; the list and the per-user page are super-admin only
  - per-user quotas: owned-company override beats the global default, joined
    override caps invitation acceptance, blank means "fall back"
  - password reset, account deactivation, and the self-deactivation guard
  - My Companies: a super admin creates their own company, its books are
    provisioned, and Open Books switches into it
  - module entitlement: the company edit form is the whole truth, a disabled
    module is off for the company admin too, and an unknown/NULL column
    still reads as enabled
  - member blocking: the membership survives, the person cannot enter the
    company, unblocking restores them
  - HR Assign Member: HR no longer mints logins; it gives an existing member
    their employee details, per-company employee codes, clash refused
"""
import os
import tempfile
import uuid

import pytest

_TMP_DB = os.path.join(tempfile.gettempdir(), "erp_superadmin_e2e.db")
os.environ.setdefault("DATABASE_URL", "sqlite:///" + _TMP_DB.replace("\\", "/"))

from app import app as flask_app  # noqa: E402


def _suffix():
    return uuid.uuid4().hex[:10]


def _create_user(email, full_name=None, password="pw12345", role="employee"):
    from shared.extensions import db
    from shared.models.base import Role, User
    with flask_app.app_context():
        r = Role.query.filter_by(name=role).first()
        u = User(
            employee_code=f"SC{_suffix()}".upper(),
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


def _add_membership(company_id, user_id, role="employee", status="active",
                    employee_code=None):
    from shared.extensions import db
    from shared.models.base import Role
    from shared.models.company import CompanyMembership
    with flask_app.app_context():
        r = Role.query.filter_by(name=role).first()
        m = CompanyMembership(company_id=company_id, user_id=user_id,
                              role_id=r.id, status=status,
                              employee_code=employee_code)
        db.session.add(m)
        db.session.commit()
        return m.id


def _login(email, password):
    c = flask_app.test_client()
    resp = c.post("/auth/login", data={"login": email, "password": password})
    with c.session_transaction() as sess:
        assert sess.get("_user_id"), f"login failed for {email}"
    return c, resp


def _super():
    c, _ = _login("admin@gmail.com", "admin123")
    return c


def _default_company_id():
    from shared.models.company import Company
    with flask_app.app_context():
        c = Company.query.filter_by(slug="default").first()
        assert c is not None, "no seeded default company"
        return c.id


@pytest.fixture(scope="module")
def seeded():
    with flask_app.app_context():
        from shared.extensions import db
        db.drop_all()
        flask_app._db_initialized = False
    flask_app.test_client().get("/")  # lazy create_all + migrate + seed


# ── Manage Users ────────────────────────────────────────────────────────────

def test_users_page_lists_and_is_super_admin_only(seeded):
    sa = _super()
    page = sa.get("/superadmin/users/")
    assert page.status_code == 200
    assert b"Create User" in page.data
    assert b"admin@gmail.com" in page.data

    emp, _ = _login("emp@solarkon.com", "emp123")
    assert emp.get("/superadmin/users/").status_code == 403


def test_super_admin_creates_a_user(seeded):
    """The console is the only door a new account comes through, and the new
    account can immediately log in even though it belongs to no company."""
    from shared.models.base import User
    email = f"made_{_suffix()}@example.com"
    sa = _super()
    resp = sa.post("/superadmin/users/", data={
        "email": email, "full_name": "Made Person", "password": "made1234"})
    assert resp.status_code == 302

    with flask_app.app_context():
        u = User.query.filter_by(email=email).first()
        assert u is not None, "user was not created"
        assert u.full_name == "Made Person"
        assert u.check_password("made1234")
        assert u.employee_code, "account needs a placeholder code (NOT NULL)"
        assert u.memberships.count() == 0, \
            "creating an account must not put anyone in a company"
        uid = u.id

    # The redirect lands on that user's page, and the page opens.
    assert resp.headers["Location"].endswith(f"/superadmin/users/{uid}/")
    assert sa.get(f"/superadmin/users/{uid}/").status_code == 200

    # The account works: it can sign in and reach the portal.
    c, login_resp = _login(email, "made1234")
    assert login_resp.headers["Location"].endswith("/portal/")


def test_duplicate_email_is_refused(seeded):
    from shared.models.base import User
    email = f"dupe_{_suffix()}@example.com"
    _create_user(email)
    sa = _super()
    sa.post("/superadmin/users/", data={
        "email": email, "full_name": "Impostor", "password": "pw12345"})
    with flask_app.app_context():
        assert User.query.filter_by(email=email).count() == 1


def test_user_detail_shows_memberships(seeded):
    email = f"member_{_suffix()}@example.com"
    uid = _create_user(email)
    cid = _create_company("Detail Co", f"detail-{_suffix()}", created_by=uid)
    _add_membership(cid, uid, role="admin", employee_code="EMP007")

    page = _super().get(f"/superadmin/users/{uid}/")
    assert page.status_code == 200
    assert b"Detail Co" in page.data
    assert b"EMP007" in page.data


# ── Per-user quotas ─────────────────────────────────────────────────────────

def test_owned_quota_override_beats_the_global_default(seeded):
    """The global default allows several companies; an override of 1 must
    stop this user at their first one."""
    from shared.models.base import User
    from shared.models.company import Company
    email = f"quota_{_suffix()}@example.com"
    uid = _create_user(email)
    sa = _super()
    sa.post(f"/superadmin/users/{uid}/", data={
        "action": "profile", "full_name": "Quota User",
        "max_companies_owned": "1", "max_companies_joined": ""})
    with flask_app.app_context():
        u = User.query.get(uid)
        assert u.max_companies_owned == 1
        assert u.max_companies_joined is None, \
            "blank must mean 'use the default', not zero"

    c, _ = _login(email, "pw12345")
    first = f"quota-a-{_suffix()}"
    c.post("/portal/create", data={"name": "Quota A", "slug": first})
    second = f"quota-b-{_suffix()}"
    c.post("/portal/create", data={"name": "Quota B", "slug": second})
    with flask_app.app_context():
        assert Company.query.filter_by(slug=first).first() is not None
        assert Company.query.filter_by(slug=second).first() is None, \
            "the per-user override did not cap company creation"

    # Clearing the override hands the user back to the global default.
    sa.post(f"/superadmin/users/{uid}/", data={
        "action": "profile", "full_name": "Quota User",
        "max_companies_owned": "", "max_companies_joined": ""})
    c.post("/portal/create", data={"name": "Quota B", "slug": second})
    with flask_app.app_context():
        assert Company.query.filter_by(slug=second).first() is not None, \
            "clearing the override must restore the global default"


def test_joined_quota_caps_invitation_acceptance(seeded):
    """An unset join limit is unlimited; a set one is enforced when the
    invitation is accepted, which is the moment membership is created."""
    from shared.extensions import db
    from shared.models.base import Role
    from shared.models.company import CompanyInvitation, CompanyMembership
    email = f"joiner_{_suffix()}@example.com"
    uid = _create_user(email)
    home = _create_company("Joiner Home", f"jhome-{_suffix()}", created_by=uid)
    _add_membership(home, uid, role="admin")
    other = _create_company("Joiner Other", f"jother-{_suffix()}")

    _super().post(f"/superadmin/users/{uid}/", data={
        "action": "profile", "full_name": "Joiner",
        "max_companies_owned": "", "max_companies_joined": "1"})

    with flask_app.app_context():
        r = Role.query.filter_by(name="manager").first()
        inv = CompanyInvitation(company_id=other, email=email,
                                role_id=r.id, status="sent")
        db.session.add(inv)
        db.session.commit()
        inv_id = inv.id

    c, _ = _login(email, "pw12345")
    c.post(f"/settings/invitations/{inv_id}/accept", data={"next": "/portal/"})
    with flask_app.app_context():
        assert CompanyInvitation.query.get(inv_id).status == "sent", \
            "the invitation must stay open when the join cap blocks it"
        assert CompanyMembership.query.filter_by(
            company_id=other, user_id=uid).first() is None

    # Raising the cap lets the same invitation through.
    _super().post(f"/superadmin/users/{uid}/", data={
        "action": "profile", "full_name": "Joiner",
        "max_companies_owned": "", "max_companies_joined": "5"})
    c.post(f"/settings/invitations/{inv_id}/accept", data={"next": "/portal/"})
    with flask_app.app_context():
        assert CompanyInvitation.query.get(inv_id).status == "accepted"
        assert CompanyMembership.query.filter_by(
            company_id=other, user_id=uid).first() is not None


# ── Password, deactivation ──────────────────────────────────────────────────

def test_password_reset_and_deactivation(seeded):
    from shared.models.base import User
    email = f"reset_{_suffix()}@example.com"
    uid = _create_user(email)
    sa = _super()

    sa.post(f"/superadmin/users/{uid}/", data={
        "action": "password", "new_password": "brandnew1"})
    with flask_app.app_context():
        assert User.query.get(uid).check_password("brandnew1")

    sa.post(f"/superadmin/users/{uid}/", data={"action": "active"})
    with flask_app.app_context():
        assert User.query.get(uid).is_active is False
    sa.post(f"/superadmin/users/{uid}/", data={"action": "active"})
    with flask_app.app_context():
        assert User.query.get(uid).is_active is True


def test_short_password_is_refused(seeded):
    from shared.models.base import User
    email = f"shortpw_{_suffix()}@example.com"
    uid = _create_user(email)
    _super().post(f"/superadmin/users/{uid}/", data={
        "action": "password", "new_password": "ab"})
    with flask_app.app_context():
        assert User.query.get(uid).check_password("pw12345"), \
            "a rejected reset must leave the old password working"


def test_super_admin_cannot_deactivate_themselves(seeded):
    """Otherwise the console can be locked with nobody left to unlock it."""
    from shared.models.base import User
    with flask_app.app_context():
        me = User.query.filter_by(email="admin@gmail.com").first()
        my_id = me.id
    _super().post(f"/superadmin/users/{my_id}/", data={"action": "active"})
    with flask_app.app_context():
        assert User.query.get(my_id).is_active is True


# ── My Companies ────────────────────────────────────────────────────────────

def test_super_admin_creates_own_company_with_provisioned_books(seeded):
    from shared.models.company import Company, CompanyMembership
    from shared.tenancy import set_current_company
    sa = _super()
    assert sa.get("/superadmin/my-companies/").status_code == 200

    slug = f"saown-{_suffix()}"
    resp = sa.post("/superadmin/my-companies/",
                   data={"name": "SA Own Books", "slug": slug})
    assert resp.status_code == 302

    with flask_app.app_context():
        c = Company.query.filter_by(slug=slug).first()
        assert c is not None, "the super admin's own company was not created"
        cid = c.id
        m = CompanyMembership.query.filter_by(company_id=cid).first()
        assert m is not None and m.status == CompanyMembership.ACTIVE
        assert m.role_name() == "admin", "the creator must be its admin"

    # provision_company ran: chart of accounts and voucher numbering exist.
    with flask_app.app_context():
        from shared.models.ledger import ChartOfAccount
        from shared.models.stock_ledger import VoucherNumber
        set_current_company(cid)
        assert ChartOfAccount.query.count() > 50, \
            "chart of accounts was not seeded"
        assert VoucherNumber.query.count() >= 14, \
            "voucher number series were not seeded"

    # It shows on the page, and Open Books switches the session into it.
    page = sa.get("/superadmin/my-companies/")
    assert b"SA Own Books" in page.data
    assert f"/company/switch/{cid}".encode() in page.data
    sa.get(f"/company/switch/{cid}")
    with sa.session_transaction() as sess:
        assert sess.get("company_id") == cid


def test_my_companies_rejects_a_bad_slug_and_a_duplicate(seeded):
    from shared.models.company import Company
    sa = _super()
    sa.post("/superadmin/my-companies/",
            data={"name": "Bad Slug Co", "slug": "Not A Slug"})
    with flask_app.app_context():
        assert Company.query.filter_by(name="Bad Slug Co").first() is None

    slug = f"sadupe-{_suffix()}"
    sa.post("/superadmin/my-companies/", data={"name": "First", "slug": slug})
    sa.post("/superadmin/my-companies/", data={"name": "Second", "slug": slug})
    with flask_app.app_context():
        assert Company.query.filter_by(slug=slug).count() == 1


def test_my_companies_is_super_admin_only(seeded):
    emp, _ = _login("emp@solarkon.com", "emp123")
    assert emp.get("/superadmin/my-companies/").status_code == 403


# ── Module entitlement ──────────────────────────────────────────────────────

def test_company_defaults_to_every_module_enabled(seeded):
    from shared.models.company import Company
    cid = _create_company("Fresh Co", f"fresh-{_suffix()}")
    with flask_app.app_context():
        c = Company.query.get(cid)
        assert set(c.enabled_modules()) == set(Company.MODULE_COLUMNS), \
            "a new company must start with every module"
        assert c.module_enabled("something_new") is True, \
            "an unmapped module key must not silently disappear"


def test_super_admin_toggles_company_modules(seeded):
    """The form is the whole truth: an unchecked box is a disabled module,
    and what is saved survives a reload of the page."""
    from shared.models.company import Company
    email = f"entitle_{_suffix()}@example.com"
    uid = _create_user(email)
    cid = _create_company("Entitled Co", f"entitle-{_suffix()}", created_by=uid)
    _add_membership(cid, uid, role="admin")
    sa = _super()

    page = sa.get(f"/superadmin/companies/{cid}/")
    assert page.status_code == 200
    assert b'name="mod_hr"' in page.data
    assert b'name="mod_fbr"' in page.data

    # Save with only HR and finance ticked.
    sa.post(f"/superadmin/companies/{cid}/", data={
        "name": "Entitled Co", "plan_name": "free", "is_active": "on",
        "mod_hr": "on", "mod_finance": "on"})
    with flask_app.app_context():
        c = Company.query.get(cid)
        assert sorted(c.enabled_modules()) == ["finance", "hr"]

    # Turning one back on leaves the others alone.
    sa.post(f"/superadmin/companies/{cid}/", data={
        "name": "Entitled Co", "plan_name": "free", "is_active": "on",
        "mod_hr": "on", "mod_finance": "on", "mod_inventory": "on"})
    with flask_app.app_context():
        c = Company.query.get(cid)
        assert sorted(c.enabled_modules()) == ["finance", "hr", "inventory"]


def test_disabled_module_is_off_for_the_company_admin_too(seeded):
    """Entitlement is what the company bought — being admin bypasses the
    per-user flag, never the entitlement. Outside a company there is no
    entitlement to apply, so the user flag alone decides."""
    from shared.models.base import User
    from shared.models.company import Company
    from shared.extensions import db
    from shared.tenancy import set_current_company

    email = f"gated_{_suffix()}@example.com"
    uid = _create_user(email)
    cid = _create_company("Gated Co", f"gated-{_suffix()}", created_by=uid)
    _add_membership(cid, uid, role="admin")
    with flask_app.app_context():
        c = Company.query.get(cid)
        c.mod_inventory_enabled = False
        db.session.commit()

    with flask_app.app_context():
        set_current_company(cid)
        u = User.query.get(uid)
        assert u.is_admin() is True
        assert u.module_access("inventory") is False, \
            "a module the company is not entitled to must be off for admin too"
        assert u.module_access("hr") is True

    # No active company (portal / console): the user's own flag decides.
    with flask_app.app_context():
        u = User.query.get(uid)
        assert u.module_access("inventory") is True


def test_disabling_a_module_removes_it_from_the_hub_and_its_routes(seeded):
    """Entitlement has to reach the screen, not just the model: the tile goes
    from the hub and the module's own pages stop opening."""
    from shared.extensions import db
    from shared.models.company import Company

    email = f"hubgate_{_suffix()}@example.com"
    uid = _create_user(email)
    cid = _create_company("Hub Gate Co", f"hubgate-{_suffix()}", created_by=uid)
    _add_membership(cid, uid, role="admin")
    c, _ = _login(email, "pw12345")
    c.get(f"/company/switch/{cid}")

    page = c.get("/dashboard/")
    assert page.status_code == 200
    assert b"Fixed Assets" in page.data

    with flask_app.app_context():
        company = Company.query.get(cid)
        company.mod_fixed_assets_enabled = False
        db.session.commit()

    page = c.get("/dashboard/")
    assert page.status_code == 200
    assert b"Fixed Assets" not in page.data, \
        "a module the company lost is still offered on the hub"
    # The module's own guard must refuse cleanly, not blow up: these routes
    # render "access_denied.html", which had no file at the root of the
    # template loader and so raised TemplateNotFound.
    resp = c.get("/fixed-assets/")
    assert resp.status_code == 200
    assert b"Module Not Available" in resp.data
    assert b"Net Book Value" not in resp.data, \
        "the refusal page must not leak the module's own dashboard"


# ── Member blocking ─────────────────────────────────────────────────────────

def test_block_and_unblock_a_member(seeded):
    """Blocking keeps the membership row — role and employee code survive —
    and only stops the person entering that company."""
    from shared.models.company import CompanyMembership
    email = f"blocked_{_suffix()}@example.com"
    uid = _create_user(email)
    cid = _create_company("Block Co", f"block-{_suffix()}", created_by=uid)
    _add_membership(cid, uid, role="admin")
    victim_id = _create_user(f"victim_{_suffix()}@example.com")
    mid = _add_membership(cid, victim_id, role="manager",
                          employee_code="EMP042")
    sa = _super()

    from shared.models.base import User
    with flask_app.app_context():
        victim_email = User.query.get(victim_id).email

    # Before blocking, the victim can enter the company.
    victim, _ = _login(victim_email, "pw12345")
    victim.get(f"/company/switch/{cid}")
    with victim.session_transaction() as sess:
        assert sess.get("company_id") == cid

    sa.post(f"/superadmin/companies/{cid}/members/{mid}/block")
    with flask_app.app_context():
        m = CompanyMembership.query.get(mid)
        assert m.status == CompanyMembership.BLOCKED
        assert m.employee_code == "EMP042", \
            "blocking must not throw away the employee code"
        assert m.role_name() == "manager", "blocking must not change the role"

    # A blocked member cannot switch into the company.
    victim, _ = _login(victim_email, "pw12345")
    resp = victim.get(f"/company/switch/{cid}")
    assert resp.status_code == 302
    with victim.session_transaction() as sess:
        assert sess.get("company_id") != cid, \
            "a blocked member entered the company anyway"

    # Unblocking restores them.
    sa.post(f"/superadmin/companies/{cid}/members/{mid}/block")
    with flask_app.app_context():
        assert CompanyMembership.query.get(mid).status == \
            CompanyMembership.ACTIVE
    victim.get(f"/company/switch/{cid}")
    with victim.session_transaction() as sess:
        assert sess.get("company_id") == cid


def test_blocking_refuses_a_pending_membership(seeded):
    from shared.models.company import CompanyMembership
    uid = _create_user(f"pend_{_suffix()}@example.com")
    cid = _create_company("Pend Co", f"pend-{_suffix()}")
    mid = _add_membership(cid, uid, role="employee", status="pending")
    _super().post(f"/superadmin/companies/{cid}/members/{mid}/block")
    with flask_app.app_context():
        assert CompanyMembership.query.get(mid).status == "pending"


def test_block_route_rejects_a_membership_from_another_company(seeded):
    """The membership id is in the URL, so it must be checked against the
    company in the URL — otherwise any membership could be flipped from any
    company's page."""
    from shared.models.company import CompanyMembership
    uid = _create_user(f"cross_{_suffix()}@example.com")
    a = _create_company("Cross A", f"crossa-{_suffix()}")
    b = _create_company("Cross B", f"crossb-{_suffix()}")
    mid = _add_membership(a, uid, role="employee")
    resp = _super().post(f"/superadmin/companies/{b}/members/{mid}/block")
    assert resp.status_code == 404
    with flask_app.app_context():
        assert CompanyMembership.query.get(mid).status == \
            CompanyMembership.ACTIVE


# ── HR Assign Member ────────────────────────────────────────────────────────

def _hr_company():
    """An admin plus an unassigned member, in a company of their own."""
    admin_email = f"hradmin_{_suffix()}@example.com"
    admin_id = _create_user(admin_email, role="admin")
    cid = _create_company("HR Co", f"hrco-{_suffix()}", created_by=admin_id)
    _add_membership(cid, admin_id, role="admin", employee_code="ADM001")
    member_email = f"hrmember_{_suffix()}@example.com"
    member_id = _create_user(member_email, full_name="New Joiner")
    _add_membership(cid, member_id, role="employee")
    c, _ = _login(admin_email, "pw12345")
    c.get(f"/company/switch/{cid}")
    return c, cid, admin_id, member_id


def test_hr_cannot_create_logins_anymore(seeded):
    """The old /users/add minted a global User with no membership, so the
    person vanished from the list they were added to. It is gone."""
    c, cid, _admin_id, _member_id = _hr_company()
    assert c.get("/auth/users/add").status_code == 404
    page = c.get("/auth/users")
    assert page.status_code == 200
    assert b"Assign Member" in page.data
    assert b"/auth/users/add" not in page.data


def test_assign_member_lists_only_unassigned_active_members(seeded):
    c, cid, _admin_id, _member_id = _hr_company()
    page = c.get("/auth/members/assign")
    assert page.status_code == 200
    assert b"New Joiner" in page.data
    assert b"ADM001" not in page.data, \
        "an already-assigned member must not be offered again"


def test_assign_member_sets_the_per_company_employee_code(seeded):
    from shared.models.company import CompanyMembership
    from shared.models.base import User
    c, cid, _admin_id, member_id = _hr_company()
    resp = c.post("/auth/members/assign", data={
        "user_id": member_id, "employee_code": "EMP100",
        "designation": "Analyst", "department": "Finance"})
    assert resp.status_code == 302

    with flask_app.app_context():
        m = CompanyMembership.query.filter_by(
            company_id=cid, user_id=member_id).first()
        assert m.employee_code == "EMP100", \
            "the code belongs to the membership, not the global user"
        u = User.query.get(member_id)
        assert u.designation == "Analyst"
        assert u.department == "Finance"
        assert u.date_of_joining is not None

    # Done once, they drop off the pending list. Asserted on the row's hidden
    # user_id, not the name — the name also appears in the success flash that
    # the unfollowed redirect leaves behind.
    page = c.get("/auth/members/assign")
    assert f'name="user_id" value="{member_id}"'.encode() not in page.data


def test_the_same_employee_code_may_be_used_in_another_company(seeded):
    """Codes are unique per company, not globally — EMP100 in two different
    companies is two different people, and that is allowed."""
    from shared.models.company import CompanyMembership
    c1, cid1, _a1, m1 = _hr_company()
    c1.post("/auth/members/assign", data={
        "user_id": m1, "employee_code": "EMP100"})
    c2, cid2, _a2, m2 = _hr_company()
    c2.post("/auth/members/assign", data={
        "user_id": m2, "employee_code": "EMP100"})
    with flask_app.app_context():
        for cid, uid in ((cid1, m1), (cid2, m2)):
            m = CompanyMembership.query.filter_by(
                company_id=cid, user_id=uid).first()
            assert m.employee_code == "EMP100"


def test_duplicate_employee_code_within_a_company_is_refused(seeded):
    from shared.models.company import CompanyMembership
    c, cid, _admin_id, member_id = _hr_company()
    c.post("/auth/members/assign", data={
        "user_id": member_id, "employee_code": "ADM001"})
    with flask_app.app_context():
        m = CompanyMembership.query.filter_by(
            company_id=cid, user_id=member_id).first()
        assert not m.employee_code, \
            "a clashing code must not be written"


def test_assign_refuses_someone_who_is_not_a_member(seeded):
    from shared.models.company import CompanyMembership
    c, cid, _admin_id, _member_id = _hr_company()
    outsider = _create_user(f"outsider_{_suffix()}@example.com")
    resp = c.post("/auth/members/assign", data={
        "user_id": outsider, "employee_code": "EMP999"})
    assert resp.status_code == 200, "must re-render the page, not redirect"
    with flask_app.app_context():
        assert CompanyMembership.query.filter_by(
            company_id=cid, user_id=outsider).first() is None
