import sys
from pathlib import Path

import pytest
from flask import Flask

HR_PROJECT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(HR_PROJECT))

from shared.extensions import db  # noqa: E402
import shared.tenancy  # noqa: E402,F401  (registers the scoping listener)


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
    import shared.models.base  # noqa: F401  (users/roles: FK targets)
    import shared.models.ledger  # noqa: F401  (chart_of_accounts: FK target)
    import shared.models.stock_ledger  # noqa: F401
    import shared.models.stock_layer  # noqa: F401
    import shared.models.company_settings  # noqa: F401  (accounting_periods, fiscal_year_rule)
    import shared.models.invoice_template  # noqa: F401  (FK target for report_settings)
    import shared.models.company  # noqa: F401  (companies/memberships: tenancy)
    import inventory_app.models.product  # noqa: F401
    import inventory_app.models.supplier  # noqa: F401
    import inventory_app.models.customer  # noqa: F401

    shared.tenancy._reset_registry()

    with application.app_context():
        db.create_all()

        # The tenancy engine fails closed, so give every test a default
        # active company: scoped models can be created (stamped with it) and
        # queried without each fixture having to set one up. Tests that need
        # a specific tenant use the `company` fixture, which creates its own
        # row and switches the active company to it.
        from shared.models.company import Company
        from shared.tenancy import set_current_company, unscoped
        with unscoped():
            default = Company(name="Unit Test Co", slug="unit-default",
                              is_active=True)
            db.session.add(default)
            db.session.commit()
        set_current_company(default.id)

        yield application
        db.session.remove()
        db.drop_all()


@pytest.fixture
def company(app):
    """A tenant row; also selects it as the active company so scoped models
    work in unit tests (the tenancy hook fails closed without one)."""
    from shared.models.company import Company
    from shared.tenancy import set_current_company, unscoped
    with unscoped():
        c = Company(name="Test Co", slug="test-co", is_active=True)
        db.session.add(c)
        db.session.commit()
        set_current_company(c.id)
    return c


@pytest.fixture
def settings(company):
    from shared.models.inventory_settings import InventorySettings
    kwargs = {"valuation_method": "weighted_average",
              "allow_negative_stock": False}
    if hasattr(InventorySettings, "company_id"):
        kwargs["company_id"] = company.id
    s = InventorySettings(**kwargs)
    db.session.add(s)
    db.session.commit()
    return s


@pytest.fixture
def product(company):
    from inventory_app.models.product import InvProduct
    kwargs = {"id": 1, "name": "Widget", "sku": "W1",
              "current_stock": 0, "cost_price": 0}
    if hasattr(InvProduct, "company_id"):
        kwargs["company_id"] = company.id
    p = InvProduct(**kwargs)
    db.session.add(p)
    db.session.commit()
    return p
