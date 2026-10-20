from io import BytesIO
from types import SimpleNamespace
from flask import Blueprint, render_template, redirect, url_for, flash, request, send_file
from flask_login import login_required, current_user
from ..extensions import db
from shared.tenancy import scoped_get_404
from ..models.product import InvProduct
from ..models.category import InvCategory
from ..models.unit import InvUnit
from ..models.stock_movement import InvStockMovement
from shared.ledger_utils import create_entity_account
from shared.permissions import deny_page
from shared import master_data as md

inv_prod_bp = Blueprint("inv_products", __name__, url_prefix="/inventory/products")


@inv_prod_bp.route("/")
@login_required
def list_products():
    q = request.args.get("q", "")
    cat_id = request.args.get("category_id", type=int)
    query = InvProduct.query
    if q:
        query = query.filter(InvProduct.name.ilike(f"%{q}%") | InvProduct.sku.ilike(f"%{q}%"))
    if cat_id:
        query = query.filter_by(category_id=cat_id)
    products = query.order_by(InvProduct.name).all()
    categories = InvCategory.query.filter_by(is_active=True).all()
    return render_template("products/list_inv.html", products=products, categories=categories)


def _units_for(product=None):
    """Unit rows for the form's select: the active units, plus the product's
    own unit if it was switched off since (or the select would silently move
    the product to the first unit in the list on its next save)."""
    units = InvUnit.query.filter_by(is_active=True).order_by(InvUnit.name).all()
    if product is not None and product.unit and product.unit not in {
            u.abbreviation for u in units}:
        own = InvUnit.query.filter_by(abbreviation=product.unit).first()
        units.append(own or SimpleNamespace(name=product.unit,
                                            abbreviation=product.unit))
    return units


PRODUCT_FORM_FIELDS = ("sku", "name", "description", "category_id", "unit_price",
                       "cost_price", "reorder_level", "current_stock", "unit",
                       "hs_code", "weight", "opening_date")


def _form_page(product=None, draft=None, cost_locked=False):
    categories = InvCategory.query.filter_by(is_active=True).all()
    return render_template("products/form_inv.html", product=product, draft=draft,
                           categories=categories, units=_units_for(product),
                           cost_locked=cost_locked)


@inv_prod_bp.route("/create", methods=["GET", "POST"])
@login_required
def create_product():
    if deny_page("products", "create"):
        return redirect(url_for("inv_products.list_products"))
    if request.method == "POST":
        raw = {f: request.form.get(f, "") for f in PRODUCT_FORM_FIELDS}
        values, errors = md.clean_product(raw, choices=md.unit_choices())
        when = None
        if not errors and values["current_stock"] > 0:
            from shared.posting_helpers import DocumentDateError, parse_doc_date
            try:
                when = parse_doc_date(raw["opening_date"])
            except DocumentDateError as e:
                errors.append(str(e))
            else:
                refusal = md.opening_date_refusal(when)
                if refusal:
                    errors.append(refusal)
        if errors:
            # Shown again with what was typed. A duplicate SKU used to reach
            # the database's unique key and come back as a server error.
            db.session.rollback()
            flash(" ".join(errors), "error")
            return _form_page(draft=raw)
        opening_qty = values.pop("current_stock")
        values.pop("category_name", None)
        prod = InvProduct(**values, current_stock=0)
        db.session.add(prod)
        db.session.flush()
        create_entity_account("product", prod.id, f"{prod.name} ({prod.sku})")
        # Opening stock is a real receipt: a cost layer at the opening cost and
        # Dr Inventory / Cr Opening Balance Equity on the opening date — not
        # just a number in the stock column the ledgers never saw.
        if opening_qty > 0:
            from shared.stock_entry import post_opening_stock
            post_opening_stock(prod, opening_qty, prod.cost_price or 0,
                               when=when, created_by=current_user.id)
        db.session.commit()
        flash(f"Product created — ledger account '{prod.name} ({prod.sku})' added under Inventory", "success")
        return redirect(url_for("inv_products.list_products"))
    return _form_page()


