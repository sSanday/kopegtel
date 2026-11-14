"""Blueprint autentikasi & endpoint publik (login, sesi, password)."""
import time
from datetime import datetime, timezone

from flask import Blueprint, g, jsonify, request, session
from werkzeug.security import check_password_hash, generate_password_hash

import auth
import db
from core.env import APP_VERSION
from core.guard import client_ip, audit

bp = Blueprint("auth", __name__)


def _establish_session(user, remember=False):
    session.clear()
    session["user_id"] = user["id"]
    session["username"] = user["username"]
    session["role"] = user["role"]
    session["last_activity_at"] = time.time()
    session[auth.SESSION_CSRF_KEY] = auth.new_csrf_token()
    session.permanent = remember


def _login_response(user):
    return jsonify(
        {
            "ok": True,
            "username": user["username"],
            "role": user["role"],
            "must_change_password": bool(user["must_change_password"]),
            "mfa_grace_pending": bool(session.get("mfa_grace_pending")),
            "csrf_token": session[auth.SESSION_CSRF_KEY],
        }
    )


@bp.get("/api/public-config")
def api_public_config():
    settings = db.get_settings()
    return jsonify(
        {
            "org_name": settings["org_name"],
            "timezone": settings["timezone"],
            "version": APP_VERSION,
        }
    )


@bp.post("/api/login")
def api_login():
    data = request.get_json(silent=True) or {}
    raw_username = str(data.get("username") or "").strip()
    password = str(data.get("password") or "")
    remember = bool(data.get("remember"))

    if not raw_username or not password:
        return jsonify({"error": "Nama pengguna dan kata sandi wajib diisi."}), 400

    # Username/password raksasa tidak boleh masuk DB atau memicu hash mahal.
    # Nilai tetap dipotong untuk penghitung kegagalan dan audit; login dengan
    # input melebihi batas selalu gagal.
    username = raw_username[: auth.USERNAME_MAX_LENGTH]
    oversized = (
        len(raw_username) > auth.USERNAME_MAX_LENGTH
        or len(password) > auth.PASSWORD_MAX_LENGTH
    )

    ip = client_ip()
    if auth.login_blocked(username, ip):
        return (
            jsonify(
                {
                    "error": "Terlalu banyak percobaan gagal. Coba lagi dalam 10 menit.",
                    "code": "login_blocked",
                }
            ),
            429,
            {"Retry-After": str(auth.LOGIN_WINDOW_MINUTES * 60)},
        )

    if oversized:
        counts = auth.record_login_failure(username, ip)
        auth.log_login_failure({"username": username}, ip, counts)
        return jsonify({"error": "Nama pengguna atau kata sandi salah."}), 401

    user = db.find_user(username)
    ok = user is not None and check_password_hash(user["password_hash"], password)
    if not ok:
        counts = auth.record_login_failure(username, ip)
        user_id = user["id"] if user else None
        auth.log_login_failure(
            {"id": user_id, "username": username}, ip, counts
        )
        return jsonify({"error": "Nama pengguna atau kata sandi salah."}), 401

    import mfa

    if user["mfa_enabled"]:
        # Password benar tetapi MFA aktif: sesi belum diterbitkan.
        session.clear()
        session["mfa_pending_user"] = user["id"]
        session["mfa_pending_at"] = time.time()
        session["mfa_pending_remember"] = bool(remember)
        session[auth.SESSION_CSRF_KEY] = auth.new_csrf_token()
        return jsonify(
            {
                "ok": True,
                "mfa_required": True,
                "username": user["username"],
                "csrf_token": session[auth.SESSION_CSRF_KEY],
            }
        )

    # Kebijakan MFA untuk peran tertentu (dengan masa tenggang).
    policy = mfa.policy_state(user)
    if policy["required"] and not policy["grace_open"]:
        return jsonify({
            "error": "MFA wajib untuk peran Anda. Hubungi administrator.",
            "code": "mfa_required",
        }), 403

    auth.clear_login_failures(username, ip)
    _establish_session(user, remember)
    if policy["required"] and policy["grace_open"]:
        session["mfa_grace_pending"] = 1

    auth.log_audit(user, "auth.login", "login", "Login berhasil", ip=ip)
    return _login_response(user)


@bp.get("/api/me")
def api_me():
    user = g.user
    settings = db.get_settings()
    return jsonify(
        {
            "ok": True,
            "username": user["username"],
            "role": user["role"],
            "role_label": auth.ROLE_LABELS.get(user["role"], user["role"]),
            "must_change_password": bool(user["must_change_password"]),
            "mfa_enabled": bool(user["mfa_enabled"]),
            "mfa_grace_pending": bool(session.get("mfa_grace_pending")),
            "csrf_token": session.get(auth.SESSION_CSRF_KEY),
            "server_time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "timezone": settings["timezone"],
            "org_name": settings["org_name"],
        }
    )


@bp.post("/api/logout")
def api_logout():
    audit("auth.logout", "logout", "Keluar")
    session.clear()
    return jsonify({"ok": True})


@bp.post("/api/users/me/password")
def api_change_own_password():
    data = request.get_json(silent=True) or {}
    current = str(data.get("current_password") or "")
    new_password = str(data.get("new_password") or "")

    user = db.find_user_by_id(g.user["id"])
    if user is None:
        return jsonify({"error": "Akun tidak ditemukan."}), 404
    if not check_password_hash(user["password_hash"], current):
        return jsonify({"error": "Kata sandi saat ini salah."}), 400

    problem = auth.password_problem(new_password)
    if problem:
        return jsonify({"error": problem}), 400

    db.update_user(user["id"], password_hash=generate_password_hash(new_password), must_change_password=0)
    session["must_change_password"] = "0"
    audit("auth.password_changed", "user", "Kata sandi diganti")
    return jsonify({"ok": True})
