def register_finance_blueprints(app):
    # Finance settings moved to the unified settings module (shared/routes/settings.py).
    from .routes.reports import finance_bp
    from .routes.accounting import acct_bp
    from .routes import bank_rec  # noqa: F401  (attaches its routes to acct_bp)
    from .routes.coa import coa_bp
    app.register_blueprint(finance_bp)
    app.register_blueprint(acct_bp)
    app.register_blueprint(coa_bp)
    return app
