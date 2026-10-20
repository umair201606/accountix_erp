"""HR: who may do what — by the role held in THIS company and the HR rights.

Before: announcements were gated on the global role's permission table (a
manager here who was an employee elsewhere could not post; an employee here
who was a manager elsewhere could); anyone could delete or move any task on
any board, and task titles ran as markup on the board; a loan was approved by
whoever some company had named as the borrower's manager, even a plain
employee here; managers ran HR reports and the live attendance board over the
whole company (every PF and loan balance, every leave reason); the payroll
template with every salaried person's code and name was open to anyone; an
HR manager could restore their own narrowed HR rights; the HR rights
(employees, attendance, leaves, payroll, loans, pf) were never read; a
manager id from any company was stored on an employee; and leave waited on a
"manager" who held no manager role here.
"""
import os
import tempfile
import uuid
from datetime import date, datetime, timedelta

import pytest

_TMP_DB = os.path.join(tempfile.gettempdir(), "erp_hr_authorization.db")
if os.path.exists(_TMP_DB):
    os.remove(_TMP_DB)
os.environ["DATABASE_URL"] = (os.environ.get("TEST_DATABASE_URL")
                              or "sqlite:///" + _TMP_DB.replace("\\", "/"))

from app import app as flask_app  # noqa: E402
from shared.extensions import db  # noqa: E402


def _monday(weeks_ahead):
    d = date.today() + timedelta(weeks=weeks_ahead)
    return d - timedelta(days=d.weekday())


MON = _monday(4)


@pytest.fixture(scope="module")
def world():
    flask_app.test_client().get("/")
    from shared.models.base import Role, User, UserPermission
    from shared.models.company import Company, CompanyMembership
    from shared.tenancy import set_current_company, unscoped
    from shared.company_setup import provision_company
    from hr_app.models.leave import LeaveType

    with flask_app.app_context():
        roles = {r.name: r.id for r in Role.query.all()}

        def mk(name, global_role):
            tag = uuid.uuid4().hex[:8]
            u = User(employee_code=f"AZ{tag}".upper(), email=f"{tag}@authz.test",
                     login_id=f"{tag}@authz.test", full_name=name,
                     role_id=roles[global_role], is_active=True, has_hr_access=True)
            u.set_password("pw12345")
            db.session.add(u)
            db.session.flush()
            return u

        with unscoped():
            founder = User.query.filter_by(is_super_admin=True).order_by(User.id).first()
            comp = Company(name="Authz Co", slug="authz-co-e2e", is_active=True,
                           created_by=founder.id)
            other = Company(name="Authz Other", slug="authz-other-e2e", is_active=True,
                            created_by=founder.id)
            db.session.add_all([comp, other])
            db.session.flush()
            # Global roles deliberately disagree with the company roles.
            boss = mk("Bea Boss", Role.EMPLOYEE)
            mgr = mk("Max Manager", Role.EMPLOYEE)
            mgr2 = mk("Nia Narrowed", Role.EMPLOYEE)
            emp = mk("Eli Employee", Role.MANAGER)
            emp2 = mk("Ema Employee", Role.EMPLOYEE)
            peer = mk("Pat Peer", Role.EMPLOYEE)
            borrower = mk("Bo Borrower", Role.EMPLOYEE)
            outsider = mk("Oz Outsider", Role.MANAGER)
            emp.manager_id = mgr.id
            emp2.manager_id = mgr2.id
            borrower.manager_id = peer.id       # an employee here
            for u, role in ((boss, Role.ADMIN), (mgr, Role.MANAGER), (mgr2, Role.MANAGER),
                            (emp, Role.EMPLOYEE), (emp2, Role.EMPLOYEE),
                            (peer, Role.EMPLOYEE), (borrower, Role.EMPLOYEE)):
                db.session.add(CompanyMembership(company_id=comp.id, user_id=u.id,
                                                 role_id=roles[role],
                                                 status=CompanyMembership.ACTIVE))
            db.session.add(CompanyMembership(company_id=other.id, user_id=outsider.id,
                                             role_id=roles[Role.MANAGER],
                                             status=CompanyMembership.ACTIVE))
            db.session.commit()
            cid = comp.id
        set_current_company(cid)
        provision_company(cid)
        set_current_company(cid)
        lt = LeaveType(name="Authz Annual", code="AZA", default_quota=10)
        db.session.add(lt)
        # mgr2's rights narrowed by an admin: a row per section governs.
        narrowed = {
            "employees": dict(can_view=True, can_create=False, can_edit=False, can_delete=False),
            "attendance": dict(can_view=True, can_edit=False),
            "leaves": dict(can_view=True, can_approve=False),
            "payroll": dict(can_view=True, can_edit=False),
            "loans": dict(can_view=True, can_approve=False),
            "pf": dict(can_view=False),
        }
        for resource, flags in narrowed.items():
            db.session.add(UserPermission(user_id=mgr2.id, resource=resource, **flags))
        db.session.commit()
        w = {"company": cid, "boss": boss.id, "mgr": mgr.id, "mgr2": mgr2.id,
             "emp": emp.id, "emp2": emp2.id, "peer": peer.id,
             "borrower": borrower.id, "outsider": outsider.id, "lt": lt.id}

    def client(uid):
        c = flask_app.test_client()
        with c.session_transaction() as s:
            s["_user_id"] = str(uid)
            s["_fresh"] = True
            s["company_id"] = cid
        return c
    w["c"] = {k: client(w[k]) for k in ("boss", "mgr", "mgr2", "emp", "emp2",
                                        "peer", "borrower")}
    return w


