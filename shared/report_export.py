"""One export pipeline for every report: Excel, PDF and CSV.

Every exported file carries the same things the screen shows and a reader
needs when the file travels without the app:

- company / title / period heading, plus the filters applied and who
  generated it when;
- column headers that stay put (frozen panes, repeated on every printed or
  PDF page) and an AutoFilter on list-style sheets;
- money as numbers in the company's number format, dates as real dates,
  words left and figures right;
- section bands, totals and grand totals styled the same in Excel and PDF;
- a print setup that fits the page width (landscape for wide reports) with
  "Page X of Y" in the footer;
- a file name that says what it is: Company_Report_Period.xlsx.

The finance module grew the first version of these builders; they live here
now so inventory, HR, executive, fixed-asset and register exports share them.
"""
import csv
import io
import re
from datetime import date, datetime
from decimal import Decimal
from io import BytesIO

import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from shared.formatting import excel_money_format, format_amount

XLSX_MIMETYPE = ("application/vnd.openxmlformats-officedocument."
                 "spreadsheetml.sheet")
PDF_MIMETYPE = "application/pdf"
CSV_MIMETYPE = "text/csv"

# ── Palette: the screen's statement styling (finance/layouts/base.html) ──
SLATE = "1E293B"         # table header, grand total
SECTION_BG = "F1F5F9"
SECTION_FG = "334155"
TOTAL_BG = "FAFAFA"
TOTAL_RULE = "94A3B8"
PROFIT_BG = "EFF6FF"
NEG_BG = "FEF2F2"
NEG_FG = "B91C1C"
MUTED = "64748B"
RULE = "E2E8F0"

THIN = Border(left=Side(style="thin", color="CBD5E1"),
              right=Side(style="thin", color="CBD5E1"),
              top=Side(style="thin", color="CBD5E1"),
              bottom=Side(style="thin", color="CBD5E1"))
HEADER_FILL = PatternFill(start_color=SLATE, end_color=SLATE, fill_type="solid")
HEADER_FONT = Font(color="FFFFFF", bold=True, size=10)
TITLE_FONT = Font(bold=True, size=15, color=SLATE)
COMPANY_FONT = Font(bold=True, size=11, color=SECTION_FG)
SUBTITLE_FONT = Font(size=10, color=SECTION_FG)
NOTE_FONT = Font(italic=True, size=8, color=MUTED)
DATA_FONT = Font(size=10)
BOLD_FONT = Font(bold=True, size=10)
CENTER = Alignment(horizontal="center", vertical="center")
RIGHT = Alignment(horizontal="right", vertical="center")
LEFT_ALIGN = Alignment(horizontal="left", vertical="center")
DATE_FMT = "dd-mmm-yyyy"
QTY_FMT = "#,##0.##"

# Rows 1-3: company / title / period. Row 4: filters + generated note.
# Column headers on row 5, content from row 6.
SHEET_FIRST_ROW = 5

ROW_KINDS = {"section", "account", "total", "subtotal", "grand", "spacer", "plain"}


# ─────────────────────────────────────────────
# Context: company, user, generated line, file name
# ─────────────────────────────────────────────

def company_name():
    try:
        from shared.models.company_settings import CompanyInfo
        info = CompanyInfo.get()
        return (info.company_name or "").strip() if info else ""
    except Exception:
        return ""


def _user_name():
    try:
        from flask_login import current_user
        if current_user and current_user.is_authenticated:
            return (getattr(current_user, "full_name", None)
                    or getattr(current_user, "email", "") or "")
    except Exception:
        pass
    return ""


def generated_line(when=None):
    from shared.clock import local_now
    when = when or local_now()     # the company's clock, not the server's
    who = _user_name()
    text = f"Generated {when:%d %b %Y, %H:%M}"
    return f"{text} by {who}" if who else text


def note_line(filters=None):
    """Row-4 / under-title note: the filters applied, then who and when."""
    parts = [f for f in (filters or []) if f]
    parts.append(generated_line())
    return "  ·  ".join(parts)


