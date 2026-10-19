"""Alembic environment. Always handed a live connection by shared/db_migrate.py
(the app's own engine), so there is no offline mode and no URL here."""
from alembic import context
from sqlalchemy import text

from shared.db_migrate import ADVISORY_LOCK_KEY

connection = context.config.attributes.get("connection")
if connection is None:
    raise RuntimeError("Run migrations through shared/db_migrate.py "
                       "or tools/db_migrate.py, not the alembic CLI.")

context.configure(connection=connection, target_metadata=None,
                  transaction_per_migration=False)

with context.begin_transaction():
    if connection.dialect.name == "postgresql":
        # Migrations wait as long as they must: an instance queued behind the
        # one migrating, or an ALTER waiting for requests in flight, must not
        # give up after the app's per-request lock_timeout
        # (shared/concurrency.py: install_lock_timeout).
        connection.execute(text("SET LOCAL lock_timeout = 0"))
        # Serialise concurrent cold starts; released at COMMIT/ROLLBACK.
        connection.execute(text("SELECT pg_advisory_xact_lock(:k)"),
                           {"k": ADVISORY_LOCK_KEY})
    context.run_migrations()
