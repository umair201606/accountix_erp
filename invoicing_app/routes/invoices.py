from flask import Blueprint, render_template, redirect, url_for, flash, request, jsonify
from flask_login import login_required, current_user
from datetime import datetime, date
import json
from decimal import Decimal
from inventory_app.extensions import db
from shared.tenancy import scoped_get, scoped_get_404
from shared.doc_numbers import is_placeholder, number_from_id, placeholder
from shared.concurrency import bump_edit, stale_edit
from inventory_app.models.invoice import InvInvoice, InvInvoiceItem
from inventory_app.models.additional_charge import AdditionalCharge
from inventory_app.models.customer import InvCustomer
from inventory_app.models.product import InvProduct
from inventory_app.models.stock_movement import InvStockMovement
from inventory_app.models.sales_order import InvSalesOrder, InvSalesOrderItem
from shared.ledger_utils import post_journal_entry, reverse_journal_entry, posting_account, party_account
from shared.models.ledger import ChartOfAccount
from shared.models.company_settings import CompanyInfo, ReportSettings
from shared.models.invoice_settings import InvoiceSettings
from shared.models.inventory_settings import InventorySettings
from shared.models.project_label import ProjectLabel
from shared.models.invoice_template import (
    InvoiceTemplate, render_invoice_template, build_totals_table,
    items_table_metrics)
from shared.formatting import format_amount as _m
from shared.permissions import deny_json, deny_page
from shared.costing import lock_products, record_out, reverse_voucher_stock, NegativeStockError
from shared.periods import ClosedPeriodError
from shared.posting_helpers import (parse_doc_date, DocumentDateError,
                                    label_weights, split_by_label)

inv_inv_bp = Blueprint("inv_invoices", __name__, url_prefix="/inventory/invoices")


# The v3 §8 chain lives in one place so the posted journal, the printed
# invoice and the FBR payload can never disagree about the same invoice.
from shared.invoice_totals import (  # noqa: E402
    sales_totals as _sales_totals,
    revenue_splits as _revenue_splits,
    output_tax_splits as _output_tax_splits,
)


def _manual_allocations(chg):
    """Per-line amounts for a "Manual per line" charge (§5.1), as JSON.

    Stored only for that method — every other one derives the split from the
    lines, so a saved copy would just go stale. Anything unparseable is dropped
    rather than saved half-read: the split falls back to pro-rata, which is
    visible, instead of to a silently wrong allocation.
    """
    if chg.get("distribution") != "manual":
        return ""
    try:
        return json.dumps([round(float(v or 0), 2) for v in (chg.get("manual") or [])])
    except (TypeError, ValueError):
        return ""


def _read_manual(raw):
    """The stored manual split, or [] if it is absent or unreadable."""
    if not raw:
        return []
    try:
        return [float(v or 0) for v in json.loads(raw)]
    except (TypeError, ValueError):
        return []


def assign_number(inv):
    """Give a new invoice its document number. A sales invoice has exactly one.

    There used to be two generators off the same counter — SI-… into
    voucher_number and INV-… into invoice_number — so every invoice carried two
    different numbers for itself and the form showed both. They are one thing.

    Both columns are still written, with the same value: 48 places across the
    app, the journal and the FBR payload read one or the other, and each column
    carries its own NOT NULL and UNIQUE constraint. Storing the same string in
    both keeps every reader working (a value is unique per column, so this does
    not collide) without a migration.

    The number is built from the invoice's own row id (shared/doc_numbers.py)
    — "last id + 1" gave two users saving at once the same number.
    """
    return number_from_id(inv, "SI", "invoice_number", "voucher_number")


