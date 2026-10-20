"""Serverless-safe storage and exact document amounts.

- Uploads (employee documents, profile pictures) are kept in the database,
  not on disk: Vercel's disk is per-instance and temporary. Company
  isolation, delete, and the legacy-disk fallback for older files.
- The payroll bulk sheet is parsed in memory and never written anywhere.
- Document amounts are NUMERIC on Postgres (exact, rounded to 6 places) and
  still reach Python as float.
- Alembic runs at boot, is at head, and revision 0002 converts a column a
  live database still holds as DOUBLE PRECISION (Postgres only).

TEST_DATABASE_URL runs the file against Postgres (production is Neon).
"""
import io
import os
import tempfile

import pytest

_TMP_DB = os.path.join(tempfile.gettempdir(), "erp_storage_money.db")
if os.path.exists(_TMP_DB):
    os.remove(_TMP_DB)
os.environ["DATABASE_URL"] = (os.environ.get("TEST_DATABASE_URL")
                              or "sqlite:///" + _TMP_DB.replace("\\", "/"))

from app import app as flask_app  # noqa: E402
from shared.extensions import db  # noqa: E402

PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
       b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\x0f"
       b"\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82")


def _new_company(uid, slug):
    from shared.models.company import Company, CompanyMembership
    from shared.models.base import Role
    from shared.tenancy import set_current_company, unscoped
    from shared.company_setup import provision_company
    with unscoped():
        comp = Company(name=slug.title(), slug=slug, is_active=True,
                       created_by=uid)
        db.session.add(comp)
        db.session.flush()
        admin_role = Role.query.filter_by(name=Role.ADMIN).first()
        db.session.add(CompanyMembership(company_id=comp.id, user_id=uid,
                                         role_id=admin_role.id,
                                         status=CompanyMembership.ACTIVE))
        db.session.commit()
        cid = comp.id
    set_current_company(cid)
    provision_company(cid)
    set_current_company(cid)
    return cid


@pytest.fixture(scope="module")
def w():
    c = flask_app.test_client()
    c.get("/")
    from hr_app.models.user import User
    with flask_app.app_context():
        user = (User.query.filter_by(is_super_admin=True).order_by(User.id).first()
                or User.query.order_by(User.id).first())
        uid = user.id
        cid = _new_company(uid, "storage-co-e2e")
        other = _new_company(uid, "storage-other-e2e")
    _enter(c, uid, cid)
    return {"c": c, "uid": uid, "cid": cid, "other": other}


def _enter(c, uid, cid):
    with c.session_transaction() as s:
        s["_user_id"] = str(uid)
        s["_fresh"] = True
        s["company_id"] = cid


def _stored(cid, key=None):
    from shared.models.stored_file import StoredFile
    from shared.tenancy import unscoped
    with flask_app.app_context(), unscoped():
        q = StoredFile.query.filter_by(company_id=cid)
        if key:
            q = q.filter_by(key=key)
        return [(r.key, r.size, r.content_type, bytes(r.data)) for r in q.all()]


def _upload_dir_files():
    root = flask_app.config["UPLOAD_FOLDER"]
    found = set()
    for dirpath, _, names in os.walk(root):
        for n in names:
            found.add(os.path.join(dirpath, n))
    return found


# ── Employee documents ──

def test_document_upload_is_stored_in_the_database(w):
    from hr_app.models.digital_file import DigitalFile
    before_disk = _upload_dir_files()
    body = b"%PDF-1.4 contract body"
    r = w["c"].post("/digital-files/upload", data={
        "file": (io.BytesIO(body), "contract.pdf", "application/pdf"),
        "title": "Contract"}, content_type="multipart/form-data")
    assert r.status_code == 302
    with flask_app.app_context():
        from shared.tenancy import unscoped
        with unscoped():
            f = (DigitalFile.query.filter_by(company_id=w["cid"], title="Contract")
                 .one())
            w["doc"] = f.id
            key = f"{f.user_id}/{f.filename}"
            assert f.file_size == len(body)
    assert _stored(w["cid"], key) == [(key, len(body), "application/pdf", body)]
    # Nothing written to the upload folder.
    assert _upload_dir_files() == before_disk
    w["doc_key"] = key


