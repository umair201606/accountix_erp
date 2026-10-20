"""Sign-in, sessions and a person's own login.

Same harness as test_superadmin_console.py: a throwaway sqlite DB seeded by
the app on first request, flows driven through the Flask test client.

Covers:
  - the sign-in throttle (counted from the audit log's failed sign-ins:
    one login from one address, one address for any login), the same for
    the console door, and a successful sign-in clearing the login's count
  - an unknown login costs a password hash too (no timing tell)
  - sessions: a new password signs out every other session of the login,
    the person who changed it stays in; a deactivated login is signed out
    on its next request; sessions from before fingerprints still work;
    nothing from before sign-in survives it
  - redirects after sign-in stay on this site; a cross-site link cannot
    sign anyone out; the console's pages send a signed-out visitor to the
    console door
  - new passwords: at least 8 characters and not a common one, everywhere
  - User ID / email changes need the current password, cannot take another
    account's email, and pending invitations follow the new email
  - a suspended company cannot be entered, even by people already inside
  - sign-up requests: a blocked one stays blocked; approving cannot make a
    second account for an email already in use, nor run twice
"""
import os
import tempfile
import uuid

import pytest

_TMP_DB = os.path.join(tempfile.gettempdir(), "erp_account_security.db")
os.environ.setdefault("DATABASE_URL", "sqlite:///" + _TMP_DB.replace("\\", "/"))

from app import app as flask_app  # noqa: E402

PW = "Str0ng-pass-1"


def _sfx():
    return uuid.uuid4().hex[:10]


def _user(password=PW, active=True, email=None):
    from shared.extensions import db
    from shared.models.base import Role, User
    email = email or f"acct_{_sfx()}@example.com"
    with flask_app.app_context():
        r = Role.query.filter_by(name="employee").first()
        u = User(employee_code=f"AS{_sfx()}".upper(), email=email, login_id=email,
                 full_name="Account " + email[:10], role_id=r.id, is_active=active,
                 has_hr_access=True)
        u.set_password(password)
        db.session.add(u)
        db.session.commit()
        return u.id, email


def _member(uid, company_id, role="employee"):
    from shared.extensions import db
    from shared.models.base import Role
    from shared.models.company import CompanyMembership
    with flask_app.app_context():
        db.session.add(CompanyMembership(
            company_id=company_id, user_id=uid, status="active",
            role_id=Role.query.filter_by(name=role).first().id))
        db.session.commit()


def _company(created_by=None):
    from shared.extensions import db
    from shared.models.company import Company
    with flask_app.app_context():
        c = Company(name="Sec Co " + _sfx(), slug="sec-" + _sfx(), is_active=True,
                    created_by=created_by)
        db.session.add(c)
        db.session.commit()
        return c.id


def _post_login(c, login, password, ip="10.0.0.1", door="/auth/login", **kw):
    return c.post(door, data={"login": login, "password": password},
                  headers={"X-Forwarded-For": ip}, **kw)


def _signed_in(c):
    with c.session_transaction() as sess:
        return bool(sess.get("_user_id"))


def _login(email, password=PW, ip="10.0.0.1"):
    c = flask_app.test_client()
    _post_login(c, email, password, ip=ip)
    assert _signed_in(c), f"login failed for {email}"
    return c


def _super():
    c = flask_app.test_client()
    c.post("/superadmin/login", data={"login": "admin@gmail.com", "password": "admin123"},
           headers={"X-Forwarded-For": "10.9.9.9"})
    assert _signed_in(c)
    return c


@pytest.fixture(scope="module")
def seeded():
    with flask_app.app_context():
        from shared.extensions import db
        db.drop_all()
        flask_app._db_initialized = False
    flask_app.test_client().get("/")


@pytest.fixture
def low_limits():
    keys = {"LOGIN_MAX_FAILS_PER_IP": 6, "LOGIN_MAX_FAILS_PER_LOGIN_FROM_IP": 3,
            "LOGIN_MAX_FAILS_PER_LOGIN": 5}
    old = {k: flask_app.config.get(k) for k in keys}
    flask_app.config.update(keys)
    yield
    for k, v in old.items():
        if v is None:
            flask_app.config.pop(k, None)
        else:
            flask_app.config[k] = v


