"""Fixed five-level chart of accounts: seed + in-place legacy migration.

Code scheme (segmented / compound numbering):

    Level 1  1                  account class (Assets .. Expenses)
    Level 2  1-01               sub-group (Current Assets, ...)
    Level 3  1-01-01            parent head (Cash & Cash Equivalents, ...)
    Level 4  1-01-01-01         child account (Cash in Hand, ...)
    Level 5  1-01-01-01-0001    operational account — the ONLY posting level

Levels 1-4 are fixed aggregating accounts. Level-5 segments 0001-0099 are
reserved for seeded defaults; per-entity subledger accounts (customers,
suppliers, products, employees, loans) use ``entity_id + 100``.

Migration is IN PLACE: journal lines reference accounts by row id, so legacy
accounts are re-coded/re-parented onto the new tree without touching a single
journal line. Trial-balance totals are invariant across the migration.
"""

import re

from shared.extensions import db
from shared.models.ledger import ChartOfAccount, JournalLine

SEGMENTED_RE = re.compile(r"^\d(-\d{2}){0,3}(-\d{4,})?$")

# (code, name, type, cash_flow_activity, pl_section)
# Level and parent are derived from the code's segments. Tags inherit downward.
FIXED_ACCOUNTS = [
    # ── 1 ASSETS ─────────────────────────────────────────────────────────
    ("1",               "Assets",                        "asset",     None,        None),
    ("1-01",            "Current Assets",                "asset",     "operating", None),
    ("1-01-01",         "Cash & Cash Equivalents",       "asset",     "cash",      None),
    ("1-01-01-01",      "Cash in Hand",                  "asset",     None,        None),
    ("1-01-01-01-0001", "Main Cash",                     "asset",     None,        None),
    ("1-01-01-02",      "Bank Accounts",                 "asset",     None,        None),
    ("1-01-02",         "Trade Receivables",             "asset",     None,        None),
    ("1-01-02-01",      "Trade Debtors",                 "asset",     None,        None),
    ("1-01-02-01-0001", "Trade Debtors — General",       "asset",     None,        None),
    ("1-01-02-02",      "Doubtful Receivables",          "asset",     None,        None),
    ("1-01-03",         "Other Receivables",             "asset",     None,        None),
    ("1-01-03-01",      "Employee Loans & Advances",     "asset",     None,        None),
    ("1-01-03-02",      "Advances to Suppliers",         "asset",     None,        None),
    ("1-01-03-02-0001", "Advances to Suppliers — General", "asset",   None,        None),
    ("1-01-04",         "Inventories",                   "asset",     None,        None),
    ("1-01-04-01",      "Trading Goods Stock",           "asset",     None,        None),
    ("1-01-04-01-0001", "Stock — General",               "asset",     None,        None),
    ("1-01-05",         "Tax Assets",                    "asset",     None,        None),
    ("1-01-05-01",      "Input Sales Tax",               "asset",     None,        None),
    ("1-01-05-01-0001", "Input Sales Tax",               "asset",     None,        None),
    ("1-01-05-02",      "WHT Receivable",                "asset",     None,        None),
    ("1-01-05-02-0001", "WHT Receivable",                "asset",     None,        None),
    ("1-01-06",         "Prepayments & Other",           "asset",     None,        None),
    ("1-01-06-01",      "Suspense & Clearing",           "asset",     None,        None),
    ("1-01-06-01-0001", "Suspense Account",              "asset",     None,        None),
    ("1-01-06-02",      "Prepaid Expenses",              "asset",     None,        None),
    ("1-01-06-02-0001", "Prepaid Expenses — General",    "asset",     None,        None),
    ("1-01-06-03",      "Other Current Assets",          "asset",     None,        None),
    ("1-02",            "Non-Current Assets",            "asset",     "investing", None),
    ("1-02-01",         "Property, Plant & Equipment",   "asset",     None,        None),
    ("1-02-01-01",      "Owned Assets",                  "asset",     None,        None),
    ("1-02-01-01-0001", "Fixed Assets — General",        "asset",     None,        None),
    ("1-02-02",         "Accumulated Depreciation",      "asset",     None,        None),
    ("1-02-02-01",      "Accumulated Depreciation",      "asset",     None,        None),
    ("1-02-02-01-0001", "Accumulated Depreciation — General", "asset", None,       None),
    ("1-02-03",         "Intangible Assets",             "asset",     None,        None),
    ("1-02-03-01",      "Intangibles",                   "asset",     None,        None),
    ("1-02-03-01-0001", "Intangibles — General",         "asset",     None,        None),
    # ── 2 LIABILITIES ────────────────────────────────────────────────────
    ("2",               "Liabilities",                   "liability", None,        None),
    ("2-01",            "Current Liabilities",           "liability", "operating", None),
    ("2-01-01",         "Trade Payables",                "liability", None,        None),
    ("2-01-01-01",      "Trade Creditors",               "liability", None,        None),
    ("2-01-01-01-0001", "Trade Creditors — General",     "liability", None,        None),
    ("2-01-02",         "Accrued & Payroll Liabilities", "liability", None,        None),
    ("2-01-02-01",      "Employee Payables",             "liability", None,        None),
    ("2-01-02-02",      "Payroll Liabilities",           "liability", None,        None),
    ("2-01-02-02-0001", "Salary Payable",                "liability", None,        None),
    ("2-01-02-02-0002", "PF Payable",                    "liability", None,        None),
    ("2-01-02-02-0003", "Loan Deductions Clearing",      "liability", None,        None),
    ("2-01-02-03",      "Accrued Expenses",              "liability", None,        None),
    ("2-01-02-03-0001", "Accrued Expenses — General",    "liability", None,        None),
    ("2-01-03",         "Tax Liabilities",               "liability", None,        None),
    ("2-01-03-01",      "Output Sales Tax",              "liability", None,        None),
    ("2-01-03-01-0001", "Output Sales Tax",              "liability", None,        None),
    ("2-01-03-02",      "Income Tax Payable",            "liability", None,        None),
    ("2-01-03-02-0001", "Income Tax Payable",            "liability", None,        None),
    ("2-01-03-03",      "WHT Payable",                   "liability", None,        None),
    ("2-01-03-03-0001", "WHT Payable",                   "liability", None,        None),
    ("2-01-03-04",      "Further Tax Payable",           "liability", None,        None),
    ("2-01-03-04-0001", "Further Tax Payable",           "liability", None,        None),
    ("2-01-04",         "Advances from Customers",       "liability", None,        None),
    ("2-01-04-01",      "Customer Advances",             "liability", None,        None),
    ("2-01-04-01-0001", "Customer Advances — General",   "liability", None,        None),
    ("2-02",            "Non-Current Liabilities",       "liability", "financing", None),
    ("2-02-01",         "Long-term Loans",               "liability", None,        None),
    ("2-02-01-01",      "Long-term Loans",               "liability", None,        None),
    ("2-02-01-01-0001", "Long-term Loans — General",     "liability", None,        None),
    ("2-02-02",         "Employee End-of-Service Benefits", "liability", None,     None),
    ("2-02-02-01",      "EOSB Provision",                "liability", None,        None),
    ("2-02-02-01-0001", "EOSB Provision — General",      "liability", None,        None),
    # ── 3 EQUITY ─────────────────────────────────────────────────────────
    ("3",               "Equity",                        "equity",    "financing", None),
    ("3-01",            "Capital",                       "equity",    None,        None),
    ("3-01-01",         "Owner Capital",                 "equity",    None,        None),
    ("3-01-01-01",      "Owner Capital",                 "equity",    None,        None),
    ("3-01-01-01-0001", "Share Capital",                 "equity",    None,        None),
    ("3-01-01-01-0002", "Suspense Equity",               "equity",    None,        None),
    ("3-02",            "Reserves & Retained Earnings",  "equity",    None,        None),
    ("3-02-01",         "Retained Earnings",             "equity",    None,        None),
    ("3-02-01-01",      "Retained Earnings — Opening",   "equity",    None,        None),
    ("3-02-01-01-0001", "Retained Earnings — Opening",   "equity",    None,        None),
    ("3-02-01-02",      "Retained Earnings — Closing",   "equity",    None,        None),
    ("3-02-01-02-0001", "Retained Earnings — Closing",   "equity",    None,        None),
    ("3-02-02",         "Dividends",                     "equity",    None,        None),
    ("3-02-02-01",      "Dividends",                     "equity",    None,        None),
    ("3-02-02-01-0001", "Dividends",                     "equity",    None,        None),
    ("3-02-03",         "Other Comprehensive Income",    "equity",    None,        None),
    ("3-02-03-01",      "Other Comprehensive Income",    "equity",    None,        None),
    ("3-02-03-01-0001", "Other Comprehensive Income",    "equity",    None,        None),
    # ── 4 REVENUE ────────────────────────────────────────────────────────
    ("4",               "Revenue",                       "revenue",   "operating", None),
    ("4-01",            "Sales",                         "revenue",   None,        "sales"),
    ("4-01-01",         "Product Sales",                 "revenue",   None,        None),
    ("4-01-01-01",      "Product Sales",                 "revenue",   None,        None),
    ("4-01-01-01-0001", "Sales — General",               "revenue",   None,        None),
    ("4-01-02",         "Service Income",                "revenue",   None,        None),
    ("4-01-02-01",      "Service Income",                "revenue",   None,        None),
    ("4-01-02-01-0001", "Service Income — General",      "revenue",   None,        None),
    ("4-02",            "Sales Returns & Discounts",     "revenue",   None,        "sales_returns"),
    ("4-02-01",         "Sales Returns",                 "revenue",   None,        None),
    ("4-02-01-01",      "Sales Returns",                 "revenue",   None,        None),
    ("4-02-01-01-0001", "Sales Returns — General",       "revenue",   None,        None),
    ("4-02-02",         "Discounts Allowed",             "revenue",   None,        None),
    ("4-02-02-01",      "Discounts Allowed",             "revenue",   None,        None),
    ("4-02-02-01-0001", "Discounts Allowed — General",   "revenue",   None,        None),
    ("4-03",            "Other Income",                  "revenue",   None,        "other_income"),
    ("4-03-01",         "Other Income",                  "revenue",   None,        None),
    ("4-03-01-01",      "Other Income",                  "revenue",   None,        None),
    ("4-03-01-01-0001", "Other Income — General",        "revenue",   None,        None),
    # ── 5 EXPENSES ───────────────────────────────────────────────────────
    ("5",               "Expenses",                      "expense",   "operating", None),
    ("5-01",            "Cost of Sales",                 "expense",   None,        "cost_of_sales"),
    ("5-01-01",         "Cost of Goods Sold",            "expense",   None,        None),
    ("5-01-01-01",      "Cost of Goods Sold",            "expense",   None,        None),
    ("5-01-01-01-0001", "Cost of Goods Sold",            "expense",   None,        None),
    ("5-01-02",         "Stock Losses, Scrap & Adjustments", "expense", None,      None),
    ("5-01-02-01",      "Stock Losses & Scrap",          "expense",   None,        None),
    ("5-01-02-01-0001", "Scrap / Write-off",             "expense",   None,        None),
    ("5-01-02-01-0002", "Inventory Adjustment",          "expense",   None,        None),
    ("5-01-02-01-0003", "Consumption Expense",           "expense",   None,        None),
    # Where a retroactive change lands. Deleting a receipt whose stock was
    # already issued leaves a posted cost backed by a purchase that no longer
    # exists; the difference is booked here rather than restating the frozen
    # cost or silently untying inventory from COGS.
    ("5-01-02-01-0004", "Inventory Cost Variance",       "expense",   None,        None),
    ("5-02",            "Administrative Expenses",       "expense",   None,        "admin"),
    ("5-02-01",         "Salaries & Benefits",           "expense",   None,        None),
    ("5-02-01-01",      "Salaries & Benefits",           "expense",   None,        None),
    ("5-02-01-01-0001", "Salary Expense",                "expense",   None,        None),
    ("5-02-01-01-0002", "PF Employer Expense",           "expense",   None,        None),
    ("5-02-02",         "Rent & Utilities",              "expense",   None,        None),
    ("5-02-02-01",      "Rent & Utilities",              "expense",   None,        None),
    ("5-02-02-01-0001", "Rent Expense",                  "expense",   None,        None),
    ("5-02-02-01-0002", "Utilities Expense",             "expense",   None,        None),
    ("5-02-03",         "Office & Supplies",             "expense",   None,        None),
    ("5-02-03-01",      "Office & Supplies",             "expense",   None,        None),
    ("5-02-03-01-0001", "Office Supplies",               "expense",   None,        None),
    ("5-02-04",         "Depreciation & Amortisation",   "expense",   None,        None),
    ("5-02-04-01",      "Depreciation & Amortisation",   "expense",   None,        None),
    ("5-02-04-01-0001", "Depreciation Expense",          "expense",   None,        None),
    ("5-02-05",         "Fees, Taxes & Licenses",        "expense",   None,        None),
    ("5-02-05-01",      "Fees, Taxes & Licenses",        "expense",   None,        None),
    ("5-02-05-01-0001", "Fees, Taxes & Licenses — General", "expense", None,       None),
    ("5-03",            "Selling & Distribution",        "expense",   None,        "selling_distribution"),
    ("5-03-01",         "Freight & Transportation",      "expense",   None,        None),
    ("5-03-01-01",      "Freight & Transportation",      "expense",   None,        None),
    ("5-03-01-01-0001", "Freight & Transportation — General", "expense", None,     None),
    ("5-03-02",         "Commissions",                   "expense",   None,        None),
    ("5-03-02-01",      "Commissions",                   "expense",   None,        None),
    ("5-03-02-01-0001", "Commissions — General",         "expense",   None,        None),
    ("5-03-03",         "Marketing & Advertising",       "expense",   None,        None),
    ("5-03-03-01",      "Marketing & Advertising",       "expense",   None,        None),
    ("5-03-03-01-0001", "Marketing & Advertising — General", "expense", None,      None),
    ("5-04",            "Other Operating Expenses",      "expense",   None,        "other_operating"),
    ("5-04-01",         "Other Operating Expenses",      "expense",   None,        None),
    ("5-04-01-01",      "Other Operating Expenses",      "expense",   None,        None),
    ("5-04-01-01-0001", "Other Expenses — General",      "expense",   None,        None),
    # Disposals net to one account: a loss is a debit, a gain a credit, so the
    # balance reads as the net result for the period. Never book this to
    # Depreciation Expense — that overstates depreciation and distorts EBITDA.
    ("5-04-01-01-0002", "Loss/(Gain) on Disposal of Fixed Assets", "expense", None, None),
    ("5-05",            "Finance Costs",                 "expense",   None,        "finance_cost"),
    ("5-05-01",         "Bank & Interest Charges",       "expense",   None,        None),
    ("5-05-01-01",      "Bank & Interest Charges",       "expense",   None,        None),
    ("5-05-01-01-0001", "Bank Charges",                  "expense",   None,        None),
    ("5-05-01-01-0002", "Interest Expense",              "expense",   None,        None),
    ("5-06",            "Taxation",                      "expense",   None,        "income_tax"),
    ("5-06-01",         "Income Tax",                    "expense",   None,        None),
    ("5-06-01-01",      "Income Tax",                    "expense",   None,        None),
    ("5-06-01-01-0001", "Income Tax Expense",            "expense",   None,        None),
]

