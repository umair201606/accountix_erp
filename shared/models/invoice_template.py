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
    out = []
    if opts.get("show_notes"):
        out.append(
            '<div style="margin-top:26px;padding-top:12px;border-top:1px solid #e2e8f0;'
            'font-size:12px;color:#475569;">'
            '<div style="font-weight:700;color:#0f172a;margin-bottom:3px;">Notes</div>'
            '{{notes}}</div>')
    if opts.get("show_signature"):
        left = "Prepared By" if doc_type == "sales" else "Checked By"
        right = "Authorised Signatory" if doc_type == "sales" else "Approved By"
        out.append(
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
        out.append(
            f'<div style="margin-top:22px;text-align:center;font-size:11px;'
            f'color:{accent};letter-spacing:.4px;">{msg}</div>')
    return "\n  ".join(out)


def _title(doc_type):
    return "SALES INVOICE" if doc_type == "sales" else "PURCHASE INVOICE"


def _logo(opts):
    return "{{company_logo}}" if opts.get("show_logo") else ""


def _company_tax(opts, style="color:#64748b;"):
    return (f'<div style="{style}">NTN {{{{company_tax_id}}}}</div>'
            if opts.get("show_company_tax_id") else "")


_PAGE_OPEN = ('<div style="font-family:Inter,Segoe UI,Arial,sans-serif;max-width:820px;'
              'margin:0 auto;padding:32px;color:#0f172a;font-size:13px;line-height:1.5;">')
_PAGE_CLOSE = "</div>"


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


_BUILDERS = {
    "classic": _design_classic,
    "modern": _design_modern,
    "minimal": _design_minimal,
    "bold": _design_bold,
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

_TABLE_OPEN = '<table style="width:100%;border-collapse:collapse;margin-top:16px;"><tr><td style="width:62%;"></td><td style="width:38%;"><table style="width:100%;border-collapse:collapse;">\n'
_TABLE_CLOSE = '\n</table></td></tr></table>'


def build_totals_table(doc_type, opts, accent):
    """Generate the totals section HTML for a given set of options.

    Called at print time so the same template can render either combined or
    item-wise totals depending on the invoice's mode per section.
    """
    return _TABLE_OPEN + _totals_rows(doc_type, opts, accent) + _TABLE_CLOSE


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

    tds = "padding:6px 8px;border:1px solid #e2e8f0;"
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
            f"<td style='{tds}'>{desc}</td>"
            f"<td style='{tdc}'>{qty}</td>"
            f"<td style='{tdc}'>{unit}</td>"
            f"<td style='{tdr}'>{price:,.2f}</td>")
        if show_disc_col:
            cells += f"<td style='{tdr}'>{dp:.1f}%</td>"
            cells += f"<td style='{tdr}'>{da:,.2f}</td>"
        cells += f"<td style='{tdr}'>{amt_excl:,.2f}</td>"
        if show_tax_col:
            cells += f"<td style='{tdr}'>{tp:.1f}%</td>"
            cells += f"<td style='{tdr}'>{tax_amt:,.2f}</td>"
        if show_chg_col:
            if doc_type == "sales":
                cells += f"<td style='{tdr}'>{c1:,.2f}</td>"
                cells += f"<td style='{tdr}'>{c2:,.2f}</td>"
            else:
                cells += f"<td style='{tdr}'>{c1:,.2f}</td>"
                cells += f"<td style='{tdr}'>{c2:,.2f}</td>"
                cells += f"<td style='{tdr}'>{c3:,.2f}</td>"
        cells += f"<td style='{tdr}'>{tax_amt:,.2f}</td>"
        cells += f"<td style='{tdr}'>{amt_incl:,.2f}</td>"
        cells += f"<td style='{tdr}'>{line_total:,.2f}</td>"
        body += "<tr>" + cells + "</tr>"

    # ── Totals footer row ────────────────────────────────────────────
    tds_b = "padding:6px 8px;border:1px solid #e2e8f0;font-weight:700;background:#f1f5f9;"
    tdr_b = tds_b + "text-align:right;"
    foot_cells = (
        f"<td style='{tds_b};text-align:center;'></td>"
        f"<td style='{tds_b}'></td>"
        f"<td style='{tds_b}'>Total</td>"
        f"<td style='{tds_b};text-align:center;'>{tot_qty}</td>"
        f"<td style='{tds_b}'></td>"
        f"<td style='{tdr_b}'></td>")
    if show_disc_col:
        foot_cells += f"<td style='{tdr_b}'></td>"
        foot_cells += f"<td style='{tdr_b}'>{tot_disc_amt:,.2f}</td>"
    foot_cells += f"<td style='{tdr_b}'>{tot_excl:,.2f}</td>"
    if show_tax_col:
        foot_cells += f"<td style='{tdr_b}'></td>"
        foot_cells += f"<td style='{tdr_b}'>{tot_tax_amt:,.2f}</td>"
    if show_chg_col:
        if doc_type == "sales":
            foot_cells += f"<td style='{tdr_b}'>{tot_chg1:,.2f}</td>"
            foot_cells += f"<td style='{tdr_b}'>{tot_chg2:,.2f}</td>"
        else:
            foot_cells += f"<td style='{tdr_b}'>{tot_chg1:,.2f}</td>"
            foot_cells += f"<td style='{tdr_b}'>{tot_chg2:,.2f}</td>"
            foot_cells += f"<td style='{tdr_b}'>{tot_chg3:,.2f}</td>"
    foot_cells += f"<td style='{tdr_b}'>{tot_tax_amt:,.2f}</td>"
    foot_cells += f"<td style='{tdr_b}'>{tot_incl:,.2f}</td>"
    foot_cells += f"<td style='{tdr_b}'>{tot_line:,.2f}</td>"

    # ── Header ───────────────────────────────────────────────────────
    hds = "padding:8px;border:1px solid #1e293b;"
    hdc = hds + "text-align:center;"
    hdl = hds + "text-align:left;"
    hdr = hds + "text-align:right;"

    head = (
        f"<th style='{hdc}'>#</th>"
        f"<th style='{hdl}'>SKU</th>"
        f"<th style='{hdl}'>Description</th>"
        f"<th style='{hdc}'>Qty</th>"
        f"<th style='{hdc}'>Unit</th>"
        f"<th style='{hdr}'>Per Unit Price</th>")
    if show_disc_col:
        head += f"<th style='{hdr}'>Discount %</th>"
        head += f"<th style='{hdr}'>Discount allowed</th>"
    head += f"<th style='{hdr}'>Amount Excl. of Sales Tax</th>"
    if show_tax_col:
        head += f"<th style='{hdr}'>Sales Tax %</th>"
        head += f"<th style='{hdr}'>Sales Tax Amount per Unit</th>"
    if show_chg_col:
        if doc_type == "sales":
            head += f"<th style='{hdr}'>Carriage Expense</th>"
            head += f"<th style='{hdr}'>Installation</th>"
        else:
            head += f"<th style='{hdr}'>Commission</th>"
            head += f"<th style='{hdr}'>Freight</th>"
            head += f"<th style='{hdr}'>Ld/Unld</th>"
    head += f"<th style='{hdr}'>Total Sales Tax</th>"
    head += f"<th style='{hdr}'>Amount Incl. of Sales Tax</th>"
    head += f"<th style='{hdr}'>Total</th>"

    return (
        '<table style="width:100%;border-collapse:collapse;font-size:12px;">'
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
        "subtotal": "760,000.00",
        "discount": "38,000.00",
        "tax": "122,760.00",
        "delivery_charges": "12,000.00",
        "installation_charges": "25,000.00",
        "commission": "8,500.00",
        "freight": "14,000.00",
        "loading_unloading": "4,200.00",
        "withholding_tax": "7,600.00",
        "grand_total": "881,760.00",
        "notes": "Payment due within 30 days. Goods remain the property of the "
                 "seller until paid in full.",
    }


def render_invoice_template(body_html, ctx):
    """Replace {{placeholder}} tokens in body_html with values from ctx dict."""
    for key, val in ctx.items():
        token = "{{" + key + "}}"
        if val is None:
            val = ""
        body_html = body_html.replace(token, str(val))
    return body_html


# ── Model ───────────────────────────────────────────────────────────────────

class InvoiceTemplate(db.Model):
    __tablename__ = "invoice_templates"
    id = db.Column(db.Integer, primary_key=True)
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
