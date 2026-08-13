"""E2E for how a per-item charge is split across lines (§6.2).

The split lives in the form's own recalc(), so these drive the real rendered
page and read the real Add. charges cells rather than reimplementing the maths.

The two lines are chosen so all three methods disagree — a test passing under
the wrong method is impossible:

    line          net     qty   total weight
    2 x 1,000    2,000     2      2 x 5  = 10
    2 x   500    1,000     2     2 x 15  = 30

    a 100 charge splits   66.67 / 33.33  by value
                             50 / 50     by quantity
                             25 / 75     by weight
"""

import os

# Same port the harness starts the server on (tests/e2e/conftest.py).
BASE_URL = "http://localhost:" + os.environ.get("E2E_PORT", "5050")
SALES_INVOICE = f"{BASE_URL}/inventory/invoices/"


SETUP = """
(method) => {
  // The charge goes in first: the Add. charges column is derived from the
  // charge set, so pushing it before the rows mount is what makes it render.
  charges.length = 0;
  charges.push({charge_account_id: 1, _display: 'Freight', _meta: '',
                description: 'Freight', amount: 100, scope: 'individual',
                distribution: method, treatment: 'bill',
                st_taxable: true, wht_taxable: false, extra_taxable: false});
  document.getElementById('itemsBody').innerHTML = '';
  rowCount = 0;
  addRow({quantity: 2, unit_price: 1000, weight: 5});
  addRow({quantity: 2, unit_price: 500,  weight: 15});
  refreshCols();
  document.querySelectorAll('#itemsBody tr').forEach(tr => recalcRow(tr));
  recalc();
  return Array.from(document.querySelectorAll('#itemsBody [data-col="addcharges"]'))
              .map(c => (c.tagName === 'INPUT' ? c.value : c.textContent));
}
"""


def _pass_gate(page):
    """Answer the §4.1 source gate with "blank" so the form is workable.

    A new invoice opens on that gate and it covers the grid until answered, so
    every test here has to get past it before touching the form.
    """
    gate = page.locator("#gateBlank")
    if gate.count():
        gate.click()
        page.wait_for_timeout(150)


def _shares(page, url, method):
    """The charge shares the page itself computed, as floats."""
    page.goto(url)
    page.wait_for_load_state("networkidle")
    _pass_gate(page)
    raw = page.evaluate(SETUP, method)
    return [float(str(v).replace(",", "").strip()) for v in raw]


class TestSalesInvoiceDistribution:
    def test_by_value_splits_on_line_net(self, admin_page):
        assert _shares(admin_page, SALES_INVOICE, "pro_rata_value") == [66.67, 33.33]

    def test_by_qty_splits_on_quantity(self, admin_page):
        """Equal quantities split evenly even though the lines are worth
        2,000 and 1,000."""
        assert _shares(admin_page, SALES_INVOICE, "pro_rata_qty") == [50.0, 50.0]

    def test_by_weight_splits_on_total_mass(self, admin_page):
        """The heavier line carries three times the freight despite being worth
        half as much. Before products had a weight this silently returned the
        by-value split."""
        assert _shares(admin_page, SALES_INVOICE, "by_weight") == [25.0, 75.0]

    def test_every_method_foots_to_the_charge(self, admin_page):
        for method in ("pro_rata_value", "pro_rata_qty", "by_weight"):
            shares = _shares(admin_page, SALES_INVOICE, method)
            assert sum(shares) == 100.0, f"{method} footed to {sum(shares)}"


MANUAL_SETUP = """
(amounts) => {
  charges.length = 0;
  charges.push({charge_account_id: 1, _display: 'Freight', _meta: '',
                description: 'Freight', amount: 100, scope: 'individual',
                distribution: 'manual', treatment: 'bill', manual: amounts,
                st_taxable: true, wht_taxable: false, extra_taxable: false});
  document.getElementById('itemsBody').innerHTML = '';
  rowCount = 0;
  addRow({quantity: 2, unit_price: 1000, weight: 5});
  addRow({quantity: 2, unit_price: 500,  weight: 15});
  refreshCols();
  document.querySelectorAll('#itemsBody tr').forEach(tr => recalcRow(tr));
  recalc();
  return {
    cells: Array.from(document.querySelectorAll('#itemsBody [data-col="addcharges"]'))
                .map(c => (c.tagName === 'INPUT' ? c.value : c.textContent)),
    unallocated: charges[0]._unallocated,
  };
}
"""


