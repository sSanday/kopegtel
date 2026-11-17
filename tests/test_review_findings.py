"""Regresi tinjauan kode lanjutan (H-4, M-14..M-19, L-11..L-13).

Setiap tes menargetkan satu temuan yang sebelumnya bisa menghasilkan
HTTP 500, kredensial terkirim ke host lain, alert palsu, job poller basi,
atau masalah kualitas kode.
"""

import ast
import os
import sys
from datetime import datetime, timezone

import pytest
from werkzeug.security import generate_password_hash

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, "backend"))

import app as app_module  # noqa: E402
import db  # noqa: E402
import dbbackup  # noqa: E402
import poller  # noqa: E402
from routes import devices as devices_routes  # noqa: E402


@pytest.fixture()
def client():
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as client:
        yield client


def _mk_user(fresh_db, username="review-admin", password="review-admin-password"):
    fresh_db.create_user(
        username, generate_password_hash(password), role="admin", must_change_password=0
    )
    return username, password


def _login(client, username, password):
    response = client.post("/api/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.get_json()
    return response.get_json()["csrf_token"]


def _device_payload(name="perangkat-review", ip="10.0.0.1", **overrides):
    payload = {
        "name": name,
        "ip": ip,
        "type": "mikrotik",
        "location": "",
        "poll_interval": 60,
        "maintenance": False,
        "snmp_enabled": False,
        "snmp_version": "v2c",
        "snmp_community": None,
        "snmp_port": 161,
        "snmp_v3_user": None,
        "snmp_v3_auth_proto": "SHA",
        "snmp_v3_auth_password": None,
        "snmp_v3_priv_proto": "AES",
        "snmp_v3_priv_password": None,
        "mikrotik_enabled": False,
        "mikrotik_port": 8728,
        "mikrotik_user": None,
        "mikrotik_password": None,
        "mikrotik_ssl": False,
    }
    payload.update(overrides)
    return payload


def _ts():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------
# H-4: riwayat port optik memanggil _downsample yang tidak diimpor
# ---------------------------------------------------------


@pytest.mark.parametrize("range_name", ["24h", "7d"])
def test_optical_port_history_raw_range_does_not_500(client, fresh_db, range_name):
    device_id = fresh_db.create_device(_device_payload())
    for _ in range(5):
        fresh_db.record_optical_readings(
            device_id,
            _ts(),
            [
                {
                    "if_name": "sfp1",
                    "rx_dbm": -20.5,
                    "tx_dbm": -3.0,
                    "temp_c": 40.0,
                    "voltage_v": 3.3,
                    "bias_ma": 6.0,
                    "rx_loss": False,
                    "tx_fault": False,
                    "module_present": True,
                    "ddm_supported": True,
                }
            ],
        )
    fresh_db.upsert_optical_port_state(
        device_id, "sfp1", is_optical=True, ddm_supported=True, ts=_ts()
    )
    state = fresh_db.get_optical_port_state(device_id, "sfp1")

    username, password = _mk_user(fresh_db)
    _login(client, username, password)
    response = client.get(f"/api/optical/ports/{state['id']}/history?range={range_name}")

    assert response.status_code == 200, response.get_data(as_text=True)
    body = response.get_json()
    assert body["source"] == "raw"
    assert len(body["points"]) == 5
    assert body["points"][0]["rx_dbm"] == pytest.approx(-20.5)


# ---------------------------------------------------------
# M-14: IP berubah tidak boleh memakai kredensial lama
# ---------------------------------------------------------


def test_device_ip_change_requires_credentials_via_api(client, fresh_db):
    device_id = fresh_db.create_device(
        _device_payload(
            name="kredensial-ip",
            ip="10.0.0.1",
            mikrotik_enabled=True,
            mikrotik_user="admin",
            mikrotik_password="password-lama",
        )
    )
    username, password = _mk_user(fresh_db)
    csrf = _login(client, username, password)

    payload = _device_payload(
        name="kredensial-ip",
        ip="10.0.0.2",
        mikrotik_enabled=True,
        mikrotik_user="admin",
        mikrotik_password="",
    )
    response = client.put(
        f"/api/devices/{device_id}", json=payload, headers={"X-CSRF-Token": csrf}
    )
    assert response.status_code == 400
    assert "IP perangkat berubah" in response.get_json()["error"]

    stored = fresh_db.get_device(device_id, include_secrets=True)
    assert stored["ip"] == "10.0.0.1"
    assert fresh_db.decrypt_secret(stored["mikrotik_password"]) == "password-lama"

    payload["mikrotik_password"] = "password-baru"
    response = client.put(
        f"/api/devices/{device_id}", json=payload, headers={"X-CSRF-Token": csrf}
    )
    assert response.status_code == 200, response.get_data(as_text=True)
    stored = fresh_db.get_device(device_id, include_secrets=True)
    assert stored["ip"] == "10.0.0.2"
    assert fresh_db.decrypt_secret(stored["mikrotik_password"]) == "password-baru"


def test_update_device_clears_secrets_on_ip_change(fresh_db):
    device_id = fresh_db.create_device(
        _device_payload(
            name="kredensial-db",
            ip="10.0.0.1",
            snmp_enabled=True,
            snmp_community="community-lama",
            mikrotik_enabled=True,
            mikrotik_user="admin",
            mikrotik_password="password-lama",
            mikrotik_ssl_fingerprint="aa" * 32,
        )
    )
    existing = fresh_db.get_device(device_id, include_secrets=True)

    # IP sama + field secret kosong = pertahankan (perilaku lama tetap aman).
    unchanged = dict(existing)
    unchanged["mikrotik_password"] = ""
    unchanged["snmp_community"] = ""
    fresh_db.update_device(device_id, unchanged)
    stored = fresh_db.get_device(device_id, include_secrets=True)
    assert fresh_db.decrypt_secret(stored["mikrotik_password"]) == "password-lama"
    assert fresh_db.decrypt_secret(stored["snmp_community"]) == "community-lama"

    # IP berubah + field secret kosong/tidak dikirim = hapus, bukan pakai lama.
    changed = dict(stored)
    changed["ip"] = "10.0.0.9"
    changed["mikrotik_password"] = ""
    changed["snmp_community"] = ""
    changed.pop("mikrotik_ssl_fingerprint", None)
    fresh_db.update_device(device_id, changed)
    stored = fresh_db.get_device(device_id, include_secrets=True)
    assert stored["ip"] == "10.0.0.9"
    assert stored["mikrotik_password"] is None
    assert stored["snmp_community"] is None
    assert stored["mikrotik_ssl_fingerprint"] is None


# ---------------------------------------------------------
# M-15: interval profil baru harus langsung menjadwalkan ulang poller
# ---------------------------------------------------------


def test_profile_interval_change_reschedules_matching_devices(client, fresh_db, monkeypatch):
    device_id = fresh_db.create_device(_device_payload(name="profil-interval", type="mikrotik"))
    username, password = _mk_user(fresh_db)
    csrf = _login(client, username, password)

    scheduled = []
    monkeypatch.setattr(
        poller, "schedule_device", lambda device: scheduled.append(dict(device))
    )
    response = client.put(
        "/api/profiles/mikrotik",
        json={"interval_seconds": 120, "thresholds": {}},
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 200, response.get_data(as_text=True)
    assert scheduled == [{"id": device_id, "poll_interval": 120}]
    assert fresh_db.get_device(device_id)["poll_interval"] == 120


# ---------------------------------------------------------
# M-16: metrik hilang bukan bukti pulih
# ---------------------------------------------------------


def test_missing_metric_does_not_resolve_threshold_problem():
    device = {
        "id": 1,
        "name": "sensor-suhu",
        "threshold_temp_warn": 60,
        "threshold_temp_crit": 75,
    }
    previous = {"temp_c": 80.0}
    current = {"temp_c": None}

    assert poller._check_thresholds(device, previous, current) == []

    # Pembacaan valid yang benar-benar normal tetap menutup masalah.
    resolved = poller._check_thresholds(device, previous, {"temp_c": 40.0})
    assert len(resolved) == 1
    assert resolved[0]["kind"] == "temp_high"
    assert resolved[0]["resolved"] is True


# ---------------------------------------------------------
# M-17: ambang NULL + default suhu 60/75 (bukan 70/90)
# ---------------------------------------------------------


def _threshold_device(fresh_db):
    device_id = fresh_db.create_device(_device_payload(name="ambang-null"))
    username, password = _mk_user(fresh_db, "ambang-admin", "ambang-admin-password")
    return device_id, username, password


def test_threshold_endpoint_handles_null_columns(client, fresh_db, monkeypatch):
    device_id, username, password = _threshold_device(fresh_db)
    csrf = _login(client, username, password)

    real_get_device = db.get_device
    calls = {"n": 0}

    def fake_get_device(device_id_arg, include_secrets=False):
        calls["n"] += 1
        device = real_get_device(device_id_arg, include_secrets=include_secrets)
        if device is not None and calls["n"] == 1:
            device = dict(device)
            device["threshold_temp_warn"] = None
            device["threshold_temp_crit"] = None
        return device

    monkeypatch.setattr(db, "get_device", fake_get_device)
    response = client.put(
        f"/api/devices/{device_id}/thresholds",
        json={"temp_crit": 80},
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 200, response.get_data(as_text=True)
    row = fresh_db.get_conn().execute(
        "SELECT threshold_temp_warn, threshold_temp_crit FROM devices WHERE id=?",
        (device_id,),
    ).fetchone()
    assert row["threshold_temp_warn"] == 60
    assert row["threshold_temp_crit"] == 80


def test_threshold_endpoint_uses_temp_defaults_60_75(client, fresh_db, monkeypatch):
    device_id, username, password = _threshold_device(fresh_db)
    csrf = _login(client, username, password)

    real_get_device = db.get_device
    calls = {"n": 0}

    def fake_get_device(device_id_arg, include_secrets=False):
        calls["n"] += 1
        device = real_get_device(device_id_arg, include_secrets=include_secrets)
        if device is not None and calls["n"] == 1:
            device = dict(device)
            device.pop("threshold_temp_warn", None)
            device.pop("threshold_temp_crit", None)
        return device

    monkeypatch.setattr(db, "get_device", fake_get_device)
    # Dengan default lama 70/90, 65 ditolak; default poller yang benar 60/75
    # membuat 65 valid.
    response = client.put(
        f"/api/devices/{device_id}/thresholds",
        json={"temp_crit": 65},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200, response.get_data(as_text=True)


def test_threshold_endpoint_rejects_nan(client, fresh_db):
    device_id, username, password = _threshold_device(fresh_db)
    csrf = _login(client, username, password)
    response = client.put(
        f"/api/devices/{device_id}/thresholds",
        json={"temp_warn": float("nan")},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 400
    assert response.get_json().get("error")


# ---------------------------------------------------------
# M-18: poll manual gagal tidak boleh 500 tanpa pesan
# ---------------------------------------------------------


def test_manual_poll_failure_returns_json_502(client, fresh_db, monkeypatch):
    device_id = fresh_db.create_device(_device_payload(name="poll-gagal"))
    username, password = _mk_user(fresh_db)
    csrf = _login(client, username, password)
    devices_routes._poll_requests.clear()

    def boom(device_id_arg):
        raise RuntimeError("detail internal rahasia")

    monkeypatch.setattr(poller, "run_check", boom)
    response = client.post(
        f"/api/devices/{device_id}/poll", headers={"X-CSRF-Token": csrf}
    )

    assert response.status_code == 502
    body = response.get_json()
    assert body.get("error")
    assert "detail internal rahasia" not in response.get_data(as_text=True)


# ---------------------------------------------------------
# M-19: perangkat yang sudah dihapus harus dilepas dari scheduler
# ---------------------------------------------------------


def test_run_check_unschedules_deleted_device(fresh_db, monkeypatch):
    removed = []
    monkeypatch.setattr(poller, "unschedule_device", lambda device_id: removed.append(device_id))

    poller.run_check(999999)

    assert removed == [999999]


# ---------------------------------------------------------
# L-11: berkas backup harus dibatasi 0600
# ---------------------------------------------------------


def test_backup_sets_restrictive_permissions(fresh_db, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        dbbackup.os, "chmod", lambda path, mode: calls.append((str(path), mode))
    )

    target = dbbackup.run_backup(out_dir=str(tmp_path / "backups"), keep=2)

    assert calls == [(target, 0o600)]


# ---------------------------------------------------------
# L-12: indeks (device_id, id) untuk metrik terbaru
# ---------------------------------------------------------


def test_device_metrics_has_device_id_index(fresh_db):
    rows = fresh_db.get_conn().execute("PRAGMA index_list('device_metrics')").fetchall()
    assert "idx_metrics_device_id" in {row["name"] for row in rows}

    columns = fresh_db.get_conn().execute(
        "PRAGMA index_info('idx_metrics_device_id')"
    ).fetchall()
    assert [row["name"] for row in columns] == ["device_id", "id"]


# ---------------------------------------------------------
# L-13: import tak terpakai yang ditinjau sudah dihapus
# ---------------------------------------------------------


def test_reviewed_modules_have_no_unused_top_level_imports():
    relative_paths = (
        "backend/poller.py",
        "backend/prometheus.py",
        "tools/route_map.py",
    )
    problems = []
    for relative in relative_paths:
        path = os.path.join(BASE_DIR, relative)
        with open(path, encoding="utf-8") as handle:
            tree = ast.parse(handle.read(), filename=path)
        imported = {}
        for node in tree.body:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    name = alias.asname or alias.name.split(".")[0]
                    imported[name] = node.lineno
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name == "*":
                        continue
                    imported[alias.asname or alias.name] = node.lineno
        used = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        problems.extend(
            f"{relative}:{line}: {name}"
            for name, line in imported.items()
            if name not in used
        )
    assert problems == []
