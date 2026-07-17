"""Invoice design compilation.

The rule these protect: a design plus a set of toggles compiles to HTML that
the print path can fill in, every placeholder gets a value, and a toggle that
is off actually removes the thing from the page.
"""

import re

import pytest

from shared.models.invoice_template import (
    DESIGNS, DESIGN_KEYS, ACCENT_PRESETS, PLACEHOLDER_HELP,
    build_body, default_options, normalise_options, option_groups,
    render_invoice_template, sample_context, InvoiceTemplate)


DOC_TYPES = ("sales", "purchase")


def rendered(design="classic", doc_type="sales", accent="#0f766e", **overrides):
    opts = default_options(doc_type)
    opts.update(overrides)
    return render_invoice_template(build_body(design, doc_type, accent, opts),
                                   sample_context(doc_type, opts=opts))


# ─────────────────────────────────────────────
# Every design compiles to complete HTML
# ─────────────────────────────────────────────

@pytest.mark.parametrize("doc_type", DOC_TYPES)
@pytest.mark.parametrize("design", DESIGN_KEYS)
def test_design_leaves_no_unfilled_placeholders(design, doc_type):
    out = rendered(design, doc_type)
    assert not re.findall(r"\{\{(\w+)\}\}", out), "every token must be substituted"


@pytest.mark.parametrize("doc_type", DOC_TYPES)
@pytest.mark.parametrize("design", DESIGN_KEYS)
def test_design_never_leaks_a_literal_none(design, doc_type):
    """A None slipping into an f-string prints the word 'None' on the invoice."""
    assert "None" not in rendered(design, doc_type)


@pytest.mark.parametrize("doc_type", DOC_TYPES)
@pytest.mark.parametrize("design", DESIGN_KEYS)
def test_design_carries_the_accent_colour(design, doc_type):
    assert "#1d4ed8" in rendered(design, doc_type, accent="#1d4ed8")


def test_unknown_design_falls_back_rather_than_crashing(product=None):
    out = render_invoice_template(
        build_body("nonsense", "sales", "#0f766e", default_options("sales")),
        sample_context("sales"))
    assert "SALES INVOICE" in out


def test_bad_accent_falls_back_to_the_default():
    out = build_body("classic", "sales", "javascript:alert(1)", default_options("sales"))
    assert "javascript" not in out
    assert "#0f766e" in out


# ─────────────────────────────────────────────
# Toggles actually change the page
# ─────────────────────────────────────────────

@pytest.mark.parametrize("key,needle", [
    ("show_party_tax_id", "3520112-8"),
    ("show_signature", "Authorised Signatory"),
    ("show_notes", "Notes"),
    ("show_delivery", "Delivery"),
    ("show_installation", "Installation"),
    ("show_discount", "Discount"),
])
def test_sales_toggle_adds_and_removes(key, needle):
    assert needle in rendered("classic", "sales", **{key: True})
    assert needle not in rendered("classic", "sales", **{key: False})


@pytest.mark.parametrize("key,needle", [
    ("show_commission", "Commission"),
    ("show_freight", "Freight"),
    ("show_loading", "Loading / Unloading"),
    ("show_withholding", "Withholding Tax"),
])
def test_purchase_toggle_adds_and_removes(key, needle):
    assert needle in rendered("classic", "purchase", **{key: True})
    assert needle not in rendered("classic", "purchase", **{key: False})


def test_logo_is_omitted_when_switched_off():
    with_logo = build_body("classic", "sales", "#0f766e",
                           dict(default_options("sales"), show_logo=True))
    without = build_body("classic", "sales", "#0f766e",
                         dict(default_options("sales"), show_logo=False))
    assert "{{company_logo}}" in with_logo
    assert "{{company_logo}}" not in without


def test_the_total_is_always_shown_whatever_is_switched_off():
    """An invoice without its total is not an invoice."""
    off = {k: False for k in default_options("sales")}
    out = render_invoice_template(build_body("classic", "sales", "#0f766e", off),
                                  sample_context("sales"))
    assert "881,760.00" in out
    assert "Net Receivable" in out


# ─────────────────────────────────────────────
# Document type shapes the document
# ─────────────────────────────────────────────

def test_sales_and_purchase_differ_where_they_should():
    sales, purchase = rendered(doc_type="sales"), rendered(doc_type="purchase")
    assert "SALES INVOICE" in sales and "Net Receivable" in sales
    assert "Bill To" in sales
    assert "PURCHASE INVOICE" in purchase and "Net Payable" in purchase
    assert "From" in purchase


