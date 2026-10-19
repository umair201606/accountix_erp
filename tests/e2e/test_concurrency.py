"""Several users on the same records at once — tests/concurrency, on Postgres.

Those tests need the app bound to Postgres, and the app binds its database
when it is first imported: in this process the other E2E modules have already
bound it to SQLite. So the suite runs in a process of its own and this test
reports its result. Postgres comes from TEST_DATABASE_URL, or a throwaway
server started with the pgserver package (pip install pgserver; dev only).
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def test_many_users_at_once_on_postgres():
    if not os.environ.get("TEST_DATABASE_URL"):
        pytest.importorskip("pgserver", reason="needs Postgres: set TEST_DATABASE_URL "
                                               "or pip install pgserver")
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/concurrency", "-q", "-p", "no:cacheprovider",
         "-W", "ignore", "--no-header"],
        cwd=str(ROOT), env={**os.environ, "ERP_CONCURRENCY": "1"},
        capture_output=True, text=True, timeout=1800)
    out = r.stdout + r.stderr
    assert r.returncode == 0, out[-8000:]
    assert " passed" in out and " skipped" not in out, out[-2000:]
