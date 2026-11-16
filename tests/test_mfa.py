"""E2-C — MFA TOTP: RFC 6238, window, anti-replay, pemulihan, lockout, kebijakan."""

import base64
import json
import os
import sys
import time
from datetime import datetime, timezone

import pytest
from werkzeug.security import generate_password_hash

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, "backend"))

import app as app_module  # noqa: E402
import auth as auth_module  # noqa: E402
import db as db_module  # noqa: E402
import mfa  # noqa: E402

RFC_SECRET = base64.b32encode(b"12345678901234567890").decode("ascii")
RFC_VECTORS = [
    (59, 287082),
    (1111111109, 81804),
    (1111111111, 50471),
    (1234567890, 5924),
    (2000000000, 279037),
    (20000000000, 353130),
]


@pytest.fixture()
def client():
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as client:
        yield client


def _mk_user(db, username, password, role="admin"):
    user_id = db.create_user(
        username, generate_password_hash(password), role=role, must_change_password=0
    )
    return user_id


def _login(client, username, password):
    return client.post("/api/login", json={"username": username, "password": password})


def _complete_mfa_login(client, username, password, code):
    response = _login(client, username, password)
    assert response.status_code == 200
    body = response.get_json()
    if not body.get("mfa_required"):
        return body
    csrf = body["csrf_token"]
    verify = client.post(
        "/api/mfa/verify-login",
        json={"code": code},
        headers={"X-CSRF-Token": csrf},
    )
    assert verify.status_code == 200, verify.get_json()
    return verify.get_json()


def _enable_mfa(client, csrf, db, user_id, at=None):
    setup = client.post("/api/mfa/setup", headers={"X-CSRF-Token": csrf})
    assert setup.status_code == 200, setup.get_json()
    secret = setup.get_json()["secret"]
    if at is None:
        at = time.time()
    code = f"{mfa.totp(secret, at=at):06d}"
    verify = client.post("/api/mfa/verify", json={"code": code}, headers={"X-CSRF-Token": csrf})
    assert verify.status_code == 200, verify.get_json()
    return secret, verify.get_json()["recovery_codes"]


# ---------------------------------------------------------
# Unit: TOTP RFC 6238 & anti-replay
# ---------------------------------------------------------

def test_totp_rfc6238_vectors():
    for moment, expected in RFC_VECTORS:
        assert mfa.totp(RFC_SECRET, at=moment) == expected


def test_verify_totp_window_and_replay():
    moment = int(time.time())
    secret = mfa.new_secret()
    step = moment // 30
    code_prev = f"{mfa.totp(secret, at=(step - 1) * 30):06d}"
    assert mfa.verify_totp(secret, code_prev, window=1, at=moment) is not None
    assert mfa.verify_totp(secret, code_prev, window=0, at=moment) is None

    code_now = f"{mfa.totp(secret, at=moment):06d}"
    used_step = mfa.verify_totp(secret, code_now, window=1, last_step=0, at=moment)
    assert used_step is not None
    # Anti-replay: langkah yang sama ditolak.
    assert mfa.verify_totp(secret, code_now, window=1, last_step=used_step, at=moment) is None


def test_recovery_codes_single_use():
    codes = mfa.new_recovery_codes()
    assert len(codes) == 10
    assert len(set(codes)) == 10
    hashes = [mfa.hash_recovery_code(code) for code in codes]
    ok, remaining = mfa.consume_recovery_code(hashes, codes[0])
    assert ok
    assert remaining == hashes[1:]
    ok2, _ = mfa.consume_recovery_code(remaining, codes[0])
    assert not ok2


def test_otpauth_uri_and_qr_svg():
    secret = mfa.new_secret()
    uri = mfa.otpauth_uri(secret, "admin", "NMS Kopegtel")
    assert uri.startswith("otpauth://totp/")
    assert secret in uri
    assert "issuer=" in uri
    svg = mfa.qr_png_svg(uri)
    assert "<svg" in svg
    assert len(svg) < 20000


# ---------------------------------------------------------
# API: aktivasi, verifikasi, recovery, disable
# ---------------------------------------------------------

