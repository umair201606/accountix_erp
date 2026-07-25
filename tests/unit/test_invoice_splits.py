"""Per-line revenue and tax sub-account splits (§12.2).

Per-line mode "can post multiple lines against the same natural account type":
a different revenue account per item category, and a different Output Sales Tax
sub-account per rate. The invariant that matters is that a split still foots to
the figure being posted — a journal that stops balancing is far worse than one
that pools two categories together.
"""

import pytest
from flask import Flask

from shared.extensions import db
from shared.invoice_totals import _allocate, revenue_splits, output_tax_splits


class TestAllocate:
    """The shared pro-rata helper, tested directly: every split goes through it,
    so its rounding is the thing that decides whether the journal balances."""

    def test_splits_pro_rata(self):
        assert _allocate(100, [("a", 3), ("b", 1)]) == [("a", 75.0), ("b", 25.0)]

    def test_the_residual_lands_on_the_last_bucket(self):
        """100 over three equal buckets is 33.33 three times, which is 99.99.
        The last one absorbs the cent so the credit still foots."""
        out = _allocate(100, [("a", 1), ("b", 1), ("c", 1)])
        assert out == [("a", 33.33), ("b", 33.33), ("c", 33.34)]
        assert sum(v for _, v in out) == 100.0

    def test_weightless_buckets_share_equally(self):
        assert _allocate(100, [("a", 0), ("b", 0)]) == [("a", 50.0), ("b", 50.0)]

    def test_a_single_bucket_takes_everything(self):
        assert _allocate(1234.56, [("a", 7)]) == [("a", 1234.56)]

    def test_nothing_to_split_produces_nothing(self):
        assert _allocate(0, [("a", 1)]) == []
        assert _allocate(100, []) == []

    @pytest.mark.parametrize("total", [100, 0.03, 999999.99, 1234.57])
    def test_any_total_always_foots(self, total):
        out = _allocate(total, [("a", 2), ("b", 3), ("c", 5)])
        assert round(sum(v for _, v in out), 2) == round(total, 2)


@pytest.fixture
def app():
    """Minimal app holding only the tables the splits read."""
    application = Flask(__name__)
    application.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    application.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(application)

    import shared.models.ledger  # noqa: F401  (chart_of_accounts: FK target)
    import shared.models.invoice_settings  # noqa: F401
    import inventory_app.models.category  # noqa: F401
    import inventory_app.models.product  # noqa: F401
    import inventory_app.models.customer  # noqa: F401
    import inventory_app.models.invoice  # noqa: F401
    import inventory_app.models.additional_charge  # noqa: F401

    with application.app_context():
        db.create_all()
        yield application
        db.session.remove()
        db.drop_all()


def _account(code, name):
    from shared.models.ledger import ChartOfAccount
    a = ChartOfAccount(code=code, name=name, type="revenue")
    db.session.add(a)
    db.session.flush()
    return a


def _invoice(lines, tax_mode="individual", global_rate=0):
    """An invoice of (category_id, value, rate) lines."""
    from inventory_app.models.product import InvProduct
    from inventory_app.models.customer import InvCustomer
    from inventory_app.models.invoice import InvInvoice, InvInvoiceItem

    cust = InvCustomer(name="C")
    db.session.add(cust)
    db.session.flush()
    inv = InvInvoice(invoice_number="SI-1", voucher_number="V-1",
                     customer_id=cust.id, tax_mode=tax_mode,
                     global_sales_tax_pct=global_rate)
    db.session.add(inv)
    db.session.flush()
    for i, (category_id, value, rate) in enumerate(lines):
        p = InvProduct(sku=f"S{i}", name=f"P{i}", category_id=category_id,
                       current_stock=0, cost_price=0)
        db.session.add(p)
        db.session.flush()
        db.session.add(InvInvoiceItem(invoice_id=inv.id, product_id=p.id,
                                      quantity=1, unit_price=value,
                                      total_before_discount=value,
                                      sales_tax_pct=rate))
    db.session.flush()
    return inv


