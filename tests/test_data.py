"""Ketersediaan, SLA, masalah (open/resolve), retensi — pada database sementara."""

from datetime import datetime, timedelta, timezone

import pytest

import db as db_module


def _naive_local(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _mk(name, ip, device_type="mikrotik"):
    return db_module.create_device(
        {
            "name": name,
            "ip": ip,
            "type": device_type,
            "location": "",
            "poll_interval": 60,
            "group_id": None,
            "parent_id": None,
            "maintenance": False,
            "snmp_enabled": False,
            "snmp_version": "v2c",
            "snmp_port": 161,
            "snmp_v3_auth_proto": "SHA",
            "snmp_v3_priv_proto": "AES",
            "mikrotik_enabled": False,
            "mikrotik_port": 8728,
            "mikrotik_user": None,
            "mikrotik_password": None,
            "mikrotik_ssl": False,
        }
    )


def test_availability_excludes_maintenance(fresh_db):
    device_id = _mk("avail-test", "10.0.0.10")
    for i in range(10):
        db_module.record_check(device_id, "online", 1.0, {})
    for _ in range(2):
        db_module.record_check(device_id, "offline", None, {}, maintenance=True)
    pct = db_module.availability(device_id, 1)["pct"]
    assert pct == 100.0


def test_availability_counts_degraded_as_up(fresh_db):
    device_id = _mk("degraded-test", "10.0.0.11")
    db_module.record_check(device_id, "online", 1.0, {})
    db_module.record_check(device_id, "degraded", 300.0, {})
    db_module.record_check(device_id, "offline", None, {})
    pct = db_module.availability(device_id, 1)["pct"]
    assert pct == pytest.approx(66.67, abs=0.01)


def test_service_sla_all_mode(fresh_db):
    a = _mk("svc-a", "10.0.0.20")
    b = _mk("svc-b", "10.0.0.21")
    service_id = db_module.create_service({"name": "Svc All", "division": "IT", "sla_target": 99.0, "mode": "all", "description": ""})
    db_module.set_service_devices(service_id, [a, b])
    ts_old = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    ts_now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    # Siklus 1: keduanya up. Siklus 2 (ts eksplisit sama): satu down.
    for ts, a_status, b_status in [(ts_old, "online", "online"), (ts_now, "online", "offline")]:
        with db_module._lock:
            conn = db_module.get_conn()
            conn.execute(
                "INSERT INTO device_checks (device_id, checked_at, status, latency_ms, detail_json, maintenance) VALUES (?,?,?,?,?,0)",
                (a, ts, a_status, 1.0, "{}"),
            )
            conn.execute(
                "INSERT INTO device_checks (device_id, checked_at, status, latency_ms, detail_json, maintenance) VALUES (?,?,?,?,?,0)",
                (b, ts, b_status, 1.0, "{}"),
            )
            conn.commit()
    stats = db_module.service_availability(service_id, 1)
    assert stats["cycles"] == 2
    assert stats["availability_pct"] == 50.0
    assert stats["down_cycles"] == 1


def test_service_sla_any_mode(fresh_db):
    a = _mk("svc-any-a", "10.0.0.22")
    b = _mk("svc-any-b", "10.0.0.23")
    service_id = db_module.create_service({"name": "Svc Any", "division": "IT", "sla_target": 99.0, "mode": "any", "description": ""})
    db_module.set_service_devices(service_id, [a, b])
    # Stempel waktu eksplisit agar kedua pemeriksaan berada pada siklus yang sama
    # (record_check memakai detik — rentang detik membuat tes tidak stabil).
    ts_now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with db_module._lock:
        conn = db_module.get_conn()
        for did, status in ((a, "offline"), (b, "online")):
            conn.execute(
                "INSERT INTO device_checks (device_id, checked_at, status, latency_ms, detail_json, maintenance) VALUES (?,?,?,?,?,0)",
                (did, ts_now, status, 1.0 if status == "online" else None, "{}"),
            )
        conn.commit()
    stats = db_module.service_availability(service_id, 1)
    assert stats["availability_pct"] == 100.0


def test_problem_open_update_resolve(fresh_db):
    device_id = _mk("prob-test", "10.0.0.30")
    pid1, is_new1 = db_module.open_problem(device_id, "device_down", "high", "tidak merespons")
    pid2, is_new2 = db_module.open_problem(device_id, "device_down", "high", "tidak merespons lagi")
    assert is_new1 is True
    assert is_new2 is False
    assert pid1 == pid2
    open_rows = list(db_module.list_problems("open"))
    assert len(open_rows) == 1
    assert open_rows[0]["message"] == "tidak merespons lagi"
    assert db_module.resolve_problem(device_id, "device_down") == 1
    assert list(db_module.list_problems("open")) == []


def test_purge_old_data_aggregates(fresh_db):
    device_id = _mk("purge-test", "10.0.0.40")
    raw_cutoff = (datetime.now(timezone.utc) - timedelta(days=8)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for cpu in (10, 30):
        db_module.record_metric(
            device_id, raw_cutoff,
            cpu_pct=float(cpu), mem_used=500_000_000, mem_total=1_000_000_000,
            disk_used=50_000_000_000, disk_total=100_000_000_000,
            temp_c=45.0, cpu_temp_c=50.0, voltage=12.0, uptime_s=1000, ping_ms=1.0,
        )
    result = db_module.purge_old_data(days=7, hourly_days=30, events_days=30, audit_days=365)
    assert result["metrics_deleted"] == 2
    assert result["hourly_deleted"] == 0
    assert len(list(db_module.get_conn().execute("SELECT * FROM device_metrics_hourly"))) == 1
