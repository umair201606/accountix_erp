from io import BytesIO
from flask import Blueprint, render_template, redirect, url_for, flash, request, send_file
from flask_login import login_required
from inventory_app.extensions import db
from shared.tenancy import scoped_get_404
from inventory_app.models.customer import InvCustomer
from shared.ledger_utils import create_entity_account
from shared.permissions import deny_page
from shared import master_data as md

inv_cust_bp = Blueprint("inv_customers", __name__, url_prefix="/inventory/customers")


def _clean(raw, exclude_id=None):
    return md.clean_party(InvCustomer, raw, credit_limit=True, exclude_id=exclude_id)


def _form_values():
    return {f: request.form.get(f, "") for f in md.CUSTOMER_COLUMNS}


@inv_cust_bp.route("/")
@login_required
def list_customers():
    q = request.args.get("q", "")
    query = InvCustomer.query
    if q:
        query = query.filter(
            InvCustomer.name.ilike(f"%{q}%") | InvCustomer.city.ilike(f"%{q}%")
        )
    customers = query.order_by(InvCustomer.name).all()
    return render_template("customers/list_inv.html", customers=customers)


@inv_cust_bp.route("/create", methods=["GET", "POST"])
@login_required
def create_customer():
    if deny_page("customers", "create"):
        return redirect(url_for("inv_customers.list_customers"))
    if request.method == "POST":
        raw = _form_values()
        values, errors = _clean(raw)
        if errors:
            # Shown again with what was typed: a blank or duplicate name used
            # to be saved as it came (the duplicate check was only a hint in
            # the browser).
            flash(" ".join(errors), "error")
            return render_template("customers/form_inv.html", customer=None, draft=raw)
        c = InvCustomer(**values)
        db.session.add(c)
        db.session.flush()
        create_entity_account("customer", c.id, c.name)
        db.session.commit()
        flash(f"Customer created — ledger account '{c.name}' added under Trade Debtors", "success")
        return redirect(url_for("inv_customers.list_customers"))
    return render_template("customers/form_inv.html", customer=None)


@inv_cust_bp.route("/edit/<int:id>", methods=["GET", "POST"])
@login_required
def edit_customer(id):
    if deny_page("customers", "edit"):
        return redirect(url_for("inv_customers.list_customers"))
    c = scoped_get_404(InvCustomer, id, for_update=request.method == "POST")
    if request.method == "POST":
        raw = _form_values()
        raw["is_active"] = request.form.get("is_active") == "on"
        values, errors = _clean(raw, exclude_id=c.id)
        if errors:
            flash(" ".join(errors), "error")
            return render_template("customers/form_inv.html", customer=c, draft=raw)
        for field, value in values.items():
            setattr(c, field, value)
        c.is_active = raw["is_active"]
        create_entity_account("customer", c.id, c.name)
        db.session.commit()
        flash("Customer updated", "success")
        return redirect(url_for("inv_customers.list_customers"))
    return render_template("customers/form_inv.html", customer=c)


@inv_cust_bp.route("/delete/<int:id>", methods=["POST"])
@login_required
def delete_customer(id):
    if deny_page("customers", "delete"):
        return redirect(url_for("inv_customers.list_customers"))
    c = scoped_get_404(InvCustomer, id)
    from inventory_app.models.sales_return import InvSalesReturn
    from shared.ledger_utils import entity_has_postings
    if (c.sales_orders.count() > 0 or c.invoices.count() > 0
            or InvSalesReturn.query.filter_by(customer_id=c.id).first()
            or entity_has_postings("customer", c.id)):
        flash("Cannot delete a customer with sales history or ledger entries", "error")
    else:
        db.session.delete(c)
        db.session.commit()
        flash("Customer deleted", "success")
    return redirect(url_for("inv_customers.list_customers"))


@inv_cust_bp.route("/check")
@login_required
def check_duplicate():
    value = request.args.get("value", "").strip()
    exclude = request.args.get("exclude", type=int)
    if not value:
        return {"exists": False}
    # Same rule the save applies: case and spacing do not make a new name.
    return {"exists": md.taken(InvCustomer, InvCustomer.name, value, exclude)}


@inv_cust_bp.route("/template")
@login_required
def download_template():
    try:
        import openpyxl
    except ImportError:
        flash("Excel support is not installed on the server.", "error")
        return redirect(url_for("inv_customers.list_customers"))
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Customers"
    ws.append(["Name", "Contact Person", "Email", "Phone", "Mobile", "City", "Tax ID", "Payment Terms", "Credit Limit", "Website", "Address", "Notes"])
    ws.append(["Sample Customer A", "John Doe", "john@example.com", "021-1234567", "0300-1234567", "Karachi", "NTN-1234567", "Net 30", "50000", "https://example.com", "123 Main St", "Regular customer"])
    ws.append(["Sample Customer B", "Jane Smith", "jane@example.com", "042-7654321", "0301-7654321", "Lahore", "", "Due on Receipt", "25000", "", "456 Oak Ave", ""])
    ws.append(["Sample Customer C", "", "", "", "", "Islamabad", "", "Net 15", "10000", "", "", ""])
    for col, w in enumerate([28, 18, 24, 14, 14, 14, 16, 16, 12, 22, 24, 24], start=1):
        ws.column_dimensions[chr(64 + col)].width = w
    out = BytesIO()
    wb.save(out)
    out.seek(0)
    return send_file(out, as_attachment=True, download_name="customer_import_template.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


def _review(rows, created=0):
    return render_template("customers/batch_editor.html", rows=rows,
                           flagged=sum(1 for r in rows if r.get("row_errors")),
                           created=created)


@inv_cust_bp.route("/upload-excel", methods=["POST"])
@login_required
def upload_excel():
    file = request.files.get("file")
    if not file or not file.filename:
        flash("No file selected.", "error")
        return redirect(url_for("inv_customers.list_customers"))
    try:
        rows = md.import_rows(md.read_sheet(file), md.CUSTOMER_COLUMNS, _clean)
    except md.Invalid as e:
        flash(str(e), "error")
        return redirect(url_for("inv_customers.list_customers"))
    if not rows:
        flash("The sheet has no customers under its heading row.", "error")
        return redirect(url_for("inv_customers.list_customers"))
    # The review screen is this response. The rows used to ride to it in the
    # session cookie, which the browser drops past 4 KB — about 40 customers
    # — so the screen opened empty and the import silently did nothing.
    return _review(rows)


@inv_cust_bp.route("/batch", methods=["GET", "POST"])
@login_required
def batch_editor():
    if request.method == "POST":
        try:
            rows = md.submitted_rows(request.form.get("data"))
        except md.Invalid as e:
            flash(str(e), "error")
            return redirect(url_for("inv_customers.list_customers"))

        def create(values):
            c = InvCustomer(**values)
            db.session.add(c)
            db.session.flush()
            create_entity_account("customer", c.id, c.name)

        created, failed = md.save_rows(rows, _clean, create)
        db.session.commit()
        if failed:
            # Back on the screen, each with its reason — the good rows are
            # saved, these wait to be fixed.
            return _review(failed, created=created)
        flash(f"{created} customer(s) created.", "success")
        return redirect(url_for("inv_customers.list_customers"))
    return _review([])
