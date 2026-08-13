"""E2E for the §12.2 posting-account maps in admin Invoice Settings.

Two optional tables: product category -> revenue account, and sales-tax rate ->
Output Sales Tax sub-account. The behaviour that matters most is the empty case
— an unmapped system must keep posting exactly one revenue credit and one
output-tax credit, so these tables can be ignored entirely.
"""

import os

# Same port the harness starts the server on (tests/e2e/conftest.py).
BASE_URL = "http://localhost:" + os.environ.get("E2E_PORT", "5050")
INVOICING_SETTINGS = f"{BASE_URL}/settings/?tab=invoicing"


def _open(page):
    page.goto(INVOICING_SETTINGS)
    page.wait_for_load_state("networkidle")
    return page


class TestPostingAccountsUI:
    def test_the_card_renders_with_both_maps(self, admin_page):
        _open(admin_page)
        body = admin_page.locator("body").inner_text().lower()
        assert "posting accounts" in body
        assert "revenue account by product category" in body
        assert "output sales tax sub-account by rate" in body

    def test_both_add_controls_are_present(self, admin_page):
        _open(admin_page)
        assert admin_page.locator('select[name="cra_new_category"]').count() == 1
        assert admin_page.locator('select[name="cra_new_account"]').count() == 1
        assert admin_page.locator('input[name="tra_new_rate"]').count() == 1
        assert admin_page.locator('select[name="tra_new_account"]').count() == 1

    def test_it_says_that_leaving_them_empty_changes_nothing(self, admin_page):
        """The fallback is the safety property of this whole feature, so the
        screen has to state it rather than leave it to be discovered."""
        _open(admin_page)
        body = admin_page.locator("body").inner_text().lower()
        assert "unmapped" in body and "global" in body

    def test_the_page_still_parses_as_balanced_html(self, admin_page):
        """A previous settings change shipped an unbalanced tag that rendered
        two copies of a control, so the tag count is checked directly."""
        _open(admin_page)
        counts = admin_page.evaluate("""() => {
          const html = document.documentElement.outerHTML;
          const open = (html.match(/<div\\b/g) || []).length;
          const close = (html.match(/<\\/div>/g) || []).length;
          return {open, close};
        }""")
        assert counts["open"] == counts["close"], counts


class TestTaxRateMapRoundTrip:
    def test_a_rate_can_be_added_and_then_removed(self, admin_page):
        _open(admin_page)
        # Asserted, not guarded: the chart of accounts always seeds level-5
        # liability ledgers, so an empty picker is a broken filter — not a
        # reason for this test to quietly pass without testing anything.
        options = admin_page.locator('select[name="tra_new_account"] option').count()
        assert options > 1, "no postable liability ledgers offered"

        admin_page.locator('input[name="tra_new_rate"]').fill("17.5")
        admin_page.locator('select[name="tra_new_account"]').select_option(index=1)
        admin_page.locator('button[type="submit"]:has-text("Save Invoice Settings")').click()
        admin_page.wait_for_load_state("networkidle")

        assert "17.5%" in admin_page.locator("body").inner_text()

        # Remove it again so the suite leaves no state behind.
        row_delete = admin_page.locator('input[name^="tra_delete_"]').first
        row_delete.check()
        admin_page.locator('button[type="submit"]:has-text("Save Invoice Settings")').click()
        admin_page.wait_for_load_state("networkidle")
        assert "17.5%" not in admin_page.locator("body").inner_text()
