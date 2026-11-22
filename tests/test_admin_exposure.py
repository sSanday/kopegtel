"""Regresi M-7: data admin tidak bocor ke peran lain dan statistik tidak berat."""

import os
import threading

import pytest
from werkzeug.security import generate_password_hash

import app as app_module
import db as db_module
from routes import admin as admin_module

PASSWORD = "admin-test-password"


@pytest.fixture()
def client():
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as client:
        yield client


def _mk_user(username, role):
    db_module.create_user(
        username,
        generate_password_hash(PASSWORD),
        role=role,
        must_change_password=0,
    )


def _login(client, username):
    response = client.post(
        "/api/login", json={"username": username, "password": PASSWORD}
    )
    assert response.status_code == 200, response.get_json()
    return response.get_json()["csrf_token"]


def test_settings_non_admin_only_gets_safe_fields(fresh_db, client):
    _mk_user("adminm7", "admin")
    _mk_user("viewerm7", "viewer")
    fresh_db.set_settings(
        {
            "db_backup_dir": r"C:\rahasia\backup",
            "metrics_enabled": "1",
        }
    )

    _login(client, "viewerm7")
    response = client.get("/api/settings")
    assert response.status_code == 200
    settings = response.get_json()["settings"]
    assert settings["default_poll_interval"] == "60"
    assert settings["org_name"]
    assert settings["timezone"]
    for key in (
        "db_backup_dir",
        "metrics_enabled",
        "retention_raw_days",
        "retention_preview",
        "mfa_required_roles",
        "ingest_rate_limit_per_sec",
    ):
        assert key not in settings


def test_settings_admin_still_gets_internal_fields(fresh_db, client):
    _mk_user("adminm7", "admin")
    fresh_db.set_settings({"db_backup_dir": r"C:\rahasia\backup"})

    _login(client, "adminm7")
    response = client.get("/api/settings")
    assert response.status_code == 200
    settings = response.get_json()["settings"]
    assert settings["db_backup_dir"] == r"C:\rahasia\backup"
    assert "retention_preview" in settings


def test_settings_non_admin_does_not_run_retention_counts(
    fresh_db, client, monkeypatch
):
    _mk_user("viewerm7", "viewer")
    calls = []
    monkeypatch.setattr(
        admin_module,
        "_retention_preview",
        lambda: calls.append(1) or {"checks": 999},
    )

    _login(client, "viewerm7")
    response = client.get("/api/settings")
    assert response.status_code == 200
    assert calls == []


def test_system_info_non_admin_hides_internal_fields(fresh_db, client, monkeypatch):
    _mk_user("viewerm7", "viewer")
    calls = []
    monkeypatch.setattr(
        db_module,
        "table_counts",
        lambda: calls.append(1) or {"users": 999},
    )

    _login(client, "viewerm7")
    response = client.get("/api/reports/system-info")
    assert response.status_code == 200
    body = response.get_json()
    assert body["app_version"]
    assert body["org_name"]
    assert body["timezone"]
    assert "poller" in body
    assert "python_version" not in body
    assert "flask_version" not in body
    assert "db_path" not in body
    assert "db_size_bytes" not in body
    assert "table_counts" not in body
    assert calls == []


def test_system_info_admin_gets_internal_fields(fresh_db, client):
    _mk_user("adminm7", "admin")
    _login(client, "adminm7")

    response = client.get("/api/reports/system-info")
    assert response.status_code == 200
    body = response.get_json()
    assert body["python_version"]
    assert body["flask_version"]
    assert body["db_path"] == os.path.basename(fresh_db.connection.DB_PATH)
    assert body["table_counts"]["users"] >= 1


def test_table_counts_does_not_wait_for_global_db_lock(fresh_db):
    """COUNT(*) memakai koneksi baca terpisah, bukan lock global."""
    acquired = threading.Event()
    release = threading.Event()

    def hold_lock():
        with fresh_db._lock:
            acquired.set()
            release.wait(10)

    holder = threading.Thread(target=hold_lock)
    holder.start()
    try:
        assert acquired.wait(5)
        result = []
        worker = threading.Thread(target=lambda: result.append(fresh_db.table_counts()))
        worker.start()
        worker.join(5)
        assert result, "table_counts tertahan oleh _lock global"
        assert result[0]["devices"] == 0
    finally:
        release.set()
        holder.join(5)


def test_retention_preview_cache_reuses_result(fresh_db, monkeypatch):
    admin_module._invalidate_retention_cache()
    calls = []

    def fake_preview():
        calls.append(1)
        return {"checks": 1}

    monkeypatch.setattr(admin_module, "_retention_preview", fake_preview)
    first = admin_module._cached_retention_preview()
    second = admin_module._cached_retention_preview()

    assert first == {"checks": 1}
    assert second == {"checks": 1}
    assert calls == [1]
