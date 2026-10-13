"""Range checks applied when a sales or purchase invoice is approved."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from shared.doc_checks import amount_errors  # noqa: E402


def _doc(**row):
    base = {"product_id": 1, "quantity": 2, "unit_price": 100}
    base.update(row)
    return {"items": [base]}


def test_a_normal_invoice_passes():
    assert amount_errors({**_doc(discount_pct=10, sales_tax_pct=18),
                          "global_discount_pct": 5, "withholding_tax_pct": 4.5}) == []


def test_negative_price_is_refused():
    assert any("unit price" in e for e in amount_errors(_doc(unit_price=-5)))


def test_discount_over_100_percent_is_refused():
    assert any("discount" in e for e in amount_errors(_doc(discount_pct=150)))
    assert any("Discount" in e for e in amount_errors({**_doc(), "global_discount_pct": 101}))


def test_discount_bigger_than_the_line_is_refused():
    assert any("more than the line" in e for e in amount_errors(_doc(discount_amount=500)))


def test_negative_tax_and_charges_are_refused():
    assert any("sales tax" in e for e in amount_errors(_doc(sales_tax_pct=-1)))
    assert any("Charge 1" in e for e in amount_errors({**_doc(), "charges": [{"amount": -10}]}))


def test_text_in_a_number_field_is_reported_not_a_crash():
    assert any("must be a number" in e for e in amount_errors(_doc(unit_price="abc")))
    assert amount_errors({**_doc(), "global_sales_tax_pct": "", "further_tax_pct": None}) == []
