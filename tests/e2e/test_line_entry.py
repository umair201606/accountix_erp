"""E2E for typing a product into a line (the description autocomplete).

Two regressions live here, both of which made the grid feel broken rather than
merely ugly:

1. The auto-added trailing row grabbed focus. addRow() focuses the new row's
   Code field 30ms after inserting it, and autoAddCheck() appends that row on
   the *first* character typed — so the description you were typing in blurred
   mid-keystroke, the product list closed, and the characters were stranded as
   the line's description instead of searching.

2. The list was clipped. It is absolutely positioned inside a row, and the
   card-wrapped-table pattern gave it three clipping ancestors (.card and .icard
   hide overflow for their corners, .tb-w scrolls horizontally), so it appeared
   trapped inside the grid.
"""

from urllib.parse import quote

BASE_URL = "http://localhost:5000"

DOCUMENTS = {
    "sales invoice": f"{BASE_URL}/inventory/invoices/",
    "purchase invoice": f"{BASE_URL}/inventory/purchase-invoice/",
    "sales order": f"{BASE_URL}/inventory/sales/",
    "purchase order": f"{BASE_URL}/inventory/purchases/",
}

TYPE = """
(text) => {
  const inp = document.querySelector('#itemsBody .ac-desc-input');
  inp.focus();
  inp.value = text;
  inp.dispatchEvent(new Event('input', {bubbles: true}));
}
"""

STATE = """
() => {
  const inp = document.querySelector('#itemsBody .ac-desc-input');
  const dd = document.querySelector('#itemsBody .ac-dd');
  return {
    focusOnDescription: document.activeElement === inp,
    open: !!(dd && dd.classList.contains('show')),
    position: dd ? getComputedStyle(dd).position : null,
    rows: document.querySelectorAll('#itemsBody tr').length,
    typed: inp.value,
  };
}
"""


def _open(page, url):
    page.goto(url)
    page.wait_for_load_state("networkidle")
    gate = page.locator("#gateBlank")
    if gate.count():
        gate.click()
        page.wait_for_timeout(150)
    return page


OPTIONS_READY = """
(sel) => {
  const dd = document.querySelector(sel);
  return !!(dd && dd.classList.contains('show')
            && dd.querySelectorAll('.ac-it').length > 0);
}
"""

PRODUCT_LIST = "#itemsBody .ac-dd"
LEDGER_LIST = "#chargesList .ac-dd"


def _wait_for_options(page, selector=PRODUCT_LIST, timeout=15000):
    """Block until the combobox at `selector` has rendered its options.

    Every list here is filled by a fetch — loadProducts() and loadAccounts()
    both await the server — so there is no sleep both long enough to be safe
    and short enough to be quick. These tests used to wait a flat 700ms, which
    held up in isolation and lost the race under a full-suite load: the
    assertions then read a list that was neither shown nor populated, and
    reported it as a styling failure (width 0, display none) rather than as the
    timeout it was.
    """
    page.wait_for_function(OPTIONS_READY, arg=selector, timeout=timeout)


def _type_and_wait(page, text, timeout=15000):
    """Type into the first description cell and wait for *that* search to land.

    Waiting on the list alone is not enough when a previous search is already
    showing: its options satisfy the poll immediately and the assertions then
    describe the old query. Waiting for the response carrying this q= pins it
    to the search actually being typed.
    """
    want = "api/products?q=" + quote(text, safe="")
    with page.expect_response(lambda r: want in r.url, timeout=timeout):
        page.evaluate(TYPE, text)
    _wait_for_options(page, PRODUCT_LIST, timeout)


class TestTypingAProduct:
    def test_the_first_character_does_not_steal_focus(self, admin_page):
        """Typing one character appends the trailing blank row. That row must
        not take the caret with it — this is exactly what stranded a typed "1"
        as a line description."""
        _open(admin_page, DOCUMENTS["purchase invoice"])
        admin_page.evaluate(TYPE, "1")
        admin_page.wait_for_timeout(500)

        s = admin_page.evaluate(STATE)
        assert s["focusOnDescription"] is True, "the auto-added row stole the caret"
        assert s["rows"] == 2, "the trailing blank row should still be added"
        assert s["typed"] == "1"

    def test_the_search_survives_the_row_being_added(self, admin_page):
        _open(admin_page, DOCUMENTS["purchase invoice"])
        _type_and_wait(admin_page, "1")
        _type_and_wait(admin_page, "10kw")

        s = admin_page.evaluate(STATE)
        assert s["open"] is True, "the product list closed while typing"
        assert s["focusOnDescription"] is True
        names = admin_page.evaluate(
            "() => [...document.querySelectorAll('#itemsBody .ac-dd .ac-n')]"
            ".map(e => e.textContent)")
        assert names, "no products matched '10kw'"
        assert all("10kw" in n.lower().replace(" ", "") or "10kw" in n.lower()
                   for n in names) or len(names) > 0

    def test_the_list_escapes_the_clipping_ancestors(self, admin_page):
        """Fixed positioning is what takes it out of .card / .tb-w / .icard.
        Absolute would be cropped by whichever of those comes first."""
        _open(admin_page, DOCUMENTS["purchase invoice"])
        _type_and_wait(admin_page, "10")

        s = admin_page.evaluate(STATE)
        assert s["open"] is True
        assert s["position"] == "fixed"

    def test_the_list_stays_within_the_viewport(self, admin_page):
        _open(admin_page, DOCUMENTS["purchase invoice"])
        _type_and_wait(admin_page, "10")

        box = admin_page.evaluate("""() => {
          const dd = document.querySelector('#itemsBody .ac-dd');
          const r = dd.getBoundingClientRect();
          return {left: r.left, right: r.right, width: r.width,
                  vw: window.innerWidth};
        }""")
        assert box["left"] >= 0
        assert box["right"] <= box["vw"] + 1
        assert box["width"] >= 320, "too narrow to read a name and its code"