@inv_inv_bp.route("/", defaults={"id": None})
@inv_inv_bp.route("/<int:id>")
@login_required
def invoice_form(id):
    invoice = scoped_get(InvInvoice, id) if id else None
    customers = InvCustomer.query.filter_by(is_active=True).order_by(InvCustomer.name).all()
    products = InvProduct.query.filter_by(is_active=True).order_by(InvProduct.name).all()
    invoice_items = []
    invoice_charges = []
    if invoice:
        for it in invoice.items.all():
            product = it.product
            invoice_items.append({
                "product_id": it.product_id,
                "product": {"sku": product.sku if product else ""},
                # §6.2 — the "By weight" split needs the line's unit weight
                # client-side; a reopened invoice must carry it too.
                "weight": (product.weight or 0) if product else 0,
                "description": it.description,
                "quantity": it.quantity,
                "unit": it.unit,
                "unit_price": it.unit_price,
                "label_id": it.label_id,
                "discount_pct": it.discount_pct,
                "discount_amount": it.discount_amount,
                "delivery": it.delivery,
                "installation": it.installation,
                "sales_tax_pct": it.sales_tax_pct,
                "total_before_discount": it.total_before_discount,
                "total_after_discount": it.total_after_discount,
                # §4.3/§5.1 — the line's source order, for the Order ref column
                # and so re-approving credits the right order line.
                "source_order_id": it.source_order_id,
                "source_order_item_id": it.source_order_item_id,
                "order_ref": it.source_order_number or "",
            })
        for chg in invoice.charges_list:
            code = chg.charge_account.code if chg.charge_account else ""
            name = chg.charge_account.name if chg.charge_account else ""
            invoice_charges.append({
                "id": chg.id,
                "charge_account_id": chg.charge_account_id,
                "account_code": code,
                "account_name": name,
                # The modal's account picker renders from _display, so reopening
                # a saved invoice has to hand it back the same "code — name".
                "_display": f"{code} — {name}" if code else name,
                "description": chg.description,
                "amount": chg.amount,
                "scope": chg.scope,
                # Without these two a reopened invoice silently reverted to
                # pro-rata by value, losing the chosen method and any manual
                # split along with it.
                "distribution": chg.distribution or "pro_rata_value",
                "manual": _read_manual(chg.manual_allocations),
                "treatment": chg.treatment or "bill",
                "st_taxable": bool(chg.st_taxable),
                "wht_taxable": bool(chg.wht_taxable),
                "extra_taxable": bool(chg.extra_taxable),
                # Legacy keys kept for any reader still looking for them.
                "taxable": chg.taxable,
                "tax_base": chg.tax_base,
            })
    rs = ReportSettings.get()
    # Print output comes from the shared renderer (shared/invoice_print)
    # so the form's print and the bulk register print can never diverge.
    if invoice:
        from shared.invoice_print import render_sales_print_html
        rendered_template = render_sales_print_html(invoice)
    else:
        rendered_template = None

    # §5.1: the Order ref column exists only on an order-sourced invoice.
    order_sourced = any(i.get("source_order_id") for i in invoice_items)
    # The order-sourcing gate belongs to the INDIRECT flow: with "Direct
    # Invoice" selected under Sales in Settings, the form opens straight.
    show_source_gate = InventorySettings.get().sales_flow == "with_so"
    return render_template("invoices/form_inv.html",
                           invoice=invoice, invoice_items=invoice_items,
                           invoice_charges=invoice_charges,
                           per_line_labeling=InventorySettings.get().per_line_labeling_invoice,
                           order_sourced=order_sourced,
                           customers=customers,
                           party_mode=rs.party_mode("sales"),
                           invoice_settings=InvoiceSettings.get(),
                           invoice_template_text=rs.template_text("sales"),
                           rendered_template=rendered_template,
                           products=products, now=datetime.utcnow(),
                           project_labels=ProjectLabel.query.filter_by(is_active=True)
                           .order_by(ProjectLabel.name).all(),
                           default_label=InventorySettings.get().default_invoice_label(),
                           show_source_gate=show_source_gate)


