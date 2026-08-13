"""Fixed assets: GL wiring, depreciation arithmetic, and reversal safety.

The point of these tests is the invariant that survives editing: nothing about
an asset's position is stored as a running total. Accumulated depreciation and
book value are DERIVED from the depreciation rows whose journal entry is still
posted, so un-posting or deleting a voucher self-corrects instead of stranding
the asset at a figure no journal supports.
"""
import sys
from datetime import date
from pathlib import Path

import pytest
from flask import Flask

ACCOUNTIX_ERP = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ACCOUNTIX_ERP))

from shared.extensions import db  # noqa: E402
import shared.tenancy  # noqa: E402,F401  (registers the scoping listener)


@pytest.fixture
def app():
    application = Flask(__name__)
    application.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    application.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(application)

    import shared.models.ledger  # noqa: F401
    import shared.models.company  # noqa: F401  (companies + memberships: FK targets)
    import shared.models.stock_ledger  # noqa: F401
    import shared.models.stock_layer  # noqa: F401
    import shared.models.company_settings  # noqa: F401
    import shared.models.invoice_template  # noqa: F401
    import shared.models.asset_transfer  # noqa: F401
    import shared.models.base  # noqa: F401
    import shared.models.inventory_settings  # noqa: F401
    import inventory_app.models.product  # noqa: F401
    import fixed_assets_app.models.asset  # noqa: F401

    shared.tenancy._reset_registry()

    with application.app_context():
        db.create_all()
        from shared.models.company import Company
        from shared.tenancy import set_current_company, unscoped
        with unscoped():
            default = Company(name="Unit Test Co", slug="unit-default",
                              is_active=True)
            db.session.add(default)
            db.session.commit()
        set_current_company(default.id)
        from shared.coa import seed_fixed_tree
        seed_fixed_tree()
        from shared.models.inventory_settings import InventorySettings
        db.session.add(InventorySettings(valuation_method="weighted_average",
                                         allow_negative_stock=False))
        db.session.commit()
        yield application
        db.session.remove()
        db.drop_all()


@pytest.fixture
def category(app):
    from fixed_assets_app.models.asset import AssetCategory
    AssetCategory.seed()
    db.session.commit()
    return AssetCategory.query.first()


def make_asset(category, code="FA-T1", cost=1_000_000.0, salvage=100_000.0,
               life=5, method="straight_line", purchase=date(2024, 1, 1)):
    from fixed_assets_app.models.asset import FixedAsset
    from fixed_assets_app.routes.assets import post_acquisition
    asset = FixedAsset(asset_code=code, name=code, category_id=category.id,
                       purchase_date=purchase, purchase_cost=cost,
                       useful_life=life, depreciation_method=method,
                       salvage_value=salvage, current_book_value=cost,
                       status="active")
    db.session.add(asset)
    db.session.flush()
    post_acquisition(asset, created_by=1)
    asset.recalculate()
    db.session.commit()
    return asset


def account_balance(account_id):
    from shared.models.ledger import JournalEntry, JournalLine
    row = db.session.query(
        db.func.coalesce(db.func.sum(JournalLine.debit), 0),
        db.func.coalesce(db.func.sum(JournalLine.credit), 0),
    ).join(JournalEntry, JournalLine.journal_entry_id == JournalEntry.id).filter(
        JournalLine.account_id == account_id,
        JournalEntry.is_posted == True).first()
    return float(row[0]) - float(row[1])


# ── Acquisition ─────────────────────────────────────────────────────────────

def test_acquisition_books_the_asset_to_the_ledger(app, category):
    """Without this the subledger held the cost and the GL held nothing."""
    asset = make_asset(category)
    from shared.ledger_utils import posting_account
    assert account_balance(asset.fixed_asset_account_id) == 1_000_000
    assert account_balance(posting_account("ap").id) == -1_000_000


def test_acquisition_is_idempotent(app, category):
    from fixed_assets_app.routes.assets import post_acquisition
    asset = make_asset(category)
    post_acquisition(asset, created_by=1)
    db.session.commit()
    assert account_balance(asset.fixed_asset_account_id) == 1_000_000


# ── Depreciation arithmetic ─────────────────────────────────────────────────

