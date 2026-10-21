"""The form's per-line sales tax must include every charge in the base.

§10 Example B puts a Combined freight charge (100.00) and a Per-item labour
charge (60.00) into the tax base. The per-line total used to compute each line as
``net x rate``, silently dropping the Combined share: 1,800 x 18% + 2,850 x 17%
= 808.50 instead of 836.32. Absorbed charges were missing for the same reason.

These drive the rendered form's own recalc() and read the totals panel, so the
browser calculation and the server split are pinned to the same figures.
"""

import os

BASE_URL = "http://127.0.0.1:" + os.environ.get("E2E_PORT", "5050")
SALES_INVOICE = f"{BASE_URL}/inventory/invoices/"


def _open(page):
    page.goto(SALES_INVOICE)
    page.wait_for_load_state("networkidle")
    gate = page.locator("#gateBlank")
    if gate.count():
        gate.click()
        page.locator("#gateContinue").click()
        page.wait_for_timeout(150)
    return page


EXAMPLE_B_SETUP = """
() => {
  setScopeMode('discount', 'individual');
  setScopeMode('tax', 'individual');
  document.getElementById('settingsDiscountMethod').value = 'amount';
  applyDiscountUI();
  charges.length = 0;
  charges.push(
    {charge_account_id: 1, _display: 'Freight', _meta: '',
     description: 'Freight', amount: 100, scope: 'general',
     distribution: 'pro_rata_value', treatment: 'bill',
     st_taxable: true, wht_taxable: false, extra_taxable: false},
    {charge_account_id: 2, _display: 'Labour', _meta: '',
     description: 'Labour', amount: 60, scope: 'individual',
     distribution: 'pro_rata_value', treatment: 'bill',
     st_taxable: true, wht_taxable: false, extra_taxable: false},
    {charge_account_id: 3, _display: 'Unloading', _meta: '',
     description: 'Unloading', amount: 40, scope: 'general',
     distribution: 'pro_rata_value', treatment: 'expense',
     st_taxable: false, wht_taxable: false, extra_taxable: false}
  );
  document.getElementById('itemsBody').innerHTML = '';
  rowCount = 0;
  addRow({quantity: 2, unit_price: 1000, discount_amount: 200,
          sales_tax_pct: 18});
  addRow({quantity: 1, unit_price: 3000, discount_amount: 150,
          sales_tax_pct: 17});
  refreshCols();
  document.querySelectorAll('#itemsBody tr').forEach(tr => recalcRow(tr));
  recalc();
  const d = collectData();
  return {
    subtotal: d.subtotal,
    discount: d.total_discount,
    taxable: d.taxable_value,
    tax: d.total_tax,
    total: d.total_amount,
    lineCharges: Array.from(
      document.querySelectorAll('#itemsBody [data-col="addcharges"]'))
      .map(c => parseFloat(c.textContent.replace(/,/g, ''))),
  };
}
"""


ABSORBED_SETUP = """
() => {
  setScopeMode('discount', 'general');
  setScopeMode('tax', 'individual');
  charges.length = 0;
  charges.push({charge_account_id: 1, _display: 'Freight', _meta: '',
                description: 'Freight', amount: 100, scope: 'general',
                distribution: 'pro_rata_value', treatment: 'absorb',
                st_taxable: false, wht_taxable: false, extra_taxable: false});
  document.getElementById('itemsBody').innerHTML = '';
  rowCount = 0;
  addRow({quantity: 1, unit_price: 1000, sales_tax_pct: 10});
  refreshCols();
  document.querySelectorAll('#itemsBody tr').forEach(tr => recalcRow(tr));
  recalc();
  const d = collectData();
  return {tax: d.total_tax, taxable: d.taxable_value};
}
"""


class TestPerLineTaxBase:
    def test_example_b_includes_combined_and_per_item_charges(self, admin_page):
        """Freight 100 (Combined, taxed) and Labour 60 (Per-item, taxed) give
        bases 1,861.94 / 2,948.06 and tax 335.15 + 501.17 = 836.32."""
        _open(admin_page)
        out = admin_page.evaluate(EXAMPLE_B_SETUP)
        assert out["subtotal"] == 5000.0
        assert abs(out["discount"]) == 350.0
        assert out["taxable"] == 4810.0
        assert out["tax"] == 836.32
        assert out["total"] == 5646.32
        # Only the per-item charge lands in the Add. charges column.
        assert out["lineCharges"] == [23.23, 36.77]

    def test_an_absorbed_charge_raises_the_per_line_base(self, admin_page):
        """An absorbed charge is already in the goods value, so a 10% line of
        1,000 with 100 absorbed carries 110.00 tax, not 100.00."""
        _open(admin_page)
        out = admin_page.evaluate(ABSORBED_SETUP)
        assert out["taxable"] == 1100.0
        assert out["tax"] == 110.0
