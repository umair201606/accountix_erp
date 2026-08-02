"""Small security helpers shared by the application entry points."""

from urllib.parse import urlsplit


def safe_local_url(target):
    """Return *target* only when it is a local absolute path.

    Redirect destinations come from the browser and must not be allowed to
    turn a successful login into an open redirect.  Backslashes are rejected
    as well because browsers may normalize them as URL separators.
    """
    if not isinstance(target, str) or not target or "\\" in target:
        return None
    parsed = urlsplit(target)
    if parsed.scheme or parsed.netloc:
        return None
    if not target.startswith("/") or target.startswith("//"):
        return None
    return target