class Ctx:
    def __init__(self, w):
        self.w = w

    def __enter__(self):
        from shared.tenancy import set_current_company
        self.cm = flask_app.app_context()
        self.cm.__enter__()
        set_current_company(self.w["company"])
        return self

    def __exit__(self, *a):
        db.session.remove()
        self.cm.__exit__(*a)


# ── Workplace ───────────────────────────────────────────────────────────────

def _announcements(w, title):
    from hr_app.models.workplace import Announcement
    with Ctx(w):
        return Announcement.query.filter_by(title=title).count()


def test_announcements_follow_the_role_held_here(world):
    c = world["c"]
    c["emp"].post("/workplace/announcements/create",
                  data={"title": "From an employee", "content": "x"})
    assert _announcements(world, "From an employee") == 0, \
        "an employee here (a manager elsewhere) posted company-wide"
    r = c["mgr"].post("/workplace/announcements/create",
                      data={"title": "From a manager", "content": "x"})
    assert r.status_code == 302
    assert _announcements(world, "From a manager") == 1, \
        "a manager here (an employee elsewhere) could not post"
    assert b'id="ann-form"' not in c["emp"].get("/workplace/").data
    assert b'id="ann-form"' in c["mgr"].get("/workplace/").data


def test_calendar_events_need_a_manager_or_admin(world):
    from hr_app.models.workplace import TeamEvent
    day = (date.today() + timedelta(days=9)).isoformat()
    world["c"]["emp"].post("/workplace/events/create",
                           data={"title": "Emp party", "event_date": day})
    world["c"]["mgr"].post("/workplace/events/create",
                           data={"title": "Team sync", "event_date": day})
    r = world["c"]["mgr"].post("/workplace/events/create",
                               data={"title": "Bad date", "event_date": "31-31-2026"})
    assert r.status_code == 302, "a bad date was a 500"
    with Ctx(world):
        titles = {e.title for e in TeamEvent.query.all()}
    assert "Team sync" in titles and "Emp party" not in titles
    assert b'id="event-form"' not in world["c"]["emp"].get("/workplace/events").data


