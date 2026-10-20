"""A login belongs to the person, not to any one company.

One account signs a person into every company they belong to. A company
admin could reset the password of any member — so anyone could make a
company from the portal, invite the person they wanted, and once the
invitation was accepted set a new password and sign in as them, into their
other companies too. HR's user editor went further: it resolved a super
admin's id even for non-members, so a company admin could set the platform
account's email and password. The module switches on the rights page were
the account's global flags, so one company switching a module off for a
person switched it off in every company.

Now a company changes a login (email, password, whether it can sign in) only
when the login reaches nothing else, and module access is stored per
company membership.
"""
import os
import tempfile
import uuid

import pytest

_TMP_DB = os.path.join(tempfile.gettempdir(), "erp_login_ownership.db")
if os.path.exists(_TMP_DB):
    os.remove(_TMP_DB)
os.environ["DATABASE_URL"] = (os.environ.get("TEST_DATABASE_URL")
                              or "sqlite:///" + _TMP_DB.replace("\\", "/"))

from app import app as flask_app  # noqa: E402
from shared.extensions import db  # noqa: E402

VICTIM_PW = "victim-original-pass"
NEW_PW = "attacker-chosen-pass"


def _sfx():
    return uuid.uuid4().hex[:8]


def _user(name, role="employee", **flags):
    from shared.models.base import Role, User
    r = Role.query.filter_by(name=role).first()
    defaults = dict(has_hr_access=True, has_inventory_access=True,
                    has_invoicing_access=True, has_finance_access=True,
                    has_accounting_access=True)
    defaults.update(flags)
    u = User(employee_code=f"LO{_sfx()}".upper(), email=f"{name}-{_sfx()}@example.com",
             full_name=name.title(), role_id=r.id, is_active=True, **defaults)
    u.set_password(VICTIM_PW)
    db.session.add(u)
    db.session.flush()
    return u


def _company(name, owner):
    from shared.models.company import Company
    c = Company(name=name, slug=f"{name.lower().replace(' ', '-')}-{_sfx()}",
                is_active=True, created_by=owner.id)
    db.session.add(c)
    db.session.flush()
    return c


def _member(company, user, role="employee", status="active"):
    from shared.models.base import Role
    from shared.models.company import CompanyMembership
    db.session.add(CompanyMembership(
        company_id=company.id, user_id=user.id, status=status,
        role_id=Role.query.filter_by(name=role).first().id))
    db.session.flush()


@pytest.fixture(scope="module")
def world():
    flask_app.test_client().get("/")          # seed
    from shared.models.base import User
    with flask_app.app_context():
        attacker = _user("attacker", role="admin")
        victim = _user("victim", role="admin")
        loner = _user("loner")
        own_co = _company("Attacker Co", attacker)
        victim_co = _company("Victim Co", victim)
        third_co = _company("Third Co", victim)
        _member(own_co, attacker, "admin")
        _member(victim_co, victim, "admin")
        _member(third_co, victim, "employee")
        _member(own_co, victim, "employee")       # accepted an invitation
        _member(own_co, loner, "employee")         # works here only
        second_admin = _user("second", role="admin")
        _member(victim_co, second_admin, "admin")
        sa = User.query.filter_by(is_super_admin=True).first()
        db.session.commit()
        ids = dict(attacker=attacker.id, victim=victim.id, loner=loner.id,
                   own=own_co.id, victim_co=victim_co.id, third=third_co.id,
                   second=second_admin.id, super=sa.id if sa else None)
    return ids


def _client(world, user_key, company_key):
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(world[user_key])
        s["_fresh"] = True
        s["company_id"] = world[company_key]
    return c


def _password_is(uid, pw):
    from shared.models.base import User
    with flask_app.app_context():
        return db.session.get(User, uid).check_password(pw)


def _user_row(uid):
    from shared.models.base import User
    with flask_app.app_context():
        u = db.session.get(User, uid)
        return {"email": u.email, "is_active": u.is_active}


def test_settings_rights_cannot_reset_a_login_shared_with_another_company(world):
    c = _client(world, "attacker", "own")
    page = c.get(f"/settings/users/{world['victim']}/rights").get_data(as_text=True)
    assert "also uses this login in another company" in page
    assert 'name="new_password"' not in page
    r = c.post(f"/settings/users/{world['victim']}/rights",
               data={"new_password": NEW_PW, "confirm_password": NEW_PW})
    assert r.status_code == 302
    assert _password_is(world["victim"], VICTIM_PW)
    assert not _password_is(world["victim"], NEW_PW)


