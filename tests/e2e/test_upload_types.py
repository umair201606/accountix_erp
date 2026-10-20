"""Uploaded files are served as what their extension says — never as a page.

The file store kept whatever type the browser declared. Avatars are served
inline from the app's own origin, so a "profile picture" uploaded as
text/html opened as a page carrying the uploader's script, running with the
session of whoever followed the link.
"""
import io
import os
import tempfile

import pytest

_TMP_DB = os.path.join(tempfile.gettempdir(), "erp_upload_types.db")
if os.path.exists(_TMP_DB):
    os.remove(_TMP_DB)
os.environ["DATABASE_URL"] = (os.environ.get("TEST_DATABASE_URL")
                              or "sqlite:///" + _TMP_DB.replace("\\", "/"))

from app import app as flask_app  # noqa: E402
from shared.extensions import db  # noqa: E402

PAYLOAD = b"<html><script>document.title='owned'</script></html>"


@pytest.fixture(scope="module")
def world():
    c = flask_app.test_client()
    c.get("/")
    from hr_app.models.user import User
    from shared.models.company import CompanyMembership
    with flask_app.app_context():
        user = User.query.filter_by(is_super_admin=True).order_by(User.id).first()
        cid = CompanyMembership.query.filter_by(user_id=user.id, status="active").first().company_id
        uid = user.id
    with c.session_transaction() as s:
        s["_user_id"] = str(uid)
        s["_fresh"] = True
        s["company_id"] = cid
    return {"client": c, "company": cid, "user": uid}


def test_an_avatar_declared_as_html_is_served_as_an_image(world):
    c = world["client"]
    r = c.post("/ess/upload-picture", content_type="multipart/form-data",
               data={"profile_picture": (io.BytesIO(PAYLOAD), "me.png", "text/html")})
    assert r.status_code == 302
    from shared.models.base import User
    with flask_app.app_context():
        name = db.session.get(User, world["user"]).profile_image
    r = c.get(f"/ess/avatar/{name}")
    assert r.status_code == 200
    assert r.mimetype == "image/png"


@pytest.mark.parametrize("key,declared,expected", [
    ("docs/report.pdf", "text/html", "application/pdf"),
    ("docs/page.html", "text/html", "application/octet-stream"),
    ("docs/drawing.svg", "image/svg+xml", "application/octet-stream"),
    ("docs/notes.xml", None, "application/octet-stream"),
    ("docs/blob", "text/html; charset=utf-8", "application/octet-stream"),
    ("avatars/a.jpg", None, "image/jpeg"),
])
def test_the_store_never_keeps_a_type_that_runs_in_the_page(world, key, declared, expected):
    from shared import file_store
    from shared.tenancy import set_current_company
    with flask_app.app_context():
        set_current_company(world["company"])
        row = file_store.save(key, PAYLOAD, declared)
        assert row.content_type == expected
        db.session.commit()
        assert file_store.read(key)[1] == expected
        file_store.delete(key)
        db.session.commit()


def test_rows_saved_before_the_fix_are_served_safely(world):
    """Files already stored with a browser-declared HTML type."""
    from shared import file_store
    from shared.models.stored_file import StoredFile
    from shared.tenancy import set_current_company
    with flask_app.app_context():
        set_current_company(world["company"])
        db.session.add(StoredFile(key="avatars/old.png", data=PAYLOAD,
                                  size=len(PAYLOAD), content_type="text/html"))
        db.session.commit()
        assert file_store.read("avatars/old.png")[1] == "image/png"
