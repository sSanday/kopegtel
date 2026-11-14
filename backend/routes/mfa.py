"""Blueprint E2 — MFA TOTP: status, setup, verifikasi, disable, reset admin."""
import time

from flask import Blueprint, g, jsonify, request, session
from werkzeug.security import check_password_hash

import auth
import db
import mfa

from core.guard import audit, roles, client_ip
from routes.auth import _establish_session, _login_response

bp = Blueprint("mfa", __name__)

MFA_PENDING_TTL_SECONDS = 300


def _pending_user():
    user_id = session.get("mfa_pending_user")
    pending_at = session.get("mfa_pending_at", 0)
    if not user_id or time.time() - float(pending_at) > MFA_PENDING_TTL_SECONDS:
        session.clear()
        return None
    user = db.find_user_by_id(int(user_id))
    if user is None:
        session.clear()
        return None
    # M-4: reset password di tengah langkah MFA membatalkan login yang belum
    # selesai; epoch di sesi sementara harus sama dengan epoch akun.
    try:
        pending_epoch = int(session.get("mfa_pending_epoch"))
    except (TypeError, ValueError):
        pending_epoch = -1
    if pending_epoch != int(user["session_epoch"] or 0):
        session.clear()
        return None
    return user


def _csrf_ok():
    token = request.headers.get("X-CSRF-Token")
    return auth.csrf_valid(session, token)


@bp.get("/api/mfa/status")
def api_mfa_status():
    return jsonify(mfa.mfa_status_for(g.user))


@bp.post("/api/mfa/setup")
def api_mfa_setup():
    user = db.find_user_by_id(g.user["id"])
    if user is None:
        return jsonify({"error": "Akun tidak ditemukan."}), 404
    if user["mfa_enabled"]:
        return jsonify({"error": "MFA sudah aktif. Nonaktifkan dulu untuk mengatur ulang."}), 400
    result = mfa.start_setup(user["id"])
    secret = result["secret"]
    settings = db.get_settings()
    uri = mfa.otpauth_uri(secret, user["username"], settings.get("org_name") or "NMS Kopegtel")
    audit("auth.mfa_setup", "user", "Memulai pendaftaran MFA")
    return jsonify({
        "ok": True,
        "secret": secret,
        "otpauth_uri": uri,
        "qr_svg": mfa.qr_png_svg(uri),
    })


@bp.post("/api/mfa/verify")
def api_mfa_verify():
    data = request.get_json(silent=True) or {}
    code = str(data.get("code") or "")
    result = mfa.enable_after_verify(g.user["id"], code)
    if result is None:
        return jsonify({"error": "Akun tidak ditemukan."}), 404
    if result.get("error"):
        audit("auth.mfa_verify_failed", "user", "Kode verifikasi MFA salah")
        return jsonify({"error": result["error"]}), 400
    audit("auth.mfa_enabled", "user", "MFA TOTP diaktifkan")
    return jsonify({"ok": True, "enabled": True, "recovery_codes": result["recovery_codes"]})


@bp.post("/api/mfa/disable")
def api_mfa_disable():
    data = request.get_json(silent=True) or {}
    password = str(data.get("password") or "")
    code = str(data.get("code") or "")
    user_row = db.find_user_by_id(g.user["id"])
    if user_row is None:
        return jsonify({"error": "Akun tidak ditemukan."}), 404
    user = dict(user_row)
    ok, error = mfa.disable(
        user,
        password,
        code,
        lambda pw: check_password_hash(user_row["password_hash"], pw),
    )
    if not ok:
        audit("auth.mfa_disable_failed", "user", "Percobaan menonaktifkan MFA gagal")
        return jsonify({"error": error}), 400
    audit("auth.mfa_disabled", "user", "MFA TOTP dinonaktifkan")
    return jsonify({"ok": True, "enabled": False})


@bp.post("/api/mfa/reset/<int:user_id>")
@roles("admin")
def api_mfa_admin_reset(user_id):
    target = db.find_user_by_id(user_id)
    if target is None:
        return jsonify({"error": "Pengguna tidak ditemukan."}), 404
    data = request.get_json(silent=True) or {}
    admin_password = str(data.get("password") or "")
    admin = db.find_user_by_id(g.user["id"])
    if admin is None or not check_password_hash(admin["password_hash"], admin_password):
        audit("auth.mfa_reset_failed", f"user:{target['username']}",
              "Reset MFA gagal (password admin salah)")
        return jsonify({"error": "Kata sandi Anda salah."}), 400
    mfa.admin_reset(user_id)
    audit("auth.mfa_reset", f"user:{target['username']}",
          f"MFA pengguna {target['username']} direset admin")
    return jsonify({"ok": True})


@bp.post("/api/mfa/verify-login")
def api_mfa_verify_login():
    """Langkah kedua login: verifikasi kode TOTP/pemulihan, lalu terbitkan sesi."""
    if not _csrf_ok():
        return jsonify({"error": "Token CSRF tidak valid atau hilang.", "code": "csrf"}), 403

    user = _pending_user()
    if user is None:
        return jsonify({"error": "Sesi verifikasi kedaluwarsa. Login ulang.", "code": "mfa_pending_expired"}), 401

    ip = client_ip()
    if auth.login_blocked(f"mfa:{user['id']}", ip):
        return jsonify({"error": "Terlalu banyak percobaan gagal. Coba lagi dalam 10 menit.",
                        "code": "login_blocked"}), 429

    data = request.get_json(silent=True) or {}
    code = str(data.get("code") or "")
    ok, kind = mfa.verify_for_login(dict(user), code)
    if not ok:
        auth.record_login_failure(f"mfa:{user['id']}", ip)
        auth.log_audit(user, "auth.mfa_failed", "login", "Kode MFA salah", ip=ip)
        return jsonify({"error": "Kode MFA salah.", "code": "mfa_invalid"}), 401

    auth.clear_login_failures(f"mfa:{user['id']}", ip)
    user = db.find_user_by_id(user["id"])
    remember = bool(session.get("mfa_pending_remember"))
    _establish_session(user, remember)
    policy = mfa.policy_state(user)
    if policy["required"] and policy["grace_open"]:
        session["mfa_grace_pending"] = 1
    if kind == "recovery":
        auth.log_audit(user, "auth.mfa_recovery_used", "login", "Kode pemulihan MFA dipakai", ip=ip)
    else:
        auth.log_audit(user, "auth.mfa_login", "login", "Login dengan MFA", ip=ip)
    return _login_response(user)
