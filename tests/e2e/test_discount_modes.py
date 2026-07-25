"""E2E for the discount scope x method matrix (§5).

Two independent choices, both made in the settings panel: the scope (Combined or
Per line) and the method (by percentage or by amount). Exactly one control is
offered for the combination chosen.

The form used to show a % box and a value box together and resolve them with
``gVal > 0 ? gVal : subtotal * gPct / 100``, so which one won depended on typing
order; per line it wrote the % result into the amount field, making the two
inputs fight. These tests pin the replacement: whichever field is *visible* is
the only authority, and the other is derived from it.

Every case uses one line of 2 x 1,000 = 2,000 so the expected figures differ per
method and a passing test cannot be ambiguous about which rule ran.
"""

BASE_URL = "http://localhost:5000"
SALES_INVOICE = f"{BASE_URL}/inventory/invoices/"
PURCHASE_INVOICE = f"{BASE_URL}/inventory/purchase-invoice/"

SETUP = """
([scope, method]) => {
  // Click the real scope pill rather than an internal helper — only one of
  // the two templates defines one, and the pill is what a user touches.
  const grp = document.getElementById('discountMode');
  const btn = grp && grp.querySelector('.pill-b[data-value="' + scope + '"]');
  if (btn && !btn.disabled) btn.click();
  document.getElementById('settingsDiscountMethod').value = method;
  applyDiscountUI();
  document.getElementById('itemsBody').innerHTML = '';
  rowCount = 0;
  addRow({quantity: 2, unit_price: 1000});
  document.querySelectorAll('#itemsBody tr').forEach(tr => recalcRow(tr));
  recalc();
}
"""

READ = """
() => {
  const vis = el => !!(el && el.offsetParent !== null);
  const row = document.querySelector('#itemsBody tr');
  return {
    pctBox: vis(document.getElementById('globalDiscPct')),
    valBox: vis(document.getElementById('globalDiscVal')),
    pctCol: vis(document.querySelector('td.c-disc-pct')),
    amtCol: vis(document.querySelector('td.c-disc-amt')),
    discount: document.getElementById('summaryDiscount').textContent,
    linePct: row.querySelector('[data-col="discount_pct"]').value,
    lineAmt: row.querySelector('[data-col="discount_amount"]').value,
    net: row.querySelector('[data-col="net_amount"]').value,
  };
}
"""


def _prepare(page, scope, method, url=SALES_INVOICE):
    page.goto(url)
    page.wait_for_load_state("networkidle")
    gate = page.locator("#gateBlank")
    if gate.count():
        gate.click()
        page.wait_for_timeout(150)
    page.evaluate(SETUP, [scope, method])
    return page


def _read(page):
    return page.evaluate(READ)


def _num(text):
    return float(str(text).replace(",", "").replace("-", "").strip())


class TestCombinedScope:
    def test_by_percentage_offers_only_the_percentage_box(self, admin_page):
        _prepare(admin_page, "general", "pct")
        s = _read(admin_page)
        assert s["pctBox"] is True
        assert s["valBox"] is False, "the amount box must not be offered as well"

    def test_by_percentage_applies_the_percentage_to_the_subtotal(self, admin_page):
        _prepare(admin_page, "general", "pct")
        admin_page.evaluate(
            "() => { document.getElementById('globalDiscPct').value = 10; recalc(); }")
        assert _num(_read(admin_page)["discount"]) == 200.0   # 10% of 2,000

    def test_by_amount_offers_only_the_amount_box(self, admin_page):
        _prepare(admin_page, "general", "amount")
        s = _read(admin_page)
        assert s["valBox"] is True
        assert s["pctBox"] is False

    def test_by_amount_uses_the_amount_verbatim(self, admin_page):
        _prepare(admin_page, "general", "amount")
        admin_page.evaluate(
            "() => { document.getElementById('globalDiscVal').value = 250; recalc(); }")
        assert _num(_read(admin_page)["discount"]) == 250.0

    def test_a_stale_percentage_cannot_hijack_an_amount_discount(self, admin_page):
        """The old rule picked whichever box was non-zero. With a leftover 10% in
        the hidden percentage box, an amount of 250 must still give 250 — not
        200, and not whichever was typed last."""
        _prepare(admin_page, "general", "amount")
        admin_page.evaluate("""() => {
          document.getElementById('globalDiscPct').value = 10;
          document.getElementById('globalDiscVal').value = 250;
          recalc();
        }""")
        assert _num(_read(admin_page)["discount"]) == 250.0

    def test_a_stale_amount_cannot_hijack_a_percentage_discount(self, admin_page):
        _prepare(admin_page, "general", "pct")
        admin_page.evaluate("""() => {
          document.getElementById('globalDiscVal').value = 999;
          document.getElementById('globalDiscPct').value = 10;
          recalc();
        }""")
        assert _num(_read(admin_page)["discount"]) == 200.0


