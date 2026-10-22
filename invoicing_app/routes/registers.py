"""Bulk invoice registers: view first, then export or print.

One page per the industry-standard sales/purchase day-book shape — filter by
month, financial year or a custom range, see the book on screen with totals,
then take the same rows to Excel, PDF or the printer. The export/print links
carry the identical query string, so the file is always what the screen
shows. Filters come off the query string (shareable, refresh-safe); an
unparseable value falls back rather than 500-ing.
"""
from flask import (Blueprint, abort, render_template, request, send_file)
from flask_login import login_required
from sqlalchemy import or_

from inventory_app.models.invoice import InvInvoice
from inventory_app.models.purchase_invoice import InvPurchaseInvoice
from shared.registers import apply_date_filter, resolve_register_filter

inv_reg_bp = Blueprint("inv_registers", __name__,
                       url_prefix="/invoicing/registers")

XLSX_MIMETYPE = ("application/vnd.openxmlformats-officedocument"
                 ".spreadsheetml.sheet")

# "Returned" is the approved credit / debit notes against the invoice: they
# settle it with no money moving, so the balance is total - paid - returned.
# Without them a returned invoice showed its full value as still owed.
SALES_COLUMNS = ["Date", "Invoice #", "Voucher #", "Customer", "Subtotal",
                 "Discount", "Sales Tax", "Further Tax", "WHT", "Total",
                 "Paid", "Returned", "Balance", "Status"]
PURCHASE_COLUMNS = ["Date", "Invoice #", "Voucher #", "Supplier", "Subtotal",
                    "Discount", "Expenses", "Tax", "WHT", "Net Payable",
                    "Paid", "Returned", "Balance", "Status"]
N_VALS = 9          # the numeric columns, Subtotal .. Balance


def _returned(doc_type, invoices):
    from shared.payment_tracking import credited_by_doc
    got = credited_by_doc(doc_type, [i.id for i in invoices])
    return {k: float(v) for k, v in got.items()}


def _returned_total(doc_type, query, model):
    """Approved returns against every invoice in the filtered set."""
    from sqlalchemy import func
    from shared.payment_tracking import _return_model
    R = _return_model(doc_type)
    ids = query.with_entities(model.id).subquery()
    total = (R.query.with_entities(func.coalesce(func.sum(R.net_return_amount), 0))
             .filter(R.status == "approved", R.original_invoice_id.in_(ids.select()))
             .scalar())
    return float(total or 0)


def _sales_query(status):
    query = InvInvoice.query
    if status == "approved":
        query = query.filter_by(voucher_status="approved")
    elif status == "unapproved":
        # NULL status reads as unapproved on the row, so it must match here
        # too — a bare != drops NULLs in SQL.
        query = query.filter(or_(InvInvoice.voucher_status.is_(None),
                                 InvInvoice.voucher_status != "approved"))
    return query


def _purchase_query(status):
    query = InvPurchaseInvoice.query
    if status == "approved":
        query = query.filter_by(status="approved")
    elif status == "unapproved":
        query = query.filter(or_(InvPurchaseInvoice.status.is_(None),
                                 InvPurchaseInvoice.status != "approved"))
    return query


def _sales_rows(invoices):
    rows, totals = [], [0.0] * N_VALS
    returned = _returned("SI", invoices)
    for inv in invoices:
        total = float(inv.total_amount or 0)
        paid = float(inv.paid_amount or 0)
        back = returned.get(inv.id, 0.0)
        vals = [float(inv.subtotal or 0), float(inv.total_discount or 0),
                float(inv.total_tax or 0),
                float(inv.total_further_tax or 0),
                float(inv.total_withholding_tax or 0), total, paid, back,
                total - paid - back]
        for i, v in enumerate(vals):
            totals[i] += v
        approved = (inv.voucher_status or "") == "approved"
        rows.append({
            "date": inv.invoice_date,
            "invoice_number": inv.invoice_number,
            "voucher_number": inv.voucher_number,
            "party": inv.customer.name if inv.customer else "-",
            "vals": vals,
            "status": "Approved" if approved else "Unapproved",
            "status_class": "approved" if approved else "unapproved",
        })
    return rows, totals


