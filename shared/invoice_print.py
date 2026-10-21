"""Single-document print HTML for sales/purchase invoices.

The sales and purchase forms each grew their own ~200-line template-context
builder for printing. The bulk registers need the identical output — one
invoice per page — so both builders live here now: the forms and the bulk
print pages call the same function, and the formats can never diverge.

Returns a complete HTML fragment (the invoice template's rendered body, or a
compact fallback when no template is configured).

XSS note: every user-controlled value interpolated below is HTML-escaped at
build time (`_e`), because the fragment is rendered with ``|safe`` by its
callers. Numbers go through the money formatter and need no escaping.
"""
from html import escape as _escape

from shared.formatting import format_amount as _m
from shared.invoice_totals import sales_totals as _sales_totals
from shared.models.company_settings import CompanyInfo, ReportSettings
from shared.models.invoice_template import (
    InvoiceTemplate,
    build_totals_table,
    items_table_metrics,
    render_invoice_template,
    strip_shared_sheet_assets,
)
from shared.tenancy import scoped_get


def _e(value):
    """HTML-escape user-controlled text interpolated into print fragments."""
    if value is None:
        return ""
    return _escape(str(value), quote=True)


def _template_for(rs, doc_type):
    """The print template a company has chosen, resolved once per request."""
    tid = (rs.purchase_template_id if doc_type == "purchase"
           else rs.sales_template_id)
    obj = scoped_get(InvoiceTemplate, tid) if tid else None
    if not obj:
        obj = InvoiceTemplate.get_default(doc_type)
    return obj


def _sales_template_obj(invoice):
    rs = ReportSettings.get()
    return rs, _template_for(rs, "sales")


def _purchase_template_obj(invoice):
    rs = ReportSettings.get()
    return rs, _template_for(rs, "purchase")


def _fallback_html(kind, invoice, party_name, items=None):
    """Compact generic invoice when no print template is configured."""
    company = CompanyInfo.get()
    rows = ""
    for i, it in enumerate(items if items is not None else invoice.items.all(),
                           start=1):
        amount = (it.total_after_discount or it.total_before_discount
                  or (it.quantity or 0) * (it.unit_price or 0))
        rows += (f"<tr><td>{i}</td><td>{_e(it.description)}</td>"
                 f"<td style='text-align:right'>{it.quantity or 0}</td>"
                 f"<td style='text-align:right'>{_m(it.unit_price)}</td>"
                 f"<td style='text-align:right'>{_m(amount)}</td></tr>")
    total = float(invoice.total_amount or 0)
    paid = float(invoice.paid_amount or 0)
    return (
        f"<div style='font-family:Inter,Arial,sans-serif;max-width:750px;"
        f"margin:0 auto;padding:24px;'>"
        f"<h2 style='text-align:center;margin:0'>{_e(company.company_name)}</h2>"
        f"<h3 style='text-align:center;margin:6px 0 16px'>{_e(kind)} — "
        f"{_e(invoice.voucher_number)}</h3>"
        f"<p><strong>{_e(party_name)}:</strong> "
        f"{invoice.invoice_date.strftime('%d-%b-%Y') if invoice.invoice_date else ''}</p>"
        f"<table style='width:100%;border-collapse:collapse;font-size:12px'>"
        f"<thead><tr><th>#</th><th>Description</th>"
        f"<th style='text-align:right'>Qty</th>"
        f"<th style='text-align:right'>Rate</th>"
        f"<th style='text-align:right'>Amount</th></tr></thead>"
        f"<tbody>{rows}</tbody>"
        f"<tfoot><tr><td colspan='4' style='text-align:right'>"
        f"<strong>Total:</strong></td>"
        f"<td style='text-align:right'><strong>{_m(total)}</strong></td></tr>"
        f"<tr><td colspan='4' style='text-align:right'>Paid:</td>"
        f"<td style='text-align:right'>{_m(paid)}</td></tr>"
        f"<tr><td colspan='4' style='text-align:right'>Balance:</td>"
        f"<td style='text-align:right'>{_m(total - paid)}</td></tr>"
        f"</tfoot></table></div>"
    )