class TestPerLineScope:
    def test_by_percentage_shows_only_the_percentage_column(self, admin_page):
        _prepare(admin_page, "individual", "pct")
        s = _read(admin_page)
        assert s["pctCol"] is True
        assert s["amtCol"] is False, "only the column being entered may show"

    def test_by_amount_shows_only_the_amount_column(self, admin_page):
        _prepare(admin_page, "individual", "amount")
        s = _read(admin_page)
        assert s["amtCol"] is True
        assert s["pctCol"] is False

    def test_the_combined_inputs_are_withdrawn_entirely(self, admin_page):
        """With the lines owning the discount, a document-level box would be a
        second, conflicting authority."""
        _prepare(admin_page, "individual", "pct")
        s = _read(admin_page)
        assert s["pctBox"] is False and s["valBox"] is False

    def test_the_summary_total_is_a_rollup_of_the_lines(self, admin_page):
        _prepare(admin_page, "individual", "pct")
        admin_page.evaluate("""() => {
          const row = document.querySelector('#itemsBody tr');
          row.querySelector('[data-col="discount_pct"]').value = 10;
          recalcRow(row); recalc();
        }""")
        s = _read(admin_page)
        assert _num(s["discount"]) == 200.0
        assert _num(s["net"]) == 1800.0

    def test_a_percentage_entry_derives_the_line_amount(self, admin_page):
        """The hidden field is kept in step so what is saved is a consistent
        pair, without ever being the thing the user typed."""
        _prepare(admin_page, "individual", "pct")
        admin_page.evaluate("""() => {
          const row = document.querySelector('#itemsBody tr');
          row.querySelector('[data-col="discount_pct"]').value = 10;
          recalcRow(row); recalc();
        }""")
        assert float(_read(admin_page)["lineAmt"]) == 200.0

    def test_an_amount_entry_derives_the_line_percentage(self, admin_page):
        _prepare(admin_page, "individual", "amount")
        admin_page.evaluate("""() => {
          const row = document.querySelector('#itemsBody tr');
          row.querySelector('[data-col="discount_amount"]').value = 300;
          recalcRow(row); recalc();
        }""")
        s = _read(admin_page)
        assert float(s["linePct"]) == 15.0      # 300 of 2,000
        assert _num(s["discount"]) == 300.0
        assert _num(s["net"]) == 1700.0

    def test_switching_method_does_not_double_the_discount(self, admin_page):
        """Going from % to amount must re-derive, not add the two bases together
        — the old code left both fields populated and summed whatever it found."""
        _prepare(admin_page, "individual", "pct")
        admin_page.evaluate("""() => {
          const row = document.querySelector('#itemsBody tr');
          row.querySelector('[data-col="discount_pct"]').value = 10;
          recalcRow(row); recalc();
          document.getElementById('settingsDiscountMethod').value = 'amount';
          applyDiscountUI();
        }""")
        assert _num(_read(admin_page)["discount"]) == 200.0


class TestPurchaseInvoiceParity:
    """The purchase invoice carries the same rule. It is a separate template
    with its own recalc(), so the fix has to be proven there too rather than
    assumed from the sales side."""

    def test_combined_by_percentage(self, admin_page):
        _prepare(admin_page, "general", "pct", PURCHASE_INVOICE)
        s = _read(admin_page)
        assert s["pctBox"] is True and s["valBox"] is False
        admin_page.evaluate(
            "() => { document.getElementById('globalDiscPct').value = 10; recalc(); }")
        assert _num(_read(admin_page)["discount"]) == 200.0

    def test_combined_by_amount_ignores_a_stale_percentage(self, admin_page):
        _prepare(admin_page, "general", "amount", PURCHASE_INVOICE)
        admin_page.evaluate("""() => {
          document.getElementById('globalDiscPct').value = 10;
          document.getElementById('globalDiscVal').value = 250;
          recalc();
        }""")
        assert _num(_read(admin_page)["discount"]) == 250.0

    def test_per_line_shows_one_column_only(self, admin_page):
        _prepare(admin_page, "individual", "pct", PURCHASE_INVOICE)
        s = _read(admin_page)
        assert s["pctCol"] is True and s["amtCol"] is False
        _prepare(admin_page, "individual", "amount", PURCHASE_INVOICE)
        s = _read(admin_page)
        assert s["amtCol"] is True and s["pctCol"] is False

    def test_per_line_rolls_up_and_derives(self, admin_page):
        _prepare(admin_page, "individual", "amount", PURCHASE_INVOICE)
        admin_page.evaluate("""() => {
          const row = document.querySelector('#itemsBody tr');
          row.querySelector('[data-col="discount_amount"]').value = 300;
          recalcRow(row); recalc();
        }""")
        s = _read(admin_page)
        assert float(s["linePct"]) == 15.0
        assert _num(s["discount"]) == 300.0
