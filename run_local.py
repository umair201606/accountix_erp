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
    # PORT lets the E2E harness run on its own port instead of competing with
    # a dev server on 5000 (see tests/e2e/conftest.py).
    port = int(os.environ.get("PORT", "5000"))
    app.run(port=port, debug=not testing, use_reloader=not testing)
