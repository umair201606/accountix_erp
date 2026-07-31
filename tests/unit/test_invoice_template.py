"""Invoice design compilation.

The rule these protect: a design plus a set of toggles compiles to HTML that
the print path can fill in, every placeholder gets a value, and a toggle that
is off actually removes the thing from the page.
"""

import re

import pytest

from shared.models.invoice_template import (
    DESIGNS, DESIGN_KEYS, ACCENT_PRESETS, PLACEHOLDER_HELP, SHEET_MARKER,
    build_body, build_totals_table, default_options, items_table_metrics,
    normalise_options, option_groups, render_invoice_template, sample_context,
    InvoiceTemplate)


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


# ─────────────────────────────────────────────
# The printed page is A4
# ─────────────────────────────────────────────

@pytest.mark.parametrize("doc_type", DOC_TYPES)
@pytest.mark.parametrize("design", DESIGN_KEYS)
def test_every_design_is_an_a4_sheet(design, doc_type):
    """Sales and purchase, every design: one page geometry, no exceptions."""
    out = rendered(design, doc_type)
    assert "@page{size:A4;margin:14mm}" in out
    assert "width:210mm;min-height:297mm" in out
    assert f'class="inv-sheet {SHEET_MARKER}"' in out


@pytest.mark.parametrize("doc_type", DOC_TYPES)
def test_the_items_table_never_uses_a_fixed_layout(doc_type):
    """A fixed layout apportions width by column count instead of by content,
    which is what clipped values and split numbers across two lines."""
    for mode in ("combined", "per_line"):
        out = rendered("classic", doc_type, discount_display=mode,
                       tax_display=mode, charges_display=mode)
        assert "table-layout:fixed" not in out


@pytest.mark.parametrize("doc_type", DOC_TYPES)
def test_only_the_description_column_may_wrap(doc_type):
    """Everything else is nowrap, so an amount never breaks across two lines."""
    out = rendered("classic", doc_type)
    body = out[out.index('<div class="inv-sheet'):]
    table = re.search(r'<table class="inv-items".*?</table>', body, re.S).group(0)
    header = re.search(r"<thead>.*?</thead>", table, re.S).group(0)
    # Exactly one marked description cell in every row: the header, each item,
    # and the totals row. Any row that missed it would wrap its numbers.
    assert header.count("inv-desc") == 1
    assert table.count("inv-desc") == len(re.findall(r"<tr[ >]", table))


@pytest.mark.parametrize("doc_type", DOC_TYPES)
def test_extra_columns_shrink_the_type_rather_than_overflow(doc_type):
    """Per-line mode adds six or seven columns; they have to be made to fit."""
    combined = rendered("classic", doc_type, discount_display="combined",
                        tax_display="combined", charges_display="combined")
    perline = rendered("classic", doc_type, discount_display="per_line",
                       tax_display="per_line", charges_display="per_line")

    def size(html):
        table = re.search(r'<table class="inv-items" style="([^"]+)"', html).group(1)
        return float(re.search(r"font-size:([\d.]+)px", table).group(1))

    assert size(perline) < size(combined)


def test_the_type_scale_is_monotonic_in_the_column_count():
    sizes = [items_table_metrics(n)["font"] for n in range(10, 20)]
    assert sizes == sorted(sizes, reverse=True)
    assert sizes[0] == 11.0, "a plain invoice is not shrunk at all"
    assert min(sizes) >= 7.0, "the starting size stays legible; auto-fit takes it further"


@pytest.mark.parametrize("doc_type", DOC_TYPES)
def test_the_totals_block_stands_up_without_the_sheet_stylesheet(doc_type):
    """A hand-written template gets {{totals_table}} substituted into HTML that
    carries none of the sheet's CSS. If the layout lives only in those classes
    the table shrinks to its contents and the summary drops to the left."""
    html = build_totals_table(doc_type, default_options(doc_type), "#0f766e")
    outer = html[:html.index(">") + 1]
    assert "width:100%" in outer, "outer table must set its own width"
    assert "border-collapse:collapse" in outer
    box = html[html.index('class="inv-totals-box"'):]
    assert box[:120].count("width:72mm") == 1, "summary keeps its width inline"


