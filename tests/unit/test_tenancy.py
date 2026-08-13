"""Spike: prove the tenancy engine scopes every query shape and fails closed.

Covers the three blocking issues from the design review:
  1. PK lookups (Query.get) at SQL level
  2. no stale company value across requests/contexts (no lambda caching)
  3. fail-closed when no company is active

Plus the shapes the finance reports use: plain, aggregate (func.sum), joins,
joined aggregates, lazy="dynamic" backrefs, and the unscoped() opt-out.
"""
import sys
from pathlib import Path

import pytest
from flask import Flask

ACCOUNTIX_ERP = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ACCOUNTIX_ERP))

from shared.extensions import db  # noqa: E402
import shared.tenancy as tenancy  # noqa: E402


class SpikeOwner(db.Model):
    __tablename__ = "spike_owners"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50))
    items = db.relationship("SpikeItem", backref="owner", lazy="dynamic")


class SpikeItem(db.Model):
    __tablename__ = "spike_items"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)  # tenant-scoped
    owner_id = db.Column(db.Integer, db.ForeignKey("spike_owners.id"))
    amount = db.Column(db.Float, default=0)


@pytest.fixture
def spike_app():
    application = Flask(__name__)
    application.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    application.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(application)
    import shared.models.base  # noqa: F401  (users/roles: FK targets)
    import shared.models.company  # noqa: F401
    import shared.models.ledger  # noqa: F401  (chart_of_accounts: FK target)
    import shared.models.inventory_settings  # noqa: F401  (FKs to COA)
    tenancy._reset_registry()
    with application.app_context():
        db.create_all()
        from shared.models.company import Company
        with tenancy.unscoped():
            c1 = Company(name="Alpha", slug="alpha", is_active=True)
            c2 = Company(name="Beta", slug="beta", is_active=True)
            db.session.add_all([c1, c2])
            db.session.commit()
            owner = SpikeOwner(name="owner")
            db.session.add(owner)
            db.session.commit()
            for cid, val in [(c1.id, 10.0), (c2.id, 20.0)]:
                db.session.add(SpikeItem(company_id=cid, owner_id=owner.id,
                                         amount=val))
            db.session.commit()
            c1_id, c2_id = c1.id, c2.id
        yield application, c1_id, c2_id
        db.session.remove()
        db.drop_all()


def _other_item_id(company_id):
    """A SpikeItem id belonging to `company_id` (read unscoped)."""
    with tenancy.unscoped():
        return SpikeItem.query.filter_by(company_id=company_id).first().id


def test_plain_query_filtered(spike_app):
    app, c1, c2 = spike_app
    with app.test_request_context():
        tenancy.set_current_company(c1)
        rows = SpikeItem.query.all()
        assert [r.company_id for r in rows] == [c1]
        assert len(rows) == 1


def test_aggregate_filtered(spike_app):
    app, c1, c2 = spike_app
    with app.test_request_context():
        tenancy.set_current_company(c2)
        total = db.session.query(db.func.sum(SpikeItem.amount)).scalar()
        assert total == 20.0  # only company 2's row


def test_join_filtered(spike_app):
    app, c1, c2 = spike_app
    with app.test_request_context():
        tenancy.set_current_company(c1)
        rows = db.session.query(SpikeItem).join(SpikeOwner).all()
        assert len(rows) == 1 and rows[0].company_id == c1


def test_joined_aggregate_filtered(spike_app):
    app, c1, c2 = spike_app
    with app.test_request_context():
        tenancy.set_current_company(c2)
        total = db.session.query(db.func.sum(SpikeItem.amount)).join(
            SpikeOwner).scalar()
        assert total == 20.0


def test_dynamic_backref_filtered(spike_app):
    app, c1, c2 = spike_app
    with app.test_request_context():
        tenancy.set_current_company(c1)
        owner = SpikeOwner.query.get(_other_item_id(c1))  # load by known id
        assert [i.company_id for i in owner.items.all()] == [c1]


def test_no_stale_value_across_contexts(spike_app):
    """The exact blocking issue: first request warms nothing, second request
    must still see its own company. (No cached lambda form.)"""
    app, c1, c2 = spike_app
    with app.test_request_context():
        tenancy.set_current_company(c1)
        assert [r.company_id for r in SpikeItem.query.all()] == [c1]
    with app.test_request_context():
        tenancy.set_current_company(c2)
        assert [r.company_id for r in SpikeItem.query.all()] == [c2]
    with app.test_request_context():
        tenancy.set_current_company(c1)
        assert [r.company_id for r in SpikeItem.query.all()] == [c1]


def test_fail_closed_without_company(spike_app):
    app, c1, c2 = spike_app
    with app.test_request_context():
        with pytest.raises(tenancy.NoActiveCompanyError):
            SpikeItem.query.all()


def test_unscoped_bypass(spike_app):
    app, c1, c2 = spike_app
    with app.test_request_context():
        with tenancy.unscoped():
            assert len(SpikeItem.query.all()) == 2


def test_scoped_get_filters_pk_lookup(spike_app):
    app, c1, c2 = spike_app
    with app.test_request_context():
        tenancy.set_current_company(c1)
        mine = SpikeItem.query.first()
        assert tenancy.scoped_get(SpikeItem, mine.id) is mine
        assert tenancy.scoped_get(SpikeItem, _other_item_id(c2)) is None


def test_query_get_is_filtered_at_sql_level(spike_app):
    """Documentation: Query.get() emits SQL under the criteria, so a foreign
    tenant's PK resolves to None. We still harden PK sites with scoped_get
    because identity-map hits bypass SQL entirely."""
    app, c1, c2 = spike_app
    with app.test_request_context():
        tenancy.set_current_company(c1)
        assert SpikeItem.query.get(_other_item_id(c2)) is None
