import json
from datetime import datetime, date
from shared.extensions import db


class CompanyInfo(db.Model):
    __tablename__ = "company_info"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)
    company_name = db.Column(db.String(200), default="My Company")
    address = db.Column(db.Text)
    city = db.Column(db.String(100))
    state = db.Column(db.String(100))
    country = db.Column(db.String(100), default="Pakistan")
    phone = db.Column(db.String(50))
    email = db.Column(db.String(200))
    website = db.Column(db.String(200))
    tax_id = db.Column(db.String(100))
    registration_number = db.Column(db.String(100))
    logo_url = db.Column(db.String(500))
    # Where a customer is meant to send the money. Printed opposite the totals
    # on a sales invoice, which is the one document that has to tell someone
    # how to pay it.
    bank_name = db.Column(db.String(200))
    bank_account_title = db.Column(db.String(200))
    bank_account_number = db.Column(db.String(100))
    fiscal_year_start_month = db.Column(db.Integer, default=1)
    currency = db.Column(db.String(10), default="PKR")
    currency_symbol = db.Column(db.String(10), default="Rs.")
    date_format = db.Column(db.String(20), default="Y-m-d")
    number_format = db.Column(db.String(10), default="en")
    timezone = db.Column(db.String(50), default="Asia/Karachi")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    @classmethod
    def get(cls):
        from shared.tenancy import current_company_id
        cid = current_company_id()
        if cid is None:
            # No active company (login page, boot): return an unsaved default
            # rather than querying a tenant-scoped table (which fails closed).
            return cls()
        c = cls.query.filter_by(company_id=cid).first()
        if not c:
            c = cls(company_id=cid, company_name="SolarKon Energy Solutions")
            db.session.add(c)
            db.session.commit()
        return c


# Default P&L layout: ordered rows, each either an account section (grouped by
# ChartOfAccount.effective_pl_section()) or a subtotal line whose label the
# user can rename ("Gross Profit" -> "Gross Margin", ...). Stored as JSON so
# the settings UI can reorder/rename without schema changes.
DEFAULT_PL_STRUCTURE = [
    {"section": "sales", "label": "Sales"},
    {"section": "sales_returns", "label": "Less: Sales Returns & Discounts", "negate": True},
    {"subtotal": "net_sales", "label": "Net Sales"},
    {"section": "cost_of_sales", "label": "Cost of Sales", "negate": True},
    {"subtotal": "gross_profit", "label": "Gross Profit"},
    {"section": "admin", "label": "Administrative Expenses", "negate": True},
    {"section": "selling_distribution", "label": "Selling & Distribution Expenses", "negate": True},
    {"section": "other_operating", "label": "Other Operating Expenses", "negate": True},
    {"subtotal": "operating_profit", "label": "Operating Profit"},
    {"section": "other_income", "label": "Other Income"},
    {"section": "finance_cost", "label": "Finance Costs", "negate": True},
    {"subtotal": "profit_before_tax", "label": "Profit Before Tax"},
    {"section": "income_tax", "label": "Taxation", "negate": True},
    {"subtotal": "net_profit", "label": "Net Profit"},
]

PL_SECTIONS = ["sales", "sales_returns", "other_income", "cost_of_sales",
               "admin", "selling_distribution", "other_operating",
               "finance_cost", "income_tax"]


