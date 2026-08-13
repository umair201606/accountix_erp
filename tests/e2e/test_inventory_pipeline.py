"""E2E tests for Inventory app pipeline — products, suppliers, purchase invoice, purchase return."""

import re
import os

# Same port the harness starts the server on (tests/e2e/conftest.py).
BASE_URL = "http://localhost:" + os.environ.get("E2E_PORT", "5050")


class TestInvLogin:
    def test_login_page_loads(self, login_page):
        assert login_page.locator("#login").is_visible()
        assert login_page.locator("#password").is_visible()

    def test_login_success(self, admin_page):
        assert "/dashboard/" in admin_page.url


class TestInvDashboard:
    def test_inventory_dashboard_loads(self, admin_page):
        admin_page.goto(f"{BASE_URL}/inventory/dashboard")
        admin_page.wait_for_load_state("networkidle")
        assert "inventory" in admin_page.url.lower() or "dashboard" in admin_page.url


class TestInvProducts:
    def test_products_list_loads(self, admin_page):
        admin_page.goto(f"{BASE_URL}/inventory/products/")
        admin_page.wait_for_load_state("networkidle")
        assert "product" in admin_page.url.lower()


class TestInvSuppliers:
    def test_suppliers_list_loads(self, admin_page):
        admin_page.goto(f"{BASE_URL}/inventory/suppliers/")
        admin_page.wait_for_load_state("networkidle")
        assert "supplier" in admin_page.url.lower()


class TestInvCustomers:
    def test_customers_list_loads(self, admin_page):
        admin_page.goto(f"{BASE_URL}/inventory/customers/")
        admin_page.wait_for_load_state("networkidle")
        assert "customer" in admin_page.url.lower()


class TestInvPurchaseInvoice:
    def test_new_purchase_invoice_form_loads(self, admin_page):
        admin_page.goto(f"{BASE_URL}/inventory/purchase-invoice/")
        admin_page.wait_for_load_state("networkidle")
        admin_page.locator("#itemsBody tr").wait_for(state="attached", timeout=5000)
        assert admin_page.locator("#supplierSearch").is_visible()
        assert admin_page.locator("#itemsBody").is_visible()
        assert admin_page.locator("#addLineBtn").is_visible()
        assert admin_page.locator("#clearAllBtn").is_visible()
        assert admin_page.locator("#saveBtn").is_visible()

    def test_purchase_invoice_add_line(self, admin_page):
        admin_page.goto(f"{BASE_URL}/inventory/purchase-invoice/")
        admin_page.wait_for_load_state("networkidle")
        init_rows = admin_page.locator("#itemsBody tr").count()
        admin_page.locator("#addLineBtn").click()
        admin_page.wait_for_timeout(200)
        new_rows = admin_page.locator("#itemsBody tr").count()
        assert new_rows > init_rows, "Add Line button should add a row"

    def test_purchase_invoice_clear_lines(self, admin_page):
        admin_page.goto(f"{BASE_URL}/inventory/purchase-invoice/")
        admin_page.wait_for_load_state("networkidle")
        admin_page.locator("#itemsBody tr").wait_for(state="attached", timeout=5000)
        admin_page.locator("#clearAllBtn").click()
        admin_page.locator("#confirmOkBtn").wait_for(state="visible", timeout=3000)
        admin_page.locator("#confirmOkBtn").click()
        admin_page.wait_for_timeout(300)
        remaining = admin_page.locator("#itemsBody tr").count()
        assert remaining == 1, "Should leave exactly 1 blank row after clear"

    def test_purchase_invoice_pill_toggles(self, admin_page):
        admin_page.goto(f"{BASE_URL}/inventory/purchase-invoice/")
        admin_page.wait_for_load_state("networkidle")
        # The scope pills now live in the Additional Settings slide-over, which
        # starts closed. A hidden pill still reports is_enabled(), so the guard
        # below would pass and the click would then time out on visibility.
        admin_page.locator(".bsm-settings").click()
        pill_combined = admin_page.locator("#discountMode .pill-b").first
        pill_combined.wait_for(state="visible")
        if pill_combined.is_enabled():
            pill_combined.click()
            admin_page.wait_for_timeout(100)
            assert "active" in (pill_combined.get_attribute("class") or "")

    def test_purchase_invoice_summary_calculates(self, admin_page):
        admin_page.goto(f"{BASE_URL}/inventory/purchase-invoice/")
        admin_page.wait_for_load_state("networkidle")
        qty = admin_page.locator("#itemsBody [data-col='quantity']").first
        if qty.is_visible():
            qty.fill("10")
            admin_page.wait_for_timeout(300)
            rate = admin_page.locator("#itemsBody [data-col='unit_price']").first
            rate.fill("100")
            admin_page.wait_for_timeout(300)
            subtotal = admin_page.locator("#summarySubtotal")
            assert subtotal.is_visible()

    def test_purchase_invoice_global_inputs(self, admin_page):
        admin_page.goto(f"{BASE_URL}/inventory/purchase-invoice/")
        admin_page.wait_for_load_state("networkidle")
        disc = admin_page.locator("#globalDiscPct")
        if disc.is_visible():
            disc.fill("5")
            admin_page.wait_for_timeout(200)
            disc_val = admin_page.locator("#globalDiscVal")
            if disc_val:
                net = admin_page.locator("#summaryNetPayable")
                assert net.is_visible()


class TestInvPurchaseReturn:
    def test_purchase_return_form_loads(self, admin_page):
        admin_page.goto(f"{BASE_URL}/inventory/purchase-return/")
        admin_page.wait_for_load_state("networkidle")
        assert "return" in admin_page.url.lower()


class TestInvResponsive:
    def test_pi_form_mobile(self, admin_mobile):
        admin_mobile.goto(f"{BASE_URL}/inventory/purchase-invoice/")
        admin_mobile.wait_for_load_state("networkidle")
        assert admin_mobile.locator("#supplierSearch").is_visible()
        assert admin_mobile.locator(".tb-w").is_visible()
        assert admin_mobile.viewport_size["width"] == 375

    def test_products_list_mobile(self, admin_mobile):
        admin_mobile.goto(f"{BASE_URL}/inventory/products/")
        admin_mobile.wait_for_load_state("networkidle")
        assert admin_mobile.locator("body").inner_text()
        assert admin_mobile.viewport_size["width"] == 375

    def test_dashboard_mobile(self, admin_mobile):
        admin_mobile.goto(f"{BASE_URL}/inventory/dashboard")
        admin_mobile.wait_for_load_state("networkidle")
        assert admin_mobile.viewport_size["width"] == 375


class TestInvLogout:
    def test_logout(self, admin_page):
        admin_page.goto(f"{BASE_URL}/auth/logout")
        admin_page.wait_for_load_state("networkidle")
        assert "/auth/login" in admin_page.url