@inv_inv_bp.route("/list")
@login_required
def list_invoices():
    status = request.args.get("status", "")
    query = InvInvoice.query
    if status:
        query = query.filter_by(voucher_status=status)
    invoices = query.order_by(InvInvoice.id.desc()).all()
    return render_template("invoices/list_inv.html", invoices=invoices)


def validate_invoice(data):
    errors = []
    if not data.get("customer_id"):
        errors.append("Customer is required")
    items = data.get("items", [])
    if not items:
        errors.append("At least one item is required")
    else:
        for i, row in enumerate(items):
            if not row.get("product_id"):
                errors.append(f"Row {i+1}: Product is required")
            try:
                qty = float(row.get("quantity", 0) or 0)
            except (TypeError, ValueError):
                qty = 0
            if qty <= 0:
                errors.append(f"Row {i+1}: Quantity must be greater than 0")
    from shared.doc_checks import amount_errors
    errors.extend(amount_errors(data))
    return errors


@inv_inv_bp.route("/save", methods=["POST"])
@login_required
def save_invoice():
    import traceback as _tb
    try:
        data = request.get_json(force=True)
        inv_id = data.get("id")
        action = data.get("action", "save")

        denied = deny_json("sales_invoices",
                       "approve" if action == "approve" else ("edit" if inv_id else "create"))
        if denied:
            return denied
    
        if inv_id:
            # Locked until commit: a second user saving or approving the same
            # invoice waits, then finds it approved instead of posting it again.
            inv = scoped_get_404(InvInvoice, inv_id, for_update=True)
            if inv.voucher_status == "approved":
                return jsonify({"ok": False, "error": "Cannot modify approved invoice"}), 400
            stale = stale_edit(inv, data.get("edit_version"), f"Invoice {inv.invoice_number}")
            if stale:
                return jsonify({"ok": False, "error": stale, "stale": True}), 409
        else:
            number = data.get("invoice_number")
            if not number or number == "(auto)":
                number = placeholder()
            inv = InvInvoice(
                voucher_number=number,
                invoice_number=number,
                created_by=current_user.id,
            )
            db.session.add(inv)
    
        if action == "approve":
            validation_errors = validate_invoice(data)
            if validation_errors:
                return jsonify({"ok": False, "error": "; ".join(validation_errors)}), 400
            from types import SimpleNamespace
            from shared.order_linkage import check_over_invoicing
            over = check_over_invoicing("sales", [
                SimpleNamespace(source_order_item_id=r.get("source_order_item_id"),
                                quantity=float(r.get("quantity", 0) or 0))
                for r in data.get("items", [])
            ])
            if over:
                return jsonify({"ok": False,
                                "error": "Over-invoicing blocked — " + "; ".join(over)}), 400
    
        inv.customer_id = data.get("customer_id")
        inv.party_account_id = data.get("party_account_id") or None
        _default_label = InventorySettings.get().default_invoice_label()
        _default_lid = _default_label.id if _default_label else None
        inv.label_id = data.get("label_id") or _default_lid
        inv.sales_order_id = data.get("sales_order_id") or None
        inv.due_date = datetime.strptime(data.get("due_date"), "%Y-%m-%d") if data.get("due_date") else None
        # The invoice posts — journal and stock — on ITS date, so a back-dated
        # invoice lands in its own period and is costed where it sits in time.
        try:
            inv.invoice_date = parse_doc_date(data.get("invoice_date"),
                                              fallback=inv.invoice_date)
        except DocumentDateError as e:
            return jsonify({"ok": False, "error": str(e)}), 400
        if inv.due_date and inv.due_date < inv.invoice_date:
            return jsonify({"ok": False,
                            "error": "Due date cannot be before the invoice date"}), 400
        inv.discount_mode = data.get("discount_mode", "general")
        inv.charges_mode = data.get("charges_mode", "general")
        inv.tax_mode = data.get("tax_mode", "general")
    
        inv.global_discount_pct = float(data.get("global_discount_pct", 0))
        inv.global_discount_value = float(data.get("global_discount_value", 0))
        inv.global_delivery = float(data.get("global_delivery", 0))
        inv.global_installation = float(data.get("global_installation", 0))
        inv.global_sales_tax_pct = float(data.get("global_sales_tax_pct", 0))
        inv.further_tax_pct = float(data.get("further_tax_pct", 0))
        inv.apply_further_tax = bool(data.get("apply_further_tax", False))
        inv.withholding_tax_pct = float(data.get("withholding_tax_pct", 0))
        inv.apply_withholding_tax = bool(data.get("apply_withholding_tax", False))
        inv.notes = data.get("notes", "")
        inv.subtotal = float(data.get("subtotal", 0))
        inv.total_discount = float(data.get("total_discount", 0))
        inv.total_charges = float(data.get("total_charges", 0))
        inv.total_tax = float(data.get("total_tax", 0))
        inv.total_further_tax = float(data.get("total_further_tax", 0))
        inv.total_withholding_tax = float(data.get("total_withholding_tax", 0))
        inv.total_amount = float(data.get("total_amount", 0))
    
        if action == "approve":
            inv.voucher_status = "approved"
            inv.approved_by = current_user.id
            inv.approved_at = datetime.utcnow()
        elif inv.voucher_status != "approved":
            inv.voucher_status = "unapproved"
    
        db.session.flush()
        if is_placeholder(inv.invoice_number):
            assign_number(inv)
        if action == "approve":
            # Every product this invoice moves, locked together and in one
            # order before any stock moves (shared/costing.py: lock_products).
            lock_products(r.get("product_id") for r in data.get("items", []))
    
        total_cogs = Decimal("0")
        cogs_by_label = {}
        InvInvoiceItem.query.filter_by(invoice_id=inv.id).delete()
        for row in data.get("items", []):
            item = InvInvoiceItem(
                invoice_id=inv.id,
                product_id=row.get("product_id"),
                description=row.get("description", ""),
                quantity=float(row.get("quantity", 1)),
                unit=row.get("unit", "pcs"),
                unit_price=float(row.get("unit_price", 0)),
                label_id=row.get("label_id") or inv.label_id or _default_lid,
                source_order_id=row.get("source_order_id") or None,
                source_order_item_id=row.get("source_order_item_id") or None,
                source_order_number=row.get("source_order_number", "") or "",
                discount_pct=float(row.get("discount_pct", 0)),
                discount_amount=float(row.get("discount_amount", 0)),
                delivery=float(row.get("delivery", 0)),
                installation=float(row.get("installation", 0)),
                sales_tax_pct=float(row.get("sales_tax_pct", 0)),
                total_before_discount=float(row.get("total_before_discount", 0)),
                total_after_discount=float(row.get("total_after_discount", 0)),
                comments=row.get("comments", ""),
            )
            db.session.add(item)
    
            if action == "approve" and item.product_id:
                prod = scoped_get(InvProduct, item.product_id)
                if prod:
                    db.session.add(InvStockMovement(
                        product_id=item.product_id, type="sale_out",
                        quantity=item.quantity,
                        reference_type="sales_invoice",
                        reference_id=inv.id,
                        notes=f"Approved invoice {inv.invoice_number}",
                        created_by=current_user.id,
                    ))
                    _unit, line_cogs = record_out(
                        item.product_id, "SI", inv.id, inv.voucher_number,
                        qty=item.quantity,
                        notes=f"Sale {inv.invoice_number}",
                        created_by=current_user.id,
                        txn_date=inv.invoice_date)
                    total_cogs += line_cogs
                    cogs_by_label[item.label_id] = (
                        cogs_by_label.get(item.label_id, Decimal("0")) + line_cogs)
    
        if action == "approve":
            from shared.order_linkage import apply_writeback
            apply_writeback("sales", InvInvoiceItem.query.filter_by(invoice_id=inv.id).all())
    
        AdditionalCharge.query.filter_by(doc_type="SI", doc_id=inv.id).delete()
        for chg in data.get("charges", []):
            if float(chg.get("amount", 0)) > 0 and chg.get("charge_account_id"):
                treatment = chg.get("treatment", "bill")
                if treatment not in ("bill", "absorb", "expense"):
                    treatment = "bill"
                billed = treatment == "bill"
                st = billed and bool(chg.get("st_taxable", chg.get("taxable", True)))
                db.session.add(AdditionalCharge(
                    doc_type="SI", doc_id=inv.id,
                    charge_account_id=int(chg["charge_account_id"]),
                    description=chg.get("description", ""),
                    amount=float(chg["amount"]),
                    scope=chg.get("scope", "general"),
                    distribution=chg.get("distribution", "pro_rata_value"),
                    manual_allocations=_manual_allocations(chg),
                    treatment=treatment,
                    st_taxable=st,
                    wht_taxable=billed and bool(chg.get("wht_taxable", False)),
                    extra_taxable=bool(chg.get("extra_taxable", False)),
                    taxable=st,
                    tax_base=chg.get("tax_base", "after_discount"),
                ))
        db.session.flush()
    
        totals = _sales_totals(inv)
        inv.total_charges = totals["billed"]
        inv.total_tax = totals["sales_tax"]
        inv.total_further_tax = totals["further_tax"]
        inv.total_withholding_tax = totals["wht"]
        inv.total_amount = totals["net_receivable"]
    
        if action == "approve":
            ar_acc = party_account("customer", inv.customer_id,
                                   inv.customer.name if inv.customer else None,
                                   inv.party_account_id)
            rev_acc = posting_account("revenue")
            cogs_acc = posting_account("cogs")
            inv_acc = posting_account("inventory")
            out_tax_acc = posting_account("sales_tax_payable")
            t = totals
            plid = inv.label_id          # party label → the AR line
            # Pooled economic lines take the FIRST item's label — items
            # already fell back to "party label, else default" on save, so a
            # single-label invoice posts 1:1 to that project.
            # Pooled economic lines (revenue, discount, charges) are split by
            # each item label's share of the goods value, so a multi-project
            # invoice reports each project's revenue — not all of it under the
            # first line's label. COGS is exact per label (costed per line).
            _items = InvInvoiceItem.query.filter_by(
                invoice_id=inv.id).order_by(InvInvoiceItem.id).all()
            weights = label_weights(
                (it.label_id or plid,
                 float(it.total_after_discount or 0)
                 or float(it.quantity or 0) * float(it.unit_price or 0))
                for it in _items) or {plid: Decimal("1")}

            def _split(account_id, debit, credit, text):
                amt = debit or credit
                for lid, share in split_by_label(amt, weights):
                    lines.append({"account_id": account_id,
                                  "debit": float(share) if debit else 0,
                                  "credit": float(share) if credit else 0,
                                  "label_id": lid, "description": text})

            lines = [
                {"account_id": ar_acc.id, "debit": t["net_receivable"], "credit": 0,
                 "label_id": plid,
                 "description": f"AR - {inv.invoice_number}"},
            ]
            for account_id, amount in _revenue_splits(inv, t["effective_subtotal"]):
                _split(account_id or rev_acc.id, 0, amount,
                       f"Revenue - {inv.invoice_number}")
            if t["discount"] > 0:
                disc_acc = ChartOfAccount.query.filter_by(code="4-02-02-01-0001").first() \
                    or posting_account("sales_returns")
                _split(disc_acc.id, t["discount"], 0,
                       f"Discount allowed - {inv.invoice_number}")
            for row in t["pools"]["billed_rows"]:
                _split(row.charge_account_id, 0, round(float(row.amount), 2),
                       f"{row.description or 'Charge'} - {inv.invoice_number}")
            if t["sales_tax"] > 0 and out_tax_acc:
                for account_id, amount in _output_tax_splits(inv, t["sales_tax"]):
                    lines.append(
                        {"account_id": account_id or out_tax_acc.id, "debit": 0,
                         "credit": amount,
                         "description": f"Output Tax - {inv.invoice_number}"},
                    )
            if t["further_tax"] > 0:
                ft_acc = posting_account("further_tax_payable")
                lines.append(
                    {"account_id": ft_acc.id, "debit": 0, "credit": t["further_tax"],
                     "description": f"Further Tax - {inv.invoice_number}"},
                )
            if t["wht"] > 0:
                wht_recv_acct = ChartOfAccount.query.filter_by(code="1-01-05-02-0001").first()
                if wht_recv_acct is None:
                    from shared.ledger_utils import get_or_create_account
                    wht_recv_acct = get_or_create_account(
                        "1-01-05-02-0001", "WHT Receivable", "asset")
                lines.append(
                    {"account_id": wht_recv_acct.id, "debit": t["wht"], "credit": 0,
                     "description": f"WHT Receivable - {inv.invoice_number}"},
                )
            for row in t["pools"]["expense_rows"]:
                amt = round(float(row.amount), 2)
                accrued_acc = posting_account("accrued")
                _split(row.charge_account_id, amt, 0,
                       f"{row.description or 'Charge'} (absorbed cost) - {inv.invoice_number}")
                lines.append(
                    {"account_id": accrued_acc.id, "debit": 0, "credit": amt,
                     "description": f"{row.description or 'Charge'} accrued - {inv.invoice_number}"},
                )
            if total_cogs > 0 and cogs_acc and inv_acc:
                for lid, cogs in cogs_by_label.items():
                    if not cogs:
                        continue
                    lid = lid or plid
                    lines.append(
                        {"account_id": cogs_acc.id, "debit": float(cogs), "credit": 0,
                         "label_id": lid,
                         "description": f"COGS - {inv.invoice_number}"},
                    )
                    lines.append(
                        {"account_id": inv_acc.id, "debit": 0, "credit": float(cogs),
                         "label_id": lid,
                         "description": f"Inventory - {inv.invoice_number}"},
                    )
            post_journal_entry(
                voucher_type="SI",
                voucher_id=inv.id,
                voucher_number=inv.voucher_number,
                description=f"Sales Invoice {inv.invoice_number} - {inv.customer.name if inv.customer else ''}",
                lines=lines,
                entry_date=inv.invoice_date,
                created_by=current_user.id,
            )
    
        db.session.commit()
        # Changing the total changes what the existing assignments settle, so
        # the invoice's paid/partial/unpaid state is re-derived here. Subledger
        # matching only — no journal is written by this call.
        from shared import payment_tracking as _pt
        _pt.sync_invoice(_pt.SALES, inv.id)
        if action == "approve":
            msg = "approved and locked"
        elif inv_id:
            msg = "changes saved"
        else:
            msg = "saved"
        return jsonify({"ok": True, "id": inv.id, "voucher_status": inv.voucher_status,
                        "edit_version": inv.edit_version,
                        "payment_status": inv.payment_status,
                        "number": inv.invoice_number, "voucher": inv.voucher_number,
                        "message": f"Invoice {msg}"})
    except (NegativeStockError, ClosedPeriodError, DocumentDateError) as e:
        # A business refusal, not a crash: nothing was committed.
        db.session.rollback()
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        db.session.rollback()
        from shared.concurrency import conflict_message
        msg = conflict_message(e)
        if msg:
            # Another user saved the same records at the same moment.
            return jsonify({"ok": False, "error": msg}), 409
        _tb.print_exc()
        return jsonify({"ok": False, "error": f"Server error: {e}"}), 500


