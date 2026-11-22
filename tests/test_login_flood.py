"""Regresi M-3: flooding login tanpa autentikasi dan batas ukuran request.

- Batas gagal login berlaku per IP, bukan hanya per (username, IP), sehingga
  mengganti username tidak menghindari blokir.
- Audit gagal login tidak ditulis satu baris per percobaan dan field-nya
  dipotong, jadi database tidak bisa dibengkakkan lewat username raksasa.
- Body request dibatasi; endpoint login/MFA memakai batas yang lebih kecil.
"""

import os
import sys
from datetime import datetime, timedelta, timezone

import pytest
from werkzeug.security import generate_password_hash

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, "backend"))

import app as app_module  # noqa: E402
import auth  # noqa: E402
import db  # noqa: E402


@pytest.fixture()
def client():
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as client:
        yield client


def _mk_user(username="m3-admin", password="m3-admin-password", role="admin"):
    db.create_user(
        username,
        generate_password_hash(password),
        role=role,
        must_change_password=0,
    )
    return username, password


def _fail(client, username, ip="198.51.100.10", password="salah-password-1"):
    return client.post(
        "/api/login",
        json={"username": username, "password": password},
        environ_base={"REMOTE_ADDR": ip},
    )


def _login(client, username, password, ip="198.51.100.10"):
    return client.post(
        "/api/login",
        json={"username": username, "password": password},
        environ_base={"REMOTE_ADDR": ip},
    )


def _audit_rows(action="auth.login_failed"):
    return db.list_audit_log(action=action, limit=500)["entries"]


def _attempt_rows():
    with db._lock:
        return db.get_conn().execute(
            "SELECT username, ip, failed_at FROM login_attempts ORDER BY id"
        ).fetchall()


def test_per_ip_limit_blocks_username_rotation(client, fresh_db, monkeypatch):
    monkeypatch.setattr(auth, "LOGIN_IP_MAX_FAILED", 3)

    for index in range(3):
        response = _fail(client, f"spray-{index}")
        assert response.status_code == 401, response.get_json()

    blocked = _fail(client, "spray-3")
    assert blocked.status_code == 429
    assert blocked.get_json()["code"] == "login_blocked"
    assert blocked.headers["Retry-After"] == str(auth.LOGIN_WINDOW_MINUTES * 60)

    # IP lain tidak ikut terblokir oleh kegagalan IP pertama.
    other_ip = _fail(client, "spray-4", ip="198.51.100.11")
    assert other_ip.status_code == 401

    assert len(_attempt_rows()) == 4  # 3 dari IP pertama + 1 dari IP kedua
    rows = _audit_rows()
    ip_rows = [row for row in rows if row["ip"] == "198.51.100.10"]
    # Hanya sinyal awal dan saat ambang IP tercapai; percobaan lain tidak
    # menulis audit_log.
    assert len(ip_rows) == 2
    assert "percobaan pertama" in ip_rows[-1]["summary"].lower()
    assert "ip diblokir" in ip_rows[0]["summary"].lower()


def test_login_audit_is_bounded_for_unique_usernames(client, fresh_db, monkeypatch):
    monkeypatch.setattr(auth, "LOGIN_IP_MAX_FAILED", 50)

    for index in range(20):
        response = _fail(client, f"unique-{index}")
        assert response.status_code == 401

    assert len(_attempt_rows()) == 20
    assert len(_audit_rows()) == 1


def test_login_audit_has_global_cap(client, fresh_db, monkeypatch):
    monkeypatch.setattr(auth, "LOGIN_AUDIT_MAX_PER_WINDOW", 1)

    assert _fail(client, "cap-a", ip="198.51.100.20").status_code == 401
    assert _fail(client, "cap-b", ip="198.51.100.21").status_code == 401

    assert len(_attempt_rows()) == 2
    assert len(_audit_rows()) == 1


def test_oversized_login_fields_are_truncated_before_storage(client, fresh_db):
    huge_username = "u" * 5000
    response = _fail(client, huge_username, password="x")
    assert response.status_code == 401

    rows = _attempt_rows()
    assert len(rows) == 1
    assert len(rows[0]["username"]) == auth.USERNAME_MAX_LENGTH

    audits = _audit_rows()
    assert audits
    assert all(len(row["username"]) <= auth.USERNAME_MAX_LENGTH for row in audits)

    # Password raksasa juga tidak boleh memicu pemeriksaan hash atau disimpan.
    long_password = client.post(
        "/api/login",
        json={"username": "m3-admin", "password": "p" * 5000},
        environ_base={"REMOTE_ADDR": "198.51.100.22"},
    )
    assert long_password.status_code == 401
    assert all("p" * 100 not in (row["username"] or "") for row in _attempt_rows())


def test_audit_fields_are_truncated_at_the_database_boundary(fresh_db):
    fresh_db.add_audit_log(
        None,
        "u" * 10000,
        "i" * 10000,
        "a" * 10000,
        "o" * 10000,
        "s" * 10000,
    )
    with fresh_db._lock:
        row = fresh_db.get_conn().execute(
            "SELECT username, ip, action, object, summary FROM audit_log"
        ).fetchone()

    assert len(row["username"]) == fresh_db.AUDIT_USERNAME_MAX
    assert len(row["ip"]) == fresh_db.AUDIT_IP_MAX
    assert len(row["action"]) == fresh_db.AUDIT_ACTION_MAX
    assert len(row["object"]) == fresh_db.AUDIT_OBJECT_MAX
    assert len(row["summary"]) == fresh_db.AUDIT_SUMMARY_MAX


def test_login_attempts_older_than_retention_are_pruned(fresh_db):
    old = (datetime.now(timezone.utc) - timedelta(days=2)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    with fresh_db._lock:
        conn = fresh_db.get_conn()
        conn.execute(
            "INSERT INTO login_attempts (username, ip, failed_at) VALUES (?,?,?)",
            ("lama", "198.51.100.30", old),
        )
        conn.commit()

    fresh_db.record_login_failure("baru", "198.51.100.30", 10)

    rows = _attempt_rows()
    assert [row["username"] for row in rows] == ["baru"]


def test_login_body_limit_returns_json_413(client, fresh_db):
    body = (
        b'{"username":"'
        + b"a" * (auth.LOGIN_MAX_BODY_BYTES + 100)
        + b'","password":"x"}'
    )
    response = client.post(
        "/api/login", data=body, content_type="application/json"
    )

    assert response.status_code == 413
    assert response.get_json()["code"] == "request_too_large"
    assert _attempt_rows() == []
    assert _audit_rows() == []


def test_global_request_body_limit_applies_to_authenticated_api(client, fresh_db):
    username, password = _mk_user()
    login = _login(client, username, password)
    assert login.status_code == 200, login.get_json()
    csrf = login.get_json()["csrf_token"]

    limit = int(app_module.app.config["MAX_CONTENT_LENGTH"])
    assert limit > 0
    body = b'{"name":"' + b"x" * (limit + 100) + b'"}'
    response = client.post(
        "/api/groups",
        data=body,
        content_type="application/json",
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 413
    assert response.get_json()["code"] == "request_too_large"
