"""Rollup optik per jam harus berjalan tanpa job tengah malam.

Pilot hanya menyalakan server pada jam kerja, jadi jadwal 02.35 tidak pernah
tercapai. Rollup harus idempoten, melewati jam yang belum selesai, dan tidak
menghapus ringkasan yang data mentahnya sudah dibuang retensi.
"""

import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, "backend")

import db  # noqa: E402


def _device(fresh_db):
    return fresh_db.create_device(
        {
            "name": "Uji Rollup",
            "ip": "10.0.0.9",
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
    )


def _hour_ts(hours_ago):
    moment = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return moment.strftime("%Y-%m-%dT%H:00:00Z")


def _at(hour_ts, minute):
    return hour_ts.replace(":00:00Z", f":{minute:02d}:00Z")


def _seed_hour(fresh_db, device_id, hour_ts, values):
    for minute, rx in values:
        fresh_db.record_optical_readings(
            device_id,
            _at(hour_ts, minute),
            [{"if_name": "sfp1", "rx_dbm": rx, "tx_dbm": 1.0}],
        )


def test_rollup_aggregates_completed_hour(fresh_db):
    device_id = _device(fresh_db)
    hour = _hour_ts(2)
    _seed_hour(fresh_db, device_id, hour, ((5, -10.0), (15, -12.0), (25, -14.0)))

    fresh_db.rollup_optical_hourly()

    rows = fresh_db.optical_hourly_rows(device_id, "sfp1", "2000-01-01T00:00:00Z")
    assert len(rows) == 1
    row = rows[0]
    assert row["hour_ts"] == hour
    assert row["samples"] == 3
    assert row["rx_min"] == -14.0
    assert row["rx_max"] == -10.0
    assert row["rx_avg"] == pytest.approx(-12.0)


def test_rollup_is_idempotent(fresh_db):
    device_id = _device(fresh_db)
    _seed_hour(fresh_db, device_id, _hour_ts(2), ((5, -10.0), (15, -12.0)))

    fresh_db.rollup_optical_hourly()
    fresh_db.rollup_optical_hourly()

    rows = fresh_db.optical_hourly_rows(device_id, "sfp1", "2000-01-01T00:00:00Z")
    assert len(rows) == 1
    assert rows[0]["samples"] == 2
    assert rows[0]["rx_avg"] == pytest.approx(-11.0)


def test_rollup_skips_current_hour(fresh_db):
    device_id = _device(fresh_db)
    current = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:00:00Z")
    fresh_db.record_optical_readings(
        device_id,
        _at(current, 1),
        [{"if_name": "sfp1", "rx_dbm": -9.0, "tx_dbm": 1.0}],
    )

    fresh_db.rollup_optical_hourly()

    assert fresh_db.optical_hourly_rows(device_id, "sfp1", "2000-01-01T00:00:00Z") == []


def test_rollup_keeps_hourly_after_raw_purged(fresh_db):
    device_id = _device(fresh_db)
    _seed_hour(fresh_db, device_id, _hour_ts(24 * 10), ((5, -13.0), (15, -13.5)))

    fresh_db.rollup_optical_hourly()
    fresh_db.purge_old_data()

    assert fresh_db.optical_hourly_rows(device_id, "sfp1", "2000-01-01T00:00:00Z") != []
    raw = fresh_db.optical_port_history_raw(device_id, "sfp1", "2000-01-01T00:00:00Z")
    assert raw == []


def test_rollup_since_hour_leaves_older_hours_untouched(fresh_db):
    device_id = _device(fresh_db)
    old_hour = _hour_ts(24 * 10)
    _seed_hour(fresh_db, device_id, old_hour, ((5, -13.0),))

    fresh_db.rollup_optical_hourly(since_hour=_hour_ts(6))

    assert fresh_db.optical_hourly_rows(device_id, "sfp1", "2000-01-01T00:00:00Z") == []


def test_maintenance_startup_runs_purge_and_backup(fresh_db, monkeypatch):
    import poller

    calls = []
    monkeypatch.setattr(db, "purge_old_checks", lambda: calls.append("checks"))
    monkeypatch.setattr(db, "purge_old_data", lambda: calls.append("data"))
    monkeypatch.setattr(poller.dbbackup, "run_due_backup", lambda: calls.append("backup"))

    poller._maintenance_startup()

    assert calls == ["checks", "data", "backup"]


def test_maintenance_startup_continues_after_one_job_fails(fresh_db, monkeypatch):
    import poller

    calls = []

    def boom():
        raise RuntimeError("gagal")

    monkeypatch.setattr(db, "purge_old_checks", boom)
    monkeypatch.setattr(db, "purge_old_data", lambda: calls.append("data"))
    monkeypatch.setattr(poller.dbbackup, "run_due_backup", lambda: calls.append("backup"))

    poller._maintenance_startup()

    assert calls == ["data", "backup"]


def test_hourly_rollup_job_limits_to_raw_retention(fresh_db, monkeypatch):
    import poller

    captured = {}
    monkeypatch.setattr(
        poller.db, "retention_days", lambda: {"retention_raw_days": 7}
    )
    monkeypatch.setattr(
        poller.db, "rollup_optical_hourly",
        lambda since_hour=None: captured.setdefault("since_hour", since_hour),
    )

    poller._rollup_optical_hourly_job()

    expected = (
        datetime.now(timezone.utc) - timedelta(days=7)
    ).strftime("%Y-%m-%dT%H:00:00Z")
    assert captured["since_hour"] == expected