# Semantic posting roles -> new canonical level-5 codes.
ROLE_CODES = {
    "cash":              "1-01-01-01-0001",
    "ar":                "1-01-02-01-0001",
    "inventory":         "1-01-04-01-0001",
    "fixed_assets":      "1-02-01-01-0001",
    "input_tax":         "1-01-05-01-0001",
    "ap":                "2-01-01-01-0001",
    "accrued":           "2-01-02-03-0001",
    "loans":             "2-02-01-01-0001",
    "wht_payable":       "2-01-03-03-0001",
    "sales_tax_payable": "2-01-03-01-0001",
    "further_tax_payable": "2-01-03-04-0001",
    "revenue":           "4-01-01-01-0001",
    "cogs":              "5-01-01-01-0001",
    "inventory_variance": "5-01-02-01-0004",
    "sales_returns":     "4-02-01-01-0001",
    "retained_earnings":          "3-02-01-02-0001",
    "dividends":                  "3-02-02-01-0001",
    "other_comprehensive_income": "3-02-03-01-0001",
    "suspense_equity":            "3-01-01-01-0002",
    "accumulated_depreciation": "1-02-02-01-0001",
    "depreciation_expense": "5-02-04-01-0001",
    "disposal_gain_loss": "5-04-01-01-0002",
}

