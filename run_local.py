import os
os.environ.setdefault("DATABASE_URL", "sqlite:///erp_dev.db")
os.environ.setdefault("SECRET_KEY", "local-dev-secret-key-2026")

from app import app

if __name__ == "__main__":
    # The E2E harness runs this script with its own DATABASE_URL (a fresh
    # sqlite file) and FLASK_ENV=testing. In that mode the werkzeug reloader
    # must stay off: it spawns a second process that races the first on the
    # same SQLite file, and the loser's company_id backfill dies with
    # "database is locked", leaving legacy rows invisible to tenancy scoping.
    testing = os.environ.get("FLASK_ENV") == "testing"
    app.run(port=5000, debug=not testing, use_reloader=not testing)
