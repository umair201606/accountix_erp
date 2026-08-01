"""Company module entitlement, enforced at the door of every module app.

Entitlement used to be presentation only: the hub hid the tile and the
sidebar dropped the links, but the routes themselves still answered to
anyone who knew the URL, so a company that had not bought Finance could
open /finance/ by typing it. Only Fixed Assets refused, because its routes
each call ``module_access`` by hand. This closes the rest in one place.

Which blueprints belong to which module is recorded as each module app
registers them, rather than listed by hand here: a blueprint added to a
module later is guarded without anyone remembering to come back and add it.
"""
from flask import render_template, request
from flask_login import current_user

# blueprint name -> module key, and bare (non-blueprint) endpoint -> module
# key, both filled in by register_module().
_BP_MODULE = {}
_EP_MODULE = {}

# Endpoints that sit inside a module app but are not the module. Signing in
# and out, the account pages, and the notification bell and avatar that
# every module's chrome calls. These live in HR's auth/ess blueprints, so
# gating them would lock a user out of the whole app — including the logout
# button — because their company dropped HR.
ALWAYS_OPEN = {
    "auth.login",
    "auth.logout",
    "auth.change_password",
    "auth.profile",
    "auth.account_settings",
    "auth.api_notifications",
    "auth.mark_all_read",
    "auth.mark_notification_read",
    "ess.avatar",
}


def register_module(app, module_key, register_fn, overrides=None):
    """Run a module app's blueprint registration, recording what it added.

    ``overrides`` maps a blueprint name to a different module key, for an
    app that registers blueprints belonging to more than one module —
    finance_app also carries Accounting and the Chart of Accounts.
    """
    overrides = overrides or {}
    before_bp = set(app.blueprints)
    before_ep = set(app.view_functions)

    register_fn(app)

    for name in set(app.blueprints) - before_bp:
        _BP_MODULE[name] = overrides.get(name, module_key)
    for endpoint in set(app.view_functions) - before_ep:
        # A bare endpoint is a route the module app hung directly on the
        # app rather than on a blueprint — HR's /dashboard is one.
        if "." not in endpoint:
            _EP_MODULE[endpoint] = overrides.get(endpoint, module_key)


def module_for_request():
    """The module key this request belongs to, or None if it belongs to
    none (the hub, the portal, settings, the super admin console)."""
    endpoint = request.endpoint
    if endpoint is None or endpoint in ALWAYS_OPEN:
        return None
    if request.blueprint:
        return _BP_MODULE.get(request.blueprint)
    return _EP_MODULE.get(endpoint)


def install_module_guard(app):
    """Refuse any module the active company is not entitled to.

    Must be installed AFTER the before_request that sets the active
    company, since entitlement is a property of that company.
    """
    @app.before_request
    def _enforce_module_entitlement():
        if not current_user.is_authenticated:
            return None
        module_key = module_for_request()
        if module_key is None:
            return None
        if current_user.module_access(module_key):
            return None
        return render_template("access_denied.html"), 403
