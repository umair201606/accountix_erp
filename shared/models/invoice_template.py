"""Invoice print templates.

Two ways to author a template:

    A DESIGN + TOGGLES  pick a ready-made layout, tick what should appear, and
                        the HTML is generated. This is the path that has to work
                        for someone who does not write HTML — which is nearly
                        everyone who configures an invoice.

    CUSTOM HTML         hand-write the body. The escape hatch for the rare case
                        a design cannot express, kept because taking it away
                        would strand anyone already using it.

Either way the stored ``body_html`` is the same thing: markup containing
``{{placeholder}}`` tokens that ``render_invoice_template`` fills in at print
time. Designs are compiled to body_html on save, so the print path never needs
to know which authoring mode produced it.
"""

import json
from datetime import datetime

from shared.extensions import db
from shared.formatting import format_amount as _m


# ── Placeholders ────────────────────────────────────────────────────────────
# The contract with the print routes: every key here is supplied in the ctx
# that invoices.py / purchase_invoice.py build.

PLACEHOLDER_HELP = {
    "company_name": "Company name",
    "company_address": "Company street address",
    "company_city": "Company city",
    "company_phone": "Company phone number",
    "company_email": "Company email",
    "company_tax_id": "Company tax/NTN number",
    "company_logo": "Company logo image tag",
    "company_bank_name": "Company bank name",
    "company_bank_account_title": "Company bank account title",
    "company_bank_account_number": "Company bank account number",
    "invoice_no": "Invoice / voucher number",
    "invoice_date": "Invoice date",
    "due_date": "Payment due date",
    "status": "Invoice status (approved/unapproved)",
    "party_name": "Customer or supplier name",
    "party_address": "Customer or supplier address",
    "party_city": "Customer or supplier city",
    "party_phone": "Customer or supplier phone",
    "party_email": "Customer or supplier email",
    "party_tax_id": "Customer or supplier tax ID",
    "items_table": "Full HTML table of invoice line items",
    "subtotal": "Subtotal amount",
    "discount": "Total discount amount",
    "tax": "Total sales tax amount",
    "grand_total": "Net total payable/receivable",
    "delivery_charges": "Delivery charges (sales only)",
    "installation_charges": "Installation charges (sales only)",
    "commission": "Commission (procurement only)",
    "freight": "Freight charges (procurement only)",
    "loading_unloading": "Loading/unloading charges (procurement only)",
    "withholding_tax": "Withholding tax (procurement only)",
    "notes": "Invoice notes",
}


# ── Designs ─────────────────────────────────────────────────────────────────

DESIGNS = [
    ("classic", "Classic",
     "Centred letterhead with ruled divisions. Formal and conservative — the "
     "look most customers expect from an invoice."),
    ("modern", "Modern",
     "Logo left, invoice details right, under a slim colour bar. Clean and "
     "current without being loud."),
    ("minimal", "Minimal",
     "Generous white space and hairline rules. Understated; lets the numbers "
     "speak."),
    ("bold", "Bold",
     "Solid colour header block. High contrast and easy to pick out of a pile "
     "of paperwork."),
    ("letterhead", "Letterhead",
     "Logo on the left with the company name centred across the page and the "
     "invoice number and date on the right. Customer on the left, invoice "
     "details on the right below it."),
]
DESIGN_KEYS = [k for k, _l, _d in DESIGNS]

ACCENT_PRESETS = [
    ("#0f766e", "Teal"),
    ("#1d4ed8", "Blue"),
    ("#6d28d9", "Purple"),
    ("#b91c1c", "Red"),
    ("#c2410c", "Orange"),
    ("#166534", "Green"),
    ("#334155", "Slate"),
    ("#0f172a", "Black"),
]


# ── What can be shown ───────────────────────────────────────────────────────
# (key, label, help). Grouped for the settings UI. Wording is deliberately in
# the user's terms ("Their tax number") rather than the database field's.

_COMMON_HEADER = [
    ("show_logo", "Company logo", "Your logo at the top of the page"),
    ("show_company_tax_id", "Your tax / NTN number", "Required on a tax invoice in most places"),
    ("show_status", "Approved / unapproved stamp", "Useful internally, usually hidden from customers"),
    ("show_due_date", "Due date", "When payment is expected"),
]

_COMMON_PARTY = [
    ("show_party_address", "Their address", None),
    ("show_party_contact", "Their phone and email", None),
    ("show_party_tax_id", "Their tax / NTN number", "Needed if they claim input tax"),
]

_COMMON_TOTALS = [
    ("show_discount", "Discount line", "Hide to show only the discounted price"),
    ("show_tax", "Sales tax line", None),
]

_DISPLAY_MODES = [
    ("discount_display", "Discount columns", "Show discount % and amount per line item or as a combined total in the footer"),
    ("tax_display", "Tax columns", "Show sales tax % and amount per line item or as a combined total in the footer"),
    ("charges_display", "Charges columns", "Show charges (delivery/installation/commission etc.) per line item or as a combined total in the footer"),
]

DISPLAY_CHOICES = {
    "discount_display": [("per_line", "Per line item"), ("combined", "Combined total")],
    "tax_display": [("per_line", "Per line item"), ("combined", "Combined total")],
    "charges_display": [("per_line", "Per line item"), ("combined", "Combined total")],
}

# Options whose value is a string rather than a boolean
_STRING_OPTIONS = {"discount_display", "tax_display", "charges_display"}

_COMMON_FOOTER = [
    ("show_notes", "Notes", "Whatever was typed in the invoice's notes box"),
    ("show_signature", "Signature lines", "Space to sign on the printed page"),
    ("show_thanks", "Closing line", None),
]

_SALES_TOTALS = [
    ("show_delivery", "Delivery charges", None),
    ("show_installation", "Installation charges", None),
    ("show_bank_details", "Bank details for payment",
     "Your bank name, account title and number, printed beside the totals"),
]

_PURCHASE_TOTALS = [
    ("show_commission", "Commission", None),
    ("show_freight", "Freight", None),
    ("show_loading", "Loading / unloading", None),
    ("show_withholding", "Withholding tax", None),
]


def option_groups(doc_type):
    """The toggles offered for a document type, grouped for the settings UI."""
    totals = _COMMON_TOTALS + (_SALES_TOTALS if doc_type == "sales" else _PURCHASE_TOTALS)
    return [
        ("Header", _COMMON_HEADER),
        ("Customer details" if doc_type == "sales" else "Supplier details", _COMMON_PARTY),
        ("Amounts", totals),
        ("Columns", _DISPLAY_MODES),
        ("Footer", _COMMON_FOOTER),
    ]


def default_options(doc_type):
    """Sensible starting point: everything a document normally carries.

    The approval stamp is off — it is an internal state, not something a
    customer needs to see.
    """
    opts = {}
    for _group, fields in option_groups(doc_type):
        for key, _label, _help in fields:
            opts[key] = True
    opts["show_status"] = False
    opts["discount_display"] = "combined"
    opts["tax_display"] = "combined"
    opts["charges_display"] = "combined"
    return opts


