"""Checks shared by the master-data forms and their Excel imports — customers,
suppliers, products and units.

Each of the four had its own copy of the same parsing, and each copy trusted
something different: the create forms checked nothing (a blank name or a
duplicate SKU reached the database, the second as a 500), the imports kept
the parsed sheet in the session cookie (a browser drops a cookie over 4 KB,
so about 40 customers came back as an empty review screen), and a batch row
holding a number where text was expected crashed on ``.strip()``. The rules
live here once, so a form, an upload and a batch save refuse the same things
in the same words.
"""
from decimal import Decimal, InvalidOperation

from sqlalchemy import func

# Warnings listed in a flash message (the message travels in the session
# cookie too, so a long list would break it the same way the rows did).
MAX_LISTED = 10


class Invalid(ValueError):
    """A value a master-data record cannot take; the message is for people."""


def text(value):
    """Any cell / form / JSON value as trimmed text ('' for None).

    Excel gives numbers for numeric-looking cells (a phone typed as a number,
    a tax id): 3001234567.0 is shown as 3001234567, not with a trailing .0.
    """
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value).strip()


def single_line(value):
    """``text`` with runs of whitespace collapsed — names and codes."""
    return " ".join(text(value).split())


def key(value):
    """Comparison form of a name: case and spacing do not make it new."""
    return single_line(value).casefold()


# Larger than any amount the NUMERIC(20,6) columns hold sensibly; "1e400"
# would otherwise become float infinity and fail at the database.
MAX_NUMBER = Decimal("1000000000000")


def number(value, label, *, minimum=Decimal("0"), whole=False, default=Decimal("0")):
    """A non-negative amount or quantity from a form, cell or JSON value.

    Accepts thousands separators ("1,250.50"). Blank gives ``default``.
    Raises Invalid with a readable message instead of silently using 0 —
    an opening stock of "2.5" used to become 0 in the batch save.
    """
    raw = text(value).replace(",", "")
    if raw == "":
        return default
    try:
        n = Decimal(raw)
    except InvalidOperation:
        raise Invalid(f"{label} must be a number (got '{text(value)[:20]}').")
    if not n.is_finite():
        raise Invalid(f"{label} must be a number.")
    if abs(n) > MAX_NUMBER:
        raise Invalid(f"{label} is too large.")
    if minimum is not None and n < minimum:
        raise Invalid(f"{label} cannot be negative." if minimum == 0
                      else f"{label} must be at least {minimum}.")
    if whole and n != n.to_integral_value():
        raise Invalid(f"{label} must be a whole number.")
    return n


def check_length(model, values, labels):
    """Refuse text longer than its column (Postgres raises on it; SQLite
    stores it silently, so local testing never showed the 500)."""
    out = []
    for field, label in labels.items():
        col = model.__table__.c.get(field)
        limit = getattr(getattr(col, "type", None), "length", None)
        value = values.get(field)
        if limit and isinstance(value, str) and len(value) > limit:
            out.append(f"{label} is longer than {limit} characters.")
    return out


def taken(model, column, value, exclude_id=None):
    """Whether another row of this company already uses ``value`` in
    ``column``, ignoring case and surrounding spaces."""
    q = model.query.filter(func.lower(func.trim(column)) == single_line(value).lower())
    if exclude_id:
        q = q.filter(model.id != exclude_id)
    return q.first() is not None


def summarise(messages, limit=MAX_LISTED):
    """First ``limit`` messages joined, then "and N more"."""
    messages = list(messages)
    shown = "; ".join(messages[:limit])
    if len(messages) > limit:
        shown += f"; and {len(messages) - limit} more"
    return shown


def read_sheet(file_storage):
    """Rows (as value tuples, header skipped) of an uploaded workbook's first
    sheet, with their spreadsheet row numbers. Raises Invalid for a file that
    is not a readable .xlsx."""
    from io import BytesIO
    import openpyxl
    try:
        wb = openpyxl.load_workbook(BytesIO(file_storage.read()), data_only=True,
                                    read_only=True)
    except Exception:
        raise Invalid("That file could not be read as an Excel workbook (.xlsx). "
                      "Download the template and fill it in.")
    ws = wb.active
    if ws is None:
        raise Invalid("The workbook has no sheet to read.")
    return [(i, row) for i, row in enumerate(ws.iter_rows(min_row=2, values_only=True),
                                             start=2)
            if any(text(v) for v in (row or ()))]


def cell(row, index):
    return row[index] if row is not None and len(row) > index else None


# ── Customers and suppliers ─────────────────────────────────────────────────

PARTY_TEXT = ("name", "contact_person", "email", "phone", "mobile", "address",
              "city", "tax_id", "payment_terms", "website", "notes")
PARTY_LABELS = {"name": "Name", "contact_person": "Contact person",
                "email": "Email", "phone": "Phone", "mobile": "Mobile",
                "city": "City", "tax_id": "Tax ID", "payment_terms": "Payment terms",
                "website": "Website"}


