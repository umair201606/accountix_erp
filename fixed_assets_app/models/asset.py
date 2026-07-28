from datetime import date, datetime
from shared.extensions import db


class AssetCategory(db.Model):
    __tablename__ = "fa_categories"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False, unique=True)
    description = db.Column(db.Text, default="")
    default_useful_life = db.Column(db.Integer, default=5)
    default_depreciation_method = db.Column(db.String(20), default="straight_line")
    default_salvage_value_pct = db.Column(db.Float, default=0)
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
    id = db.Column(db.Integer, primary_key=True)
    asset_code = db.Column(db.String(50), unique=True, nullable=False)
    name = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, default="")
    category_id = db.Column(db.Integer, db.ForeignKey("fa_categories.id"), nullable=False)
    purchase_date = db.Column(db.Date, nullable=False)
    purchase_cost = db.Column(db.Float, nullable=False, default=0)
    useful_life = db.Column(db.Integer, nullable=False, default=5)
    depreciation_method = db.Column(db.String(20), nullable=False, default="straight_line")
    salvage_value = db.Column(db.Float, default=0)
    accumulated_depreciation = db.Column(db.Float, default=0)
    current_book_value = db.Column(db.Float, nullable=False, default=0)
    status = db.Column(db.String(20), default="active")
    location = db.Column(db.String(200), default="")
    assigned_to = db.Column(db.String(100), default="")
    vendor = db.Column(db.String(200), default="")
    serial_number = db.Column(db.String(100), default="")
    notes = db.Column(db.Text, default="")
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    depreciation_entries = db.relationship("AssetDepreciation", backref="asset",
                                           lazy="dynamic", order_by="AssetDepreciation.entry_date")

    @property
    def net_book_value(self):
        return self.purchase_cost - self.accumulated_depreciation

    @property
    def depreciable_amount(self):
        return self.purchase_cost - self.salvage_value

    @property
    def annual_depreciation(self):
        if self.useful_life <= 0:
            return 0
        if self.depreciation_method == "straight_line":
            return self.depreciable_amount / self.useful_life
        elif self.depreciation_method == "declining_balance":
            rate = 2.0 / self.useful_life
            return (self.purchase_cost - self.accumulated_depreciation) * rate
        return 0

    def calculate_depreciation(self, for_date=None):
        if for_date is None:
            for_date = date.today()
        years_elapsed = (for_date - self.purchase_date).days / 365.0
        if years_elapsed <= 0:
            return 0
        total_months = int(years_elapsed * 12)
        months_to_depreciate = min(total_months, self.useful_life * 12)
        return (self.annual_depreciation / 12) * months_to_depreciate

    def __repr__(self):
        return f"<FixedAsset {self.asset_code}: {self.name}>"


class AssetDepreciation(db.Model):
    __tablename__ = "asset_depreciation"
    id = db.Column(db.Integer, primary_key=True)
    asset_id = db.Column(db.Integer, db.ForeignKey("fixed_assets.id"), nullable=False)
    entry_date = db.Column(db.Date, nullable=False)
    amount = db.Column(db.Float, nullable=False, default=0)
    accumulated_after = db.Column(db.Float, nullable=False, default=0)
    net_book_value_after = db.Column(db.Float, nullable=False, default=0)
    notes = db.Column(db.String(200), default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<AssetDepreciation {self.asset_id} @ {self.entry_date}: {self.amount}>"