@pytest.mark.parametrize("doc_type", DOC_TYPES)
def test_the_items_table_stands_up_without_the_sheet_stylesheet(doc_type):
    out = rendered("classic", doc_type)
    body = out[out.index('<div class="inv-sheet'):]
    table = re.search(r'<table class="inv-items" style="([^"]+)"', body).group(1)
    assert "width:100%" in table
    assert "border-collapse:collapse" in table
    # Numbers must not wrap even with no stylesheet backing the table.
    row = re.search(r"<tbody>.*?</tr>", body, re.S).group(0)
    money = re.findall(r"<td style='([^']*)'>[\d,]+\.\d\d</td>", row)
    assert money, "expected money cells"
    assert all("white-space:nowrap" in c for c in money)


@pytest.mark.parametrize("doc_type", DOC_TYPES)
def test_the_summary_is_pinned_to_the_right_at_a_fixed_width(doc_type):
    """Adding item columns must not move the totals block."""
    narrow = rendered("classic", doc_type)
    wide = rendered("classic", doc_type, discount_display="per_line",
                    tax_display="per_line", charges_display="per_line")
    for out in (narrow, wide):
        body = out[out.index('<div class="inv-sheet'):]
        assert body.count('class="inv-totals-box"') == 1
    assert "td.inv-totals-box{width:72mm}" in narrow


@pytest.mark.parametrize("doc_type", DOC_TYPES)
@pytest.mark.parametrize("design", DESIGN_KEYS)
def test_signature_and_closing_line_sit_at_the_foot_of_the_page(design, doc_type):
    out = rendered(design, doc_type)
    body = out[out.index('<div class="inv-sheet'):]
    assert '<div class="inv-foot">' in body
    foot = body[body.index('<div class="inv-foot">'):]
    assert "Signatory" in foot or "Approved By" in foot
    assert "Thank you" in foot or "internal record" in foot
    assert ".inv-sheet>.inv-foot{margin-top:auto" in out


@pytest.mark.parametrize("doc_type", DOC_TYPES)
def test_notes_stay_with_the_content_not_in_the_foot(doc_type):
    """Notes read as part of the invoice; only the signing block is anchored."""
    out = rendered("classic", doc_type)
    body = out[out.index('<div class="inv-sheet'):]
    assert body.index("Notes") < body.index('<div class="inv-foot">')


@pytest.mark.parametrize("doc_type", DOC_TYPES)
@pytest.mark.parametrize("design", DESIGN_KEYS)
def test_every_page_gets_a_number_in_the_bottom_right(design, doc_type):
    out = rendered(design, doc_type)
    assert '<div class="inv-pagenums"' in out
    assert ".inv-pageno{position:absolute;right:0" in out
    assert "'Page ' + (i + 1) + ' of ' + pages" in out


# ─────────────────────────────────────────────
# Bank details opposite the summary
# ─────────────────────────────────────────────

def test_sales_invoice_prints_bank_details_opposite_the_totals():
    out = rendered("classic", "sales", show_bank_details=True)
    body = out[out.index('<div class="inv-sheet'):]
    gap = body[body.index('class="inv-totals-gap"'):body.index('class="inv-totals-box"')]
    assert "Payment Details" in gap
    assert "Meezan Bank Ltd" in gap, "bank name"
    assert "0102-0101234567-01" in gap, "account number"
    assert "Title" in gap


def test_bank_details_can_be_switched_off():
    assert "Payment Details" not in rendered("classic", "sales",
                                             show_bank_details=False)


def test_a_purchase_invoice_never_prints_our_own_bank_details():
    """It is the supplier's demand for payment; our account number has no
    business on it."""
    assert "show_bank_details" not in default_options("purchase")
    for design in DESIGN_KEYS:
        assert "Payment Details" not in rendered(design, "purchase")


def test_bank_details_do_not_move_the_summary():
    with_bank = rendered("classic", "sales", show_bank_details=True)
    without = rendered("classic", "sales", show_bank_details=False)
    for out in (with_bank, without):
        body = out[out.index('<div class="inv-sheet'):]
        assert body.count('class="inv-totals-box"') == 1
        assert body.count('class="inv-totals-gap"') == 1


def test_bank_details_prefer_the_real_company_profile():
    class FakeCompany:
        company_name = "Acme Solar"
        address = city = phone = email = tax_id = logo_url = None
        bank_name = "HBL, Model Town"
        bank_account_title = "Acme Solar Pvt Ltd"
        bank_account_number = "PK36HABB0000001234567890"
    ctx = sample_context("sales", FakeCompany())
    assert ctx["company_bank_name"] == "HBL, Model Town"
    assert ctx["company_bank_account_number"] == "PK36HABB0000001234567890"


