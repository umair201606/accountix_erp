"""Manager, designation and department belong to each company.

They lived only on the user row that every company the person belongs to
shares. One company's HR choosing someone's manager made that person their
manager everywhere — leave, loan and timesheet approver, and the one who saw
them in "my team" — and a designation typed in one company printed on the
other company's payslips. Each company now keeps its own on the membership
(Alembic 0015 copied the old values there).
"""
import importlib.util
import os
import tempfile
import uuid

import pytest

_TMP_DB = os.path.join(tempfile.gettempdir(), "erp_membership_employment.db")
if os.path.exists(_TMP_DB):
    os.remove(_TMP_DB)
os.environ["DATABASE_URL"] = (os.environ.get("TEST_DATABASE_URL")
                              or "sqlite:///" + _TMP_DB.replace("\\", "/"))

from app import app as flask_app  # noqa: E402
from shared.extensions import db  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _sfx():
    return uuid.uuid4().hex[:8]


def _user(name, role="employee", **home):
    from shared.models.base import Role, User
    u = User(employee_code=f"ME{_sfx()}".upper(), email=f"{name}-{_sfx()}@example.com",
             full_name=name, role_id=Role.query.filter_by(name=role).first().id,
             is_active=True, has_hr_access=True, **home)
    u.set_password("employment-pass-1")
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
    m = CompanyMembership(company_id=company.id, user_id=user.id, status=status,
                          role_id=Role.query.filter_by(name=role).first().id)
    db.session.add(m)
    db.session.flush()
    return m


@pytest.fixture(scope="module")
def world():
    flask_app.test_client().get("/")          # seed
    from shared.tenancy import unscoped
    with flask_app.app_context(), unscoped():
        boss_a = _user("Boss A", "admin")
        boss_b = _user("Boss B", "admin")
        mgr_a = _user("Mia Manager", "manager")
        mgr_b = _user("Ben Manager", "manager")
        person = _user("Pat Person")
        co_a = _company("Employment A", boss_a)
        co_b = _company("Employment B", boss_b)
        _member(co_a, boss_a, "admin")
        _member(co_b, boss_b, "admin")
        _member(co_a, mgr_a, "manager")
        _member(co_b, mgr_a, "manager")      # Mia manages in both companies
        _member(co_b, mgr_b, "manager")
        _member(co_a, person)
        _member(co_b, person)
        db.session.commit()
        ids = dict(boss_a=boss_a.id, boss_b=boss_b.id, mgr_a=mgr_a.id, mgr_b=mgr_b.id,
                   person=person.id, a=co_a.id, b=co_b.id)
    return ids


def _client(world, user_key, company_key):
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(world[user_key])
        s["_fresh"] = True
        s["company_id"] = world[company_key]
    return c


def _edit(world, boss, company, **fields):
    from shared.models.base import Role, User
    with flask_app.app_context():
        u = db.session.get(User, world["person"])
        form = {"email": u.email, "full_name": u.full_name, "is_active": "1",
                "role_id": str(Role.query.filter_by(name="employee").first().id),
                "manager_id": "", "designation": "", "department": "", "phone": ""}
    form.update(fields)
    r = _client(world, boss, company).post(f"/auth/users/{world['person']}/edit", data=form)
    assert r.status_code == 302 and "/auth/users" in r.headers["Location"], r.get_data(as_text=True)[:400]


def _in(world, company_key, fn):
    from shared.tenancy import set_current_company
    with flask_app.app_context():
        set_current_company(world[company_key])
        try:
            return fn()
        finally:
            db.session.remove()


def _person(world):
    from shared.models.base import User
    return db.session.get(User, world["person"])


def test_each_company_sets_its_own_manager_and_title(world):
    _edit(world, "boss_a", "a", manager_id=str(world["mgr_a"]),
          designation="Analyst", department="Finance")
    _edit(world, "boss_b", "b", manager_id=str(world["mgr_b"]),
          designation="Consultant", department="Projects")
    in_a = _in(world, "a", lambda: (_person(world).manager_id, _person(world).designation,
                                    _person(world).department))
    in_b = _in(world, "b", lambda: (_person(world).manager_id, _person(world).designation,
                                    _person(world).department))
    assert in_a == (world["mgr_a"], "Analyst", "Finance")
    assert in_b == (world["mgr_b"], "Consultant", "Projects")
    # Changing A again leaves B as B set it.
    _edit(world, "boss_a", "a", manager_id="", designation="Senior Analyst")
    assert _in(world, "b", lambda: (_person(world).manager_id,
                                    _person(world).designation)) == (world["mgr_b"], "Consultant")
    assert _in(world, "a", lambda: _person(world).manager_id) is None


def test_the_account_row_is_not_rewritten_for_someone_in_two_companies(world):
    from shared.models.base import User
    with flask_app.app_context():
        u = db.session.get(User, world["person"])
        assert u.home_designation is None and u.home_manager_id is None


def test_leave_goes_to_the_manager_of_that_company(world):
    from hr_app.leave_flow import approver_for
    _edit(world, "boss_a", "a", manager_id=str(world["mgr_a"]), designation="Analyst")
    assert _in(world, "a", lambda: approver_for(_person(world))) == world["mgr_a"]
    # Mia also manages in B, but in B Pat reports to Ben.
    assert _in(world, "b", lambda: approver_for(_person(world))) == world["mgr_b"]


def test_my_team_follows_the_company(world):
    from shared.models.base import User

    def team(mgr):
        return lambda: {u.id for u in db.session.get(User, world[mgr]).reports_of()}
    assert world["person"] in _in(world, "a", team("mgr_a"))
    assert world["person"] not in _in(world, "b", team("mgr_a"))
    assert world["person"] in _in(world, "b", team("mgr_b"))
    page = _client(world, "mgr_a", "b").get("/mss/team").get_data(as_text=True)
    assert "Pat Person" not in page
    page = _client(world, "mgr_a", "a").get("/mss/team").get_data(as_text=True)
    assert "Pat Person" in page and "Analyst" in page


