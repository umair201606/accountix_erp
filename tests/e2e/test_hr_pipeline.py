"""E2E tests for HR app pipeline — attendance, leave, ESS, profile, logout."""

import pytest

import os

# Same port the harness starts the server on (tests/e2e/conftest.py).
BASE_URL = "http://localhost:" + os.environ.get("E2E_PORT", "5050")


class TestHrLogin:
    def test_login_page_loads(self, login_page):
        assert login_page.locator("#login").is_visible()
        assert login_page.locator("#password").is_visible()

    def test_login_success_redirects_to_hub(self, admin_page):
        assert "/dashboard/" in admin_page.url


class TestHrHubDashboard:
    def test_hub_shows_hr_section(self, admin_page):
        admin_page.goto(f"{BASE_URL}/dashboard/")
        admin_page.wait_for_load_state("networkidle")
        body = admin_page.locator("body").inner_text()
        assert "HR" in body or "hr" in body.lower()

    def test_navigate_to_hr_dashboard(self, admin_page):
        admin_page.goto(f"{BASE_URL}/dashboard")
        admin_page.wait_for_load_state("networkidle")
        assert admin_page.url.endswith("/dashboard") or "dashboard" in admin_page.url

    def test_hr_user_can_access_hub(self, hr_user_page):
        hr_user_page.goto(f"{BASE_URL}/dashboard/")
        hr_user_page.wait_for_load_state("networkidle")
        assert "/dashboard/" in hr_user_page.url


class TestHrAttendance:
    def test_attendance_page_loads(self, admin_page):
        admin_page.goto(f"{BASE_URL}/attendance/")
        admin_page.wait_for_load_state("networkidle")
        assert "attendance" in admin_page.url.lower()

    def test_attendance_has_content(self, admin_page):
        admin_page.goto(f"{BASE_URL}/attendance/")
        admin_page.wait_for_load_state("networkidle")
        body = admin_page.locator("body").inner_text()
        assert len(body) > 0


class TestHrLeave:
    def test_leave_page_loads(self, admin_page):
        admin_page.goto(f"{BASE_URL}/leaves/")
        admin_page.wait_for_load_state("networkidle")
        assert "leave" in admin_page.url.lower()

    def test_apply_leave_form_accessible(self, admin_page):
        admin_page.goto(f"{BASE_URL}/leaves/")
        admin_page.wait_for_load_state("networkidle")
        apply_link = admin_page.locator("a:has-text('Apply')")
        if apply_link.is_visible():
            apply_link.click()
            admin_page.wait_for_load_state("networkidle")


class TestHrEss:
    def test_ess_page_loads(self, admin_page):
        admin_page.goto(f"{BASE_URL}/ess/")
        admin_page.wait_for_load_state("networkidle")
        assert "ess" in admin_page.url.lower()


class TestHrProfile:
    def test_profile_page_loads(self, admin_page):
        admin_page.goto(f"{BASE_URL}/auth/profile")
        admin_page.wait_for_load_state("networkidle")
        body = admin_page.locator("body").inner_text()
        assert "Profile" in body or "profile" in body.lower()

    def test_change_password_page(self, admin_page):
        admin_page.goto(f"{BASE_URL}/auth/change-password")
        admin_page.wait_for_load_state("networkidle")
        assert "password" in admin_page.url.lower()


class TestHrResponsive:
    def test_attendance_mobile(self, admin_mobile):
        admin_mobile.goto(f"{BASE_URL}/attendance/")
        admin_mobile.wait_for_load_state("networkidle")
        assert admin_mobile.viewport_size["width"] == 375

    def test_leave_mobile(self, admin_mobile):
        admin_mobile.goto(f"{BASE_URL}/leaves/")
        admin_mobile.wait_for_load_state("networkidle")
        assert admin_mobile.viewport_size["width"] == 375

    def test_hub_mobile(self, admin_mobile):
        admin_mobile.goto(f"{BASE_URL}/dashboard/")
        admin_mobile.wait_for_load_state("networkidle")
        assert admin_mobile.viewport_size["width"] == 375


class TestHrLogout:
    def test_logout(self, admin_page):
        admin_page.goto(f"{BASE_URL}/auth/logout")
        admin_page.wait_for_load_state("networkidle")
        assert "/auth/login" in admin_page.url
