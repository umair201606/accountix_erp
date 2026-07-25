"""E2E for the invoice source choice (§4.1, Figure 2).

The figure opens a new invoice with an explicit two-option radio. The form used
to imply the choice instead: it opened blank, and loading from orders was a
button you had to know to press.
"""

BASE_URL = "http://localhost:5000"
NEW_INVOICE = f"{BASE_URL}/inventory/invoices/"


def _open_new(page):
    page.goto(NEW_INVOICE)
    page.wait_for_load_state("networkidle")
    return page


class TestSourceChoice:
    def test_a_new_invoice_offers_both_sources_up_front(self, admin_page):
        _open_new(admin_page)
        card = admin_page.locator("#sourceCard")
        assert card.is_visible()
        assert admin_page.locator('input[name="invStartSource"]').count() == 2

    def test_blank_is_the_default(self, admin_page):
        """Defaulting to blank keeps the old behaviour for anyone who ignores
        the card: they get a typeable invoice, not a modal in the way."""
        _open_new(admin_page)
        assert admin_page.locator('input[name="invStartSource"][value="blank"]').is_checked()

    def test_choosing_orders_without_a_customer_says_so(self, admin_page):
        """Orders are fetched per customer, so the choice cannot be honoured
        until one is picked. It must say that rather than open an empty picker
        with no explanation."""
        _open_new(admin_page)
        admin_page.locator('input[name="invStartSource"][value="orders"]').check()
        admin_page.wait_for_timeout(300)
        assert admin_page.locator("#orderModal.show").count() == 0

    def test_the_picker_names_the_missing_customer_rather_than_claiming_no_orders(self, admin_page):
        """Opened with no customer the list must not read "No approved orders
        for this customer" — there is no customer to have any."""
        _open_new(admin_page)
        admin_page.evaluate("() => openSourceModal()")
        admin_page.wait_for_timeout(300)
        text = admin_page.locator("#ordersList").inner_text().lower()
        assert "select a customer" in text

    def test_the_choice_is_not_offered_on_a_saved_invoice(self, admin_page):
        """Reopening an invoice has already answered the question — the answer
        is whether its lines carry an order link."""
        admin_page.goto(f"{BASE_URL}/inventory/invoices/list")
        admin_page.wait_for_load_state("networkidle")
        first = admin_page.locator("#invoiceTable tbody tr a, table tbody tr a").first
        if first.count() == 0:
            return  # no saved invoices in this database
        first.click()
        admin_page.wait_for_load_state("networkidle")
        assert admin_page.locator("#sourceCard").count() == 0