def normalise_options(doc_type, raw):
    """Coerce stored/submitted options to the full known set.

    Unknown keys are dropped and missing keys fall back to the default, so a
    template saved before an option existed keeps working and simply picks up
    the new field's default.
    """
    known = default_options(doc_type)
    if not raw:
        return known
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return known
    if not isinstance(raw, dict):
        return known
    out = {}
    for k, v in known.items():
        if k in _STRING_OPTIONS:
            out[k] = raw.get(k, v) if raw.get(k, v) in ("per_line", "combined") else v
        else:
            out[k] = bool(raw.get(k, v))
    return out


# ── HTML generation ─────────────────────────────────────────────────────────

def _esc_attr(v):
    return str(v or "").replace('"', "&quot;")


def _totals_rows(doc_type, opts, accent):
    """The money block, in the order an accountant reads it.

    Items whose display mode is ``per_line`` are omitted here — they appear
    as columns in the items table instead.
    """
    rows = [("Subtotal", "{{subtotal}}", False)]
    if opts.get("show_discount") and opts.get("discount_display") != "per_line":
        rows.append(("Discount", "{{discount}}", False))
    if opts.get("show_tax") and opts.get("tax_display") != "per_line":
        rows.append(("Sales Tax", "{{tax}}", False))
    if doc_type == "sales":
        if opts.get("show_delivery") and opts.get("charges_display") != "per_line":
            rows.append(("Delivery", "{{delivery_charges}}", False))
        if opts.get("show_installation") and opts.get("charges_display") != "per_line":
            rows.append(("Installation", "{{installation_charges}}", False))
    else:
        if opts.get("show_commission") and opts.get("charges_display") != "per_line":
            rows.append(("Commission", "{{commission}}", False))
        if opts.get("show_freight") and opts.get("charges_display") != "per_line":
            rows.append(("Freight", "{{freight}}", False))
        if opts.get("show_loading") and opts.get("charges_display") != "per_line":
            rows.append(("Loading / Unloading", "{{loading_unloading}}", False))
        if opts.get("show_withholding") and opts.get("charges_display") != "per_line":
            rows.append(("Withholding Tax", "{{withholding_tax}}", False))
    label = "Net Receivable" if doc_type == "sales" else "Net Payable"
    rows.append((label, "{{grand_total}}", True))

    out = []
    for text, token, is_total in rows:
        if is_total:
            out.append(
                f'<tr><td style="padding:10px 12px;text-align:right;font-weight:700;'
                f'font-size:15px;border-top:2px solid {accent};">{text}</td>'
                f'<td style="padding:10px 12px;text-align:right;font-weight:800;'
                f'font-size:15px;border-top:2px solid {accent};color:{accent};'
                f'white-space:nowrap;">{token}</td></tr>')
        else:
            out.append(
                f'<tr><td style="padding:5px 12px;text-align:right;color:#475569;">{text}</td>'
                f'<td style="padding:5px 12px;text-align:right;font-weight:600;'
                f'white-space:nowrap;">{token}</td></tr>')
    return "\n      ".join(out)


def _party_block(doc_type, opts, label_style=""):
    heading = "Bill To" if doc_type == "sales" else "From"
    parts = [f'<div style="{label_style}">{heading}</div>',
             '<div style="font-weight:700;font-size:15px;margin:4px 0 2px;">{{party_name}}</div>']
    if opts.get("show_party_address"):
        parts.append('<div style="color:#475569;">{{party_address}}</div>')
        parts.append('<div style="color:#475569;">{{party_city}}</div>')
    if opts.get("show_party_contact"):
        parts.append('<div style="color:#475569;">{{party_phone}}</div>')
        parts.append('<div style="color:#475569;">{{party_email}}</div>')
    if opts.get("show_party_tax_id"):
        parts.append('<div style="color:#475569;margin-top:3px;">NTN {{party_tax_id}}</div>')
    return "\n        ".join(parts)


def _meta_rows(opts, muted="#64748b"):
    rows = [f'<div><span style="color:{muted};">Invoice #</span> '
            '<strong>{{invoice_no}}</strong></div>',
            f'<div><span style="color:{muted};">Date</span> '
            '<strong>{{invoice_date}}</strong></div>']
    if opts.get("show_due_date"):
        rows.append(f'<div><span style="color:{muted};">Due</span> '
                    '<strong>{{due_date}}</strong></div>')
    if opts.get("show_status"):
        rows.append(f'<div><span style="color:{muted};">Status</span> '
                    '<strong>{{status}}</strong></div>')
    return "\n        ".join(rows)


def _footer(doc_type, opts, accent):
    """Notes stay with the content; signatures and the closing line sit at the
    foot of the page.

    Someone signs at the bottom of the sheet, not wherever the item list
    happened to stop — an invoice with three lines and one with twenty should
    put the signature in the same place. ``.inv-foot`` is pushed down by the
    flex rule in the sheet CSS.
    """
    out = []
    if opts.get("show_notes"):
        out.append(
            '<div style="margin-top:26px;padding-top:12px;border-top:1px solid #e2e8f0;'
            'font-size:12px;color:#475569;">'
            '<div style="font-weight:700;color:#0f172a;margin-bottom:3px;">Notes</div>'
            '{{notes}}</div>')

    bottom = []
    if opts.get("show_signature"):
        left = "Prepared By" if doc_type == "sales" else "Checked By"
        right = "Authorised Signatory" if doc_type == "sales" else "Approved By"
        bottom.append(
            '<table style="width:100%;margin-top:44px;border-collapse:collapse;">'
            '<tr>'
            f'<td style="width:45%;border-top:1px solid #94a3b8;padding-top:6px;'
            f'font-size:11px;color:#64748b;text-align:center;">{left}</td>'
            '<td style="width:10%;"></td>'
            f'<td style="width:45%;border-top:1px solid #94a3b8;padding-top:6px;'
            f'font-size:11px;color:#64748b;text-align:center;">{right}</td>'
            '</tr></table>')
    if opts.get("show_thanks"):
        msg = ("Thank you for your business." if doc_type == "sales"
               else "This document is for internal record.")
        bottom.append(
            f'<div style="margin-top:22px;text-align:center;font-size:11px;'
            f'color:{accent};letter-spacing:.4px;">{msg}</div>')
    if bottom:
        out.append('<div class="inv-foot">' + "\n    ".join(bottom) + '</div>')
    return "\n  ".join(out)


def _title(doc_type):
    return "SALES INVOICE" if doc_type == "sales" else "PURCHASE INVOICE"


def _logo(opts):
    return "{{company_logo}}" if opts.get("show_logo") else ""


def _company_tax(opts, style="color:#64748b;"):
    return (f'<div style="{style}">NTN {{{{company_tax_id}}}}</div>'
            if opts.get("show_company_tax_id") else "")


# ── Page geometry ───────────────────────────────────────────────────────────
# One sheet of A4. The printable strip inside 14mm margins is 182mm, and every
# design used to be laid out at max-width:820px (~217mm), so wide tables ran off
# the right edge of the paper. Sizing in millimetres keeps the on-screen preview
# and the printed page the same shape.
#
# The rules below also carry the items table, because the three places that
# build one (this module's preview, invoices.py, purchase_invoice.py) all have
# to wrap identically or the preview stops predicting the print. Each of them
# emits class="inv-items" and marks its description cells "inv-desc"; the
# behaviour lives here so it is defined once.