@inv_prod_bp.route("/edit/<int:id>", methods=["GET", "POST"])
@login_required
def edit_product(id):
    if deny_page("products", "edit"):
        return redirect(url_for("inv_products.list_products"))
    prod = scoped_get_404(InvProduct, id, for_update=request.method == "POST")
    from shared.models.stock_ledger import StockLedger
    # Once stock has moved, the costing engine keeps the cost at the current
    # valuation; the form's copy (read when the page opened) would overwrite
    # it with a stale one.
    cost_locked = StockLedger.query.filter_by(product_id=prod.id).first() is not None
    if request.method == "POST":
        raw = {f: request.form.get(f, "") for f in PRODUCT_FORM_FIELDS}
        raw["is_active"] = request.form.get("is_active") == "on"
        if cost_locked:
            raw["cost_price"] = prod.cost_price or 0
        values, errors = md.clean_product(raw, choices=md.unit_choices(prod.unit),
                                          exclude_id=prod.id, opening=False)
        if errors:
            flash(" ".join(errors), "error")
            return _form_page(product=prod, draft=raw, cost_locked=cost_locked)
        values.pop("category_name", None)
        if cost_locked:
            values.pop("cost_price")
        for field, value in values.items():
            setattr(prod, field, value)
        prod.is_active = raw["is_active"]
        create_entity_account("product", prod.id, f"{prod.name} ({prod.sku})")
        db.session.commit()
        flash("Product updated", "success")
        return redirect(url_for("inv_products.list_products"))
    return _form_page(product=prod, cost_locked=cost_locked)


@inv_prod_bp.route("/delete/<int:id>", methods=["POST"])
@login_required
def delete_product(id):
    if deny_page("products", "delete"):
        return redirect(url_for("inv_products.list_products"))
    # Locked: a sale of this product in flight finishes first, and its stock
    # history then refuses the delete.
    prod = scoped_get_404(InvProduct, id, for_update=True)
    from shared.models.stock_ledger import StockLedger
    if prod.po_items.count() > 0 or prod.so_items.count() > 0:
        flash("Cannot delete product with order history", "error")
    elif StockLedger.query.filter_by(product_id=prod.id).first() is not None:
        # Its stock movements and their journals are history; deleting the
        # product would orphan them. Deactivate it instead.
        flash("Cannot delete a product with stock history — mark it inactive "
              "instead.", "error")
    else:
        db.session.delete(prod)
        db.session.commit()
        flash("Product deleted", "success")
    return redirect(url_for("inv_products.list_products"))


@inv_prod_bp.route("/adjust-stock/<int:id>", methods=["GET", "POST"])
@login_required
def adjust_stock(id):
    if deny_page("adjustment_vouchers", "approve"):
        return redirect(url_for("inv_products.list_products"))
    prod = scoped_get_404(InvProduct, id)
    if request.method == "POST":
        note = request.form.get("notes", "")
        try:
            qty = float(md.number(request.form.get("quantity"), "Quantity", minimum=None))
        except md.Invalid as e:
            qty = None
            flash(str(e), "error")
        if qty == 0:
            flash("Quantity must be non-zero", "error")
        elif qty is not None:
            # Was: current_stock += qty and nothing else — no cost, no
            # journal, so the inventory account never followed. Now an
            # approved Stock Adjustment voucher (reviewable, reversible).
            from shared.stock_entry import quick_adjustment
            from shared.posting_helpers import parse_doc_date
            v = quick_adjustment(prod, qty, note or f"Manual adjustment of {qty:g}",
                                 when=parse_doc_date(request.form.get("date")),
                                 created_by=current_user.id)
            db.session.add(InvStockMovement(
                product_id=prod.id,
                type="adjustment_in" if qty > 0 else "adjustment_out",
                quantity=abs(qty), reference_type="stock_adjustment",
                reference_id=v.id, notes=note or f"Manual adjustment of {qty:g}",
                created_by=current_user.id))
            db.session.commit()
            flash(f"Stock adjusted by {qty:g} ({v.voucher_number}). New stock: "
                  f"{prod.current_stock}", "success")
            return redirect(url_for("inv_products.list_products"))
    return render_template("products/adjust_stock_inv.html", product=prod)


@inv_prod_bp.route("/check")
@login_required
def check_duplicate():
    field = request.args.get("field")
    value = request.args.get("value", "").strip()
    exclude = request.args.get("exclude", type=int)
    if field not in ("name", "sku") or not value:
        return {"exists": False}
    # Same rule the save applies: case and spacing do not make a new one.
    return {"exists": md.taken(InvProduct, getattr(InvProduct, field), value, exclude)}