# Entity subledger kinds -> level-4 parent code. Entity accounts are level-5
# children coded <parent>-<entity_id + 100> (0001-0099 reserved for defaults).
ENTITY_PARENT_CODES = {
    "supplier": "2-01-01-01",   # Trade Creditors
    "customer": "1-01-02-01",   # Trade Debtors
    "product":  "1-01-04-01",   # Trading Goods Stock
    "employee": "2-01-02-01",   # Employee Payables
    "loan":     "1-01-03-01",   # Employee Loans & Advances
    "fixed_asset": "1-02-01-01", # Property, Plant & Equipment — Owned Assets
    "accum_dep":  "1-02-02-01", # Accumulated Depreciation
}
ENTITY_ID_OFFSET = 100

# Legacy flat/old-series codes -> new level-5 codes. Covers both numbering
# generations (production 1000-series and the older 111-series seed).
LEGACY_CODE_MAP = {
    "1000": "1-01-01-01-0001", "111": "1-01-01-01-0001",
    "1100": "1-01-02-01-0001", "112": "1-01-02-01-0001",
    "1200": "1-01-04-01-0001", "113": "1-01-04-01-0001",
    "1300": "1-02-01-01-0001", "121": "1-02-01-01-0001",
    "1400": "1-01-05-01-0001", "114": "1-01-05-01-0001",
    "2000": "2-01-01-01-0001", "211": "2-01-01-01-0001",
    "2100": "2-01-02-03-0001", "212": "2-01-02-03-0001",
    "2200": "2-02-01-01-0001", "221": "2-02-01-01-0001",
    "6400": "2-01-03-03-0001", "214": "2-01-03-03-0001",
    "6500": "2-01-03-01-0001", "213": "2-01-03-01-0001",
    "4000": "4-01-01-01-0001", "411": "4-01-01-01-0001",
    "412":  "4-01-02-01-0001",
    "5000": "5-01-01-01-0001", "511": "5-01-01-01-0001",
    "5121": "5-02-01-01-0001",
    "5122": "5-02-01-01-0002",
    "2121": "2-01-02-02-0001",
    "2122": "2-01-03-02-0001",
    "2123": "2-01-02-02-0002",
    "2124": "2-01-02-02-0003",
    "5700": "5-01-02-01-0003",
    "5800": "5-01-02-01-0001",
    "5900": "5-01-02-01-0002",
    "3111": "3-01-01-01-0001", "3000": "3-01-01-01-0001",
    "3112": "3-02-01-01-0001", "3100": "3-02-01-01-0001",
    "1111": "1-01-06-01-0001",
    # Remaining production flat-series expense/revenue heads.
    "4100": "4-01-02-01-0001",
    "5100": "5-02-01-01-0001", "512": "5-02-01-01-0001",
    "5200": "5-02-02-01-0001", "513": "5-02-02-01-0001",
    "5300": "5-02-03-01-0001", "514": "5-02-03-01-0001",
    "5400": "5-03-01-01-0001", "515": "5-03-01-01-0001",
    "5500": "5-02-05-01-0001", "516": "5-02-05-01-0001",
    "5600": "5-02-04-01-0001", "517": "5-02-04-01-0001",
    "519":  "5-04-01-01-0001",
    "6000": "5-01-01-01-0002",   # Purchase Discounts (contra to COGS)
    "6600": "5-01-01-01-0003",   # Purchase Returns  (contra to COGS)
    "6100": "5-03-02-01-0001",
    "6200": "5-03-01-01-0001",   # Freight (shares slot with 5400; loser parks next door)
    "6300": "5-03-01-01-0002",
}