def clean_party(model, raw, *, credit_limit=False, exclude_id=None):
    """(values, errors) for a customer or supplier from any source.

    ``raw`` is a dict of field -> value (form, sheet or JSON). Names are
    unique per company ignoring case and spacing — the ledger account is
    named after the party, and two "Ali Traders" accounts cannot be told
    apart in a trial balance.
    """
    values = {f: text(raw.get(f)) for f in PARTY_TEXT}
    values["name"] = single_line(values["name"])
    errors = []
    if not values["name"]:
        errors.append("Name is required.")
    elif taken(model, model.name, values["name"], exclude_id):
        errors.append(f"'{values['name']}' already exists.")
    email = values["email"]
    if email and ("@" not in email or "." not in email.rsplit("@", 1)[-1]):
        errors.append(f"Email '{email[:40]}' does not look like an email address.")
    if credit_limit:
        try:
            values["credit_limit"] = float(number(raw.get("credit_limit"), "Credit limit"))
        except Invalid as e:
            errors.append(str(e))
            values["credit_limit"] = text(raw.get("credit_limit"))
    errors += check_length(model, values, PARTY_LABELS)
    return values, errors


CUSTOMER_COLUMNS = ("name", "contact_person", "email", "phone", "mobile", "city",
                    "tax_id", "payment_terms", "credit_limit", "website",
                    "address", "notes")
SUPPLIER_COLUMNS = ("name", "contact_person", "email", "phone", "mobile", "city",
                    "tax_id", "payment_terms", "website", "address", "notes")

# A sheet longer than this is refused rather than half-read: the review page
# posts every row back in one request.
MAX_IMPORT_ROWS = 2000


def import_rows(sheet_rows, columns, clean, name_fields=("name",)):
    """Sheet rows -> review rows: each a dict of cleaned values plus
    ``row_line`` (its spreadsheet row) and ``row_errors`` (why it cannot be
    saved yet). Every row is kept — a row with a problem is shown, flagged,
    for the user to fix on the review screen, instead of being dropped with
    a warning they had to copy down. A row repeating an earlier name
    (ignoring case and spacing) is flagged too."""
    if len(sheet_rows) > MAX_IMPORT_ROWS:
        raise Invalid(f"The sheet has {len(sheet_rows):,} rows; import at most "
                      f"{MAX_IMPORT_ROWS:,} at a time.")
    seen = {f: set() for f in name_fields}
    out = []
    for line, row in sheet_rows:
        raw = {f: cell(row, i) for i, f in enumerate(columns)}
        values, errors = clean(raw)
        errors += _repeats(values, seen, name_fields, "in this sheet")
        values["row_line"] = line
        values["row_errors"] = errors
        out.append(values)
    return out


def _repeats(values, seen, fields, where):
    out = []
    for f in fields:
        k = key(values.get(f))
        if not k:
            continue
        if k in seen[f]:
            out.append(f"'{single_line(values.get(f))}' appears more than once {where}.")
        seen[f].add(k)
    return out


def submitted_rows(raw_json):
    """The review screen's rows as a list of dicts. Raises Invalid for
    anything that is not a JSON list; entries that are not objects, are
    marked deleted or are entirely blank are dropped."""
    import json
    try:
        data = json.loads(raw_json or "[]")
    except (TypeError, ValueError):
        raise Invalid("The rows sent were not readable — reload the import and try again.")
    if not isinstance(data, list):
        raise Invalid("The rows sent were not readable — reload the import and try again.")
    if len(data) > MAX_IMPORT_ROWS:
        raise Invalid(f"At most {MAX_IMPORT_ROWS:,} rows can be saved at a time.")
    return [r for r in data if isinstance(r, dict) and not r.get("_delete")
            and any(text(v) for k, v in r.items() if not str(k).startswith("_"))]


def save_rows(rows, clean, create, name_fields=("name",)):
    """Validate every submitted row again (the screen could be stale, or two
    people could be importing at once) and create the good ones. Returns
    (created_count, failed_rows) — failed rows carry ``row_errors`` and go
    back on the review screen, so nothing typed into them is lost."""
    seen = {f: set() for f in name_fields}
    created, failed = 0, []
    for raw in rows:
        values, errors = clean(raw)
        errors += _repeats(values, seen, name_fields, "in these rows")
        if errors:
            values["row_errors"] = errors
            failed.append(values)
            continue
        create(values)
        created += 1
    return created, failed


# ── Products ────────────────────────────────────────────────────────────────

PRODUCT_COLUMNS = ("sku", "name", "description", "category_name", "unit_price",
                   "cost_price", "reorder_level", "current_stock", "unit", "hs_code")
PRODUCT_LABELS = {"sku": "SKU", "name": "Name", "unit": "Unit", "hs_code": "HS code"}


def unit_choices(current=None):
    """Units a product may use, as casefolded name/abbreviation ->
    abbreviation: the company's active units, plus the product's own unit
    even if it was later switched off (editing the product must not silently
    move it to the first unit in the list)."""
    from inventory_app.models.unit import InvUnit
    units = InvUnit.query.filter_by(is_active=True).order_by(InvUnit.name).all()
    choices = {u.name.casefold(): u.abbreviation for u in units}
    choices.update({u.abbreviation.casefold(): u.abbreviation for u in units})
    if current:
        choices.setdefault(current.casefold(), current)
    return choices


