from datetime import date, datetime
from shared.extensions import db
from shared.models.money import Money


class AssetCategory(db.Model):
    __tablename__ = "fa_categories"
    __table_args__ = (
        db.UniqueConstraint("company_id", "name",
                            name="uq_fa_categories_name"),
    )
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)
    name = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text, default="")
    default_useful_life = db.Column(db.Integer, default=5)
    default_depreciation_method = db.Column(db.String(20), default="straight_line")
    default_salvage_value_pct = db.Column(Money, default=0)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    assets = db.relationship("FixedAsset", backref="category_obj", lazy="dynamic")

    @staticmethod
    def seed():
        defaults = [
            ("Computers & IT Equipment", "Desktop computers, laptops, servers, printers", 5, "straight_line", 0),
            ("Office Furniture", "Desks, chairs, cabinets, shelving", 10, "straight_line", 5),
            ("Vehicles", "Cars, trucks, forklifts", 8, "declining_balance", 10),
            ("Machinery & Equipment", "Production machines, tools, heavy equipment", 15, "straight_line", 5),
            ("Buildings & Leasehold Improvements", "Office buildings, warehouse, renovations", 30, "straight_line", 10),
            ("Software & Licenses", "ERP licenses, development tools", 3, "straight_line", 0),
        ]
        for name, desc, life, method, sv_pct in defaults:
            if not AssetCategory.query.filter_by(name=name).first():
                db.session.add(AssetCategory(
                    name=name, description=desc,
                    default_useful_life=life,
                    default_depreciation_method=method,
                    default_salvage_value_pct=sv_pct,
                ))

    def __repr__(self):
        return f"<AssetCategory {self.name}>"