class ReportSettings(db.Model):
    __tablename__ = "report_settings"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)
    pl_structure_json = db.Column(db.Text)  # JSON list; null -> DEFAULT_PL_STRUCTURE
    # Accounts listed per P&L section before collapsing the rest into "Others".
    pl_detail_rows = db.Column(db.Integer, default=10)
    # Deprecated: superseded by purchase_party_mode / sales_party_mode, which
    # are set independently. Retained so existing rows migrate cleanly.
    invoice_party_mode = db.Column(db.String(10), default="relevant")
    # Invoice party picker, per document type: "relevant" = suppliers (or
    # customers) only; "all" = any postable ledger account may be the
    # counterparty, via a Post To Ledger Account override on the form.
    purchase_party_mode = db.Column(db.String(10), default="relevant")
    sales_party_mode = db.Column(db.String(10), default="relevant")
    purchase_template_text = db.Column(db.Text)
    sales_template_text = db.Column(db.Text)
    purchase_template_id = db.Column(db.Integer, db.ForeignKey("invoice_templates.id"))
    sales_template_id = db.Column(db.Integer, db.ForeignKey("invoice_templates.id"))
    cash_flow_method = db.Column(db.String(20), default="indirect")  # indirect or direct
    # 13-week cash flow: minimum cash floor; NULL hides the headroom line.
    twcf_cash_floor = db.Column(db.Numeric(16, 4))
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    @classmethod
    def get(cls):
        from shared.tenancy import current_company_id
        cid = current_company_id()
        if cid is None:
            # No active company (login page, boot): return an unsaved default
            # rather than querying a tenant-scoped table (which fails closed).
            return cls()
        s = cls.query.filter_by(company_id=cid).first()
        if not s:
            s = cls(company_id=cid)
            db.session.add(s)
            db.session.commit()
        return s

    def party_mode(self, doc):
        """Party-picker mode for "purchase" or "sales" documents.

        Falls back to the deprecated single flag so databases written before
        the split keep the behaviour their admin chose.
        """
        value = self.purchase_party_mode if doc == "purchase" else self.sales_party_mode
        return value or self.invoice_party_mode or "relevant"

    def template_text(self, doc):
        """Custom invoice body template for "purchase" or "sales" documents."""
        return self.purchase_template_text if doc == "purchase" else self.sales_template_text

    def pl_structure(self):
        if self.pl_structure_json:
            try:
                rows = json.loads(self.pl_structure_json)
                if isinstance(rows, list) and rows:
                    return rows
            except (ValueError, TypeError):
                pass
        return DEFAULT_PL_STRUCTURE

    def set_pl_structure(self, rows):
        self.pl_structure_json = json.dumps(rows) if rows else None


class FiscalYearRule(db.Model):
    __tablename__ = "fiscal_year_rule"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)
    start_month = db.Column(db.Integer, nullable=False, default=1)
    start_day = db.Column(db.Integer, nullable=False, default=1)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    @classmethod
    def get(cls):
        from shared.tenancy import current_company_id
        cid = current_company_id()
        if cid is None:
            # No active company (login page, boot): return an unsaved default
            # rather than querying a tenant-scoped table (which fails closed).
            return cls()
        r = cls.query.filter_by(company_id=cid).first()
        if not r:
            r = cls(company_id=cid, start_month=1, start_day=1)
            db.session.add(r)
            db.session.commit()
        return r

    def generate_periods(self, from_year=None, to_year=None):
        """Delete all existing periods and regenerate yearly periods from this rule.

        If from_year/to_year are None, auto-detect from existing journal entries.
        """
        from shared.models.ledger import JournalEntry

        AccountingPeriod.query.delete()
        today = date.today()

        if from_year is not None and to_year is not None:
            min_year, max_year = from_year, to_year
        else:
            earliest = db.session.query(db.func.min(JournalEntry.entry_date)).scalar()
            min_year = earliest.year if earliest else today.year - 5
            max_year = today.year + 2

        for y in range(min_year, max_year + 1):
            start_date = date(y, self.start_month, self.start_day)
            end_date = date(y + 1, self.start_month, self.start_day) - __import__("datetime").timedelta(days=1)

            if self.start_month == 1 and self.start_day == 1:
                fiscal_year = str(y)
                period_name = f"FY {y}"
            else:
                fiscal_year = f"{y}-{y + 1}"
                period_name = f"FY {y}-{y + 1}"

            db.session.add(AccountingPeriod(
                fiscal_year=fiscal_year,
                period_name=period_name,
                start_date=start_date,
                end_date=end_date,
                is_open=True,
            ))
        db.session.commit()


class AccountingPeriod(db.Model):
    __tablename__ = "accounting_periods"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)
    fiscal_year = db.Column(db.String(10), nullable=False, index=True)
    period_name = db.Column(db.String(50), nullable=False)
    start_date = db.Column(db.Date, nullable=False)
    end_date = db.Column(db.Date, nullable=False)
    is_open = db.Column(db.Boolean, default=True)
    is_closed = db.Column(db.Boolean, default=False)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    closed_at = db.Column(db.DateTime)
