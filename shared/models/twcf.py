"""13-Week Cash Flow (TWCF) forecast lines.

The forecast itself is never stored — the matrix is recomputed from posted
journals (opening cash, customer-collection and supplier-payment schedules
from the aged AR/AP balances) plus these user-entered lines. A line is either
a one-off cash event ("VAT payment 15 Oct") or a recurring obligation
("Salaries, monthly on the 25th") that repeats across the 13-week window.

Recurring occurrences that fall outside the currently-viewed window are
simply not shown; the same lines re-bucket when the user rolls the forecast
forward to a later start date, which is how the report "rolls".
"""

from datetime import datetime

from shared.extensions import db

# Direction of the cash movement the line forecasts.
TWCF_IN = "in"
TWCF_OUT = "out"

# Frequencies a recurring line can repeat with.
TWCF_ONEOFF = "oneoff"
TWCF_WEEKLY = "weekly"
TWCF_MONTHLY = "monthly"
TWCF_QUARTERLY = "quarterly"
TWCF_YEARLY = "yearly"

# Display names for the report rows (inflows first, then outflows).
TWCF_IN_CATEGORIES = {
    "collections": "Collections from customers",
    "other_in": "Other receipts",
}
TWCF_OUT_CATEGORIES = {
    "suppliers": "Payments to suppliers",
    "payroll": "Salaries & wages",
    "rent": "Rent & utilities",
    "taxes": "Taxes & duties",
    "capex": "Capital expenditure",
    "debt": "Loan repayments",
    "other_out": "Other payments",
}

# Categories a user-entered line may claim. The auto rows (collections /
# suppliers) are computed from ledgers and are not user-editable.
TWCF_USER_IN_CATEGORIES = ["other_in"]
TWCF_USER_OUT_CATEGORIES = ["payroll", "rent", "taxes", "capex", "debt",
                            "other_out"]

TWCF_FREQUENCIES = [TWCF_ONEOFF, TWCF_WEEKLY, TWCF_MONTHLY,
                    TWCF_QUARTERLY, TWCF_YEARLY]


class TwcfLine(db.Model):
    __tablename__ = "twcf_lines"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)

    # "in" (receipt) or "out" (payment). The category refines it; only the
    # user-enterable categories above are allowed.
    direction = db.Column(db.String(5), nullable=False)
    category = db.Column(db.String(30), nullable=False)
    description = db.Column(db.String(200), nullable=False)
    amount = db.Column(db.Numeric(16, 4), nullable=False, default=0)

    # First occurrence (one-off lines: the single occurrence).
    start_date = db.Column(db.Date, nullable=False)

    # "oneoff" | "weekly" | "monthly" | "quarterly" | "yearly"
    frequency = db.Column(db.String(10), nullable=False, default=TWCF_ONEOFF)
    # Weekly lines land on a weekday (0=Mon..6); monthly/quarterly/yearly
    # lines land on a day-of-month (1..31, clamped); yearly lines also carry
    # a month (1..12).
    day_of_week = db.Column(db.Integer, default=0)
    day_of_month = db.Column(db.Integer, default=1)
    month = db.Column(db.Integer, default=1)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):  # pragma: no cover - debug aid
        return (f"<TwcfLine {self.direction} {self.category} "
                f"{self.description} {self.amount} @ {self.start_date}>")
