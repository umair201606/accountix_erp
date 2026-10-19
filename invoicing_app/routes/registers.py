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
    (PDF via print-to-PDF); Excel comes from the register export carrying
    the same filters.
    """
    from shared.invoice_print import (render_purchase_print_html,
                                      render_sales_print_html)
    doc = (request.args.get("doc") or "sales").strip().lower()
    status = (request.args.get("status") or "").strip().lower()
    if status not in ("approved", "unapproved"):
        status = ""
    from_date, to_date, fctx = resolve_register_filter(request.args)
    doc = doc if doc == "purchase" else "sales"
    title = ("Purchase Invoice Register" if doc == "purchase"
             else "Sales Invoice Register")
    docs = None
    if request.args.get("view") == "1":
        # No-JS fallback: render every document server-side. Without it the
        # page fetches chunks from /data itself, so nothing is loaded here.
        _d, _t, _c, _r, _tot, invoices, _n = _register_data(
            doc, status, from_date, to_date)
        render = (render_purchase_print_html if doc == "purchase"
                  else render_sales_print_html)
        docs = [render(inv) for inv in invoices]
    label = fctx["label"]
    if status:
        label += f" · {status.title()}"
    return render_template(
        "registers/documents.html", docs=docs, label=label,
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
                           20 if full else 500))
    except (TypeError, ValueError):
        limit = 20 if full else 200
    try:
        offset = max(0, int(request.args.get("offset", 0)))
    except (TypeError, ValueError):
        offset = 0
    from_date, to_date, _fctx = resolve_register_filter(request.args)
    doc, title, columns, rows, totals, invoices, total = _register_data(
        doc, status, from_date, to_date, limit=limit, offset=offset)
    if full:
        from shared.invoice_print import (render_purchase_print_html,
                                          render_sales_print_html)
        render = (render_purchase_print_html if doc == "purchase"
                  else render_sales_print_html)
        docs = [render(inv) for inv in invoices]
        return jsonify({"total": total, "rows": len(rows),
                        "offset": offset, "limit": limit, "docs": docs})
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