def test_depreciation_charges_evenly_without_drift(app, category):
    """``days / 30`` drifted, so a catch-up could double-charge a period."""
    from fixed_assets_app.routes.depreciation import post_asset_depreciation
    from fixed_assets_app.models.asset import AssetDepreciation
    asset = make_asset(category)
    for year in (2025, 2026, 2027, 2028, 2029):
        post_asset_depreciation(asset, date(year, 1, 1), created_by=1)
        db.session.commit()
    charges = [e.amount for e in
               AssetDepreciation.query.order_by(AssetDepreciation.id).all()]
    assert charges == [180_000] * 5
    assert asset.posted_depreciation == 900_000


def test_last_depreciation_date_is_the_latest_not_the_earliest(app, category):
    """The relationship's own order_by used to win, returning the oldest row."""
    from fixed_assets_app.routes.depreciation import (post_asset_depreciation,
                                                      last_depreciation_date)
    asset = make_asset(category)
    for year in (2025, 2026, 2027):
        post_asset_depreciation(asset, date(year, 1, 1), created_by=1)
        db.session.commit()
    assert last_depreciation_date(asset) == date(2027, 1, 1)


@pytest.mark.parametrize("method", ["straight_line", "declining_balance"])
def test_depreciation_never_breaches_the_salvage_floor(app, category, method):
    """Declining balance charged on cost - accumulated, ignoring salvage."""
    from fixed_assets_app.routes.depreciation import post_asset_depreciation
    asset = make_asset(category, code=f"FA-{method}", method=method)
    for year in range(2025, 2040):
        post_asset_depreciation(asset, date(year, 1, 1), created_by=1)
        db.session.commit()
    assert asset.net_book_value == pytest.approx(100_000, abs=0.01)
    assert asset.posted_depreciation <= 900_000 + 0.01


def test_manual_charge_is_capped_at_the_salvage_floor(app, category):
    from fixed_assets_app.routes.depreciation import post_asset_depreciation
    asset = make_asset(category)
    charged = post_asset_depreciation(asset, date(2025, 1, 1), created_by=1,
                                      amount=5_000_000)
    db.session.commit()
    assert charged == 900_000
    assert asset.net_book_value == pytest.approx(100_000, abs=0.01)


# ── Reversal safety ─────────────────────────────────────────────────────────

def test_reversing_a_charge_restores_the_balances(app, category):
    from fixed_assets_app.routes.depreciation import (post_asset_depreciation,
                                                      reverse_asset_depreciation)
    from fixed_assets_app.models.asset import AssetDepreciation
    asset = make_asset(category)
    for year in (2025, 2026):
        post_asset_depreciation(asset, date(year, 1, 1), created_by=1)
        db.session.commit()
    assert asset.posted_depreciation == 360_000

    latest = AssetDepreciation.query.order_by(AssetDepreciation.id.desc()).first()
    reverse_asset_depreciation(latest, created_by=1)
    db.session.commit()

    assert asset.posted_depreciation == 180_000
    assert asset.accumulated_depreciation == 180_000  # cache agrees
    assert asset.current_book_value == 820_000
    # and the subledger still equals the GL
    assert asset.posted_depreciation == -account_balance(asset.accum_dep_account_id)


def test_reversing_one_charge_leaves_the_others_posted(app, category):
    """Every charge shared voucher_id=asset.id, so one reversal killed them all."""
    from fixed_assets_app.routes.depreciation import (post_asset_depreciation,
                                                      reverse_asset_depreciation)
    from fixed_assets_app.models.asset import AssetDepreciation
    asset = make_asset(category)
    for year in (2025, 2026, 2027):
        post_asset_depreciation(asset, date(year, 1, 1), created_by=1)
        db.session.commit()
    latest = AssetDepreciation.query.order_by(AssetDepreciation.id.desc()).first()
    reverse_asset_depreciation(latest, created_by=1)
    db.session.commit()
    assert asset.live_depreciation_query().count() == 2
    assert asset.posted_depreciation == 360_000


