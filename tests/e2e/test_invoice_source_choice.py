"""E2E for the invoice source gate (§4.1, Figure 2).

A new invoice must decide where its lines come from *before* the form is worked
in — not by pressing a button on an already-blank invoice, and not by a control
sitting inside the form. The gate is the first thing the screen presents.
"""

import os

# Same port the harness starts the server on (tests/e2e/conftest.py).
BASE_URL = "http://localhost:" + os.environ.get("E2E_PORT", "5050")
NEW_INVOICE = f"{BASE_URL}/inventory/invoices/"


def _open_new(page):
    page.goto(NEW_INVOICE)
    page.wait_for_load_state("networkidle")
    return page


class TestSourceGate:
    def test_a_new_invoice_opens_on_the_gate(self, admin_page):
        _open_new(admin_page)
        assert admin_page.locator("#sourceGate.show").count() == 1
        assert admin_page.locator("#gateBlank").is_visible()
        assert admin_page.locator("#gateOrders").is_visible()

    def test_the_gate_covers_the_form_until_it_is_answered(self, admin_page):
        """It is a decision, not a suggestion: the form behind it must not be
        reachable while the question is open."""
        _open_new(admin_page)
        # A click aimed at the grid lands on the gate, not on the row beneath.
        blocked = admin_page.evaluate("""() => {
          const cell = document.querySelector('#itemsBody [data-col="quantity"]');
          if (!cell) return 'no cell';
          const r = cell.getBoundingClientRect();
          const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
          return hit && hit.closest('#sourceGate') ? 'gate' : 'form';
        }""")
        assert blocked == "gate"

    def test_choosing_blank_dismisses_the_gate_and_frees_the_form(self, admin_page):
        _open_new(admin_page)
        admin_page.locator("#gateBlank").click()
        admin_page.wait_for_timeout(200)
        assert admin_page.locator("#sourceGate.show").count() == 0
        qty = admin_page.locator('#itemsBody [data-col="quantity"]').first
        qty.fill("3")
        assert qty.input_value() == "3"

    def test_choosing_blank_leaves_no_order_link(self, admin_page):
        """A blank invoice must carry no order reference — the Order ref column
        stays hidden and nothing is written back to an order on approve."""
        _open_new(admin_page)
        admin_page.locator("#gateBlank").click()
        admin_page.wait_for_timeout(200)
        assert admin_page.evaluate("() => loadedOrderIds.length") == 0

    def test_choosing_orders_opens_the_picker(self, admin_page):
        _open_new(admin_page)
        admin_page.locator("#gateOrders").click()
        admin_page.wait_for_timeout(400)
        assert admin_page.locator("#sourceGate.show").count() == 0
        assert admin_page.locator("#orderModal.show").count() == 1

    def test_the_picker_names_the_missing_customer_rather_than_claiming_no_orders(self, admin_page):
        """Reached with no customer chosen, the list must not read "No approved
        orders for this customer" — there is no customer to have any."""
        _open_new(admin_page)
        admin_page.locator("#gateOrders").click()
        admin_page.wait_for_timeout(400)
        text = admin_page.locator("#ordersList").inner_text().lower()
        assert "select a customer" in text

    def test_the_gate_is_not_shown_on_a_saved_invoice(self, admin_page):
        """Reopening has already answered the question — the answer is whether
        the lines carry an order link."""
        admin_page.goto(f"{BASE_URL}/inventory/invoices/list")
        admin_page.wait_for_load_state("networkidle")
        first = admin_page.locator("table tbody tr a").first
        if first.count() == 0:
            return  # no saved invoices in this database
        first.click()
        admin_page.wait_for_load_state("networkidle")
        assert admin_page.locator("#sourceGate").count() == 0