def _slug(text, limit=60, keep_dash=False):
    text = re.sub(r"&amp;|&", "and", text or "")
    text = re.sub(r"[^A-Za-z0-9-]+" if keep_dash else r"[^A-Za-z0-9]+", "_", text)
    return text.strip("_-")[:limit].strip("_-")


def export_filename(title, ext, period=None, company=True):
    """``Company_Report_Period.ext`` — safe on every OS, readable in a
    downloads folder full of exports."""
    parts = []
    if company:
        c = _slug(company_name(), 30)
        if c:
            parts.append(c)
    parts.append(_slug(title) or "report")
    if period:
        p = period
        if isinstance(p, (date, datetime)):
            p = p.strftime("%Y-%m-%d")
        p = _slug(str(p), 40, keep_dash=True)
        if p:
            parts.append(p)
    return "_".join(parts) + "." + ext


def send_export(buf, fmt, title, period=None):
    """send_file() for a built export with the standard name and mimetype."""
    from flask import send_file
    ext, mime = {"excel": ("xlsx", XLSX_MIMETYPE), "xlsx": ("xlsx", XLSX_MIMETYPE),
                 "pdf": ("pdf", PDF_MIMETYPE), "csv": ("csv", CSV_MIMETYPE)}[fmt]
    return send_file(buf, as_attachment=True,
                     download_name=export_filename(title, ext, period),
                     mimetype=mime)


# ─────────────────────────────────────────────
# Cell helpers
# ─────────────────────────────────────────────

def looks_numeric(v):
    """Is this cell a figure, whoever formatted it? ("1,234.50", "(9.5)")"""
    if isinstance(v, bool):
        return False
    if isinstance(v, (int, float, Decimal)):
        return True
    if not isinstance(v, str):
        return False
    s = v.strip().replace(",", "")
    if s.startswith("(") and s.endswith(")"):
        s = s[1:-1]
    if s.endswith("%"):
        s = s[:-1]
    if not s:
        return False
    try:
        float(s)
        return True
    except ValueError:
        return False


def cell_text(v):
    """What Excel will display — what a column must be wide enough for."""
    if isinstance(v, bool) or v is None:
        return "" if v is None else str(v)
    if isinstance(v, (datetime, date)):
        return "00-Mmm-0000"
    if isinstance(v, (int, float, Decimal)):
        return format_amount(v)
    return str(v)


def _is_number(v):
    return isinstance(v, (int, float, Decimal)) and not isinstance(v, bool)


def _excel_value(v):
    if isinstance(v, Decimal):
        return float(v)
    return v


# ─────────────────────────────────────────────
# Excel
# ─────────────────────────────────────────────

def write_sheet_heading(ws, ncols, title, period="", filters=None):
    """Company / title / period, then the filters + generated note.
    Returns the header row (SHEET_FIRST_ROW)."""
    span = max(ncols, 1)
    lines = [(company_name(), COMPANY_FONT), (title, TITLE_FONT),
             (period or "", SUBTITLE_FONT), (note_line(filters), NOTE_FONT)]
    for r, (text, font) in enumerate(lines, 1):
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=span)
        c = ws.cell(row=r, column=1, value=text)
        c.font = font
        c.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[2].height = 22
    return SHEET_FIRST_ROW


def style_header_row(ws, row, headers):
    for ci, h in enumerate(headers, 1):
        c = ws.cell(row=row, column=ci, value=h)
        c.font = HEADER_FONT
        c.fill = HEADER_FILL
        c.alignment = Alignment(horizontal="center", vertical="center",
                                wrap_text=True)
        c.border = THIN
    ws.row_dimensions[row].height = 30


def _fill(color):
    return PatternFill(start_color=color, end_color=color, fill_type="solid")


