"""Tes alat bukti pilot: tools/pilot_report.py."""

import sqlite3
from datetime import datetime, timezone

import pytest

from tools import pilot_report


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
