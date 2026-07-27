import sys
from pathlib import Path

import pytest
from flask import Flask

HR_PROJECT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(HR_PROJECT))

from shared.extensions import db  # noqa: E402


@pytest.fixture
def app():
    """A minimal app holding only what the costing engine touches.

    Deliberately not the real app factory: these are unit tests over the
    engine's arithmetic, and booting the full app would drag in seeding,
    the chart of accounts and every blueprint for no benefit.
    """
    application = Flask(__name__)
    application.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    application.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(application)

    # Imported for their side effect: registering the tables on the metadata
    # that create_all() reads.
    import shared.models.ledger  # noqa: F401  (chart_of_accounts: FK target)
    import shared.models.stock_ledger  # noqa: F401
    import shared.models.stock_layer  # noqa: F401
    import shared.models.company_settings  # noqa: F401  (accounting_periods, fiscal_year_rule)
    import shared.models.invoice_template  # noqa: F401  (FK target for report_settings)
    import inventory_app.models.product  # noqa: F401

    with application.app_context():
        db.create_all()
        yield application
        db.session.remove()
        db.drop_all()


@pytest.fixture
def settings(app):
    from shared.models.inventory_settings import InventorySettings
    s = InventorySettings(valuation_method="weighted_average",
                          allow_negative_stock=False)
    db.session.add(s)
    db.session.commit()
    return s


@pytest.fixture
def product(app):
    from inventory_app.models.product import InvProduct
    p = InvProduct(id=1, name="Widget", sku="W1", current_stock=0, cost_price=0)
    db.session.add(p)
    db.session.commit()
    return p
