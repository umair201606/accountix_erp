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

    @classmethod
    def by_slug(cls, slug):
        return cls.query.filter_by(slug=slug).first()


class CompanyMembership(db.Model):
    """A user's role inside one company. Role is per company, NOT global —
    the same user can be admin in A and employee in B."""
    __tablename__ = "company_memberships"

    PENDING = "pending"
    ACTIVE = "active"
    REMOVED = "removed"

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
