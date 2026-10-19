"""Multi-user tests: several users acting on the same records at the same
instant, against Postgres (production is Neon; SQLite serialises every write
and cannot show these races).

The app binds its database when ``app`` is first imported, so these tests
need a process of their own. ``tests/e2e/test_concurrency.py`` starts one
with ``ERP_CONCURRENCY=1``; to run them directly:

    ERP_CONCURRENCY=1 venv/Scripts/python -m pytest tests/concurrency -q

Database: ``TEST_DATABASE_URL`` if set, else a throwaway local Postgres
started with the ``pgserver`` package (pip install pgserver; dev only).
"""
import os

if os.environ.get("ERP_CONCURRENCY") != "1":
    # Collected alongside the other suites, this directory would bind the
    # shared app to Postgres for every module that imports it after us.
    collect_ignore_glob = ["test_*.py"]
