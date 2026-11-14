"""Regresi M-1: pengaturan retensi UI harus dipakai job pembersihan & pratinjau."""

from datetime import datetime, timedelta, timezone

import db as db_module
from routes import admin as admin_module


def _mk(name, ip):
    return db_module.create_device(
        {
            "name": name,
            "ip": ip,
            "type": "mikrotik",
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


def _ts(days=0):
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _seed_retention_rows(device_id):
    with db_module._lock:
        conn = db_module.get_conn()
        # Data mentah: satu terlalu tua, satu masih dalam retensi.
        for days in (3, 1):
            conn.execute(
                "INSERT INTO device_metrics (device_id, ts, cpu_pct) VALUES (?,?,?)",
                (device_id, _ts(days), 10.0),
            )
        # Ringkasan per jam: satu terlalu tua, satu masih dalam retensi.
        for days in (11, 5):
            conn.execute(
                "INSERT INTO device_metrics_hourly (device_id, hour_ts, samples) VALUES (?,?,?)",
                (device_id, _ts(days)[:13] + ":00:00Z", 1),
            )
        # Event dan audit log.
        for days in (5, 3):
            conn.execute(
                "INSERT INTO events (device_id, new_status, message, created_at) VALUES (?,?,?,?)",
                (device_id, "online", f"event-{days}", _ts(days)),
            )
        for days in (6, 4):
            conn.execute(
                "INSERT INTO audit_log (ts, username, action, object, summary) VALUES (?,?,?,?,?)",
                (_ts(days), "admin", "test", "retention", f"audit-{days}"),
            )
        # Riwayat pemeriksaan perangkat.
        for days in (7, 5):
            conn.execute(
                "INSERT INTO device_checks (device_id, checked_at, status, latency_ms, detail_json, maintenance)"
                " VALUES (?,?,?,?,?,0)",
                (device_id, _ts(days), "online", 1.0, "{}"),
            )
        conn.commit()


def test_retention_days_reads_ui_settings(fresh_db):
    fresh_db.set_settings(
        {
            "retention_checks_days": "6",
            "retention_raw_days": "2",
            "retention_hourly_days": "10",
            "retention_events_days": "4",
            "retention_audit_days": "5",
        }
    )

    assert fresh_db.retention_days() == {
        "retention_checks_days": 6,
        "retention_raw_days": 2,
        "retention_hourly_days": 10,
        "retention_events_days": 4,
        "retention_audit_days": 5,
    }


def test_retention_days_falls_back_on_invalid_values(fresh_db):
    fresh_db.set_settings(
        {
            "retention_checks_days": "bukan-angka",
            "retention_raw_days": "0",
            "retention_hourly_days": "999999",
        }
    )

    resolved = fresh_db.retention_days()
    assert resolved["retention_checks_days"] == 30
    assert resolved["retention_raw_days"] == 7
    assert resolved["retention_hourly_days"] == 3650


def test_purge_uses_settings_and_preview_matches(fresh_db):
    device_id = _mk("retention-test", "10.20.30.40")
    fresh_db.set_settings(
        {
            "retention_checks_days": "6",
            "retention_raw_days": "2",
            "retention_hourly_days": "10",
            "retention_events_days": "4",
            "retention_audit_days": "5",
        }
    )
    _seed_retention_rows(device_id)

    preview = admin_module._retention_preview()
    assert preview["checks"] == 1
    assert preview["metrics"] == 1
    assert preview["interfaces"] == 0
    assert preview["hourly"] == 1
    assert preview["events"] == 1
    assert preview["audit"] == 1

    result = fresh_db.purge_old_data()
    checks_deleted = fresh_db.purge_old_checks()

    assert result["metrics_deleted"] == preview["metrics"]
    assert result["hourly_deleted"] == preview["hourly"]
    assert result["events_deleted"] == preview["events"]
    assert result["audit_deleted"] == preview["audit"]
    assert checks_deleted == preview["checks"]

    with fresh_db._lock:
        conn = fresh_db.get_conn()
        assert conn.execute("SELECT COUNT(*) AS n FROM device_metrics").fetchone()["n"] == 1
        assert conn.execute("SELECT COUNT(*) AS n FROM device_metrics_hourly").fetchone()["n"] == 2
        assert conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"] == 1
        assert conn.execute("SELECT COUNT(*) AS n FROM audit_log").fetchone()["n"] == 1
        assert conn.execute("SELECT COUNT(*) AS n FROM device_checks").fetchone()["n"] == 1


def test_purge_explicit_days_still_override_settings(fresh_db):
    """Pemanggil lama (mis. tes/skrip) tetap bisa memberi nilai eksplisit."""
    device_id = _mk("retention-explicit", "10.20.30.41")
    fresh_db.set_settings({"retention_raw_days": "30"})
    with fresh_db._lock:
        conn = fresh_db.get_conn()
        conn.execute(
            "INSERT INTO device_metrics (device_id, ts, cpu_pct) VALUES (?,?,?)",
            (device_id, _ts(10), 10.0),
        )
        conn.commit()

    result = fresh_db.purge_old_data(days=7)

    assert result["metrics_deleted"] == 1


def test_scheduler_purge_jobs_do_not_pass_hardcoded_days(fresh_db, monkeypatch):
    import poller

    monkeypatch.setattr(poller, "_scheduler", None)
    scheduler = poller.start_poller()
    try:
        jobs = {job.id: job for job in scheduler.get_jobs()}
        assert jobs["purge-old-checks"].kwargs == {}
        assert jobs["purge-old-data"].kwargs == {}
    finally:
        poller.shutdown_poller()