def render_sales_print_html(invoice, items=None, settings=None, template=None,
                            product_map=None, charge_rows=None):
    """Full print HTML for one sales invoice (same as the form's print).

    The optional arguments let the bulk renderer hand in an invoice whose
    items, products, charges and template settings were loaded in one batch.
    Called on its own, every argument defaults to the single-document query.
    """
    if settings is None:
        rs, invoice_template_obj = _sales_template_obj(invoice)
    else:
        rs, invoice_template_obj = settings, template
    company = CompanyInfo.get()
    if not invoice_template_obj:
        party = invoice.customer
        return _fallback_html("Sales Invoice", invoice,
                              party.name if party else "", items=items)
    topts = invoice_template_obj.options

    # Decide per-section display based on invoice's mode
    if invoice.discount_mode == "individual":
        show_disc_col = topts.get("discount_display") == "per_line"
    else:
        show_disc_col = False

    if invoice.tax_mode == "individual":
        show_tax_col = topts.get("tax_display") == "per_line"
    else:
        show_tax_col = False

    if invoice.charges_mode == "individual":
        show_chg_col = topts.get("charges_display") == "per_line"
    else:
        show_chg_col = False

    # Style constants. Sizing keys off the real column count, not off how
    # many option groups are on: a group can add two columns, so counting
    # groups under-reports the width the table actually needs.
    n_cols = (10
              + (2 if show_disc_col else 0)
              + (2 if show_tax_col else 0)
              + (2 if show_chg_col else 0))
    m = items_table_metrics(n_cols)
    fs = m["font"]
    tds = f"padding:{m['pad']};border:1px solid #e2e8f0;white-space:nowrap;"
    tdc = tds + "text-align:center;"
    tdr = tds + "text-align:right;"

    tot_qty = 0
    tot_disc_amt = 0.0
    tot_excl = 0.0
    tot_delivery = 0.0
    tot_install = 0.0
    tot_tax_amt = 0.0
    tot_incl = 0.0
    tot_line = 0.0

    items_rows = ""
    for i, it in enumerate(items if items is not None else invoice.items.all(),
                           start=1):
        product = (product_map.get(it.product_id)
                   if product_map is not None else it.product)
        da = it.discount_amount or 0
        tp = it.sales_tax_pct or 0
        line_total = it.total_before_discount or 0
        amt_excl = line_total - da
        tax_amt = amt_excl * tp / 100
        amt_incl = amt_excl + tax_amt

        tot_qty += it.quantity or 0
        tot_disc_amt += da
        tot_excl += amt_excl
        tot_delivery += it.delivery or 0
        tot_install += it.installation or 0
        tot_tax_amt += tax_amt
        tot_incl += amt_incl
        tot_line += line_total

        cells = (
            f"<td style='{tdc}'>{i}</td>"
            f"<td style='{tds}'>{_e(product.sku) if product else ''}</td>"
            f"<td class='inv-desc' style='{tds}white-space:normal;'>{_e(it.description)}</td>"
            f"<td style='{tdc}'>{it.quantity}</td>"
            f"<td style='{tdc}'>{_e(it.unit)}</td>"
            f"<td style='{tdr}'>{_m(it.unit_price)}</td>")
        if show_disc_col:
            cells += f"<td style='{tdr}'>{it.discount_pct:.1f}%</td>"
            cells += f"<td style='{tdr}'>{_m(da)}</td>"
        cells += f"<td style='{tdr}'>{_m(amt_excl)}</td>"
        if show_tax_col:
            # Per-unit sales tax (distinct from the line's Total Sales Tax
            # column below — the two must not render the same figure twice).
            pu_tax = tax_amt / (it.quantity or 1)
            cells += f"<td style='{tdr}'>{tp:.1f}%</td>"
            cells += f"<td style='{tdr}'>{_m(pu_tax)}</td>"
        if show_chg_col:
            cells += f"<td style='{tdr}'>{_m(it.delivery)}</td>"
            cells += f"<td style='{tdr}'>{_m(it.installation)}</td>"
        cells += f"<td style='{tdr}'>{_m(tax_amt)}</td>"
        cells += f"<td style='{tdr}'>{_m(amt_incl)}</td>"
        cells += f"<td style='{tdr}'>{_m(line_total)}</td>"
        items_rows += "<tr>" + cells + "</tr>"

    # Totals footer row
    tds_b = (f"padding:{m['pad']};border:1px solid #e2e8f0;font-weight:700;"
             f"background:#f1f5f9;white-space:nowrap;")
    tdr_b = tds_b + "text-align:right;"
    foot = (
        f"<td style='{tds_b};text-align:center;'></td>"
        f"<td style='{tds_b}'></td>"
        f"<td class='inv-desc' style='{tds_b}'>Total</td>"
        f"<td style='{tds_b};text-align:center;'>{tot_qty}</td>"
        f"<td style='{tds_b}'></td>"
        f"<td style='{tdr_b}'></td>")
    if show_disc_col:
        foot += f"<td style='{tdr_b}'></td>"
        foot += f"<td style='{tdr_b}'>{_m(tot_disc_amt)}</td>"
    foot += f"<td style='{tdr_b}'>{_m(tot_excl)}</td>"
    if show_tax_col:
        foot += f"<td style='{tdr_b}'></td>"
        foot += f"<td style='{tdr_b}'>{_m(tot_tax_amt)}</td>"
    if show_chg_col:
        foot += f"<td style='{tdr_b}'>{_m(tot_delivery)}</td>"
        foot += f"<td style='{tdr_b}'>{_m(tot_install)}</td>"
    foot += f"<td style='{tdr_b}'>{_m(tot_tax_amt)}</td>"
    foot += f"<td style='{tdr_b}'>{_m(tot_incl)}</td>"
    foot += f"<td style='{tdr_b}'>{_m(tot_line)}</td>"

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
        head += f"<th style='{hd}'>Carriage Expense</th>"
        head += f"<th style='{hd}'>Installation</th>"
    head += f"<th style='{hd}'>Total Sales Tax</th>"
    head += f"<th style='{hd}'>Amount Incl. of Sales Tax</th>"
    head += f"<th style='{hd}'>Total</th>"

    items_table = (
        f'<table class="inv-items" style="width:100%;border-collapse:collapse;'
        f'font-size:{fs}px;">'
        '<thead><tr style="background:#1e293b;color:#fff;">' + head +
        '</tr></thead><tbody>' + items_rows +
        '<tr>' + foot + '</tr></tbody></table>'
    )

    party = invoice.customer
    ctx = {
        "company_logo": f'<img src="{_e(company.logo_url)}" style="max-height:60px;" alt="Logo">' if company.logo_url else "",
        "company_name": _e(company.company_name),
        "company_address": _e(company.address),
        "company_city": _e(company.city),
        "company_phone": _e(company.phone),
        "company_email": _e(company.email),
        "company_tax_id": _e(company.tax_id),
        "company_bank_name": _e(company.bank_name),
        "company_bank_account_title": _e(company.bank_account_title),
        "company_bank_account_number": _e(company.bank_account_number),
        "invoice_no": _e(invoice.voucher_number),
        "invoice_date": invoice.invoice_date.strftime("%d-%b-%Y") if invoice.invoice_date else "",
        # Purchase invoices carry no due date column; reading it unguarded
        # crashed every purchase invoice page once a print template was set.
        "due_date": (invoice.due_date.strftime("%d-%b-%Y")
                     if getattr(invoice, "due_date", None) else ""),
        "status": ("Approved" if invoice.approved_at else "Unapproved"),
        "party_name": _e(party.name) if party else "",
        "party_address": _e(party.address) if party and party.address else "",
        "party_city": _e(party.city) if party and party.city else "",
        "party_phone": _e(party.phone) if party and party.phone else "",
        "party_email": _e(party.email) if party and party.email else "",
        "party_tax_id": _e(party.tax_id) if party and party.tax_id else "",
        "items_table": items_table,
        "totals_table": "",
        "subtotal": _m(invoice.subtotal),
        "discount": _m(invoice.total_discount),
        "tax": _m(invoice.total_tax),
        "delivery_charges": _m(invoice.global_delivery),
        "installation_charges": _m(invoice.global_installation),
        "grand_total": _m(0),
        "commission": _m(0),
        "freight": _m(0),
        "loading_unloading": _m(0),
        "taxable_value": _m(_sales_totals(
            invoice, charge_rows=charge_rows)["sales_tax_base"]),
        "additional_charges": _m(invoice.total_charges),
        "further_tax": _m(invoice.total_further_tax),
        "withholding_tax": _m(invoice.total_withholding_tax),
        "notes": _e(invoice.notes),
    }
    # Build totals table — respect invoice mode per section
    display_opts = dict(topts)
    if invoice.discount_mode == "individual":
        if topts.get("discount_display") == "per_line":
            display_opts["show_discount"] = False
    if invoice.tax_mode == "individual":
        if topts.get("tax_display") == "per_line":
            display_opts["show_tax"] = False
    if invoice.charges_mode == "individual":
        if topts.get("charges_display") == "per_line":
            display_opts["show_delivery"] = False
            display_opts["show_installation"] = False
    ctx["totals_table"] = build_totals_table("sales", display_opts,
                                              invoice_template_obj.accent_color or "#0f766e")
    # The printed total must be the figure that was posted, not a second
    # derivation of it — recomputing here from a few legacy fields silently
    # dropped additional charges, further tax and withholding.
    ctx["grand_total"] = _m(invoice.total_amount)
    return render_invoice_template(invoice_template_obj.body_html, ctx)


