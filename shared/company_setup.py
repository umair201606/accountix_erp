"""Provision the books of a newly created company.

Kept outside app.py so the portal and super admin routes can call it
without importing the app factory. Every step is company-scoped and
idempotent, mirroring what _seed_all_data does for the default company.
"""
from shared.extensions import db
from shared.tenancy import current_company_id, set_current_company

VOUCHER_PREFIXES = ["PI", "PR", "CONS", "SCRAP", "ADJ", "ST", "CPV", "CRV",
                    "BPV", "BRV", "JV", "PRL", "FA-TRF", "INV-FA"]


def provision_company(company_id):
    """Seed the chart, settings, periods, numbering and defaults for a
    company, scoped to it. Requires an app context and an active company
    context (the caller should switch to company_id first); the previous
    active company is restored afterwards."""
    previous = current_company_id()
    set_current_company(company_id)
    try:
        from shared.coa import ensure_fixed_coa
        ensure_fixed_coa()

        from shared.models.stock_ledger import VoucherNumber
        for prefix in VOUCHER_PREFIXES:
            if not VoucherNumber.query.filter_by(prefix=prefix).first():
                db.session.add(VoucherNumber(prefix=prefix, next_number=1))

        from fixed_assets_app.models.asset import AssetCategory as FAssetCat
        FAssetCat.seed()

        from shared.models.company_settings import (AccountingPeriod,
                                                    CompanyInfo,
                                                    FiscalYearRule,
                                                    ReportSettings)
        CompanyInfo.get()
        rule = FiscalYearRule.get()
        if not AccountingPeriod.query.first():
            rule.generate_periods()
        ReportSettings.get()

        from shared.models.inventory_settings import InventorySettings
        InventorySettings.get()

        from shared.models.invoice_template import InvoiceTemplate
        InvoiceTemplate.seed_defaults()

        db.session.commit()
    finally:
        set_current_company(previous)
