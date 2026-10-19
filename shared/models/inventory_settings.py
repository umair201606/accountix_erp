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
    # Per-line project-label column on voucher rows and invoice item rows,
    # turned on independently per document class. Off (default): lines take
    # the header/party label server-side and the column is hidden. On: the
    # form shows one label pick per row, prefilled with the header/party
    # label or the class default, editable per line.
    per_line_labeling_voucher = db.Column(db.Boolean, default=False)
    per_line_labeling_invoice = db.Column(db.Boolean, default=False)
    # Default project labels per document class: a voucher / invoice saved
    # without an explicit pick gets its class default. Deactivated labels no
    # longer resolve (default_*_label() returns None), so an archived default
    # can never keep auto-assigning.
    default_voucher_label_id = db.Column(db.Integer,
                                         db.ForeignKey("project_labels.id"),
                                         nullable=True)
    default_invoice_label_id = db.Column(db.Integer,
                                         db.ForeignKey("project_labels.id"),
                                         nullable=True)

    @classmethod
    def get(cls):
        from shared.tenancy import current_company_id
        cid = current_company_id()
        if cid is None:
            # No active company (login page, boot): return an unsaved default
            # rather than querying a tenant-scoped table (which fails closed).
            return cls()
        s = cls.query.filter_by(company_id=cid).order_by(cls.id).first()
        if not s:
            # First use: queued per company, so two requests arriving together
            # create one row, not two (with two, .first() could hand back either
            # and a setting saved on one came and went).
            from shared.concurrency import serialize
            serialize("first-use", cls.__tablename__, cid)
            s = cls.query.filter_by(company_id=cid).order_by(cls.id).first()
        if not s:
            s = cls(company_id=cid)
            db.session.add(s)
            # Flush only this row: the getter may run mid-save while another
            # object (a half-built invoice) is pending in the session.
            db.session.flush(objects=[s])
        return s

    def _default_label(self, col):
        if getattr(self, col) is None:
            return None
        from shared.models.project_label import ProjectLabel
        return ProjectLabel.query.filter_by(
            id=getattr(self, col), is_active=True).first()

    def default_voucher_label(self):
        return self._default_label("default_voucher_label_id")

    def default_invoice_label(self):
        return self._default_label("default_invoice_label_id")

    def is_fifo(self):
        return self.valuation_method == "fifo"

    def is_weighted_average(self):
        return self.valuation_method == "weighted_average"
