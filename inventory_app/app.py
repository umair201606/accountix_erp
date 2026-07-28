from flask import render_template, redirect, url_for, request
from flask_login import current_user
from shared.extensions import db


def register_inventory_blueprints(app):
    @app.context_processor
    def inject_inv_now():
        return {"now": __import__("datetime").datetime.utcnow()}

    @app.context_processor
    def inject_inv_settings():
        from shared.models.inventory_settings import InventorySettings
        s = InventorySettings.get()
        return {
            "purchase_flow": s.purchase_flow if s else "with_po",
            "sales_flow": s.sales_flow if s else "with_so",
        }
    from .routes.auth import inv_auth_bp
    from .routes.categories import inv_cat_bp
    from .routes.products import inv_prod_bp
    from .routes.stock import inv_stock_bp
    from .routes.vouchers import inv_vouchers_bp
    from .routes.reports import inv_reports_bp
    from .routes.units import inv_units_bp
    from .routes.transfers import inv_transfers_bp

    app.register_blueprint(inv_auth_bp)
    app.register_blueprint(inv_cat_bp)
    app.register_blueprint(inv_prod_bp)
    app.register_blueprint(inv_stock_bp)
    app.register_blueprint(inv_vouchers_bp)
    app.register_blueprint(inv_reports_bp)
    app.register_blueprint(inv_units_bp)
    app.register_blueprint(inv_transfers_bp)

    return app
