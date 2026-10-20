"""Multi-company (tenant) scoping engine — the single authority for tenancy.

Contract (all agents and code MUST follow this):

1. Tenant-scoped models declare a plain ``company_id`` Integer column
   (indexed, NO foreign key — cross-dialect ALTER-safe). Global models
   (companies, roles, permissions, users, company_memberships,
   company_invitations, global_limits) do NOT declare it.

2. ``set_current_company(cid)`` selects the active company for the current
   request. ``current_company_id()`` reads it (None outside a request).

3. A ``do_orm_execute`` session event automatically adds
   ``with_loader_criteria`` to every ORM statement touching a scoped table —
   plain queries, joins, aggregates, aliases, subqueries, relationship loads
   and lazy="dynamic" backrefs all pass through it.

4. FAIL CLOSED: if a scoped table is queried with no active company, the
   event raises NoActiveCompanyError instead of returning all tenants' rows.
   Deliberate global reads must wrap in ``with unscoped():`` — greppable and
   reviewed.

5. Primary-key lookups NEVER use ``Model.query.get(pk)`` or
   ``get_or_404(pk)`` on scoped models (identity-map hits bypass the event).
   Use ``scoped_get(model, pk)`` / ``scoped_get_404(model, pk)``.

6. Rows are stamped on INSERT: a before_flush event copies the active
   company onto every new scoped row that does not set ``company_id``
   explicitly. No code path may create a NULL-company row (it would be
   invisible to every tenant-scoped query); outside ``unscoped()`` blocks
   that raises instead of writing the row.

7. Bulk DML (``Query.update()``/``Query.delete()``) is tenant-filtered too:
   the hook adds ``company_id == cid`` to UPDATE/DELETE statements on
   scoped tables (loader criteria do not apply to them), and fails closed
   with no active company. Committed rows can never change tenants: a
   flush that reassigns ``company_id`` raises instead of writing.
"""

from contextlib import contextmanager

from flask import g, has_app_context
from sqlalchemy import event, inspect as sa_inspect
from sqlalchemy.sql.base import Executable
from sqlalchemy.sql.dml import Delete, Update
from sqlalchemy.sql.selectable import AliasedReturnsRows, Join, Select
from sqlalchemy.orm import with_loader_criteria

from shared.extensions import db

__all__ = [
    "NoActiveCompanyError",
    "set_current_company",
    "current_company_id",
    "unscoped",
    "scoped_get",
    "scoped_get_404",
    "get_member",
    "scoped_model_classes",
    "is_scoped_model",
]


class NoActiveCompanyError(Exception):
    """Raised when a tenant-scoped query runs with no active company."""


# ── Active-company context (Flask g, request-scoped) ────────────────────────

def set_current_company(company_id):
    """Select the active company for the current request/context."""
    if has_app_context():
        g.company_id = int(company_id) if company_id is not None else None


def current_company_id():
    """The active company id, or None when unset / outside a request."""
    if not has_app_context():
        return None
    return getattr(g, "company_id", None)


@contextmanager
def unscoped():
    """Explicit, audited opt-out from tenancy scoping for this block.

    Only for: super-admin global views, seeding/backfill, CLI work.
    Raises RuntimeError outside an app context.
    """
    if not has_app_context():
        raise RuntimeError("unscoped() requires an app context (Flask g)")
    g._tenancy_bypass = getattr(g, "_tenancy_bypass", 0) + 1
    try:
        yield
    finally:
        g._tenancy_bypass = getattr(g, "_tenancy_bypass", 0) - 1


def _bypassed():
    return has_app_context() and getattr(g, "_tenancy_bypass", 0) > 0


# ── Scoped-model registry ───────────────────────────────────────────────────

_scoped_registry_cache = None