def test_document_downloads_from_the_database(w):
    r = w["c"].get(f"/digital-files/download/{w['doc']}")
    assert r.status_code == 200
    assert r.data == b"%PDF-1.4 contract body"
    assert "contract.pdf" in r.headers["Content-Disposition"]


def test_another_company_cannot_reach_the_document(w):
    _enter(w["c"], w["uid"], w["other"])
    try:
        r = w["c"].get(f"/digital-files/download/{w['doc']}")
        assert r.status_code == 404
        # And the bytes are not visible from that company either.
        from shared import file_store
        from shared.tenancy import set_current_company
        with flask_app.test_request_context():
            set_current_company(w["other"])
            assert file_store.read(w["doc_key"]) is None
    finally:
        _enter(w["c"], w["uid"], w["cid"])


def test_document_delete_removes_the_bytes(w):
    r = w["c"].post(f"/digital-files/delete/{w['doc']}")
    assert r.status_code == 302
    assert _stored(w["cid"], w["doc_key"]) == []
    assert w["c"].get(f"/digital-files/download/{w['doc']}").status_code == 404


# ── Profile pictures ──

def _profile_image(uid):
    from hr_app.models.user import User
    with flask_app.app_context():
        return db.session.get(User, uid).profile_image


def test_avatar_upload_serve_and_replace(w):
    r = w["c"].post("/ess/upload-picture", data={
        "profile_picture": (io.BytesIO(PNG), "me.png", "image/png")},
        content_type="multipart/form-data")
    assert r.status_code == 302
    first = _profile_image(w["uid"])
    assert _stored(w["cid"], f"avatars/{first}")[0][3] == PNG
    r = w["c"].get(f"/ess/avatar/{first}")
    assert r.status_code == 200 and r.data == PNG
    assert r.mimetype == "image/png"

    # A new picture replaces the old one's bytes.
    import time
    time.sleep(1.1)  # file names carry a seconds timestamp
    w["c"].post("/ess/upload-picture", data={
        "profile_picture": (io.BytesIO(PNG + b"v2"), "me2.png", "image/png")},
        content_type="multipart/form-data")
    second = _profile_image(w["uid"])
    assert second != first
    assert _stored(w["cid"], f"avatars/{first}") == []
    assert w["c"].get(f"/ess/avatar/{second}").data == PNG + b"v2"
    assert w["c"].get(f"/ess/avatar/{first}").status_code == 404


def test_avatar_from_before_the_database_store_still_serves(w):
    d = os.path.join(flask_app.config["UPLOAD_FOLDER"], str(w["cid"]), "avatars")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "avatar_legacy_1.png")
    with open(path, "wb") as fh:
        fh.write(PNG)
    try:
        r = w["c"].get("/ess/avatar/avatar_legacy_1.png")
        assert r.status_code == 200 and r.data == PNG
    finally:
        os.remove(path)


def test_avatar_route_does_not_walk_out_of_the_store(w):
    r = w["c"].get("/ess/avatar/..%2F..%2Fapp.py")
    assert r.status_code == 404


# ── Payroll bulk sheet ──

def test_payroll_bulk_sheet_is_parsed_in_memory(w):
    before_disk = _upload_dir_files()
    before_rows = len(_stored(w["cid"]))
    csv_body = "Employee Code,Bonus\nNOBODY-999,500\n".encode("utf-8-sig")
    r = w["c"].post("/compensation/upload-bulk-data", data={
        "file": (io.BytesIO(csv_body), "bulk.csv", "text/csv")},
        content_type="multipart/form-data")
    assert r.status_code == 200, r.data[:300]
    assert r.get_json()["adjustments"] == {}
    assert _upload_dir_files() == before_disk
    assert len(_stored(w["cid"])) == before_rows


# ── Exact document amounts ──