# ─────────────────────────────────────────────
# Existing templates pick up a new layout
# ─────────────────────────────────────────────

def test_refresh_designs_rebuilds_bodies_saved_under_an_old_layout(app):
    """Without this an existing install keeps printing the old page, because
    body_html is only regenerated when someone re-saves the template."""
    from shared.extensions import db
    stale = "<div style='max-width:820px'>{{grand_total}}</div>"
    for doc_type in DOC_TYPES:
        t = InvoiceTemplate(name=f"Old {doc_type}", type=doc_type,
                            design="classic", accent_color="#0f766e",
                            body_html=stale)
        t.set_options(default_options(doc_type))
        db.session.add(t)
    db.session.flush()

    assert InvoiceTemplate.refresh_designs() == 2
    for t in InvoiceTemplate.query.all():
        assert SHEET_MARKER in t.body_html
    assert InvoiceTemplate.refresh_designs() == 0, "already current, nothing to do"
    db.session.rollback()


def test_refresh_designs_leaves_hand_written_bodies_alone(app):
    from shared.extensions import db
    t = InvoiceTemplate(name="Mine", type="sales", design="custom",
                        body_html="<p>mine {{grand_total}}</p>")
    db.session.add(t)
    db.session.flush()
    InvoiceTemplate.refresh_designs()
    assert t.body_html == "<p>mine {{grand_total}}</p>"
    db.session.rollback()


def test_designs_and_accents_are_well_formed():
    assert len(DESIGNS) >= 3
    for key, label, desc in DESIGNS:
        assert key and label and desc
    for hex_code, name in ACCENT_PRESETS:
        assert re.fullmatch(r"#[0-9a-f]{6}", hex_code), hex_code


# ─────────────────────────────────────────────
# Money on the invoice reads the same as money everywhere else
#
# The invoice document used to format its own figures: the print routes wrote
# f"{v:.2f}" (no thousands separator at all) while this module wrote f"{v:,.2f}"
# (grouped, western only). So one document could show 881760.00 in the totals
# panel and 881,760.00 in the items table, and neither honoured a company set
# to Indian grouping. Both now go through shared.formatting.format_amount.
# ─────────────────────────────────────────────

def _cells(html):
    """The text inside every <td> of a rendered table."""
    return re.findall(r"<td[^>]*>([^<]*)</td>", html)


@pytest.mark.parametrize("doc_type", DOC_TYPES)
def test_invoice_money_is_grouped_in_thousands(doc_type):
    """The defect: the live print routes emitted 881760.00, ungrouped."""
    ctx = sample_context(doc_type)
    assert ctx["grand_total"] == "881,760.00"
    assert ctx["subtotal"] == "760,000.00"


@pytest.mark.parametrize("doc_type", DOC_TYPES)
def test_items_table_and_totals_panel_use_one_convention(doc_type):
    """Both halves of the same document must group money the same way."""
    from shared.models.invoice_template import _sample_items_table
    from shared.models.invoice_template import default_options

    table = _sample_items_table(doc_type, default_options(doc_type))
    money = [c for c in _cells(table)
             if re.fullmatch(r"[\d,]+\.\d{2}", c or "")]
    assert money, "the items table should contain money cells"
    # A figure of four digits or more must carry a separator, exactly as the
    # totals panel does.
    big = [c for c in money if len(c.split(".")[0].replace(",", "")) > 3]
    assert big, "sample data should include a figure above 999"
    for cell in big:
        assert "," in cell, f"{cell} is not grouped like the totals panel"


def test_invoice_money_follows_the_company_number_format(monkeypatch):
    """A company set to Indian grouping gets it on the invoice too, not just
    on the finance reports."""
    import shared.formatting as fmt
    monkeypatch.setattr(fmt, "company_number_format", lambda default=None: fmt.INDIAN)
    ctx = sample_context("sales")
    # 881760 -> 8,81,760.00 in Indian grouping, not 881,760.00
    assert ctx["grand_total"] == "8,81,760.00"


def test_invoice_negatives_use_accounting_brackets():
    """A credit line prints (1,234.50), the convention the reports and the
    spreadsheet already use — never a bare minus."""
    from shared.formatting import format_amount
    assert format_amount(-1234.5) == "(1,234.50)"
    assert "-" not in format_amount(-1234.5)
