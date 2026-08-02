"""E2E regression coverage for the HR/multi-company guard fixes:

  1. Invite accept/decline must not open-redirect on a crafted ``next``.
  2. MSS team/approval queries must be scoped to the active company
     (a global ``manager_id`` match must not leak another company's staff).
  3. HR user_edit must write the per-company membership role (not the global
     role column that role_in() ignores), and its manager dropdown must list
     managers by membership role.
  4. HR user_edit must refuse a duplicate email instead of 500ing on the
     unique constraint.
  5. member_assign must not write a per-company employee code onto the
     globally-unique users.employee_code when another company already holds
     that code.

Same harness as test_multicompany.py: throwaway sqlite, seeded through the
app's own first-request init, requests through Flask's test client.
"""
import os
import tempfile
import uuid

import pytest

_TMP_DB = os.path.join(tempfile.gettempdir(), "erp_multicompany_e2e.db")
os.environ.setdefault("DATABASE_URL", "sqlite:///" + _TMP_DB.replace("\\", "/"))

from app import app as flask_app  # noqa: E402


def _suffix():
    return uuid.uuid4().hex[:10]


def _default_company_id():
    from shared.models.company import Company
    with flask_app.app_context():
        return Company.query.filter_by(slug="default").first().id


def _uid(email):
    from shared.models.base import User
    with flask_app.app_context():
        return User.query.filter_by(email=email).first().id


