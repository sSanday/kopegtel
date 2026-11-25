"""Regresi H-1: uji koneksi tidak boleh memakai kredensial tersimpan ke tujuan pilihan klien."""

import os
import sys

import pytest
from werkzeug.security import generate_password_hash

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, "backend"))

import app as app_module  # noqa: E402
from routes import devices as devices_module  # noqa: E402

OPERATOR = "operator-h1"
OPERATOR_PASSWORD = "operator-h1-password"
DEVICE_IP = "192.168.30.1"
SNMP_COMMUNITY = "community-rahasia-h1"
MIKROTIK_PASSWORD = "password-rahasia-h1"


@pytest.fixture()
def client():
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as client:
        yield client


@pytest.fixture(autouse=True)
def reset_rate_limit():
    devices_module._test_calls.clear()
    yield
    devices_module._test_calls.clear()


@pytest.fixture()
def operator(fresh_db):
    fresh_db.create_user(
        OPERATOR,
        generate_password_hash(OPERATOR_PASSWORD),
        role="operator",
        must_change_password=0,
    )
    return OPERATOR


@pytest.fixture()
def collectors(monkeypatch):
    calls = {"ping": [], "snmp": [], "mikrotik": []}

    def fake_ping(ip):
        calls["ping"].append(ip)
        return {"ok": True}

    def fake_snmp(config):
        calls["snmp"].append(dict(config))
        return {"ok": True}

    def fake_mikrotik(config):
        calls["mikrotik"].append(dict(config))
        return {"ok": True}

    monkeypatch.setattr(devices_module.ping_collector, "check_ping", fake_ping)
    monkeypatch.setattr(devices_module.snmp_collector, "check_snmp", fake_snmp)
    monkeypatch.setattr(devices_module.mikrotik_collector, "check_mikrotik", fake_mikrotik)
    return calls


def _login(client):
    response = client.post("/api/login", json={"username": OPERATOR, "password": OPERATOR_PASSWORD})
    assert response.status_code == 200, response.get_json()
    return response.get_json()["csrf_token"]


def _device(fresh_db):
    return fresh_db.create_device({
        "name": "Router H-1", "ip": DEVICE_IP, "type": "mikrotik", "location": "",
        "poll_interval": 60, "group_id": None, "parent_id": None, "maintenance": False,
        "snmp_enabled": True, "snmp_version": "v2c", "snmp_port": 161,
        "snmp_community": SNMP_COMMUNITY,
        "snmp_v3_auth_proto": "SHA", "snmp_v3_priv_proto": "AES",
        "mikrotik_enabled": True, "mikrotik_port": 8728,
        "mikrotik_user": "api", "mikrotik_password": MIKROTIK_PASSWORD,
        "mikrotik_ssl": False,
    })


def test_operator_cannot_send_stored_credentials_to_foreign_ip(client, fresh_db, operator, collectors):
    device_id = _device(fresh_db)
    csrf = _login(client)

    response = client.post(
        "/api/devices/test",
        json={
            "id": device_id,
            "ip": "203.0.113.50",
            "snmp_enabled": True,
            "snmp_port": 161,
            "mikrotik_enabled": True,
            "mikrotik_port": 8728,
        },
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 400
    assert "IP uji" in response.get_json()["error"]
    assert collectors == {"ping": [], "snmp": [], "mikrotik": []}


@pytest.mark.parametrize(
    "extra",
    [
        {"snmp_enabled": True, "snmp_port": 1161},
        {"mikrotik_enabled": True, "mikrotik_port": 18728},
    ],
)
def test_operator_cannot_send_stored_credentials_to_foreign_port(
    client, fresh_db, operator, collectors, extra
):
    device_id = _device(fresh_db)
    csrf = _login(client)

    response = client.post(
        "/api/devices/test",
        json={"id": device_id, "ip": DEVICE_IP, **extra},
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 400
    assert "port perangkat tersimpan" in response.get_json()["error"].lower()
    assert collectors == {"ping": [], "snmp": [], "mikrotik": []}


def test_stored_credentials_only_reach_stored_device(client, fresh_db, operator, collectors):
    device_id = _device(fresh_db)
    csrf = _login(client)

    response = client.post(
        "/api/devices/test",
        json={
            "id": device_id,
            "ip": DEVICE_IP,
            "snmp_enabled": True,
            "snmp_port": 161,
            "mikrotik_enabled": True,
            "mikrotik_port": 8728,
        },
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 200
    assert collectors["ping"] == [DEVICE_IP]

    snmp_config = collectors["snmp"][0]
    assert snmp_config["ip"] == DEVICE_IP
    assert snmp_config["port"] == 161
    assert snmp_config["community"] == SNMP_COMMUNITY

    mikrotik_config = collectors["mikrotik"][0]
    assert mikrotik_config["ip"] == DEVICE_IP
    assert mikrotik_config["port"] == 8728
    assert mikrotik_config["password"] == MIKROTIK_PASSWORD


def test_new_device_with_existing_ip_is_rejected_before_testing(
    client, fresh_db, operator, collectors
):
    """Uji koneksi pada form Tambah dengan IP yang sudah terdaftar harus
    diarahkan ke Edit perangkat, bukan diuji dengan community kosong/salah."""
    _device(fresh_db)
    csrf = _login(client)

    response = client.post(
        "/api/devices/test",
        json={
            "name": "Duplikat",
            "ip": DEVICE_IP,
            "type": "mikrotik",
            "snmp_enabled": True,
            "snmp_version": "v2c",
            "snmp_port": 161,
            "snmp_community": "public",
            "mikrotik_enabled": False,
        },
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 400
    error = response.get_json()["error"]
    assert "sudah terdaftar" in error
    assert "Router H-1" in error
    assert collectors == {"ping": [], "snmp": [], "mikrotik": []}