def clean_product(raw, *, choices, categories=None, exclude_id=None,
                  opening=True, upper_sku=False):
    """(values, errors) for a product from the form, a sheet row or a batch
    row. ``choices`` from ``unit_choices``; ``categories`` maps a category
    name key -> id for imports (the form sends ``category_id`` instead)."""
    from inventory_app.models.product import InvProduct
    from inventory_app.models.category import InvCategory
    from shared.tenancy import scoped_get
    sku = single_line(raw.get("sku"))
    values = {"sku": sku.upper() if upper_sku else sku,
              "name": single_line(raw.get("name")),
              "description": text(raw.get("description")),
              "hs_code": single_line(raw.get("hs_code")),
              "category_name": single_line(raw.get("category_name")),
              "category_id": None}
    errors = []
    if not values["sku"]:
        errors.append("SKU is required.")
    elif taken(InvProduct, InvProduct.sku, values["sku"], exclude_id):
        errors.append(f"SKU '{values['sku']}' is already used by another product.")
    if not values["name"]:
        errors.append("Name is required.")
    elif taken(InvProduct, InvProduct.name, values["name"], exclude_id):
        errors.append(f"A product named '{values['name']}' already exists.")
    for field, label, whole in (("unit_price", "Sale price", False),
                                ("cost_price", "Cost price", False),
                                ("reorder_level", "Reorder level", True),
                                ("weight", "Weight", False)):
        try:
            n = number(raw.get(field), label, whole=whole)
            values[field] = int(n) if whole else float(n)
        except Invalid as e:
            errors.append(str(e))
            values[field] = text(raw.get(field))
    if opening:
        try:
            qty = number(raw.get("current_stock"), "Opening stock")
            values["current_stock"] = qty
            if qty > 0 and isinstance(values["cost_price"], float) \
                    and values["cost_price"] <= 0:
                errors.append("Opening stock needs a cost price — stock brought on at "
                              "zero cost would later be sold with no cost of sales.")
        except Invalid as e:
            errors.append(str(e))
            values["current_stock"] = text(raw.get("current_stock"))
    unit = single_line(raw.get("unit"))
    if not choices:
        values["unit"] = unit or "pcs"
    elif not unit:
        values["unit"] = choices.get("pcs", "")
        if not values["unit"]:
            errors.append("Unit is required.")
    elif unit.casefold() in choices:
        values["unit"] = choices[unit.casefold()]
    else:
        values["unit"] = unit
        errors.append(f"Unit '{unit}' is not set up — add it under Units, or pick one.")
    if raw.get("category_id") not in (None, ""):
        try:
            cat = scoped_get(InvCategory, int(raw.get("category_id")))
        except (TypeError, ValueError):
            cat = None
        if cat is None:
            errors.append("That category does not exist.")
        else:
            values["category_id"] = cat.id
    elif values["category_name"]:
        cat_id = (categories or {}).get(key(values["category_name"]))
        if cat_id is None:
            errors.append(f"Category '{values['category_name']}' not found — "
                          f"add it under Categories first.")
        values["category_id"] = cat_id
    errors += check_length(InvProduct, values, PRODUCT_LABELS)
    return values, errors


def category_index():
    from inventory_app.models.category import InvCategory
    return {key(c.name): c.id for c in InvCategory.query.filter_by(is_active=True)}


def opening_date_refusal(when):
    """Why opening stock cannot be dated ``when`` (a closed period), or None."""
    from shared.periods import ClosedPeriodError, require_open_period
    try:
        require_open_period(when, action="bring opening stock on")
    except ClosedPeriodError as e:
        return str(e)
    return None


# ── Units ───────────────────────────────────────────────────────────────────

UNIT_COLUMNS = ("name", "abbreviation", "explanation")


def clean_unit(raw, exclude_id=None):
    """(values, errors) for a unit of measure. Name and abbreviation are each
    unique ignoring case: products record their unit by abbreviation, and
    "KG" next to "kg" would make every import ambiguous."""
    from inventory_app.models.unit import InvUnit
    values = {"name": single_line(raw.get("name")),
              "abbreviation": single_line(raw.get("abbreviation")),
              "explanation": text(raw.get("explanation"))}
    errors = []
    if not values["name"]:
        errors.append("Name is required.")
    elif taken(InvUnit, InvUnit.name, values["name"], exclude_id):
        errors.append(f"Unit '{values['name']}' already exists.")
    if not values["abbreviation"]:
        errors.append("Abbreviation is required.")
    elif taken(InvUnit, InvUnit.abbreviation, values["abbreviation"], exclude_id):
        errors.append(f"Abbreviation '{values['abbreviation']}' already exists.")
    errors += check_length(InvUnit, values, {"name": "Name", "abbreviation": "Abbreviation"})
    return values, errors