def test_mfa_setup_and_enable_flow(client, fresh_db):
    _mk_user(fresh_db, "mfa-admin", "mfa-admin-password", role="admin")
    response = _login(client, "mfa-admin", "mfa-admin-password")
    assert response.status_code == 200
    csrf = response.get_json()["csrf_token"]

    setup = client.post("/api/mfa/setup", headers={"X-CSRF-Token": csrf})
    assert setup.status_code == 200
    data = setup.get_json()
    assert data["otpauth_uri"].startswith("otpauth://totp/")
    assert "<svg" in data["qr_svg"]

    bad = client.post("/api/mfa/verify", json={"code": "000000"},
                      headers={"X-CSRF-Token": csrf})
    # 000000 bisa saja cocok secara kebetulan; hampir tidak mungkin, tetapi
    # tangani dua kasus dengan kode yang jelas salah.
    if bad.status_code == 400:
        assert "salah" in bad.get_json()["error"].lower()

    secret = data["secret"]
    code = f"{mfa.totp(secret):06d}"
    verify = client.post("/api/mfa/verify", json={"code": code},
                         headers={"X-CSRF-Token": csrf})
    assert verify.status_code == 200
    assert len(verify.get_json()["recovery_codes"]) == 10

    status = client.get("/api/mfa/status").get_json()
    assert status["enabled"] is True
    assert status["recovery_codes_left"] == 10


def test_mfa_login_totp_and_recovery_and_replay(client, fresh_db):
    _mk_user(fresh_db, "mfa-login", "mfa-login-password", role="admin")
    response = _login(client, "mfa-login", "mfa-login-password")
    csrf = response.get_json()["csrf_token"]
    secret, recovery = _enable_mfa(client, csrf, fresh_db, None)

    client.post("/api/logout", headers={"X-CSRF-Token": csrf})

    # Login password-only tidak menerbitkan sesi penuh; wajib kode.
    response = _login(client, "mfa-login", "mfa-login-password")
    body = response.get_json()
    assert body["mfa_required"] is True
    assert client.get("/api/me").status_code == 401

    csrf2 = body["csrf_token"]
    code = f"{mfa.totp(secret):06d}"
    ok = _complete_mfa_login(client, "mfa-login", "mfa-login-password", code)
    assert ok["ok"] is True
    assert client.get("/api/me").status_code == 200

    # Replay kode yang sama ditolak.
    client.post("/api/logout", headers={"X-CSRF-Token": ok["csrf_token"]})
    response = _login(client, "mfa-login", "mfa-login-password")
    csrf3 = response.get_json()["csrf_token"]
    replay = client.post(
        "/api/mfa/verify-login", json={"code": code},
        headers={"X-CSRF-Token": csrf3},
    )
    assert replay.status_code == 401

    # Kode pemulihan sekali pakai.
    recovery_code = recovery[0]
    ok2 = _complete_mfa_login(client, "mfa-login", "mfa-login-password", recovery_code)
    assert ok2["ok"] is True
    client.post("/api/logout", headers={"X-CSRF-Token": ok2["csrf_token"]})
    response = _login(client, "mfa-login", "mfa-login-password")
    csrf4 = response.get_json()["csrf_token"]
    reuse = client.post(
        "/api/mfa/verify-login", json={"code": recovery_code},
        headers={"X-CSRF-Token": csrf4},
    )
    assert reuse.status_code == 401


def test_mfa_lockout_after_5_failures(client, fresh_db):
    _mk_user(fresh_db, "mfa-lockout", "mfa-lockout-password", role="admin")
    response = _login(client, "mfa-lockout", "mfa-lockout-password")
    csrf = response.get_json()["csrf_token"]
    _enable_mfa(client, csrf, fresh_db, None)
    client.post("/api/logout", headers={"X-CSRF-Token": csrf})

    for _ in range(5):
        response = _login(client, "mfa-lockout", "mfa-lockout-password")
        csrf_try = response.get_json()["csrf_token"]
        bad = client.post(
            "/api/mfa/verify-login", json={"code": "00000000"},
            headers={"X-CSRF-Token": csrf_try},
        )
        assert bad.status_code == 401

    response = _login(client, "mfa-lockout", "mfa-lockout-password")
    csrf_try = response.get_json()["csrf_token"]
    blocked = client.post(
        "/api/mfa/verify-login", json={"code": "123456"},
        headers={"X-CSRF-Token": csrf_try},
    )
    assert blocked.status_code == 429


def test_mfa_disable_requires_password_and_code(client, fresh_db):
    _mk_user(fresh_db, "mfa-disable", "mfa-disable-password", role="admin")
    response = _login(client, "mfa-disable", "mfa-disable-password")
    csrf = response.get_json()["csrf_token"]
    secret, _ = _enable_mfa(client, csrf, fresh_db, None)

    wrong_pw = client.post(
        "/api/mfa/disable",
        json={"password": "salah-password", "code": f"{mfa.totp(secret):06d}"},
        headers={"X-CSRF-Token": csrf},
    )
    assert wrong_pw.status_code == 400

    code = f"{mfa.totp(secret):06d}"
    ok = client.post(
        "/api/mfa/disable",
        json={"password": "mfa-disable-password", "code": code},
        headers={"X-CSRF-Token": csrf},
    )
    assert ok.status_code == 200
    status = client.get("/api/mfa/status").get_json()
    assert status["enabled"] is False