@inv_inv_bp.route("/unapprove/<int:id>", methods=["POST"])
@login_required
def unapprove_invoice(id):
    denied = deny_json("sales_invoices", "approve")
    if denied:
        return denied
    inv = scoped_get_404(InvInvoice, id, for_update=True)
    if inv.voucher_status != "approved":
        return jsonify({"ok": False, "error": "Only approved invoices can be unapproved"}), 400
    # A posted return credits this invoice; un-posting the invoice under it
    # would leave the customer credited for a sale that no longer exists.
    from inventory_app.models.sales_return import InvSalesReturn
    returns = InvSalesReturn.query.filter_by(original_invoice_id=inv.id,
                                             status="approved").all()
    if returns:
        return jsonify({"ok": False, "error": (
            "Cannot unapprove: approved sales return(s) "
            + ", ".join(r.return_number for r in returns)
            + " credit this invoice. Unapprove them first.")}), 400
    # Reported to FBR: the tax authority holds this invoice as issued. Editing
    # it here would leave the books and FBR's record disagreeing; the
    # correction is a sales return (credit note), which is reported too.
    from shared.models.fbr import FBRInvoiceRecord
    reported = FBRInvoiceRecord.query.filter_by(inv_invoice_id=inv.id, status="success").first()
    if reported:
        return jsonify({"ok": False, "error": (
            f"Cannot unapprove: this invoice was reported to FBR"
            f"{' as ' + reported.fbr_invoice_number if reported.fbr_invoice_number else ''}. "
            "Correct it with a sales return (credit note).")}), 400

    try:
        reverse_journal_entry("SI", inv.id, current_user.id)
    except ClosedPeriodError as e:
        db.session.rollback()
        return jsonify({"ok": False, "error": str(e)}), 400

    # §4.4: un-posting gives the quantities back to the source orders, which
    # reopen (Fully -> Partially -> Open) as their balances are restored.
    from shared.order_linkage import apply_writeback
    apply_writeback("sales", InvInvoiceItem.query.filter_by(invoice_id=inv.id).all(),
                    sign=-1)

    inv.voucher_status = "unapproved"
    inv.payment_status = "unpaid"
    inv.approved_by = None
    inv.approved_at = None

    InvStockMovement.query.filter_by(
        reference_type="sales_invoice", reference_id=inv.id
    ).delete()

    # Remove this invoice's issues from the cost history. Later issues of the
    # same products are re-costed in date order and any change is journalled.
    reverse_voucher_stock("SI", inv.id, created_by=current_user.id)

    bump_edit(inv)
    db.session.commit()
    # An unapproved invoice owes nothing, so anything assigned to it stops
    # counting and the receipts return to the feed.
    from shared import payment_tracking as _pt
    _pt.sync_invoice(_pt.SALES, inv.id)
    return jsonify({"ok": True, "voucher_status": "unapproved",
                    "edit_version": inv.edit_version,
                    "message": "Invoice unapproved and unlocked"})


