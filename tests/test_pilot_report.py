"""Tes alat bukti pilot: tools/pilot_report.py."""

import sqlite3
from datetime import datetime, timezone

import pytest

from tools import pilot_report


def _make_db(tmp_path, seed):
    path = tmp_path / "nms.db"
    conn = sqlite3.connect(path)
    try:
        seed(conn)
        conn.commit()
    finally:
        conn.close()
    return path


def test_parse_period_default_seven_days_uses_local_day_boundaries():
    now = datetime(2026, 11, 25, 1, 0, tzinfo=timezone.utc)  # 08:00 WIB
    since, until = pilot_report.parse_period(days=7, now=now)
    assert since == "2026-11-18T17:00:00Z"  # 19 Nov 00:00 WIB
    assert until == "2026-11-25T17:00:00Z"  # 26 Nov 00:00 WIB


def test_parse_period_explicit_since_until_validates_order():
    since, until = pilot_report.parse_period(since="2026-11-20", until="2026-11-22")
    assert since == "2026-11-19T17:00:00Z"
    assert until == "2026-11-22T17:00:00Z"
    with pytest.raises(ValueError):
        pilot_report.parse_period(since="2026-11-23", until="2026-11-22")


def test_connect_ro_rejects_writes(tmp_path):
    db = tmp_path / "x.db"
    with sqlite3.connect(db) as raw:
        raw.execute("CREATE TABLE t (id INTEGER)")
    conn = pilot_report.connect_ro(db)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO t (id) VALUES (1)")
    conn.close()


def test_main_missing_db_returns_2(tmp_path, capsys):
    code = pilot_report.main(["--db", str(tmp_path / "tidak-ada.db")])
    assert code == 2
    assert "tidak ditemukan" in capsys.readouterr().out.lower()


def _seed_coverage(conn):
    conn.executescript(
        """
        CREATE TABLE devices (id INTEGER PRIMARY KEY, name TEXT, ip TEXT);
        CREATE TABLE device_checks (id INTEGER PRIMARY KEY, device_id INTEGER,
            checked_at TEXT, status TEXT, maintenance INTEGER DEFAULT 0);
        INSERT INTO devices VALUES (1,'Mikrotik','192.168.30.1'),(2,'Switch','192.168.30.211');
        INSERT INTO device_checks VALUES
          (1,1,'2026-11-24T01:00:00Z','online',0),
          (2,1,'2026-11-24T02:00:00Z','online',0),
          (3,1,'2026-11-24T03:00:00Z','offline',0),
          (4,2,'2026-11-24T01:00:00Z','online',1),
          (5,2,'2026-11-24T02:00:00Z','degraded',0);
        """
    )
    conn.commit()


def test_observe_windows_groups_utc_into_jakarta_days():
    rows = ["2026-11-24T01:00:00Z", "2026-11-24T03:30:00Z", "2026-11-24T16:59:00Z"]
    windows = pilot_report.observe_windows(rows)
    assert windows == [
        {"tanggal": "2026-11-24", "mulai": "08:00", "selesai": "23:59", "jam": 15.98}
    ]


def test_build_coverage_excludes_maintenance_and_counts_status(tmp_path):
    conn = pilot_report.connect_ro(_make_db(tmp_path, _seed_coverage))
    cov = pilot_report.build_coverage(conn, "2026-11-23T17:00:00Z", "2026-11-24T17:00:00Z")
    assert cov["devices_registered"] == 2
    assert cov["devices_seen"] == 2
    assert cov["checks_total"] == 5
    assert cov["checks_maintenance"] == 1
    assert cov["status_counts"] == {"online": 2, "degraded": 1, "offline": 1}
    assert cov["online_pct"] == 50.0  # 2 dari 4 cek non-maintenance
    assert cov["days_with_data"] == 1
    assert cov["observed_hours"] == 2.0
    assert cov["expected_hours"] == 9.0
    assert cov["coverage_pct"] == pytest.approx(22.2, abs=0.1)
