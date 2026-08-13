"""Tenancy models: companies, memberships, invitations, global limits.

These are GLOBAL tables — they must never declare ``company_id``.
"""
from datetime import datetime

from shared.extensions import db


class Company(db.Model):
    """A tenant. Holds the profile/letterhead fields that used to live in
    CompanyInfo (which becomes a per-company row of this table), plus quota
    fields driven by super admin / subscription plan."""
    __tablename__ = "companies"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    slug = db.Column(db.String(120), unique=True, nullable=False)
    is_active = db.Column(db.Boolean, default=True)
    # Quota: NULL max_members = use GlobalLimits.default_max_members.
    plan_name = db.Column(db.String(50), default="free")
    max_members = db.Column(db.Integer, nullable=True)
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

    # Module entitlement, set by the super admin. Default on, so existing
    # companies keep every module they had before this existed. A user's
    # effective access is this AND their own has_*_access flag — see
    # User.module_access.
    mod_hr_enabled = db.Column(db.Boolean, default=True)
    mod_inventory_enabled = db.Column(db.Boolean, default=True)
    mod_invoicing_enabled = db.Column(db.Boolean, default=True)
    mod_finance_enabled = db.Column(db.Boolean, default=True)
    mod_accounting_enabled = db.Column(db.Boolean, default=True)
    mod_fbr_enabled = db.Column(db.Boolean, default=True)
    mod_fixed_assets_enabled = db.Column(db.Boolean, default=True)
    mod_executive_enabled = db.Column(db.Boolean, default=True)

    # Profile / letterhead (previously CompanyInfo)
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
    updated_at = db.Column(db.DateTime, default=datetime.utcnow,
                           onupdate=datetime.utcnow)

    memberships = db.relationship(
        "CompanyMembership", backref="company",
        lazy="dynamic", cascade="all, delete-orphan")

    # module key (shared.permissions.MODULES) -> entitlement column. One
    # mapping, so a new module is added in exactly two places.
    MODULE_COLUMNS = {
        "hr": "mod_hr_enabled",
        "inventory": "mod_inventory_enabled",
        "invoicing": "mod_invoicing_enabled",
        "finance": "mod_finance_enabled",
        "accounting": "mod_accounting_enabled",
        "fbr": "mod_fbr_enabled",
        "fixed_assets": "mod_fixed_assets_enabled",
        "executive": "mod_executive_enabled",
    }

    @classmethod
    def by_slug(cls, slug):
        return cls.query.filter_by(slug=slug).first()

    def module_enabled(self, module_key):
        """Whether this company is entitled to a module at all.

        Unknown keys are allowed rather than denied: a module that predates
        this mapping must not silently disappear from every company.
        """
        col = self.MODULE_COLUMNS.get(module_key)
        if col is None:
            return True
        # Columns added by migration are NULL on rows written before it, and
        # NULL means "not yet decided", which is the old always-on behaviour.
        value = getattr(self, col, None)
        return True if value is None else bool(value)

    def enabled_modules(self):
        return [k for k in self.MODULE_COLUMNS if self.module_enabled(k)]


class CompanyMembership(db.Model):
    """A user's role inside one company. Role is per company, NOT global —
    the same user can be admin in A and employee in B."""
    __tablename__ = "company_memberships"

    PENDING = "pending"
    ACTIVE = "active"
    REMOVED = "removed"
    # Suspended by the super admin: the membership is kept (role, employee
    # code, history) but every gate filters on ACTIVE, so a blocked member
    # cannot enter the company. Distinct from REMOVED, which is a departure.
    BLOCKED = "blocked"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"),
                           nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"),
                        nullable=False, index=True)
    role_id = db.Column(db.Integer, db.ForeignKey("roles.id"), nullable=False)
    status = db.Column(db.String(10), default=ACTIVE, nullable=False)
    employee_code = db.Column(db.String(20), nullable=True)
    joined_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint("company_id", "user_id",
                            name="uq_membership_company_user"),
        db.UniqueConstraint("company_id", "employee_code",
                            name="uq_membership_company_emp_code"),
    )

    def role_name(self):
        from shared.models.base import Role
        r = Role.query.get(self.role_id) if self.role_id else None
        return r.name if r else ""