def style_row(ws, row, ncols, kind, negative=False):
    """Section band / total / subtotal / grand styling for one sheet row —
    the same treatment the PDF gives the row."""
    if kind not in ("section", "total", "subtotal", "grand"):
        return
    for ci in range(1, ncols + 1):
        c = ws.cell(row=row, column=ci)
        if kind == "section":
            c.fill = _fill(SECTION_BG)
            c.font = Font(bold=True, size=10, color=SECTION_FG)
        elif kind == "total":
            c.fill = _fill(TOTAL_BG)
            c.font = BOLD_FONT
            c.border = Border(left=THIN.left, right=THIN.right,
                              top=Side(style="thin", color=TOTAL_RULE),
                              bottom=THIN.bottom)
        elif kind == "subtotal":
            c.fill = _fill(NEG_BG if negative else PROFIT_BG)
            c.font = Font(bold=True, size=10, color=NEG_FG if negative else "000000")
            c.border = Border(left=THIN.left, right=THIN.right,
                              top=Side(style="medium", color=SLATE),
                              bottom=Side(style="medium", color=SLATE))
        elif kind == "grand":
            c.fill = HEADER_FILL
            c.font = Font(bold=True, size=10, color="FFFFFF")
            c.border = Border(top=Side(style="medium", color=SLATE),
                              bottom=Side(style="double", color=SLATE))


def _find_header_row(ws, ncols):
    """The column-header row: the first row from SHEET_FIRST_ROW down whose
    first cell carries the header fill."""
    for r in range(SHEET_FIRST_ROW - 2, min(ws.max_row, 40) + 1):
        c = ws.cell(row=r, column=1)
        if c.fill is not None and c.fill.fill_type == "solid" and \
                (c.fill.start_color.rgb or "").upper().endswith(SLATE):
            return r
    return None


def setup_sheet(ws, ncols, header_row=None, title=None, autofilter=False,
                last_row=None, landscape_mode=None):
    """Frozen headers, print layout, footer and (optionally) AutoFilter."""
    if header_row is None:
        header_row = _find_header_row(ws, ncols)
    if header_row:
        ws.freeze_panes = ws.cell(row=header_row + 1, column=1)
        ws.print_title_rows = f"{header_row}:{header_row}"
        last = last_row or ws.max_row
        if autofilter and last > header_row:
            ws.auto_filter.ref = (f"A{header_row}:"
                                  f"{get_column_letter(max(ncols, 1))}{last}")
    if landscape_mode is None:
        landscape_mode = ncols > 5
    ws.page_setup.orientation = "landscape" if landscape_mode else "portrait"
    ws.page_setup.paperSize = ws.PAPERSIZE_A4
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.print_options.horizontalCentered = True
    ws.page_margins.left = ws.page_margins.right = 0.4
    ws.page_margins.top = ws.page_margins.bottom = 0.6
    company = company_name()
    ws.oddFooter.left.text = (company + (" · " if company and title else "")
                              + (title or "")).replace("&", "&&")
    ws.oddFooter.left.size = 8
    ws.oddFooter.right.text = "Page &P of &N"
    ws.oddFooter.right.size = 8
    ws.sheet_view.showGridLines = False


def set_properties(wb, title):
    wb.properties.title = title
    wb.properties.creator = _user_name() or "Accountix ERP"
    wb.properties.company = company_name()
    wb.properties.subject = title


def autosize(ws, ncols, first_row, skip_merged=True):
    merged = set()
    if skip_merged:
        for rng in ws.merged_cells.ranges:
            if rng.max_col > rng.min_col:
                for r in range(rng.min_row, rng.max_row + 1):
                    for c in range(rng.min_col, rng.max_col + 1):
                        merged.add((r, c))
    for ci in range(1, ncols + 1):
        max_len = 0
        for row in ws.iter_rows(min_row=first_row, min_col=ci, max_col=ci):
            cell = row[0]
            if (cell.row, ci) in merged:
                continue
            text = cell_text(cell.value)
            # A wrapped header contributes its longest word, not its length.
            if cell.alignment is not None and cell.alignment.wrap_text:
                text = max(str(cell.value or "").split() or [""], key=len)
            max_len = max(max_len, len(text))
        ws.column_dimensions[get_column_letter(ci)].width = \
            min(max(max_len + 2, 8), 60)


