"""Regresi M-12: ALLOWED_NETWORKS kosong tidak boleh berarti semua IP diizinkan.

Kebijakan default (kosong): hanya rentang privat (RFC1918 IPv4 dan ULA
fc00::/7 IPv6) yang boleh dipakai sebagai target monitoring — perangkat,
uji koneksi, pemindaian, dan penambahan massal. Loopback, link-local,
CGNAT, serta alamat publik ditolak. Dokumentasi .env.example menyebut
hanya rentang privat yang boleh; tes ini menjaganya.
"""

import ipaddress
import os
import sys

import pytest
from werkzeug.security import generate_password_hash

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, "backend"))

import app as app_module  # noqa: E402
import core.validation as validation  # noqa: E402
from core import net as net_module  # noqa: E402
from routes import discovery as discovery_module  # noqa: E402
from routes import devices as devices_module  # noqa: E402

OPERATOR = "operator-m12"
OPERATOR_PASSWORD = "operator-m12-password"

PRIVATE_IPS = ["10.1.2.3", "172.16.0.5", "172.31.255.255", "192.168.1.1", "192.168.30.1", "fd12::1"]
REJECTED_IPS = [
    "8.8.8.8",           # publik
    "1.1.1.1",           # publik
    "127.0.0.1",         # loopback
    "::1",               # loopback v6
    "169.254.1.1",       # link-local
    "fe80::dead:beef",   # link-local v6
    "100.64.0.1",        # CGNAT (RFC 6598)
    "192.0.2.1",         # TEST-NET-1 (dokumentasi)
    "198.51.100.5",      # TEST-NET-2
    "203.0.113.9",       # TEST-NET-3
    "172.15.0.1",        # di bawah /12
    "172.32.0.1",        # di atas /12
    "2001:4860:4860::8888",  # DNS publik Google
    "0.0.0.0",           # unspecified
    "224.0.0.1",         # multicast
]


# ---------------------------------------------------------
# Unit: kebijakan default & jaringan
# ---------------------------------------------------------

@pytest.mark.parametrize("ip", PRIVATE_IPS)
def test_default_allows_private_ips(ip):
    assert validation._ip_allowed(ipaddress.ip_address(ip)) is True


@pytest.mark.parametrize("ip", REJECTED_IPS)
def test_default_rejects_non_private_ips(ip):
    assert validation._ip_allowed(ipaddress.ip_address(ip)) is False


@pytest.mark.parametrize("cidr", [
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "192.168.30.0/24",
    "10.9.9.0/29", "fc00::/7", "fd00::/8",
])
def test_network_default_allowed_private(cidr):
    assert net_module.network_default_allowed(ipaddress.ip_network(cidr)) is True


@pytest.mark.parametrize("cidr", [
    "8.8.8.0/29", "127.0.0.0/8", "169.254.0.0/16", "100.64.0.0/10",
    "192.0.2.0/24", "203.0.113.0/29", "2001:4860:4860::/48",
    "fe80::/10", "::1/128",
    "172.0.0.0/11",      # lebih lebar dari /12
    "192.168.0.0/15",    # menjulur ke luar /16
    "10.0.0.0/7",        # lebih lebar dari /8
])
def test_network_default_rejected(cidr):
    assert net_module.network_default_allowed(ipaddress.ip_network(cidr)) is False


def test_explicit_allowed_networks_override_default(monkeypatch):
    custom = net_module.parse_allowed_networks("203.0.113.0/24")
    monkeypatch.setattr(validation, "ALLOWED_NETWORKS", custom)
    assert validation._ip_allowed(ipaddress.ip_address("203.0.113.5")) is True
    # Tanpa daftar eksplisit, IP publik pasti ditolak lewat kebijakan default.
    monkeypatch.setattr(validation, "ALLOWED_NETWORKS", [])
    assert validation._ip_allowed(ipaddress.ip_address("203.0.113.5")) is False


# ---------------------------------------------------------
# Fixtures untuk uji route
# ---------------------------------------------------------

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


@pytest.fixture(autouse=True)
def reset_rate_limit():
    devices_module._test_calls.clear()
    yield
    devices_module._test_calls.clear()


@pytest.fixture()
def collectors(monkeypatch):
    monkeypatch.setattr(discovery_module.ping_collector, "check_ping", lambda _ip: {"ok": False})
    monkeypatch.setattr(discovery_module.snmp_collector, "check_snmp", lambda _c: {"ok": False})
    monkeypatch.setattr(discovery_module.mikrotik_collector, "check_mikrotik", lambda _c: {"ok": False})
    calls = {"mikrotik": []}

    def fake_mikrotik(config):
        calls["mikrotik"].append(dict(config))
        return {"ok": True}

    monkeypatch.setattr(devices_module.ping_collector, "check_ping", lambda _ip: {"ok": True})
    monkeypatch.setattr(devices_module.mikrotik_collector, "check_mikrotik", fake_mikrotik)
    return calls