def test_a_reversed_period_becomes_chargeable_again(app, category):
    from fixed_assets_app.routes.depreciation import (post_asset_depreciation,
                                                      reverse_asset_depreciation,
                                                      due_depreciation)
    from fixed_assets_app.models.asset import AssetDepreciation
    asset = make_asset(category)
    post_asset_depreciation(asset, date(2025, 1, 1), created_by=1)
    db.session.commit()
    entry = AssetDepreciation.query.one()
    reverse_asset_depreciation(entry, created_by=1)
    db.session.commit()

    assert due_depreciation(asset, date(2025, 1, 1)) == 180_000
    post_asset_depreciation(asset, date(2025, 1, 1), created_by=1)
    db.session.commit()
    assert asset.posted_depreciation == 180_000
    assert asset.posted_depreciation == -account_balance(asset.accum_dep_account_id)


def test_hard_deleting_a_voucher_self_corrects(app, category):
    """Not just un-posting: the row may be deleted outright."""
    from shared.models.ledger import JournalEntry, JournalLine
    from fixed_assets_app.routes.depreciation import post_asset_depreciation
    from fixed_assets_app.models.asset import AssetDepreciation
    asset = make_asset(category)
    for year in (2025, 2026):
        post_asset_depreciation(asset, date(year, 1, 1), created_by=1)
        db.session.commit()

    victim = AssetDepreciation.query.order_by(AssetDepreciation.id).first()
    entry = db.session.get(JournalEntry, victim.journal_entry_id)
    JournalLine.query.filter_by(journal_entry_id=entry.id).delete()
    db.session.delete(entry)
    db.session.delete(victim)
    db.session.commit()

    asset.recalculate()
    db.session.commit()
    assert asset.posted_depreciation == 180_000
    assert asset.posted_depreciation == -account_balance(asset.accum_dep_account_id)


# ── Disposal ────────────────────────────────────────────────────────────────

def _dispose(asset, proceeds=0.0, proceeds_account_id=None):
    """Mirror of the dispose_asset route's journal, without the HTTP layer."""
    from shared.ledger_utils import (post_journal_entry, posting_account,
                                     create_fixed_asset_accounts)
    if not asset.accum_dep_account_id:
        _, accum = create_fixed_asset_accounts(asset, asset.name)
        asset.accum_dep_account_id = accum.id
        db.session.flush()
    accum_dep = asset.posted_depreciation
    net_book = asset.purchase_cost - accum_dep
    lines = [
        {"account_id": asset.accum_dep_account_id, "debit": accum_dep,
         "credit": 0, "description": "accum"},
        {"account_id": asset.fixed_asset_account_id, "debit": 0,
         "credit": asset.purchase_cost, "description": "cost"},
    ]
    if proceeds:
        lines.append({"account_id": proceeds_account_id or posting_account("cash").id,
                      "debit": proceeds, "credit": 0, "description": "proceeds"})
    result = round(proceeds - net_book, 2)
    if result:
        lines.append({"account_id": posting_account("disposal_gain_loss").id,
                      "debit": abs(result) if result < 0 else 0,
                      "credit": result if result > 0 else 0,
                      "description": "gain/loss"})
    asset.status = "disposed"
    asset.is_active = False
    db.session.flush()
    post_journal_entry(voucher_type="FA-DISP", voucher_id=asset.id,
                       voucher_number=f"FA-DISP-{asset.asset_code}",
                       description="disposal", entry_date=date(2026, 1, 1),
                       created_by=1, lines=lines)
    db.session.commit()
    return result


def test_scrapping_writes_the_book_value_off_to_disposal_not_depreciation(app, category):
    """The loss used to be booked to Depreciation Expense, overstating it."""
    from shared.ledger_utils import posting_account
    from fixed_assets_app.routes.depreciation import post_asset_depreciation
    asset = make_asset(category, code="FA-D1")
    post_asset_depreciation(asset, date(2025, 1, 1), created_by=1)
    db.session.commit()

    result = _dispose(asset, proceeds=0)
    assert result == -820_000
    assert account_balance(posting_account("disposal_gain_loss").id) == 820_000
    assert account_balance(posting_account("depreciation_expense").id) == 180_000
    assert account_balance(asset.fixed_asset_account_id) == 0
    assert account_balance(asset.accum_dep_account_id) == 0


