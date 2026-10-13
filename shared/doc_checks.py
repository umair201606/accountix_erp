"""Range checks on the figures an invoice is approved with.

Approval used to check only that each line had a product and a positive
quantity. A negative unit price, a discount over 100% or a negative tax rate
went straight into the journal as negative revenue, a discount bigger than
the sale, or tax credited the wrong way. Sales and purchase invoices share
these checks.
"""


def _num(v):
    if v in (None, ""):
        return 0.0
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def amount_errors(data):
    """Messages for out-of-range figures in an invoice payload (empty = fine)."""
    errors = []

    def pct(label, v):
        n = _num(v)
        if n is None:
            errors.append(f"{label} must be a number")
        elif not 0 <= n <= 100:
            errors.append(f"{label} must be between 0 and 100%")

    def non_negative(label, v):
        n = _num(v)
        if n is None:
            errors.append(f"{label} must be a number")
        elif n < 0:
            errors.append(f"{label} cannot be negative")
        return n or 0.0

    for key, label in (("global_discount_pct", "Discount"),
                       ("global_sales_tax_pct", "Sales tax"),
                       ("further_tax_pct", "Further tax"),
                       ("withholding_tax_pct", "Withholding tax")):
        pct(label, data.get(key))
    for key, label in (("global_discount_value", "Discount amount"),
                       ("global_delivery", "Delivery"),
                       ("global_installation", "Installation")):
        non_negative(label, data.get(key))

    for i, row in enumerate(data.get("items") or [], 1):
        qty = _num(row.get("quantity"))
        if qty is None:
            errors.append(f"Row {i}: quantity must be a number")
            continue
        price = non_negative(f"Row {i}: unit price", row.get("unit_price"))
        pct(f"Row {i}: discount", row.get("discount_pct"))
        pct(f"Row {i}: sales tax", row.get("sales_tax_pct"))
        disc = non_negative(f"Row {i}: discount amount", row.get("discount_amount"))
        non_negative(f"Row {i}: delivery", row.get("delivery"))
        non_negative(f"Row {i}: installation", row.get("installation"))
        if qty and price is not None and disc > qty * price + 0.005:
            errors.append(f"Row {i}: the discount is more than the line's value")
    for i, chg in enumerate(data.get("charges") or [], 1):
        n = _num(chg.get("amount"))
        if n is None or n < 0:
            errors.append(f"Charge {i}: amount must be zero or more")
    return errors