def test_mfa_admin_reset_requires_admin_password(client, fresh_db):
    target_id = _mk_user(fresh_db, "mfa-target", "mfa-target-password", role="operator")
    _mk_user(fresh_db, "mfa-admin2", "mfa-admin2-password", role="admin")

    # Aktifkan MFA target lewat login target.
    response = _login(client, "mfa-target", "mfa-target-password")
    csrf_t = response.get_json()["csrf_token"]
    _enable_mfa(client, csrf_t, fresh_db, None)
    client.post("/api/logout", headers={"X-CSRF-Token": csrf_t})

    # Admin salah password -> 400 dan MFA tidak berubah.
    response = _login(client, "mfa-admin2", "mfa-admin2-password")
    csrf_a = response.get_json()["csrf_token"]
    bad = client.post(
        f"/api/mfa/reset/{target_id}",
        json={"password": "salah-password-admin"},
        headers={"X-CSRF-Token": csrf_a},
    )
    assert bad.status_code == 400
    with fresh_db._lock:
        row = fresh_db.get_conn().execute(
            "SELECT mfa_enabled FROM users WHERE id=?", (target_id,)
        ).fetchone()
    assert row["mfa_enabled"] == 1

    ok = client.post(
        f"/api/mfa/reset/{target_id}",
        json={"password": "mfa-admin2-password"},
        headers={"X-CSRF-Token": csrf_a},
    )
    assert ok.status_code == 200
    with fresh_db._lock:
        row = fresh_db.get_conn().execute(
            "SELECT mfa_enabled FROM users WHERE id=?", (target_id,)
        ).fetchone()
        audit = fresh_db.get_conn().execute(
            "SELECT action FROM audit_log WHERE action='auth.mfa_reset'"
        ).fetchone()
    assert row["mfa_enabled"] == 0
    assert audit is not None


# ---------------------------------------------------------
# Kebijakan MFA per peran + masa tenggang
# ---------------------------------------------------------