# Tenancy machinery tables are GLOBAL even though some carry a company_id
# column (the membership's company reference). Queries on them are always
# written explicitly with company_id, so they must never be auto-scoped.
GLOBAL_TABLES = {"companies", "company_memberships", "company_invitations",
                 "global_limits",
                 # The audit trail stamps company_id itself (NULL for platform
                 # events like a super admin signing in) and every reader
                 # filters by company explicitly.
                 "audit_logs"}


def _build_scoped_registry():
    """table_name -> mapped class, for every mapper whose table has company_id
    (excluding the tenancy machinery tables)."""
    registry = {}
    for mapper in db.Model.registry.mappers:
        cls = mapper.class_
        table = mapper.local_table
        if table is None:
            continue
        if table.name in GLOBAL_TABLES:
            continue
        if hasattr(table, "c") and "company_id" in table.c:
            registry[table.name] = cls
    return registry


def scoped_model_classes():
    """Dict of {tablename: class} for all tenant-scoped models."""
    global _scoped_registry_cache
    if _scoped_registry_cache is None:
        _scoped_registry_cache = _build_scoped_registry()
    return _scoped_registry_cache


def is_scoped_model(cls):
    return hasattr(cls, "company_id")


def _reset_registry():
    """Test hook: drop the cached registry (models may be registered late)."""
    global _scoped_registry_cache
    _scoped_registry_cache = None


# ── Table collection from a statement ───────────────────────────────────────

def _collect_table_names(node, out):
    """Walk a FROM/selectable and collect table names into ``out``.

    Handles: Table, Join (left/right), Subquery/Select (recurses into its
    FROMs), AliasedClass (its mapper's local table). This is what lets the
    criteria cover the aggregate and joined queries the finance reports use
    (``db.session.query(func.sum(col)).join(...)``), where the scoped entity
    is not in execute_state.entities.
    """
    if node is None:
        return
    if isinstance(node, Join):
        _collect_table_names(node.left, out)
        _collect_table_names(node.right, out)
        return
    if isinstance(node, Select):
        for frm in node.get_final_froms():
            _collect_table_names(frm, out)
        return
    if isinstance(node, AliasedReturnsRows):
        # Alias / Subquery / CTE all wrap a selectable via ``.element`` —
        # recurse into it instead of recording the synthetic alias name
        # (this is what ``Query.count()`` and other aggregate wrappers
        # produce, e.g. ``FROM (SELECT ... FROM voucher_numbers) AS anon_1``).
        _collect_table_names(node.element, out)
        return
    if isinstance(node, Executable):
        return
    # Table and other FromClauses
    if hasattr(node, "name"):
        out.add(node.name)
        return
    if hasattr(node, "class_"):  # AliasedClass
        try:
            insp = sa_inspect(node)
            _collect_table_names(insp.mapper.local_table, out)
        except Exception:
            pass


def _statement_table_names(statement):
    names = set()
    try:
        for frm in statement.get_final_froms():
            _collect_table_names(frm, names)
    except Exception:
        pass
    return names


# ── The scoping event ───────────────────────────────────────────────────────