def test_kanban_tasks_belong_to_the_board_owner_and_the_assignee(world):
    from hr_app.models.workplace import KanbanTask
    c = world["c"]
    bid = c["emp"].post("/workplace/kanban/create-board", data={"title": "Eli's board"}).get_json()["id"]
    r = c["emp"].post(f"/workplace/kanban/{bid}/add-task",
                      data={"title": "Write report", "assignee_id": world["emp2"]})
    assert r.status_code == 200, r.get_data(as_text=True)
    tid = r.get_json()["id"]
    # Someone else's board: no adding, moving or deleting.
    assert c["peer"].post(f"/workplace/kanban/{bid}/add-task",
                          data={"title": "Sneaky"}).status_code == 403
    assert c["peer"].post(f"/workplace/kanban/update-task/{tid}",
                          json={"status": "done"}).status_code == 403
    assert c["peer"].post(f"/workplace/kanban/delete-task/{tid}").status_code == 403
    # The assignee moves it, but does not delete it.
    assert c["emp2"].post(f"/workplace/kanban/update-task/{tid}",
                          json={"status": "in_progress"}).status_code == 200
    assert c["emp2"].post(f"/workplace/kanban/update-task/{tid}",
                          json={"status": "nonsense"}).status_code == 400
    assert c["emp2"].post(f"/workplace/kanban/delete-task/{tid}").status_code == 403
    # Not someone outside this company.
    assert c["emp"].post(f"/workplace/kanban/{bid}/add-task",
                         data={"title": "Outside", "assignee_id": world["outsider"]}).status_code == 400
    tasks = c["peer"].get(f"/workplace/kanban/{bid}/tasks").get_json()
    assert [t["can_delete"] for t in tasks] == [False]
    with Ctx(world):
        t = db.session.get(KanbanTask, tid)
        assert t.status == "in_progress"
    assert c["emp"].post(f"/workplace/kanban/delete-task/{tid}").status_code == 200
    with Ctx(world):
        assert db.session.get(KanbanTask, tid) is None


def test_the_board_treats_task_titles_as_text(world):
    body = world["c"]["emp"].get("/workplace/kanban?board=1").get_data(as_text=True)
    assert "function esc(" in body and "${esc(t.title)}" in body
    assert "${t.title}" not in body


# ── Loans, salary revisions ─────────────────────────────────────────────────

def _loan(w, who):
    from hr_app.models.loan import LoanAdvanceRequest
    with Ctx(w):
        loan = LoanAdvanceRequest(user_id=w[who], request_type="loan", amount=20000,
                                  purpose="test", installment_months=4,
                                  monthly_installment=5000, remaining_amount=20000)
        db.session.add(loan)
        db.session.commit()
        return loan.id


def _loan_status(w, lid):
    from hr_app.models.loan import LoanAdvanceRequest
    with Ctx(w):
        return db.session.get(LoanAdvanceRequest, lid).status


def test_a_loan_is_decided_by_a_manager_here_with_the_loans_right(world):
    c = world["c"]
    lid = _loan(world, "borrower")
    hdr = {"Accept": "application/json"}
    # The borrower's manager_id points at a plain employee of this company.
    assert c["peer"].post(f"/mss/approve-loan/{lid}", data={"action": "approve"},
                          headers=hdr).status_code == 403
    assert c["mgr"].post(f"/mss/approve-loan/{lid}", data={"action": "approve"},
                         headers=hdr).status_code == 403, "not their report"
    assert _loan_status(world, lid) == "pending"
    lid2 = _loan(world, "emp2")
    r = c["mgr2"].post(f"/mss/approve-loan/{lid2}", data={"action": "approve"}, headers=hdr)
    assert r.status_code == 403 and "Loans approve" in r.get_json()["error"]
    assert _loan_status(world, lid2) == "pending"
    lid3 = _loan(world, "emp")
    assert c["mgr"].post(f"/mss/approve-loan/{lid3}", data={"action": "approve"},
                         headers=hdr).status_code == 200
    assert _loan_status(world, lid3) == "approved"
    assert c["boss"].post(f"/mss/approve-loan/{lid}", data={"action": "reject"},
                          headers=hdr).status_code == 200
    assert _loan_status(world, lid) == "rejected"


