"""Fixed assets through the real routes: what an auditor would try.

One fresh company. Every step ends with Books Integrity (asset register = GL,
inventory GL = stock valuation, every journal balanced), so a rule that moves
the subledger without the ledger fails here.
"""
import os
import tempfile
from datetime import date

import pytest

_TMP_DB = os.path.join(tempfile.gettempdir(), "erp_fa_rules.db")
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
    from shared.models.company import Company, CompanyMembership
    from shared.models.base import Role
    from shared.tenancy import set_current_company, unscoped
    from shared.company_setup import provision_company
    from fixed_assets_app.models.asset import AssetCategory
    from inventory_app.models.product import InvProduct
    from shared.stock_entry import post_opening_stock

    with flask_app.app_context():
        user = (User.query.filter_by(is_super_admin=True).order_by(User.id).first()
                or User.query.order_by(User.id).first())
        uid = user.id
        with unscoped():
            comp = Company(name="FA Rules Co", slug="fa-rules-e2e",
                           is_active=True, created_by=uid)
            db.session.add(comp)
            db.session.flush()
            db.session.add(CompanyMembership(
                company_id=comp.id, user_id=uid,
                role_id=Role.query.filter_by(name=Role.ADMIN).first().id,
                status=CompanyMembership.ACTIVE))
            db.session.commit()
            cid = comp.id
        set_current_company(cid)
        provision_company(cid)
        set_current_company(cid)
        if not AssetCategory.query.first():
            AssetCategory.seed()
        db.session.commit()
        cat = AssetCategory.query.order_by(AssetCategory.id).first().id
        p = InvProduct(name="Generator 10kVA", sku="GEN10", current_stock=0,
                       cost_price=0, unit_price=0, is_active=True)
        db.session.add(p)
        db.session.flush()
        post_opening_stock(p, 3, 250_000, when=date(2024, 1, 1), created_by=uid)
        db.session.commit()
        pid = p.id
    with c.session_transaction() as sess:
        sess["_user_id"] = str(uid)
        sess["_fresh"] = True
        sess["company_id"] = cid
    return {"client": c, "company": cid, "user": uid, "cat": cat, "product": pid}


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


def assert_books_tie(w, step):
    from shared import integrity
    with Ctx(w):
        bad = integrity.failures()
    assert not bad, f"after {step}: " + "; ".join(
        f"{b['label']}: expected {b['expected']} got {b['actual']} ({b['detail']})"
        for b in bad)


def asset(w, name):
    from fixed_assets_app.models.asset import FixedAsset
    with Ctx(w):
        a = FixedAsset.query.filter_by(name=name).first()
        if a is None:
            return None
        return {"id": a.id, "status": a.status, "cost": float(a.purchase_cost),
                "dep": float(a.posted_depreciation), "life": a.useful_life,
                "code": a.asset_code, "active": a.is_active}


def create(w, name, **over):
    data = {"name": name, "category_id": w["cat"], "purchase_date": "2024-01-01",
            "purchase_cost": "1200000", "useful_life": "5", "salvage_value": "0",
            "depreciation_method": "straight_line"}
    data.update(over)
    return w["client"].post("/fixed-assets/assets/create", data=data,
                            headers={"Referer": "/fixed-assets/assets/create"},
                            follow_redirects=True)


def edit(w, a, **over):
    data = {"name": "Delivery Van", "category_id": w["cat"], "purchase_date": "2024-01-01",
            "purchase_cost": "1200000", "useful_life": str(a["life"]), "salvage_value": "0",
            "depreciation_method": "straight_line"}
    data.update(over)
    return w["client"].post(f"/fixed-assets/assets/{a['id']}/edit", data=data,
                            headers={"Referer": f"/fixed-assets/assets/{a['id']}/edit"},
                            follow_redirects=True)


def test_01_create_refuses_nonsense_terms(world):
    assert "less than the cost" in create(world, "Bad A", salvage_value="1200000").get_data(as_text=True)
    assert "more than zero" in create(world, "Bad B", purchase_cost="0").get_data(as_text=True)
    assert "between 1 and 100" in create(world, "Bad C", useful_life="0").get_data(as_text=True)
    assert asset(world, "Bad A") is None and asset(world, "Bad B") is None


def test_02_depreciation_run_and_the_planned_schedule(world):
    create(world, "Delivery Van")
    a = asset(world, "Delivery Van")
    assert a["code"].startswith("FA-")
    page = world["client"].get("/fixed-assets/depreciation/?as_of=2025-01-01").get_data(as_text=True)
    assert "Delivery Van" in page and "240,000" in page
    r = world["client"].post("/fixed-assets/depreciation/post",
                             data={"asset_ids[]": [a["id"]], "entry_date": "2025-01-01"})
    assert r.status_code in (302, 303)
    assert asset(world, "Delivery Van")["dep"] == 240_000
    view = world["client"].get(f"/fixed-assets/assets/{a['id']}").get_data(as_text=True)
    assert "Planned depreciation" in view
    assert_books_tie(world, "depreciation run")


def test_03_change_of_life_is_prospective(world):
    a = asset(world, "Delivery Van")
    edit(world, a, useful_life="9")           # 8 years (96 months) left
    from fixed_assets_app.models.asset import FixedAsset
    from fixed_assets_app.routes.depreciation import due_depreciation
    with Ctx(world):
        fa = db.session.get(FixedAsset, a["id"])
        assert due_depreciation(fa, date(2026, 1, 1)) == pytest.approx(960_000 / 8, abs=0.01)


