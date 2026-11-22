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
# Batas per-IP melengkapi batas per akun+IP. Tanpa ini, penyerang cukup
# mengganti username untuk menghindari blokir 5 percobaan.
LOGIN_IP_MAX_FAILED = 50
# Audit kegagalan login tidak ditulis untuk setiap percobaan (bisa membanjiri
# audit_log yang disimpan 365 hari). Hanya kejadian pertama per IP dan saat
# ambang blokir tercapai yang dicatat, dengan cap global sebagai pengaman.
LOGIN_AUDIT_MAX_PER_WINDOW = 500
# Batas panjang input login. Username akun aplikasi maksimal 32 karakter;
# 64 memberi ruang untuk data lama tanpa menerima string tak terbatas.
USERNAME_MAX_LENGTH = 64
PASSWORD_MAX_LENGTH = 1024
# Body login/MFA tidak perlu besar; batas global tetap berlaku untuk endpoint lain.
LOGIN_MAX_BODY_BYTES = 8 * 1024

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
    if len(password) > PASSWORD_MAX_LENGTH:
        return f"Kata sandi maksimal {PASSWORD_MAX_LENGTH} karakter."
    return None


def login_blocked(username, ip):
    """True bila batas akun+IP atau batas IP (tanpa melihat username) tercapai."""
    counts = db.count_recent_login_failures(username, ip, LOGIN_WINDOW_MINUTES)
    return counts["ip"] >= LOGIN_IP_MAX_FAILED or counts["pair"] >= LOGIN_MAX_FAILED


def record_login_failure(username, ip):
    """Simpan satu kegagalan; kembalikan jumlah per IP dan per akun+IP."""
    return db.record_login_failure(username, ip, LOGIN_WINDOW_MINUTES)


def clear_login_failures(username, ip):
    db.clear_login_failures(username, ip)


def _login_failure_audit(counts):
    """Pilih momen audit yang jarang tetapi cukup untuk investigasi.

    Semua percobaan tetap tersimpan singkat di ``login_attempts``; audit_log
    hanya menerima sinyal awal dan saat ambang blokir tercapai.
    """
    if counts["ip"] == 1:
        return (
            "Gagal login (percobaan pertama dari IP ini dalam "
            f"{LOGIN_WINDOW_MINUTES} menit)"
        )
    if counts["pair"] == LOGIN_MAX_FAILED:
        return f"Akun+IP diblokir sementara setelah {LOGIN_MAX_FAILED} kegagalan"
    if counts["ip"] == LOGIN_IP_MAX_FAILED:
        return f"IP diblokir sementara setelah {LOGIN_IP_MAX_FAILED} kegagalan"
    return None


def log_login_failure(user, ip, counts):
    """Catat kegagalan login secara terbatas (bukan satu baris per percobaan)."""
    summary = _login_failure_audit(counts)
    if summary is None:
        return
    if (
        db.count_recent_audit("auth.login_failed", LOGIN_WINDOW_MINUTES)
        >= LOGIN_AUDIT_MAX_PER_WINDOW
    ):
        return
    log_audit(user, "auth.login_failed", "login", summary, ip=ip)


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
