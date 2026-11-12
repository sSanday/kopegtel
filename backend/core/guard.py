"""Guard API: autentikasi, CSRF, idle timeout, wajib ganti password."""
import functools
import os
import time

import db
import auth

from flask import g, jsonify, request, session


def _env_flag(name):
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def client_ip():
    return request.remote_addr or "-"


def audit(action, obj, summary=""):
    user = getattr(g, "user", None) or {}
    auth.log_audit(user, action, obj, summary, ip=client_ip())


def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    user = db.find_user_by_id(uid)
    if user is None:
        session.clear()
        return None
    return user


def roles(*allowed):
    """Batasi endpoint ke peran tertentu (admin/operator/viewer). Viewer: GET saja."""

    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            user = getattr(g, "user", None)
            if user is None or user["role"] not in allowed:
                return jsonify({"error": "Anda tidak memiliki izin untuk tindakan ini.", "code": "forbidden"}), 403
            return fn(*args, **kwargs)

        return wrapper

    return decorator


_PUBLIC_API = {"/api/login", "/api/public-config", "/api/mfa/verify-login"}
_CSRF_EXEMPT = {"/api/login"}
_MUST_CHANGE_ALLOW = {"/api/me", "/api/logout", "/api/users/me/password"}
_MUTATING = {"POST", "PUT", "DELETE", "PATCH"}


def guard_api():
    if not request.path.startswith("/api"):
        return None

    if request.method in _MUTATING and request.path not in _CSRF_EXEMPT:
        if not auth.csrf_valid(session, request.headers.get("X-CSRF-Token")):
            return jsonify({"error": "Token CSRF tidak valid atau hilang.", "code": "csrf"}), 403

    if request.path in _PUBLIC_API:
        return None

    user = current_user()
    if user is None:
        return jsonify({"error": "Belum login.", "code": "unauthorized"}), 401

    now = time.time()
    try:
        idle_minutes = int(db.get_setting("idle_timeout_minutes", "30"))
    except (TypeError, ValueError):
        idle_minutes = 30
    idle_minutes = min(24 * 60, max(5, idle_minutes))
    last = session.get("last_activity_at", now)
    if now - last > idle_minutes * 60:
        session.clear()
        return jsonify({"error": f"Sesi berakhir karena tidak aktif selama {idle_minutes} menit.", "code": "idle_timeout"}), 401

    session["last_activity_at"] = now
    g.user = user

    if user["must_change_password"] and request.path not in _MUST_CHANGE_ALLOW:
        return jsonify({"error": "Anda wajib mengganti kata sandi sebelum melanjutkan.", "code": "must_change_password"}), 403

    return None


def add_security_headers(response):
    response = auth.security_headers(response)
    if _env_flag("HSTS_ENABLED"):
        # Hanya aktifkan bila aplikasi berada di belakang HTTPS (mis. reverse proxy).
        response.headers["Strict-Transport-Security"] = "max-age=31536000"
    # HTML tidak boleh heuristically cached — halaman menunjuk aset ber-?v= yang berubah.
    if (response.content_type or "").startswith("text/html"):
        response.headers["Cache-Control"] = "no-cache"
    return response


def register_guard(app):
    """Pasang before_request/after_request pada app."""
    app.before_request(guard_api)
    app.after_request(add_security_headers)