# Unmatched legacy accounts that carry journal lines land under these
# type-based catch-all level-4 parents.
CATCHALL_PARENT_CODES = {
    "asset":     "1-01-06-03",   # Other Current Assets
    "liability": "2-01-02-03",   # Accrued Expenses
    "equity":    "3-02-01-01",   # Retained Earnings
    "revenue":   "4-03-01-01",   # Other Income
    "expense":   "5-04-01-01",   # Other Operating Expenses
}

# Old entity-account code suffix, e.g. "112-C0007" / "2100-E0012".
LEGACY_ENTITY_RE = re.compile(r"^.+-([SCPEL])(\d+)$")
ENTITY_PREFIX_KIND = {"S": "supplier", "C": "customer", "P": "product",
                      "E": "employee", "L": "loan"}


def code_level(code):
    return len(code.split("-"))


def parent_code_of(code):
    return code.rsplit("-", 1)[0] if "-" in code else None


def is_segmented(code):
    return bool(SEGMENTED_RE.match(code or ""))


def next_child_code(parent):
    """Next free segmented code under ``parent`` — 2-digit segments for
    levels 2-4, 4-digit operational segments for level 5. Queries by prefix
    (not the relationship collection) so pending re-parents are visible."""
    width = 4 if parent.level == 4 else 2
    used = set()
    rows = ChartOfAccount.query.filter(
        ChartOfAccount.code.like(parent.code + "-%")).all()
    for child in rows:
        rest = child.code[len(parent.code) + 1:]
        if rest.isdigit():
            used.add(int(rest))
    n = 1
    while n in used:
        n += 1
    return f"{parent.code}-{n:0{width}d}"


