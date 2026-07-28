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

    One row per (user, resource/section). A user with NO row for a resource
    keeps full access (backward compatible); once admin saves that user's
    rights, every section is stored explicitly and the flags govern.
    """
    __tablename__ = "user_permissions"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    resource = db.Column(db.String(100), nullable=False)
    can_view = db.Column(db.Boolean, default=True)
    can_create = db.Column(db.Boolean, default=False)
    can_edit = db.Column(db.Boolean, default=False)
    can_approve = db.Column(db.Boolean, default=False)
    can_delete = db.Column(db.Boolean, default=False)

    __table_args__ = (db.UniqueConstraint("user_id", "resource", name="uq_user_perm"),)


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
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    manager = db.relationship("User", remote_side=[id], backref="direct_reports")

    @classmethod
    def employees(cls):
        """Query for users visible inside the HR module.

        Admin accounts are regulators, not employees — they must never appear
        in HR lists, payroll, attendance or team views. Admin is managed only
        from the ERP hub Settings.
        """
        admin_role = Role.query.filter_by(name=Role.ADMIN).first()
        q = cls.query
        if admin_role:
            q = q.filter(cls.role_id != admin_role.id)
        return q

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

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
        """Whether this user may open a module. Admin sees everything."""
        if self.is_admin():
            return True
        flags = {
            "hr": self.has_hr_access,
            "inventory": self.has_inventory_access,
            "invoicing": self.has_invoicing_access,
            "finance": self.has_finance_access,
            "accounting": self.has_accounting_access,
            "fbr": self.has_fbr_access,
            "fixed_assets": self.has_fixed_assets_access,
        }
        return bool(flags.get(module_key))

    def can(self, resource, action="view"):
        """Per-user section rights (view/create/edit/approve/delete).

        No UserPermission row for the resource means unrestricted — admin
        restricts users explicitly via the Settings module.
        """
        if self.is_admin():
            return True
        perm = UserPermission.query.filter_by(user_id=self.id, resource=resource).first()
        if perm is None:
            return True
        return bool(getattr(perm, f"can_{action}", False))

    def is_admin(self):
        return self.role_obj and self.role_obj.name == Role.ADMIN

    def is_manager(self):
        return self.role_obj and self.role_obj.name == Role.MANAGER

    def is_employee(self):
        return self.role_obj and self.role_obj.name == Role.EMPLOYEE

    def get_role_name(self):
        return self.role_obj.name if self.role_obj else ""


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))