def test_a_salary_revision_needs_the_payroll_edit_right(world):
    from hr_app.models.compensation import SalaryRevision
    c = world["c"]
    c["mgr2"].post(f"/mss/evaluate/{world['emp2']}", data={"new_basic": "90000", "reason": "x"})
    c["mgr"].post(f"/mss/evaluate/{world['emp']}", data={"new_basic": "80000", "reason": "x"})
    with Ctx(world):
        got = {r.user_id for r in SalaryRevision.query.all()}
    assert world["emp"] in got
    assert world["emp2"] not in got, "a manager without Payroll edit revised a salary"


# ── Attendance ──────────────────────────────────────────────────────────────

def test_live_board_and_corrections_follow_the_team_and_the_rights(world):
    from hr_app.models.attendance import Attendance
    from shared.clock import local_today
    with Ctx(world):
        today = local_today()
        ids = {}
        for who in ("emp", "emp2"):
            a = Attendance(user_id=world[who], date=today,
                           clock_in=datetime.combine(today, datetime.min.time()) + timedelta(hours=9))
            db.session.add(a)
            db.session.flush()
            ids[who] = a.id
        db.session.commit()
    live = world["c"]["mgr"].get("/attendance/live").get_json()
    names = {row["name"] for row in live}
    assert "Eli Employee" in names and "Ema Employee" not in names, \
        "a manager saw the whole company's live status"
    hdr = {"Accept": "application/json"}
    r = world["c"]["mgr2"].post("/attendance/correct", headers=hdr, data={
        "attendance_id": ids["emp2"], "field": "status", "new_value": "absent",
        "reason": "test"})
    assert r.status_code == 403, "a manager without Attendance edit corrected a record"
    r = world["c"]["mgr"].post("/attendance/correct", headers=hdr, data={
        "attendance_id": ids["emp"], "field": "status", "new_value": "late",
        "reason": "test"})
    assert r.status_code == 200, r.get_data(as_text=True)


# ── HR reports ──────────────────────────────────────────────────────────────

def test_a_manager_reports_on_their_own_team(world):
    c = world["c"]
    r = c["mgr"].post("/reports/query", json={"scope": "company",
                                               "columns": ["employee", "pf_balance"]})
    names = {row["employee"] for row in r.get_json()["results"]}
    assert names == {"Eli Employee"}, names
    r = c["mgr2"].post("/reports/query", json={"scope": "company",
                                                "columns": ["employee", "pf_balance"]})
    assert r.status_code == 403, "PF balances without the PF view right"
    r = c["boss"].post("/reports/query", json={"scope": "individual", "user_id": 999999,
                                                "columns": ["employee"]})
    assert r.status_code == 404, "an unknown employee was a 500"
    r = c["mgr"].post("/reports/query", json={"scope": "individual",
                                               "user_id": world["emp2"],
                                               "columns": ["employee"]})
    assert r.status_code == 403
    csv = c["mgr"].get("/reports/export-attendance?format=csv").get_data(as_text=True)
    assert "Ema Employee" not in csv
    everyone = c["boss"].get("/reports/export-attendance?format=csv").get_data(as_text=True)
    assert "Ema Employee" in everyone and "Eli Employee" in everyone


def test_the_payroll_template_is_for_admins(world):
    r = world["c"]["emp"].get("/compensation/download-template")
    assert r.status_code == 302, "the staff list downloaded by an employee"
    r = world["c"]["boss"].get("/compensation/download-template")
    assert r.status_code == 200 and r.mimetype.endswith("sheet")


# ── HR user management ──────────────────────────────────────────────────────

