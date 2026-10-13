"""Leave: apply, route to an approver, decide — and the balance stays right.

Before: approving the same request twice (double click, back button) counted
its days twice; rejecting an approved one subtracted days that were no longer
pending; ``remaining`` (the headline figure) was never updated; an employee
without a manager in this company got no approval row, so the request stayed
pending for ever; people who joined after the seed had no quota, so the
balance check was skipped; the same dates could be applied for twice; the MSS
Approve / Reject buttons landed on a bare {"success": true} page.
"""
import os
import tempfile
import uuid
from datetime import date, timedelta

import pytest

_TMP_DB = os.path.join(tempfile.gettempdir(), "erp_leave_flow.db")
if os.path.exists(_TMP_DB):
    os.remove(_TMP_DB)
os.environ["DATABASE_URL"] = (os.environ.get("TEST_DATABASE_URL")
                              or "sqlite:///" + _TMP_DB.replace("\\", "/"))

from app import app as flask_app  # noqa: E402
from shared.extensions import db  # noqa: E402


def _monday(weeks_ahead):
    d = date.today() + timedelta(weeks=weeks_ahead)
    return d - timedelta(days=d.weekday())


MON = _monday(3)            # Mon..Tue of a future week = 2 working days
TUE = MON + timedelta(days=1)
FRI = MON + timedelta(days=4)


@pytest.fixture(scope="module")
def world():
    flask_app.test_client().get("/")
    from shared.models.base import Role, User
    from shared.models.company import Company, CompanyMembership
    from shared.tenancy import set_current_company, unscoped
    from shared.company_setup import provision_company
    from hr_app.models.leave import LeaveType

    def mk_user(name, role):
        r = Role.query.filter_by(name=role).first()
        tag = uuid.uuid4().hex[:8]
        u = User(employee_code=f"LV{tag}".upper(), email=f"{tag}@leave.test",
                 login_id=f"{tag}@leave.test", full_name=name, role_id=r.id,
                 is_active=True, has_hr_access=True)
        u.set_password("pw12345")
        db.session.add(u)
        db.session.flush()
        return u

    with flask_app.app_context():
        admin = (User.query.filter_by(is_super_admin=True).order_by(User.id).first())
        with unscoped():
            comp = Company(name="Leave Co", slug="leave-co-e2e", is_active=True,
                           created_by=admin.id)
            db.session.add(comp)
            db.session.flush()
            roles = {r.name: r.id for r in Role.query.all()}
            mgr = mk_user("Mona Manager", Role.MANAGER)
            emp = mk_user("Ed Employee", Role.EMPLOYEE)
            outsider = mk_user("Oscar Outsider", Role.MANAGER)   # not a member
            solo = mk_user("Fay NoManager", Role.EMPLOYEE)
            emp.manager_id = mgr.id
            solo.manager_id = outsider.id
            for u, role in ((admin, Role.ADMIN), (mgr, Role.MANAGER),
                            (emp, Role.EMPLOYEE), (solo, Role.EMPLOYEE)):
                db.session.add(CompanyMembership(company_id=comp.id, user_id=u.id,
                                                 role_id=roles[role],
                                                 status=CompanyMembership.ACTIVE))
            db.session.commit()
            cid = comp.id
        set_current_company(cid)
        provision_company(cid)
        set_current_company(cid)
        lt = LeaveType(name="Annual Test", code="ATL", default_quota=5)
        db.session.add(lt)
        db.session.commit()
        w = {"company": cid, "admin": admin.id, "mgr": mgr.id, "emp": emp.id,
             "solo": solo.id, "lt": lt.id}

    def client(uid):
        c = flask_app.test_client()
        with c.session_transaction() as s:
            s["_user_id"] = str(uid)
            s["_fresh"] = True
            s["company_id"] = cid
        return c
    w["c"] = {k: client(w[k]) for k in ("admin", "mgr", "emp", "solo")}
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


def quota(w, who):
    from hr_app.models.leave import LeaveQuota
    with Ctx(w):
        q = LeaveQuota.query.filter_by(user_id=w[who], leave_type_id=w["lt"],
                                       year=MON.year).first()
        return None if q is None else (float(q.total), float(q.used),
                                       float(q.pending), float(q.remaining))


