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
"""

from contextlib import contextmanager

from flask import g, has_app_context
from sqlalchemy import event, inspect as sa_inspect
from sqlalchemy.sql.base import Executable
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
                 "global_limits"}


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


# ── Safe PK lookups (identity-map bypasses the event, so NEVER use .get) ────

def scoped_get(model, pk):
    """PK lookup that is always tenant-filtered.

    For scoped models filters by (id, company_id). For global models
    (users, roles, ...) falls back to a plain PK lookup.

    Inside ``unscoped()`` this defers to the bypass like the query event does:
    seeding and backfill reach scoped models through shared posting paths
    (journals, costing, order write-back), and those must not fail closed in
    the one mode that has deliberately opted out.
    """
    if is_scoped_model(model) and not _bypassed():
        cid = current_company_id()
        if cid is None:
            raise NoActiveCompanyError(
                f"scoped_get({model.__name__}, {pk}) with no active company")
        return model.query.filter(model.id == pk,
                                  model.company_id == cid).first()
    return model.query.get(pk)


def scoped_get_404(model, pk):
    """Like scoped_get, but 404s when missing (replaces get_or_404)."""
    from flask import abort
    obj = scoped_get(model, pk)
    if obj is None:
        abort(404)
    return obj


def get_member(uid):
    """A user visible inside the current company: an active member of it
    (or a super admin). Use for every user-id from the URL — replaces bare
    ``User.query.get(uid)`` in HR/team flows.
    """
    from shared.models.base import User
    from shared.models.company import CompanyMembership
    u = User.query.get(uid)
    if u is None:
        return None
    if getattr(u, "is_super_admin", False):
        return u
    cid = current_company_id()
    if cid is None:
        return None
    m = CompanyMembership.query.filter_by(
        company_id=cid, user_id=uid, status=CompanyMembership.ACTIVE).first()
    return u if m else None
