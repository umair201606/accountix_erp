def register_invoicing_blueprints(app):
    # Invoicing/inventory settings moved to the unified settings module
    # (shared/routes/settings.py).
    from .routes.dashboard import invoicing_bp
    from .routes.suppliers import inv_sup_bp
    from .routes.customers import inv_cust_bp
    from .routes.purchases import inv_pur_bp
    from .routes.sales import inv_sale_bp
    from .routes.invoices import inv_inv_bp
    from .routes.purchase_invoice import inv_pinv_bp
    from .routes.purchase_return import inv_preturn_bp
    from .routes.sales_return import inv_sreturn_bp
    from .routes.tracking import inv_track_bp

    app.register_blueprint(invoicing_bp)
    app.register_blueprint(inv_sup_bp)
    app.register_blueprint(inv_cust_bp)
    app.register_blueprint(inv_pur_bp)
    app.register_blueprint(inv_sale_bp)
    app.register_blueprint(inv_inv_bp)
    app.register_blueprint(inv_pinv_bp)
    app.register_blueprint(inv_preturn_bp)
    app.register_blueprint(inv_sreturn_bp)
    app.register_blueprint(inv_track_bp)
    return app