class TestRevenueSplits:
    def test_with_nothing_mapped_it_stays_one_credit(self, app):
        """The whole point of the fallback: a system that configures none of
        this must post exactly the single revenue credit it always did."""
        inv = _invoice([(None, 600, 0), (None, 400, 0)])
        assert revenue_splits(inv, 1000) == [(None, 1000.0)]

    def test_two_mapped_categories_credit_two_accounts(self, app):
        from inventory_app.models.category import InvCategory
        from shared.models.invoice_settings import CategoryRevenueAccount
        goods = InvCategory(name="Goods")
        services = InvCategory(name="Services")
        db.session.add_all([goods, services])
        db.session.flush()
        a1 = _account("4-01-01-01-0001", "Revenue - Goods")
        a2 = _account("4-01-01-02-0001", "Revenue - Services")
        db.session.add_all([
            CategoryRevenueAccount(category_id=goods.id, account_id=a1.id),
            CategoryRevenueAccount(category_id=services.id, account_id=a2.id)])
        db.session.flush()

        inv = _invoice([(goods.id, 600, 0), (services.id, 400, 0)])
        assert dict(revenue_splits(inv, 1000)) == {a1.id: 600.0, a2.id: 400.0}

    def test_an_unmapped_category_falls_back_alongside_a_mapped_one(self, app):
        from inventory_app.models.category import InvCategory
        from shared.models.invoice_settings import CategoryRevenueAccount
        goods = InvCategory(name="Goods")
        other = InvCategory(name="Other")
        db.session.add_all([goods, other])
        db.session.flush()
        a1 = _account("4-01-01-01-0001", "Revenue - Goods")
        db.session.add(CategoryRevenueAccount(category_id=goods.id, account_id=a1.id))
        db.session.flush()

        inv = _invoice([(goods.id, 700, 0), (other.id, 300, 0)])
        assert dict(revenue_splits(inv, 1000)) == {a1.id: 700.0, None: 300.0}

    def test_absorbed_charges_ride_along_pro_rata(self, app):
        """The amount passed in is subtotal + absorbed. The absorbed part
        belongs to no category, so it follows the goods it went into."""
        from inventory_app.models.category import InvCategory
        from shared.models.invoice_settings import CategoryRevenueAccount
        goods = InvCategory(name="Goods")
        db.session.add(goods)
        db.session.flush()
        a1 = _account("4-01-01-01-0001", "Revenue - Goods")
        db.session.add(CategoryRevenueAccount(category_id=goods.id, account_id=a1.id))
        db.session.flush()

        inv = _invoice([(goods.id, 500, 0), (None, 500, 0)])
        # 1,100 = 1,000 of goods plus 100 absorbed, so each side takes 550.
        assert dict(revenue_splits(inv, 1100)) == {a1.id: 550.0, None: 550.0}

    def test_the_split_always_foots_to_what_is_posted(self, app):
        from inventory_app.models.category import InvCategory
        from shared.models.invoice_settings import CategoryRevenueAccount
        cats = []
        for i in range(3):
            c = InvCategory(name=f"C{i}")
            db.session.add(c)
            db.session.flush()
            a = _account(f"4-01-01-0{i}-0001", f"Revenue {i}")
            db.session.add(CategoryRevenueAccount(category_id=c.id, account_id=a.id))
            cats.append(c)
        db.session.flush()

        inv = _invoice([(c.id, 333.33, 0) for c in cats])
        out = revenue_splits(inv, 1000)
        assert sum(v for _, v in out) == 1000.0


class TestOutputTaxSplits:
    def test_with_nothing_mapped_it_stays_one_credit(self, app):
        inv = _invoice([(None, 600, 18), (None, 400, 18)])
        assert output_tax_splits(inv, 180) == [(None, 180.0)]

    def test_two_rates_credit_two_sub_accounts(self, app):
        """A document carrying 18% and 5% should leave the two rates separable
        without unpicking a pooled balance."""
        from shared.models.invoice_settings import TaxRateAccount
        a18 = _account("2-01-03-01-0018", "Output Tax 18%")
        a5 = _account("2-01-03-01-0005", "Output Tax 5%")
        db.session.add_all([TaxRateAccount(rate_pct=18, account_id=a18.id),
                            TaxRateAccount(rate_pct=5, account_id=a5.id)])
        db.session.flush()

        # 1,000 @ 18% = 180 and 1,000 @ 5% = 50, so 230 splits 180 / 50.
        inv = _invoice([(None, 1000, 18), (None, 1000, 5)])
        assert dict(output_tax_splits(inv, 230)) == {a18.id: 180.0, a5.id: 50.0}

    def test_zero_rated_lines_draw_no_tax(self, app):
        from shared.models.invoice_settings import TaxRateAccount
        a18 = _account("2-01-03-01-0018", "Output Tax 18%")
        db.session.add(TaxRateAccount(rate_pct=18, account_id=a18.id))
        db.session.flush()

        inv = _invoice([(None, 1000, 18), (None, 5000, 0)])
        assert output_tax_splits(inv, 180) == [(a18.id, 180.0)]

    def test_combined_mode_uses_the_document_rate(self, app):
        """In combined mode the lines carry no rate of their own — the rate is
        the document's, so the whole credit belongs to that rate's account."""
        from shared.models.invoice_settings import TaxRateAccount
        a18 = _account("2-01-03-01-0018", "Output Tax 18%")
        db.session.add(TaxRateAccount(rate_pct=18, account_id=a18.id))
        db.session.flush()

        inv = _invoice([(None, 600, 0), (None, 400, 0)],
                       tax_mode="general", global_rate=18)
        assert output_tax_splits(inv, 180) == [(a18.id, 180.0)]

    def test_the_split_apportions_the_posted_tax_exactly(self, app):
        """It divides up the tax already posted rather than recomputing it, so
        the journal cannot drift from the invoice total by a rounding cent."""
        from shared.models.invoice_settings import TaxRateAccount
        a18 = _account("2-01-03-01-0018", "Output Tax 18%")
        a5 = _account("2-01-03-01-0005", "Output Tax 5%")
        db.session.add_all([TaxRateAccount(rate_pct=18, account_id=a18.id),
                            TaxRateAccount(rate_pct=5, account_id=a5.id)])
        db.session.flush()

        inv = _invoice([(None, 333.33, 18), (None, 333.33, 5)])
        out = output_tax_splits(inv, 76.67)
        assert sum(v for _, v in out) == 76.67
