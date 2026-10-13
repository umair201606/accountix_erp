"""Attendance: the company's clock, corrections that work, policy hours.

Before: clock-in/out used the server clock (UTC on Vercel — a 09:30 Lahore
clock-in was stored as 04:30 and never late); an admin correction assigned
the form's text to a DateTime column (500) and never re-derived late /
half-day / overtime, and the plain correction form landed on a JSON page;
overtime was split at a fixed 8 h / 4 h whatever the time policy said.
"""
import os
import tempfile
from datetime import date, datetime, time, timedelta

import pytest
from dateutil import tz

_TMP_DB = os.path.join(tempfile.gettempdir(), "erp_attendance_flow.db")
if os.path.exists(_TMP_DB):
    os.remove(_TMP_DB)
os.environ["DATABASE_URL"] = (os.environ.get("TEST_DATABASE_URL")
                              or "sqlite:///" + _TMP_DB.replace("\\", "/"))

from app import app as flask_app  # noqa: E402
from shared.extensions import db  # noqa: E402

DAY = date.today() - timedelta(days=3)


@pytest.fixture(scope="module")
def world():
    c = flask_app.test_client()
    c.get("/")
    from shared.models.base import Role, User
    from shared.models.company import Company, CompanyMembership
    from shared.tenancy import set_current_company, unscoped
    from hr_app.models.attendance import Attendance
    with flask_app.app_context():
        admin = User.query.filter_by(is_super_admin=True).order_by(User.id).first()
        with unscoped():
            comp = Company(name="Clock Co", slug="clock-co-e2e", is_active=True,
                           created_by=admin.id, timezone="Asia/Karachi")
            db.session.add(comp)
            db.session.flush()
            role = Role.query.filter_by(name=Role.ADMIN).first()
            db.session.add(CompanyMembership(company_id=comp.id, user_id=admin.id,
                                             role_id=role.id,
                                             status=CompanyMembership.ACTIVE))
            db.session.commit()
            cid = comp.id
        set_current_company(cid)
        att = Attendance(user_id=admin.id, date=DAY, status="present",
                         clock_in=datetime.combine(DAY, time(9, 0)),
                         clock_out=datetime.combine(DAY, time(17, 0)),
                         is_late=False, is_half_day=False)
        db.session.add(att)
        db.session.commit()
        w = {"company": cid, "user": admin.id, "att": att.id}
    with c.session_transaction() as s:
        s["_user_id"] = str(w["user"])
        s["_fresh"] = True
        s["company_id"] = cid
    w["client"] = c
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


def record(w):
    from hr_app.models.attendance import Attendance
    from hr_app.models.holiday import OvertimeAccount
    with Ctx(w):
        a = db.session.get(Attendance, w["att"])
        oa = OvertimeAccount.query.filter_by(user_id=w["user"], date=DAY).first()
        return {"in": a.clock_in, "out": a.clock_out, "late": a.is_late,
                "half": a.is_half_day, "status": a.status,
                "reg": oa and float(oa.regular_hours), "ot": oa and float(oa.overtime_hours)}


def correct(w, field, value, json=False):
    return w["client"].post("/attendance/correct", data={
        "attendance_id": w["att"], "field": field, "new_value": value,
        "reason": "badge reader down"},
        headers={"Accept": "application/json"} if json else {})


def test_01_clock_in_uses_the_company_clock(world):
    r = world["client"].post("/attendance/clock-in")
    assert r.status_code == 200, r.get_data(as_text=True)
    from hr_app.models.attendance import Attendance
    with Ctx(world):
        today = datetime.now(tz.gettz("Asia/Karachi")).date()
        a = Attendance.query.filter_by(user_id=world["user"], date=today).first()
        assert a is not None, "attendance must be filed under the company's date"
        local = datetime.now(tz.gettz("Asia/Karachi")).replace(tzinfo=None)
        assert abs((a.clock_in - local).total_seconds()) < 120


def test_02_correcting_clock_in_parses_and_rederives(world):
    r = correct(world, "clock_in", "10:00")
    assert r.status_code == 302, "plain form must redirect, not show JSON"
    rec = record(world)
    assert rec["in"] == datetime.combine(DAY, time(10, 0))
    assert rec["late"] is True and rec["half"] is False
    assert (rec["reg"], rec["ot"]) == (7.0, 0.0)


def test_03_correcting_clock_out_books_overtime_by_policy(world):
    correct(world, "clock_out", "20:00")
    assert (record(world)["reg"], record(world)["ot"]) == (8.0, 2.0)
    from hr_app.models.holiday import TimePolicy
    with Ctx(world):
        db.session.add(TimePolicy(name="Long days", department="", is_active=True,
                                  shift_start=time(9, 0), shift_end=time(19, 0),
                                  grace_period_minutes=15, max_regular_hours=9,
                                  max_overtime_hours=4, max_consecutive_days=6))
        db.session.commit()
    correct(world, "clock_out", "20:00")
    assert (record(world)["reg"], record(world)["ot"]) == (9.0, 1.0)


def test_04_bad_values_are_refused_not_500(world):
    r = correct(world, "clock_in", "half past nine", json=True)
    assert r.status_code == 400
    r = correct(world, "clock_out", "08:00", json=True)
    assert r.status_code == 400 and "after" in r.get_json()["error"]
    r = correct(world, "status", "on the moon", json=True)
    assert r.status_code == 400
    assert record(world)["in"] == datetime.combine(DAY, time(10, 0))


def test_05_late_afternoon_clock_in_is_a_half_day(world):
    correct(world, "clock_in", "14:00")
    rec = record(world)
    assert rec["half"] is True and rec["status"] == "half-day"


def test_06_the_clock_follows_the_company_zone_not_the_server(world):
    from shared.clock import local_now
    from shared.models.company import Company
    from shared.tenancy import unscoped
    for zone in ("UTC", "America/New_York", "Asia/Karachi"):
        with Ctx(world):
            with unscoped():
                db.session.get(Company, world["company"]).timezone = zone
                db.session.commit()
        with flask_app.test_request_context():
            from shared.tenancy import set_current_company
            set_current_company(world["company"])
            got = local_now()
        want = datetime.now(tz.gettz(zone)).replace(tzinfo=None)
        assert abs((got - want).total_seconds()) < 60, zone