def finish_sheet(ws, ncols, first_row=3, title=None, autofilter=False):
    """Number formats, widths, frozen header and print setup for a sheet
    built by hand (statements with their own layout)."""
    money_fmt = excel_money_format()
    for row in ws.iter_rows(min_row=first_row, max_col=ncols):
        for c in row:
            if _is_number(c.value) and c.number_format in (None, "General"):
                c.number_format = money_fmt
            elif isinstance(c.value, (date, datetime)) and \
                    c.number_format in (None, "General"):
                c.number_format = DATE_FMT
    autosize(ws, ncols, first_row)
    setup_sheet(ws, ncols, title=title or ws.title, autofilter=autofilter)


def _resolve_qty_formats(rows, col_formats):
    """Excel has no "decimals only when needed" format ("#,##0.##" shows a
    stray dot on 10), so a quantity column is whole-number when every value
    in it is whole, two decimals otherwise."""
    out = dict(col_formats or {})
    for ci, fmt in out.items():
        if fmt not in (QTY_FMT, "0.#"):
            continue
        vals = [r[ci] for r in rows if ci < len(r) and _is_number(r[ci])]
        whole = all(float(v).is_integer() for v in vals)
        out[ci] = "#,##0" if whole else "#,##0.00"
    return out


def build_excel(title, headers, rows, col_widths=None, bold_rows=None,
                number_format=None, sheet_title=None, period="", filters=None,
                row_kinds=None, col_formats=None, autofilter=None, extra_sheets=None):
    """A complete report workbook.

    rows: lists of cell values (numbers stay numbers, dates stay dates).
    row_kinds: per-row "section"/"total"/"subtotal"/"grand"/... as in the PDF.
    col_formats: {column index: Excel number format} for non-money columns
    (quantities, percentages). autofilter defaults to on for flat lists
    (no section rows). extra_sheets: [(name, headers, rows)] appended as
    further plain list sheets.
    """
    if number_format is None:
        number_format = excel_money_format()
    col_formats = _resolve_qty_formats(rows, col_formats)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = _sheet_name(sheet_title or title)
    set_properties(wb, title)
    ncols = len(headers)

    hdr_row = write_sheet_heading(ws, ncols, title, period=period, filters=filters)
    style_header_row(ws, hdr_row, headers)

    kinds = list(row_kinds or [])
    kinds += ["plain"] * (len(rows) - len(kinds))
    bold_rows = set(bold_rows or ())
    for i, row in enumerate(rows):
        ri = hdr_row + 1 + i
        kind = kinds[i]
        for ci, val in enumerate(list(row)[:ncols], 1):
            c = ws.cell(row=ri, column=ci, value=_excel_value(val))
            c.font = BOLD_FONT if i in bold_rows else DATA_FONT
            c.border = THIN
            if _is_number(val):
                c.alignment = RIGHT
                c.number_format = col_formats.get(ci - 1, number_format)
            elif isinstance(val, (date, datetime)):
                c.alignment = LEFT_ALIGN
                c.number_format = DATE_FMT
            else:
                c.alignment = LEFT_ALIGN
        if i in bold_rows and kind == "plain":
            kind = "total"
        negative = kind == "subtotal" and any(
            _is_number(v) and v < 0 for v in row)
        style_row(ws, ri, ncols, kind, negative)

    last_row = hdr_row + len(rows)
    if col_widths:
        for ci, w in enumerate(col_widths, 1):
            ws.column_dimensions[get_column_letter(ci)].width = w
    else:
        autosize(ws, ncols, hdr_row)
    if autofilter is None:
        autofilter = not any(k in ("section", "subtotal") for k in kinds)
    # The filter range stops above a closing total/grand row, so sorting or
    # filtering the list never moves the total into the middle of it.
    filter_last = last_row
    while filter_last > hdr_row and kinds[filter_last - hdr_row - 1] in ("total", "grand"):
        filter_last -= 1
    setup_sheet(ws, ncols, header_row=hdr_row, title=title,
                autofilter=autofilter, last_row=filter_last)

    for name, sh_headers, sh_rows in (extra_sheets or []):
        add_list_sheet(wb, name, sh_headers, sh_rows, title=title, period=period)

    out = BytesIO()
    wb.save(out)
    out.seek(0)
    return out