class TestManualDistribution:
    """§5.1 — in manual mode the typed amounts ARE the split. Nothing is derived,
    and nothing is forced onto the last line to make it foot."""

    def _run(self, page, amounts):
        page.goto(SALES_INVOICE)
        page.wait_for_load_state("networkidle")
        _pass_gate(page)
        out = page.evaluate(MANUAL_SETUP, amounts)
        return ([float(str(v).replace(",", "").strip()) for v in out["cells"]],
                out["unallocated"])

    def test_typed_amounts_are_used_verbatim(self, admin_page):
        """30/70 is not what any derived method would produce — by value it
        would be 66.67/33.33 — so this can only pass if the input was honoured."""
        cells, _ = self._run(admin_page, [30, 70])
        assert cells == [30.0, 70.0]

    def test_a_short_allocation_is_reported_not_absorbed(self, admin_page):
        """The older methods force the residual onto the last line. Manual must
        not: silently moving the operator's money is worse than a visible gap."""
        cells, unallocated = self._run(admin_page, [30, 50])
        assert cells == [30.0, 50.0]
        assert unallocated == 20.0

    def test_an_over_allocation_reports_a_negative_gap(self, admin_page):
        cells, unallocated = self._run(admin_page, [80, 40])
        assert cells == [80.0, 40.0]
        assert unallocated == -20.0

    def test_a_fully_allocated_charge_reports_no_gap(self, admin_page):
        cells, unallocated = self._run(admin_page, [40, 60])
        assert cells == [40.0, 60.0]
        assert unallocated == 0

    def test_lines_with_no_entry_take_nothing(self, admin_page):
        cells, unallocated = self._run(admin_page, [])
        assert cells == [0.0, 0.0]
        assert unallocated == 100.0

    def test_the_cell_opens_an_editor_and_typing_in_it_moves_the_split(self, admin_page):
        """The whole point of §5.1: the Add. charges cell stops being read-only
        and becomes where the per-line amount is entered."""
        self._run(admin_page, [40, 60])

        admin_page.locator('#itemsBody [data-col="addcharges"]').first.click()
        editor = admin_page.locator("#chgPopBody .pop-man").first
        editor.wait_for(state="visible")
        assert editor.input_value() == "40"

        editor.fill("25")
        editor.dispatch_event("change")
        admin_page.wait_for_timeout(200)

        cells = admin_page.evaluate(
            "() => Array.from(document.querySelectorAll('#itemsBody [data-col=\\\"addcharges\\\"]'))"
            ".map(c => c.tagName === 'INPUT' ? c.value : c.textContent)")
        assert [float(str(v).replace(",", "").strip()) for v in cells] == [25.0, 60.0]
        assert admin_page.evaluate("() => charges[0]._unallocated") == 15.0

    def test_a_zero_line_still_opens_the_editor(self, admin_page):
        """A line with nothing allocated yet is precisely the one that needs the
        editor, so the zero-cell guard must not swallow the click."""
        self._run(admin_page, [])
        admin_page.locator('#itemsBody [data-col="addcharges"]').first.click()
        assert admin_page.locator("#chgPopBody .pop-man").first.is_visible()


# The sales order is deliberately not covered here. It never received the
# unified Add. charges column — its grid still carries the legacy per-line
# Delivery and Installation inputs — so its recalc writes the computed share to
# a [data-col="addcharges"] cell that does not exist, and there is nothing to
# read back. The share is not inert: it feeds per-line sales tax, which is why
# the split there was corrected to honour each charge's method. Asserting it
# needs the column built first.