def test_hr_cannot_reset_password_change_email_or_deactivate_a_shared_login(world):
    c = _client(world, "attacker", "own")
    before = _user_row(world["victim"])
    c.post(f"/auth/users/{world['victim']}/reset-password", data={"new_password": NEW_PW})
    assert _password_is(world["victim"], VICTIM_PW)
    c.post(f"/auth/users/{world['victim']}/edit",
           data={"email": "taken-over@example.com", "full_name": "Victim",
                 "role_id": "3", "is_active": "1"})
    c.post(f"/auth/users/{world['victim']}/delete")
    c.post(f"/settings/users/{world['victim']}/toggle-active")
    assert _user_row(world["victim"]) == before
    listing = c.get("/auth/users").get_data(as_text=True)
    assert f"resetPw({world['victim']}," not in listing


def test_hr_editor_still_saves_employment_details_of_a_shared_login(world):
    from shared.models.base import Role, User
    with flask_app.app_context():
        emp_role = Role.query.filter_by(name="employee").first().id
        email = db.session.get(User, world["victim"]).email
    c = _client(world, "attacker", "own")
    r = c.post(f"/auth/users/{world['victim']}/edit",
               data={"email": email, "full_name": "Victim", "role_id": str(emp_role),
                     "is_active": "1", "designation": "Analyst", "department": "Ops"})
    assert r.status_code == 302 and "/auth/users" in r.headers["Location"]
    with flask_app.app_context():
        # Employment details belong to the company the editor works in; the
        # shared account row keeps its own copy for outside a company.
        from shared.tenancy import set_current_company
        set_current_company(world["own"])
        v = db.session.get(User, world["victim"])
        assert v.employment_in(world["own"]) == (None, "Analyst", "Ops")
        assert v.home_designation is None and v.home_department is None
    assert _password_is(world["victim"], VICTIM_PW)


def test_a_login_used_only_here_can_still_be_reset(world):
    c = _client(world, "attacker", "own")
    page = c.get(f"/settings/users/{world['loner']}/rights").get_data(as_text=True)
    assert 'name="new_password"' in page
    c.post(f"/auth/users/{world['loner']}/reset-password", data={"new_password": NEW_PW})
    assert _password_is(world["loner"], NEW_PW)


def test_the_company_owner_login_is_the_owners_own(world):
    c = _client(world, "second", "victim_co")
    c.post(f"/settings/users/{world['victim']}/rights",
           data={"new_password": NEW_PW, "confirm_password": NEW_PW})
    c.post(f"/auth/users/{world['victim']}/reset-password", data={"new_password": NEW_PW})
    assert _password_is(world["victim"], VICTIM_PW)


def test_a_company_admin_cannot_reach_the_super_admin_account(world):
    if world["super"] is None:
        pytest.skip("seed made no super admin")
    from shared.models.base import User
    with flask_app.app_context():
        sa = db.session.get(User, world["super"])
        email, hashed = sa.email, sa.password_hash
    c = _client(world, "attacker", "own")
    assert c.get(f"/auth/users/{world['super']}/edit").status_code == 404
    assert c.post(f"/auth/users/{world['super']}/reset-password",
                  data={"new_password": NEW_PW}).status_code == 404
    assert c.post(f"/auth/users/{world['super']}/edit",
                  data={"email": "x@example.com"}).status_code == 404
    with flask_app.app_context():
        sa = db.session.get(User, world["super"])
        assert (sa.email, sa.password_hash) == (email, hashed)


def test_module_access_is_set_per_company(world):
    from shared.models.base import User
    from shared.tenancy import set_current_company
    from shared.permissions import MODULES
    c = _client(world, "attacker", "own")
    form = {f"module_{key}": "on" for key, *_ in MODULES if key != "inventory"}
    r = c.post(f"/settings/users/{world['victim']}/rights", data=form)
    assert r.status_code == 302
    with flask_app.app_context():
        v = db.session.get(User, world["victim"])
        assert v.has_inventory_access, "the account-wide flag is untouched"
        set_current_company(world["own"])
        assert v.module_access("inventory") is False
        assert v.module_access("invoicing") is True
        set_current_company(world["third"])
        assert v.module_access("inventory") is True, "another company is unaffected"
    page = c.get(f"/settings/users/{world['victim']}/rights").get_data(as_text=True)
    assert 'name="module_inventory"' in page
    assert 'name="module_inventory" checked' not in page.replace("\n", " ")


def test_members_never_configured_keep_the_account_flags(world):
    from shared.models.base import User
    from shared.tenancy import set_current_company
    with flask_app.app_context():
        loner = db.session.get(User, world["loner"])
        set_current_company(world["own"])
        assert loner.module_access("inventory") is True
        assert loner.module_access("fbr") is False
