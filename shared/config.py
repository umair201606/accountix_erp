import os


def normalize_database_url(url):
    """Any Postgres URL form -> the psycopg2 driver this app ships with.

    Neon / Vercel hand out ``postgres://``, ``postgresql://`` and
    ``postgresql+psycopg://`` (psycopg 3) URLs. Only psycopg2 is installed
    (requirements.txt), and a ``+psycopg`` URL made SQLAlchemy import the
    missing ``psycopg`` package and the whole app fail to start. Quotes and
    whitespace pasted into the environment variable are stripped too.
    """
    url = (url or "").strip().strip('"').strip("'")
    if not url:
        return url
    for prefix in ("postgres://", "postgresql://", "postgresql+psycopg://",
                   "postgresql+psycopg2://", "postgresql+pg8000://",
                   "postgresql+asyncpg://"):
        if url.startswith(prefix):
            return "postgresql+psycopg2://" + url[len(prefix):]
    return url


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", os.urandom(24).hex())
    SQLALCHEMY_DATABASE_URI = (normalize_database_url(os.environ.get("DATABASE_URL"))
                               or "sqlite:///erp.db")
    # Neon closes idle connections; a warm serverless instance would otherwise
    # hand the next request a dead one ("SSL connection has been closed
    # unexpectedly"). Ping before use and recycle well inside Neon's timeout.
    #
    # Pool size: a Vercel instance serves one request at a time, so it needs
    # one connection for the request plus one for the audit log's
    # record_now() (its own connection, so refused postings survive the
    # rollback). Five idle connections per instance, times every warm
    # instance, only ate into Neon's connection limit.
    if SQLALCHEMY_DATABASE_URI.startswith("postgresql"):
        _serverless = bool(os.environ.get("VERCEL"))
        SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True, "pool_recycle": 280,
                                     "pool_size": 2 if _serverless else 5,
                                     "max_overflow": 2 if _serverless else 5}
    elif SQLALCHEMY_DATABASE_URI.startswith("sqlite"):
        # Local / test server: SQLite lets one writer in at a time and makes
        # the next wait — 5 s by default, after which a second user saving at
        # a busy moment got "database is locked". Wait longer instead.
        SQLALCHEMY_ENGINE_OPTIONS = {"connect_args": {"timeout": 30}}
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    WTF_CSRF_ENABLED = False
    # Form tokens are off (above), so the cookies themselves must refuse to
    # travel with a request another website starts: SameSite=Lax keeps them
    # off cross-site POSTs (Firefox and Safari only do that when the
    # attribute is set explicitly), and shared/csrf_guard.py refuses any
    # write whose Origin / Referer names another site. HttpOnly keeps them
    # out of page scripts; Secure on Vercel, which only serves HTTPS (local
    # dev is plain http, where a Secure cookie would never come back).
    _https = bool(os.environ.get("VERCEL"))
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SECURE = _https
    REMEMBER_COOKIE_SAMESITE = "Lax"
    REMEMBER_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_SECURE = _https
    MAX_CONTENT_LENGTH = 16 * 1024 * 1024
    UPLOAD_FOLDER = os.path.join(os.path.dirname(os.path.dirname(__file__)), "uploads")

    # Serverless/Vercel: detect read-only filesystem
    _readonly = False
    try:
        test_path = os.path.join(os.getcwd(), ".vercel_write_test")
        with open(test_path, "w") as f:
            f.write("test")
        os.remove(test_path)
    except (OSError, IOError):
        _readonly = True

    if _readonly:
        if not SQLALCHEMY_DATABASE_URI or "sqlite" in SQLALCHEMY_DATABASE_URI:
            SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
        UPLOAD_FOLDER = "/tmp/uploads"