def _purchase_rows(invoices):
    rows, totals = [], [0.0] * N_VALS
    returned = _returned("PI", invoices)
    for inv in invoices:
        net = (float(inv.net_payable) if inv.net_payable is not None
               else float(inv.total_amount or 0))
        paid = float(inv.paid_amount or 0)
        back = returned.get(inv.id, 0.0)
        vals = [float(inv.subtotal or 0), float(inv.total_discount or 0),
                float(inv.total_expenses or 0), float(inv.total_tax or 0),
                float(inv.total_withholding_tax or 0), net, paid, back,
                net - paid - back]
        for i, v in enumerate(vals):
            totals[i] += v
        approved = (inv.status or "") == "approved"
        rows.append({
            "date": inv.invoice_date,
            "invoice_number": inv.invoice_number,
            "voucher_number": inv.voucher_number,
            "party": inv.supplier.name if inv.supplier else "-",
            "vals": vals,
            "status": "Approved" if approved else "Unapproved",
            "status_class": "approved" if approved else "unapproved",
        })
    return rows, totals


def _sums(query, *cols):
    """SUMs over the full filtered query (one round trip, no row reads)."""
    from sqlalchemy import func
    sums = query.with_entities(
        *[func.coalesce(func.sum(c), 0) for c in cols]).first()
    return [float(v or 0) for v in (sums or [])]


def _register_data(doc, status, from_date, to_date,
                   limit=None, offset=0):
    """(doc, title, columns, rows, totals, invoices, total_count).

    ``totals`` and ``total_count`` always cover the whole filtered set;
    ``rows``/``invoices`` are the requested chunk (everything when
    ``limit`` is None).
    """
    if doc == "purchase":
        query = _purchase_query(status)
        query = apply_date_filter(query, InvPurchaseInvoice.invoice_date,
                                  from_date, to_date)
        total_count = query.count()
        sums = _sums(query, InvPurchaseInvoice.subtotal,
                     InvPurchaseInvoice.total_discount,
                     InvPurchaseInvoice.total_expenses,
                     InvPurchaseInvoice.total_tax,
                     InvPurchaseInvoice.total_withholding_tax)
        ordered = query.order_by(InvPurchaseInvoice.invoice_date.asc(),
                                 InvPurchaseInvoice.id.asc())
        if limit is not None:
            ordered = ordered.offset(offset).limit(limit)
        invoices = ordered.all()
        rows, _t = _purchase_rows(invoices)
        paid = _sums(query, InvPurchaseInvoice.paid_amount)[0]
        # Per-row net is net_payable falling back to total_amount; the
        # aggregate must use the same rule or the cards drift from the rows.
        from sqlalchemy import func as _func
        net = _sums(query, _func.coalesce(InvPurchaseInvoice.net_payable,
                                          InvPurchaseInvoice.total_amount))[0]
        back = _returned_total("PI", query, InvPurchaseInvoice)
        totals = sums[:5] + [net, paid, back, net - paid - back]
        title, columns = "Purchase Invoice Register", PURCHASE_COLUMNS
    else:
        doc = "sales"
        query = _sales_query(status)
        query = apply_date_filter(query, InvInvoice.invoice_date,
                                  from_date, to_date)
        total_count = query.count()
        sums = _sums(query, InvInvoice.subtotal, InvInvoice.total_discount,
                     InvInvoice.total_tax, InvInvoice.total_further_tax,
                     InvInvoice.total_withholding_tax)
        ordered = query.order_by(InvInvoice.invoice_date.asc(),
                                 InvInvoice.id.asc())
        if limit is not None:
            ordered = ordered.offset(offset).limit(limit)
        invoices = ordered.all()
        rows, _t = _sales_rows(invoices)
        paid = _sums(query, InvInvoice.paid_amount)[0]
        total = _sums(query, InvInvoice.total_amount)[0]
        back = _returned_total("SI", query, InvInvoice)
        totals = sums[:5] + [total, paid, back, total - paid - back]
        title, columns = "Sales Invoice Register", SALES_COLUMNS
    for r, inv in zip(rows, invoices):
        r["id"] = inv.id
    return doc, title, columns, rows, totals, invoices, total_count