def test_selling_above_book_value_books_a_gain(app, category):
    from shared.ledger_utils import posting_account
    asset = make_asset(category, code="FA-D2")
    result = _dispose(asset, proceeds=1_200_000)
    assert result == 200_000
    # a gain is a credit, so the net expense account goes negative
    assert account_balance(posting_account("disposal_gain_loss").id) == -200_000
    assert account_balance(posting_account("cash").id) == 1_200_000


def test_selling_below_book_value_books_a_loss(app, category):
    from shared.ledger_utils import posting_account
    asset = make_asset(category, code="FA-D3")
    result = _dispose(asset, proceeds=600_000)
    assert result == -400_000
    assert account_balance(posting_account("disposal_gain_loss").id) == 400_000


def test_undoing_a_disposal_restores_cost_and_depreciation(app, category):
    """Disposal used to be a one-way door."""
    from shared.ledger_utils import reverse_journal_entry
    from fixed_assets_app.routes.depreciation import post_asset_depreciation
    asset = make_asset(category, code="FA-D4")
    post_asset_depreciation(asset, date(2025, 1, 1), created_by=1)
    db.session.commit()
    _dispose(asset, proceeds=500_000)

    reverse_journal_entry("FA-DISP", asset.id, created_by=1)
    asset.status = "active"
    asset.is_active = True
    asset.recalculate()
    db.session.commit()

    assert account_balance(asset.fixed_asset_account_id) == 1_000_000
    assert asset.posted_depreciation == 180_000
    assert asset.posted_depreciation == -account_balance(asset.accum_dep_account_id)
    assert asset.current_book_value == 820_000


# ── Transfer to inventory ───────────────────────────────────────────────────

def _transfer(asset, product):
    from shared.ledger_utils import create_entity_account
    from shared.models.asset_transfer import AssetTransfer
    from shared.models.stock_ledger import VoucherNumber
    from fixed_assets_app.routes.transfers import _approve_transfer
    stock_account = create_entity_account("product", product.id, product.name)
    transfer = AssetTransfer(voucher_number=VoucherNumber.next("FA-TRF"),
                             direction="to_inventory", asset_id=asset.id,
                             product_id=product.id, transfer_amount=0,
                             status="unapproved", created_by=1)
    db.session.add(transfer)
    db.session.flush()
    _approve_transfer(transfer, asset, stock_account.id, product.name, created_by=1)
    db.session.commit()
    return transfer, stock_account


def _receive_stock(product, qty, unit_cost):
    """Seed stock the way a purchase does: layers AND the matching GL debit."""
    from shared import costing
    from shared.ledger_utils import (create_entity_account, posting_account,
                                     post_journal_entry)
    costing.record_in(product.id, "PI", 1, "PI-1", qty=qty, unit_cost=unit_cost,
                      created_by=1)
    stock_account = create_entity_account("product", product.id, product.name)
    post_journal_entry(
        voucher_type="PI", voucher_id=1, voucher_number="PI-1",
        description="Opening purchase", entry_date=date(2025, 1, 1), created_by=1,
        lines=[
            {"account_id": stock_account.id, "debit": qty * unit_cost, "credit": 0,
             "description": "stock"},
            {"account_id": posting_account("ap").id, "debit": 0,
             "credit": qty * unit_cost, "description": "supplier"},
        ],
    )
    db.session.commit()
    return stock_account


@pytest.fixture
def product(app):
    from inventory_app.models.product import InvProduct
    p = InvProduct(name="Ex-asset", sku="EXA", current_stock=0, cost_price=0,
                   unit_price=0, is_active=True)
    db.session.add(p)
    db.session.commit()
    return p


def test_transfer_creates_stock_matching_the_journal(app, category, product):
    """The journal used to be posted with no stock movement at all."""
    from shared import costing
    from shared.models.stock_layer import StockLayer
    asset = make_asset(category, code="FA-TR", salvage=0.0)
    transfer, stock_account = _transfer(asset, product)

    assert StockLayer.query.filter_by(product_id=product.id).count() == 1
    assert float(costing.stock_value(product.id)) == account_balance(stock_account.id)
    assert product.current_stock == 1
    # the asset is fully derecognised
    assert account_balance(asset.fixed_asset_account_id) == 0
    costing.assert_invariant(product.id)