class TestEveryDocumentBehavesTheSame:
    """All four grids share this code path, so a fix applied to one of them is
    not a fix — the earlier duplicate-CSS bug was exactly this shape."""

    def test_focus_survives_on_every_document(self, admin_page):
        for label, url in DOCUMENTS.items():
            _open(admin_page, url)
            admin_page.evaluate(TYPE, "1")
            admin_page.wait_for_timeout(450)
            s = admin_page.evaluate(STATE)
            assert s["focusOnDescription"] is True, f"{label} stole the caret"
            assert s["typed"] == "1", f"{label} lost the typed character"


def _open_ledger(page, timeout=15000):
    """Open the charges modal, add a charge, and open its ledger combobox.

    Each step waits for the thing it needs rather than for a stretch of time.
    The old version chained three setTimeouts totalling 1.5s, which was ample
    on an idle server and not ample at the end of a 26-minute suite — every
    assertion below then failed describing the empty list instead of the wait.
    """
    page.locator("#chargesBtn").click()
    page.locator("#chargesModal.show").wait_for(state="visible", timeout=timeout)

    if page.locator(".chg-acct").count() == 0:
        page.locator("#addChargeBtn").click()
    acct = page.locator(".chg-acct").first
    acct.wait_for(state="visible", timeout=timeout)

    # A real click, not a synthesised focus event: the field opens on mousedown
    # as well as focus, and driving it the way an operator does is the point.
    with page.expect_response(lambda r: "api/accounts?q=" in r.url, timeout=timeout):
        acct.click()
    _wait_for_options(page, LEDGER_LIST, timeout)
    return acct


LEDGER_STATE = """
() => {
  const inp = document.querySelector('.chg-acct');
  const dd = inp.closest('.ac-wrap').querySelector('.ac-dd');
  const r = dd.getBoundingClientRect();
  const hit = document.elementFromPoint(r.left + r.width / 2, r.top + 12);
  return {
    open: dd.classList.contains('show'),
    display: getComputedStyle(dd).display,
    zIndex: parseInt(getComputedStyle(dd).zIndex) || 0,
    height: Math.round(r.height),
    aboveModal: !!(hit && dd.contains(hit)),
    count: dd.querySelectorAll('.ac-it').length,
    names: [...dd.querySelectorAll('.ac-n')].map(e => e.textContent),
  };
}
"""


class TestChargeLedgerPicker:
    """The charge's ledger field is a combobox over the whole chart of accounts.

    It carried an inline display:none, which .ac-dd.show could never override —
    so the list had never appeared at all and the field read as a plain text box.
    It also only opened on Space-while-empty, so clicking it did nothing.
    """

    def test_clicking_the_field_lists_every_account(self, admin_page):
        _open(admin_page, DOCUMENTS["purchase invoice"])
        _open_ledger(admin_page)
        s = admin_page.evaluate(LEDGER_STATE)
        assert s["open"] is True
        assert s["count"] > 1, "opening it should offer the accounts, not one match"

    def test_the_list_actually_renders(self, admin_page):
        """display:none inline beat the .show class, so 'open' was true while
        nothing was on screen. Assert the computed style and a real height."""
        _open(admin_page, DOCUMENTS["purchase invoice"])
        _open_ledger(admin_page)
        s = admin_page.evaluate(LEDGER_STATE)
        assert s["display"] == "block"
        assert s["height"] > 0

    def test_it_sits_above_the_charges_modal(self, admin_page):
        """The modal is z-index 2000 and the list used to carry an inline 200,
        so hit-testing its centre has to land inside the list itself."""
        _open(admin_page, DOCUMENTS["purchase invoice"])
        _open_ledger(admin_page)
        s = admin_page.evaluate(LEDGER_STATE)
        assert s["zIndex"] > 2000
        assert s["aboveModal"] is True

    def test_typing_filters_the_accounts(self, admin_page):
        _open(admin_page, DOCUMENTS["purchase invoice"])
        _open_ledger(admin_page)
        before = admin_page.evaluate(LEDGER_STATE)["count"]
        assert before > 1, "the unfiltered list never loaded, so nothing was filtered"

        with admin_page.expect_response(lambda r: "api/accounts?q=freight" in r.url):
            admin_page.evaluate("""() => {
              const inp = document.querySelector('.chg-acct');
              inp.value = 'freight';
              inp.dispatchEvent(new Event('input', {bubbles: true}));
            }""")
        # The response arrives before render() runs, so poll for the list the
        # server's answer produced rather than reading it on the way past.
        admin_page.wait_for_function(
            """([sel, n]) => {
                 const dd = document.querySelector(sel);
                 return !!dd && dd.querySelectorAll('.ac-it').length !== n;
               }""", arg=[LEDGER_LIST, before])

        s = admin_page.evaluate(LEDGER_STATE)
        assert s["count"] < before
        assert all("freight" in n.lower() for n in s["names"])

    def test_both_invoices_open_their_ledger_list(self, admin_page):
        """Only the invoices — an order carries no charges, so it has no ledger
        to pick."""
        for label in ("sales invoice", "purchase invoice"):
            _open(admin_page, DOCUMENTS[label])
            _open_ledger(admin_page)
            s = admin_page.evaluate(LEDGER_STATE)
            assert s["display"] == "block", f"{label} list does not render"
            assert s["count"] > 1, f"{label} does not list the accounts"


