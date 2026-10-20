from io import BytesIO
from flask import Blueprint, render_template, redirect, url_for, flash, request, send_file
from flask_login import login_required
from inventory_app.extensions import db
from shared.tenancy import scoped_get_404
from inventory_app.models.supplier import InvSupplier
from shared.ledger_utils import create_entity_account
from shared.permissions import deny_page
from shared import master_data as md

inv_sup_bp = Blueprint("inv_suppliers", __name__, url_prefix="/inventory/suppliers")


def _clean(raw, exclude_id=None):
    return md.clean_party(InvSupplier, raw, exclude_id=exclude_id)


def _form_values():
    return {f: request.form.get(f, "") for f in md.SUPPLIER_COLUMNS}


@inv_sup_bp.route("/")
@login_required
def list_suppliers():
    q = request.args.get("q", "")
    query = InvSupplier.query
    if q:
        query = query.filter(
            InvSupplier.name.ilike(f"%{q}%") | InvSupplier.city.ilike(f"%{q}%")
        )
    suppliers = query.order_by(InvSupplier.name).all()
    return render_template("suppliers/list_inv.html", suppliers=suppliers)


@inv_sup_bp.route("/create", methods=["GET", "POST"])
@login_required
def create_supplier():
    if deny_page("suppliers", "create"):
        return redirect(url_for("inv_suppliers.list_suppliers"))
    if request.method == "POST":
        raw = _form_values()
        values, errors = _clean(raw)
        if errors:
            flash(" ".join(errors), "error")
            return render_template("suppliers/form_inv.html", supplier=None, draft=raw)
        s = InvSupplier(**values)
        db.session.add(s)
        db.session.flush()
        create_entity_account("supplier", s.id, s.name)
        db.session.commit()
        flash(f"Supplier created — ledger account '{s.name}' added under Trade Creditors", "success")
        return redirect(url_for("inv_suppliers.list_suppliers"))
    return render_template("suppliers/form_inv.html", supplier=None)


@inv_sup_bp.route("/edit/<int:id>", methods=["GET", "POST"])
@login_required
def edit_supplier(id):
    if deny_page("suppliers", "edit"):
        return redirect(url_for("inv_suppliers.list_suppliers"))
    s = scoped_get_404(InvSupplier, id, for_update=request.method == "POST")
    if request.method == "POST":
        raw = _form_values()
        raw["is_active"] = request.form.get("is_active") == "on"
        values, errors = _clean(raw, exclude_id=s.id)
        if errors:
            flash(" ".join(errors), "error")
            return render_template("suppliers/form_inv.html", supplier=s, draft=raw)
        for field, value in values.items():
            setattr(s, field, value)
        s.is_active = raw["is_active"]
        create_entity_account("supplier", s.id, s.name)
        db.session.commit()
        flash("Supplier updated", "success")
        return redirect(url_for("inv_suppliers.list_suppliers"))
    return render_template("suppliers/form_inv.html", supplier=s)


@inv_sup_bp.route("/delete/<int:id>", methods=["POST"])
@login_required
def delete_supplier(id):
    if deny_page("suppliers", "delete"):
        return redirect(url_for("inv_suppliers.list_suppliers"))
    s = scoped_get_404(InvSupplier, id)
    # Only purchase orders were checked: a supplier with posted purchase
    # invoices, returns or payments could be deleted, orphaning its
    # documents and its ledger.
    from inventory_app.models.purchase_invoice import InvPurchaseInvoice
    from inventory_app.models.purchase_return import InvPurchaseReturn
    from shared.ledger_utils import entity_has_postings
    if (s.purchase_orders.count() > 0
            or InvPurchaseInvoice.query.filter_by(supplier_id=s.id).first()
            or InvPurchaseReturn.query.filter_by(supplier_id=s.id).first()
            or entity_has_postings("supplier", s.id)):
        flash("Cannot delete a supplier with purchase history or ledger entries", "error")
    else:
        db.session.delete(s)
        db.session.commit()
        flash("Supplier deleted", "success")
    return redirect(url_for("inv_suppliers.list_suppliers"))


@inv_sup_bp.route("/check")
@login_required
def check_duplicate():
    value = request.args.get("value", "").strip()
    exclude = request.args.get("exclude", type=int)
    if not value:
        return {"exists": False}
    # Same rule the save applies: case and spacing do not make a new name.
    return {"exists": md.taken(InvSupplier, InvSupplier.name, value, exclude)}


@inv_sup_bp.route("/template")
@login_required
def download_template():
    try:
        import openpyxl
    except ImportError:
        flash("Excel support is not installed on the server.", "error")
        return redirect(url_for("inv_suppliers.list_suppliers"))
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Suppliers"
    ws.append(["Name", "Contact Person", "Email", "Phone", "Mobile", "City", "Tax ID", "Payment Terms", "Website", "Address", "Notes"])
    ws.append(["Sample Supplier A", "Ali Khan", "ali@supplier.com", "021-1112222", "0300-1112222", "Karachi", "NTN-7654321", "Net 30", "https://supplier-a.com", "789 Industrial Rd", ""])
    ws.append(["Sample Supplier B", "Sara Ahmed", "sara@supplier.com", "042-3334444", "0301-3334444", "Lahore", "", "Net 45", "", "321 Trade Ave", "Preferred vendor"])
    ws.append(["Sample Supplier C", "", "", "", "", "Islamabad", "", "Due on Receipt", "", "", ""])
    for col, w in enumerate([28, 18, 24, 14, 14, 14, 16, 16, 22, 24, 24], start=1):
        ws.column_dimensions[chr(64 + col)].width = w
    out = BytesIO()
    wb.save(out)
    out.seek(0)
    return send_file(out, as_attachment=True, download_name="supplier_import_template.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


def _review(rows, created=0):
    return render_template("suppliers/batch_editor.html", rows=rows,
                           flagged=sum(1 for r in rows if r.get("row_errors")),
                           created=created)


@inv_sup_bp.route("/upload-excel", methods=["POST"])
@login_required
def upload_excel():
    file = request.files.get("file")
    if not file or not file.filename:
        flash("No file selected.", "error")
        return redirect(url_for("inv_suppliers.list_suppliers"))
    try:
        rows = md.import_rows(md.read_sheet(file), md.SUPPLIER_COLUMNS, _clean)
    except md.Invalid as e:
        flash(str(e), "error")
        return redirect(url_for("inv_suppliers.list_suppliers"))
    if not rows:
        flash("The sheet has no suppliers under its heading row.", "error")
        return redirect(url_for("inv_suppliers.list_suppliers"))
    # The review screen is this response — the rows no longer ride in the
    # session cookie, which the browser drops past 4 KB.
    return _review(rows)


@inv_sup_bp.route("/batch", methods=["GET", "POST"])
@login_required
def batch_editor():
    if request.method == "POST":
        try:
            rows = md.submitted_rows(request.form.get("data"))
        except md.Invalid as e:
            flash(str(e), "error")
            return redirect(url_for("inv_suppliers.list_suppliers"))

        def create(values):
            s = InvSupplier(**values)
            db.session.add(s)
            db.session.flush()
            create_entity_account("supplier", s.id, s.name)

        created, failed = md.save_rows(rows, _clean, create)
        db.session.commit()
        if failed:
            return _review(failed, created=created)
        flash(f"{created} supplier(s) created.", "success")
        return redirect(url_for("inv_suppliers.list_suppliers"))
    return _review([])