def add_list_sheet(wb, name, headers, rows, title=None, period="", filters=None):
    ws = wb.create_sheet(_sheet_name(name))
    hdr = write_sheet_heading(ws, len(headers), f"{title} — {name}" if title else name,
                              period=period, filters=filters)
    style_header_row(ws, hdr, headers)
    money = excel_money_format()
    for i, row in enumerate(rows):
        for ci, val in enumerate(row, 1):
            c = ws.cell(row=hdr + 1 + i, column=ci, value=_excel_value(val))
            c.border = THIN
            c.font = DATA_FONT
            if _is_number(val):
                c.number_format = money
                c.alignment = RIGHT
            elif isinstance(val, (date, datetime)):
                c.number_format = DATE_FMT
    autosize(ws, len(headers), hdr)
    setup_sheet(ws, len(headers), header_row=hdr, title=name, autofilter=True)
    return ws


def _sheet_name(name):
    name = re.sub(r"[\[\]:*?/\\]", "-", name or "Report").replace("&amp;", "&")
    return name[:31] or "Report"


# ─────────────────────────────────────────────
# PDF
# ─────────────────────────────────────────────

PDF_HEAD_BG = colors.HexColor("#" + SLATE)
PDF_RULE = colors.HexColor("#" + RULE)
PDF_SECTION_BG = colors.HexColor("#" + SECTION_BG)
PDF_SECTION_FG = colors.HexColor("#" + SECTION_FG)
PDF_SECTION_RULE = colors.HexColor("#CBD5E1")
PDF_TOTAL_BG = colors.HexColor("#" + TOTAL_BG)
PDF_TOTAL_RULE = colors.HexColor("#" + TOTAL_RULE)
PDF_PROFIT_BG = colors.HexColor("#" + PROFIT_BG)
PDF_NEG_BG = colors.HexColor("#" + NEG_BG)
PDF_NEG_FG = colors.HexColor("#" + NEG_FG)
PDF_MUTED = colors.HexColor("#" + MUTED)


def _numbered_canvas(footer_left, footer_mid):
    """A canvas that knows the page count, for "Page X of Y"."""
    class NumberedCanvas(rl_canvas.Canvas):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self._saved = []

        def showPage(self):
            self._saved.append(dict(self.__dict__))
            self._startPage()

        def save(self):
            total = len(self._saved)
            for state in self._saved:
                self.__dict__.update(state)
                self._footer(total)
                super().showPage()
            super().save()

        def _footer(self, total):
            w, _h = self._pagesize
            self.saveState()
            self.setStrokeColor(PDF_RULE)
            self.setLineWidth(0.5)
            self.line(10 * mm, 12 * mm, w - 10 * mm, 12 * mm)
            self.setFont("Helvetica", 7)
            self.setFillColor(PDF_MUTED)
            self.drawString(10 * mm, 8 * mm, footer_left[:110])
            self.drawCentredString(w / 2, 8 * mm, footer_mid[:90])
            self.drawRightString(w - 10 * mm, 8 * mm,
                                 f"Page {self._pageNumber} of {total}")
            self.restoreState()
    return NumberedCanvas