def _full_invoices(doc, status, from_date, to_date, limit=None, offset=0,
                   total=None):
    """(total_count, invoices) for the bulk print/export path.

    The full-document path needs the invoices themselves, not the register
    rows or its sum cards. Querying only what it renders is what keeps a
    310-invoice preview from recomputing the whole register on every chunk.
    """
    from sqlalchemy.orm import joinedload
    if doc == "purchase":
        query = _purchase_query(status)
        query = apply_date_filter(query, InvPurchaseInvoice.invoice_date,
                                  from_date, to_date)
        if total is None:
            total = query.count()
        ordered = (query.options(joinedload(InvPurchaseInvoice.supplier))
                   .order_by(InvPurchaseInvoice.invoice_date.asc(),
                             InvPurchaseInvoice.id.asc()))
    else:
        doc = "sales"
        query = _sales_query(status)
        query = apply_date_filter(query, InvInvoice.invoice_date,
                                  from_date, to_date)
        if total is None:
            total = query.count()
        ordered = (query.options(joinedload(InvInvoice.customer))
                   .order_by(InvInvoice.invoice_date.asc(),
                             InvInvoice.id.asc()))
    if limit is not None:
        ordered = ordered.offset(offset).limit(limit)
    return int(total or 0), ordered.all()


@inv_reg_bp.route("/")
@login_required
def index():
    doc = (request.args.get("doc") or "sales").strip().lower()
    status = (request.args.get("status") or "").strip().lower()
    if status not in ("approved", "unapproved"):
        status = ""
    from_date, to_date, fctx = resolve_register_filter(request.args)
    rows, totals = [], [0.0] * N_VALS
    loaded = request.args.get("view") == "1"
    if loaded:
        doc, title, columns, rows, totals, _invoices, _total = _register_data(
            doc, status, from_date, to_date)
    else:
        doc = doc if doc == "purchase" else "sales"
        title = ("Purchase Invoice Register" if doc == "purchase"
                 else "Sales Invoice Register")
        columns = (PURCHASE_COLUMNS if doc == "purchase"
                   else SALES_COLUMNS)
    view_endpoint = ("inv_purchase_invoice.invoice_form" if doc == "purchase"
                     else "inv_invoices.invoice_form")
    return render_template(
        "registers/index.html", doc=doc, title=title, columns=columns,
        rows=rows, totals=totals, status=status,
        from_date=from_date, to_date=to_date, f=fctx,
        periods=fctx["periods"], view_endpoint=view_endpoint,
        loaded=loaded,
    )


@inv_reg_bp.route("/documents")
@login_required
def documents():
    """Bulk print: every filtered invoice as its full print document.

    Each invoice renders through the shared print renderer — the same HTML
    the invoice form prints — one per page. Print All uses the browser
    (PDF via print-to-PDF); Excel downloads the invoices themselves
    (headers, lines and charges), not the register summary.
    """
    from shared.invoice_print import render_invoice_print_many
    doc = (request.args.get("doc") or "sales").strip().lower()
    status = (request.args.get("status") or "").strip().lower()
    if status not in ("approved", "unapproved"):
        status = ""
    from_date, to_date, fctx = resolve_register_filter(request.args)
    doc = doc if doc == "purchase" else "sales"
    title = ("Purchase Invoice Register" if doc == "purchase"
             else "Sales Invoice Register")
    full_render = request.args.get("view") == "1"
    if full_render:
        # No-JS fallback: render every document server-side.
        total, invoices = _full_invoices(doc, status, from_date, to_date)
        docs = render_invoice_print_many(invoices, doc)
    else:
        # Paint the first pages with the page itself. The browser then only
        # fetches what is left, so opening a long preview does not wait for a
        # second serverless request before showing anything.
        total, invoices = _full_invoices(
            doc, status, from_date, to_date, limit=20, offset=0)
        docs = render_invoice_print_many(invoices, doc)
    label = fctx["label"]
    if status:
        label += f" · {status.title()}"
    return render_template(
        "registers/documents.html", docs=docs, total=total,
        loaded_count=len(docs), full_render=full_render, label=label,
        title=title, f=fctx,
    )