A4_WIDTH_MM = 210
A4_MARGIN_MM = 14
A4_CONTENT_MM = A4_WIDTH_MM - 2 * A4_MARGIN_MM  # 182mm

_SHEET_CSS = """<style>
@page{size:A4;margin:14mm}
.inv-sheet{width:210mm;min-height:297mm;box-sizing:border-box;padding:14mm;
  margin:0 auto;background:#fff;color:#0f172a;font-size:13px;line-height:1.5;
  font-family:Inter,Segoe UI,Arial,sans-serif;
  position:relative;display:flex;flex-direction:column}
.inv-sheet *{box-sizing:border-box}
/* Flex items do not shrink-to-fit the way blocks do; without this a table or a
   heading can end up narrower than the page. */
.inv-sheet>*{flex:none;width:100%}

/* Signatures and the closing line belong at the foot of the sheet, not
   wherever the item list happened to stop. The bottom padding is the strip the
   page number sits in, so the closing line does not share its baseline. */
.inv-sheet>.inv-foot{margin-top:auto;padding-top:10mm;padding-bottom:7mm}

/* Page numbers, bottom-right. Stamped by script because the count depends on
   how far the content actually runs, and Chromium does not implement the
   @page margin boxes that would otherwise carry counter(page). */
.inv-pagenums{position:absolute;left:14mm;right:14mm;top:14mm;bottom:14mm;
  width:auto;pointer-events:none}
.inv-pageno{position:absolute;right:0;font-size:9px;color:#94a3b8;
  letter-spacing:.3px;white-space:nowrap}

/* Items table: columns size themselves to their content, and only the
   description is allowed to wrap. Without the nowrap a long header squeezes a
   money column until "1,234,567.00" breaks across two lines.
   Size is set once, on the table — cells inherit it and pad in em — so the
   auto-fit below can resize the whole table by changing a single property. */
.inv-sheet table.inv-items{width:100%;border-collapse:collapse;table-layout:auto}
.inv-sheet table.inv-items th,
.inv-sheet table.inv-items td{white-space:nowrap;overflow-wrap:normal}
/* Headers read as labels for the column, so they sit centred in the cell both
   ways — including the tall ones that wrap to two or three lines. The body
   cells keep their own alignment: numbers right, codes left. */
.inv-sheet table.inv-items th{white-space:normal;word-break:normal;
  overflow-wrap:break-word;text-align:center;vertical-align:middle;
  line-height:1.25;font-size:.95em}
/* Every other column takes exactly the width its content needs — width:1% on a
   nowrap cell resolves to min-content — so the slack all lands on the
   description instead of being shared out and leaving it to wrap early. */
.inv-sheet table.inv-items th:not(.inv-desc),
.inv-sheet table.inv-items td:not(.inv-desc){width:1%}
/* The description absorbs the width the other columns do not need, and is the
   one cell that may run to a second line. */
.inv-sheet table.inv-items td.inv-desc{white-space:normal;overflow-wrap:break-word;
  width:100%;min-width:22mm}
.inv-sheet table.inv-items th.inv-desc{width:100%;min-width:22mm}
.inv-sheet table.inv-items tr{page-break-inside:avoid}
.inv-sheet table.inv-items thead{display:table-header-group}

/* The summary stays pinned to the right edge at a fixed width, so adding or
   removing item columns moves it not at all. */
.inv-sheet table.inv-totals{width:100%;border-collapse:collapse;margin-top:14px;
  page-break-inside:avoid}
.inv-sheet table.inv-totals>tbody>tr>td.inv-totals-gap{width:auto}
.inv-sheet table.inv-totals>tbody>tr>td.inv-totals-box{width:72mm}

@media print{
  /* The @page margin is the paper margin; a second one on the sheet would
     double it and push the table off the page again. The scale applied for
     small screens has no business on paper, where the sheet is already the
     size of the page. */
  .inv-sheet{width:auto;min-height:0;padding:0;margin:0!important;
    transform:none!important;zoom:1!important}
  /* Padding is gone on paper — the @page margin is doing that job — so the
     number strip has to sit flush with the content box instead. */
  .inv-pagenums{left:0;right:0;top:0;bottom:0}
  .inv-sheet table.inv-items th{background:#1e293b!important;color:#fff!important;
    -webkit-print-color-adjust:exact;print-color-adjust:exact}
}
</style>"""

