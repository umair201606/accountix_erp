from io import BytesIO
from flask import Blueprint, render_template, redirect, url_for, flash, request, send_file, jsonify
from flask_login import login_required
from ..extensions import db
from shared.tenancy import scoped_get_404
from ..models.unit import InvUnit
from ..models.product import InvProduct
from shared import master_data as md

inv_units_bp = Blueprint("inv_units", __name__, url_prefix="/inventory/units")


@inv_units_bp.route("/check")
@login_required
def check_duplicate():
    field = request.args.get("field")
    value = request.args.get("value", "").strip()
    exclude = request.args.get("exclude", type=int)
    if field not in ("name", "abbreviation") or not value:
        return {"exists": False}
    # Same rule the save applies: case and spacing do not make a new unit.
    return {"exists": md.taken(InvUnit, getattr(InvUnit, field), value, exclude)}


@inv_units_bp.route("/template")
@login_required
def download_template():
    try:
        import openpyxl
    except ImportError:
        flash("Excel support is not installed on the server.", "error")
        return redirect(url_for("inv_units.list_units"))
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Units"
    ws.append(["Name", "Abbreviation", "Explanation"])
    ws.append(["Kilogram", "kg", "Weight in kilograms"])
    ws.append(["Meter", "m", "Length in meters"])
    ws.append(["Piece", "pcs", "Individual unit count"])
    ws.column_dimensions["A"].width = 24
    ws.column_dimensions["B"].width = 16
    ws.column_dimensions["C"].width = 30
    out = BytesIO()
    wb.save(out)
    out.seek(0)
    return send_file(out, as_attachment=True, download_name="unit_import_template.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@inv_units_bp.route("/")
@login_required
def list_units():
    units = InvUnit.query.order_by(InvUnit.name).all()
    return render_template("units/list_inv.html", units=units)


def _form_values():
    return {f: request.form.get(f, "") for f in md.UNIT_COLUMNS}


def _products_using(abbreviation):
    return InvProduct.query.filter(InvProduct.unit == abbreviation)


@inv_units_bp.route("/create", methods=["GET", "POST"])
@login_required
def create_unit():
    if request.method == "POST":
        raw = _form_values()
        values, errors = md.clean_unit(raw)
        if errors:
            flash(" ".join(errors), "error")
            return render_template("units/form_inv.html", unit=None, **values)
        unit = InvUnit(name=values["name"], abbreviation=values["abbreviation"],
                       explanation=values["explanation"] or None)
        db.session.add(unit)
        db.session.commit()
        flash(f"Unit '{unit.name}' created.", "success")
        return redirect(url_for("inv_units.list_units"))

    return render_template("units/form_inv.html", unit=None, name="", abbreviation="", explanation="")


@inv_units_bp.route("/edit/<int:id>", methods=["GET", "POST"])
@login_required
def edit_unit(id):
    unit = scoped_get_404(InvUnit, id, for_update=request.method == "POST")
    if request.method == "POST":
        raw = _form_values()
        values, errors = md.clean_unit(raw, exclude_id=unit.id)
        if errors:
            flash(" ".join(errors), "error")
            return render_template("units/form_inv.html", unit=unit, **values)
        old = unit.abbreviation
        unit.name = values["name"]
        unit.abbreviation = values["abbreviation"]
        unit.explanation = values["explanation"] or None
        unit.is_active = request.form.get("is_active") == "on"
        moved = 0
        if old != unit.abbreviation:
            # Products record their unit by abbreviation: renaming "Kg" to
            # "kg" used to leave every product on the old spelling, which no
            # unit matched any more (the product form then showed the first
            # unit in the list and saved that instead).
            # Row by row, so each product's change is in the audit log.
            for product in _products_using(old).all():
                product.unit = unit.abbreviation
                moved += 1
        db.session.commit()
        flash(f"Unit '{unit.name}' updated."
              + (f" {moved} product(s) moved to '{unit.abbreviation}'." if moved else ""),
              "success")
        return redirect(url_for("inv_units.list_units"))

    return render_template("units/form_inv.html", unit=unit)


@inv_units_bp.route("/delete/<int:id>", methods=["POST"])
@login_required
def delete_unit(id):
    unit = scoped_get_404(InvUnit, id, for_update=True)
    name = unit.name
    in_use = _products_using(unit.abbreviation).count()
    if in_use:
        flash(f"{in_use} product(s) use '{unit.abbreviation}' — move them to another "
              f"unit first, or switch this unit off instead of deleting it.", "error")
        return redirect(url_for("inv_units.list_units"))
    db.session.delete(unit)
    db.session.commit()
    flash(f"Unit '{name}' deleted.", "success")
    return redirect(url_for("inv_units.list_units"))


@inv_units_bp.route("/quick-create", methods=["POST"])
@login_required
def quick_create():
    values, errors = md.clean_unit(_form_values())
    if errors:
        return jsonify(ok=False, error=" ".join(errors))
    unit = InvUnit(name=values["name"], abbreviation=values["abbreviation"],
                   explanation=values["explanation"] or None)
    db.session.add(unit)
    db.session.commit()
    return jsonify(ok=True, id=unit.id, name=unit.name, abbreviation=unit.abbreviation)


def _review(rows, created=0):
    return render_template("units/batch_editor.html", rows=rows,
                           flagged=sum(1 for r in rows if r.get("row_errors")),
                           created=created)


@inv_units_bp.route("/upload-excel", methods=["POST"])
@login_required
def upload_excel():
    file = request.files.get("file")
    if not file or not file.filename:
        flash("No file selected.", "error")
        return redirect(url_for("inv_units.list_units"))
    try:
        rows = md.import_rows(md.read_sheet(file), md.UNIT_COLUMNS, md.clean_unit,
                              name_fields=("name", "abbreviation"))
    except md.Invalid as e:
        flash(str(e), "error")
        return redirect(url_for("inv_units.list_units"))
    if not rows:
        flash("The sheet has no units under its heading row.", "error")
        return redirect(url_for("inv_units.list_units"))
    # The review screen is this response — the rows no longer ride in the
    # session cookie, which the browser drops past 4 KB.
    return _review(rows)


@inv_units_bp.route("/batch", methods=["GET", "POST"])
@login_required
def batch_editor():
    if request.method == "POST":
        try:
            rows = md.submitted_rows(request.form.get("data"))
        except md.Invalid as e:
            flash(str(e), "error")
            return redirect(url_for("inv_units.list_units"))

        def create(values):
            db.session.add(InvUnit(name=values["name"], abbreviation=values["abbreviation"],
                                   explanation=values["explanation"] or None))
            db.session.flush()

        created, failed = md.save_rows(rows, md.clean_unit, create,
                                       name_fields=("name", "abbreviation"))
        db.session.commit()
        if failed:
            return _review(failed, created=created)
        flash(f"{created} unit(s) created.", "success")
        return redirect(url_for("inv_units.list_units"))
    return _review([])