@inv_inv_bp.route("/delete/<int:id>", methods=["POST"])
@login_required
def delete_invoice(id):
    denied = deny_json("sales_invoices", "delete")
    if denied:
        return denied
    inv = scoped_get_404(InvInvoice, id, for_update=True)
    if inv.voucher_status == "approved":
        return jsonify({"ok": False, "error": "Cannot delete an approved invoice. Unapprove it first."}), 400
    try:
        InvInvoiceItem.query.filter_by(invoice_id=inv.id).delete()
        from shared import payment_tracking as _pt
        _pt.drop_allocations_for_doc(_pt.SALES, inv.id)
        db.session.delete(inv)
        db.session.commit()
        return jsonify({"ok": True, "message": "Invoice deleted successfully"})
    except Exception as e:
        db.session.rollback()
        return jsonify({"ok": False, "error": str(e)}), 500


@inv_inv_bp.route("/pay/<int:id>", methods=["POST"])
@login_required
def pay_invoice(id):
    """Retired: receipts are recorded as vouchers and assigned in Tracking.

    This used to post its own ``PMT`` journal (Dr Cash / Cr AR) and add to
    ``paid_amount`` directly. That made a second, invisible way for money to
    enter the books — one with no voucher behind it, unreachable from the
    accounting module and impossible to assign to anything — and it wrote the
    same ``paid_amount`` the tracker derives from assignments, so the two
    disagreed the moment both were used.

    A receipt is now a CRV/BRV (or a JV line) like any other, and Invoicing >
    Tracking assigns it to the invoices it settles. The route is kept so old
    links land somewhere useful instead of on a 404, and it deliberately
    neither posts nor writes anything.
    """
    inv = scoped_get_404(InvInvoice, id)
    flash("Record receipts as a Cash/Bank Receipt Voucher first.", "info")
    return redirect(url_for("inv_tracking.invoice_history",
                            doc_type="SI", doc_id=inv.id))