@event.listens_for(db.session, "do_orm_execute")
def _tenancy_hook(execute_state):
    """Attach tenant criteria to every ORM execution touching scoped tables.

    The criteria is a fresh ``Cls.company_id == cid`` expression built per
    execution (never a cached lambda), so the value can never go stale across
    requests — the do_orm_execute event fires inside the request, reading the
    live ``g.company_id``.
    """
    if _bypassed():
        return
    registry = scoped_model_classes()
    if not registry:
        return  # no scoped models registered yet — nothing to filter

    statement = execute_state.statement
    cid = current_company_id()

    # Bulk DML (Query.update()/delete()) carries no entities and exposes no
    # FROMs, so the table walk below sees nothing and with_loader_criteria
    # does not apply to it either — without this branch every bulk write ran
    # unscoped across all tenants (and fail-open with no company set).
    if isinstance(statement, (Update, Delete)):
        table = getattr(statement, "table", None)
        tname = getattr(table, "name", None)
        if tname in registry:
            if cid is None:
                raise NoActiveCompanyError(
                    "Tenant-scoped bulk write attempted with no active "
                    "company. Set a company with set_current_company() or "
                    "wrap the block in tenancy.unscoped() if the write is "
                    "deliberately global."
                )
            col = table.c.get("company_id", None)
            if col is not None:
                execute_state.statement = statement.where(col == cid)
        return

    names = _statement_table_names(statement)
    for ent in getattr(execute_state, "entities", ()) or ():
        cls = getattr(ent, "class_", ent)  # AliasedClass -> mapped class
        mapper = getattr(cls, "__mapper__", None)
        table = mapper.local_table if mapper is not None else None
        if table is not None and hasattr(table, "name"):
            names.add(table.name)

    scoped_touched = names.intersection(registry)
    if not scoped_touched:
        return

    if cid is None:
        raise NoActiveCompanyError(
            "Tenant-scoped query attempted with no active company. "
            "Set a company with set_current_company() or wrap the block "
            "in tenancy.unscoped() if the read is deliberately global."
        )

    for name in scoped_touched:
        cls = registry[name]
        statement = statement.options(
            with_loader_criteria(cls, cls.company_id == cid,
                                 include_aliases=True)
        )
    execute_state.statement = statement


# ── Insert stamping (no row may be created untagged) ─────────────────────────

@event.listens_for(db.session, "before_flush")
def _stamp_company_id(session, flush_context, instances):
    """Copy the active company onto new scoped rows lacking an explicit
    company_id. NULL rows would be invisible to every tenant-scoped query
    (NULL never matches a cid), so a flush that would create one fails
    closed instead — except inside ``unscoped()`` blocks, where the writer
    has opted into managing company_id by hand."""
    if _bypassed():
        return
    cid = current_company_id()
    registry = scoped_model_classes()
    for obj in session.new:
        cls = type(obj)
        if cls not in registry.values():
            continue
        if getattr(obj, "company_id", None) is not None:
            continue
        if cid is None:
            raise NoActiveCompanyError(
                f"Creating {cls.__name__} without company_id and no active "
                "company; set one with set_current_company() or wrap the "
                "block in tenancy.unscoped() if the row is deliberately "
                "global."
            )
        obj.company_id = cid
    # Rows must never change tenants: re-homing a row would expose one
    # company's data to another. Explicit moves are only possible inside
    # unscoped() blocks (which returned above).
    for obj in session.dirty:
        if type(obj) not in registry.values():
            continue
        try:
            hist = sa_inspect(obj).attrs.company_id.history
        except Exception:
            continue
        if hist.deleted and hist.added and hist.deleted[0] != hist.added[0]:
            raise ValueError(
                f"Cannot move {type(obj).__name__} id "
                f"{getattr(obj, 'id', '?')} from company {hist.deleted[0]} "
                f"to company {hist.added[0]}: rows never change tenants."
            )


# ── Safe PK lookups (identity-map bypasses the event, so NEVER use .get) ────

def scoped_get(model, pk, for_update=False):
    """PK lookup that is always tenant-filtered.

    For scoped models filters by (id, company_id). For global models
    (users, roles, ...) falls back to a plain PK lookup.

    Inside ``unscoped()`` this defers to the bypass like the query event does:
    seeding and backfill reach scoped models through shared posting paths
    (journals, costing, order write-back), and those must not fail closed in
    the one mode that has deliberately opted out.

    ``for_update=True`` locks the row until the request commits (SELECT ...
    FOR NO KEY UPDATE; a no-op on SQLite, which serialises writers anyway)
    and reloads it. Every route that changes a document's state — approve,
    unapprove, delete, pay, post — loads it this way: a second user acting on
    the same document at the same instant waits, then sees the first one's
    result ("already approved") instead of posting it a second time.
    """
    if is_scoped_model(model) and not _bypassed():
        cid = current_company_id()
        if cid is None:
            raise NoActiveCompanyError(
                f"scoped_get({model.__name__}, {pk}) with no active company")
        q = model.query.filter(model.id == pk, model.company_id == cid)
    else:
        if not for_update:
            return model.query.get(pk)
        q = model.query.filter(model.id == pk)
    if for_update:
        # FOR NO KEY UPDATE: excludes every other locker of the row, but not
        # the key-share lock a foreign-key insert takes on it. With plain FOR
        # UPDATE, two users who had each inserted a row pointing at it (a
        # return at its invoice) and then locked it waited on each other.
        q = q.with_for_update(key_share=True).populate_existing()
    return q.first()


