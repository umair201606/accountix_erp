"""Finance report exports.

The rule these protect: what the screen shows is what the file contains, at a
size that fits the page, with money formatted as money. Every one of these
covers a defect that shipped — a report that printed off the edge of the paper,
or a spreadsheet full of raw floats — because nothing here was exercised.
"""

import io
import re
from decimal import Decimal

import openpyxl
import pytest
from PyPDF2 import PdfReader

from finance_app.routes.reports import (
    MONEY_FMT, _build_excel_wb, _build_pdf, _cell_text, _finish_sheet)


HEADERS = ["Date", "Voucher", "Account Code", "Account Name", "Description",
           "Debit", "Credit", "Balance"]


def _rows(n=3):
    return [["2026-07-%02d" % ((i % 28) + 1), "JV-202607-%04d" % i, "5-2-03-00704",
             "Selling and Distribution Expenses - Carriage Outward",
             "Being carriage paid on delivery to the customer site",
             12345678.90, 0.0, 98765432.10] for i in range(n)]


def _pdf(headers=None, rows=None, **kw):
    buf = _build_pdf("General Ledger", headers or HEADERS, rows or _rows(), **kw)
    return PdfReader(io.BytesIO(buf.getvalue()))


def _page_text(page):
    return page.extract_text() or ""


def _font_sizes(page):
    """Every font size the page actually draws with."""
    data = page.get_contents().get_data().decode("latin-1")
    return sorted({round(float(m), 2) for m in re.findall(r"/F\d+ ([\d.]+) Tf", data)})


# ─────────────────────────────────────────────
# PDF
# ─────────────────────────────────────────────

def test_every_page_is_numbered():
    """The callbacks were handed to the SimpleDocTemplate constructor, which
    takes arbitrary keywords and drops them, so no PDF was ever numbered."""
    r = _pdf(rows=_rows(120))
    assert len(r.pages) > 1, "expected a multi-page report"
    for i, page in enumerate(r.pages, 1):
        assert "Page %d" % i in _page_text(page)


def test_a_wide_report_goes_landscape():
    portrait = _pdf(headers=HEADERS[:4], rows=[r[:4] for r in _rows()])
    landscape = _pdf()
    pw, ph = portrait.pages[0].mediabox.width, portrait.pages[0].mediabox.height
    lw, lh = landscape.pages[0].mediabox.width, landscape.pages[0].mediabox.height
    assert float(pw) < float(ph), "few columns stay portrait"
    assert float(lw) > float(lh), "many columns turn landscape"


def test_the_type_shrinks_as_columns_are_added():
    """The fitted size only reached cells wrapped in a Paragraph; everything
    else kept reportlab's 10pt default and ran off the page."""
    def smallest(ncols):
        heads = ["Column Heading %d" % i for i in range(ncols)]
        rows = [["Carriage Outward Expenses"] + [1234567.89] * (ncols - 1)
                for _ in range(4)]
        return min(_font_sizes(_pdf(headers=heads, rows=rows).pages[0]))

    narrow, wide = smallest(4), smallest(16)
    assert wide < narrow, "a wider table has to set smaller type"
    assert wide >= 5.0, "but never below legibility"


def test_headings_are_not_broken_mid_word():
    """Columns budgeted less width than reportlab's own cell padding, so every
    one came out narrower than its widest word: Vouche/r, Balanc/e."""
    text = _page_text(_pdf().pages[0])
    for head in ("Voucher", "Balance", "Credit", "Description"):
        assert head in text, head
        assert head[:-1] + "\n" not in text, "%s broke mid-word" % head


def test_the_heading_row_is_drawn_bold():
    """Header cells are Paragraphs, which draw their own text — TableStyle's
    white bold heading never reached them, leaving black on dark blue."""
    page = _pdf().pages[0]
    fonts = {v.get_object().get("/BaseFont")
             for v in page["/Resources"]["/Font"].values()}
    assert "/Helvetica-Bold" in {str(f) for f in fonts}


def test_the_report_title_and_subtitle_survive():
    text = _page_text(_pdf(subtitle="01-Jul-2026 to 31-Jul-2026").pages[0])
    assert "General Ledger" in text
    assert "01-Jul-2026" in text


def test_an_empty_report_still_builds():
    """A period with no entries must produce a page, not an exception."""
    r = _pdf(rows=[])
    assert len(r.pages) == 1
    assert "General Ledger" in _page_text(r.pages[0])


# ─────────────────────────────────────────────
# Excel
# ─────────────────────────────────────────────

def _sheet(**kw):
    buf = _build_excel_wb("Trial Balance", ["Code", "Account Name", "Debit", "Credit"],
                          [["1-1-01", "Cash at Bank", 1234567.891, 0.0],
                           ["5-2-03", "Carriage Outward", 0.0, -9876.5]], **kw)
    return openpyxl.load_workbook(io.BytesIO(buf.getvalue())).active


def test_money_cells_carry_a_money_format():
    """number_format was passed on one export path out of eight, so the rest
    wrote 1234.5 where the report says 1,234.50."""
    ws = _sheet()
    for row in ws.iter_rows(min_row=4, min_col=3, max_col=4):
        for c in row:
            assert c.number_format == MONEY_FMT, (c.coordinate, c.number_format)


def test_text_cells_are_left_as_text():
    ws = _sheet()
    for row in ws.iter_rows(min_row=4, min_col=1, max_col=2):
        for c in row:
            assert c.number_format == "General"


def test_money_stays_a_number_so_the_sheet_can_sum_it():
    ws = _sheet()
    assert isinstance(ws.cell(row=4, column=3).value, (int, float, Decimal))


def test_columns_are_wide_enough_for_the_formatted_figure():
    """Sizing from str(1234567.891) budgets eleven characters for the thirteen
    that get drawn, and Excel shows ###### instead of a number."""
    ws = _sheet()
    width = ws.column_dimensions["C"].width
    assert width >= len("1,234,567.89"), width


@pytest.mark.parametrize("value,expected", [
    (1234567.891, "1,234,567.89"),
    (-9876.5, "-9,876.50"),
    (0, "0.00"),
    (None, ""),
    ("Cash at Bank", "Cash at Bank"),
    (True, "True"),
])
def test_cell_text_reports_what_excel_will_show(value, expected):
    assert _cell_text(value) == expected


def test_finish_sheet_formats_a_hand_built_sheet():
    """The export routes that build their own sheet go through the same pass,
    rather than each carrying a copy that forgot the number format."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.cell(row=3, column=1, value="Account")
    ws.cell(row=3, column=2, value="Amount")
    ws.cell(row=4, column=1, value="Carriage Outward")
    ws.cell(row=4, column=2, value=1234567.891)
    _finish_sheet(ws, 2)
    assert ws.cell(row=4, column=2).number_format == MONEY_FMT
    assert ws.column_dimensions["B"].width >= len("1,234,567.89")


def test_finish_sheet_leaves_a_deliberate_format_alone():
    wb = openpyxl.Workbook()
    ws = wb.active
    c = ws.cell(row=4, column=1, value=0.155)
    c.number_format = "0.00%"
    _finish_sheet(ws, 1)
    assert c.number_format == "0.00%", "an explicit format is not overwritten"