def _create_user(email, full_name=None, password="pw12345", role="employee",
                 global_code=None):
    from shared.extensions import db
    from shared.models.base import Role, User
    with flask_app.app_context():
        r = Role.query.filter_by(name=role).first()
        u = User(
            employee_code=global_code or f"TG{_suffix()}".upper(),
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
        cid = c.id
    with flask_app.app_context():
        from shared.company_setup import provision_company
        from shared.tenancy import set_current_company
        set_current_company(cid)
        provision_company(cid)
    return cid


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
    c.post("/auth/login", data={"login": email, "password": password},
           follow_redirects=True)
    with c.session_transaction() as sess:
        assert sess.get("_user_id"), f"login failed for {email}"
    return c


@pytest.fixture(scope="module")
def seeded():
    with flask_app.app_context():
        from shared.extensions import db
        db.drop_all()
        flask_app._db_initialized = False
    flask_app.test_client().get("/")  # lazy create_all + migrate + seed


# ── 1. Invite accept/decline: no open redirect ─────────────────────────────

def test_invite_accept_next_cannot_open_redirect(seeded):
    from shared.models.base import User
    from shared.models.company import CompanyInvitation

    default_id = _default_company_id()
    admin = _login("admin@gmail.com", "admin123")
    inv_email = f"tgr_{_suffix()}@example.com"
    _create_user(inv_email)
    admin.post("/settings/invite", data={"email": inv_email,
                                         "role": "employee"})
    with flask_app.app_context():
        inv_id = CompanyInvitation.query.filter_by(
            company_id=default_id, email=inv_email,
            status=CompanyInvitation.SENT).first().id

    c = _login(inv_email, "pw12345")
    for evil in ("https://evil.example/phish",
                 "//evil.example/phish",
                 "http://evil.example/phish"):
        resp = c.post(f"/settings/invitations/{inv_id}/accept",
                      data={"next": evil})
        assert resp.status_code == 302
        loc = resp.headers["Location"]
        assert not loc.startswith("http") and not loc.startswith("//"), \
            f"accept open-redirected to {loc}"
    with flask_app.app_context():
        assert CompanyInvitation.query.get(inv_id).status == "accepted", \
            "first evil-next accept must still go through normally"

    # A local next is still honoured.
    invitee2 = f"tgr2_{_suffix()}@example.com"
    _create_user(invitee2)
    admin.post("/settings/invite", data={"email": invitee2,
                                         "role": "employee"})
    with flask_app.app_context():
        inv2_id = CompanyInvitation.query.filter_by(
            company_id=default_id, email=invitee2,
            status=CompanyInvitation.SENT).first().id
    c2 = _login(invitee2, "pw12345")
    resp = c2.post(f"/settings/invitations/{inv2_id}/accept",
                   data={"next": "/settings/"})
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/settings/")


def test_invite_decline_next_cannot_open_redirect(seeded):
    from shared.models.company import CompanyInvitation

    default_id = _default_company_id()
    admin = _login("admin@gmail.com", "admin123")
    inv_email = f"tgd_{_suffix()}@example.com"
    _create_user(inv_email)
    admin.post("/settings/invite", data={"email": inv_email,
                                         "role": "employee"})
    with flask_app.app_context():
        inv_id = CompanyInvitation.query.filter_by(
            company_id=default_id, email=inv_email,
            status=CompanyInvitation.SENT).first().id

    c = _login(inv_email, "pw12345")
    resp = c.post(f"/settings/invitations/{inv_id}/decline",
                  data={"next": "https://evil.example/phish"})
    assert resp.status_code == 302
    loc = resp.headers["Location"]
    assert not loc.startswith("http") and not loc.startswith("//"), \
        f"decline open-redirected to {loc}"
    assert loc.endswith("/settings/invitations/")


# ── 2. MSS scoped to the active company ─────────────────────────────────────

def test_mss_team_scoped_to_active_company(seeded):
    manager_email = "manager@solarkon.com"
    manager_uid = _uid(manager_email)
    emp_email = f"tgm_{_suffix()}@example.com"
    emp_uid = _create_user(emp_email, full_name="Epsilon Employee")
    with flask_app.app_context():
        from shared.extensions import db
        from shared.models.base import User
        User.query.get(emp_uid).manager_id = manager_uid
        db.session.commit()

    # Company B: the employee is a member there; the manager is not (yet).
    bid = _create_company("MSS Leak Co", f"mss-leak-{_suffix()}")
    _add_membership(bid, emp_uid, role="employee")

    c = _login(manager_email, "mgr123")
    # Manager's active company is the default company; the employee belongs
    # to company B, so the team view must not leak the name.
    resp = c.get("/mss/team")
    assert resp.status_code == 200
    assert b"Epsilon Employee" not in resp.data, \
        "team view leaked another company's employee"

    # Once the manager joins company B, the same employee appears there.
    _add_membership(bid, manager_uid, role="manager")
    resp = c.get(f"/company/switch/{bid}?next=/mss/team",
                 follow_redirects=True)
    assert resp.status_code == 200
    assert b"Epsilon Employee" in resp.data, \
        "team view must show the employee inside their own company"


def test_mss_index_and_approvals_scoped_to_active_company(seeded):
    manager_uid = _uid("manager@solarkon.com")
    emp_email = f"tga_{_suffix()}@example.com"
    emp_uid = _create_user(emp_email, full_name="Gamma Employee")
    with flask_app.app_context():
        from shared.extensions import db
        from shared.models.base import User
        User.query.get(emp_uid).manager_id = manager_uid
        db.session.commit()

    bid = _create_company("MSS Approve Co", f"mss-appr-{_suffix()}")
    _add_membership(bid, emp_uid, role="employee")

    c = _login("manager@solarkon.com", "mgr123")
    resp = c.get("/mss/")
    assert resp.status_code == 200
    assert b"Gamma Employee" not in resp.data, \
        "MSS index leaked another company's employee"


# ── 3. HR user_edit writes the membership role ─────────────────────────────

def test_user_edit_role_writes_membership_not_global(seeded):
    from shared.extensions import db
    from shared.models.base import Role, User
    from shared.models.company import CompanyMembership

    admin = _login("admin@gmail.com", "admin123")
    emp_uid = _uid("emp@solarkon.com")
    with flask_app.app_context():
        mgr_role = Role.query.filter_by(name=Role.MANAGER).first()
        emp_role = Role.query.filter_by(name=Role.EMPLOYEE).first()
        m = CompanyMembership.query.filter_by(
            company_id=_default_company_id(), user_id=emp_uid).first()
        original_membership_role = m.role_id
        original_global_role = User.query.get(emp_uid).role_id

    # The edit page's manager dropdown is membership-based: a manager by
    # membership appears, a manager by global role alone does not.
    member_mgr = _create_user(f"tgmgr_{_suffix()}@example.com",
                              full_name="Promoted Member",
                              role="employee")
    _add_membership(_default_company_id(), member_mgr, role="manager")
    foreign_mgr = _create_user(f"tggm_{_suffix()}@example.com",
                               full_name="Foreign Global Manager",
                               role="manager")  # global role only, no membership
    resp = admin.get(f"/auth/users/{emp_uid}/edit")
    assert resp.status_code == 200
    assert b"Promoted Member" in resp.data
    assert b"Foreign Global Manager" not in resp.data, \
        "manager dropdown must not list a non-member of this company"

    # Promote through the form: the membership role changes, the global
    # column (which role_in() ignores) stays.
    resp = admin.post(f"/auth/users/{emp_uid}/edit", data={
        "email": "emp@solarkon.com", "full_name": "Employee User",
        "role_id": str(mgr_role.id), "manager_id": "", "designation": "",
        "department": "", "phone": "", "is_active": "1"})
    assert resp.status_code == 302
    with flask_app.app_context():
        from shared.tenancy import set_current_company
        set_current_company(_default_company_id())
        m = CompanyMembership.query.filter_by(
            company_id=_default_company_id(), user_id=emp_uid).first()
        assert m.role_id == mgr_role.id, \
            "user_edit must change the membership role"
        u = User.query.get(emp_uid)
        assert u.role_id == original_global_role, \
            "user_edit must not touch the global role column"
        assert u.is_manager(), "membership role must drive is_manager()"

    # Restore the original membership role so other tests see the seed state.
    with flask_app.app_context():
        m = CompanyMembership.query.filter_by(
            company_id=_default_company_id(), user_id=emp_uid).first()
        m.role_id = original_membership_role
        db.session.commit()


def test_user_edit_cannot_demote_self(seeded):
    from shared.models.base import Role
    from shared.models.company import CompanyMembership

    admin = _login("admin@gmail.com", "admin123")
    admin_uid = _uid("admin@gmail.com")
    with flask_app.app_context():
        emp_role = Role.query.filter_by(name=Role.EMPLOYEE).first()
        m = CompanyMembership.query.filter_by(
            company_id=_default_company_id(), user_id=admin_uid).first()
        assert m is not None
    resp = admin.post(f"/auth/users/{admin_uid}/edit", data={
        "email": "admin@gmail.com", "full_name": "Administrator",
        "role_id": str(emp_role.id), "manager_id": "", "designation": "",
        "department": "", "phone": "", "is_active": "1"})
    assert resp.status_code == 302
    with flask_app.app_context():
        m = CompanyMembership.query.filter_by(
            company_id=_default_company_id(), user_id=admin_uid).first()
        assert m.role_name() == "admin", \
            "an admin must not be able to demote themselves"


# ── 4. Duplicate email refused, not a 500 ──────────────────────────────────

def test_user_edit_duplicate_email_refused(seeded):
    from shared.extensions import db
    from shared.models.base import Role, User

    admin = _login("admin@gmail.com", "admin123")
    emp_uid = _uid("emp@solarkon.com")
    with flask_app.app_context():
        emp_role = Role.query.filter_by(name=Role.EMPLOYEE).first()
        assert User.query.filter_by(email="john.doe@solarkon.com").first(), \
            "test precondition: john.doe exists and owns that email"
    resp = admin.post(f"/auth/users/{emp_uid}/edit", data={
        "email": "john.doe@solarkon.com",  # taken by another member
        "full_name": "Employee User",
        "role_id": str(emp_role.id), "manager_id": "", "designation": "",
        "department": "", "phone": "", "is_active": "1"})
    assert resp.status_code == 200, "duplicate email must not 500"
    assert b"already in use" in resp.data
    with flask_app.app_context():
        assert User.query.get(emp_uid).email == "emp@solarkon.com", \
            "duplicate email must not be saved"


# ── 5. member_assign must not collide on the global employee_code ──────────

def test_member_assign_global_code_collision_guarded(seeded):
    from shared.extensions import db
    from shared.models.base import User
    from shared.models.company import CompanyMembership

    default_id = _default_company_id()
    # X already holds EMP100 GLOBALLY (assigned earlier in another company).
    x_uid = _create_user(f"tggx_{_suffix()}@example.com", global_code="EMP100")
    with flask_app.app_context():
        assert User.query.get(x_uid).employee_code == "EMP100"
    _add_membership(default_id, x_uid, role="employee", employee_code="EMP100")

    # Y is an unassigned member of company B with no global code (empty
    # string: the column is NOT NULL and unique, and nobody else holds "").
    y_uid = _create_user(f"tggy_{_suffix()}@example.com")
    with flask_app.app_context():
        from shared.extensions import db
        User.query.get(y_uid).employee_code = ""
        db.session.commit()
    bid = _create_company("Code Collide Co", f"code-col-{_suffix()}")
    _add_membership(bid, y_uid, role="employee")
    _add_membership(bid, _uid("admin@gmail.com"), role="admin")

    admin = _login("admin@gmail.com", "admin123")
    assert admin.get(f"/company/switch/{bid}").status_code == 302
    resp = admin.post("/auth/members/assign", data={
        "user_id": str(y_uid), "employee_code": "EMP100",
        "designation": "", "department": "", "manager_id": ""})
    assert resp.status_code == 302, \
        "per-company EMP100 must not 500 on the global unique constraint"
    with flask_app.app_context():
        m = CompanyMembership.query.filter_by(
            company_id=bid, user_id=y_uid).first()
        assert m.employee_code == "EMP100", \
            "the per-company code must still be assigned"
        assert User.query.get(y_uid).employee_code is None or \
            User.query.get(y_uid).employee_code == "", \
            "the global code must not be overwritten by another company's"
