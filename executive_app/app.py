def register_executive_blueprints(app):
    from .routes.reports import exec_bp

    app.register_blueprint(exec_bp)
    return app