def render_purchase_print_html(invoice, items=None, settings=None, template=None,
                               product_map=None, charge_rows=None):
    """Full print HTML for one purchase invoice (same as the form's print).

    The optional arguments mirror ``render_sales_print_html`` so the bulk
    renderer can supply a batch-loaded invoice; ``charge_rows`` is accepted
    for signature symmetry and unused (purchase print does not build the
    sales-tax pools).
    """
    if settings is None:
        rs, invoice_template_obj = _purchase_template_obj(invoice)
    else:
        rs, invoice_template_obj = settings, template
    company = CompanyInfo.get()
    if not invoice_template_obj:
        party = invoice.supplier
        return _fallback_html("Purchase Invoice", invoice,
                              party.name if party else "", items=items)
    topts = invoice_template_obj.options

    if invoice.discount_mode == "individual":
        show_disc_col = topts.get("discount_display") == "per_line"
    else:
        show_disc_col = False

    if invoice.tax_mode == "individual":
        show_tax_col = topts.get("tax_display") == "per_line"
    else:
        show_tax_col = False

    if invoice.expenses_mode == "individual":
        show_chg_col = topts.get("charges_display") == "per_line"
    else:
        show_chg_col = False

    # Style constants. Sizing keys off the real column count, not off how
    # many option groups are on: charges alone adds three columns here, so
    # counting groups under-reports the width the table actually needs.
    n_cols = (10
              + (2 if show_disc_col else 0)
              + (2 if show_tax_col else 0)
              + (3 if show_chg_col else 0))
    m = items_table_metrics(n_cols)
    fs = m["font"]
    tds = f"padding:{m['pad']};border:1px solid #e2e8f0;white-space:nowrap;"
    tdc = tds + "text-align:center;"
    tdr = tds + "text-align:right;"

    tot_qty = 0
    tot_disc_amt = 0.0
    tot_excl = 0.0
    tot_comm = 0.0
    tot_freight = 0.0
    tot_ld = 0.0
    tot_tax_amt = 0.0
    tot_incl = 0.0
    tot_line = 0.0

    items_rows = ""
    for i, it in enumerate(items if items is not None else invoice.items.all(),
                           start=1):
        product = (product_map.get(it.product_id)
                   if product_map is not None else it.product)
        da = it.discount_amount or 0
        tp = it.sales_tax_pct or 0
        line_total = it.total_before_discount or 0
        amt_excl = line_total - da
        tax_amt = amt_excl * tp / 100
        amt_incl = amt_excl + tax_amt

        tot_qty += it.quantity or 0
        tot_disc_amt += da
        tot_excl += amt_excl
        tot_comm += it.commission or 0
        tot_freight += it.freight or 0
        tot_ld += it.loading_unloading or 0
        tot_tax_amt += tax_amt
        tot_incl += amt_incl
        tot_line += line_total

        cells = (
            f"<td style='{tdc}'>{i}</td>"
            f"<td style='{tds}'>{_e(product.sku) if product else ''}</td>"
            f"<td class='inv-desc' style='{tds}white-space:normal;'>{_e(it.description)}</td>"
            f"<td style='{tdc}'>{it.quantity}</td>"
            f"<td style='{tdc}'>{_e(it.unit)}</td>"
            f"<td style='{tdr}'>{_m(it.unit_price)}</td>")
        if show_disc_col:
            cells += f"<td style='{tdr}'>{it.discount_pct:.1f}%</td>"
            cells += f"<td style='{tdr}'>{_m(da)}</td>"
        cells += f"<td style='{tdr}'>{_m(amt_excl)}</td>"
        if show_tax_col:
            cells += f"<td style='{tdr}'>{tp:.1f}%</td>"
            cells += f"<td style='{tdr}'>{_m(tax_amt)}</td>"
        if show_chg_col:
            cells += f"<td style='{tdr}'>{_m(it.commission)}</td>"
            cells += f"<td style='{tdr}'>{_m(it.freight)}</td>"
            cells += f"<td style='{tdr}'>{_m(it.loading_unloading)}</td>"
        cells += f"<td style='{tdr}'>{_m(tax_amt)}</td>"
        cells += f"<td style='{tdr}'>{_m(amt_incl)}</td>"
        cells += f"<td style='{tdr}'>{_m(line_total)}</td>"
        items_rows += "<tr>" + cells + "</tr>"

    tds_b = (f"padding:{m['pad']};border:1px solid #e2e8f0;font-weight:700;"
             f"background:#f1f5f9;white-space:nowrap;")
    tdr_b = tds_b + "text-align:right;"
    foot = (
        f"<td style='{tds_b};text-align:center;'></td>"
        f"<td style='{tds_b}'></td>"
        f"<td class='inv-desc' style='{tds_b}'>Total</td>"
        f"<td style='{tds_b};text-align:center;'>{tot_qty}</td>"
        f"<td style='{tds_b}'></td>"
        f"<td style='{tdr_b}'></td>")
    if show_disc_col:
        foot += f"<td style='{tdr_b}'></td>"
        foot += f"<td style='{tdr_b}'>{_m(tot_disc_amt)}</td>"
    foot += f"<td style='{tdr_b}'>{_m(tot_excl)}</td>"
    if show_tax_col:
        foot += f"<td style='{tdr_b}'></td>"
        foot += f"<td style='{tdr_b}'>{_m(tot_tax_amt)}</td>"
    if show_chg_col:
        foot += f"<td style='{tdr_b}'>{_m(tot_comm)}</td>"
        foot += f"<td style='{tdr_b}'>{_m(tot_freight)}</td>"
        foot += f"<td style='{tdr_b}'>{_m(tot_ld)}</td>"
    foot += f"<td style='{tdr_b}'>{_m(tot_tax_amt)}</td>"
    foot += f"<td style='{tdr_b}'>{_m(tot_incl)}</td>"
    foot += f"<td style='{tdr_b}'>{_m(tot_line)}</td>"

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
        head += f"<th style='{hd}'>Commission</th>"
        head += f"<th style='{hd}'>Freight</th>"
        head += f"<th style='{hd}'>Ld/Unld</th>"
    head += f"<th style='{hd}'>Total Sales Tax</th>"
    head += f"<th style='{hd}'>Amount Incl. of Sales Tax</th>"
    head += f"<th style='{hd}'>Total</th>"

    items_table = (
        f'<table class="inv-items" style="width:100%;border-collapse:collapse;'
        f'font-size:{fs}px;">'
        '<thead><tr style="background:#1e293b;color:#fff;">' + head +
        '</tr></thead><tbody>' + items_rows +
        '<tr>' + foot + '</tr></tbody></table>'
    )

    party = invoice.supplier
    ctx = {
        "company_logo": f'<img src="{_e(company.logo_url)}" style="max-height:60px;" alt="Logo">' if company.logo_url else "",
        "company_name": _e(company.company_name),
        "company_address": _e(company.address),
        "company_city": _e(company.city),
        "company_phone": _e(company.phone),
        "company_email": _e(company.email),
        "company_tax_id": _e(company.tax_id),
        "company_bank_name": _e(company.bank_name),
        "company_bank_account_title": _e(company.bank_account_title),
        "company_bank_account_number": _e(company.bank_account_number),
        "invoice_no": _e(invoice.voucher_number),
        "invoice_date": invoice.invoice_date.strftime("%d-%b-%Y") if invoice.invoice_date else "",
        # Purchase invoices carry no due date column; reading it unguarded
        # crashed every purchase invoice page once a print template was set.
        "due_date": (invoice.due_date.strftime("%d-%b-%Y")
                     if getattr(invoice, "due_date", None) else ""),
        "status": ("Approved" if invoice.approved_at else "Unapproved"),
        "party_name": _e(party.name) if party else "",
        "party_address": _e(party.address) if party and party.address else "",
        "party_city": _e(party.city) if party and party.city else "",
        "party_phone": _e(party.phone) if party and party.phone else "",
        "party_email": _e(party.email) if party and party.email else "",
        "party_tax_id": _e(party.tax_id) if party and party.tax_id else "",
        "items_table": items_table,
        "totals_table": "",
        "subtotal": _m(invoice.subtotal),
        "discount": _m(invoice.total_discount),
        "tax": _m(invoice.total_tax),
        "commission": _m(0),
        "freight": _m(0),
        "loading_unloading": _m(invoice.total_expenses),
        "withholding_tax": _m(invoice.total_withholding_tax),
        "grand_total": _m(0),
        "delivery_charges": _m(0),
        "installation_charges": _m(0),
        "notes": _e(invoice.notes),
    }
    # Build totals table — respect invoice mode per section
    display_opts = dict(topts)
    if invoice.discount_mode == "individual":
        if topts.get("discount_display") == "per_line":
            display_opts["show_discount"] = False
    if invoice.tax_mode == "individual":
        if topts.get("tax_display") == "per_line":
            display_opts["show_tax"] = False
    if invoice.expenses_mode == "individual":
        if topts.get("charges_display") == "per_line":
            display_opts["show_commission"] = False
            display_opts["show_freight"] = False
            display_opts["show_loading"] = False
            display_opts["show_withholding"] = False
    ctx["totals_table"] = build_totals_table("purchase", display_opts,
                                              invoice_template_obj.accent_color or "#0f766e")
    net = invoice.net_payable or (
        (invoice.subtotal or 0) - (invoice.total_discount or 0)
        + (invoice.total_tax or 0) - (invoice.total_withholding_tax or 0))
    ctx["grand_total"] = _m(net)
    return render_invoice_template(invoice_template_obj.body_html, ctx)


