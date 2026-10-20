"""Sign-in safeguards and the rules for changing a login.

Serverless (Vercel): nothing can be kept in memory between requests, so the
sign-in throttle counts the failed sign-ins the audit log already records.
"""
import hashlib
import hmac
from datetime import datetime, timedelta

from werkzeug.security import check_password_hash, generate_password_hash

MIN_PASSWORD_LENGTH = 8

# The handful every guessing list starts with.
_COMMON = {
    "password", "password1", "password123", "12345678", "123456789",
    "1234567890", "11111111", "00000000", "qwerty123", "qwertyui",
    "iloveyou", "admin123", "admin1234", "welcome1", "letmein1",
    "abc12345", "abcd1234", "pakistan", "pakistan1", "pakistan123",
}


def password_problem(password):
    """Why a NEW password is refused, or None.

    Asked only when a password is being set — existing logins keep working
    whatever their length.
    """
    if not password or len(password) < MIN_PASSWORD_LENGTH:
        return f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
    if len(password) > 128:
        return "Password must be at most 128 characters."
    if password.strip() != password or not password.strip():
        return "Password cannot start or end with a space."
    if password.lower() in _COMMON:
        return "That password is too common — choose another."
    return None


# ── Sign-in throttle ────────────────────────────────────────────────────────

THROTTLE_WINDOW = timedelta(minutes=15)
# Failed sign-ins in the window before more are refused without checking
# the password: from one address (any login), for one login from one
# address, and for one login from anywhere (a spread-out attack). A
# successful sign-in clears the login's own count.
MAX_FAILS_PER_IP = 30
MAX_FAILS_PER_LOGIN_FROM_IP = 10
MAX_FAILS_PER_LOGIN = 50
THROTTLED_MESSAGE = ("Too many failed sign-in attempts. Wait 15 minutes, "
                     "then try again.")


def client_ip():
    """The address the audit log records for this request."""
    from flask import has_request_context, request
    if not has_request_context():
        return None
    fwd = request.headers.get("X-Forwarded-For", "")
    ip = (fwd.split(",")[0].strip() if fwd else request.remote_addr) or None
    return ip[:64] if ip else None


def login_key(login_id):
    """How a typed login is filed on its failed sign-ins (the audit row's
    reference), the same whether or not such an account exists."""
    return (login_id or "").strip().lower()[:120] or None


def _limits():
    from flask import current_app
    cfg = current_app.config
    return (cfg.get("LOGIN_MAX_FAILS_PER_IP", MAX_FAILS_PER_IP),
            cfg.get("LOGIN_MAX_FAILS_PER_LOGIN_FROM_IP", MAX_FAILS_PER_LOGIN_FROM_IP),
            cfg.get("LOGIN_MAX_FAILS_PER_LOGIN", MAX_FAILS_PER_LOGIN))


def sign_in_throttled(login_id, user=None):
    """Whether this sign-in must be refused before its password is looked at."""
    from sqlalchemy import func, or_
    from shared.extensions import db
    from shared.models.audit_log import AuditLog
    per_ip, per_pair, per_login = _limits()
    since = datetime.utcnow() - THROTTLE_WINDOW
    ip = client_ip()
    fails = AuditLog.query.filter(AuditLog.action == "login_failed",
                                  AuditLog.created_at >= since)
    if ip and fails.filter(AuditLog.ip_address == ip).count() >= per_ip:
        return True
    key = login_key(login_id)
    who = [AuditLog.reference == key] if key else []
    if user is not None:
        who.append(AuditLog.user_id == user.id)
        last_ok = (db.session.query(func.max(AuditLog.created_at))
                   .filter(AuditLog.action == "login",
                           AuditLog.user_id == user.id,
                           AuditLog.created_at >= since).scalar())
        if last_ok is not None:
            fails = fails.filter(AuditLog.created_at > last_ok)
    if not who:
        return False
    mine = fails.filter(or_(*who))
    if mine.count() >= per_login:
        return True
    return bool(ip) and mine.filter(AuditLog.ip_address == ip).count() >= per_pair


def record_failed_sign_in(summary, login_id, user=None, **kw):
    from shared.audit import record_now
    record_now("login_failed", summary, reference=login_key(login_id),
               user_id=user.id if user is not None else None, **kw)


_DUMMY_HASH = None


def spend_password_check(password):
    """Hash the typed password against a throwaway hash, so a login that
    does not exist takes as long to refuse as a wrong password — the reply
    time must not tell which logins are real."""
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = generate_password_hash("no account has this password")
    check_password_hash(_DUMMY_HASH, password or "")
    return False


# ── Session identity ────────────────────────────────────────────────────────

def session_fingerprint(user):
    """A short keyed digest of the stored password hash. It rides in the
    session next to the user id, so a new password signs out every other
    session of the login (a stolen one included)."""
    from flask import current_app
    secret = current_app.secret_key or ""
    if isinstance(secret, str):
        secret = secret.encode()
    return hmac.new(secret, (user.password_hash or "").encode(),
                    hashlib.sha256).hexdigest()[:16]


def refresh_own_session(user):
    """After this request's own user changes their password: carry the new
    fingerprint so they stay signed in here."""
    from flask import has_request_context, session
    if not has_request_context() or "_user_id" not in session:
        return
    from flask_login import current_user
    try:
        same = current_user.is_authenticated and current_user.id == user.id
    except Exception:
        same = False
    if same:
        session["_user_id"] = user.get_id()


# ── A person's own login: User ID and email ─────────────────────────────────

def identity_problem(user, login_id, email):
    """Why this User ID / email cannot belong to ``user``, or None.

    A User ID that is another account's email (or the other way round)
    would let one person's sign-in land on someone else's account — sign-in
    tries the User ID first, then the email.
    """
    from shared.extensions import db
    from shared.models.base import User
    if not login_id or any(ch.isspace() for ch in login_id) or len(login_id) < 3:
        return "User ID must be at least 3 characters, with no spaces."
    if len(login_id) > 120 or len(email or "") > 120:
        return "User ID and email must be at most 120 characters."
    if not email or "@" not in email or any(ch.isspace() for ch in email):
        return "Enter a valid email address."
    others = User.query.filter(User.id != user.id)
    lid, mail = login_id.lower(), email.lower()
    if others.filter(db.or_(db.func.lower(User.login_id) == lid,
                            db.func.lower(User.email) == lid)).first():
        return "That User ID is already taken."
    if others.filter(db.or_(db.func.lower(User.email) == mail,
                            db.func.lower(User.login_id) == mail)).first():
        return "That email is already in use."
    from shared.models.company import RegistrationRequest
    if mail != (user.email or "").lower() and RegistrationRequest.email_exists(mail):
        return "That email is already in use."
    return None


def move_invitations(old_email, new_email):
    """Invitations waiting for this person follow them to their new email.
    Left on the old address, whoever took that address next could accept
    them and join companies that meant to invite this person."""
    from shared.extensions import db
    from shared.models.company import CompanyInvitation
    old, new = (old_email or "").lower(), (new_email or "").lower()
    if not old or old == new:
        return
    (CompanyInvitation.query
     .filter(db.func.lower(CompanyInvitation.email) == old,
             CompanyInvitation.status == CompanyInvitation.SENT)
     .update({"email": new}, synchronize_session=False))