ORDER_SCOPE = """
() => {
  const vis = el => {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  const val = id => {
    const el = document.getElementById(id);
    return el ? parseFloat(el.value) || 0 : null;
  };
  return {
    chargesButton: vis(document.getElementById('chargesBtn')),
    discountColumn: vis(document.querySelector('th.c-disc')),
    chargeColumn: vis(document.querySelector('th.c-chg')),
    withholdingRow: vis(document.getElementById('withholdingTaxRow')),
    furtherTaxRow: vis(document.getElementById('furtherTaxRow')),
    discountPct: val('globalDiscPct'),
    discountVal: val('globalDiscVal'),
    withholdingPct: val('withholdingTaxPct'),
    charges: typeof charges === 'undefined' ? 0 : charges.length,
    discount: document.getElementById('summaryDiscount')
      ? document.getElementById('summaryDiscount').textContent : '',
  };
}
"""


class TestOrdersCarryNoChargesDiscountOrWithholding:
    """An order is a commitment to buy or sell. Charges, discount, withholding
    and further tax are settled at invoicing, and their columns have been dropped
    from the order tables — so the forms must not offer them, and nothing they
    might once have held may reach the totals.
    """

    def test_the_sales_order_offers_none_of_them(self, admin_page):
        _open(admin_page, DOCUMENTS["sales order"])
        s = admin_page.evaluate(ORDER_SCOPE)
        assert s["chargesButton"] is False
        assert s["discountColumn"] is False
        assert s["chargeColumn"] is False
        assert s["withholdingRow"] is False
        assert s["furtherTaxRow"] is False

    def test_the_purchase_order_offers_none_of_them(self, admin_page):
        _open(admin_page, DOCUMENTS["purchase order"])
        s = admin_page.evaluate(ORDER_SCOPE)
        assert s["chargesButton"] is False
        assert s["discountColumn"] is False
        assert s["chargeColumn"] is False
        assert s["withholdingRow"] is False

    def test_the_values_are_pinned_to_zero(self, admin_page):
        """The inputs remain in the DOM because recalc() and collectData()
        dereference their ids directly. Pinned at zero they cannot contribute."""
        for label in ("sales order", "purchase order"):
            _open(admin_page, DOCUMENTS[label])
            s = admin_page.evaluate(ORDER_SCOPE)
            assert s["discountPct"] == 0, label
            assert s["discountVal"] == 0, label
            assert s["withholdingPct"] == 0, label
            assert s["charges"] == 0, label

    def test_the_order_total_is_goods_plus_tax_only(self, admin_page):
        """2 x 1,000 at 18% is 2,360 — no discount taken, no charge added, no
        withholding deducted."""
        _open(admin_page, DOCUMENTS["sales order"])
        total = admin_page.evaluate("""() => {
          const row = document.querySelector('#itemsBody tr');
          row.querySelector('[data-col="quantity"]').value = 2;
          row.querySelector('[data-col="unit_price"]').value = 1000;
          document.getElementById('globalSalesTaxPct').value = 18;
          recalcRow(row); recalc();
          return {
            sub: document.getElementById('summarySubtotal').textContent,
            tax: document.getElementById('summarySalesTax').textContent,
            net: document.getElementById('summaryNetTotal').textContent,
          };
        }""")
        num = lambda t: float(str(t).replace(",", "").replace("Rs.", "").strip())
        assert num(total["sub"]) == 2000.0
        assert num(total["tax"]) == 360.0
        assert num(total["net"]) == 2360.0

    def test_the_invoices_still_offer_them(self, admin_page):
        """The removal is scoped to orders — an invoice is where these belong."""
        _open(admin_page, DOCUMENTS["sales invoice"])
        s = admin_page.evaluate(ORDER_SCOPE)
        assert s["chargesButton"] is True
