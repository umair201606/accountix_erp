"""E2E for the invoice design editor.

The point of this screen is that someone who does not write HTML can set up an
invoice. These drive it the way that person would: open it, see a preview, click
a design, untick a field, save.
"""

import os

# Same port the harness starts the server on (tests/e2e/conftest.py).
BASE_URL = "http://localhost:" + os.environ.get("E2E_PORT", "5050")
NEW_SALES = f"{BASE_URL}/settings/templates/create?type=sales"
NEW_PURCHASE = f"{BASE_URL}/settings/templates/create?type=purchase"


def _open(page, url):
    page.goto(url)
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(900)   # let the first preview land
    return page


class TestDesignEditorLoads:
    def test_editor_offers_designs_colours_and_toggles(self, admin_page):
        _open(admin_page, NEW_SALES)
        assert admin_page.locator(".tf-design").count() >= 3
        assert admin_page.locator(".tf-sw").count() >= 4
        assert admin_page.locator(".tf-toggle").count() > 0

    def test_no_html_is_required_to_use_the_screen(self, admin_page):
        """The HTML editor exists but must stay tucked away: it is the escape
        hatch, not the interface."""
        _open(admin_page, NEW_SALES)
        assert not admin_page.locator("#bodyHtml").is_visible()

    def test_preview_renders_a_sample_invoice(self, admin_page):
        _open(admin_page, NEW_SALES)
        frame = admin_page.frame_locator("#tfPreview")
        assert frame.locator("text=SALES INVOICE").count() > 0
        assert frame.locator("text=881,760.00").count() > 0

    def test_preview_is_labelled_as_sample_data(self, admin_page):
        _open(admin_page, NEW_SALES)
        assert "not a real invoice" in admin_page.locator(".tf-sample").inner_text().lower()

    def test_templates_tab_lists_both_document_types(self, admin_page):
        admin_page.goto(f"{BASE_URL}/settings/?tab=templates")
        admin_page.wait_for_load_state("networkidle")
        body = admin_page.locator("body").inner_text().lower()
        assert "sales invoices" in body
        assert "purchase invoices" in body


class TestDesignEditorReacts:
    def test_choosing_a_design_updates_the_form_and_preview(self, admin_page):
        _open(admin_page, NEW_SALES)
        admin_page.click('.tf-design[data-design="bold"]')
        admin_page.wait_for_timeout(1100)
        assert admin_page.input_value("#designField") == "bold"
        assert admin_page.locator('.tf-design[data-design="bold"]').get_attribute("class").find("on") >= 0

    def test_choosing_a_colour_reaches_the_preview(self, admin_page):
        _open(admin_page, NEW_SALES)
        admin_page.click('.tf-sw[data-accent="#6d28d9"]')
        admin_page.wait_for_timeout(1100)
        assert admin_page.input_value("#accentField") == "#6d28d9"
        html = admin_page.frame_locator("#tfPreview").locator("body").inner_html()
        assert "#6d28d9" in html

    def test_unticking_a_field_removes_it_from_the_preview(self, admin_page):
        _open(admin_page, NEW_SALES)
        frame = admin_page.frame_locator("#tfPreview")
        assert frame.locator("text=Authorised Signatory").count() > 0
        admin_page.uncheck('input[name="show_signature"]')
        admin_page.wait_for_timeout(1100)
        assert admin_page.frame_locator("#tfPreview").locator(
            "text=Authorised Signatory").count() == 0

    def test_switching_to_custom_html_reveals_the_editor(self, admin_page):
        _open(admin_page, NEW_SALES)
        admin_page.locator(".tf-adv summary").click()
        admin_page.check("#customToggle")
        admin_page.wait_for_timeout(400)
        assert admin_page.locator("#bodyHtml").is_visible()
        assert admin_page.input_value("#designField") == "custom"


class TestDocumentTypeShapesTheForm:
    def test_sales_offers_delivery_not_freight(self, admin_page):
        _open(admin_page, NEW_SALES)
        assert admin_page.locator('input[name="show_delivery"]').count() == 1
        assert admin_page.locator('input[name="show_freight"]').count() == 0

    def test_purchase_offers_freight_not_delivery(self, admin_page):
        _open(admin_page, NEW_PURCHASE)
        assert admin_page.locator('input[name="show_freight"]').count() == 1
        assert admin_page.locator('input[name="show_delivery"]').count() == 0

    def test_purchase_preview_shows_a_purchase_invoice(self, admin_page):
        _open(admin_page, NEW_PURCHASE)
        frame = admin_page.frame_locator("#tfPreview")
        assert frame.locator("text=PURCHASE INVOICE").count() > 0
        assert frame.locator("text=Net Payable").count() > 0


class TestSaving:
    def test_creating_a_design_lands_back_on_the_list(self, admin_page):
        _open(admin_page, NEW_SALES)
        admin_page.fill('input[name="name"]', "E2E Teal Classic")
        admin_page.click('.tf-design[data-design="minimal"]')
        admin_page.uncheck('input[name="show_notes"]')
        admin_page.click('button[type="submit"]')
        admin_page.wait_for_load_state("networkidle")
        assert "tab=templates" in admin_page.url
        assert "E2E Teal Classic" in admin_page.locator("body").inner_text()

    def test_a_saved_design_can_be_reopened_with_its_choices_intact(self, admin_page):
        _open(admin_page, NEW_SALES)
        admin_page.fill('input[name="name"]', "E2E Bold Purple")
        admin_page.click('.tf-design[data-design="bold"]')
        admin_page.click('.tf-sw[data-accent="#6d28d9"]')
        admin_page.uncheck('input[name="show_signature"]')
        admin_page.click('button[type="submit"]')
        admin_page.wait_for_load_state("networkidle")

        row = admin_page.locator("tr", has_text="E2E Bold Purple")
        row.locator("a", has_text="Edit").first.click()
        admin_page.wait_for_load_state("networkidle")
        assert admin_page.input_value("#designField") == "bold"
        assert admin_page.input_value("#accentField") == "#6d28d9"
        assert not admin_page.is_checked('input[name="show_signature"]')

    def test_a_design_without_a_name_is_not_saved(self, admin_page):
        _open(admin_page, NEW_SALES)
        admin_page.click('button[type="submit"]')
        admin_page.wait_for_load_state("networkidle")
        # The browser's own required-field check keeps us on the form.
        assert "templates/create" in admin_page.url


class TestDesignEditorResponsive:
    def test_editor_on_mobile(self, admin_mobile):
        admin_mobile.goto(NEW_SALES)
        admin_mobile.wait_for_load_state("networkidle")
        assert admin_mobile.locator(".tf-design").first.is_visible()
        assert admin_mobile.locator('input[name="name"]').is_visible()
        assert admin_mobile.viewport_size["width"] == 375