# ── Bulk print: one pass over the whole chunk ──────────────────────────────

def prefetch_invoice_print_data(invoices, doc_type):
    """Load the items, products and charges a chunk of invoices prints.

    Returns ``(item_map, product_map, charge_map)`` keyed by invoice /
    product id. Dynamic ``invoice.items`` and lazy ``item.product`` would
    otherwise run one query per invoice and per line: 189 queries for twenty
    invoices became six here.

    The maps are also used by the bulk Excel export, so its line rows come
    from the same load as the print preview.
    """
    from inventory_app.models.additional_charge import AdditionalCharge
    from inventory_app.models.invoice import InvInvoiceItem
    from inventory_app.models.product import InvProduct
    from inventory_app.models.purchase_invoice import InvPurchaseInvoiceItem

    invoices = list(invoices)
    ids = [inv.id for inv in invoices]
    if not ids:
        return {}, {}, {}

    doc_type = "purchase" if doc_type == "purchase" else "sales"
    item_model = (InvPurchaseInvoiceItem if doc_type == "purchase"
                  else InvInvoiceItem)
    item_rows = (item_model.query
                 .filter(item_model.invoice_id.in_(ids))
                 .order_by(item_model.invoice_id.asc(),
                           item_model.id.asc())
                 .all())
    item_map = {}
    product_ids = set()
    for it in item_rows:
        item_map.setdefault(it.invoice_id, []).append(it)
        if it.product_id:
            product_ids.add(it.product_id)
    product_map = {}
    if product_ids:
        products = InvProduct.query.filter(InvProduct.id.in_(product_ids)).all()
        product_map = {p.id: p for p in products}

    charge_map = {}
    if doc_type == "sales":
        charge_rows = (AdditionalCharge.query
                       .filter(AdditionalCharge.doc_type == "SI",
                               AdditionalCharge.doc_id.in_(ids))
                       .order_by(AdditionalCharge.id.asc())
                       .all())
        for chg in charge_rows:
            charge_map.setdefault(chg.doc_id, []).append(chg)
    return item_map, product_map, charge_map


def render_invoice_print_many(invoices, doc_type, include_shared_assets=True):
    """Print HTML for a chunk of invoices, resolving shared data once.

    Same output as calling ``render_sales_print_html`` /
    ``render_purchase_print_html`` per invoice, without the per-invoice
    settings, template, line, product and charge queries.

    ``include_shared_assets`` keeps the A4 stylesheet and fit script on the
    first document only. Later chunks are inserted after that first document,
    so repeating the same 12 KB (and hundreds of <style> blocks) buys nothing.
    """
    invoices = list(invoices)
    if not invoices:
        return []
    doc_type = "purchase" if doc_type == "purchase" else "sales"
    rs = ReportSettings.get()
    template = _template_for(rs, doc_type)
    item_map, product_map, charge_map = prefetch_invoice_print_data(
        invoices, doc_type)
    render = (render_purchase_print_html if doc_type == "purchase"
              else render_sales_print_html)
    out = []
    for index, inv in enumerate(invoices):
        html = render(inv, items=item_map.get(inv.id, []), settings=rs,
                      template=template, product_map=product_map,
                      charge_rows=charge_map.get(inv.id, []))
        if index or not include_shared_assets:
            html = strip_shared_sheet_assets(html)
        out.append(html)
    return out
