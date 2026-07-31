"""Money formatting, shared by the screen and both exports.

One figure, one rendering. These lived as three separate implementations —
a closure in app.py for the screen, an Excel format string, and a hardcoded
f-string in the PDF builder — which is how the same amount came to print three
different ways.
"""

import pytest

from shared.formatting import (INDIAN, MONEY_FMT_WESTERN, WESTERN,
                               excel_money_format, format_amount)


@pytest.mark.parametrize("value, expected", [
    (1234567.891, "1,234,567.89"),
    (0, "0.00"),
    (0.0, "0.00"),
    (None, "0.00"),
    (-1234.5, "(1,234.50)"),
    (-0.004, "(0.00)"),
])
def test_western_amounts(value, expected):
    assert format_amount(value, fmt=WESTERN) == expected


@pytest.mark.parametrize("value, expected", [
    (1234567.89, "12,34,567.89"),
    (100000, "1,00,000.00"),
    (999, "999.00"),
    (-1234567.89, "(12,34,567.89)"),
])
def test_indian_amounts(value, expected):
    assert format_amount(value, fmt=INDIAN) == expected


def test_negatives_use_round_brackets_not_a_minus():
    """The accounting presentation, and the same one on every surface."""
    assert format_amount(-100000, fmt=WESTERN) == "(100,000.00)"
    assert "-" not in format_amount(-100000, fmt=WESTERN)


def test_brackets_can_be_turned_off_for_a_bare_signed_number():
    assert format_amount(-1234.5, fmt=WESTERN, brackets=False) == "-1,234.50"


def test_indian_decimals_come_from_the_rounded_string():
    """(rounded - int_part) * 100 lands on 49.999... for ordinary values and
    truncated to the wrong cent."""
    assert format_amount(1234.50, fmt=INDIAN) == "1,234.50"
    assert format_amount(7.07, fmt=INDIAN) == "7.07"
    assert format_amount(1.999, fmt=INDIAN) == "2.00"


def test_decimal_places_are_respected():
    assert format_amount(1234.567, decimal_places=0, fmt=WESTERN) == "1,235"
    assert format_amount(-1234.567, decimal_places=1, fmt=WESTERN) == "(1,234.6)"


def test_a_non_numeric_value_is_passed_through():
    assert format_amount("n/a", fmt=WESTERN) == "n/a"


def test_excel_format_brackets_negatives():
    assert excel_money_format(WESTERN) == MONEY_FMT_WESTERN
    assert "(" in excel_money_format(WESTERN)


def test_excel_format_switches_grouping_with_the_company_setting():
    """Excel has no native Indian grouping; the multi-clause pattern
    approximates it by switching format at each magnitude."""
    assert excel_money_format(INDIAN) != excel_money_format(WESTERN)
    assert "[>=100000]" in excel_money_format(INDIAN)