# Auto-fit. The starting size below is chosen for the column count, but the
# column count is only half the story — an invoice whose amounts run to eight
# digits needs more room per column than one whose amounts run to five, and no
# formula on the server knows how wide the text will actually set. So the page
# measures itself once it is laid out and steps the type down until the table
# is inside the printable width. Everything stays legible-by-measurement rather
# than legible-by-guess, and nothing is ever clipped.
#
# Ships with the sheet so it applies wherever a design is rendered — the
# settings preview and both print pages — instead of being wired up three
# times and drifting apart.
_SHEET_FIT_JS = """<script>
(function () {
  function fitOne(sheet, tbl) {
    var cs = window.getComputedStyle(sheet);
    var avail = sheet.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight);
    if (!avail || avail <= 0) { return; }
    var start = parseFloat(tbl.getAttribute('data-basesize') || '0');
    if (!start) {
      start = parseFloat(window.getComputedStyle(tbl).fontSize) || 11;
      tbl.setAttribute('data-basesize', start);
    }
    var size = start;
    tbl.style.fontSize = size + 'px';
    // Step down until it fits. The floor is the point past which the paper is
    // not readable anyway; below it, let the description column wrap harder
    // rather than shrink the type into illegibility.
    for (var i = 0; i < 60 && tbl.scrollWidth > avail + 1 && size > 5.5; i++) {
      size = Math.round((size - 0.25) * 100) / 100;
      tbl.style.fontSize = size + 'px';
    }
    if (tbl.scrollWidth > avail + 1) {
      tbl.style.tableLayout = 'fixed';
      tbl.style.width = '100%';
    }
  }
  // A sheet is a fixed 210mm because that is what A4 is. On any screen narrower
  // than that — a phone, a split pane, the settings preview — show the whole
  // page scaled down rather than letting it hang off the side or reflowing it
  // into something that is no longer A4. What you see stays the page, just
  // smaller, so the preview still answers "does this fit on paper".
  //
  // zoom rather than transform: zoom shrinks the layout box, so the document
  // ends where the sheet ends. A transform only repaints, leaving the full
  // 297mm reserved and a screenful of dead space under a scaled-down page.
  var CAN_ZOOM = (typeof document.documentElement.style.zoom !== 'undefined');

  function unscale(sheet) {
    sheet.style.zoom = '';
    sheet.style.transform = '';
    sheet.style.marginRight = '';
    sheet.style.marginBottom = '';
  }

  function scaleSheet(sheet) {
    if (window.matchMedia && window.matchMedia('print').matches) { return; }
    var box = sheet.parentElement || document.body;
    var avail = box.clientWidth;
    var natural = sheet.offsetWidth;
    if (!avail || !natural || avail >= natural) { return; }
    var k = avail / natural;
    if (CAN_ZOOM) { sheet.style.zoom = k; return; }
    // Fallback for engines without zoom: transform, then pull the leftover
    // width and height back in so nothing scrolls to empty space.
    sheet.style.transformOrigin = 'top left';
    sheet.style.transform = 'scale(' + k + ')';
    sheet.style.marginRight = -Math.round(natural - avail) + 'px';
    sheet.style.marginBottom = -Math.round(sheet.offsetHeight * (1 - k)) + 'px';
  }

  // Millimetres in CSS pixels, measured rather than assumed — a page zoom or a
  // non-96dpi setting changes it.
  function mmPx() {
    var probe = document.createElement('div');
    probe.style.cssText = 'position:absolute;visibility:hidden;width:100mm;height:0';
    document.body.appendChild(probe);
    var px = probe.offsetWidth / 100;
    document.body.removeChild(probe);
    return px || (96 / 25.4);
  }

  // A4 minus the 14mm margins top and bottom: the strip of paper one page of
  // content actually gets.
  var PAGE_CONTENT_MM = 297 - 28;

  function stampPages(sheet) {
    var host = sheet.querySelector('.inv-pagenums');
    if (!host) { return; }
    host.innerHTML = '';
    var mm = mmPx();
    var per = PAGE_CONTENT_MM * mm;
    if (!per || !host.clientHeight) { return; }
    var printing = !!(window.matchMedia && window.matchMedia('print').matches);

    // A hair of tolerance, or a page that fills exactly to its margin claims a
    // second, empty one.
    var pages = Math.max(1, Math.ceil((host.clientHeight - 2) / per));

    // On screen the sheet is one continuous block, so round it up to whole
    // pages. That keeps it looking like the stack of paper it will print as,
    // and it puts the signature block — which floats to the bottom — at the
    // foot of the last page rather than partway down it.
    // On paper the browser is already doing the pagination; growing the sheet
    // there would just add a blank page at the end.
    sheet.style.minHeight = printing ? '' : (pages * per + 28 * mm) + 'px';

    var total = host.clientHeight;
    for (var i = 0; i < pages; i++) {
      var tag = document.createElement('div');
      tag.className = 'inv-pageno';
      tag.textContent = 'Page ' + (i + 1) + ' of ' + pages;
      host.appendChild(tag);
      // Sit the tag's baseline just inside the bottom edge of each page's
      // content strip — measured from its own height, so it is never half cut
      // off by the edge — and never past where the content actually ends, or
      // the last one is stranded outside the sheet and never drawn.
      var h = tag.offsetHeight || 12;
      var y = (i + 1) * per - h - 1;
      tag.style.top = Math.max(0, Math.min(y, total - h - 1)) + 'px';
    }
  }

  // A sheet is usually laid out inside something not visible yet — the preview
  // pane behind a tab, the mobile preview before it slides up. Measured while
  // hidden every width is zero, so the fit is skipped; nothing then fires when
  // the container is finally shown and the page is left at full 210mm inside a
  // phone-width frame, overflowing to the right with the summary off-screen.
  // Watching the box it sits in catches that moment and every later resize.
  //
  // Wired from fitAll rather than on load: this script is emitted ahead of the
  // sheet markup, so at the time it first runs there is no sheet to observe.
  var ro = null;
  function observeSheets(sheets) {
    if (!window.ResizeObserver) { return; }
    if (!ro) {
      ro = new ResizeObserver(function (entries) {
        // Width is what drives the scale. Refitting on a height change would
        // chase the height the fit itself just produced.
        var moved = false;
        for (var i = 0; i < entries.length; i++) {
          var el = entries[i].target;
          var w = el.clientWidth;
          if (el.__invLastW !== w) { el.__invLastW = w; moved = true; }
        }
        if (!moved) { return; }
        clearTimeout(window.__invFitT);
        window.__invFitT = setTimeout(fitAll, 60);
      });
    }
    for (var s = 0; s < sheets.length; s++) {
      var box = sheets[s].parentElement;
      if (box && !box.__invObserved) {
        box.__invObserved = true;
        box.__invLastW = box.clientWidth;
        ro.observe(box);
      }
    }
  }

  function fitAll() {
    var sheets = document.querySelectorAll('.inv-sheet');
    observeSheets(sheets);
    for (var s = 0; s < sheets.length; s++) {
      // Measure at true size: a scale left over from the last pass would make
      // the sheet look narrower than A4 and shrink the type for no reason.
      unscale(sheets[s]);
      // Type first — the table has to fit the sheet's own 182mm whatever size
      // the sheet is being displayed at.
      var tables = sheets[s].querySelectorAll('table.inv-items');
      for (var t = 0; t < tables.length; t++) { fitOne(sheets[s], tables[t]); }
      // Then count pages, which depends on the height the fitted type produced.
      stampPages(sheets[s]);
      scaleSheet(sheets[s]);
    }
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', fitAll);
  } else {
    fitAll();
  }
  window.addEventListener('load', fitAll);
  // ResizeObserver is the right tool for "the pane just became visible", but it
  // is suspended while a tab is not rendering and absent on older engines. A
  // few settling passes cover the common case — a preview revealed a moment
  // after load — without depending on it. Bounded, so this cannot become a
  // polling loop.
  var settleAt = [200, 600, 1500];
  for (var q = 0; q < settleAt.length; q++) { setTimeout(fitAll, settleAt[q]); }
  // Print geometry differs from screen geometry, so measure again for paper.
  if (window.matchMedia) {
    try { window.matchMedia('print').addListener(fitAll); } catch (e) {}
  }
  window.addEventListener('beforeprint', fitAll);
  window.addEventListener('resize', function () {
    clearTimeout(window.__invFitT);
    window.__invFitT = setTimeout(fitAll, 120);
  });

  window.invoiceFitSheets = fitAll;
})();
</script>"""

# Stamped into every generated body so ``refresh_designs`` can tell which
# templates were built by the current layout. Bump it whenever the sheet
# changes in a way already-saved templates need to pick up.
SHEET_MARKER = "inv-sheet-v1"

_PAGE_OPEN = _SHEET_CSS + _SHEET_FIT_JS + f'<div class="inv-sheet {SHEET_MARKER}">'
# The number strip is a sibling of the content, filled in by the script above.
_PAGE_CLOSE = '<div class="inv-pagenums" aria-hidden="true"></div></div>'


# ── Items table sizing ──────────────────────────────────────────────────────
# Base layout is 10 columns (#, SKU, Description, Qty, Unit, Per Unit Price,
# Amount Excl., Total Sales Tax, Amount Incl., Total). Per-line discount, tax
# and charges add two or three more each, up to 17 on a purchase invoice.

_BASE_COLUMNS = 10


def items_table_metrics(n_cols):
    """Starting type size and cell padding for an ``n_cols`` items table.

    A starting point, not the final word: the auto-fit in ``_SHEET_FIT_JS``
    measures the rendered table and steps this down further if the real data is
    wider than the column count alone suggests. Picking a sensible start still
    matters — it is what a PDF generated without scripting gets, and it keeps
    the common cases from being resized at all.

    Padding is in em so it tracks the font size; the size itself goes on the
    table, once, and the cells inherit it.
    """
    n = max(1, int(n_cols or 0))
    over = max(0, n - _BASE_COLUMNS)
    font = max(7.0, 11.0 - 0.55 * over)
    if over == 0:
        pad = "0.5em 0.7em"
    elif over <= 3:
        pad = "0.45em 0.55em"
    elif over <= 5:
        pad = "0.4em 0.45em"
    else:
        pad = "0.35em 0.4em"
    return {"font": round(font, 1), "pad": pad}


