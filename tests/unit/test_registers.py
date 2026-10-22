"""Bulk-register date filter: month / financial year / custom range.

Uses the shared unit `company` fixture (app context + active company), the
same way the costing tests do.
"""
import sys
from datetime import date
from pathlib import Path

ACCOUNTIX_ERP = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ACCOUNTIX_ERP))

from shared.registers import resolve_register_filter  # noqa: E402


class _Args(dict):
    pass


def test_month_mode_defaults_to_current_month(company):
    fd, td, ctx = resolve_register_filter(_Args())
    today = date.today()
    assert (fd.year, fd.month, fd.day) == (today.year, today.month, 1)
    assert (td.year, td.month) == (today.year, today.month)
    assert ctx["mode"] == "month"
    assert str(today.year) in ctx["label"]


def test_month_mode_parses_explicit_month(company):
    fd, td, ctx = resolve_register_filter(
        _Args({"mode": "month", "month": "2026-02"}))
    assert (fd.year, fd.month, fd.day) == (2026, 2, 1)
    assert (td.year, td.month, td.day) == (2026, 2, 28)
    assert ctx["label"] == "February 2026"


def test_garbage_month_falls_back(company):
    fd, _td, ctx = resolve_register_filter(
        _Args({"mode": "month", "month": "not-a-month"}))
    assert fd.day == 1 and ctx["mode"] == "month"


def test_custom_range_parses_both_sides(company):
    fd, td, ctx = resolve_register_filter(
        _Args({"mode": "custom", "from": "2026-01-05", "to": "2026-01-20"}))
    assert fd.isoformat() == "2026-01-05"
    assert td.isoformat() == "2026-01-20"
    assert "Jan 2026" in ctx["label"]


def test_custom_range_open_ended(company):
    fd, td, _ctx = resolve_register_filter(
        _Args({"mode": "custom", "from": "2026-03-01"}))
    assert fd.isoformat() == "2026-03-01" and td is None
    fd, td, ctx = resolve_register_filter(_Args({"mode": "custom"}))
    assert fd is None and td is None
    assert ctx["label"] == "All dates"


def test_unknown_mode_falls_back(company):
    _fd, _td, ctx = resolve_register_filter(_Args({"mode": "nope"}))
    assert ctx["mode"] == "month"


def test_year_mode_without_periods_means_all_dates(company):
    _fd, _td, ctx = resolve_register_filter(_Args({"mode": "year"}))
    assert ctx["mode"] == "year"
    assert ctx["label"] == "All dates"


def test_unapproved_filter_includes_null_status(company):
    """The row renderer shows a NULL status as Unapproved, so the filter
    must too — a bare ``!= 'approved'`` drops NULLs in SQL."""
    from shared.extensions import db
    from inventory_app.models.customer import InvCustomer
    from inventory_app.models.supplier import InvSupplier
    from inventory_app.models.invoice import InvInvoice
    from inventory_app.models.purchase_invoice import InvPurchaseInvoice
    from invoicing_app.routes.registers import _purchase_query, _sales_query
    db.create_all()
    cust = InvCustomer(name="C")
    supp = InvSupplier(name="S")
    db.session.add_all([cust, supp])
    db.session.flush()
    for n, st in enumerate(("approved", "unapproved", None)):
        db.session.add(InvInvoice(invoice_number=f"SI{n}", voucher_number=f"SV{n}",
                                  customer_id=cust.id, voucher_status=st))
        db.session.add(InvPurchaseInvoice(invoice_number=f"PI{n}",
                                          voucher_number=f"PV{n}",
                                          supplier_id=supp.id,
                                          status=st if st else None))
    db.session.flush()
    # default="..." only fills omitted columns; force the NULLs explicitly.
    InvInvoice.query.filter_by(invoice_number="SI2").update({"voucher_status": None})
    InvPurchaseInvoice.query.filter_by(invoice_number="PI2").update({"status": None})
    db.session.commit()

    assert {i.invoice_number for i in _sales_query("unapproved")} == {"SI1", "SI2"}
    assert {i.invoice_number for i in _sales_query("approved")} == {"SI0"}
    assert {i.invoice_number for i in _purchase_query("unapproved")} == {"PI1", "PI2"}
    assert _sales_query("").count() == 3


def test_full_invoices_page_in_date_then_id_order(company):
    """The bulk preview reads 20 first, then 100 at a time; the pages must
    join without gaps or repeats and the count must survive between them."""
    from shared.extensions import db
    from inventory_app.models.customer import InvCustomer
    from inventory_app.models.invoice import InvInvoice
    from invoicing_app.routes.registers import _full_invoices
    db.create_all()
    cust = InvCustomer(name="Chunk Co")
    db.session.add(cust)
    db.session.flush()
    for n in range(130):
        db.session.add(InvInvoice(
            invoice_number=f"CH{n:03d}", voucher_number=f"CV{n:03d}",
            customer_id=cust.id, invoice_date=date(2026, 6, 15),
            total_amount=n))
    db.session.commit()

    total, first = _full_invoices(
        "sales", "", date(2026, 6, 1), date(2026, 6, 30), limit=20, offset=0)
    assert total == 130 and len(first) == 20

    # The page already knows the count and passes it back for later chunks.
    total2, middle = _full_invoices(
        "sales", "", date(2026, 6, 1), date(2026, 6, 30), limit=100,
        offset=20, total=total)
    assert total2 == 130 and len(middle) == 100

    _total3, last = _full_invoices(
        "sales", "", date(2026, 6, 1), date(2026, 6, 30), limit=100,
        offset=120, total=total)
    assert len(last) == 10
    page_ids = [i.id for i in first + middle + last]
    assert page_ids == sorted(page_ids)
    assert len(set(page_ids)) == 130
    assert [i.invoice_number for i in middle[:2]] == ["CH020", "CH021"]