@inv_inv_bp.route("/api/products")
@login_required
def api_products():
    q = request.args.get("q", "").strip()
    query = InvProduct.query.filter_by(is_active=True)
    if q:
        query = query.filter(
            db.or_(
                InvProduct.name.ilike(f"%{q}%"),
                InvProduct.sku.ilike(f"%{q}%"),
            )
        )
    products = query.order_by(InvProduct.name).limit(20).all()
    return jsonify([{
        "id": p.id, "name": p.name, "sku": p.sku,
        "unit_price": p.unit_price, "current_stock": p.current_stock,
        "unit": p.unit, "weight": p.weight or 0,
    } for p in products])


@inv_inv_bp.route("/api/customers")
@login_required
def api_customers():
    q = request.args.get("q", "").strip()
    query = InvCustomer.query.filter_by(is_active=True)
    if q:
        query = query.filter(InvCustomer.name.ilike(f"%{q}%"))
    customers = query.order_by(InvCustomer.name).limit(20).all()
    return jsonify([{
        "id": c.id, "name": c.name, "city": c.city or "",
        "phone": c.phone or "", "address": c.address or "",
        # Shown in the party card once a customer is picked, as on the mockup.
        "tax_id": c.tax_id or "", "payment_terms": c.payment_terms or "",
    } for c in customers])