def test_capitalising_stock_books_it_at_the_costing_engine_value(app, category, product):
    """The inventory -> fixed asset direction had no implementation at all."""
    from shared import costing
    from shared.models.asset_transfer import AssetTransfer
    from shared.models.stock_ledger import VoucherNumber
    from shared.ledger_utils import create_entity_account
    from fixed_assets_app.models.asset import FixedAsset
    from fixed_assets_app.routes.transfers import _capitalise_from_stock

    stock_account = _receive_stock(product, qty=5, unit_cost=20_000)

    asset = FixedAsset(asset_code="FA-CAP1", name="Forklift",
                       category_id=category.id, purchase_date=date(2025, 1, 1),
                       purchase_cost=0, useful_life=5,
                       depreciation_method="straight_line", salvage_value=0,
                       current_book_value=0, status="active")
    db.session.add(asset)
    db.session.flush()
    transfer = AssetTransfer(voucher_number=VoucherNumber.next("FA-CAP"),
                             direction="to_fixed_asset", asset_id=asset.id,
                             source_product_id=product.id, product_id=product.id,
                             status="unapproved", created_by=1)
    db.session.add(transfer)
    db.session.flush()
    _capitalise_from_stock(transfer, product, qty=1, created_by=1)
    db.session.commit()

    assert asset.purchase_cost == 20_000
    assert account_balance(asset.fixed_asset_account_id) == 20_000
    assert float(costing.stock_value(product.id)) == 80_000
    assert float(costing.stock_value(product.id)) == account_balance(stock_account.id)
    costing.assert_invariant(product.id)


def test_unapproving_a_capitalisation_returns_the_stock(app, category, product):
    from shared import costing
    from shared.ledger_utils import reverse_journal_entry, create_entity_account
    from shared.models.asset_transfer import AssetTransfer
    from shared.models.stock_ledger import VoucherNumber
    from fixed_assets_app.models.asset import FixedAsset
    from fixed_assets_app.routes.transfers import _capitalise_from_stock

    stock_account = _receive_stock(product, qty=5, unit_cost=20_000)
    asset = FixedAsset(asset_code="FA-CAP2", name="Forklift",
                       category_id=category.id, purchase_date=date(2025, 1, 1),
                       purchase_cost=0, useful_life=5,
                       depreciation_method="straight_line", salvage_value=0,
                       current_book_value=0, status="active")
    db.session.add(asset)
    db.session.flush()
    transfer = AssetTransfer(voucher_number=VoucherNumber.next("FA-CAP"),
                             direction="to_fixed_asset", asset_id=asset.id,
                             source_product_id=product.id, product_id=product.id,
                             status="unapproved", created_by=1)
    db.session.add(transfer)
    db.session.flush()
    _capitalise_from_stock(transfer, product, qty=1, created_by=1)
    db.session.commit()

    costing.reverse_voucher_stock("FA-CAP", transfer.id, created_by=1)
    reverse_journal_entry("FA-CAP", transfer.id, created_by=1)
    asset.purchase_cost = 0
    asset.recalculate()
    db.session.commit()

    assert float(costing.stock_value(product.id)) == 100_000
    assert float(costing.stock_value(product.id)) == account_balance(stock_account.id)
    assert account_balance(asset.fixed_asset_account_id) == 0
    costing.assert_invariant(product.id)


def test_unapproving_a_transfer_withdraws_stock_and_journal(app, category, product):
    from shared import costing
    from shared.ledger_utils import reverse_journal_entry
    asset = make_asset(category, code="FA-TR2", salvage=0.0)
    transfer, stock_account = _transfer(asset, product)

    costing.reverse_voucher_stock("FA-TRF", transfer.id, created_by=1)
    reverse_journal_entry("FA-TRF", transfer.id, created_by=1)
    asset.status = "active"
    asset.is_active = True
    asset.recalculate()
    db.session.commit()

    assert float(costing.stock_value(product.id)) == 0
    assert account_balance(stock_account.id) == 0
    assert product.current_stock == 0
    assert account_balance(asset.fixed_asset_account_id) == 1_000_000
    costing.assert_invariant(product.id)
