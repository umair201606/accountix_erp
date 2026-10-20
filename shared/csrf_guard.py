"""Refuse writes that another website started.

Form tokens are switched off (``WTF_CSRF_ENABLED = False``) and every module
posts plain forms and fetch() calls, so a page on any other site could submit
a form to this app and the user's browser would send their session cookie
with it: approve an invoice, delete a voucher, reset a password. The cookies
now carry SameSite=Lax (shared/config.py), and this guard is the second
lock: a browser names the page a request came from in ``Origin`` (every
cross-site POST and fetch) or ``Referer``, and a write naming another site
is refused before any view runs.

A request with neither header is let through — browsers send at least one
on cross-site writes, while curl, the Flask test client and server-to-server
callers send none. ``Origin: null`` (sandboxed frames, some redirects,
file:// pages) is refused: it can never be this app's own page.

Behind Vercel the Host header is the domain the user typed; the forwarded
host is accepted as well, and ``TRUSTED_ORIGINS`` (comma-separated, e.g.
``https://erp.example.com``) adds any other domain the app is served under.
"""
import os
from urllib.parse import urlsplit

from flask import jsonify, request

UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_DEFAULT_PORTS = {"http": "80", "https": "443"}

REFUSAL = ("This request came from another website, so it was refused. "
           "Open the page in this app and try again.")


def _host_key(netloc, scheme):
    """``host[:port]`` lower-cased, the scheme's default port dropped."""
    netloc = (netloc or "").strip().lower().rsplit("@", 1)[-1]
    if netloc.startswith("["):                      # IPv6 literal
        host, _, rest = netloc.partition("]")
        host, port = host + "]", rest.lstrip(":")
    else:
        host, _, port = netloc.partition(":")
    if port == _DEFAULT_PORTS.get(scheme):
        port = ""
    return host + (":" + port if port else "")


def _own_hosts():
    hosts = {_host_key(request.host, request.scheme)}
    for part in request.headers.get("X-Forwarded-Host", "").split(","):
        if part.strip():
            hosts.add(_host_key(part, request.headers.get(
                "X-Forwarded-Proto", request.scheme).split(",")[0].strip()))
    for origin in os.environ.get("TRUSTED_ORIGINS", "").split(","):
        origin = origin.strip()
        if origin:
            parts = urlsplit(origin if "//" in origin else "//" + origin)
            hosts.add(_host_key(parts.netloc, parts.scheme or "https"))
    hosts.discard("")
    return hosts


def _same_site(url, hostless=False):
    """Whether ``url`` points at this app. ``hostless`` is the answer for a
    URL with no host: a bare path names no other site (a Referer can be
    one), while an Origin must always carry a host."""
    parts = urlsplit(url)
    if not parts.netloc:
        return hostless
    return _host_key(parts.netloc, parts.scheme) in _own_hosts()


def foreign_write():
    """True when the current request is a write started by another site."""
    if request.method not in UNSAFE_METHODS:
        return False
    origin = request.headers.get("Origin")
    if origin is not None:
        origin = origin.strip()
        if not origin or origin.lower() == "null":
            return True
        return not _same_site(origin)
    referer = request.headers.get("Referer")
    if referer:
        return not _same_site(referer, hostless=True)
    return False


def _wants_json():
    if request.is_json:
        return True
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return True
    best = request.accept_mimetypes.best_match(["application/json", "text/html"])
    return best == "application/json" and request.accept_mimetypes[best] > \
        request.accept_mimetypes["text/html"]


def install_origin_guard(app):
    @app.before_request
    def _refuse_foreign_writes():
        if not foreign_write():
            return None
        if _wants_json():
            return jsonify({"ok": False, "error": REFUSAL}), 403
        return (REFUSAL, 403, {"Content-Type": "text/plain; charset=utf-8"})


def install_security_headers(app):
    """Headers every answer carries.

    - ``X-Frame-Options`` / ``frame-ancestors``: no other site may show the
      app inside a frame of its own. Framed under a decoy page, a signed-in
      user's clicks on "Approve" or "Delete" could be stolen (clickjacking) —
      the origin guard cannot see that, the click is the app's own page.
    - ``X-Content-Type-Options: nosniff``: a browser serves a file as the
      type the app names, never as HTML it guessed from the content.
    - ``Referrer-Policy``: links out of the app do not carry its full URLs
      (report filters, document ids) to other sites.
    """
    @app.after_request
    def _security_headers(response):
        h = response.headers
        h.setdefault("X-Frame-Options", "SAMEORIGIN")
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        if "Content-Security-Policy" not in h:
            h["Content-Security-Policy"] = "frame-ancestors 'self'"
        return response
