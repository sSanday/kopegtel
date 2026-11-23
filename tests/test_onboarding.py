"""Test onboarding: seed dummy device dan idle timeout."""
import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, "backend"))

import pytest


def test_seed_dummy_device_when_empty(fresh_db):
    import db
    import app as app_module
    # bootstrap sudah dipanggil di conftest; pastikan seed jalan
    app_module.bootstrap()
    devices = db.query_devices(per_page=100)
    names = [d["name"] for d in devices.get("devices", [])]
    assert "contoh-router" in names, "Seed device 'contoh-router' harus ada saat devices kosong"


def test_no_seed_when_devices_exist(fresh_db):
    import db
    import app as app_module
    # Buat satu device dulu
    db.create_device({
        "name": "existing", "ip": "192.168.1.1", "type": "router",
        "location": "", "poll_interval": 60, "maintenance": False,
        "snmp_enabled": False, "snmp_version": "v2c", "snmp_port": 161,
        "snmp_community": None, "snmp_v3_user": None,
        "snmp_v3_auth_proto": "SHA", "snmp_v3_auth_password": None,
        "snmp_v3_priv_proto": "AES", "snmp_v3_priv_password": None,
        "mikrotik_enabled": False, "mikrotik_port": 8728,
        "mikrotik_user": None, "mikrotik_password": None,
        "mikrotik_ssl": False, "mikrotik_ssl_fingerprint": None,
        "group_id": None, "parent_id": None,
    })
    count_before = len(db.query_devices(per_page=100).get("devices", []))
    app_module.bootstrap()
    count_after = len(db.query_devices(per_page=100).get("devices", []))
    assert count_after == count_before, "Seed tidak boleh ditambahkan jika sudah ada device"


def test_idle_timeout_default_120(fresh_db):
    import db
    value = db.get_setting("idle_timeout_minutes", "30")
    assert value == "120", f"Default idle timeout harus 120 menit, dapat {value}"