def test_policy_blocks_login_after_grace_expired(client, fresh_db):
    _mk_user(fresh_db, "mfa-policy", "mfa-policy-password", role="admin")
    old = (datetime.now(timezone.utc).timestamp() - 30 * 86400)
    started = datetime.fromtimestamp(old, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    fresh_db.set_settings({
        "mfa_required_roles": json.dumps(["admin"]),
        "mfa_grace_days": "7",
        "mfa_policy_started_at": started,
    })
    response = _login(client, "mfa-policy", "mfa-policy-password")
    assert response.status_code == 403
    assert response.get_json()["code"] == "mfa_required"


def test_policy_allows_login_inside_grace(client, fresh_db):
    _mk_user(fresh_db, "mfa-grace", "mfa-grace-password", role="admin")
    fresh_db.set_settings({
        "mfa_required_roles": json.dumps(["admin"]),
        "mfa_grace_days": "7",
        "mfa_policy_started_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    })
    response = _login(client, "mfa-grace", "mfa-grace-password")
    assert response.status_code == 200
    assert response.get_json()["mfa_grace_pending"] is True


def test_policy_not_required_for_other_roles(client, fresh_db):
    _mk_user(fresh_db, "mfa-viewer", "mfa-viewer-password", role="viewer")
    old = datetime.now(timezone.utc).timestamp() - 30 * 86400
    started = datetime.fromtimestamp(old, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    fresh_db.set_settings({
        "mfa_required_roles": json.dumps(["admin"]),
        "mfa_grace_days": "7",
        "mfa_policy_started_at": started,
    })
    response = _login(client, "mfa-viewer", "mfa-viewer-password")
    assert response.status_code == 200


def test_verify_login_requires_csrf(client, fresh_db):
    _mk_user(fresh_db, "mfa-csrf", "mfa-csrf-password", role="admin")
    response = _login(client, "mfa-csrf", "mfa-csrf-password")
    csrf = response.get_json()["csrf_token"]
    _enable_mfa(client, csrf, fresh_db, None)
    client.post("/api/logout", headers={"X-CSRF-Token": csrf})

    response = _login(client, "mfa-csrf", "mfa-csrf-password")
    assert response.get_json()["mfa_required"] is True
    no_csrf = client.post("/api/mfa/verify-login", json={"code": "123456"})
    assert no_csrf.status_code == 403


# ---------------------------------------------------------
# M-6: kode pemulihan 6 digit, input non-ASCII, secret kosong
# ---------------------------------------------------------

def test_verify_totp_rejects_non_ascii_and_invalid_secret():
    secret = mfa.new_secret()
    moment = int(time.time())
    code = f"{mfa.totp(secret, at=moment):06d}"
    arabic = "".join(chr(0x0660 + int(digit)) for digit in code)
    assert arabic.isdigit() and not arabic.isascii()

    # Digit Unicode tidak boleh sampai ke hmac.compare_digest (TypeError/500).
    assert mfa.verify_totp(secret, arabic, at=moment) is None
    # Pemisah spasi/tanda hubung tetap diterima seperti sebelumnya.
    assert mfa.verify_totp(secret, f"{code[:3]} {code[3:]}", at=moment) is not None
    assert mfa.verify_totp(secret, f"{code[:3]}-{code[3:]}", at=moment) is not None
    # Secret NULL/kosong/rusak tidak boleh menjadi kunci HMAC kosong.
    assert mfa.verify_totp(None, code, at=moment) is None
    assert mfa.verify_totp("", code, at=moment) is None
    assert mfa.verify_totp("bukan secret!", code, at=moment) is None


def test_verify_for_login_missing_secret_is_not_empty_hmac_key(fresh_db):
    user_id = _mk_user(fresh_db, "mfa-null-secret", "mfa-null-secret-password")
    with fresh_db._lock:
        conn = fresh_db.get_conn()
        conn.execute(
            "UPDATE users SET mfa_enabled=1, mfa_secret=NULL, "
            "mfa_recovery_hashes='[]', mfa_last_step=0 WHERE id=?",
            (user_id,),
        )
        conn.commit()

    user = fresh_db.find_user_by_id(user_id)
    empty_key_code = f"{mfa._hotp(b'', int(time.time()) // 30):06d}"
    assert mfa.verify_for_login(dict(user), empty_key_code) == (False, None)


def test_mfa_login_accepts_six_digit_recovery_code(client, fresh_db):
    user_id = _mk_user(
        fresh_db, "mfa-recovery-six", "mfa-recovery-six-password", role="admin"
    )
    response = _login(client, "mfa-recovery-six", "mfa-recovery-six-password")
    csrf = response.get_json()["csrf_token"]
    _enable_mfa(client, csrf, fresh_db, None)

    recovery_code = "23456-7ABCD"
    assert sum(ch.isdigit() for ch in recovery_code) == 6
    with fresh_db._lock:
        conn = fresh_db.get_conn()
        conn.execute(
            "UPDATE users SET mfa_recovery_hashes=? WHERE id=?",
            (json.dumps([mfa.hash_recovery_code(recovery_code)]), user_id),
        )
        conn.commit()

    client.post("/api/logout", headers={"X-CSRF-Token": csrf})
    ok = _complete_mfa_login(
        client, "mfa-recovery-six", "mfa-recovery-six-password", recovery_code
    )
    assert ok["ok"] is True
    assert client.get("/api/me").status_code == 200

    # Kode pemulihan tetap sekali pakai.
    client.post("/api/logout", headers={"X-CSRF-Token": ok["csrf_token"]})
    response = _login(client, "mfa-recovery-six", "mfa-recovery-six-password")
    csrf2 = response.get_json()["csrf_token"]
    replay = client.post(
        "/api/mfa/verify-login",
        json={"code": recovery_code},
        headers={"X-CSRF-Token": csrf2},
    )
    assert replay.status_code == 401


def test_mfa_login_rejects_non_ascii_code_without_500(client, fresh_db):
    _mk_user(fresh_db, "mfa-unicode", "mfa-unicode-password", role="admin")
    response = _login(client, "mfa-unicode", "mfa-unicode-password")
    csrf = response.get_json()["csrf_token"]
    _enable_mfa(client, csrf, fresh_db, None)
    client.post("/api/logout", headers={"X-CSRF-Token": csrf})

    response = _login(client, "mfa-unicode", "mfa-unicode-password")
    body = response.get_json()
    assert body["mfa_required"] is True
    bad = client.post(
        "/api/mfa/verify-login",
        json={"code": "\u0661\u0662\u0663\u0664\u0665\u0666"},
        headers={"X-CSRF-Token": body["csrf_token"]},
    )
    assert bad.status_code == 401
    assert bad.get_json()["code"] == "mfa_invalid"
