"""Two people on one document, in the browser.

- A tab holding an older copy of a draft invoice is told, in the form, that
  someone else changed it — and its save overwrites nothing
  (static/js/edit_version.js + shared/concurrency.py: stale_edit). The same
  tab saving twice in a row is never refused: each answer carries the new
  version back to the page.
- A double-clicked form submits once (static/js/app.js submit guard).

The server-side races (two users at the same instant) are in tests/concurrency.
"""
import os
import uuid

from tests.e2e.test_invoice_form_flow import _open_new, _seed_line

BASE_URL = "http://127.0.0.1:" + os.environ.get("E2E_PORT", "5050")


def _save_draft(page):
    page.keyboard.press("Control+s")
    page.wait_for_function("() => document.getElementById('invoiceId').value !== ''")
    page.wait_for_function("() => window.formDirty === false")


def _qty(page):
    return page.locator('#itemsBody [data-col="quantity"]').first


def test_a_stale_tab_is_told_and_overwrites_nothing(admin_page):
    a = admin_page
    _open_new(a)
    _seed_line(a)
    _save_draft(a)
    _qty(a).fill("2")
    _save_draft(a)                       # the same tab saving again is fine
    inv_id = a.evaluate("document.getElementById('invoiceId').value")

    b = a.context.new_page()             # a colleague opens the same draft
    try:
        b.goto(f"{BASE_URL}/inventory/invoices/{inv_id}")
        b.wait_for_load_state("networkidle")
        _qty(b).fill("7")
        _save_draft(b)

        _qty(a).fill("3")                # A still holds the copy before B's save
        a.keyboard.press("Control+s")
        a.wait_for_selector("#toastContainer >> text=after you opened it", timeout=10000)
        assert a.evaluate("window.formDirty") is True, "A's edit must stay unsaved"

        a.reload()
        a.wait_for_load_state("networkidle")
        assert _qty(a).input_value() in ("7", "7.0", "7.00"), "B's edit was overwritten"
    finally:
        b.close()


def test_a_double_clicked_form_submits_once(admin_page):
    """The guard's decisions, read off the submit events themselves: the
    first submit goes through untouched, a second while it is in flight is
    cancelled, and a form that saves with fetch (its own handler cancels the
    submit) is never marked. (Whether a browser's own double submit reaches
    the server twice depends on network timing a test cannot reproduce.)"""
    page = admin_page
    page.goto(f"{BASE_URL}/inventory/customers/create")
    page.wait_for_load_state("networkidle")
    page.fill("input[name='name']", f"Double Click {uuid.uuid4().hex[:6]}")
    seen = page.evaluate("""() => {
        const f = document.querySelector("input[name='name']").form;
        const seen = [];
        // Registered after app.js, so it runs after the guard: record what
        // the guard decided, then stop the navigation the test does not need.
        window.addEventListener('submit', (e) => {
            seen.push([e.target === f ? 'form' : 'fetch-form', e.defaultPrevented]);
            e.preventDefault();
        });
        f.requestSubmit();
        f.requestSubmit();
        const g = document.createElement('form');
        g.method = 'post';
        g.addEventListener('submit', (e) => e.preventDefault());   // saves with fetch
        document.body.appendChild(g);
        g.requestSubmit();
        g.requestSubmit();
        return {seen, marked: f.dataset.submitting || null, fetchMarked: g.dataset.submitting || null};
    }""")
    assert seen["seen"] == [["form", False], ["form", True],
                            ["fetch-form", True], ["fetch-form", True]], seen
    assert seen["marked"] == "1" and seen["fetchMarked"] is None, seen
