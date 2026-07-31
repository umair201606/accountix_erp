"""Money formatting, shared by the screen and both export formats.

These lived inside ``_create_app()`` as closures, so the PDF builder could not
reach them and hardcoded ``f"{v:,.2f}"`` instead. The result was three
conventions for the same figure: the screen honoured the company's number
format and wrote a minus, Excel wrote accounting brackets, and the PDF wrote a
minus in western grouping whatever the company was set to. One function now
decides, and all three call it.

Negatives are drawn in round brackets — (1,234.50) — the accounting
presentation, on every surface.
"""

WESTERN, INDIAN = "en", "hi"


def company_number_format(default=WESTERN):
    """The active company's number format, or the default outside a request."""
    try:
        from shared.models.company_settings import CompanyInfo
        info = CompanyInfo.get()
        return (info.number_format if info else None) or default
    except Exception:
        # No app context, no table yet (first boot), no company row: the
        # formatter must still work — it is used by unit tests and by seeds.
        return default


def _grouped_indian(int_str):
    """12,34,567 — last three digits, then groups of two."""
    if len(int_str) <= 3:
        return int_str
    head, tail = int_str[:-3], int_str[-3:]
    groups = []
    while head:
        groups.append(head[-2:])
        head = head[:-2]
    return ",".join(reversed(groups)) + "," + tail


def format_amount(value, decimal_places=2, fmt=None, brackets=True):
    """A money figure as every surface should print it.

    ``brackets`` renders negatives as (1,234.50); pass False for the few places
    that need a bare signed number (CSV, a chart axis).
    """
    if value is None:
        value = 0
    try:
        value = float(value)
    except (TypeError, ValueError):
        return str(value)

    if decimal_places < 0:
        decimal_places = 0
    if fmt is None:
        fmt = company_number_format()

    negative = value < 0
    magnitude = abs(value)

    if fmt == INDIAN:
        rounded = round(magnitude, decimal_places)
        int_part = int(rounded)
        text = _grouped_indian(str(int_part))
        if decimal_places > 0:
            # Derive the decimals from the rounded string rather than
            # arithmetic: (rounded - int_part) * 100 lands on 49.999... for
            # plenty of ordinary values and truncates to the wrong cent.
            frac = f"{rounded:.{decimal_places}f}".split(".")[1]
            text = f"{text}.{frac}"
    else:
        text = f"{magnitude:,.{decimal_places}f}"

    if not negative:
        return text
    return f"({text})" if brackets else f"-{text}"


# Excel writes real numbers and lets the workbook format them, so the bracket
# convention has to be expressed as a format string rather than applied to the
# value. Indian grouping has no native Excel format; the multi-clause form
# below approximates it by switching pattern at each magnitude.
MONEY_FMT_WESTERN = "#,##0.00;(#,##0.00)"
MONEY_FMT_INDIAN = (
    "[>=10000000]##\\,##\\,##\\,##0.00;"
    "[>=100000]##\\,##\\,##0.00;"
    "#,##0.00"
)


def excel_money_format(fmt=None):
    """The workbook number format matching the active company's convention."""
    if fmt is None:
        fmt = company_number_format()
    return MONEY_FMT_INDIAN if fmt == INDIAN else MONEY_FMT_WESTERN