def _design_classic(doc_type, opts, accent):
    return f"""{_PAGE_OPEN}
  <div style="text-align:center;padding-bottom:16px;">
    {_logo(opts)}
    <div style="font-size:24px;font-weight:800;letter-spacing:.5px;margin-top:6px;">{{{{company_name}}}}</div>
    <div style="color:#475569;margin-top:3px;">{{{{company_address}}}}, {{{{company_city}}}}</div>
    <div style="color:#475569;">{{{{company_phone}}}} &nbsp;&bull;&nbsp; {{{{company_email}}}}</div>
    {_company_tax(opts)}
  </div>
  <div style="border-top:3px double {accent};margin:4px 0 18px;"></div>
  <div style="text-align:center;font-size:15px;font-weight:800;letter-spacing:3px;color:{accent};margin-bottom:18px;">{_title(doc_type)}</div>
  <table style="width:100%;border-collapse:collapse;margin-bottom:18px;">
    <tr>
      <td style="width:55%;vertical-align:top;">
        {_party_block(doc_type, opts, "font-size:10px;font-weight:700;letter-spacing:1px;color:#64748b;text-transform:uppercase;")}
      </td>
      <td style="width:45%;vertical-align:top;text-align:right;line-height:1.9;">
        {_meta_rows(opts)}
      </td>
    </tr>
  </table>
  {{{{items_table}}}}
  {{{{totals_table}}}}
  {_footer(doc_type, opts, accent)}
{_PAGE_CLOSE}"""


def _design_modern(doc_type, opts, accent):
    return f"""{_PAGE_OPEN}
  <div style="height:6px;background:{accent};border-radius:3px;margin-bottom:22px;"></div>
  <table style="width:100%;border-collapse:collapse;margin-bottom:24px;">
    <tr>
      <td style="width:55%;vertical-align:top;">
        {_logo(opts)}
        <div style="font-size:20px;font-weight:800;margin-top:4px;">{{{{company_name}}}}</div>
        <div style="color:#475569;margin-top:3px;">{{{{company_address}}}}, {{{{company_city}}}}</div>
        <div style="color:#475569;">{{{{company_phone}}}} &nbsp;&bull;&nbsp; {{{{company_email}}}}</div>
        {_company_tax(opts)}
      </td>
      <td style="width:45%;vertical-align:top;text-align:right;">
        <div style="font-size:26px;font-weight:800;color:{accent};letter-spacing:1px;">{_title(doc_type)}</div>
        <div style="margin-top:10px;line-height:1.9;">
        {_meta_rows(opts)}
        </div>
      </td>
    </tr>
  </table>
  <div style="background:#f8fafc;border-left:3px solid {accent};padding:12px 14px;border-radius:0 6px 6px 0;margin-bottom:20px;">
    {_party_block(doc_type, opts, "font-size:10px;font-weight:700;letter-spacing:1px;color:" + accent + ";text-transform:uppercase;")}
  </div>
  {{{{items_table}}}}
  {{{{totals_table}}}}
  {_footer(doc_type, opts, accent)}
{_PAGE_CLOSE}"""


def _design_minimal(doc_type, opts, accent):
    return f"""{_PAGE_OPEN}
  <table style="width:100%;border-collapse:collapse;margin-bottom:40px;">
    <tr>
      <td style="width:60%;vertical-align:top;">
        {_logo(opts)}
        <div style="font-size:16px;font-weight:700;letter-spacing:.3px;margin-top:4px;">{{{{company_name}}}}</div>
        <div style="color:#94a3b8;font-size:12px;margin-top:2px;">{{{{company_address}}}}, {{{{company_city}}}}</div>
        <div style="color:#94a3b8;font-size:12px;">{{{{company_phone}}}} &nbsp;&bull;&nbsp; {{{{company_email}}}}</div>
        {_company_tax(opts, "color:#94a3b8;font-size:12px;")}
      </td>
      <td style="width:40%;vertical-align:top;text-align:right;">
        <div style="font-size:11px;font-weight:700;letter-spacing:3px;color:#94a3b8;">{_title(doc_type)}</div>
        <div style="margin-top:8px;line-height:1.9;font-size:12px;">
        {_meta_rows(opts, "#94a3b8")}
        </div>
      </td>
    </tr>
  </table>
  <div style="margin-bottom:24px;">
    {_party_block(doc_type, opts, "font-size:10px;font-weight:700;letter-spacing:1.5px;color:#94a3b8;text-transform:uppercase;")}
  </div>
  {{{{items_table}}}}
  {{{{totals_table}}}}
  {_footer(doc_type, opts, accent)}
{_PAGE_CLOSE}"""


def _design_bold(doc_type, opts, accent):
    return f"""{_PAGE_OPEN}
  <table style="width:100%;border-collapse:collapse;background:{accent};border-radius:8px;margin-bottom:24px;">
    <tr>
      <td style="padding:22px 24px;vertical-align:middle;">
        {_logo(opts)}
        <div style="font-size:22px;font-weight:800;color:#fff;margin-top:4px;">{{{{company_name}}}}</div>
        <div style="color:rgba(255,255,255,.85);font-size:12px;margin-top:3px;">{{{{company_address}}}}, {{{{company_city}}}}</div>
        <div style="color:rgba(255,255,255,.85);font-size:12px;">{{{{company_phone}}}} &nbsp;&bull;&nbsp; {{{{company_email}}}}</div>
      </td>
      <td style="padding:22px 24px;vertical-align:middle;text-align:right;">
        <div style="font-size:22px;font-weight:800;color:#fff;letter-spacing:1px;">{_title(doc_type)}</div>
        <div style="margin-top:8px;line-height:1.9;color:#fff;font-size:12px;">
        {_meta_rows(opts, "rgba(255,255,255,.72)")}
        </div>
      </td>
    </tr>
  </table>
  <table style="width:100%;border-collapse:collapse;margin-bottom:20px;">
    <tr>
      <td style="width:60%;vertical-align:top;">
        {_party_block(doc_type, opts, "font-size:10px;font-weight:700;letter-spacing:1px;color:" + accent + ";text-transform:uppercase;")}
      </td>
      <td style="width:40%;vertical-align:top;text-align:right;">
        {_company_tax(opts, "color:#64748b;font-size:12px;")}
      </td>
    </tr>
  </table>
  {{{{items_table}}}}
  {{{{totals_table}}}}
  {_footer(doc_type, opts, accent)}
{_PAGE_CLOSE}"""