def scoped_get_404(model, pk, for_update=False):
    """Like scoped_get, but 404s when missing (replaces get_or_404)."""
    from flask import abort
    obj = scoped_get(model, pk, for_update=for_update)
    if obj is None:
        abort(404)
    return obj


def get_member(uid):
    """A user visible inside the current company: an active member of it.
    Use for every user-id from the URL — replaces bare
    ``User.query.get(uid)`` in HR/team flows.

    A super admin used to be returned whether or not they were a member, so
    any company admin could open the platform account in HR's user editor
    and set its email and password. Now only their own id resolves for them.
    """
    from flask_login import current_user
    from shared.models.base import User
    from shared.models.company import CompanyMembership
    u = User.query.get(uid)
    if u is None:
        return None
    if getattr(u, "is_super_admin", False) and getattr(
            current_user, "id", None) == u.id:
        return u
    cid = current_company_id()
    if cid is None:
        return None
    m = CompanyMembership.query.filter_by(
        company_id=cid, user_id=uid, status=CompanyMembership.ACTIVE).first()
    return u if m else None


def login_change_refusal(user):
    """Why the active company may not change this person's login (email,
    password, whether it can sign in), or None when it may.

    A login is one account across every company it belongs to. A company
    admin resetting the password of someone who also works elsewhere could
    sign in as them and open those other companies — anyone can make a
    company from the portal and invite the person they want. So a company
    manages a login only when it reaches nothing else: not a super admin,
    not this company's owner, and no membership (active, invited or
    blocked) in another company.
    """
    from flask_login import current_user
    from shared.models.company import Company, CompanyMembership
    if user is None:
        return "That person is not a member of this company."
    name = user.full_name or user.email
    if getattr(user, "is_super_admin", False):
        return f"{name} is a platform administrator; that login is managed from the console."
    cid = current_company_id()
    if user.id == getattr(current_user, "id", None):
        return None
    if cid is not None:
        company = Company.query.get(cid)
        if company is not None and company.created_by == user.id:
            return (f"{name} owns this company; only they can change their "
                    f"own login.")
    elsewhere = (CompanyMembership.query
                 .filter(CompanyMembership.user_id == user.id,
                         CompanyMembership.company_id != cid,
                         CompanyMembership.status != CompanyMembership.REMOVED)
                 .first())
    if elsewhere is not None:
        return (f"{name} also uses this login in another company, so only "
                f"they can change it.")
    return None


def logins_managed_elsewhere(users):
    """Ids of ``users`` whose login this company may not change — the bulk
    form of ``login_change_refusal`` for list pages (one query)."""
    from flask_login import current_user
    from shared.models.company import Company, CompanyMembership
    ids = [u.id for u in users]
    if not ids:
        return set()
    cid = current_company_id()
    out = {u.id for u in users if getattr(u, "is_super_admin", False)}
    company = Company.query.get(cid) if cid is not None else None
    if company is not None and company.created_by:
        out.add(company.created_by)
    out |= {uid for (uid,) in db.session.query(CompanyMembership.user_id)
            .filter(CompanyMembership.user_id.in_(ids),
                    CompanyMembership.company_id != cid,
                    CompanyMembership.status != CompanyMembership.REMOVED)
            .distinct()}
    out.discard(getattr(current_user, "id", None))
    return out