class CompanyInvitation(db.Model):
    """An invite sent by a company admin to a REGISTERED user's email.
    The user accepts from their hub; acceptance is quota-checked."""
    __tablename__ = "company_invitations"

    SENT = "sent"
    ACCEPTED = "accepted"
    REVOKED = "revoked"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"),
                           nullable=False, index=True)
    email = db.Column(db.String(120), nullable=False)
    role_id = db.Column(db.Integer, db.ForeignKey("roles.id"), nullable=False)
    invited_by = db.Column(db.Integer, db.ForeignKey("users.id"),
                           nullable=True)
    status = db.Column(db.String(10), default=SENT, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    company = db.relationship("Company", backref="invitations")

    def role_name(self):
        from shared.models.base import Role
        r = Role.query.get(self.role_id) if self.role_id else None
        return r.name if r else ""


class GlobalLimits(db.Model):
    """Super-admin set global quota defaults; companies.max_members overrides
    the member limit per company."""
    __tablename__ = "global_limits"

    id = db.Column(db.Integer, primary_key=True)
    max_companies_per_user = db.Column(db.Integer, default=3, nullable=False)
    default_max_members = db.Column(db.Integer, default=25, nullable=False)
    updated_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    updated_at = db.Column(db.DateTime, default=datetime.utcnow,
                           onupdate=datetime.utcnow)

    @classmethod
    def get(cls):
        r = cls.query.first()
        if not r:
            r = cls()
            db.session.add(r)
            db.session.commit()
        return r

    def member_limit_for(self, company):
        return company.max_members or self.default_max_members

    # Per-user overrides beat the global default; NULL means "use the
    # default", which is why these read the attribute rather than trusting a
    # column value that is NULL on every row predating the migration.
    def company_limit_for(self, user):
        """How many companies this user may create."""
        return (getattr(user, "max_companies_owned", None)
                or self.max_companies_per_user)

    def join_limit_for(self, user):
        """How many companies this user may belong to. No global default
        exists for joining, so an unset override means unlimited."""
        return getattr(user, "max_companies_joined", None)


class RegistrationRequest(db.Model):
    """A visitor's request for platform access.

    Lives outside any company (global table). The super admin reviews pending
    requests and either creates the account (allow) or blocks the email. The
    request tracks whether it has been seen and its current status so the
    console can surface what needs attention."""
    __tablename__ = "registration_requests"

    PENDING = "pending"
    SEEN = "seen"
    APPROVED = "approved"
    BLOCKED = "blocked"
    DELETED = "deleted"

    id = db.Column(db.Integer, primary_key=True)
    full_name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(120), nullable=False, index=True)
    phone = db.Column(db.String(30), nullable=True, index=True)
    # Stored only until approval, then cleared. Never kept for blocked/deleted.
    password_hash = db.Column(db.String(255), nullable=False)
    company_name = db.Column(db.String(200), nullable=True)
    notes = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(15), default=PENDING, nullable=False, index=True)
    seen = db.Column(db.Boolean, default=False, nullable=False)
    reviewed_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    reviewed_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    reviewer = db.relationship("User", foreign_keys=[reviewed_by])

    @staticmethod
    def email_exists(email):
        """An active user or a non-blocked request already holds this email."""
        from shared.models.base import User
        return (User.query.filter_by(email=email.lower()).first() is not None
                or RegistrationRequest.query.filter(
                    RegistrationRequest.email == email.lower(),
                    RegistrationRequest.status.in_(
                        [RegistrationRequest.PENDING,
                         RegistrationRequest.SEEN,
                         RegistrationRequest.APPROVED])).first() is not None)

    @staticmethod
    def phone_exists(phone):
        """An active user or a pending/seen/approved request holds this phone."""
        if not phone:
            return False
        from shared.models.base import User
        user_has = User.query.filter_by(phone=phone).first() is not None
        req_has = RegistrationRequest.query.filter(
            RegistrationRequest.phone == phone,
            RegistrationRequest.status.in_(
                [RegistrationRequest.PENDING,
                 RegistrationRequest.SEEN,
                 RegistrationRequest.APPROVED])).first() is not None
        return user_has or req_has

    @staticmethod
    def pending_count():
        return RegistrationRequest.query.filter(
            RegistrationRequest.status.in_(
                [RegistrationRequest.PENDING,
                 RegistrationRequest.SEEN])).count()

    def set_password(self, password):
        from werkzeug.security import generate_password_hash
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        from werkzeug.security import check_password_hash
        return check_password_hash(self.password_hash, password)