@inv_prod_bp.route("/template")
@login_required
def download_template():
    try:
        import openpyxl
    except ImportError:
        flash("Excel support is not installed on the server.", "error")
        return redirect(url_for("inv_products.list_products"))
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Products"
    # Column order is what the import reads (md.PRODUCT_COLUMNS); HS Code was
    # read from the tenth column but missing from the heading.
    ws.append(["SKU", "Name", "Description", "Category", "Unit Price", "Cost Price",
               "Reorder Level", "Opening Stock", "Unit", "HS Code"])
    ws.append(["SKU-001", "Sample Product A", "Description for product A", "Category Name", "100", "70", "10", "50", "pcs", "8541.4000"])
    ws.append(["SKU-002", "Sample Product B", "Description for product B", "Category Name", "250", "180", "5", "20.5", "kg", ""])
    ws.append(["SKU-003", "Sample Product C", "Description for product C", "Category Name", "50", "30", "20", "100", "m", ""])
    for col, width in zip("ABCDEFGHIJ", (14, 22, 28, 16, 12, 12, 14, 14, 10, 12)):
        ws.column_dimensions[col].width = width
    out = BytesIO()
    wb.save(out)
    out.seek(0)
    return send_file(out, as_attachment=True, download_name="product_import_template.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


def _batch_cleaner():
    """One validator for a whole sheet or batch: units and categories read
    once, not per row."""
    choices = md.unit_choices()
    categories = md.category_index()

    def clean(raw):
        return md.clean_product(raw, choices=choices, categories=categories,
                                upper_sku=True)
    return clean


def _review(rows, created=0, opening_date="", date_error=None):
    from shared.clock import local_today
    units = InvUnit.query.filter_by(is_active=True).order_by(InvUnit.name).all()
    return render_template("products/batch_editor.html", rows=rows, units=units,
                           flagged=sum(1 for r in rows if r.get("row_errors")),
                           created=created, date_error=date_error,
                           opening_date=opening_date or local_today().isoformat())


@inv_prod_bp.route("/upload-excel", methods=["POST"])
@login_required
def upload_excel():
    file = request.files.get("file")
    if not file or not file.filename:
        flash("No file selected.", "error")
        return redirect(url_for("inv_products.list_products"))
    try:
        rows = md.import_rows(md.read_sheet(file), md.PRODUCT_COLUMNS,
                              _batch_cleaner(), name_fields=("sku", "name"))
    except md.Invalid as e:
        flash(str(e), "error")
        return redirect(url_for("inv_products.list_products"))
    if not rows:
        flash("The sheet has no products under its heading row.", "error")
        return redirect(url_for("inv_products.list_products"))
    # The review screen is this response — the rows no longer ride in the
    # session cookie, which the browser drops past 4 KB.
    return _review(rows)


@inv_prod_bp.route("/batch", methods=["GET", "POST"])
@login_required
def batch_editor():
    if request.method == "POST":
        try:
            rows = md.submitted_rows(request.form.get("data"))
        except md.Invalid as e:
            flash(str(e), "error")
            return redirect(url_for("inv_products.list_products"))
        opening_date = request.form.get("opening_date", "")
        # Opening stock is dated once for the whole import (the go-live
        # date); a closed period refuses it here, with the rows kept, rather
        # than half-way through the save.
        when = None
        if any(_has_stock(r) for r in rows):
            from shared.posting_helpers import DocumentDateError, parse_doc_date
            try:
                when = parse_doc_date(opening_date)
            except DocumentDateError as e:
                return _review(rows, opening_date=opening_date, date_error=str(e))
            refusal = md.opening_date_refusal(when)
            if refusal:
                db.session.rollback()
                return _review(rows, opening_date=opening_date, date_error=refusal)

        def create(values):
            qty = values.pop("current_stock")
            values.pop("category_name", None)
            prod = InvProduct(**values, current_stock=0)
            db.session.add(prod)
            db.session.flush()
            create_entity_account("product", prod.id, f"{prod.name} ({prod.sku})")
            if qty > 0:
                from shared.stock_entry import post_opening_stock
                post_opening_stock(prod, qty, prod.cost_price or 0, when=when,
                                   created_by=current_user.id)

        created, failed = md.save_rows(rows, _batch_cleaner(), create,
                                       name_fields=("sku", "name"))
        db.session.commit()
        if failed:
            return _review(failed, created=created, opening_date=opening_date)
        flash(f"{created} product(s) created.", "success")
        return redirect(url_for("inv_products.list_products"))
    return _review([])


def _has_stock(raw):
    try:
        return md.number(raw.get("current_stock"), "Opening stock") > 0
    except md.Invalid:
        return False