def test_charge_lines_do_not_cross_over():
    assert "Commission" not in rendered(doc_type="sales")
    assert "Delivery" not in rendered(doc_type="purchase")


def test_option_groups_offer_the_right_charges_per_type():
    sales_keys = {k for _g, f in option_groups("sales") for k, _l, _h in f}
    purch_keys = {k for _g, f in option_groups("purchase") for k, _l, _h in f}
    assert "show_delivery" in sales_keys and "show_delivery" not in purch_keys
    assert "show_freight" in purch_keys and "show_freight" not in sales_keys


# ─────────────────────────────────────────────
# Options round-trip safely
# ─────────────────────────────────────────────

def test_status_is_off_by_default():
    """Internal approval state is not the customer's business."""
    assert default_options("sales")["show_status"] is False
    assert "Status" not in rendered(doc_type="sales")


def test_normalise_fills_in_options_added_after_a_template_was_saved():
    saved = {"show_logo": False}
    opts = normalise_options("sales", saved)
    assert opts["show_logo"] is False, "an explicit choice survives"
    assert opts["show_notes"] is True, "a new option takes its default"


def test_normalise_survives_junk():
    for junk in (None, "", "not json", "[]", "null", 12):
        assert normalise_options("sales", junk) == default_options("sales")


def test_normalise_drops_unknown_keys():
    assert "bogus" not in normalise_options("sales", {"bogus": True})


# ─────────────────────────────────────────────
# The model
# ─────────────────────────────────────────────

def test_recompile_rebuilds_body_from_the_design(app):
    t = InvoiceTemplate(name="T", type="sales", design="bold",
                        accent_color="#b91c1c", body_html="stale")
    t.set_options(default_options("sales"))
    t.recompile()
    assert t.body_html != "stale"
    assert "#b91c1c" in t.body_html


def test_recompile_never_touches_hand_written_html(app):
    t = InvoiceTemplate(name="T", type="sales", design="custom",
                        body_html="<p>mine {{grand_total}}</p>")
    t.recompile()
    assert t.body_html == "<p>mine {{grand_total}}</p>", \
        "a custom body is the user's own and must survive"


def test_a_template_predating_designs_counts_as_custom(app):
    """Rows that existed before the design column was added have design NULL
    and a hand-written body. Treating them as 'classic' would overwrite that
    body the first time the user opened and saved them."""
    t = InvoiceTemplate(name="Legacy", type="sales", design=None,
                        body_html="<p>hand written</p>")
    assert t.is_custom is True
    t.recompile()
    assert t.body_html == "<p>hand written</p>"


def test_seed_defaults_is_idempotent_and_gives_each_type_a_default(app):
    from shared.extensions import db
    InvoiceTemplate.seed_defaults()
    InvoiceTemplate.seed_defaults()
    for doc_type in DOC_TYPES:
        rows = InvoiceTemplate.query.filter_by(type=doc_type).all()
        assert len(rows) == 2, "seeding twice must not duplicate"
        combined = next(r for r in rows if r.is_default)
        perline = next(r for r in rows if not r.is_default)
        assert combined.name.startswith("Standard")
        assert "Item Wise" in perline.name
        assert InvoiceTemplate.get_default(doc_type) is combined
    db.session.rollback()


def test_seeded_template_renders_a_complete_invoice(app):
    InvoiceTemplate.seed_defaults()
    t = InvoiceTemplate.get_default("sales")
    out = render_invoice_template(t.body_html, sample_context("sales"))
    assert not re.findall(r"\{\{(\w+)\}\}", out)
    assert "881,760.00" in out


# ─────────────────────────────────────────────
# Preview data
# ─────────────────────────────────────────────

def test_sample_context_covers_every_placeholder():
    """A missing key would render as a literal {{token}} on the preview."""
    for doc_type in DOC_TYPES:
        missing = set(PLACEHOLDER_HELP) - set(sample_context(doc_type))
        assert not missing, f"sample data missing {missing}"


def test_sample_context_prefers_the_real_company_profile():
    class FakeCompany:
        company_name = "Acme Solar"
        address = None
        city = None
        phone = None
        email = None
        tax_id = None
        logo_url = None
    ctx = sample_context("sales", FakeCompany())
    assert ctx["company_name"] == "Acme Solar"
    assert ctx["company_address"], "a blank profile field still needs a stand-in"


def test_designs_and_accents_are_well_formed():
    assert len(DESIGNS) >= 3
    for key, label, desc in DESIGNS:
        assert key and label and desc
    for hex_code, name in ACCENT_PRESETS:
        assert re.fullmatch(r"#[0-9a-f]{6}", hex_code), hex_code