class FixedAsset(db.Model):
    __tablename__ = "fixed_assets"
    __table_args__ = (
        db.UniqueConstraint("company_id", "asset_code",
                            name="uq_fixed_assets_asset_code"),
    )
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)
    asset_code = db.Column(db.String(50), nullable=False)
    name = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, default="")
    category_id = db.Column(db.Integer, db.ForeignKey("fa_categories.id"), nullable=False)
    purchase_date = db.Column(db.Date, nullable=False)
    purchase_cost = db.Column(Money, nullable=False, default=0)
    useful_life = db.Column(db.Integer, nullable=False, default=5)
    depreciation_method = db.Column(db.String(20), nullable=False, default="straight_line")
    salvage_value = db.Column(Money, default=0)
    accumulated_depreciation = db.Column(Money, default=0)
    current_book_value = db.Column(Money, nullable=False, default=0)
    status = db.Column(db.String(20), default="active")
    location = db.Column(db.String(200), default="")
    assigned_to = db.Column(db.String(100), default="")
    vendor = db.Column(db.String(200), default="")
    serial_number = db.Column(db.String(100), default="")
    fixed_asset_account_id = db.Column(db.Integer, db.ForeignKey("chart_of_accounts.id"), nullable=True)
    accum_dep_account_id = db.Column(db.Integer, db.ForeignKey("chart_of_accounts.id"), nullable=True)
    dep_expense_account_id = db.Column(db.Integer, db.ForeignKey("chart_of_accounts.id"), nullable=True)
    # Credited when the acquisition is booked (payable, cash or bank).
    acquisition_credit_account_id = db.Column(db.Integer, db.ForeignKey("chart_of_accounts.id"), nullable=True)
    notes = db.Column(db.Text, default="")
    # Project label: tags the acquisition and every depreciation charge, so a
    # project's P&L carries the depreciation of the assets it uses.
    label_id = db.Column(db.Integer, db.ForeignKey("project_labels.id"))
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    depreciation_entries = db.relationship("AssetDepreciation", backref="asset",
                                           lazy="dynamic", order_by="AssetDepreciation.entry_date")

    # ── Derived balances ────────────────────────────────────────────────────
    # ``accumulated_depreciation`` / ``current_book_value`` are CACHES. The
    # truth is the set of depreciation rows whose journal entry is still posted,
    # so deleting a voucher or un-posting it (reverse_journal_entry flips
    # is_posted and keeps the row) silently drops that month back out of the
    # total. Nothing accumulates by incrementing a stored number, so a reversal
    # can never leave the asset stranded at a figure no journal supports.

    def live_depreciation_query(self):
        """Depreciation rows still backed by a posted journal entry.

        A NULL ``journal_entry_id`` means the row predates the link and has no
        journal to contradict it, so it counts.
        """
        from shared.models.ledger import JournalEntry
        return (AssetDepreciation.query
                .outerjoin(JournalEntry,
                           AssetDepreciation.journal_entry_id == JournalEntry.id)
                .filter(AssetDepreciation.asset_id == self.id)
                .filter(db.or_(AssetDepreciation.journal_entry_id.is_(None),
                               JournalEntry.is_posted == True)))

    @property
    def posted_depreciation(self):
        """Accumulated depreciation derived from live rows, floored at cost."""
        from shared.extensions import db as _db
        total = (self.live_depreciation_query()
                 .with_entities(_db.func.coalesce(
                     _db.func.sum(AssetDepreciation.amount), 0)).scalar()) or 0
        return min(float(total), float(self.depreciable_amount))

    def recalculate(self):
        """Refresh the cached columns from the live rows. Idempotent."""
        self.accumulated_depreciation = self.posted_depreciation
        self.current_book_value = self.purchase_cost - self.accumulated_depreciation
        return self.accumulated_depreciation

    @property
    def net_book_value(self):
        return self.purchase_cost - self.posted_depreciation

    @property
    def depreciable_amount(self):
        return self.purchase_cost - self.salvage_value

    @property
    def remaining_depreciable(self):
        """What may still be charged before the salvage floor is reached."""
        return max(0.0, float(self.depreciable_amount) - float(self.posted_depreciation))

    METHODS = ("straight_line", "declining_balance")

    def monthly_charges(self, months, charged_months, accumulated=None):
        """The next ``months`` monthly charges, after ``charged_months`` of the
        life have already been charged and ``accumulated`` has been posted.

        Changes in estimate are prospective (IAS 16 / IAS 8): whatever is still
        depreciable is spread over whatever life is left, so editing the life,
        salvage or cost re-plans the remaining charges instead of carrying on
        at the original rate. The month the life ends takes the remainder, so
        book value lands exactly on salvage — straight-line used to stop a few
        rupees short from rounding, and reducing balance never reached salvage
        at all (it stopped at the end of the life with a residual nothing would
        ever charge).

        Reducing balance is double-declining (2 / life on net book value) with
        the standard switch to straight-line once that gives the larger charge.
        """
        cost = float(self.purchase_cost or 0)
        salvage = float(self.salvage_value or 0)
        life_m = int(self.useful_life or 0) * 12
        acc = float(self.posted_depreciation if accumulated is None else accumulated)
        rate = (2.0 / self.useful_life / 12) if self.useful_life else 0
        out = []
        for i in range(max(0, int(months))):
            remaining = cost - salvage - acc
            if remaining <= 0.005 or life_m <= 0:
                break
            left = life_m - charged_months - i
            if left <= 1:
                charge = remaining        # last month of the life, or overdue
            else:
                charge = remaining / left
                if self.depreciation_method == "declining_balance":
                    charge = max(charge, (cost - acc) * rate)
                charge = min(charge, remaining)
            acc += charge
            out.append(charge)
        return out

    @property
    def annual_depreciation(self):
        """The charge planned for the next twelve months."""
        from fixed_assets_app.routes.depreciation import charged_months
        return round(sum(self.monthly_charges(12, charged_months(self))), 2)

    @staticmethod
    def next_code():
        """FA-0001, FA-0002, … per company. Three creation paths used to
        number from the last id or from count()+1 — the latter collided with
        the unique code after any delete and the create failed.

        Two users creating assets at once both read the same highest code, so
        the read is queued per company (an advisory lock held to commit) and
        the second one numbers after the first one's asset."""
        import re
        from shared.concurrency import serialize
        from shared.tenancy import current_company_id
        serialize("asset-code", current_company_id())
        top = 0
        for (code,) in db.session.query(FixedAsset.asset_code).filter(
                FixedAsset.asset_code.like("FA-%")):
            m = re.fullmatch(r"FA-(\d+)", code or "")
            if m:
                top = max(top, int(m.group(1)))
        return f"FA-{top + 1:04d}"

    def __repr__(self):
        return f"<FixedAsset {self.asset_code}: {self.name}>"


class AssetDepreciation(db.Model):
    __tablename__ = "asset_depreciation"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)
    asset_id = db.Column(db.Integer, db.ForeignKey("fixed_assets.id"), nullable=False)
    entry_date = db.Column(db.Date, nullable=False)
    amount = db.Column(Money, nullable=False, default=0)
    # accumulated_after / net_book_value_after are a point-in-time SNAPSHOT for
    # display. They are deliberately not read back for arithmetic: a later
    # reversal would make them lie. FixedAsset derives its totals instead.
    accumulated_after = db.Column(Money, nullable=False, default=0)
    net_book_value_after = db.Column(Money, nullable=False, default=0)
    # The journal this charge was posted as. Un-posting or deleting it removes
    # this row from every derived total.
    journal_entry_id = db.Column(db.Integer, db.ForeignKey("journal_entries.id"), nullable=True)
    notes = db.Column(db.String(200), default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    @property
    def is_live(self):
        from shared.models.ledger import JournalEntry
        from shared.tenancy import scoped_get
        if not self.journal_entry_id:
            return True
        je = scoped_get(JournalEntry, self.journal_entry_id)
        return bool(je and je.is_posted)

    def __repr__(self):
        return f"<AssetDepreciation {self.asset_id} @ {self.entry_date}: {self.amount}>"
