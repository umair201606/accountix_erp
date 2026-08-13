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
    manager_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    designation = db.Column(db.String(100))
    department = db.Column(db.String(100))
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

    manager = db.relationship("User", remote_side=[id], backref="direct_reports")
    memberships = db.relationship("CompanyMembership", backref="user",
                                  lazy="dynamic", cascade="all, delete-orphan")

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

        ``manager_id`` lives on the global users table, so a bare
        ``User.query.filter_by(manager_id=...)`` would hand a manager the
        names of staff from every company that shares that manager_id. The
        membership join narrows it to this company. [] when no company is
        active.
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
            .filter(User.manager_id == self.id,
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
        flags = {
            "hr": self.has_hr_access,
            "inventory": self.has_inventory_access,
            "invoicing": self.has_invoicing_access,
            "finance": self.has_finance_access,
            "accounting": self.has_accounting_access,
            "fbr": self.has_fbr_access,
            "fixed_assets": self.has_fixed_assets_access,
            "executive": self.has_executive_access,
        }
        if not self._company_entitled(module_key):
            return False
        if self.is_admin():
            return True
        return bool(flags.get(module_key))

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
    return User.query.get(int(user_id))
