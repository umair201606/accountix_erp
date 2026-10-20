"""Read/write uploaded files for the active company.

New files go to the database (shared/models/stored_file.py). Reads fall
back to the old disk layout -- ``UPLOAD_FOLDER/<company_id>/<key>``, then
``UPLOAD_FOLDER/<key>``, then the flat ``UPLOAD_FOLDER/<file name>`` -- so
files uploaded before this change (on a local install) keep working.

Callers commit; save()/delete() only add to the session, so the file and
the record pointing at it are written in the same transaction.
"""
import mimetypes
import os

from flask import current_app

from shared.extensions import db
from shared.models.stored_file import StoredFile
from shared.tenancy import current_company_id


# Types a browser would run as a page or script if one of these files were
# opened directly. Uploads are served under the app's own origin, so a "picture"
# uploaded as text/html would run its script with the viewer's session.
_ACTIVE_TYPES = ("text/html", "application/xhtml+xml", "image/svg+xml",
                 "text/xml", "application/xml", "text/javascript",
                 "application/javascript", "application/x-javascript")


def _safe_type(key, declared=None):
    """The type a stored file is served as: from its own extension, never
    the browser's say-so, and never one that would run in the page."""
    guessed = mimetypes.guess_type(key)[0] or declared or "application/octet-stream"
    base = guessed.split(";")[0].strip().lower()
    if base in _ACTIVE_TYPES or base.startswith("text/html"):
        return "application/octet-stream"
    return guessed


def _require_company():
    cid = current_company_id()
    if cid is None:
        raise RuntimeError("No active company for file storage")
    return cid


def _legacy_paths(key, company_id):
    root = current_app.config.get("UPLOAD_FOLDER") or ""
    if not root:
        return
    parts = [p for p in key.split("/") if p not in ("", ".", "..")]
    if company_id is not None:
        yield os.path.join(root, str(company_id), *parts)
    yield os.path.join(root, *parts)
    if len(parts) > 1:
        # Pre-multi-company documents sat flat in the upload folder.
        yield os.path.join(root, parts[-1])


def save(key, data, content_type=None):
    """Store ``data`` (bytes) under ``key``, replacing any file already there."""
    _require_company()
    row = StoredFile.query.filter_by(key=key).first()
    if row is None:
        row = StoredFile(key=key)
        db.session.add(row)
    row.data = data
    row.size = len(data)
    row.content_type = _safe_type(key, content_type)
    return row


def read(key, company_id=None):
    """(bytes, content_type) for ``key``, or None when there is no such file.

    ``company_id`` is only used for the legacy-disk fallback (the database
    lookup is scoped to the active company by tenancy)."""
    row = StoredFile.query.filter_by(key=key).first()
    if row is not None:
        return row.data, _safe_type(key, row.content_type)
    cid = company_id if company_id is not None else current_company_id()
    for path in _legacy_paths(key, cid):
        if os.path.isfile(path):
            with open(path, "rb") as fh:
                return fh.read(), _safe_type(path)
    return None


def delete(key, company_id=None):
    """Remove ``key`` from the database and any legacy copy on disk."""
    StoredFile.query.filter_by(key=key).delete(synchronize_session=False)
    cid = company_id if company_id is not None else current_company_id()
    for path in _legacy_paths(key, cid):
        try:
            if os.path.isfile(path):
                os.remove(path)
        except OSError:
            pass  # read-only filesystem (Vercel): nothing to clean there