def _design_letterhead(doc_type, opts, accent):
    """Logo left, company name centred, invoice number and date right.

    The three-cell top row is a table rather than flexbox because this markup
    is also read by print engines and by whatever a user pastes it into; a
    table row is the one layout every one of them agrees on. The logo and meta
    cells are held to a fixed width so the centred name stays centred on the
    page whether or not a logo has been uploaded.
    """
    logo = _logo(opts)
    return f"""{_PAGE_OPEN}
  <table style="width:100%;border-collapse:collapse;">
    <tr>
      <td style="width:34mm;vertical-align:middle;">{logo}</td>
      <td style="vertical-align:middle;text-align:center;padding:0 4mm;">
        <div style="font-size:22px;font-weight:800;letter-spacing:.3px;line-height:1.2;">{{{{company_name}}}}</div>
        <div style="color:#475569;font-size:11.5px;margin-top:3px;">{{{{company_address}}}}, {{{{company_city}}}}</div>
        <div style="color:#475569;font-size:11.5px;">{{{{company_phone}}}} &nbsp;&bull;&nbsp; {{{{company_email}}}}</div>
        {_company_tax(opts, "color:#64748b;font-size:11.5px;")}
      </td>
      <!-- Balances the logo cell so the company name is centred on the page,
           not on the space left over beside the logo. The invoice number and
           date live in the Bill To row below, once. -->
      <td style="width:34mm;"></td>
    </tr>
  </table>
  <div style="border-top:2px solid {accent};margin:10px 0 0;"></div>
  <div style="text-align:center;font-size:13px;font-weight:800;letter-spacing:2.5px;
    color:{accent};margin:12px 0 16px;">{_title(doc_type)}</div>
  <table style="width:100%;border-collapse:collapse;margin-bottom:16px;">
    <tr>
      <td style="width:55%;vertical-align:top;">
        {_party_block(doc_type, opts, "font-size:10px;font-weight:700;letter-spacing:1px;color:#64748b;text-transform:uppercase;")}
      </td>
      <td style="width:45%;vertical-align:top;text-align:right;line-height:1.9;">
        {_meta_rows(opts)}
      </td>
    </tr>
  </table>
  {{{{items_table}}}}
  {{{{totals_table}}}}
  {_footer(doc_type, opts, accent)}
{_PAGE_CLOSE}"""


_BUILDERS = {
    "classic": _design_classic,
    "modern": _design_modern,
    "minimal": _design_minimal,
    "bold": _design_bold,
    "letterhead": _design_letterhead,
}


def build_body(design, doc_type, accent_color, options):
    """Compile a design + toggles into template HTML.

    Called on save, so ``body_html`` is always the single thing the print path
    reads, regardless of how it was authored.
    """
    doc_type = "sales" if doc_type == "sales" else "purchase"
    builder = _BUILDERS.get(design, _design_classic)
    accent = accent_color if (accent_color or "").startswith("#") else "#0f766e"
    return builder(doc_type, normalise_options(doc_type, options), accent)


# ── Print-time totals table ─────────────────────────────────────────────────

def _bank_block(doc_type, opts, accent):
    """Where to send the money, printed opposite the totals.

    Sales only: a purchase invoice is the supplier's demand for payment, so
    printing our own account details on it would be telling ourselves where to
    pay. Empty when the toggle is off, which leaves the gap cell as it was.
    """
    if doc_type != "sales" or not opts.get("show_bank_details"):
        return ""
    label = ("font-size:10px;font-weight:700;letter-spacing:1px;"
             f"color:{accent};text-transform:uppercase;")
    row = "padding:1px 0;color:#475569;font-size:11.5px;"
    key = "padding:1px 10px 1px 0;color:#94a3b8;font-size:11.5px;white-space:nowrap;"
    return (
        '<div style="max-width:82mm;">'
        f'<div style="{label}">Payment Details</div>'
        '<table style="border-collapse:collapse;margin-top:5px;">'
        f'<tr><td style="{key}">Bank</td>'
        f'<td style="{row}font-weight:600;color:#0f172a;">{{{{company_bank_name}}}}</td></tr>'
        f'<tr><td style="{key}">Title</td>'
        f'<td style="{row}">{{{{company_bank_account_title}}}}</td></tr>'
        f'<tr><td style="{key}">Account #</td>'
        f'<td style="{row}">{{{{company_bank_account_number}}}}</td></tr>'
        '</table></div>')


# The gap cell takes whatever is left over, so the summary sits hard against
# the right edge at a fixed 72mm however many item columns are on the page.
# Anything in that cell — the bank block — grows leftwards and cannot push the
# summary out of position.
_TABLE_CLOSE = '\n</table></td></tr></table>'


def _table_open(gap_content=""):
    """The totals block, styled inline as well as by class.

    The classes let the sheet refine it, but the layout cannot depend on them:
    a hand-written template still gets ``{{totals_table}}`` substituted into
    HTML that carries none of the sheet's CSS, and without an explicit width
    the table shrinks to its contents and the summary lands on the left.
    """
    return ('<table class="inv-totals" style="width:100%;border-collapse:collapse;'
            'margin-top:14px;"><tr>'
            f'<td class="inv-totals-gap" style="vertical-align:top;">{gap_content}</td>'
            '<td class="inv-totals-box" style="width:72mm;vertical-align:top;">'
            '<table style="width:100%;border-collapse:collapse;">\n')


def build_totals_table(doc_type, opts, accent):
    """Generate the totals section HTML for a given set of options.

    Called at print time so the same template can render either combined or
    item-wise totals depending on the invoice's mode per section.
    """
    return (_table_open(_bank_block(doc_type, opts, accent))
            + _totals_rows(doc_type, opts, accent) + _TABLE_CLOSE)


# ── Preview ─────────────────────────────────────────────────────────────────

