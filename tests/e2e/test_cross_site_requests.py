"""Writes started by another website are refused.

Form tokens are off app-wide (``WTF_CSRF_ENABLED = False``), so any page on
the internet could post a form to this app and the signed-in user's browser
would attach their session cookie: approve an invoice, delete a voucher,
reset a password. Two locks now: the cookies carry ``SameSite=Lax`` (kept
off cross-site POSTs) and ``shared/csrf_guard.py`` refuses any POST / PUT /
PATCH / DELETE whose ``Origin`` or ``Referer`` names another site. Requests
with neither header (curl, this test client, server-to-server) pass.
"""
import os
import tempfile

import pytest

_TMP_DB = os.path.join(tempfile.gettempdir(), "erp_cross_site.db")
if os.path.exists(_TMP_DB):
    os.remove(_TMP_DB)
os.environ["DATABASE_URL"] = "sqlite:///" + _TMP_DB.replace("\\", "/")

from app import app as flask_app  # noqa: E402
from shared.csrf_guard import REFUSAL  # noqa: E402

EVIL = "https://evil.example"


@pytest.fixture(scope="module")
def client():
    c = flask_app.test_client()
    c.get("/")  # first request seeds the fresh database
    return c


@pytest.fixture()
def signed_in(client):
    from shared.extensions import db
    from shared.models.base import Role, User
    from shared.models.company import Company, CompanyMembership
    with flask_app.app_context():
        user = User.query.filter_by(email="admin@solarkon.com").first()
        company = Company.query.order_by(Company.id).first()
        m = CompanyMembership.query.filter_by(company_id=company.id,
                                              user_id=user.id).first()
        if m is None:
            db.session.add(CompanyMembership(
                company_id=company.id, user_id=user.id,
                role_id=Role.query.filter_by(name=Role.ADMIN).first().id,
                status=CompanyMembership.ACTIVE))
            db.session.commit()
        uid, cid = user.id, company.id
        db.session.remove()
    with client.session_transaction() as s:
        s["_user_id"] = str(uid)
        s["_fresh"] = True
        s["company_id"] = cid
    yield {"user": uid, "company": cid}
    with client.session_transaction() as s:
        s.clear()


def _refused(resp):
    return resp.status_code == 403 and REFUSAL in resp.get_data(as_text=True)


def test_a_form_posted_from_another_site_is_refused(client):
    resp = client.post("/auth/login", data={"login": "admin@solarkon.com",
                                            "password": "admin123"},
                       headers={"Origin": EVIL})
    assert _refused(resp)
    assert resp.headers["Content-Type"].startswith("text/plain")


def test_a_fetch_from_another_site_gets_a_json_refusal(client, signed_in):
    resp = client.post("/invoicing/tracking/api/auto-match", json={},
                       headers={"Origin": EVIL})
    assert resp.status_code == 403
    body = resp.get_json()
    assert body == {"ok": False, "error": REFUSAL}


def test_origin_null_is_refused(client):
    # Sandboxed frames and some cross-site redirects send "null".
    assert _refused(client.post("/auth/login", data={}, headers={"Origin": "null"}))


def test_a_referer_from_another_site_is_refused(client):
    resp = client.post("/auth/login", data={},
                       headers={"Referer": EVIL + "/pretty-cat.html"})
    assert _refused(resp)


@pytest.mark.parametrize("headers", [
    {},                                                  # curl / test client
    {"Origin": "http://localhost"},                      # this app's own page
    {"Referer": "http://localhost/auth/login"},
    {"Referer": "/auth/login"},                          # a bare path names no site
    {"Origin": "https://erp.example.com", "X-Forwarded-Host": "erp.example.com",
     "X-Forwarded-Proto": "https"},                      # behind Vercel's proxy
])
def test_the_apps_own_pages_still_post(client, headers):
    resp = client.post("/auth/login", data={"login": "nobody@example.com",
                                            "password": "x"}, headers=headers)
    assert not _refused(resp)
    assert resp.status_code == 200          # the login page: bad password


def test_a_port_or_scheme_change_is_another_site(client):
    assert _refused(client.post("/auth/login", data={},
                                headers={"Origin": "http://localhost:8080"}))
    assert _refused(client.post("/auth/login", data={},
                                headers={"Origin": "http://localhost.evil.example"}))


def test_trusted_origins_extend_the_list(client, monkeypatch):
    monkeypatch.setenv("TRUSTED_ORIGINS", "https://books.example.com")
    resp = client.post("/auth/login", data={},
                       headers={"Origin": "https://books.example.com"})
    assert not _refused(resp)


def test_reading_is_never_refused(client):
    resp = client.get("/auth/login", headers={"Origin": EVIL, "Referer": EVIL})
    assert resp.status_code == 200


def test_session_cookie_is_lax_and_http_only(client):
    resp = client.post("/auth/login", data={"login": "admin@solarkon.com",
                                            "password": "admin123"})
    cookies = [v for k, v in resp.headers.items() if k.lower() == "set-cookie"]
    session = [c for c in cookies if c.startswith("session=")]
    assert session, cookies
    assert "SameSite=Lax" in session[0] and "HttpOnly" in session[0]
    assert flask_app.config["REMEMBER_COOKIE_SAMESITE"] == "Lax"
    assert flask_app.config["REMEMBER_COOKIE_HTTPONLY"] is True
    with client.session_transaction() as s:
        s.clear()


def test_a_link_on_another_site_cannot_switch_the_company(client, signed_in):
    """``/company/switch/<id>`` is a plain link; another site sending the
    browser there would change which company the user's open tabs save
    into. The browser marks such navigations ``Sec-Fetch-Site: cross-site``."""
    from shared.extensions import db
    from shared.models.company import Company, CompanyMembership
    from shared.models.base import Role
    from shared.tenancy import unscoped
    with flask_app.app_context():
        with unscoped():
            other = Company.query.filter_by(slug="cross-site-two").first()
            if other is None:
                other = Company(name="Second Co", slug="cross-site-two", is_active=True,
                                created_by=signed_in["user"])
                db.session.add(other)
                db.session.flush()
                db.session.add(CompanyMembership(
                    company_id=other.id, user_id=signed_in["user"],
                    role_id=Role.query.filter_by(name=Role.ADMIN).first().id,
                    status=CompanyMembership.ACTIVE))
                db.session.commit()
            other_id = other.id
        db.session.remove()
    client.get(f"/company/switch/{other_id}", headers={"Sec-Fetch-Site": "cross-site"})
    with client.session_transaction() as s:
        assert s["company_id"] == signed_in["company"]
    client.get(f"/company/switch/{other_id}", headers={"Sec-Fetch-Site": "same-origin"})
    with client.session_transaction() as s:
        assert s["company_id"] == other_id


def test_every_answer_refuses_framing_and_type_sniffing():
    """Clickjacking: another site showing the app in a frame could steal a
    signed-in user's clicks; nosniff stops a file being run as a guessed type."""
    c = flask_app.test_client()
    for path in ("/", "/auth/login"):
        r = c.get(path)
        assert r.headers.get("X-Frame-Options") == "SAMEORIGIN"
        assert "frame-ancestors 'self'" in r.headers.get("Content-Security-Policy", "")
        assert r.headers.get("X-Content-Type-Options") == "nosniff"
        assert r.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"
