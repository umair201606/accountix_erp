import os
import pytest
import subprocess
import tempfile
import time
import socket
import sys
from pathlib import Path

HR_PROJECT = Path(__file__).resolve().parent.parent.parent
# The suite runs on its own port, not the dev server's 5000. Sharing one port
# meant the two fought over it: whoever bound first won, and the loser's user
# was served the other's pages — either the suite silently exercising the dev
# database, or a developer reading test data out of a server whose templates
# are frozen (FLASK_ENV=testing turns debug, and so template reloading, off).
PORT = int(os.environ.get("E2E_PORT", "5050"))
BASE_URL = f"http://localhost:{PORT}"


def _assert_port_free():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", PORT))
    except OSError:
        raise RuntimeError(
            f"Port {PORT} is already in use by another process. The E2E server "
            f"must own this port, or every Playwright test silently runs "
            f"against the wrong database (the classic suite-flake). Stop the "
            f"process holding port {PORT} and re-run."
        )
    finally:
        s.close()


@pytest.fixture(scope="session")
def flask_server():
    _assert_port_free()
    # A fresh database file per run: the old fixed-name e2e_test.db accumulated
    # rows across sessions, so "empty state" assertions flaked on leftovers.
    db_file = tempfile.NamedTemporaryFile(prefix="e2e_db_", suffix=".db",
                                          delete=False)
    db_file.close()
    test_env = {**dict(os.environ),
                "DATABASE_URL": "sqlite:///" + db_file.name.replace("\\", "/"),
                "PORT": str(PORT),
                "FLASK_ENV": "testing"}
    # Server output goes to a file, never to an unread PIPE: Flask logs a line
    # per request, and once a pipe's buffer fills with nobody draining it the
    # server blocks on write() and stops serving mid-suite.
    log = tempfile.NamedTemporaryFile(prefix="e2e_server_", suffix=".log",
                                      delete=False)
    proc = subprocess.Popen(
        [sys.executable, "run_local.py"],
        cwd=str(HR_PROJECT),
        env=test_env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )

    def _server_log(limit=2000):
        log.flush()
        with open(log.name, errors="replace") as fh:
            return fh.read()[-limit:]

    try:
        for i in range(60):
            time.sleep(2)
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                s.connect(("127.0.0.1", PORT))
                s.close()
                break
            except OSError:
                s.close()
        else:
            proc.terminate()
            raise RuntimeError("Flask server did not start\n" + _server_log())
        if proc.poll() is not None:
            proc.terminate()
            raise RuntimeError("Flask server died during startup\n"
                               + _server_log())
        yield
    finally:
        proc.terminate()
        proc.wait()
        log.close()
        try:
            os.unlink(db_file.name)
        except OSError:
            pass


@pytest.fixture
def login_page(page, flask_server):
    page.goto(f"{BASE_URL}/auth/login")
    return page


def _enter_first_company(page):
    """Login lands on the company portal now (portal-first), so open the
    first company's books ('Open books' link) to reach the hub, which is
    where these fixtures used to land directly."""
    page.wait_for_url("**/portal/**")
    page.locator("a.enter").first.click()
    page.wait_for_url("**/dashboard/**")
    return page


@pytest.fixture
def admin_page(page, flask_server):
    """Super admin logs in through the super admin door, then enters the
    first company's books (the regular form rejects super admins)."""
    page.goto(f"{BASE_URL}/superadmin/login")
    page.fill("#login", "admin@gmail.com")
    page.fill("#password", "admin123")
    page.click("button[type='submit']")
    page.wait_for_url("**/superadmin/**")
    page.goto(f"{BASE_URL}/portal/")
    return _enter_first_company(page)


@pytest.fixture
def hr_user_page(page, flask_server):
    page.goto(f"{BASE_URL}/auth/login")
    page.fill("#login", "emp@solarkon.com")
    page.fill("#password", "emp123")
    page.click("button[type='submit']")
    return _enter_first_company(page)


@pytest.fixture
def mobile_page(page, flask_server):
    page.set_viewport_size({"width": 375, "height": 812})
    return page


@pytest.fixture
def admin_mobile(browser, flask_server):
    ctx = browser.new_context(viewport={"width": 375, "height": 812})
    p = ctx.new_page()
    p.goto(f"{BASE_URL}/superadmin/login")
    p.fill("#login", "admin@gmail.com")
    p.fill("#password", "admin123")
    p.click("button[type='submit']")
    p.wait_for_url("**/superadmin/**")
    p.goto(f"{BASE_URL}/portal/")
    _enter_first_company(p)
    yield p
    ctx.close()
