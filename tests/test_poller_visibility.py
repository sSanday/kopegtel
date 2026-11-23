"""Test poller visibility: last_checked & check_reason."""
import os, sys
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, "backend"))


def _mk_device(db, name="dev", ip="192.168.99.1"):
    return db.create_device({
        "name": name, "ip": ip, "type": "router", "location": "", "poll_interval": 60,
        "maintenance": False, "snmp_enabled": False, "snmp_version": "v2c", "snmp_port": 161,
        "snmp_community": None, "snmp_v3_user": None, "snmp_v3_auth_proto": "SHA",
        "snmp_v3_auth_password": None, "snmp_v3_priv_proto": "AES", "snmp_v3_priv_password": None,
        "mikrotik_enabled": False, "mikrotik_port": 8728, "mikrotik_user": None,
        "mikrotik_password": None, "mikrotik_ssl": False, "mikrotik_ssl_fingerprint": None,
        "group_id": None, "parent_id": None,
    })


def test_device_without_check_has_null_last_checked(fresh_db):
    import db
    did = _mk_device(db)
    dev = db.get_device(did)
    assert "last_checked" in dev
    assert dev["last_checked"] is None
    assert dev["check_reason"] is None


def test_device_with_check_exposes_reason(fresh_db):
    import db
    did = _mk_device(db)
    db.record_check(did, "offline", None, {"ping": {"ok": False, "reason": "timeout", "detail": "tidak merespons ping"}}, maintenance=False)
    dev = db.get_device(did)
    assert dev["last_checked"] is not None
    assert "ping" in (dev["check_reason"] or "").lower() or "merespons" in (dev["check_reason"] or "").lower()


def test_poller_status_returns_expected_keys(fresh_db):
    import poller
    status = poller.poller_status()
    for key in ("workers", "interval", "last_check_at", "last_error", "heartbeat", "scheduled_devices"):
        assert key in status, f"key {key} hilang"
    assert isinstance(status["workers"], int) and status["workers"] >= 1
