from datetime import datetime, date
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from shared.extensions import db, login_manager


class Role(db.Model):
    __tablename__ = "roles"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), unique=True, nullable=False)
    description = db.Column(db.String(200))
    users = db.relationship("User", backref="role_obj", lazy="dynamic")

    ADMIN = "admin"
    MANAGER = "manager"
    EMPLOYEE = "employee"

    @staticmethod
    def seed():
        for r in [Role.ADMIN, Role.MANAGER, Role.EMPLOYEE]:
            if not Role.query.filter_by(name=r).first():
                db.session.add(Role(name=r, description=f"{r.title()} role"))


class Permission(db.Model):
    __tablename__ = "permissions"
    id = db.Column(db.Integer, primary_key=True)
    role_id = db.Column(db.Integer, db.ForeignKey("roles.id"), nullable=False)
    resource = db.Column(db.String(100), nullable=False)
    can_read = db.Column(db.Boolean, default=False)
    can_write = db.Column(db.Boolean, default=False)
    can_delete = db.Column(db.Boolean, default=False)


class UserPermission(db.Model):
    """Per-user fine-grained rights, managed by admin in the Settings module.

    One row per (company, user, resource/section). A user with NO row for a
    resource keeps full access (backward compatible); once admin saves that
    user's rights, every section is stored explicitly and the flags govern.
    """
    __tablename__ = "user_permissions"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)  # tenant-scoped
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    resource = db.Column(db.String(100), nullable=False)
    can_view = db.Column(db.Boolean, default=True)
    can_create = db.Column(db.Boolean, default=False)
    can_edit = db.Column(db.Boolean, default=False)
    can_approve = db.Column(db.Boolean, default=False)
    can_delete = db.Column(db.Boolean, default=False)

    __table_args__ = (db.UniqueConstraint("company_id", "user_id", "resource",
                                          name="uq_user_perm_company"),)


