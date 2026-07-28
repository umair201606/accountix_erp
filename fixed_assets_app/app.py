from flask import render_template, redirect, url_for, request
from flask_login import current_user
from shared.extensions import db


def register_fixed_assets_blueprints(app):
    from .routes.auth import fa_auth_bp
    from .routes.assets import fa_assets_bp
    from .routes.categories import fa_categories_bp
    from .routes.reports import fa_reports_bp
    from .routes.depreciation import fa_dep_bp
    from .routes.transfers import fa_transfers_bp

    app.register_blueprint(fa_auth_bp)
    app.register_blueprint(fa_assets_bp)
    app.register_blueprint(fa_categories_bp)
    app.register_blueprint(fa_reports_bp)
    app.register_blueprint(fa_dep_bp)
    app.register_blueprint(fa_transfers_bp)

    return app
