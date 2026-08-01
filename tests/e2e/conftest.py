import pytest
import subprocess
import tempfile
import time
import socket
import sys
from pathlib import Path

BASE_URL = "http://localhost:5000"
HR_PROJECT = Path(__file__).resolve().parent.parent.parent


@pytest.fixture(scope="session")
def flask_server():
    test_env = {**{k: v for k, v in __import__("os").environ.items()},
                "DATABASE_URL": "sqlite:///e2e_test.db",
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
                s.connect(("127.0.0.1", 5000))
                s.close()
                break
            except ConnectionRefusedError:
                s.close()
        else:
            proc.terminate()
            raise RuntimeError("Flask server did not start\n" + _server_log())
        yield
    finally:
        proc.terminate()
        proc.wait()
        log.close()


@pytest.fixture
def login_page(page, flask_server):
    page.goto(f"{BASE_URL}/auth/login")
    return page


@pytest.fixture
def admin_page(page, flask_server):
    page.goto(f"{BASE_URL}/auth/login")
    page.fill("#login", "admin@gmail.com")
    page.fill("#password", "admin123")
    page.click("button[type='submit']")
    page.wait_for_url("**/dashboard/**")
    return page


@pytest.fixture
def hr_user_page(page, flask_server):
    page.goto(f"{BASE_URL}/auth/login")
    page.fill("#login", "emp@solarkon.com")
    page.fill("#password", "emp123")
    page.click("button[type='submit']")
    page.wait_for_url("**/dashboard/**")
    return page


@pytest.fixture
def mobile_page(page, flask_server):
    page.set_viewport_size({"width": 375, "height": 812})
    return page


@pytest.fixture
def admin_mobile(browser, flask_server):
    ctx = browser.new_context(viewport={"width": 375, "height": 812})
    p = ctx.new_page()
    p.goto(f"{BASE_URL}/auth/login")
    p.fill("#login", "admin@gmail.com")
    p.fill("#password", "admin123")
    p.click("button[type='submit']")
    p.wait_for_url("**/dashboard/**")
    yield p
    ctx.close()