def _sample_items_table(doc_type="sales", opts=None):
    """Mirrors the items table the print routes build, so the preview shows the
    real thing rather than an approximation of it.

    When ``opts`` contains display-mode choices (``discount_display``,
    ``tax_display``, ``charges_display``) set to ``"per_line"``, extra columns
    are added to the table head and body.
    """
    if opts is None:
        opts = {}
    show_disc_col = opts.get("show_discount") and opts.get("discount_display") == "per_line"
    show_tax_col = opts.get("show_tax") and opts.get("tax_display") == "per_line"
    show_chg_col = opts.get("charges_display") == "per_line"

    # (n, sku, desc, qty, unit, price, disc_pct, disc_amt, tax_pct,
    #  chg1, chg2, chg3)
    if doc_type == "sales":
        items = [
            (1, "SKU-1001", "Solar Panel 550W Monocrystalline", 12, "pcs", 24500.00,
             5.0, 14700.00, 16.0, 4000.00, 8333.00, 0),
            (2, "SKU-1002", "Hybrid Inverter 8kW", 2, "pcs", 185000.00,
             5.0, 18500.00, 16.0, 6000.00, 5000.00, 0),
            (3, "SKU-1180", "Mounting Rail 4.2m Aluminium", 30, "pcs", 3200.00,
             5.0, 4800.00, 16.0, 2000.00, 3334.00, 0),
        ]
    else:
        items = [
            (1, "SKU-1001", "Solar Panel 550W Monocrystalline", 12, "pcs", 24500.00,
             5.0, 14700.00, 16.0, 2833.00, 4667.00, 1400.00),
            (2, "SKU-1002", "Hybrid Inverter 8kW", 2, "pcs", 185000.00,
             5.0, 18500.00, 16.0, 3500.00, 5333.00, 1800.00),
            (3, "SKU-1180", "Mounting Rail 4.2m Aluminium", 30, "pcs", 3200.00,
             5.0, 4800.00, 16.0, 2167.00, 4000.00, 1000.00),
        ]

    n_cols = (_BASE_COLUMNS
              + (2 if show_disc_col else 0)
              + (2 if show_tax_col else 0)
              + ((2 if doc_type == "sales" else 3) if show_chg_col else 0))
    m = items_table_metrics(n_cols)

    tds = f"padding:{m['pad']};border:1px solid #e2e8f0;white-space:nowrap;"
    tdc = tds + "text-align:center;"
    tdr = tds + "text-align:right;"

    # Accumulators for the totals row
    tot_qty = 0
    tot_disc_amt = 0.0
    tot_excl = 0.0
    tot_chg1 = 0.0
    tot_chg2 = 0.0
    tot_chg3 = 0.0
    tot_tax_amt = 0.0
    tot_incl = 0.0
    tot_line = 0.0

    body = ""
    for n, sku, desc, qty, unit, price, dp, da, tp, c1, c2, c3 in items:
        line_total = price * qty
        amt_excl = line_total - da
        tax_amt = amt_excl * tp / 100
        amt_incl = amt_excl + tax_amt

        tot_qty += qty
        tot_disc_amt += da
        tot_excl += amt_excl
        tot_chg1 += c1
        tot_chg2 += c2
        tot_chg3 += c3
        tot_tax_amt += tax_amt
        tot_incl += amt_incl
        tot_line += line_total

        cells = (
            f"<td style='{tdc}'>{n}</td>"
            f"<td style='{tds}'>{sku}</td>"
            f"<td class='inv-desc' style='{tds}white-space:normal;'>{desc}</td>"
            f"<td style='{tdc}'>{qty}</td>"
            f"<td style='{tdc}'>{unit}</td>"
            f"<td style='{tdr}'>{_m(price)}</td>")
        if show_disc_col:
            cells += f"<td style='{tdr}'>{dp:.1f}%</td>"
            cells += f"<td style='{tdr}'>{_m(da)}</td>"
        cells += f"<td style='{tdr}'>{_m(amt_excl)}</td>"
        if show_tax_col:
            cells += f"<td style='{tdr}'>{tp:.1f}%</td>"
            cells += f"<td style='{tdr}'>{_m(tax_amt)}</td>"
        if show_chg_col:
            if doc_type == "sales":
                cells += f"<td style='{tdr}'>{_m(c1)}</td>"
                cells += f"<td style='{tdr}'>{_m(c2)}</td>"
            else:
                cells += f"<td style='{tdr}'>{_m(c1)}</td>"
                cells += f"<td style='{tdr}'>{_m(c2)}</td>"
                cells += f"<td style='{tdr}'>{_m(c3)}</td>"
        cells += f"<td style='{tdr}'>{_m(tax_amt)}</td>"
        cells += f"<td style='{tdr}'>{_m(amt_incl)}</td>"
        cells += f"<td style='{tdr}'>{_m(line_total)}</td>"
        body += "<tr>" + cells + "</tr>"

    # ── Totals footer row ────────────────────────────────────────────
    tds_b = (f"padding:{m['pad']};border:1px solid #e2e8f0;font-weight:700;"
             f"background:#f1f5f9;white-space:nowrap;")
    tdr_b = tds_b + "text-align:right;"
    foot_cells = (
        f"<td style='{tds_b};text-align:center;'></td>"
        f"<td style='{tds_b}'></td>"
        f"<td class='inv-desc' style='{tds_b}'>Total</td>"
        f"<td style='{tds_b};text-align:center;'>{tot_qty}</td>"
        f"<td style='{tds_b}'></td>"
        f"<td style='{tdr_b}'></td>")
    if show_disc_col:
        foot_cells += f"<td style='{tdr_b}'></td>"
        foot_cells += f"<td style='{tdr_b}'>{_m(tot_disc_amt)}</td>"
    foot_cells += f"<td style='{tdr_b}'>{_m(tot_excl)}</td>"
    if show_tax_col:
        foot_cells += f"<td style='{tdr_b}'></td>"
        foot_cells += f"<td style='{tdr_b}'>{_m(tot_tax_amt)}</td>"
    if show_chg_col:
        if doc_type == "sales":
            foot_cells += f"<td style='{tdr_b}'>{_m(tot_chg1)}</td>"
            foot_cells += f"<td style='{tdr_b}'>{_m(tot_chg2)}</td>"
        else:
            foot_cells += f"<td style='{tdr_b}'>{_m(tot_chg1)}</td>"
            foot_cells += f"<td style='{tdr_b}'>{_m(tot_chg2)}</td>"
            foot_cells += f"<td style='{tdr_b}'>{_m(tot_chg3)}</td>"
    foot_cells += f"<td style='{tdr_b}'>{_m(tot_tax_amt)}</td>"
    foot_cells += f"<td style='{tdr_b}'>{_m(tot_incl)}</td>"
    foot_cells += f"<td style='{tdr_b}'>{_m(tot_line)}</td>"

    # ── Header ───────────────────────────────────────────────────────
    # One style for every header: centred in the cell both ways.
    hd = (f"padding:{m['pad']};border:1px solid #1e293b;white-space:normal;"
          "text-align:center;vertical-align:middle;")

    head = (
        f"<th style='{hd}'>#</th>"
        f"<th style='{hd}'>SKU</th>"
        f"<th class='inv-desc' style='{hd}'>Description</th>"
        f"<th style='{hd}'>Qty</th>"
        f"<th style='{hd}'>Unit</th>"
        f"<th style='{hd}'>Per Unit Price</th>")
    if show_disc_col:
        head += f"<th style='{hd}'>Discount %</th>"
        head += f"<th style='{hd}'>Discount allowed</th>"
    head += f"<th style='{hd}'>Amount Excl. of Sales Tax</th>"
    if show_tax_col:
        head += f"<th style='{hd}'>Sales Tax %</th>"
        head += f"<th style='{hd}'>Sales Tax Amount per Unit</th>"
    if show_chg_col:
        if doc_type == "sales":
            head += f"<th style='{hd}'>Carriage Expense</th>"
            head += f"<th style='{hd}'>Installation</th>"
        else:
            head += f"<th style='{hd}'>Commission</th>"
            head += f"<th style='{hd}'>Freight</th>"
            head += f"<th style='{hd}'>Ld/Unld</th>"
    head += f"<th style='{hd}'>Total Sales Tax</th>"
    head += f"<th style='{hd}'>Amount Incl. of Sales Tax</th>"
    head += f"<th style='{hd}'>Total</th>"

    return (
        f'<table class="inv-items" style="width:100%;border-collapse:collapse;'
        f'font-size:{m["font"]}px;">'
        '<thead><tr style="background:#1e293b;color:#fff;">' + head +
        '</tr></thead><tbody>' + body +
        '<tr>' + foot_cells + '</tr></tbody></table>')


