"""Leave balances and decisions — one path for the leave page and MSS.

Balances are derived from the requests themselves: ``used`` is the approved
days, ``pending`` the days still awaiting a decision, ``remaining`` what is
left of ``total``. They used to be nudged with += / -= in two copies of the
approve code, so approving the same request twice (double click, back
button) counted its days twice, rejecting an approved one subtracted days
that were no longer pending, and ``remaining`` (the figure the leave page
leads with) was only ever set by the seed.

A request belongs to the quota of its start date's year (unchanged).
"""
from sqlalchemy import extract, func

from .extensions import db
from .models.leave import LeaveApproval, LeaveQuota, LeaveRequest

OPEN = ("pending", "approved")


def quota_for(user_id, leave_type, year):
    """The user's quota for this type and year, created from the type's
    default when it does not exist yet. Only the seed used to create
    quotas, so anyone who joined later had none — and with no quota the
    balance check was skipped: unlimited leave."""
    q = LeaveQuota.query.filter_by(user_id=user_id, leave_type_id=leave_type.id,
                                   year=year).first()
    if q is None:
        q = LeaveQuota(user_id=user_id, leave_type_id=leave_type.id, year=year,
                       total=leave_type.default_quota or 0, used=0, pending=0,
                       remaining=leave_type.default_quota or 0)
        db.session.add(q)
        db.session.flush()
    return q


def refresh(quota):
    """Recompute used / pending / remaining from the quota's requests."""
    def days(status):
        return float(db.session.query(func.coalesce(func.sum(LeaveRequest.total_days), 0))
                     .filter(LeaveRequest.user_id == quota.user_id,
                             LeaveRequest.leave_type_id == quota.leave_type_id,
                             LeaveRequest.status == status,
                             extract("year", LeaveRequest.start_date) == quota.year)
                     .scalar() or 0)
    db.session.flush()
    quota.used = days("approved")
    quota.pending = days("pending")
    quota.remaining = float(quota.total or 0) - quota.used - quota.pending
    return quota


def refresh_for(lr):
    q = LeaveQuota.query.filter_by(user_id=lr.user_id, leave_type_id=lr.leave_type_id,
                                   year=lr.start_date.year).first()
    return refresh(q) if q is not None else None


def overlapping(user_id, start, end):
    """An open (pending or approved) request of this user covering any of
    these dates."""
    return LeaveRequest.query.filter(
        LeaveRequest.user_id == user_id, LeaveRequest.status.in_(OPEN),
        LeaveRequest.start_date <= end, LeaveRequest.end_date >= start).first()


def approver_for(user, right=None):
    """Who decides this user's leave (or timesheet) inside the ACTIVE company.

    ``user.manager_id`` is this company's choice (kept on the membership);
    a manager who is not an active member here — or no manager at all —
    leaves it to a company admin. A request with no approval row was invisible to every
    approver and stayed pending for ever. None = nobody else can decide
    (the applicant is the company's only admin).

    The manager must hold the manager or admin role HERE: someone who is a
    plain employee in this company could not open the approvals page, so
    the request waited on them for ever, and a manager_id written by
    another company decided this company's requests. ``right`` — a
    (section, action) such as ("leaves", "approve") — also skips a manager
    an admin has taken that right from."""
    from shared.models.base import Role, User
    from shared.models.company import CompanyMembership
    from shared.tenancy import current_company_id
    cid = current_company_id()
    if cid is None:
        return None
    active = CompanyMembership.query.filter_by(company_id=cid,
                                               status=CompanyMembership.ACTIVE)
    lead_roles = [r.id for r in Role.query.filter(
        Role.name.in_((Role.ADMIN, Role.MANAGER))).all()]
    if user.manager_id and user.manager_id != user.id and \
            active.filter(CompanyMembership.user_id == user.manager_id,
                          CompanyMembership.role_id.in_(lead_roles)).first():
        manager = User.query.get(user.manager_id)
        if right is None or (manager is not None and manager.can(*right)):
            return user.manager_id
    admin = Role.query.filter_by(name=Role.ADMIN).first()
    if admin is None:
        return None
    m = (active.filter(CompanyMembership.role_id == admin.id,
                       CompanyMembership.user_id != user.id)
         .order_by(CompanyMembership.id).first())
    return m.user_id if m else None


def decide(approval, action, comment, user_id):
    """Record one approver's decision. Returns an error message, or None.

    Only a pending approval on a pending request can be decided, by its own
    approver; a rejection needs a reason. The request is approved when all
    its approvals are."""
    if approval.approver_id != user_id:
        return "You are not the approver of this request."
    if action not in ("approve", "reject"):
        return "Unknown action."
    lr = approval.leave_request
    # One decision at a time per employee: two approvers (or two clicks)
    # deciding at once both found it pending, and each rewrote the balance
    # from what it could see. The request is re-read after the wait.
    from shared.concurrency import serialize
    serialize("leave", lr.user_id)
    from shared.extensions import db as _db
    _db.session.refresh(approval)
    _db.session.refresh(lr)
    if approval.status != "pending" or lr.status != "pending":
        return "This request has already been decided."
    comment = (comment or "").strip()
    if action == "reject" and not comment:
        return "Please give a reason for the rejection."
    approval.status = "approved" if action == "approve" else "rejected"
    approval.comment = comment
    if action == "reject":
        lr.status = "rejected"
    elif all(a.status == "approved" for a in lr.approvals):
        lr.status = "approved"
    refresh_for(lr)
    return None