def build_pdf(title, headers, rows, col_widths=None, bold_rows=None,
              subtitle=None, company=None, row_kinds=None, indent_col=None,
              mono_col=None, filters=None, landscape_mode=None, col_formats=None):
    from reportlab.pdfbase.pdfmetrics import stringWidth
    col_formats = col_formats or {}

    ncols = len(headers)
    if landscape_mode is None:
        landscape_mode = ncols > 5
    pagesize = landscape(A4) if landscape_mode else A4
    buf = BytesIO()

    doc = SimpleDocTemplate(buf, pagesize=pagesize,
                            rightMargin=10 * mm, leftMargin=10 * mm,
                            topMargin=12 * mm, bottomMargin=17 * mm,
                            title=re.sub(r"&amp;", "&", title),
                            author=_user_name() or "Accountix ERP")
    styles = getSampleStyleSheet()
    elements = []

    centred = ParagraphStyle("hdr", parent=styles["Normal"], alignment=1,
                             textColor=PDF_SECTION_FG, fontSize=9.5)
    company_style = ParagraphStyle("co", parent=centred, fontName="Helvetica-Bold",
                                   fontSize=11)
    title_style = ParagraphStyle("ttl", parent=styles["Title"], fontSize=16,
                                 leading=19, spaceAfter=2,
                                 textColor=PDF_HEAD_BG)
    note_style = ParagraphStyle("note", parent=centred, fontSize=7.5,
                                textColor=PDF_MUTED)
    if company is None:
        company = company_name()
    if company:
        elements.append(Paragraph(company, company_style))
    elements.append(Paragraph(title, title_style))
    if subtitle:
        elements.append(Paragraph(subtitle, centred))
    flt = [f for f in (filters or []) if f]
    if flt:
        elements.append(Paragraph("  ·  ".join(flt), note_style))
    elements.append(Spacer(1, 5 * mm))

    kinds = list(row_kinds or [])
    kinds += ["plain"] * (len(rows) - len(kinds))
    rows = [list(r[:ncols]) + [""] * (ncols - len(r)) for r in rows]

    def _txt(c, ci=None):
        if _is_number(c):
            fmt = col_formats.get(ci)
            if fmt in ("0", "#,##0"):
                return f"{float(c):,.0f}"
            if fmt in (QTY_FMT, "0.#", "#,##0.#"):
                # Quantities and counts: no trailing ".00" on whole numbers.
                return f"{float(c):,.2f}".rstrip("0").rstrip(".")
            return format_amount(c)
        if isinstance(c, datetime):
            return c.strftime("%d %b %Y %H:%M")
        if isinstance(c, date):
            return c.strftime("%d %b %Y")
        return "" if c is None else str(c)

    body = []
    for row, kind in zip(rows, kinds):
        cells = [_txt(c, ci) for ci, c in enumerate(row)]
        if kind == "section":
            label = next((c for c in cells if c.strip()), "")
            cells = [label] + [""] * (len(cells) - 1)
        body.append(cells)
    data = [list(headers)] + body
    available_width = doc.width

    numeric_cols = set()
    for ci in range(ncols):
        seen = False
        for row in rows:
            if ci >= len(row) or row[ci] in (None, ""):
                continue
            if not looks_numeric(row[ci]):
                seen = False
                break
            seen = True
        if seen:
            numeric_cols.add(ci)

    base_font = 8
    header_font_size = base_font + 1
    pad_x = 4
    if col_widths is None:
        body_w = [0] * ncols
        for row in body:
            for ci, val in enumerate(row):
                body_w[ci] = max(body_w[ci], stringWidth(str(val), "Helvetica", base_font))

        def _head_w(whole):
            # A heading with an explicit line break ("Wk 1" over "03 Aug") is two
            # lines that each stay whole; otherwise wrapping may only break
            # between words.
            out = []
            for h in headers:
                lines = str(h).split("\n")
                if len(lines) > 1 or whole:
                    parts = lines
                else:
                    parts = str(h).split() or [""]
                out.append(max(stringWidth(t, "Helvetica-Bold", header_font_size)
                               for t in parts))
            return out
        col_widths = [max(b, h) + 2 * pad_x + 2
                      for b, h in zip(body_w, _head_w(True))]
        if sum(col_widths) > available_width:
            # Too wide: let headings wrap between words (never mid-word)
            # before shrinking the type.
            col_widths = [max(b, h) + 2 * pad_x + 2
                          for b, h in zip(body_w, _head_w(False))]

    total_width = sum(col_widths)
    if total_width > available_width:
        scale = available_width / total_width
        base_font = max(5.5, base_font * scale)
        header_font_size = max(6.0, base_font + 1)
        col_widths = [w * scale for w in col_widths]
    elif total_width < available_width * 0.75 and ncols > 1:
        # A narrow table stretched to the page reads better than one
        # floating in the middle; widen the text columns only.
        text_cols = [i for i in range(ncols) if i not in numeric_cols] or list(range(ncols))
        extra = (available_width * 0.92 - total_width) / len(text_cols)
        col_widths = [w + extra if i in text_cols else w
                      for i, w in enumerate(col_widths)]

    font_size = base_font
    cell_style = ParagraphStyle("cell", fontName="Helvetica", fontSize=font_size,
                                leading=font_size * 1.25)
    head_style = ParagraphStyle("cellhead", fontName="Helvetica-Bold",
                                fontSize=header_font_size,
                                leading=header_font_size * 1.2,
                                textColor=colors.white, alignment=1)

    def _kind_style(name, **kw):
        return ParagraphStyle(name, parent=cell_style, **kw)

    label_styles = {
        "section": _kind_style("s", fontName="Helvetica-Bold", textColor=PDF_SECTION_FG),
        "total": _kind_style("t", fontName="Helvetica-Bold"),
        "subtotal": _kind_style("st", fontName="Helvetica-Bold"),
        "subtotal_neg": _kind_style("stn", fontName="Helvetica-Bold", textColor=PDF_NEG_FG),
        "grand": _kind_style("g", fontName="Helvetica-Bold", textColor=colors.white),
        "mono": _kind_style("m", fontName="Courier"),
    }

    table_data = []
    for ri, row in enumerate(data):
        kind = "header" if ri == 0 else kinds[ri - 1]
        table_row = []
        for ci, val in enumerate(row):
            if ri == 0:
                table_row.append(Paragraph(_escape(val).replace("\n", "<br/>"),
                                           head_style))
            elif ci in numeric_cols:
                table_row.append(val)
            else:
                key = kind
                if kind == "subtotal" and any(str(c).startswith("(") for c in row):
                    key = "subtotal_neg"
                elif kind == "account" and ci == mono_col:
                    key = "mono"
                table_row.append(Paragraph(_escape(val), label_styles.get(key, cell_style)))
        table_data.append(table_row)

    t = Table(table_data, colWidths=col_widths, repeatRows=1)
    pad = 4 if font_size >= 7 else 2
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), PDF_HEAD_BG),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), header_font_size),
        ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 1), (-1, -1), font_size),
        ("LEADING", (0, 0), (-1, -1), font_size * 1.25),
        ("ALIGN", (0, 0), (-1, 0), "CENTER"),
        ("TOPPADDING", (0, 0), (-1, -1), pad),
        ("BOTTOMPADDING", (0, 0), (-1, -1), pad),
        ("LEFTPADDING", (0, 0), (-1, -1), pad_x),
        ("RIGHTPADDING", (0, 0), (-1, -1), pad_x),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]
    for ci in range(ncols):
        style.append(("ALIGN", (ci, 1), (ci, -1),
                      "RIGHT" if ci in numeric_cols else "LEFT"))

    for ri, kind in enumerate(kinds, 1):
        if kind == "spacer":
            continue
        if kind == "section":
            style += [
                ("SPAN", (0, ri), (-1, ri)),
                ("BACKGROUND", (0, ri), (-1, ri), PDF_SECTION_BG),
                ("TEXTCOLOR", (0, ri), (-1, ri), PDF_SECTION_FG),
                ("FONTNAME", (0, ri), (-1, ri), "Helvetica-Bold"),
                ("LINEABOVE", (0, ri), (-1, ri), 0.5, PDF_SECTION_RULE),
                ("ALIGN", (0, ri), (-1, ri), "LEFT"),
            ]
        elif kind == "total":
            style += [
                ("BACKGROUND", (0, ri), (-1, ri), PDF_TOTAL_BG),
                ("FONTNAME", (0, ri), (-1, ri), "Helvetica-Bold"),
                ("LINEABOVE", (0, ri), (-1, ri), 0.6, PDF_TOTAL_RULE),
            ]
        elif kind == "subtotal":
            negative = any(str(c).startswith("(") for c in body[ri - 1])
            style += [
                ("BACKGROUND", (0, ri), (-1, ri), PDF_NEG_BG if negative else PDF_PROFIT_BG),
                ("FONTNAME", (0, ri), (-1, ri), "Helvetica-Bold"),
                ("LINEABOVE", (0, ri), (-1, ri), 1.0, PDF_HEAD_BG),
                ("LINEBELOW", (0, ri), (-1, ri), 1.0, PDF_HEAD_BG),
            ]
            if negative:
                style.append(("TEXTCOLOR", (0, ri), (-1, ri), PDF_NEG_FG))
        elif kind == "grand":
            style += [
                ("BACKGROUND", (0, ri), (-1, ri), PDF_HEAD_BG),
                ("TEXTCOLOR", (0, ri), (-1, ri), colors.white),
                ("FONTNAME", (0, ri), (-1, ri), "Helvetica-Bold"),
            ]
        else:
            style.append(("LINEBELOW", (0, ri), (-1, ri), 0.4, PDF_RULE))
            if kind == "account":
                if indent_col is not None:
                    style.append(("LEFTPADDING", (indent_col, ri), (indent_col, ri),
                                  pad_x + 10))
                if mono_col is not None:
                    style.append(("FONTNAME", (mono_col, ri), (mono_col, ri), "Courier"))
    for ri in (bold_rows or ()):
        r = ri + 1
        style += [("FONTNAME", (0, r), (-1, r), "Helvetica-Bold"),
                  ("BACKGROUND", (0, r), (-1, r), colors.HexColor("#E8EEF5"))]
    t.setStyle(TableStyle(style))
    elements.append(t)
    if not rows:
        elements.append(Spacer(1, 4 * mm))
        elements.append(Paragraph("No entries for the selected filters.", note_style))

    plain_title = re.sub(r"<[^>]+>", "", title).replace("&amp;", "&")
    footer_left = " · ".join(p for p in (company, plain_title) if p)
    doc.build(elements, canvasmaker=_numbered_canvas(footer_left, generated_line()))
    buf.seek(0)
    return buf


