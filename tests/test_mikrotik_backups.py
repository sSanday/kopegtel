"""M6 — backup konfigurasi MikroTik via SSH: db, API, host key, diff, retensi."""

import base64
import hashlib
from datetime import datetime, timezone

import pytest
from werkzeug.security import generate_password_hash

import backups
import db as db_module
from collectors import mikrotik_backup


def _device_payload(name="mt-backup", ip="10.30.0.1"):
    return {
        "name": name, "ip": ip, "type": "mikrotik", "location": "",
        "poll_interval": 60, "group_id": None, "parent_id": None,
        "maintenance": False, "snmp_enabled": False, "snmp_version": "v2c",
        "snmp_port": 161, "snmp_v3_auth_proto": "SHA", "snmp_v3_priv_proto": "AES",
        "mikrotik_enabled": True, "mikrotik_port": 8728,
        "mikrotik_user": "api", "mikrotik_password": None, "mikrotik_ssl": False,
    }


def _make_user(fresh_db, username, role, password="test-password-123"):
    fresh_db.create_user(username, generate_password_hash(password), role=role, must_change_password=0)
    return username, password


def _login(client, username, password):
    response = client.post("/api/login", json={"username": username, "password": password})
    assert response.status_code == 200
    token = response.get_json().get("csrf_token")
    client.environ_base["HTTP_X_CSRF_TOKEN"] = token


@pytest.fixture()
def app_module(fresh_db):
    import app as module

    module.app.config["TESTING"] = True
    return module


def _client(app_module, fresh_db, role):
    username, password = _make_user(fresh_db, f"{role}-backup-test", role)
    client = app_module.app.test_client()
    _login(client, username, password)
    return client


def _seed_device(fresh_db, **settings):
    device_id = fresh_db.create_device(_device_payload())
    if settings:
        fresh_db.save_mt_device_settings(device_id, settings)
    return device_id


def test_backups_endpoints_are_admin_only(app_module, fresh_db):
    device_id = _seed_device(fresh_db)
    operator = _client(app_module, fresh_db, "operator")
    admin = _client(app_module, fresh_db, "admin")

    assert operator.get(f"/api/mikrotik/devices/{device_id}/backups").status_code == 403
    assert operator.post(f"/api/mikrotik/devices/{device_id}/backups", json={}).status_code == 403
    assert admin.get(f"/api/mikrotik/devices/{device_id}/backups").status_code == 200
    assert admin.get(f"/api/mikrotik/devices/{device_id}/backups").get_json()["backups"] == []


def test_backup_run_requires_ssh_configuration(app_module, fresh_db):
    device_id = _seed_device(fresh_db)
    admin = _client(app_module, fresh_db, "admin")
    response = admin.post(f"/api/mikrotik/devices/{device_id}/backups", json={})
    assert response.status_code == 400
    assert "SSH" in response.get_json()["error"]


def test_backup_run_saves_encrypted_payload_and_records_audit(app_module, fresh_db, monkeypatch):
    device_id = _seed_device(
        fresh_db,
        ssh_enabled=True, ssh_user="backup", ssh_password="rahasia-ssh",
        ssh_port=22, ssh_host_key="SHA256:abcdefghij1234567890abcdefghij1234567890ab",
    )
    monkeypatch.setattr("routes.mikrotik.MT_BACKUP_COOLDOWN", 0)
    monkeypatch.setattr(
        "backups.mikrotik_backup.run_export",
        lambda **kwargs: "/interface set ether1 comment=LAN\n/user add name=foo\n",
    )
    admin = _client(app_module, fresh_db, "admin")
    response = admin.post(f"/api/mikrotik/devices/{device_id}/backups", json={})
    assert response.status_code == 200, response.get_json()
    saved = response.get_json()["backup"]
    assert saved["size"] > 0

    stored = fresh_db.get_mt_backup(device_id, saved["id"])
    assert stored["content"].startswith("/interface set ether1")
    with fresh_db._lock:
        raw = fresh_db.get_conn().execute(
            "SELECT content FROM mt_backups WHERE id = ?", (saved["id"],)
        ).fetchone()
    assert raw["content"] != stored["content"]
    assert "ether1" not in raw["content"]

    listing = admin.get(f"/api/mikrotik/devices/{device_id}/backups").get_json()["backups"]
    assert len(listing) == 1
    assert listing[0]["sha256"] == saved["sha256"]

    audit = fresh_db.list_audit_log(action="mikrotik.backup")
    assert any(row["action"] == "mikrotik.backup" for row in audit["entries"])


