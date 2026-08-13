"""Browser coverage for the Accounting voucher register and entry screen."""

import os

# Same port the harness starts the server on (tests/e2e/conftest.py).
BASE_URL = "http://localhost:" + os.environ.get("E2E_PORT", "5050")


def _super_admin_page(page, flask_server):
    page.goto(f"{BASE_URL}/superadmin/login")
    page.fill("#login", "admin@gmail.com")
    page.fill("#password", "admin123")
    page.click("button[type='submit']")
    page.wait_for_url("**/superadmin/**")
    return page


def test_voucher_register_has_clear_filters_and_empty_state(page, flask_server):
    page = _super_admin_page(page, flask_server)
    page.goto(f"{BASE_URL}/accounting/vouchers/list")

    assert page.get_by_role("heading", name="Vouchers", exact=True).is_visible()
    assert page.get_by_role("region", name="Filter vouchers").is_visible()
    assert page.get_by_label("Voucher type", exact=True).is_visible()
    assert page.get_by_label("Status", exact=True).is_visible()
    assert page.get_by_role("region", name="Voucher register").is_visible()
    assert page.get_by_text("No vouchers found", exact=True).is_visible()
    assert page.get_by_role("link", name="+ New Voucher", exact=True).is_visible()


def test_new_voucher_screen_exposes_type_entry_and_save_flow(page, flask_server):
    page = _super_admin_page(page, flask_server)
    page.goto(f"{BASE_URL}/accounting/vouchers")

    assert page.locator('[aria-label="Voucher workflow"]').count() == 0
    assert page.get_by_role("button", name="CPV Cash Payment", exact=True).is_visible()
    assert page.get_by_role("button", name="JV Journal", exact=True).is_visible()
    assert page.get_by_role("textbox", name="Cash Account").is_visible()
    assert page.get_by_role("button", name="+ Add line", exact=True).is_visible()
    assert page.get_by_role("button", name="Save & Approve", exact=True).is_visible()


def test_voucher_register_fits_a_mobile_viewport(page, flask_server):
    page.set_viewport_size({"width": 375, "height": 812})
    page = _super_admin_page(page, flask_server)
    page.goto(f"{BASE_URL}/accounting/vouchers/list")

    assert page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth")
    assert page.locator(".voucher-table thead").is_hidden()
    assert page.get_by_role("button", name="Filter", exact=True).is_visible()


def test_new_voucher_entries_layout_fits_mobile(page, flask_server):
    page.set_viewport_size({"width": 375, "height": 812})
    page = _super_admin_page(page, flask_server)
    page.goto(f"{BASE_URL}/accounting/vouchers")

    assert page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth")
    assert page.locator(".vtb thead").is_hidden()
    assert page.get_by_role("textbox", name="Account search").is_visible()
    assert page.get_by_role("textbox", name="Description").first.is_visible()
    assert page.get_by_label("Amount").first.is_visible()


def test_coa_tree_mobile_layout_and_toggle_aria(page, flask_server):
    page.set_viewport_size({"width": 375, "height": 812})
    page = _super_admin_page(page, flask_server)
    page.goto(f"{BASE_URL}/accounting/coa/")

    assert page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth")
    assert page.locator(".tree-header").is_hidden()
    assert page.get_by_label("Search accounts").is_visible()

    toggles = page.locator(".tree-row button.tw:not(.leafpad)")
    assert toggles.count() > 0
    first = toggles.first
    assert first.get_attribute("aria-expanded") is not None
    before = first.get_attribute("aria-expanded")
    first.click()
    after = first.get_attribute("aria-expanded")
    assert after != before, "toggle must flip aria-expanded"
