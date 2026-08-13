"""E2E tests for the Sales Return (credit note) flow.

Covers the screens and the API the form is driven by. The costing rules the
return depends on — goods re-entering at the cost they sold at — are asserted
in tests/unit/test_costing.py, which can set up multi-price stock far more
cheaply than driving the UI.
"""

import os

# Same port the harness starts the server on (tests/e2e/conftest.py).
BASE_URL = "http://localhost:" + os.environ.get("E2E_PORT", "5050")


class TestSalesReturnPages:
    def test_return_form_loads(self, admin_page):
        admin_page.goto(f"{BASE_URL}/invoicing/sales-return/")
        admin_page.wait_for_load_state("networkidle")
        assert "sales-return" in admin_page.url
        assert admin_page.locator("#originalInvoiceSelect").is_visible()

    def test_return_list_loads(self, admin_page):
        admin_page.goto(f"{BASE_URL}/invoicing/sales-return/list")
        admin_page.wait_for_load_state("networkidle")
        assert "sales-return" in admin_page.url

    def test_form_prompts_for_an_invoice_before_showing_items(self, admin_page):
        admin_page.goto(f"{BASE_URL}/invoicing/sales-return/")
        admin_page.wait_for_load_state("networkidle")
        assert admin_page.locator("#noInvoiceMsg").is_visible()
        assert admin_page.locator("#itemsBody").inner_html().strip() == ""

    def test_form_shows_the_cost_columns(self, admin_page):
        """The cost basis is the point of the screen: it says what goes back
        into stock, as distinct from what the customer is credited."""
        admin_page.goto(f"{BASE_URL}/invoicing/sales-return/")
        admin_page.wait_for_load_state("networkidle")
        # inner_text() is the RENDERED text, and the header CSS uppercases it.
        headers = admin_page.locator("#itemsTable thead").inner_text().lower()
        assert "cost basis" in headers
        assert "cost back" in headers


class TestSalesReturnNav:
    def test_sales_return_is_linked_from_the_sales_section(self, admin_page):
        admin_page.goto(f"{BASE_URL}/invoicing/")
        admin_page.wait_for_load_state("networkidle")
        assert admin_page.locator("a[href*='sales-return']").count() > 0, \
            "Sales Return must be reachable from the sidebar"

    def test_sales_return_sits_beside_the_other_sales_screens(self, admin_page):
        """Guards the asymmetry this feature closed: Procurement had a return
        and Sales did not."""
        admin_page.goto(f"{BASE_URL}/invoicing/")
        admin_page.wait_for_load_state("networkidle")
        assert admin_page.locator("a[href*='purchase-return']").count() > 0
        assert admin_page.locator("a[href*='sales-return']").count() > 0


class TestSalesReturnApi:
    def test_invoice_list_api_returns_json(self, admin_page):
        r = admin_page.request.get(f"{BASE_URL}/invoicing/sales-return/api/invoices")
        assert r.ok
        assert isinstance(r.json(), list)

    def test_unknown_invoice_is_rejected(self, admin_page):
        r = admin_page.request.get(
            f"{BASE_URL}/invoicing/sales-return/api/invoice/999999")
        assert r.status == 404
        assert r.json()["ok"] is False

    def test_save_without_an_invoice_is_rejected(self, admin_page):
        r = admin_page.request.post(
            f"{BASE_URL}/invoicing/sales-return/save",
            data={"action": "approve", "items": []})
        assert not r.json().get("ok")


class TestSalesReturnResponsive:
    def test_return_form_mobile(self, admin_mobile):
        admin_mobile.goto(f"{BASE_URL}/invoicing/sales-return/")
        admin_mobile.wait_for_load_state("networkidle")
        assert admin_mobile.locator("#originalInvoiceSelect").is_visible()
        # The item grid scrolls inside its own container rather than pushing
        # the page sideways (AGENTS.md §3).
        assert admin_mobile.locator(".tb-w").is_visible()
        assert admin_mobile.viewport_size["width"] == 375

    def test_return_list_mobile(self, admin_mobile):
        admin_mobile.goto(f"{BASE_URL}/invoicing/sales-return/list")
        admin_mobile.wait_for_load_state("networkidle")
        assert admin_mobile.locator("body").inner_text()
        assert admin_mobile.viewport_size["width"] == 375