def requests_of(w, who):
    from hr_app.models.leave import LeaveRequest
    with Ctx(w):
        return [(r.id, r.status, float(r.total_days)) for r in
                LeaveRequest.query.filter_by(user_id=w[who]).order_by(LeaveRequest.id)]


def approval_of(w, request_id):
    from hr_app.models.leave import LeaveApproval
    with Ctx(w):
        a = LeaveApproval.query.filter_by(leave_request_id=request_id).first()
        return None if a is None else (a.id, a.approver_id, a.status)


def apply(w, who, start, end, half=False):
    data = {"leave_type": w["lt"], "start_date": start.isoformat(),
            "end_date": end.isoformat(), "reason": "family"}
    if half:
        data["is_half_day"] = "on"
    return w["c"][who].post("/leaves/apply", data=data)


def test_01_apply_routes_to_the_manager_and_holds_the_days(world):
    r = apply(world, "emp", MON, TUE)
    assert r.status_code == 302
    [(rid, status, days)] = requests_of(world, "emp")
    assert (status, days) == ("pending", 2.0)
    assert approval_of(world, rid)[1:] == (world["mgr"], "pending")
    # A quota was created from the type's default: the employee had none.
    assert quota(world, "emp") == (5.0, 0.0, 2.0, 3.0)
    world["r1"] = rid


def test_02_mss_approve_redirects_and_moves_days_to_used(world):
    aid = approval_of(world, world["r1"])[0]
    r = world["c"]["mgr"].post(f"/mss/approve-leave/{aid}", data={"action": "approve"})
    assert r.status_code == 302, "the plain form must not land on a JSON page"
    assert requests_of(world, "emp")[0][1] == "approved"
    assert quota(world, "emp") == (5.0, 2.0, 0.0, 3.0)


def test_03_a_second_decision_is_refused(world):
    aid = approval_of(world, world["r1"])[0]
    c = world["c"]["mgr"]
    c.post(f"/mss/approve-leave/{aid}", data={"action": "approve"})
    assert quota(world, "emp") == (5.0, 2.0, 0.0, 3.0), "approving twice counted twice"
    c.post(f"/mss/approve-leave/{aid}", data={"action": "reject", "comment": "oops"})
    assert requests_of(world, "emp")[0][1] == "approved"
    assert quota(world, "emp") == (5.0, 2.0, 0.0, 3.0)
    r = c.post(f"/mss/approve-leave/{aid}", data={"action": "approve"},
               headers={"Accept": "application/json"})
    assert r.status_code == 400 and "already" in r.get_json()["error"]


def test_04_overlapping_dates_are_refused(world):
    apply(world, "emp", TUE, FRI)
    assert len(requests_of(world, "emp")) == 1


def test_05_balance_is_enforced(world):
    apply(world, "emp", FRI + timedelta(days=3), FRI + timedelta(days=7))  # 5 days > 3 left
    assert len(requests_of(world, "emp")) == 1


def test_06_half_day_keeps_its_half(world):
    apply(world, "emp", FRI, FRI, half=True)
    rid, status, days = requests_of(world, "emp")[-1]
    assert (status, days) == ("pending", 0.5)
    assert quota(world, "emp") == (5.0, 2.0, 0.5, 2.5)
    page = world["c"]["emp"].get("/leaves/").get_data(as_text=True)
    assert "2.5" in page


def test_07_half_day_must_be_one_date(world):
    before = len(requests_of(world, "emp"))
    apply(world, "emp", MON + timedelta(weeks=2), TUE + timedelta(weeks=2), half=True)
    assert len(requests_of(world, "emp")) == before


def test_08_no_manager_in_this_company_goes_to_an_admin(world):
    apply(world, "solo", MON, TUE)
    [(rid, status, _)] = requests_of(world, "solo")
    assert status == "pending"
    assert approval_of(world, rid)[1] == world["admin"], \
        "manager outside the company must not be the approver"
    world["r_solo"] = rid


