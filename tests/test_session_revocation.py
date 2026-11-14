"""Regresi M-4: sesi lama harus gugur setelah password diganti atau direset.

Cookie sesi tidak menyimpan status di server, jadi guard membandingkan
``session_epoch`` di cookie dengan ``users.session_epoch``. Setiap perubahan
password menaikkan epoch; semua sesi lama (termasuk ``remember``) langsung
ditolak, sedangkan sesi yang sedang dipakai untuk mengganti password tetap
valid.
"""

import os
import sys

import pytest
from werkzeug.security import generate_password_hash

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, "backend"))

import app as app_module  # noqa: E402
import db  # noqa: E402


@pytest.fixture()
def client():
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as client:
        yield client


def _mk_user(username, password, role="admin", must_change_password=0):
    return db.create_user(
        username,
        generate_password_hash(password),
        role=role,
        must_change_password=must_change_password,
    )


def _login(client, username, password, remember=False, ip="198.51.100.50"):
    return client.post(
        "/api/login",
        json={"username": username, "password": password, "remember": remember},
        environ_base={"REMOTE_ADDR": ip},
    )


def _csrf(response):
    return response.get_json()["csrf_token"]


def _new_client():
    return app_module.app.test_client()


def test_own_password_change_revokes_other_sessions(client, fresh_db):
    username, old = "m4-user", "m4-user-password-1"
    new = "m4-user-password-2"
    _mk_user(username, old)

    other = _new_client()
    current = _login(client, username, old)
    stolen = _login(other, username, old)
    assert current.status_code == 200, current.get_json()
    assert stolen.status_code == 200, stolen.get_json()

    changed = client.post(
        "/api/users/me/password",
        json={"current_password": old, "new_password": new},
        headers={"X-CSRF-Token": _csrf(current)},
    )
    assert changed.status_code == 200, changed.get_json()

    # Sesi yang dipakai mengganti password tetap hidup.
    assert client.get("/api/me").status_code == 200

    revoked = other.get("/api/me")
    assert revoked.status_code == 401
    assert revoked.get_json()["code"] == "session_revoked"

    assert _login(other, username, old).status_code == 401
    assert _login(other, username, new).status_code == 200


def test_admin_password_reset_revokes_target_sessions(client, fresh_db):
    _mk_user("m4-admin", "m4-admin-password-1")
    target_id = _mk_user("m4-target", "m4-target-password-1", role="viewer")

    target = _new_client()
    assert _login(target, "m4-target", "m4-target-password-1").status_code == 200
    admin = _login(client, "m4-admin", "m4-admin-password-1")
    assert admin.status_code == 200, admin.get_json()

    reset = client.put(
        f"/api/users/{target_id}",
        json={"password": "m4-target-password-2"},
        headers={"X-CSRF-Token": _csrf(admin)},
    )
    assert reset.status_code == 200, reset.get_json()

    revoked = target.get("/api/me")
    assert revoked.status_code == 401
    assert revoked.get_json()["code"] == "session_revoked"

    assert _login(target, "m4-target", "m4-target-password-1").status_code == 401
    relogin = _login(target, "m4-target", "m4-target-password-2")
    assert relogin.status_code == 200, relogin.get_json()
    assert relogin.get_json()["must_change_password"] is True

    # Sesi admin yang melakukan reset tidak ikut gugur.
    assert client.get("/api/me").status_code == 200


def test_legacy_cookie_without_epoch_is_rejected(client, fresh_db):
    username, password = "m4-legacy", "m4-legacy-password-1"
    _mk_user(username, password)
    assert _login(client, username, password).status_code == 200

    with client.session_transaction() as sess:
        sess.pop("session_epoch", None)

    response = client.get("/api/me")
    assert response.status_code == 401
    assert response.get_json()["code"] == "session_revoked"

    assert _login(client, username, password).status_code == 200


def test_remember_session_is_revoked_too(client, fresh_db):
    username, old = "m4-remember", "m4-remember-password-1"
    user_id = _mk_user(username, old)

    login = _login(client, username, old, remember=True)
    assert login.status_code == 200
    with client.session_transaction() as sess:
        assert sess.permanent is True

    db.update_user(
        user_id,
        password_hash=generate_password_hash("m4-remember-password-2"),
    )

    response = client.get("/api/me")
    assert response.status_code == 401
    assert response.get_json()["code"] == "session_revoked"


def test_password_update_bumps_epoch_at_the_database_boundary(fresh_db):
    user_id = _mk_user("m4-epoch", "m4-epoch-password-1")
    before = fresh_db.find_user_by_id(user_id)["session_epoch"]

    fresh_db.update_user(user_id, role="viewer")
    fresh_db.update_user(user_id, must_change_password=1)
    assert fresh_db.find_user_by_id(user_id)["session_epoch"] == before

    fresh_db.update_user(
        user_id,
        password_hash=generate_password_hash("m4-epoch-password-2"),
    )
    assert fresh_db.find_user_by_id(user_id)["session_epoch"] == before + 1


def test_password_reset_cancels_pending_mfa_login(client, fresh_db):
    username, old = "m4-mfa", "m4-mfa-password-1"
    user_id = _mk_user(username, old)
    with fresh_db._lock:
        conn = fresh_db.get_conn()
        conn.execute("UPDATE users SET mfa_enabled=1 WHERE id=?", (user_id,))
        conn.commit()

    pending = _login(client, username, old)
    assert pending.status_code == 200, pending.get_json()
    body = pending.get_json()
    assert body.get("mfa_required") is True

    fresh_db.update_user(
        user_id,
        password_hash=generate_password_hash("m4-mfa-password-2"),
    )

    verify = client.post(
        "/api/mfa/verify-login",
        json={"code": "000000"},
        headers={"X-CSRF-Token": body["csrf_token"]},
    )
    assert verify.status_code == 401
    assert verify.get_json()["code"] == "mfa_pending_expired"


def test_admin_self_reset_keeps_current_session(client, fresh_db):
    admin_id = _mk_user("m4-self", "m4-self-password-1")
    login = _login(client, "m4-self", "m4-self-password-1")
    assert login.status_code == 200

    reset = client.put(
        f"/api/users/{admin_id}",
        json={"password": "m4-self-password-2"},
        headers={"X-CSRF-Token": _csrf(login)},
    )
    assert reset.status_code == 200, reset.get_json()

    assert client.get("/api/me").status_code == 200
    assert _login(_new_client(), "m4-self", "m4-self-password-1").status_code == 401