@inv_inv_bp.route("/api/orders/<int:customer_id>")
@login_required
def api_orders_for_customer(customer_id):
    """Approved orders for the picker (§4.2).

    Fully-invoiced orders are still returned so the user can see they exist,
    but flagged unselectable — the document greys them rather than hiding them.
    """
    from shared.order_linkage import picker_payload
    orders = InvSalesOrder.query.filter_by(
        customer_id=customer_id, status="approved").order_by(InvSalesOrder.id.desc()).all()
    return jsonify([picker_payload("sales", o) for o in orders])


@inv_inv_bp.route("/api/accounts")
@login_required
def api_charge_accounts():
    q = request.args.get("q", "").strip()
    query = ChartOfAccount.query.filter_by(is_active=True, level=5)
    if q:
        query = query.filter(
            db.or_(
                ChartOfAccount.name.ilike(f"%{q}%"),
                ChartOfAccount.code.ilike(f"%{q}%"),
            )
        )
    accts = query.order_by(ChartOfAccount.code).limit(30).all()
    # §11.1: a ledger's admin defaults ride along, so picking it in the charges
    # form seeds the treatment and tax-base switches instead of leaving the
    # user to set the same thing every time.
    from shared.models.invoice_settings import ChargeLedgerDefault
    defaults = {d.account_id: d for d in ChargeLedgerDefault.query.filter(
        ChargeLedgerDefault.account_id.in_([a.id for a in accts] or [0])).all()}
    out = []
    for a in accts:
        row = {"id": a.id, "code": a.code, "name": a.name, "type": a.type}
        d = defaults.get(a.id)
        if d and d.applies_to in ("both", "sales"):
            row["defaults"] = {"treatment": d.treatment, "st_taxable": d.st_taxable,
                               "wht_taxable": d.wht_taxable, "extra_taxable": d.extra_taxable}
        out.append(row)
    return jsonify(out)
