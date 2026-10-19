"""Phone-width layout across the app's list pages and dashboards.

The shell sets body{overflow-x:hidden}, so anything wider than a phone was
not scrollable — it was cut off, with the columns past the edge unreachable.
Causes fixed: `.table-responsive` (used by most inventory/invoicing lists)
had no CSS at all; ui.css made `.table-wrap` overflow:hidden; header import
forms kept a ~250px file picker; the inventory dashboard used fixed 2- and
4-column inline grids; and the stock list multiplied a number by a FORMATTED
STRING (`qty * cost|amount_format`), printing "28,00028,000…" 50 times over.
"""
import os
import re

import pytest

BASE_URL = "http://127.0.0.1:" + os.environ.get("E2E_PORT", "5050")

PAGES = [
    "/inventory/dashboard", "/inventory/products/", "/inventory/customers/",
    "/inventory/suppliers/", "/inventory/units/", "/inventory/categories/",
    "/inventory/stock/", "/inventory/stock/movements",
    "/inventory/invoices/list", "/inventory/purchase-invoice/list",
    "/inventory/purchase-return/list", "/invoicing/sales-return/list",
    "/inventory/vouchers/product-ledger/list",
    "/fixed-assets/assets/", "/fixed-assets/categories/", "/fixed-assets/reports/",
    "/fixed-assets/reports/movement", "/fixed-assets/transfers/", "/fixed-assets/depreciation/",
    "/inventory/transfer-to-fa/", "/compensation/run-payroll", "/compensation/tax-settings",
    "/compensation/loans", "/pf/", "/accounting/bank-reconciliation",
    "/compensation/",
    "/invoicing/tracking/", "/invoicing/tracking/sales", "/invoicing/tracking/purchases",
]

# Elements wider than the screen that are NOT inside a scroll container.
CLIPPED_JS = r"""() => {
  const W = innerWidth, out = [];
  for (const el of document.querySelectorAll('body *')) {
    const r = el.getBoundingClientRect();
    if (r.right <= W + 2 || !r.width) continue;
    // Inside a scroll container, or a fixed overlay (a closed slide-in
    // drawer parked off-screen): neither widens the page.
    let p = el.parentElement, scrolls = false;
    while (p && p !== document.body) {
      const cs = getComputedStyle(p);
      if (cs.overflowX === 'auto' || cs.overflowX === 'scroll' || cs.position === 'fixed') {
        scrolls = true; break;
      }
      p = p.parentElement;
    }
    if (!scrolls && getComputedStyle(el).position !== 'fixed')
      out.push(el.tagName + '.' + [...el.classList].join('.') + ' right=' + Math.round(r.right));
  }
  return out.slice(0, 5);
}"""


@pytest.mark.parametrize("path", PAGES)
def test_nothing_is_cut_off_on_a_360px_phone(admin_mobile, path):
    page = admin_mobile
    page.set_viewport_size({"width": 360, "height": 780})
    page.goto(BASE_URL + path)
    page.wait_for_load_state("networkidle")
    clipped = page.evaluate(CLIPPED_JS)
    assert clipped == [], f"{path}: content past the screen edge: {clipped}"


def test_stock_value_is_a_number_not_a_repeated_string(admin_page):
    admin_page.goto(f"{BASE_URL}/inventory/stock/")
    admin_page.wait_for_load_state("networkidle")
    cells = admin_page.locator("table.table tbody tr td:nth-child(6)").all_inner_texts()
    assert cells, "stock list rendered no rows"
    for c in cells:
        assert re.fullmatch(r"-?[\d,]+(\.\d+)?", c.strip()), c[:60]
        assert len(c.strip()) < 20


def test_payroll_run_page_fits_a_360px_phone(admin_mobile):
    """The payroll run detail page (slips, totals, accounting entry button)."""
    import datetime
    page = admin_mobile
    page.set_viewport_size({"width": 360, "height": 780})
    # The E2E company has no payroll profile and no run: give the first
    # employee a profile through the form, then run last month (replace_all
    # keeps it repeatable).
    page.goto(BASE_URL + "/compensation/create-profile")
    uid = page.locator("select[name=user_id] option[value]:not([value=''])").first.get_attribute("value")
    page.request.post(BASE_URL + "/compensation/create-profile", form={
        "user_id": uid, "basic_salary": "50000", "effective_from": "2020-01-01"})
    last = datetime.date.today().replace(day=1) - datetime.timedelta(days=1)
    r = page.request.post(BASE_URL + "/compensation/run-payroll", form={
        "month": str(last.month), "year": str(last.year), "replace_all": "1"})
    assert r.ok, r.status
    page.goto(BASE_URL + "/compensation/")
    page.wait_for_load_state("networkidle")
    href = page.evaluate("""() => { const a = document.querySelector('a[href*="/compensation/run/"]');
                                    return a ? a.getAttribute('href') : null; }""")
    assert href, "no payroll run to open"
    page.goto(BASE_URL + href)
    page.wait_for_load_state("networkidle")
    assert page.locator("#gleFab").count() == 1
    clipped = page.evaluate(CLIPPED_JS)
    assert clipped == [], f"{href}: content past the screen edge: {clipped}"