def test_document_amounts_are_numeric_and_read_back_as_float(w):
    from inventory_app.models.product import InvProduct
    from shared.tenancy import set_current_company
    with flask_app.app_context():
        set_current_company(w["cid"])
        p = InvProduct(sku="MNY-1", name="Money Bolt", unit="pcs",
                       unit_price=0.1 + 0.2, cost_price=1 / 3, current_stock=0)
        db.session.add(p)
        db.session.commit()
        pid = p.id
        db.session.expire_all()
        p = db.session.get(InvProduct, pid)
        assert isinstance(p.unit_price, float)
        assert isinstance(p.cost_price, float)
        if db.engine.dialect.name == "postgresql":
            # Stored as the exact decimal, binary noise gone.
            assert p.unit_price == 0.3
            assert p.cost_price == 0.333333
            col_type = db.session.execute(db.text(
                "SELECT data_type, numeric_precision, numeric_scale "
                "FROM information_schema.columns WHERE table_name = "
                "'inv_products' AND column_name = 'unit_price'")).one()
            assert tuple(col_type) == ("numeric", 20, 6)
        else:
            assert abs(p.unit_price - 0.3) < 1e-12


def test_alembic_is_at_head_after_boot(w):
    from shared import db_migrate
    from alembic.script import ScriptDirectory
    head = ScriptDirectory.from_config(db_migrate.alembic_config()).get_current_head()
    with flask_app.app_context():
        assert db_migrate.current(db.engine) == head == "0015_membership_employment"


def test_money_revision_converts_a_double_precision_column(w):
    from shared import db_migrate
    with flask_app.app_context():
        if db.engine.dialect.name != "postgresql":
            pytest.skip("Postgres-only revision (SQLite types are affinities)")
        with db.engine.begin() as conn:
            conn.execute(db.text(
                "ALTER TABLE inv_products ALTER COLUMN unit_price "
                "TYPE DOUBLE PRECISION"))
            conn.execute(db.text(
                "UPDATE inv_products SET unit_price = 0.1::float8 + 0.2::float8 "
                "WHERE sku = 'MNY-1'"))
            conn.execute(db.text(
                "UPDATE alembic_version SET version_num = '0001_baseline'"))
        db_migrate.upgrade(db.engine)
        with db.engine.connect() as conn:
            dtype = conn.execute(db.text(
                "SELECT data_type FROM information_schema.columns WHERE "
                "table_name = 'inv_products' AND column_name = 'unit_price'"
            )).scalar()
            raw = conn.execute(db.text(
                "SELECT unit_price::text FROM inv_products WHERE sku = 'MNY-1'"
            )).scalar()
        assert dtype == "numeric"
        assert raw == "0.300000"
        assert db_migrate.current(db.engine) == "0015_membership_employment"
        # Running again is a no-op.
        db_migrate.upgrade(db.engine)


def test_invoice_date_revision_converts_a_legacy_date_column(w):
    """Live databases older than the DateTime model still held
    inv_invoices.invoice_date as DATE; the return routes' ``.date()`` call
    then 500'd there. Revision 0004 converts the column."""
    from shared import db_migrate
    with flask_app.app_context():
        if db.engine.dialect.name != "postgresql":
            pytest.skip("Postgres-only revision (SQLite types are affinities)")
        with db.engine.begin() as conn:
            for col in ("invoice_date", "due_date"):
                conn.execute(db.text(
                    f"ALTER TABLE inv_invoices ALTER COLUMN {col} TYPE DATE"))
            conn.execute(db.text(
                "UPDATE alembic_version SET version_num = '0003_gl_reporting'"))
        db_migrate.upgrade(db.engine)
        with db.engine.connect() as conn:
            types = dict(conn.execute(db.text(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_name = 'inv_invoices' AND column_name IN "
                "('invoice_date', 'due_date')")).fetchall())
        assert types == {"invoice_date": "timestamp without time zone",
                         "due_date": "timestamp without time zone"}
        assert db_migrate.current(db.engine) == "0015_membership_employment"


def test_return_date_check_accepts_date_and_datetime():
    """The return routes compare through as_date, whatever type the column
    hands back."""
    from datetime import date, datetime
    from shared.posting_helpers import as_date
    assert as_date(datetime(2026, 3, 4, 15, 0)) < as_date(date(2026, 3, 5))
    assert as_date(date(2026, 3, 5)) == as_date(datetime(2026, 3, 5, 9, 30))