def test_backup_retention_keeps_only_last_versions(app_module, fresh_db, monkeypatch):
    fresh_db.set_settings({"mt_backup_retention_versions": "2"})
    device_id = _seed_device(
        fresh_db,
        ssh_enabled=True, ssh_user="backup", ssh_password="rahasia-ssh",
        ssh_host_key="SHA256:abcdefghij1234567890abcdefghij1234567890ab",
    )
    admin = _client(app_module, fresh_db, "admin")
    monkeypatch.setattr("routes.mikrotik.MT_BACKUP_COOLDOWN", 0)

    contents = iter([
        "/interface set 1\n",
        "/interface set 2\n",
        "/interface set 3\n",
    ])

    class FakeExport:
        @staticmethod
        def run_export(**kwargs):
            return next(contents)

    monkeypatch.setattr("backups.mikrotik_backup", FakeExport)
    for _ in range(3):
        response = admin.post(f"/api/mikrotik/devices/{device_id}/backups", json={})
        assert response.status_code == 200, response.get_json()
    listing = admin.get(f"/api/mikrotik/devices/{device_id}/backups").get_json()["backups"]
    assert len(listing) == 2


def test_backup_diff_and_download(app_module, fresh_db):
    device_id = _seed_device(fresh_db)
    first = db_module.create_mt_backup(
        device_id, source="manual", filename="v1.rsc", size=10,
        sha256=hashlib.sha256(b"line-a\nline-b\n").hexdigest(),
        content_encrypted=db_module.encrypt_secret("line-a\nline-b\n"),
        created_at="2026-11-10T00:00:01Z",
    )
    second = db_module.create_mt_backup(
        device_id, source="manual", filename="v2.rsc", size=10,
        sha256=hashlib.sha256(b"line-a\nline-c\n").hexdigest(),
        content_encrypted=db_module.encrypt_secret("line-a\nline-c\n"),
        created_at="2026-11-10T00:00:02Z",
    )
    admin = _client(app_module, fresh_db, "admin")

    diff = admin.get(
        f"/api/mikrotik/devices/{device_id}/backups/{second['id']}/diff"
    ).get_json()
    assert diff["from"] == "v1.rsc"
    assert diff["to"] == "v2.rsc"
    assert "-line-b" in diff["diff"]
    assert "+line-c" in diff["diff"]

    response = admin.get(
        f"/api/mikrotik/devices/{device_id}/backups/{first['id']}/download"
    )
    assert response.status_code == 200
    assert "line-a" in response.get_data(as_text=True)
    assert response.headers["Content-Disposition"].startswith("attachment; filename=v1.rsc")

    audit = fresh_db.list_audit_log(action="mikrotik.backup.download")
    assert any(row["action"] == "mikrotik.backup.download" for row in audit["entries"])

    operator = _client(app_module, fresh_db, "operator")
    assert operator.get(
        f"/api/mikrotik/devices/{device_id}/backups/{first['id']}/download"
    ).status_code == 403


def test_host_key_fingerprint_parsing():
    import paramiko

    key = paramiko.RSAKey.generate(2048)
    blob = key.asbytes()
    expected_fp = "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).rstrip(b"=").decode("ascii")
    public_line = f"ssh-rsa {base64.b64encode(blob).decode('ascii')} perangkat-test"

    assert mikrotik_backup.parse_expected_fingerprint(expected_fp) == expected_fp
    assert mikrotik_backup.parse_expected_fingerprint(public_line) == expected_fp
    with pytest.raises(mikrotik_backup.BackupError):
        mikrotik_backup.parse_expected_fingerprint("MD5:aa:bb:cc:dd")
    with pytest.raises(mikrotik_backup.BackupError):
        mikrotik_backup.parse_expected_fingerprint("")


def test_scheduled_backup_runs_once_a_day(app_module, fresh_db, monkeypatch):
    import app as app_module

    from datetime import timedelta

    device_id = _seed_device(
        fresh_db,
        ssh_enabled=True, ssh_user="backup", ssh_password="rahasia-ssh",
        ssh_host_key="SHA256:abcdefghij1234567890abcdefghij1234567890ab",
        backup_enabled=True, backup_hour=(datetime.now(timezone.utc) + timedelta(hours=7)).hour,
    )
    created = []

    class FakeExport:
        @staticmethod
        def run_export(**kwargs):
            created.append(1)
            return "/interface set 1\n"

    monkeypatch.setattr("backups.mikrotik_backup", FakeExport)
    assert backups.run_due_backups() is True
    assert len(created) == 1
    assert backups.run_due_backups() is True
    assert len(created) == 1