# ── Sign-in throttle ────────────────────────────────────────────────────────

def test_repeated_wrong_passwords_lock_that_login_from_that_address(seeded, low_limits):
    uid, email = _user()
    c = flask_app.test_client()
    for _ in range(3):
        assert _post_login(c, email, "wrong-guess", ip="10.1.1.1").status_code == 200
    # Locked: even the right password is not looked at.
    r = _post_login(c, email, PW, ip="10.1.1.1")
    assert r.status_code == 429 and b"Too many failed sign-in attempts" in r.data
    assert not _signed_in(c)
    # The owner, from somewhere else, still gets in — and that clears the
    # login's own count.
    other = flask_app.test_client()
    _post_login(other, email, PW, ip="10.1.1.2")
    assert _signed_in(other)
    from shared.models.audit_log import AuditLog
    with flask_app.app_context():
        rows = AuditLog.query.filter_by(action="login_failed", reference=email).all()
        assert len(rows) >= 4 and {r.ip_address for r in rows} == {"10.1.1.1"}


def test_spread_out_guessing_locks_the_login_everywhere(seeded, low_limits):
    _uid, email = _user()
    for i in range(5):
        _post_login(flask_app.test_client(), email, "nope", ip=f"10.2.0.{i}")
    c = flask_app.test_client()
    assert _post_login(c, email, PW, ip="10.2.9.9").status_code == 429
    assert not _signed_in(c)


def test_one_address_guessing_many_logins_is_locked(seeded, low_limits):
    _uid, email = _user()
    for i in range(6):
        _post_login(flask_app.test_client(), f"nobody{i}_{_sfx()}@example.com", "x",
                    ip="10.3.3.3")
    c = flask_app.test_client()
    assert _post_login(c, email, PW, ip="10.3.3.3").status_code == 429
    _post_login(c, email, PW, ip="10.3.3.4")
    assert _signed_in(c)


def test_console_door_is_throttled_too(seeded, low_limits):
    c = flask_app.test_client()
    for _ in range(3):
        _post_login(c, "admin@gmail.com", "bad", ip="10.4.4.4", door="/superadmin/login")
    r = _post_login(c, "admin@gmail.com", "admin123", ip="10.4.4.4", door="/superadmin/login")
    assert r.status_code == 429 and not _signed_in(c)


def test_an_unknown_login_costs_a_password_hash(seeded, monkeypatch):
    from shared import account_security as acs
    calls = []
    real = acs.spend_password_check
    monkeypatch.setattr(acs, "spend_password_check",
                        lambda pw: calls.append(pw) or real(pw))
    _post_login(flask_app.test_client(), f"ghost_{_sfx()}@example.com", "guess-1",
                ip="10.5.5.5")
    assert calls == ["guess-1"]


# ── Sessions ────────────────────────────────────────────────────────────────

def test_a_new_password_signs_out_every_other_session(seeded):
    _uid, email = _user()
    laptop = _login(email, ip="10.6.0.1")
    phone = _login(email, ip="10.6.0.2")
    new = "An0ther-pass-2"
    r = phone.post("/auth/change-password", data={
        "current_password": PW, "new_password": new, "confirm_password": new})
    assert r.status_code == 302
    assert phone.get("/auth/profile").status_code == 200, "the changer stays signed in"
    r = laptop.get("/auth/profile")
    assert r.status_code == 302 and "/auth/login" in r.headers["Location"]


def test_a_deactivated_login_is_signed_out_on_its_next_request(seeded):
    from shared.extensions import db
    from shared.models.base import User
    uid, email = _user()
    c = _login(email, ip="10.7.0.1")
    assert c.get("/auth/profile").status_code == 200
    with flask_app.app_context():
        db.session.get(User, uid).is_active = False
        db.session.commit()
    r = c.get("/auth/profile")
    assert r.status_code == 302 and "/auth/login" in r.headers["Location"]


