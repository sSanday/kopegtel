"""Helper keamanan terpusat: peran, CSRF, batas percobaan login, audit, header.

Dipakai oleh app.py — jangan impor modul ini dari skrip satu kali.
"""

import hmac
import secrets

import db

# Peran yang didukung (ditegakkan di server pada tiap endpoint).
ROLE_ADMIN = "admin"
ROLE_OPERATOR = "operator"
ROLE_VIEWER = "viewer"
ROLES = (ROLE_ADMIN, ROLE_OPERATOR, ROLE_VIEWER)

ROLE_LABELS = {
    ROLE_ADMIN: "Administrator",
    ROLE_OPERATOR: "Operator",
    ROLE_VIEWER: "Viewer",
}

# Minimal 10 karakter untuk semua kata sandi yang diatur lewat aplikasi.
PASSWORD_MIN_LENGTH = 10
LOGIN_MAX_FAILED = 5
LOGIN_WINDOW_MINUTES = 10

SESSION_CSRF_KEY = "csrf_token"


def new_csrf_token():
    return secrets.token_urlsafe(32)


def csrf_valid(session, header_token):
    expected = session.get(SESSION_CSRF_KEY)
    if not expected or not header_token:
        return False
    return hmac.compare_digest(str(expected), str(header_token))


def password_problem(password):
    """Kembalikan pesan galat bila kata sandi tidak memenuhi kebijakan, else None."""
    if not password:
        return "Kata sandi wajib diisi."
    if len(password) < PASSWORD_MIN_LENGTH:
        return f"Kata sandi minimal {PASSWORD_MIN_LENGTH} karakter."
    return None


def login_blocked(username, ip):
    """True bila gagal login >= LOGIN_MAX_FAILED dalam LOGIN_WINDOW_MINUTES terakhir."""
    return db.count_recent_login_failures(username, ip, LOGIN_WINDOW_MINUTES) >= LOGIN_MAX_FAILED


def record_login_failure(username, ip):
    db.record_login_failure(username, ip)


def clear_login_failures(username, ip):
    db.clear_login_failures(username, ip)


def log_audit(user, action, obj, summary="", ip=None):
    """Catat jejak audit tanpa secret. ``user`` boleh None / dict / sqlite3.Row."""
    if user is not None and not isinstance(user, dict):
        try:
            user = dict(user)
        except Exception:
            user = None
    user = user or {}
    db.add_audit_log(
        user_id=user.get("id"),
        username=user.get("username") or "-",
        ip=ip or "-",
        action=action,
        obj=obj,
        summary=summary,
    )


def security_headers(response):
    """Header keamanan dasar (CSP diuji agar tidak merusak halaman)."""
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "font-src 'self' data:; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self'"
    )
    return response