def _login(client):
    response = client.post(
        "/api/login", json={"username": OPERATOR, "password": OPERATOR_PASSWORD}
    )
    assert response.status_code == 200, response.get_json()
    return response.get_json()["csrf_token"]


def _device_payload(name, ip, **overrides):
    payload = {
        "name": name, "ip": ip, "type": "server", "location": "",
        "poll_interval": 60, "group_id": None, "parent_id": None,
        "maintenance": False, "snmp_enabled": False, "snmp_version": "v2c",
        "snmp_port": 161, "snmp_v3_auth_proto": "SHA", "snmp_v3_priv_proto": "AES",
        "mikrotik_enabled": False, "mikrotik_port": 8728,
        "mikrotik_user": None, "mikrotik_password": None, "mikrotik_ssl": False,
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------
# Route perangkat
# ---------------------------------------------------------

def test_create_device_rejects_public_and_loopback_ip(client, fresh_db, operator):
    csrf = _login(client)
    for ip in ("8.8.8.8", "127.0.0.1", "169.254.1.1", "203.0.113.50"):
        response = client.post(
            "/api/devices",
            json=_device_payload(f"mt-m12-{ip}", ip),
            headers={"X-CSRF-Token": csrf},
        )
        assert response.status_code == 400, (ip, response.get_json())


def test_create_device_accepts_private_ip(client, fresh_db, operator):
    csrf = _login(client)
    response = client.post(
        "/api/devices",
        json=_device_payload("mt-m12-ok", "192.168.30.99"),
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 201, response.get_json()


def test_update_device_rejects_move_to_public_ip(client, fresh_db, operator):
    device_id = fresh_db.create_device(
        _device_payload("mt-m12-edit", "192.168.30.77")
    )
    csrf = _login(client)
    response = client.put(
        f"/api/devices/{device_id}",
        json=_device_payload("mt-m12-edit", "8.8.4.4"),
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 400


def test_test_endpoint_rejects_public_ip_without_id(client, fresh_db, operator):
    csrf = _login(client)
    response = client.post(
        "/api/devices/test",
        json={"ip": "203.0.113.88"},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 400
    assert "ALLOWED_NETWORKS" in response.get_json()["error"]


# ---------------------------------------------------------
# Route discovery: scan & penambahan massal
# ---------------------------------------------------------

def test_scan_rejects_non_private_cidrs(client, fresh_db, operator, collectors):
    csrf = _login(client)
    for cidr in ("8.8.8.0/29", "127.0.0.0/29", "169.254.0.0/29", "203.0.113.0/29", "100.64.0.0/29"):
        response = client.post(
            "/api/discovery/scan",
            json={"cidr": cidr},
            headers={"X-CSRF-Token": csrf},
        )
        assert response.status_code == 403, (cidr, response.get_json())


def test_scan_accepts_private_cidr(client, fresh_db, operator, collectors):
    csrf = _login(client)
    response = client.post(
        "/api/discovery/scan",
        json={"cidr": "192.168.250.0/29"},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 202, response.get_json()


def test_scan_explicit_allowlist_applies_to_public(client, fresh_db, operator, collectors, monkeypatch):
    allowlist = net_module.parse_allowed_networks("203.0.113.0/24")
    monkeypatch.setattr(validation, "ALLOWED_NETWORKS", allowlist)
    monkeypatch.setattr(discovery_module, "ALLOWED_NETWORKS", allowlist)
    csrf = _login(client)
    allowed = client.post(
        "/api/discovery/scan",
        json={"cidr": "203.0.113.0/29"},
        headers={"X-CSRF-Token": csrf},
    )
    assert allowed.status_code == 202, allowed.get_json()

    rejected = client.post(
        "/api/discovery/scan",
        json={"cidr": "192.168.30.0/29"},
        headers={"X-CSRF-Token": csrf},
    )
    assert rejected.status_code == 403


def test_bulk_add_skips_public_and_loopback_ips(client, fresh_db, operator):
    csrf = _login(client)
    response = client.post(
        "/api/discovery/bulk-add",
        json={"ips": ["8.8.8.8", "127.0.0.1", "192.168.30.222"], "type": "server"},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 201, response.get_json()
    body = response.get_json()
    created_ips = {item["ip"] for item in body["created"]}
    skipped_ips = {item["ip"] for item in body["skipped"]}
    assert created_ips == {"192.168.30.222"}
    assert {"8.8.8.8", "127.0.0.1"} <= skipped_ips
