from shared.extensions import db


class InventorySettings(db.Model):
    __tablename__ = "inventory_settings"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)
    valuation_method = db.Column(db.String(20), default="weighted_average")
    allow_negative_stock = db.Column(db.Boolean, default=False)
    decimal_places = db.Column(db.Integer, default=4)
    auto_generate_vouchers = db.Column(db.Boolean, default=True)
    purchase_flow = db.Column(db.String(20), default="with_po")  # with_po or direct_invoice
    sales_flow = db.Column(db.String(20), default="with_so")     # with_so or direct_invoice
    default_cogs_account_id = db.Column(db.Integer, db.ForeignKey("chart_of_accounts.id"), nullable=True)
    default_inventory_account_id = db.Column(db.Integer, db.ForeignKey("chart_of_accounts.id"), nullable=True)
    default_return_account_id = db.Column(db.Integer, db.ForeignKey("chart_of_accounts.id"), nullable=True)

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

    def is_fifo(self):
        return self.valuation_method == "fifo"

    def is_weighted_average(self):
        return self.valuation_method == "weighted_average"
