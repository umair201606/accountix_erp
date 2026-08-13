"""E2E tests for Fixed Assets Management module."""

import os

# Same port the harness starts the server on (tests/e2e/conftest.py).
BASE_URL = "http://localhost:" + os.environ.get("E2E_PORT", "5050")


class TestFALogin:
    def test_login_page_loads(self, login_page):
        assert login_page.locator("#login").is_visible()
        assert login_page.locator("#password").is_visible()

    def test_login_success(self, admin_page):
        assert "/dashboard/" in admin_page.url


class TestFADashboard:
    def test_fa_dashboard_loads(self, admin_page):
        admin_page.goto(f"{BASE_URL}/fixed-assets/dashboard")
        admin_page.wait_for_load_state("networkidle")
        assert "dashboard" in admin_page.url.lower()

    def test_dashboard_shows_stats(self, admin_page):
        admin_page.goto(f"{BASE_URL}/fixed-assets/dashboard")
        admin_page.wait_for_load_state("networkidle")
        # Case-insensitive: .stat-label is text-transform:uppercase, and
        # inner_text() returns the transformed text. Asserting on the rendered
        # case tested the stylesheet, not the dashboard.
        body = admin_page.locator("body").inner_text().lower()
        assert "total assets" in body


class TestFACategories:
    def test_categories_list_loads(self, admin_page):
        admin_page.goto(f"{BASE_URL}/fixed-assets/categories/")
        admin_page.wait_for_load_state("networkidle")
        assert "categories" in admin_page.url.lower()

    def test_create_category(self, admin_page):
        admin_page.goto(f"{BASE_URL}/fixed-assets/categories/create")
        admin_page.wait_for_load_state("networkidle")
        admin_page.fill("input[name='name']", "E2E Test Category")
        admin_page.fill("textarea[name='description']", "Created during E2E test")
        admin_page.click("button[type='submit']")
        admin_page.wait_for_load_state("networkidle")
        body = admin_page.locator("body").inner_text()
        assert "E2E Test Category" in body


class TestFAAssets:
    def test_assets_list_loads(self, admin_page):
        admin_page.goto(f"{BASE_URL}/fixed-assets/assets/")
        admin_page.wait_for_load_state("networkidle")
        assert "assets" in admin_page.url.lower()

    def test_create_asset(self, admin_page):
        admin_page.goto(f"{BASE_URL}/fixed-assets/assets/create")
        admin_page.wait_for_load_state("networkidle")
        admin_page.fill("input[name='name']", "E2E Test Laptop")
        admin_page.select_option("select[name='category_id']", index=1)
        admin_page.fill("input[name='purchase_date']", "2026-01-15")
        admin_page.fill("input[name='purchase_cost']", "150000")
        admin_page.fill("input[name='useful_life']", "5")
        admin_page.fill("input[name='location']", "Head Office")
        admin_page.fill("input[name='assigned_to']", "IT Department")
        admin_page.fill("input[name='serial_number']", "SN-E2E-001")
        admin_page.click("button[type='submit']")
        admin_page.wait_for_load_state("networkidle")
        body = admin_page.locator("body").inner_text()
        assert "E2E Test Laptop" in body

    def test_view_asset(self, admin_page):
        admin_page.goto(f"{BASE_URL}/fixed-assets/assets/")
        admin_page.wait_for_load_state("networkidle")
        page_text = admin_page.locator("body").inner_text()
        if "E2E Test Laptop" in page_text:
            admin_page.click("text=E2E Test Laptop")
            admin_page.wait_for_load_state("networkidle")
            body = admin_page.locator("body").inner_text()
            assert "Purchase Cost" in body

    def test_record_depreciation(self, admin_page):
        admin_page.goto(f"{BASE_URL}/fixed-assets/assets/")
        admin_page.wait_for_load_state("networkidle")
        page_text = admin_page.locator("body").inner_text()
        if "E2E Test Laptop" in page_text:
            admin_page.click("text=E2E Test Laptop")
            admin_page.wait_for_load_state("networkidle")
            admin_page.fill("input[name='entry_date']", "2026-06-30")
            admin_page.fill("input[name='amount']", "25000")
            admin_page.click("button:has-text('Record')")
            admin_page.wait_for_load_state("networkidle")
            body = admin_page.locator("body").inner_text()
            assert "Depreciation" in body or "25,000" in body or "25000" in body


class TestFAReports:
    def test_reports_page_loads(self, admin_page):
        admin_page.goto(f"{BASE_URL}/fixed-assets/reports/")
        admin_page.wait_for_load_state("networkidle")
        assert "reports" in admin_page.url.lower()

    def test_reports_show_summary(self, admin_page):
        admin_page.goto(f"{BASE_URL}/fixed-assets/reports/")
        admin_page.wait_for_load_state("networkidle")
        body = admin_page.locator("body").inner_text()
        assert "Total Cost" in body


class TestFAResponsive:
    def test_mobile_dashboard(self, admin_mobile):
        admin_mobile.goto(f"{BASE_URL}/fixed-assets/dashboard")
        admin_mobile.wait_for_load_state("networkidle")
        body = admin_mobile.locator("body").inner_text().lower()
        assert "total assets" in body

    def test_mobile_assets_list(self, admin_mobile):
        admin_mobile.goto(f"{BASE_URL}/fixed-assets/assets/")
        admin_mobile.wait_for_load_state("networkidle")
        assert "assets" in admin_mobile.url.lower()

    def test_mobile_create_asset(self, admin_mobile):
        admin_mobile.goto(f"{BASE_URL}/fixed-assets/assets/create")
        admin_mobile.wait_for_load_state("networkidle")
        assert admin_mobile.locator("input[name='name']").is_visible()
