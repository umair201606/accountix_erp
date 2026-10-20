"""An account still in use cannot be deleted from the chart.

Delete only checked for journal lines (then it deactivated the account) and
children. An account picked in a setting — the inventory account for
stock, a fixed asset's own account, an accounting voucher draft — was
deleted outright, leaving those records pointing at nothing on SQLite and
failing as "removed by another user" on Postgres.
"""
import os
import tempfile

import pytest

_TMP_DB = os.path.join(tempfile.gettempdir(), "erp_coa_delete_guard.db")
if os.path.exists(_TMP_DB):
    os.remove(_TMP_DB)
os.environ["DATABASE_URL"] = (os.environ.get("TEST_DATABASE_URL")
                              or "sqlite:///" + _TMP_DB.replace("\\", "/"))

from app import app as flask_app  # noqa: E402
from shared.extensions import db  # noqa: E402


@pytest.fixture(scope="module")
def world():
    c = flask_app.test_client()
    c.get("/")
    from hr_app.models.user import User
    from shared.models.company import CompanyMembership
    with flask_app.app_context():
        user = User.query.filter_by(is_super_admin=True).order_by(User.id).first()
        m = CompanyMembership.query.filter_by(user_id=user.id, status="active").first()
        uid, cid = user.id, m.company_id
    with c.session_transaction() as s:
        s["_user_id"] = str(uid)
        s["_fresh"] = True
        s["company_id"] = cid
    return {"client": c, "company": cid}


def _ctx(world):
    from shared.tenancy import set_current_company
    cm = flask_app.app_context()
    cm.push()
    set_current_company(world["company"])
    return cm


def _add(world, name):
    from shared.models.ledger import ChartOfAccount
    cm = _ctx(world)
    try:
        parent = ChartOfAccount.query.filter_by(code="1-01-01-01").first()
        pid = parent.id
    finally:
        db.session.remove()
        cm.pop()
    r = world["client"].post("/accounting/coa/add", data={"name": name, "parent_id": pid})
    assert r.status_code == 302
    cm = _ctx(world)
    try:
        return ChartOfAccount.query.filter_by(name=name).one().id
    finally:
        db.session.remove()
        cm.pop()


def _exists(world, acct_id):
    from shared.models.ledger import ChartOfAccount
    cm = _ctx(world)
    try:
        return ChartOfAccount.query.filter_by(id=acct_id).first() is not None
    finally:
        db.session.remove()
        cm.pop()


def test_an_unused_account_is_deleted(world):
    acct = _add(world, "Spare Cash Box")
    world["client"].post(f"/accounting/coa/{acct}/delete")
    assert not _exists(world, acct)


def test_an_account_picked_in_settings_is_refused_with_where_it_is_used(world):
    from shared.models.inventory_settings import InventorySettings
    acct = _add(world, "Stock In Transit Cash")
    cm = _ctx(world)
    try:
        s = InventorySettings.get()
        before = s.default_inventory_account_id
        s.default_inventory_account_id = acct
        db.session.commit()
    finally:
        db.session.remove()
        cm.pop()
    r = world["client"].post(f"/accounting/coa/{acct}/delete", follow_redirects=True)
    assert "still used by inventory settings" in r.get_data(as_text=True)
    assert _exists(world, acct)
    cm = _ctx(world)
    try:
        s = InventorySettings.get()
        s.default_inventory_account_id = before
        db.session.commit()
    finally:
        db.session.remove()
        cm.pop()
    world["client"].post(f"/accounting/coa/{acct}/delete")
    assert not _exists(world, acct), "once nothing points at it, it goes"


def test_every_foreign_key_to_the_chart_is_checked(world):
    """The check reads the models, so a reference added later is covered."""
    from finance_app.routes import coa
    cm = _ctx(world)
    try:
        fk_tables = {t.name for t in db.Model.metadata.sorted_tables
                     for col in t.columns for fk in col.foreign_keys
                     if fk.column.table.name == "chart_of_accounts"}
        unlabelled = fk_tables - set(coa._USED_BY_LABELS) - {
            "chart_of_accounts", "journal_lines", "journal_entries"}
        assert not unlabelled, f"give these a plain name in _USED_BY_LABELS: {unlabelled}"
    finally:
        db.session.remove()
        cm.pop()
