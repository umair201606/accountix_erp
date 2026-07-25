"""Unit tests for the credit-note -> order-balance allocation (§4.4).

``allocate_return_to_order_lines`` is pure, so it is tested directly with stub
line objects rather than through a seeded invoice: the interesting behaviour is
how a returned quantity is apportioned when a product was billed more than
once, which is awkward to set up and easy to misread through the UI.
"""

from shared.order_linkage import (allocate_return_to_order_lines,
                                  tally_returned_quantities)


class ReturnRow:
    """Stands in for a credit-note item row."""

    def __init__(self, product_id, current_return_qty):
        self.product_id = product_id
        self.current_return_qty = current_return_qty


class Line:
    """Stands in for an invoice item: the three attributes the allocator reads."""

    def __init__(self, product_id, quantity, source_order_item_id=None):
        self.product_id = product_id
        self.quantity = quantity
        self.source_order_item_id = source_order_item_id


def released(lines, returned):
    """Allocation as a list of (order_item_id, qty), for readable assertions."""
    return [(r.source_order_item_id, r.quantity)
            for r in allocate_return_to_order_lines(lines, returned)]


def test_tally_sums_repeated_products_across_return_rows():
    """Two rows for one product must add up, not overwrite: the allocation works
    per product, so a lost row would silently under-release the balance."""
    rows = [ReturnRow(7, 2), ReturnRow(8, 5), ReturnRow(7, 3)]
    assert tally_returned_quantities(rows) == {7: 5.0, 8: 5.0}


def test_tally_skips_rows_with_no_product():
    rows = [ReturnRow(None, 4), ReturnRow(7, 2)]
    assert tally_returned_quantities(rows) == {7: 2.0}


def test_tally_treats_a_missing_quantity_as_zero():
    assert tally_returned_quantities([ReturnRow(7, None)]) == {7: 0.0}


def test_returned_qty_goes_back_to_the_order_line_that_billed_it():
    lines = [Line(product_id=7, quantity=10, source_order_item_id=99)]
    assert released(lines, {7: 4}) == [(99, 4.0)]


def test_a_full_return_releases_the_whole_billed_quantity():
    lines = [Line(product_id=7, quantity=10, source_order_item_id=99)]
    assert released(lines, {7: 10}) == [(99, 10.0)]


def test_lines_not_drawn_from_an_order_release_nothing():
    """A hand-typed invoice line never consumed a balance, so it has none to give
    back — releasing against it would invent order capacity."""
    lines = [Line(product_id=7, quantity=10)]
    assert released(lines, {7: 4}) == []


def test_products_absent_from_the_return_are_untouched():
    lines = [Line(product_id=7, quantity=10, source_order_item_id=99),
             Line(product_id=8, quantity=5, source_order_item_id=100)]
    assert released(lines, {7: 3}) == [(99, 3.0)]


def test_one_product_billed_on_two_lines_fills_the_first_line_first():
    """The return names a product, not a line. Filling in invoice order keeps the
    released total equal to the returned total without splitting fractionally."""
    lines = [Line(product_id=7, quantity=4, source_order_item_id=99),
             Line(product_id=7, quantity=6, source_order_item_id=100)]
    assert released(lines, {7: 4}) == [(99, 4.0)]


def test_a_return_larger_than_one_line_spills_onto_the_next():
    lines = [Line(product_id=7, quantity=4, source_order_item_id=99),
             Line(product_id=7, quantity=6, source_order_item_id=100)]
    assert released(lines, {7: 7}) == [(99, 4.0), (100, 3.0)]


def test_no_line_releases_more_than_it_billed():
    """Even if the returned quantity exceeds everything invoiced, each line is
    capped at its own quantity so no order line goes below zero invoiced."""
    lines = [Line(product_id=7, quantity=4, source_order_item_id=99),
             Line(product_id=7, quantity=6, source_order_item_id=100)]
    assert released(lines, {7: 25}) == [(99, 4.0), (100, 6.0)]


def test_zero_and_negative_returns_are_ignored():
    lines = [Line(product_id=7, quantity=10, source_order_item_id=99)]
    assert released(lines, {7: 0}) == []
    assert released(lines, {7: -3}) == []


def test_an_empty_return_allocates_nothing():
    lines = [Line(product_id=7, quantity=10, source_order_item_id=99)]
    assert released(lines, {}) == []


def test_a_zero_quantity_invoice_line_is_skipped_not_credited():
    lines = [Line(product_id=7, quantity=0, source_order_item_id=99),
             Line(product_id=7, quantity=5, source_order_item_id=100)]
    assert released(lines, {7: 2}) == [(100, 2.0)]


def test_fractional_quantities_do_not_accumulate_float_drift():
    """0.1 three times must not leave a 1e-17 crumb that reopens a closed line."""
    lines = [Line(product_id=7, quantity=0.1, source_order_item_id=99),
             Line(product_id=7, quantity=0.1, source_order_item_id=100),
             Line(product_id=7, quantity=0.1, source_order_item_id=101)]
    out = released(lines, {7: 0.3})
    assert [oid for oid, _ in out] == [99, 100, 101]
    assert round(sum(qty for _, qty in out), 6) == 0.3