def test_04_edit_guards(world):
    a = asset(world, "Delivery Van")
    body = edit(world, a, purchase_cost="200000").get_data(as_text=True)
    assert "cannot be below" in body
    body = edit(world, a, purchase_date="2025-06-01").get_data(as_text=True)
    assert "cannot be later" in body
    # "Disposed" is no longer a form choice: a posted status is ignored.
    edit(world, a, status="disposed")
    assert asset(world, "Delivery Van")["status"] == "active"
    # Restating the cost moves the acquisition journal with it.
    edit(world, a, purchase_cost="1300000")
    assert asset(world, "Delivery Van")["cost"] == 1_300_000
    assert_books_tie(world, "cost restated")


def test_05_disposal_cannot_predate_posted_depreciation(world):
    a = asset(world, "Delivery Van")
    r = world["client"].post(f"/fixed-assets/assets/{a['id']}/dispose",
                             data={"disposal_date": "2024-06-30", "proceeds": "0"},
                             follow_redirects=True)
    assert "Depreciation is posted after that date" in r.get_data(as_text=True)
    assert asset(world, "Delivery Van")["status"] == "active"
    r = world["client"].post(f"/fixed-assets/assets/{a['id']}/dispose",
                             data={"disposal_date": "2025-03-31", "proceeds": "900000"})
    assert asset(world, "Delivery Van")["status"] == "disposed"
    # Out of service: manual charges and cost edits are refused.
    r = world["client"].post(f"/fixed-assets/assets/{a['id']}/depreciate",
                             data={"entry_date": "2025-04-30", "amount": "1000"},
                             follow_redirects=True)
    assert "in service" in r.get_data(as_text=True)
    edit(world, a, purchase_cost="5", name="Delivery Van")
    assert asset(world, "Delivery Van")["cost"] == 1_300_000
    assert_books_tie(world, "disposal")


def test_06_draft_capitalisation_is_not_in_service(world):
    """A saved-but-unapproved capitalisation used to create an active asset
    the depreciation run then charged with nothing in the ledger."""
    from shared.models.asset_transfer import AssetTransfer
    c = world["client"]
    c.post("/fixed-assets/transfers/capitalise", data={
        "source_product_id": world["product"], "quantity": "2", "name": "Standby Generators",
        "category_id": world["cat"], "useful_life": "10", "depreciation_method": "straight_line",
        "salvage_value": "0", "transfer_date": "2024-07-01", "status": "unapproved"})
    a = asset(world, "Standby Generators")
    assert a["status"] == "draft" and not a["active"] and a["cost"] == 0
    run = c.get("/fixed-assets/depreciation/?as_of=2025-07-01").get_data(as_text=True)
    assert "Standby Generators" not in run
    with Ctx(world):
        t = AssetTransfer.query.filter_by(asset_id=a["id"]).one()
        tid = t.id
        assert float(t.quantity) == 2
    # Raised from Fixed Assets, so the Inventory list shows it too.
    assert t.voucher_number in c.get("/inventory/transfer-to-fa/").get_data(as_text=True)
    # Approving later uses the stored quantity and the costing engine's cost.
    r = c.post(f"/fixed-assets/transfers/{tid}/approve-capitalisation")
    assert r.status_code in (302, 303)
    a = asset(world, "Standby Generators")
    assert a["status"] == "active" and a["cost"] == 500_000
    assert_books_tie(world, "capitalisation approved")
    # State changes are POST only.
    assert c.get(f"/fixed-assets/transfers/{tid}/unapprove-capitalisation").status_code == 405
    c.post(f"/fixed-assets/transfers/{tid}/unapprove-capitalisation")
    assert asset(world, "Standby Generators")["status"] == "draft"
    assert_books_tie(world, "capitalisation unapproved")
    c.post(f"/fixed-assets/transfers/{tid}/delete")
    assert asset(world, "Standby Generators") is None
    with Ctx(world):
        assert db.session.get(AssetTransfer, tid) is None


def test_07_inventory_side_draft_has_no_typed_cost(world):
    c = world["client"]
    c.post("/inventory/transfer-to-fa/create", data={
        "product_id": world["product"], "name": "Workshop Generator", "category_id": world["cat"],
        "purchase_date": "2024-08-01", "quantity": "1", "useful_life": "8",
        "purchase_cost": "999999", "salvage_value": "0", "status": "unapproved"})
    a = asset(world, "Workshop Generator")
    assert a["status"] == "draft" and a["cost"] == 0
    assert "Workshop Generator" in c.get("/inventory/transfer-to-fa/").get_data(as_text=True)
    assert_books_tie(world, "inventory draft")


def test_08_asset_to_stock_needs_a_stock_item(world):
    create(world, "Old Compressor", purchase_cost="300000")
    a = asset(world, "Old Compressor")
    r = world["client"].post(f"/fixed-assets/transfers/create?asset_id={a['id']}",
                             data={"transfer_date": "2024-02-01", "status": "approved"})
    assert "Pick the stock item" in r.get_data(as_text=True)
    assert asset(world, "Old Compressor")["status"] == "active"
    assert_books_tie(world, "refused transfer")


def test_09_movement_schedule_rolls_forward(world):
    from fixed_assets_app.movement import movement
    with Ctx(world):
        m = movement(date(2025, 1, 1), date(2025, 12, 31))
    t = m["total"]
    assert t["cost_close"] == pytest.approx(t["cost_open"] + t["additions"] - t["cost_disposals"])
    assert t["dep_close"] == pytest.approx(t["dep_open"] + t["charge"] - t["dep_disposals"])
    assert t["cost_disposals"] == 1_300_000          # the van, sold in March 2025
    page = world["client"].get("/fixed-assets/reports/movement?from=2025-01-01&to=2025-12-31")
    assert page.status_code == 200 and "Movement Schedule" in page.get_data(as_text=True)
    x = world["client"].get("/fixed-assets/reports/movement?from=2025-01-01&to=2025-12-31&format=excel")
    assert x.status_code == 200 and x.data[:2] == b"PK"
