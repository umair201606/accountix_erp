def register_fbr_blueprints(app):
    from .routes.settings import fbr_settings_bp
    from .routes.dashboard import fbr_dashboard_bp
    app.register_blueprint(fbr_settings_bp)
    app.register_blueprint(fbr_dashboard_bp)
    return app