def test_a_first_company_starts_from_the_account_details(world):
    """Set on the account before any company existed (console, seeds)."""
    from shared.models.company import Company, CompanyMembership
    from shared.tenancy import unscoped
    with flask_app.app_context(), unscoped():
        mgr = db.session.get(__import__("shared.models.base", fromlist=["User"]).User,
                             world["mgr_a"])
        solo = _user("Sam Solo", home_designation="Trainee", home_department="Stores",
                     home_manager_id=mgr.id)
        first = _member(db.session.get(Company, world["a"]), solo)
        second = _member(db.session.get(Company, world["b"]), solo)
        db.session.commit()
        first = db.session.get(CompanyMembership, first.id)
        second = db.session.get(CompanyMembership, second.id)
        assert (first.manager_id, first.designation, first.department) == (
            world["mgr_a"], "Trainee", "Stores")
        assert (second.manager_id, second.designation, second.department) == (None, None, None)


def test_a_company_only_login_keeps_the_account_copy_in_step(world):
    from shared.models.base import User
    from shared.tenancy import set_current_company, unscoped
    with flask_app.app_context():
        with unscoped():
            co = db.session.get(__import__("shared.models.company", fromlist=["Company"]).Company,
                                world["a"])
            only = _user("Olu Only")
            _member(co, only)
            db.session.commit()
            oid = only.id
        set_current_company(world["a"])
        u = db.session.get(User, oid)
        u.designation = "Storekeeper"
        db.session.commit()
        db.session.remove()
        assert db.session.get(User, oid).home_designation == "Storekeeper"


def test_changing_an_email_in_hr_moves_waiting_invitations(world):
    from shared.models.base import Role, User
    from shared.models.company import CompanyInvitation
    from shared.tenancy import unscoped
    with flask_app.app_context(), unscoped():
        co = db.session.get(__import__("shared.models.company", fromlist=["Company"]).Company,
                            world["a"])
        mover = _user("Max Mover")
        _member(co, mover)
        old = mover.email
        db.session.add(CompanyInvitation(company_id=world["b"], email=old,
                                         role_id=Role.query.filter_by(name="employee").first().id,
                                         invited_by=world["boss_b"]))
        db.session.commit()
        mid, name = mover.id, mover.full_name
    new = f"moved-{_sfx()}@example.com"
    c = _client(world, "boss_a", "a")
    with flask_app.app_context():
        role = str(Role.query.filter_by(name="employee").first().id)
    r = c.post(f"/auth/users/{mid}/edit", data={
        "email": new, "full_name": name, "is_active": "1", "role_id": role,
        "manager_id": "", "designation": "", "department": "", "phone": ""})
    assert r.status_code == 302
    with flask_app.app_context():
        assert db.session.get(User, mid).email == new
        assert CompanyInvitation.query.filter_by(email=old).count() == 0
        assert CompanyInvitation.query.filter_by(email=new, status="sent").count() == 1


def _load_0015():
    path = os.path.join(ROOT, "migrations", "versions", "0015_membership_employment.py")
    spec = importlib.util.spec_from_file_location("rev_0015", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_migration_copies_the_old_values_onto_memberships(world):
    """Memberships from before 0015 (all three empty) take the user row's
    values; the manager only where they are an active member of that same
    company; memberships already set are left alone."""
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext
    from sqlalchemy import text
    from shared.models.company import Company, CompanyMembership
    from shared.tenancy import unscoped
    with flask_app.app_context(), unscoped():
        co_a = db.session.get(Company, world["a"])
        co_b = db.session.get(Company, world["b"])
        mgr_b_only = _user("Bea Bonly", "manager")
        _member(co_b, mgr_b_only, "manager")
        old = _user("Olga Oldtimer")
        m_a, m_b = _member(co_a, old), _member(co_b, old)
        kept = _user("Ken Kept")
        m_k = _member(co_a, kept)
        db.session.commit()
        ids = (m_a.id, m_b.id, m_k.id, old.id, kept.id, mgr_b_only.id)
    m_a, m_b, m_k, old_id, kept_id, mgr_b_only = ids
    with flask_app.app_context(), db.engine.begin() as conn:
        # The shape before 0015: values on the user rows only.
        conn.execute(text("UPDATE company_memberships SET manager_id = NULL, "
                          "designation = NULL, department = NULL WHERE id IN (:a, :b)"),
                     {"a": m_a, "b": m_b})
        conn.execute(text("UPDATE users SET manager_id = :m, designation = 'Clerk', "
                          "department = 'Accounts' WHERE id = :u"),
                     {"m": mgr_b_only, "u": old_id})
        conn.execute(text("UPDATE company_memberships SET designation = 'Set here' "
                          "WHERE id = :k"), {"k": m_k})
        conn.execute(text("UPDATE users SET designation = 'Account copy' WHERE id = :u"),
                     {"u": kept_id})
    mod = _load_0015()
    with flask_app.app_context(), db.engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            mod.upgrade()
            mod.upgrade()                      # running it again changes nothing
    with flask_app.app_context():
        rows = {m.id: (m.manager_id, m.designation, m.department)
                for m in CompanyMembership.query.filter(
                    CompanyMembership.id.in_([m_a, m_b, m_k]))}
    assert rows[m_a] == (None, "Clerk", "Accounts"), "Bea is not a member of A"
    assert rows[m_b] == (mgr_b_only, "Clerk", "Accounts")
    assert rows[m_k] == (None, "Set here", None)