def test_09_rejection_needs_a_reason_then_frees_the_days(world):
    aid = approval_of(world, world["r_solo"])[0]
    c = world["c"]["admin"]
    c.post(f"/leaves/review/{aid}", data={"action": "reject", "comment": ""})
    assert requests_of(world, "solo")[0][1] == "pending"
    assert quota(world, "solo")[2] == 2.0
    c.post(f"/leaves/review/{aid}", data={"action": "reject", "comment": "busy week"})
    assert requests_of(world, "solo")[0][1] == "rejected"
    assert quota(world, "solo") == (5.0, 0.0, 0.0, 5.0)


def test_10_only_the_approver_decides(world):
    apply(world, "solo", MON + timedelta(weeks=1), MON + timedelta(weeks=1))
    rid = requests_of(world, "solo")[-1][0]
    aid = approval_of(world, rid)[0]
    world["c"]["mgr"].post(f"/mss/approve-leave/{aid}", data={"action": "approve"})
    assert requests_of(world, "solo")[-1][1] == "pending"


def test_11_missing_dates_do_not_500(world):
    r = world["c"]["emp"].post("/leaves/apply", data={"leave_type": world["lt"],
                                                      "reason": "x"})
    assert r.status_code == 200


def test_12_ess_shows_live_balances(world):
    html = world["c"]["emp"].get("/ess/").get_data(as_text=True)
    assert "Annual Test" in html and "2.5" in html


# ── Timesheets: same approver routing, frozen once submitted ──

def _week(w, who):
    from hr_app.models.timesheet import TimesheetWeek
    w["c"][who].get("/timesheets/")
    with Ctx(w):
        wk = TimesheetWeek.query.filter_by(user_id=w[who]).order_by(TimesheetWeek.id.desc()).first()
        return wk.id, wk.week_start, wk.status


def test_13_timesheet_entry_is_validated(world):
    wid, start, _ = _week(world, "emp")
    c = world["c"]["emp"]
    assert c.post("/timesheets/add-entry", data={"week_id": wid, "day": "", "task": "x",
                                                 "hours": 2}).status_code == 400
    assert c.post("/timesheets/add-entry", data={"week_id": wid, "day": start.isoformat(),
                                                 "task": "x", "hours": 30}).status_code == 400
    outside = (start - timedelta(days=1)).isoformat()
    assert c.post("/timesheets/add-entry", data={"week_id": wid, "day": outside,
                                                 "task": "x", "hours": 2}).status_code == 400
    r = c.post("/timesheets/add-entry", data={"week_id": wid, "day": start.isoformat(),
                                              "task": "Site survey", "hours": 6})
    assert r.status_code == 200


def test_14_submit_routes_to_the_manager_once_and_freezes(world):
    from hr_app.models.timesheet import TimesheetApproval
    wid, start, _ = _week(world, "emp")
    c = world["c"]["emp"]
    c.post(f"/timesheets/submit/{wid}")
    c.post(f"/timesheets/submit/{wid}")
    with Ctx(world):
        apps = TimesheetApproval.query.filter_by(week_id=wid).all()
        assert [a.approver_id for a in apps] == [world["mgr"]]
        world["ts_aid"] = apps[0].id
    r = c.post("/timesheets/add-entry", data={"week_id": wid, "day": start.isoformat(),
                                              "task": "late add", "hours": 1})
    assert r.status_code == 400


def test_15_mss_timesheet_approve_redirects_and_is_final(world):
    aid = world["ts_aid"]
    c = world["c"]["mgr"]
    assert c.post(f"/mss/approve-timesheet/{aid}", data={"action": "approve"}).status_code == 302
    r = c.post(f"/mss/approve-timesheet/{aid}", data={"action": "reject", "comment": "no"},
               headers={"Accept": "application/json"})
    assert r.status_code == 400
    assert _week(world, "emp")[2] == "approved"


def test_16_merge_report_works(world):
    wid, start, _ = _week(world, "emp")
    r = world["c"]["admin"].post("/timesheets/merge-report", data={
        "period_start": (start - timedelta(days=7)).isoformat(),
        "period_end": (start + timedelta(days=7)).isoformat()})
    assert r.status_code == 200, "merge report used an unimported name (NameError)"
    assert any(x["user"] == "Ed Employee" and x["total_hours"] == 6
               for x in r.get_json()["results"])