@inv_reg_bp.route("/data")
@login_required
def data():
    """One chunk of the register as JSON.

    Table mode (default): {total, rows, totals, html} where ``html`` is the
    table body rendered through the same partial as the no-JS view.
    Full mode (``full=1``): {total, rows, docs} where ``docs`` is a list of
    complete print-ready invoice documents, one per invoice, for the bulk
    print page. Totals always cover the whole filtered set.
    """
    from flask import jsonify
    from shared.formatting import format_amount
    doc = (request.args.get("doc") or "sales").strip().lower()
    status = (request.args.get("status") or "").strip().lower()
    if status not in ("approved", "unapproved"):
        status = ""
    full = request.args.get("full") == "1"
    try:
        limit = max(1, min(int(request.args.get("limit", 200)),
                           100 if full else 500))
    except (TypeError, ValueError):
        limit = 100 if full else 200
    try:
        offset = max(0, int(request.args.get("offset", 0)))
    except (TypeError, ValueError):
        offset = 0
    # After the first chunk the browser sends back the total it was given;
    # the chunk's own rows are still read from the database, so this only
    # saves a count query per chunk on the 310-invoice bulk preview.
    try:
        known_total = max(0, int(request.args.get("total", "")))
    except (TypeError, ValueError):
        known_total = None
    from_date, to_date, _fctx = resolve_register_filter(request.args)
    if full:
        from shared.invoice_print import render_invoice_print_many
        total, invoices = _full_invoices(
            doc, status, from_date, to_date, limit=limit, offset=offset,
            total=known_total)
        docs = render_invoice_print_many(
            invoices, doc, include_shared_assets=(offset == 0))
        return jsonify({"total": total, "rows": len(invoices),
                        "offset": offset, "limit": limit, "docs": docs})
    doc, title, columns, rows, totals, invoices, total = _register_data(
        doc, status, from_date, to_date, limit=limit, offset=offset)
    html = render_template("registers/_table_body.html", rows=rows,
                           totals=totals, columns=columns, view_endpoint=(
                               "inv_purchase_invoice.invoice_form"
                               if doc == "purchase"
                               else "inv_invoices.invoice_form"))
    tfoot = render_template("registers/_table_totals.html", totals=totals)
    labels = {"grand": format_amount(totals[5], 0),
              "paid": format_amount(totals[6], 0),
              "returned": format_amount(totals[7], 0),
              "balance": format_amount(totals[8], 0)}
    return jsonify({"total": total, "rows": len(rows), "totals": totals,
                    "labels": labels, "offset": offset, "limit": limit,
                    "html": html, "tfoot": tfoot})


@inv_reg_bp.route("/export")
@login_required
def export():
    from shared import report_export as rx
    fmt = (request.args.get("fmt") or "pdf").strip().lower()
    if fmt not in ("excel", "pdf", "csv"):
        abort(404)
    doc = (request.args.get("doc") or "sales").strip().lower()
    status = (request.args.get("status") or "").strip().lower()
    if status not in ("approved", "unapproved"):
        status = ""
    from_date, to_date, fctx = resolve_register_filter(request.args)
    doc, title, columns, rows, totals, _invoices, _total = _register_data(
        doc, status, from_date, to_date)
    subtitle = fctx["label"]
    if status:
        subtitle += f" · {status.title()}"
    data, kinds = [], []
    for r in rows:
        data.append([r["date"], r["invoice_number"], r["voucher_number"], r["party"],
                     *[round(v, 2) for v in r["vals"]], r["status"]])
        kinds.append("account")
    data.append(["", "", "", "TOTAL", *[round(v, 2) for v in totals], ""])
    kinds.append("grand")
    return rx.export_table(fmt, title, columns, data, period=subtitle,
                           row_kinds=kinds, mono_col=1,
                           filters=[f"{len(rows)} documents"],
                           file_period=fctx["label"])