def _perms(w, uid, resource):
    from shared.models.base import UserPermission
    with Ctx(w):
        p = UserPermission.query.filter_by(user_id=uid, resource=resource).first()
        return None if p is None else (p.can_view, p.can_edit)


def test_hr_rights_cannot_be_given_to_yourself(world):
    before = _perms(world, world["mgr2"], "payroll")
    form = {f"perm_{s}_{a}": "on" for s in ("employees", "attendance", "leaves",
                                             "payroll", "loans", "pf")
            for a in ("view", "create", "edit", "approve", "delete")}
    world["c"]["mgr2"].post(f"/auth/users/{world['mgr2']}/hr-rights", data=form)
    assert _perms(world, world["mgr2"], "payroll") == before
    world["c"]["mgr"].post(f"/auth/users/{world['mgr']}/hr-rights", data=form)
    assert _perms(world, world["mgr"], "payroll") is None


def test_the_employees_rights_govern_hr_staff(world):
    c = world["c"]
    assert c["mgr2"].get("/auth/users").status_code == 200          # view kept
    r = c["mgr2"].get(f"/auth/users/{world['emp2']}/edit")
    assert r.status_code == 302, "edit page open without the Employees edit right"
    r = c["mgr2"].post(f"/auth/users/{world['emp2']}/delete")
    from shared.models.base import User
    with Ctx(world):
        assert db.session.get(User, world["emp2"]).is_active
    assert c["mgr"].get(f"/auth/users/{world['emp']}/edit").status_code == 200


def _edit_form(w, who, **over):
    from shared.models.base import User
    with Ctx(w):
        u = db.session.get(User, w[who])
        m = u.membership_in(w["company"])
        data = {"email": u.email, "full_name": u.full_name, "role_id": m.role_id,
                "manager_id": u.manager_id or "", "designation": "", "department": "",
                "phone": "", "is_active": "1"}
    data.update(over)
    return data


def test_the_manager_must_be_one_of_this_companys_managers(world):
    from shared.models.base import User
    c = world["c"]
    c["mgr"].post(f"/auth/users/{world['emp']}/edit",
                  data=_edit_form(world, "emp", manager_id=world["outsider"]))
    c["mgr"].post(f"/auth/users/{world['emp']}/edit",
                  data=_edit_form(world, "emp", manager_id=world["emp"]))
    with Ctx(world):
        assert db.session.get(User, world["emp"]).manager_id == world["mgr"]
    c["mgr"].post(f"/auth/users/{world['emp']}/edit",
                  data=_edit_form(world, "emp", manager_id=world["mgr2"]))
    with Ctx(world):
        assert db.session.get(User, world["emp"]).manager_id == world["mgr2"]
        db.session.get(User, world["emp"]).manager_id = world["mgr"]
        db.session.commit()


# ── Leave routing ───────────────────────────────────────────────────────────

def _apply(w, who, start):
    return w["c"][who].post("/leaves/apply", data={
        "leave_type": w["lt"], "start_date": start.isoformat(),
        "end_date": start.isoformat(), "reason": "family"})


def _approver(w, who):
    from hr_app.models.leave import LeaveApproval, LeaveRequest
    with Ctx(w):
        lr = (LeaveRequest.query.filter_by(user_id=w[who])
              .order_by(LeaveRequest.id.desc()).first())
        a = LeaveApproval.query.filter_by(leave_request_id=lr.id).first()
        return a.approver_id if a else None


def test_leave_goes_to_someone_who_can_decide_it(world):
    _apply(world, "borrower", MON)
    assert _approver(world, "borrower") == world["boss"], \
        "leave waited on a 'manager' who is a plain employee here"
    _apply(world, "emp2", MON + timedelta(days=1))
    assert _approver(world, "emp2") == world["boss"], \
        "leave routed to a manager without the Leaves approve right"
    _apply(world, "emp", MON + timedelta(days=2))
    assert _approver(world, "emp") == world["mgr"]