class User(UserMixin, db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    employee_code = db.Column(db.String(20), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    # Login identifier — distinct from email, but may hold the same value.
    # Users sign in with this; defaults to the email at creation.
    login_id = db.Column(db.String(120), unique=True)
    password_hash = db.Column(db.String(256), nullable=False)
    full_name = db.Column(db.String(100), nullable=False)
    role_id = db.Column(db.Integer, db.ForeignKey("roles.id"), nullable=False)
    # Account-wide copies of the employment details. Each company keeps its
    # own on the membership (CompanyMembership.manager_id / designation /
    # department); ``manager_id``, ``designation`` and ``department`` below
    # read and write the ACTIVE company's, and fall back to these only
    # outside a company or for someone not a member of it.
    home_manager_id = db.Column("manager_id", db.Integer, db.ForeignKey("users.id"),
                                nullable=True)
    home_designation = db.Column("designation", db.String(100))
    home_department = db.Column("department", db.String(100))
    date_of_birth = db.Column(db.Date)
    date_of_joining = db.Column(db.Date, default=date.today)
    phone = db.Column(db.String(20))
    cnic = db.Column(db.String(20))
    bank_name = db.Column(db.String(100))
    bank_account_title = db.Column(db.String(100))
    bank_account_number = db.Column(db.String(50))
    emergency_contact = db.Column(db.String(100))
    emergency_phone = db.Column(db.String(20))
    address = db.Column(db.Text)
    profile_image = db.Column(db.String(255))
    is_active = db.Column(db.Boolean, default=True)
    has_hr_access = db.Column(db.Boolean, default=False)
    has_inventory_access = db.Column(db.Boolean, default=False)
    has_invoicing_access = db.Column(db.Boolean, default=False)
    has_finance_access = db.Column(db.Boolean, default=False)
    has_accounting_access = db.Column(db.Boolean, default=False)
    has_fbr_access = db.Column(db.Boolean, default=False)
    has_fixed_assets_access = db.Column(db.Boolean, default=False)
    has_executive_access = db.Column(db.Boolean, default=False)
    is_super_admin = db.Column(db.Boolean, default=False)
    # Per-user company quotas set by the super admin. NULL = fall back to
    # GlobalLimits (see company_limit_for / join_limit_for).
    max_companies_owned = db.Column(db.Integer, nullable=True)
    max_companies_joined = db.Column(db.Integer, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    memberships = db.relationship("CompanyMembership", backref="user",
                                  lazy="dynamic", cascade="all, delete-orphan")

    # ── Employment in the active company ────────────────────────────────

    def _employment(self, company_id=None):
        """This person's membership row in the company (any status: a
        leaver's payslip still shows the designation they left with), or
        None outside a company / for a non-member. A found row is cached on
        the instance (lists read three fields per person); a miss is not, so
        a membership added later in the same session is picked up."""
        from shared.tenancy import current_company_id
        from shared.models.company import CompanyMembership
        cid = company_id if company_id is not None else current_company_id()
        if cid is None or self.id is None:
            return None
        cache = self.__dict__.setdefault("_employment_cache", {})
        m = cache.get(cid)
        if m is None:
            m = (CompanyMembership.query
                 .filter_by(company_id=cid, user_id=self.id).first())
            if m is not None:
                cache[cid] = m
        return m

    def _only_company(self, company_id):
        """True when the person belongs to no other company (removed
        memberships aside) — then the account-wide copy follows this one."""
        from shared.models.company import CompanyMembership
        return CompanyMembership.query.filter(
            CompanyMembership.user_id == self.id,
            CompanyMembership.company_id != company_id,
            CompanyMembership.status != CompanyMembership.REMOVED).first() is None

    def _get_employment(self, field):
        m = self._employment()
        if m is not None:
            return getattr(m, field)
        return getattr(self, "home_" + field)

    def _set_employment(self, field, value):
        m = self._employment()
        if m is not None:
            setattr(m, field, value)
            if not self._only_company(m.company_id):
                return
        setattr(self, "home_" + field, value)

    @property
    def manager_id(self):
        """Who this person reports to in the active company."""
        return self._get_employment("manager_id")

    @manager_id.setter
    def manager_id(self, value):
        self._set_employment("manager_id", value)

    @property
    def designation(self):
        return self._get_employment("designation")

    @designation.setter
    def designation(self, value):
        self._set_employment("designation", value)

    @property
    def department(self):
        return self._get_employment("department")

    @department.setter
    def department(self, value):
        self._set_employment("department", value)

    @property
    def manager(self):
        mid = self.manager_id
        return User.query.get(mid) if mid else None

    def employment_in(self, company_id):
        """(manager_id, designation, department) in a given company."""
        m = self._employment(company_id)
        if m is not None:
            return m.manager_id, m.designation, m.department
        return self.home_manager_id, self.home_designation, self.home_department

    # ── Membership helpers (multi-company) ───────────────────────────────

    def membership_in(self, company_id, statuses=("active",)):
        """This user's membership row in a company (active by default)."""
        from shared.models.company import CompanyMembership
        return CompanyMembership.query.filter_by(
            company_id=company_id, user_id=self.id).filter(
            CompanyMembership.status.in_(statuses)).first()

    def role_in(self, company_id):
        """Role name ('admin'/'manager'/'employee') in a company, or ''."""
        m = self.membership_in(company_id)
        if m:
            return m.role_name()
        # Legacy fallback: pre-multi-company role column.
        return self.get_role_name()

    def active_companies(self):
        """Companies this user is an active member of."""
        from shared.models.company import CompanyMembership
        from shared.models.company import Company
        return (Company.query
                .join(CompanyMembership,
                      CompanyMembership.company_id == Company.id)
                .filter(CompanyMembership.user_id == self.id,
                        CompanyMembership.status == "active",
                        Company.is_active == True).all())  # noqa: E712

    def owns_company_count(self):
        """Companies this user created (the 'companies you own' quota)."""
        from shared.models.company import Company
        return Company.query.filter_by(created_by=self.id).count()

    def company_role_id(self, company_id):
        m = self.membership_in(company_id)
        return m.role_id if m else self.role_id

    @property
    def active_company_id(self):
        """The current request's active company (session/g-driven)."""
        from shared.tenancy import current_company_id
        return current_company_id()

    def is_company_admin(self):
        return self.role_in(self.active_company_id) == Role.ADMIN

    def is_company_manager(self):
        return self.role_in(self.active_company_id) == Role.MANAGER

    @classmethod
    def employees(cls):
        """Query for users visible inside the HR module of the ACTIVE company.

        Admin accounts are regulators, not employees — they must never appear
        in HR lists, payroll, attendance or team views. Admin is managed only
        from the ERP hub Settings. Scoped to active members of the current
        company (multi-company).
        """
        from shared.tenancy import current_company_id
        from shared.models.company import CompanyMembership
        cid = current_company_id()
        admin_role = Role.query.filter_by(name=Role.ADMIN).first()
        q = cls.query.join(CompanyMembership, db.and_(
            CompanyMembership.user_id == cls.id,
            CompanyMembership.company_id == cid,
            CompanyMembership.status == "active"))
        if admin_role:
            q = q.filter(CompanyMembership.role_id != admin_role.id)
        return q

    def reports_of(self):
        """Active members of the ACTIVE company who report to this user.

        Who reports to whom is kept per company on the membership, so this
        reads the active company's rows only. [] when no company is active.
        """
        from shared.tenancy import current_company_id
        from shared.models.company import CompanyMembership
        cid = current_company_id()
        if cid is None:
            return []
        return (User.query.join(CompanyMembership, db.and_(
            CompanyMembership.user_id == User.id,
            CompanyMembership.company_id == cid,
            CompanyMembership.status == CompanyMembership.ACTIVE))
            .filter(CompanyMembership.manager_id == self.id,
                    User.is_active == True).all())  # noqa: E712

    @property
    def company_role_name(self):
        """Role name inside the active company ('' when none). Display
        helper for lists where the membership role and the legacy global
        role disagree."""
        from shared.tenancy import current_company_id
        return self.role_in(current_company_id())

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)
        # The session carries a fingerprint of the hash (get_id): the person
        # changing their own password stays signed in here, every other
        # session of this login is signed out.
        from shared.account_security import refresh_own_session
        refresh_own_session(self)

    def get_id(self):
        """Session identity: the id plus a fingerprint of the password hash,
        so a new password ends every other session of this login."""
        from shared.account_security import session_fingerprint
        return f"{self.id}:{session_fingerprint(self)}"

    def set_password_prehashed(self, password_hash):
        """Store an already-hashed password (e.g. migrating a request to a user)."""
        self.password_hash = password_hash

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def has_permission(self, resource, action="read"):
        perm = Permission.query.filter_by(role_id=self.role_id, resource=resource).first()
        if not perm:
            return False
        if action == "read":
            return perm.can_read
        if action == "write":
            return perm.can_write
        if action == "delete":
            return perm.can_delete
        return False

    def module_access(self, module_key):
        """Whether this user may open a module in the ACTIVE company.

        Effective access is company entitlement AND the user's own flag. A
        company admin bypasses the user flag — that is what being admin means
        — but never the entitlement: a module the company is not entitled to
        is off for everyone in it, admin included.

        Outside a company (portal, super admin console) there is no
        entitlement to apply, so the user flag alone decides.
        """
        if not self._company_entitled(module_key):
            return False
        if self.is_admin():
            return True
        return self.module_flag(module_key)

    MODULE_FLAG_ATTRS = {
        "hr": "has_hr_access",
        "inventory": "has_inventory_access",
        "invoicing": "has_invoicing_access",
        "finance": "has_finance_access",
        "accounting": "has_accounting_access",
        "fbr": "has_fbr_access",
        "fixed_assets": "has_fixed_assets_access",
        "executive": "has_executive_access",
    }

    def module_flag(self, module_key, company_id=None):
        """The person's own switch for a module (no admin bypass, no
        entitlement): what the company set on their membership, else the
        account-wide default on this row."""
        from shared.tenancy import current_company_id
        cid = company_id if company_id is not None else current_company_id()
        if cid is not None:
            m = self.membership_in(cid)
            value = m.module_flag(module_key) if m is not None else None
            if value is not None:
                return value
        attr = self.MODULE_FLAG_ATTRS.get(module_key)
        return bool(getattr(self, attr, False)) if attr else False

    @staticmethod
    def _company_entitled(module_key):
        from shared.models.company import Company
        from shared.tenancy import current_company_id
        cid = current_company_id()
        if cid is None:
            return True
        company = Company.query.get(cid)
        return company.module_enabled(module_key) if company else True

    def can(self, resource, action="view"):
        """Per-user section rights (view/create/edit/approve/delete),
        scoped to the active company.

        No UserPermission row for the resource means unrestricted — admin
        restricts users explicitly via the Settings module.
        """
        if self.is_admin():
            return True
        from shared.tenancy import current_company_id
        q = UserPermission.query.filter_by(user_id=self.id,
                                           resource=resource)
        cid = current_company_id()
        if cid is not None:
            q = q.filter_by(company_id=cid)
        perm = q.first()
        if perm is None:
            return True
        return bool(getattr(perm, f"can_{action}", False))

    def is_admin(self):
        """Admin of the ACTIVE company, or a super admin, or (legacy) global
        admin role when no company context exists yet."""
        if self.is_super_admin:
            return True
        cid = self.active_company_id
        if cid is not None:
            return self.role_in(cid) == Role.ADMIN
        return self.role_obj and self.role_obj.name == Role.ADMIN

    def is_manager(self):
        if self.is_super_admin:
            return True
        cid = self.active_company_id
        if cid is not None:
            return self.role_in(cid) == Role.MANAGER
        return self.role_obj and self.role_obj.name == Role.MANAGER

    def is_employee(self):
        cid = self.active_company_id
        if cid is not None:
            return self.role_in(cid) == Role.EMPLOYEE
        return self.role_obj and self.role_obj.name == Role.EMPLOYEE

    def get_role_name(self):
        return self.role_obj.name if self.role_obj else ""


@login_manager.user_loader
def load_user(user_id):
    """The signed-in user of a session, or None to sign it out.

    A deactivated login is signed out on its next request (it used to keep
    working until the browser closed), and so is a session minted before the
    password last changed. A bare id (no fingerprint) is a session from
    before fingerprints existed and is still accepted.
    """
    raw, _sep, mark = str(user_id).partition(":")
    try:
        user = User.query.get(int(raw))
    except (TypeError, ValueError):
        return None
    if user is None or not user.is_active:
        return None
    if mark:
        import hmac
        from shared.account_security import session_fingerprint
        if not hmac.compare_digest(mark, session_fingerprint(user)):
            return None
    return user
