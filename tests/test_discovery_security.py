"""Regresi H-3: kredensial scan tidak boleh disimpan ke database.

Kredensial SNMP/RouterOS hanya boleh hidup di memori thread pemindaian.
Database lama yang masih memiliki kolom kredensial harus dimigrasikan tanpa
meninggalkan salinan plaintext di nms.db maupun nms.db-wal.
"""

import os
import sqlite3
import sys
import threading

import pytest
from werkzeug.security import generate_password_hash

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, "backend"))

import app as app_module  # noqa: E402
import db  # noqa: E402
from routes import discovery as discovery_module  # noqa: E402

OPERATOR = "operator-h3"
OPERATOR_PASSWORD = "operator-h3-password"
SCAN_COMMUNITY = "community-h3-jangan-disimpan"
SCAN_USER = "user-h3-jangan-disimpan"
SCAN_PASSWORD = "password-h3-jangan-disimpan"
LEGACY_COLUMNS = (
    "id, cidr, status, total, completed, found, community, mikrotik_user,"
    " mikrotik_password, created_by, created_at, finished_at, results"
)


@pytest.fixture()
def client():
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as client:
        yield client


@pytest.fixture()
def operator(fresh_db):
    fresh_db.create_user(
        OPERATOR,
        generate_password_hash(OPERATOR_PASSWORD),
        role="operator",
        must_change_password=0,
    )
    return OPERATOR


def _login(client):
    response = client.post("/api/login", json={"username": OPERATOR, "password": OPERATOR_PASSWORD})
    assert response.status_code == 200, response.get_json()
    return response.get_json()["csrf_token"]


def _assert_secret_not_in_files(secret):
    for suffix in ("", "-wal", "-shm", "-journal"):
        path = db.DB_PATH + suffix
        if os.path.exists(path):
            with open(path, "rb") as handle:
                assert secret.encode("utf-8") not in handle.read(), f"secret tersisa di {path}"


def _legacy_db(path):
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """
            CREATE TABLE scan_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cidr TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'running',
                total INTEGER NOT NULL DEFAULT 0,
                completed INTEGER NOT NULL DEFAULT 0,
                found INTEGER NOT NULL DEFAULT 0,
                community TEXT,
                mikrotik_user TEXT,
                mikrotik_password TEXT,
                created_by TEXT NOT NULL DEFAULT '-',
                created_at TEXT NOT NULL,
                finished_at TEXT,
                results TEXT NOT NULL DEFAULT '[]'
            )
            """
        )
        conn.execute(
            f"INSERT INTO scan_jobs ({LEGACY_COLUMNS}) VALUES"
            " (1, '192.168.30.0/24', 'done', 254, 254, 3, ?, ?, ?, 'operator',"
            " '2026-11-15T00:00:00Z', '2026-11-15T00:01:00Z', '[{\"ip\": \"192.168.30.1\"}]')",
            (SCAN_COMMUNITY, SCAN_USER, SCAN_PASSWORD),
        )
        conn.commit()
    finally:
        conn.close()


def _point_db_at(monkeypatch, path):
    for target in (db, db.connection):
        monkeypatch.setattr(target, "DB_PATH", path)
        with db.connection._lock:
            if getattr(target, "_conn", None) is not None:
                target._conn.close()
            target._conn = None


def _close_db():
    with db.connection._lock:
        if db.connection._conn is not None:
            db.connection._conn.close()
            db.connection._conn = None


def test_scan_jobs_schema_has_no_credential_columns(fresh_db):
    columns = {row["name"] for row in fresh_db.get_conn().execute("PRAGMA table_info(scan_jobs)")}
    assert columns == set(fresh_db.SCAN_JOB_COLUMNS)
    assert not (columns & set(fresh_db.SCAN_JOB_SECRET_COLUMNS))


def test_start_scan_keeps_credentials_only_in_memory(client, fresh_db, operator, monkeypatch):
    seen = {}
    done = threading.Event()

    def fake_run(job_id, cidr, community, mikrotik_user, mikrotik_password):
        seen.update(
            {
                "job_id": job_id,
                "cidr": cidr,
                "community": community,
                "mikrotik_user": mikrotik_user,
                "mikrotik_password": mikrotik_password,
            }
        )
        done.set()

    monkeypatch.setattr(discovery_module, "_run_scan", fake_run)
    csrf = _login(client)

    response = client.post(
        "/api/discovery/scan",
        json={
            "cidr": "192.168.50.0/29",
            "community": SCAN_COMMUNITY,
            "mikrotik_user": SCAN_USER,
            "mikrotik_password": SCAN_PASSWORD,
        },
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 202, response.get_json()
    assert done.wait(timeout=2), "thread pemindaian tidak berjalan"
    job_id = response.get_json()["scan"]["id"]
    assert seen == {
        "job_id": job_id,
        "cidr": "192.168.50.0/29",
        "community": SCAN_COMMUNITY,
        "mikrotik_user": SCAN_USER,
        "mikrotik_password": SCAN_PASSWORD,
    }

    job = fresh_db.get_scan_job(job_id)
    assert job is not None
    assert not (set(job) & set(fresh_db.SCAN_JOB_SECRET_COLUMNS))
    assert SCAN_PASSWORD not in str(job)
    _assert_secret_not_in_files(SCAN_PASSWORD)
    _assert_secret_not_in_files(SCAN_COMMUNITY)
    _assert_secret_not_in_files(SCAN_USER)


def test_init_db_migrates_legacy_credentials_and_purges_bytes(tmp_path, monkeypatch):
    path = str(tmp_path / "legacy.db")
    _legacy_db(path)
    assert SCAN_PASSWORD.encode() in open(path, "rb").read()

    _point_db_at(monkeypatch, path)
    db.init_db()
    _close_db()

    check = sqlite3.connect(path)
    try:
        columns = {row[1] for row in check.execute("PRAGMA table_info(scan_jobs)")}
        assert not (columns & set(db.SCAN_JOB_SECRET_COLUMNS))
        assert check.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        row = check.execute("SELECT id, cidr, status, results FROM scan_jobs").fetchone()
        assert row[0] == 1
        assert row[1] == "192.168.30.0/24"
        assert row[2] == "done"
        assert "192.168.30.1" in row[3]
    finally:
        check.close()

    for suffix in ("", "-wal", "-shm", "-journal"):
        side = path + suffix
        if os.path.exists(side):
            assert SCAN_PASSWORD.encode() not in open(side, "rb").read(), f"secret tersisa di {side}"
            assert SCAN_COMMUNITY.encode() not in open(side, "rb").read()
            assert SCAN_USER.encode() not in open(side, "rb").read()


def test_scrub_scan_credentials_file_cleans_backup(tmp_path):
    path = str(tmp_path / "backup-lama.db")
    _legacy_db(path)

    assert db.scrub_scan_credentials_file(path) is True
    assert db.scrub_scan_credentials_file(path) is False

    check = sqlite3.connect(path)
    try:
        columns = {row[1] for row in check.execute("PRAGMA table_info(scan_jobs)")}
        assert not (columns & set(db.SCAN_JOB_SECRET_COLUMNS))
        assert check.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert check.execute("SELECT COUNT(*) FROM scan_jobs").fetchone()[0] == 1
    finally:
        check.close()

    for suffix in ("", "-wal", "-shm", "-journal"):
        side = path + suffix
        if os.path.exists(side):
            assert SCAN_PASSWORD.encode() not in open(side, "rb").read(), f"secret tersisa di {side}"