def _bulk_invoice_workbook(doc, invoices, period):
    """Workbook of the invoices themselves: headers, lines and charges.

    This is deliberately not the register export. The register page already
    has that summary; the Bulk Invoices page's Excel button must carry the
    documents being printed, so each invoice appears with its line items and
    additional charges on separate filterable sheets.
    """
    from shared import report_export as rx
    from shared.invoice_print import prefetch_invoice_print_data
    from shared.invoice_totals import line_taxes
    from shared.models.ledger import ChartOfAccount

    sales = doc != "purchase"
    party_attr = "customer" if sales else "supplier"
    party_label = "Customer" if sales else "Supplier"
    returned = _returned("SI" if sales else "PI", invoices)
    item_map, product_map, charge_map = prefetch_invoice_print_data(invoices, doc)
    if not sales and invoices:
        # Prefetch loads sales charges only; purchase needs its own rows for
        # the Charges sheet and for the per-line tax weights below.
        from inventory_app.models.additional_charge import AdditionalCharge
        for chg in (AdditionalCharge.query
                    .filter(AdditionalCharge.doc_type == "PI",
                            AdditionalCharge.doc_id.in_(
                                [inv.id for inv in invoices]))
                    .order_by(AdditionalCharge.doc_id.asc(),
                              AdditionalCharge.id.asc())
                    .all()):
            charge_map.setdefault(chg.doc_id, []).append(chg)

    if sales:
        inv_headers = ["Date", "Invoice #", "Voucher #", party_label,
                       "Status", "Due Date", "Subtotal", "Discount",
                       "Charges", "Sales Tax", "Further Tax", "WHT", "Total",
                       "Paid", "Returned", "Balance", "Notes"]
        item_headers = ["Invoice #", "Voucher #", "Date", party_label,
                        "Line", "SKU", "Description", "Qty", "Unit",
                        "Unit Price", "Discount %", "Discount", "Delivery",
                        "Installation", "Tax %", "Excl. Tax", "Sales Tax",
                        "Incl. Tax", "Line Total"]
    else:
        inv_headers = ["Date", "Invoice #", "Voucher #", party_label,
                       "Status", "Subtotal", "Discount", "Expenses", "Tax",
                       "WHT", "Net Payable", "Paid", "Returned", "Balance",
                       "Notes"]
        item_headers = ["Invoice #", "Voucher #", "Date", party_label,
                        "Line", "SKU", "Description", "Qty", "Unit",
                        "Unit Price", "Discount %", "Discount", "Commission",
                        "Freight", "Loading/Unloading", "Tax %", "Excl. Tax",
                        "Tax", "Incl. Tax", "Line Total"]

    inv_rows, item_rows = [], []
    for inv in invoices:
        party = getattr(inv, party_attr)
        paid = float(inv.paid_amount or 0)
        back = returned.get(inv.id, 0.0)
        rows_added = 0
        if sales:
            total = float(inv.total_amount or 0)
            inv_rows.append([
                inv.invoice_date, inv.invoice_number, inv.voucher_number,
                party.name if party else "", "Approved" if (
                    inv.voucher_status or "") == "approved" else "Unapproved",
                inv.due_date, round(float(inv.subtotal or 0), 2),
                round(float(inv.total_discount or 0), 2),
                round(float(inv.total_charges or 0), 2),
                round(float(inv.total_tax or 0), 2),
                round(float(inv.total_further_tax or 0), 2),
                round(float(inv.total_withholding_tax or 0), 2),
                round(total, 2), round(paid, 2), round(back, 2),
                round(total - paid - back, 2), inv.notes or "",
            ])
        else:
            net = (float(inv.net_payable) if inv.net_payable is not None
                   else float(inv.total_amount or 0))
            inv_rows.append([
                inv.invoice_date, inv.invoice_number, inv.voucher_number,
                party.name if party else "", "Approved" if (
                    inv.status or "") == "approved" else "Unapproved",
                round(float(inv.subtotal or 0), 2),
                round(float(inv.total_discount or 0), 2),
                round(float(inv.total_expenses or 0), 2),
                round(float(inv.total_tax or 0), 2),
                round(float(inv.total_withholding_tax or 0), 2),
                round(net, 2), round(paid, 2), round(back, 2),
                round(net - paid - back, 2), inv.notes or "",
            ])
        lines = item_map.get(inv.id, [])
        line_tax = line_taxes(inv, items=lines,
                              charge_rows=charge_map.get(inv.id, []))
        combined_tax = (inv.tax_mode or "general") == "general"
        for line_no, it in enumerate(lines, start=1):
            product = product_map.get(it.product_id)
            before = float(it.total_before_discount or 0)
            discount = float(it.discount_amount or 0)
            excl = round(before - discount, 2)
            tax_pct = (float(inv.global_sales_tax_pct or 0) if combined_tax
                       else float(it.sales_tax_pct or 0))
            tax = (line_tax[line_no - 1] if line_no <= len(line_tax) else 0.0)
            base = [inv.invoice_number, inv.voucher_number, inv.invoice_date,
                    party.name if party else "", line_no,
                    product.sku if product else "", it.description or "",
                    float(it.quantity or 0), it.unit or "",
                    round(float(it.unit_price or 0), 2),
                    round(float(it.discount_pct or 0), 2), round(discount, 2)]
            if sales:
                base += [round(float(it.delivery or 0), 2),
                         round(float(it.installation or 0), 2)]
            else:
                base += [round(float(it.commission or 0), 2),
                         round(float(it.freight or 0), 2),
                         round(float(it.loading_unloading or 0), 2)]
            base += [round(tax_pct, 2), excl, tax, round(excl + tax, 2),
                     round(before, 2)]
            item_rows.append(base)
            rows_added += 1
        if not rows_added:
            # An invoice with no lines still belongs in the workbook.
            empty = [inv.invoice_number, inv.voucher_number, inv.invoice_date,
                     party.name if party else "", 0, "", "", 0, "", 0, 0, 0]
            empty += [0, 0] if sales else [0, 0, 0]
            empty += [0, 0, 0, 0, 0]
            item_rows.append(empty)

    charge_rows = []
    if invoices:
        charges = []
        for rows_for_invoice in charge_map.values():
            charges.extend(rows_for_invoice)
        charges.sort(key=lambda c: (c.doc_id, c.id))
        account_ids = {c.charge_account_id for c in charges
                       if c.charge_account_id}
        accounts = {}
        if account_ids:
            accounts = {a.id: a.name for a in
                        ChartOfAccount.query.filter(
                            ChartOfAccount.id.in_(account_ids)).all()}
        by_id = {inv.id: inv for inv in invoices}
        for chg in charges:
            inv = by_id.get(chg.doc_id)
            if inv is None:
                continue
            party = getattr(inv, party_attr)
            charge_rows.append([
                inv.invoice_number, inv.voucher_number, inv.invoice_date,
                party.name if party else "", chg.description or "",
                accounts.get(chg.charge_account_id, ""),
                round(float(chg.amount or 0), 2),
                (chg.treatment or "bill").title(),
                (chg.scope or "general").title(),
                "Yes" if chg.st_taxable else "No",
                "Yes" if chg.wht_taxable else "No",
            ])

    num_from, num_to = (6, 16) if sales else (5, 14)
    total_vals = [
        round(sum(float(r[i] or 0) for r in inv_rows), 2)
        for i in range(num_from, num_to)
    ]
    inv_rows.append(["", "", "", "TOTAL"] + (
        ["", ""] if sales else [""]) + total_vals + [""])
    kinds = ["account"] * (len(inv_rows) - 1) + ["grand"]
    extra_sheets = [
        ("Items", item_headers, item_rows),
        ("Charges", ["Invoice #", "Voucher #", "Date", party_label,
                     "Description", "Account", "Amount", "Treatment", "Scope",
                     "In sales-tax base", "In WHT base"], charge_rows),
    ]
    return rx.build_excel(
        "Bulk Invoices", inv_headers, inv_rows, sheet_title="Invoices",
        period=period, filters=[f"{len(invoices)} invoices"], row_kinds=kinds,
        extra_sheets=extra_sheets)


@inv_reg_bp.route("/documents/export")
@login_required
def documents_export():
    """Excel of the bulk-print selection: invoice headers, lines, charges."""
    from shared import report_export as rx
    fmt = (request.args.get("fmt") or "excel").strip().lower()
    if fmt not in ("excel", "xlsx"):
        abort(404)
    doc = (request.args.get("doc") or "sales").strip().lower()
    status = (request.args.get("status") or "").strip().lower()
    if status not in ("approved", "unapproved"):
        status = ""
    from_date, to_date, fctx = resolve_register_filter(request.args)
    doc = doc if doc == "purchase" else "sales"
    _total, invoices = _full_invoices(doc, status, from_date, to_date)
    subtitle = fctx["label"]
    if status:
        subtitle += f" · {status.title()}"
    buf = _bulk_invoice_workbook(doc, invoices, subtitle)
    return rx.send_export(buf, "xlsx", "Bulk Invoices", period=subtitle)
