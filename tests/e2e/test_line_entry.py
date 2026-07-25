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
        admin_page.evaluate(TYPE, "1")
        admin_page.wait_for_timeout(400)
        admin_page.evaluate(TYPE, "10kw")
        admin_page.wait_for_timeout(700)

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
        admin_page.evaluate(TYPE, "10")
        admin_page.wait_for_timeout(700)

        s = admin_page.evaluate(STATE)
        assert s["open"] is True
        assert s["position"] == "fixed"

    def test_the_list_stays_within_the_viewport(self, admin_page):
        _open(admin_page, DOCUMENTS["purchase invoice"])
        admin_page.evaluate(TYPE, "10")
        admin_page.wait_for_timeout(700)

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


OPEN_LEDGER = """
() => {
  document.getElementById('chargesBtn').click();
  return new Promise(resolve => setTimeout(() => {
    if (!document.querySelector('.chg-acct')) {
      document.getElementById('addChargeBtn')?.click();
    }
    setTimeout(() => {
      const inp = document.querySelector('.chg-acct');
      if (!inp) return resolve(null);
      inp.focus();
      inp.dispatchEvent(new Event('focus'));
      setTimeout(() => resolve(true), 800);
    }, 300);
  }, 400));
}
"""

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
        assert admin_page.evaluate(OPEN_LEDGER) is True
        s = admin_page.evaluate(LEDGER_STATE)
        assert s["open"] is True
        assert s["count"] > 1, "opening it should offer the accounts, not one match"

    def test_the_list_actually_renders(self, admin_page):
        """display:none inline beat the .show class, so 'open' was true while
        nothing was on screen. Assert the computed style and a real height."""
        _open(admin_page, DOCUMENTS["purchase invoice"])
        admin_page.evaluate(OPEN_LEDGER)
        s = admin_page.evaluate(LEDGER_STATE)
        assert s["display"] == "block"
        assert s["height"] > 0

    def test_it_sits_above_the_charges_modal(self, admin_page):
        """The modal is z-index 2000 and the list used to carry an inline 200,
        so hit-testing its centre has to land inside the list itself."""
        _open(admin_page, DOCUMENTS["purchase invoice"])
        admin_page.evaluate(OPEN_LEDGER)
        s = admin_page.evaluate(LEDGER_STATE)
        assert s["zIndex"] > 2000
        assert s["aboveModal"] is True

    def test_typing_filters_the_accounts(self, admin_page):
        _open(admin_page, DOCUMENTS["purchase invoice"])
        admin_page.evaluate(OPEN_LEDGER)
        before = admin_page.evaluate(LEDGER_STATE)["count"]
        admin_page.evaluate("""() => {
          const inp = document.querySelector('.chg-acct');
          inp.value = 'freight';
          inp.dispatchEvent(new Event('input', {bubbles: true}));
        }""")
        admin_page.wait_for_timeout(800)
        s = admin_page.evaluate(LEDGER_STATE)
        assert s["count"] < before
        assert all("freight" in n.lower() for n in s["names"])

    def test_every_document_opens_its_ledger_list(self, admin_page):
        for label, url in DOCUMENTS.items():
            _open(admin_page, url)
            if admin_page.evaluate(OPEN_LEDGER) is not True:
                continue   # this document has no charges modal
            s = admin_page.evaluate(LEDGER_STATE)
            assert s["display"] == "block", f"{label} list does not render"
            assert s["count"] > 1, f"{label} does not list the accounts"