def _escape(val):
    """Text for a Paragraph: escape a bare & or < so a party name like
    "A & B Traders" doesn't break the markup — but keep entities and the
    simple tags reports already send (&amp;, <b>)."""
    s = str(val)
    if "<" in s and re.search(r"</?(b|i|br|font)\b", s):
        return s
    s = re.sub(r"&(?!amp;|lt;|gt;|#\d+;)", "&amp;", s)
    return s.replace("<", "&lt;")


# ─────────────────────────────────────────────
# CSV
# ─────────────────────────────────────────────

def build_csv(headers, rows):
    """UTF-8 with BOM (so Excel opens Urdu names and the rupee sign right),
    numbers unformatted, dates ISO."""
    sio = io.StringIO()
    w = csv.writer(sio)
    w.writerow(headers)
    for row in rows:
        w.writerow(["" if v is None else
                    (v.isoformat() if isinstance(v, (date, datetime)) else v)
                    for v in row])
    return BytesIO(sio.getvalue().encode("utf-8-sig"))


# ─────────────────────────────────────────────
# One-call export for a list-style report
# ─────────────────────────────────────────────

def export_table(fmt, title, headers, rows, period="", filters=None,
                 row_kinds=None, file_period=None, col_formats=None,
                 mono_col=None, landscape_mode=None):
    """Excel / PDF / CSV response for a report given as headers + rows.
    ``fmt`` is "excel"/"xlsx", "pdf" or "csv"; anything else → 404."""
    from flask import abort
    fmt = (fmt or "").lower()
    if fmt in ("excel", "xlsx"):
        buf = build_excel(title, headers, rows, period=period, filters=filters,
                          row_kinds=row_kinds, col_formats=col_formats)
    elif fmt == "pdf":
        buf = build_pdf(title, headers, rows, subtitle=period, filters=filters,
                        row_kinds=row_kinds, mono_col=mono_col,
                        landscape_mode=landscape_mode, col_formats=col_formats)
    elif fmt == "csv":
        buf = build_csv(headers, rows)
    else:
        abort(404)
    return send_export(buf, fmt, title, file_period or period)