def _get(code):
    return ChartOfAccount.query.filter_by(code=code).first()


def seed_fixed_tree(levels=(1, 2, 3, 4, 5)):
    """Create the fixed chart idempotently (keyed on code). Existing rows get
    their tags/level/fixed flag trued up but keep their name if user-edited."""
    created = {}
    for code, name, type_, cf, pl in FIXED_ACCOUNTS:
        level = code_level(code)
        if level not in levels:
            continue
        acct = _get(code)
        if not acct:
            parent = _get(parent_code_of(code)) if parent_code_of(code) else None
            acct = ChartOfAccount(code=code, name=name, type=type_,
                                  parent_id=parent.id if parent else None,
                                  level=level, is_fixed=(level <= 4),
                                  cash_flow_activity=cf, pl_section=pl)
            db.session.add(acct)
            db.session.flush()
        else:
            acct.level = level
            if level <= 4:
                acct.is_fixed = True
            if cf and not acct.cash_flow_activity:
                acct.cash_flow_activity = cf
            if pl and not acct.pl_section:
                acct.pl_section = pl
        created[code] = acct
    return created


def _has_lines(acct_id):
    return db.session.query(JournalLine.id).filter_by(account_id=acct_id).first() is not None


def _retag(acct, new_code, parent):
    acct.code = new_code
    acct.parent_id = parent.id
    acct.level = 5
    acct.type = parent.type
    acct.is_fixed = False