def sample_context(doc_type, company=None, opts=None):
    """Realistic stand-in data for the live preview.

    Uses the real company profile wherever it is filled in, so the preview shows
    the user their own letterhead rather than a fictional one.

    If ``opts`` is given it is passed through to ``_sample_items_table`` so
    display-mode choices are reflected in the preview.
    """
    def c(attr, fallback):
        val = getattr(company, attr, None) if company else None
        return val or fallback

    logo = ""
    if company is not None and getattr(company, "logo_url", None):
        logo = f'<img src="{_esc_attr(company.logo_url)}" style="max-height:60px;" alt="Logo">'

    party = ({"name": "Meezan Traders (Pvt) Ltd", "addr": "14-B Gulberg III",
              "city": "Lahore", "phone": "+92 42 3577 1200",
              "email": "accounts@meezantraders.pk", "tax": "3520112-8"}
             if doc_type == "sales" else
             {"name": "Zenith Solar Supplies", "addr": "Plot 22, SITE Area",
              "city": "Karachi", "phone": "+92 21 3255 8800",
              "email": "sales@zenithsolar.pk", "tax": "1790443-2"})

    return {
        "company_logo": logo,
        "company_name": c("company_name", "Your Company Name"),
        "company_address": c("address", "123 Business Road"),
        "company_city": c("city", "Lahore"),
        "company_phone": c("phone", "+92 42 1234 5678"),
        "company_email": c("email", "info@yourcompany.pk"),
        "company_tax_id": c("tax_id", "1234567-8"),
        "company_bank_name": c("bank_name", "Meezan Bank Ltd, Gulberg Branch"),
        "company_bank_account_title": c("bank_account_title",
                                        c("company_name", "Your Company Name")),
        "company_bank_account_number": c("bank_account_number", "0102-0101234567-01"),
        "invoice_no": "VCH-202607-0042" if doc_type == "sales" else "PINV-202607-0042",
        "invoice_date": datetime.utcnow().strftime("%d-%b-%Y"),
        "due_date": datetime.utcnow().strftime("%d-%b-%Y"),
        "status": "Approved",
        "party_name": party["name"],
        "party_address": party["addr"],
        "party_city": party["city"],
        "party_phone": party["phone"],
        "party_email": party["email"],
        "party_tax_id": party["tax"],
        "items_table": _sample_items_table(doc_type, opts),
        "totals_table": build_totals_table(doc_type, opts or default_options(doc_type), "#0f766e"),
        # Formatted through the shared formatter, so the design preview shows
        # the company's own number convention rather than a western sample.
        "subtotal": _m(760000),
        "discount": _m(38000),
        "tax": _m(122760),
        "delivery_charges": _m(12000),
        "installation_charges": _m(25000),
        "commission": _m(8500),
        "freight": _m(14000),
        "loading_unloading": _m(4200),
        "withholding_tax": _m(7600),
        "grand_total": _m(881760),
        "notes": "Payment due within 30 days. Goods remain the property of the "
                 "seller until paid in full.",
    }


def render_invoice_template(body_html, ctx):
    """Replace {{placeholder}} tokens in body_html with values from ctx dict.

    Repeats until nothing more resolves, because some values are themselves
    markup carrying tokens: ``totals_table`` holds the bank block, which holds
    ``{{company_bank_name}}``. A single pass over the context substituted
    ``totals_table`` after it had already walked past ``company_bank_name``,
    so those tokens printed on the invoice as literal braces.

    Bounded so a value that contains its own token cannot spin forever.
    """
    for _pass in range(5):
        before = body_html
        for key, val in ctx.items():
            token = "{{" + key + "}}"
            if token in body_html:
                body_html = body_html.replace(token, "" if val is None else str(val))
        if body_html == before:
            break
    return body_html


# ── Model ───────────────────────────────────────────────────────────────────

class InvoiceTemplate(db.Model):
    __tablename__ = "invoice_templates"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)
    name = db.Column(db.String(100), nullable=False)
    type = db.Column(db.String(10), nullable=False)  # "sales" or "purchase"
    body_html = db.Column(db.Text, nullable=False)
    is_default = db.Column(db.Boolean, default=False)
    # How this template was authored. "custom" means body_html was hand-written
    # and must never be regenerated; anything else is compiled from the design.
    design = db.Column(db.String(20), default="classic")
    accent_color = db.Column(db.String(20), default="#0f766e")
    options_json = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    @property
    def is_custom(self):
        # A NULL design is a template that pre-dates the designs: its body_html
        # was hand-written, so it must be treated as custom. Defaulting it to a
        # design instead would silently overwrite the user's own HTML the first
        # time they opened it and pressed Save.
        return self.design in (None, "", "custom")

    @property
    def options(self):
        return normalise_options(self.type, self.options_json)

    def set_options(self, opts):
        self.options_json = json.dumps(opts)

    def recompile(self):
        """Regenerate body_html from the design. No-op for custom templates,
        whose body is the user's own and must survive untouched."""
        if self.is_custom:
            return
        self.body_html = build_body(self.design, self.type, self.accent_color,
                                    self.options)

    @classmethod
    def get_default(cls, doc_type):
        t = cls.query.filter_by(type=doc_type, is_default=True).first()
        if t:
            return t
        return cls.query.filter_by(type=doc_type).order_by(cls.id).first()

    @classmethod
    def default_body(cls, doc_type):
        """Starting HTML for a new template — the Classic design at defaults."""
        return build_body("classic", doc_type, "#0f766e", default_options(doc_type))

    @classmethod
    def refresh_designs(cls):
        """Recompile design-built templates whose body predates the current
        page layout.

        ``recompile`` otherwise only runs when someone opens a template and
        presses Save, so an existing install would keep printing the old
        layout — off the edge of the A4 sheet — until every template happened
        to be re-saved by hand. Sales and purchase templates both go through
        here, so neither is left behind.

        Hand-written bodies are skipped by ``recompile`` itself: they are the
        user's own HTML and are never regenerated.
        """
        changed = 0
        for t in cls.query.all():
            if t.is_custom or SHEET_MARKER in (t.body_html or ""):
                continue
            t.recompile()
            changed += 1
        if changed:
            db.session.commit()
        return changed

    @classmethod
    def _seed_one(cls, doc_type, name, accent_color, opts, is_default):
        """Create and add a single template row (helper for seed_defaults)."""
        t = cls(name=name, type=doc_type, design="classic",
                accent_color=accent_color,
                options_json=json.dumps(opts),
                is_default=is_default)
        t.recompile()
        db.session.add(t)

    @classmethod
    def seed_defaults(cls):
        """Give each document type two usable templates out of the box.

        *Combined* — discount, tax and charges appear as totals in the footer.
        *Item wise* — the same values appear per line as extra columns in the
        items table so the user can print either style without rebuilding a
        template from scratch.

        Without any template, printing on a fresh database silently produces
        nothing: the print routes look up a template and skip rendering when
        none exists.
        """
        for doc_type, combined_name, perline_name in (
                ("sales",  "Standard Sales Invoice",
                 "Sales Invoice (Item Wise)"),
                ("purchase", "Standard Purchase Invoice",
                 "Purchase Invoice (Item Wise)")):
            if cls.query.filter_by(type=doc_type).first():
                continue

            # Combined default (existing behaviour)
            opts = default_options(doc_type)
            cls._seed_one(doc_type, combined_name, "#0f766e", opts, is_default=True)

            # Per-line template
            per_opts = default_options(doc_type)
            per_opts.update(discount_display="per_line",
                            tax_display="per_line",
                            charges_display="per_line")
            cls._seed_one(doc_type, perline_name, "#b91c1c", per_opts,
                          is_default=False)
        db.session.commit()