def test_a_session_from_before_fingerprints_still_works(seeded):
    uid, _email = _user()
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess["_user_id"] = str(uid)
        sess["_fresh"] = True
    assert c.get("/auth/profile").status_code == 200


def test_nothing_from_before_sign_in_survives_it(seeded):
    _uid, email = _user()
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess["planted"] = "by someone else"
        sess["company_id"] = 999999
    _post_login(c, email, PW, ip="10.8.0.1")
    with c.session_transaction() as sess:
        assert sess.get("_user_id") and "planted" not in sess
        assert sess.get("company_id") != 999999


# ── Redirects and sign-out ──────────────────────────────────────────────────

@pytest.mark.parametrize("target", ["//evil.example.com/x", "https://evil.example.com/",
                                    "/\\evil.example.com", "javascript:alert(1)"])
def test_sign_in_never_redirects_off_site(seeded, target):
    _uid, email = _user()
    c = flask_app.test_client()
    r = c.post(f"/auth/login?next={target}", data={"login": email, "password": PW},
               headers={"X-Forwarded-For": "10.10.0.1"})
    assert r.status_code == 302
    loc = r.headers["Location"]
    assert "evil" not in loc and not loc.startswith("javascript")


def test_sign_in_follows_a_local_next(seeded):
    _uid, email = _user()
    c = flask_app.test_client()
    r = c.post("/auth/login?next=/auth/profile", data={"login": email, "password": PW},
               headers={"X-Forwarded-For": "10.10.0.2"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/auth/profile")


def test_invitation_answers_never_redirect_off_site(seeded):
    from shared.extensions import db
    from shared.models.base import Role
    from shared.models.company import CompanyInvitation
    _uid, email = _user()
    cid = _company()
    with flask_app.app_context():
        inv = CompanyInvitation(company_id=cid, email=email, status="sent",
                                role_id=Role.query.filter_by(name="employee").first().id)
        db.session.add(inv)
        db.session.commit()
        inv_id = inv.id
    c = _login(email, ip="10.10.0.3")
    r = c.post(f"/settings/invitations/{inv_id}/decline",
               data={"next": "//evil.example.com/"})
    assert r.status_code == 302 and "evil" not in r.headers["Location"]


def test_a_link_on_another_site_cannot_sign_you_out(seeded):
    _uid, email = _user()
    c = _login(email, ip="10.11.0.1")
    c.get("/auth/logout", headers={"Sec-Fetch-Site": "cross-site"})
    assert _signed_in(c)
    c.get("/auth/logout", headers={"Sec-Fetch-Site": "same-origin"})
    assert not _signed_in(c)
    with c.session_transaction() as sess:
        assert "company_id" not in sess


def test_console_pages_send_a_signed_out_visitor_to_the_console_door(seeded):
    r = flask_app.test_client().get("/superadmin/users/")
    assert r.status_code == 302 and "/superadmin/login" in r.headers["Location"]
    c = flask_app.test_client()
    r = c.post("/superadmin/login?next=/superadmin/users/",
               data={"login": "admin@gmail.com", "password": "admin123"},
               headers={"X-Forwarded-For": "10.12.0.1"})
    assert r.headers["Location"].endswith("/superadmin/users/")
    c2 = flask_app.test_client()
    r = c2.post("/superadmin/login?next=//evil.example.com/",
                data={"login": "admin@gmail.com", "password": "admin123"},
                headers={"X-Forwarded-For": "10.12.0.2"})
    assert "evil" not in r.headers["Location"]


# ── New passwords ───────────────────────────────────────────────────────────

def test_new_passwords_need_eight_characters_and_not_a_common_one(seeded):
    from shared.account_security import password_problem
    assert password_problem("short7!")
    assert password_problem("password123")
    assert password_problem(" padded-pass ")
    assert password_problem("Str0ng-pass-1") is None

    uid, email = _user()
    c = _login(email, ip="10.13.0.1")
    c.post("/auth/change-password", data={"current_password": PW, "new_password": "abc123",
                                          "confirm_password": "abc123"})
    from shared.models.base import User
    with flask_app.app_context():
        assert User.query.get(uid).check_password(PW), "a 6-character password was accepted"

    sa = _super()
    short = f"short_{_sfx()}@example.com"
    sa.post("/superadmin/users/", data={"email": short, "full_name": "S", "password": "1234567"})
    sa.post("/signup", data={"full_name": "S", "email": f"su_{_sfx()}@example.com",
                             "password": "1234567"})
    with flask_app.app_context():
        from shared.models.company import RegistrationRequest
        assert User.query.filter_by(email=short).first() is None
        assert RegistrationRequest.query.filter(
            RegistrationRequest.email.like("su_%")).count() == 0
    r = sa.post(f"/superadmin/users/{uid}/", data={"action": "password",
                                                  "new_password": "1234567"},
                follow_redirects=True)
    assert b"at least 8" in r.data
    with flask_app.app_context():
        assert User.query.get(uid).check_password(PW)


def test_existing_short_passwords_still_sign_in(seeded):
    _uid, email = _user(password="abc1")
    assert _signed_in(_login(email, password="abc1", ip="10.14.0.1"))


# ── User ID and email ───────────────────────────────────────────────────────

def test_changing_user_id_or_email_needs_the_current_password(seeded):
    from shared.models.base import User
    uid, email = _user()
    c = _login(email, ip="10.15.0.1")
    new_email = f"moved_{_sfx()}@example.com"
    c.post("/auth/account-settings", data={"login_id": new_email, "email": new_email})
    with flask_app.app_context():
        assert User.query.get(uid).email == email
    c.post("/auth/account-settings", data={"login_id": new_email, "email": new_email,
                                           "current_password": PW})
    with flask_app.app_context():
        assert User.query.get(uid).email == new_email


def test_a_user_id_cannot_be_another_accounts_email(seeded):
    from shared.models.base import User
    _victim, victim_email = _user()
    uid, email = _user()
    c = _login(email, ip="10.16.0.1")
    r = c.post("/auth/account-settings", data={"login_id": victim_email, "email": email,
                                               "current_password": PW},
               follow_redirects=True)
    assert b"already taken" in r.data
    with flask_app.app_context():
        assert User.query.get(uid).login_id == email
    # Signing in as the victim's email still reaches the victim.
    v = flask_app.test_client()
    _post_login(v, victim_email, PW, ip="10.16.0.2")
    with v.session_transaction() as sess:
        assert sess["_user_id"].split(":")[0] == str(_victim)


def test_pending_invitations_follow_a_changed_email(seeded):
    from shared.extensions import db
    from shared.models.base import Role
    from shared.models.company import CompanyInvitation
    uid, email = _user()
    cid = _company()
    with flask_app.app_context():
        inv = CompanyInvitation(company_id=cid, email=email, status="sent",
                                role_id=Role.query.filter_by(name="employee").first().id)
        db.session.add(inv)
        db.session.commit()
        inv_id = inv.id
    c = _login(email, ip="10.17.0.1")
    new_email = f"newhome_{_sfx()}@example.com"
    c.post("/auth/account-settings", data={"login_id": email, "email": new_email,
                                           "current_password": PW})
    with flask_app.app_context():
        assert CompanyInvitation.query.get(inv_id).email == new_email


def test_settings_account_tab_needs_the_current_password_for_sign_in_changes(seeded):
    from shared.models.base import User
    uid, email = _user()
    cid = _company()
    _member(uid, cid)
    c = _login(email, ip="10.18.0.1")
    c.post("/settings/account", data={"full_name": "Renamed Only", "email": email,
                                      "login_id": email})
    new = "Brand-new-pass-3"
    c.post("/settings/account", data={"email": email, "login_id": email,
                                      "new_password": new, "confirm_password": new})
    with flask_app.app_context():
        u = User.query.get(uid)
        assert u.full_name == "Renamed Only", "a name change needs no password"
        assert u.check_password(PW), "the password changed without the current one"
    c.post("/settings/account", data={"email": email, "login_id": email,
                                      "new_password": new, "confirm_password": new,
                                      "current_password": PW})
    with flask_app.app_context():
        assert User.query.get(uid).check_password(new)
    assert c.get("/auth/profile").status_code == 200, "changing it here keeps you signed in"


# ── Suspended companies ─────────────────────────────────────────────────────

def test_a_suspended_company_cannot_be_entered(seeded):
    from shared.extensions import db
    from shared.models.company import Company
    uid, email = _user()
    cid = _company()
    _member(uid, cid)
    c = _login(email, ip="10.19.0.1")
    r = c.get(f"/company/switch/{cid}")
    assert r.status_code == 302 and "/dashboard" in r.headers["Location"]
    assert c.get("/dashboard/").status_code == 200, "inside the company"
    with flask_app.app_context():
        db.session.get(Company, cid).is_active = False
        db.session.commit()
    r = c.get("/dashboard/")
    assert r.status_code == 302 and "/portal" in r.headers["Location"], \
        "a suspended company kept working for someone already inside"
    r = c.get(f"/company/switch/{cid}", follow_redirects=True)
    assert b"suspended" in r.data


# ── Sign-up requests ────────────────────────────────────────────────────────

def test_a_blocked_sign_up_request_stays_blocked(seeded):
    from shared.extensions import db
    from shared.models.company import RegistrationRequest
    email = f"spam_{_sfx()}@example.com"
    anon = flask_app.test_client()
    anon.post("/signup", data={"full_name": "Spam", "email": email, "password": PW})
    with flask_app.app_context():
        req = RegistrationRequest.query.filter_by(email=email).one()
        req.status = RegistrationRequest.BLOCKED
        db.session.commit()
    r = anon.post("/signup", data={"full_name": "Spam", "email": email, "password": PW})
    assert b"already been reviewed" in r.data
    with flask_app.app_context():
        assert RegistrationRequest.query.filter_by(email=email).count() == 1


def test_approving_twice_or_onto_a_taken_email_makes_no_second_account(seeded):
    from shared.models.base import User
    from shared.models.company import RegistrationRequest
    anon = flask_app.test_client()
    email = f"req_{_sfx()}@example.com"
    anon.post("/signup", data={"full_name": "Req", "email": email, "password": PW})
    with flask_app.app_context():
        rid = RegistrationRequest.query.filter_by(email=email).one().id
    sa = _super()
    sa.post(f"/superadmin/requests/{rid}/approve")
    r = sa.post(f"/superadmin/requests/{rid}/approve", follow_redirects=True)
    assert b"already approved" in r.data
    with flask_app.app_context():
        assert User.query.filter_by(email=email).count() == 1

    # A request whose email became an account meanwhile is not approved
    # into a second one (it used to hit the unique key).
    email2 = f"req2_{_sfx()}@example.com"
    anon.post("/signup", data={"full_name": "Req2", "email": email2, "password": PW})
    with flask_app.app_context():
        rid2 = RegistrationRequest.query.filter_by(email=email2).one().id
    _user(email=email2)
    r = sa.post(f"/superadmin/requests/{rid2}/approve", follow_redirects=True)
    assert r.status_code == 200 and b"already signs in as" in r.data
    with flask_app.app_context():
        assert User.query.filter_by(email=email2).count() == 1
        assert RegistrationRequest.query.get(rid2).status != RegistrationRequest.APPROVED


def test_console_company_form_refuses_a_short_typed_password(seeded):
    from shared.models.base import User
    from shared.models.company import Company
    sa = _super()
    email = f"coadmin_{_sfx()}@example.com"
    slug = "co-" + _sfx()
    r = sa.post("/superadmin/companies/", data={"name": "Short PW Co", "slug": slug,
                                                "admin_email": email, "password": "abc"},
                follow_redirects=True)
    assert b"at least 8" in r.data
    with flask_app.app_context():
        assert Company.query.filter_by(slug=slug).first() is None
        assert User.query.filter_by(email=email).first() is None