def migrate_legacy_coa():
    """Re-code/re-parent every legacy (non-segmented) account onto the fixed
    tree, in place. Journal lines are untouched (they reference row ids)."""
    # The aggregating skeleton must exist before anything is re-parented.
    seed_fixed_tree(levels=(1, 2, 3, 4))

    legacy = [a for a in ChartOfAccount.query.all() if not is_segmented(a.code)]
    if not legacy:
        seed_fixed_tree(levels=(5,))
        db.session.flush()
        return

    # 1) Entity subledger accounts (children first, so their old parents can
    #    be safely converted or retired afterwards).
    for acct in legacy:
        m = LEGACY_ENTITY_RE.match(acct.code)
        if not m:
            continue
        kind = ENTITY_PREFIX_KIND[m.group(1)]
        parent = _get(ENTITY_PARENT_CODES[kind])
        new_code = f"{parent.code}-{int(m.group(2)) + ENTITY_ID_OFFSET:04d}"
        clash = _get(new_code)
        if clash and clash.id != acct.id:
            new_code = next_child_code(parent)
        _retag(acct, new_code, parent)
        db.session.flush()

    # 2) Explicitly mapped posting accounts -> their canonical L5 slot. Names
    #    are standardised to the fixed-tree name for that slot.
    slot_names = {code: name for code, name, *_ in FIXED_ACCOUNTS}
    for acct in legacy:
        target = LEGACY_CODE_MAP.get(acct.code)
        if not target:
            continue
        parent = _get(parent_code_of(target))
        occupied = _get(target)
        if occupied and occupied.id != acct.id:
            # Slot already filled (e.g. seeded earlier, or another legacy
            # generation claimed it first); park next to it, name kept.
            _retag(acct, next_child_code(parent), parent)
        else:
            _retag(acct, target, parent)
            acct.name = slot_names.get(target, acct.name)
            acct.is_active = True
        db.session.flush()

    # 3) Remaining legacy accounts: keep any that carry journal lines or are
    #    leaves someone might still post to — as L5 under the type catch-all.
    #    Old empty aggregates (Assets, Current Assets, ...) are retired.
    remaining = [a for a in ChartOfAccount.query.all() if not is_segmented(a.code)]
    # Deepest first so children are moved before their parents are examined.
    remaining.sort(key=lambda a: -(a.level or 0))
    for acct in remaining:
        has_children = ChartOfAccount.query.filter_by(parent_id=acct.id).count() > 0
        if _has_lines(acct.id) or (not has_children and acct.is_active
                                   and acct.level and acct.level >= 4):
            parent = _get(CATCHALL_PARENT_CODES.get(acct.type, "5-04-01-01"))
            _retag(acct, next_child_code(parent), parent)
            db.session.flush()
        elif not has_children:
            acct.is_active = False
            acct.is_fixed = False
            if not acct.name.endswith("(legacy)"):
                acct.name = f"{acct.name} (legacy)"
    db.session.flush()

    # Second pass: aggregates that only became childless after the loop above.
    for acct in [a for a in ChartOfAccount.query.all() if not is_segmented(a.code)]:
        if ChartOfAccount.query.filter_by(parent_id=acct.id).count() == 0 \
                and not _has_lines(acct.id):
            acct.is_active = False
            acct.is_fixed = False
            if not acct.name.endswith("(legacy)"):
                acct.name = f"{acct.name} (legacy)"

    # 4) Fill in the seeded L5 defaults that migration didn't claim.
    seed_fixed_tree(levels=(5,))
    db.session.flush()


def ensure_fixed_coa():
    """Startup entry point: build the fixed tree and absorb any legacy chart."""
    migrate_legacy_coa()
