"""Tes alat bukti pilot: tools/pilot_report.py."""

import sqlite3
from datetime import datetime, timedelta, timezone

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


def _add_checks(conn, device_id, start_utc, end_utc, status="online", maintenance=0):
    """Isi cek per menit pada rentang UTC [start, end)."""
    current = datetime.fromisoformat(start_utc.replace("Z", "+00:00"))
    end = datetime.fromisoformat(end_utc.replace("Z", "+00:00"))
    rows = []
    while current < end:
        rows.append(
            (
                device_id,
                current.strftime("%Y-%m-%dT%H:%M:%SZ"),
                status,
                maintenance,
            )
        )
        current += timedelta(minutes=1)
    conn.executemany(
        "INSERT INTO device_checks (device_id, checked_at, status, maintenance)"
        " VALUES (?, ?, ?, ?)",
        rows,
    )


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
    assert cov["days_expected"] == 1
    assert cov["days_without_data"] == 0
    assert cov["observed_hours"] == 0.05
    assert cov["expected_hours"] == 9.0
    assert cov["coverage_pct"] == pytest.approx(0.6, abs=0.1)
    assert cov["per_device"][0]["checks_non_maintenance"] == 3
    assert cov["per_device"][1]["checks_non_maintenance"] == 1


def _seed_problems(conn):
    conn.executescript(
        """
        CREATE TABLE devices (id INTEGER PRIMARY KEY, name TEXT, ip TEXT,
            poll_interval INTEGER DEFAULT 60);
        CREATE TABLE fiber_links (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE problems (id INTEGER PRIMARY KEY, device_id INTEGER, kind TEXT,
            severity TEXT, message TEXT, status TEXT, started_at TEXT, resolved_at TEXT,
            acked_by TEXT, acked_at TEXT, link_id INTEGER);
        CREATE TABLE device_checks (id INTEGER PRIMARY KEY, device_id INTEGER,
            checked_at TEXT, status TEXT, maintenance INTEGER DEFAULT 0);
        INSERT INTO devices VALUES (1,'Mikrotik','192.168.30.1',60);
        INSERT INTO fiber_links VALUES (1,'Link A');
        INSERT INTO device_checks VALUES
          (1,1,'2026-11-23T23:59:30Z','online',0),
          (2,1,'2026-11-24T00:00:00Z','offline',0);
        INSERT INTO problems VALUES
          (1,1,'device_down','high','mati','resolved','2026-11-24T00:00:30Z','2026-11-24T00:10:30Z',NULL,NULL,NULL),
          (2,1,'fiber_rx_low','warning','rx turun','open','2026-11-24T05:00:00Z',NULL,'operator',NULL,1);
        """
    )
    conn.commit()


def test_build_incidents_mttd_mttr_and_open_overlap(tmp_path):
    conn = pilot_report.connect_ro(_make_db(tmp_path, _seed_problems))
    data = pilot_report.build_incidents(conn, "2026-11-23T17:00:00Z", "2026-11-25T17:00:00Z")
    assert data["summary"]["total"] == 2
    assert data["summary"]["open"] == 1
    assert data["summary"]["resolved"] == 1
    assert data["summary"]["mttr_median_min"] == 10.0
    assert data["summary"]["mttd_median_sec"] == 30.0
    down = next(i for i in data["items"] if i["kind"] == "device_down")
    assert down["mttd_sec"] == 30.0
    fiber = next(i for i in data["items"] if i["kind"] == "fiber_rx_low")
    assert fiber["mttd_sec"] is None
    assert fiber["resolved_at"] is None


def test_build_incidents_includes_problem_started_before_range(tmp_path):
    conn = pilot_report.connect_ro(_make_db(tmp_path, _seed_problems))
    data = pilot_report.build_incidents(conn, "2026-11-24T00:05:00Z", "2026-11-24T01:00:00Z")
    assert [i["id"] for i in data["items"]] == [1]


def _seed_predictions(conn):
    _seed_empty(conn)
    conn.executescript(
        """
        INSERT INTO fiber_links VALUES (1,'Link A');
        INSERT INTO fiber_prediction_log VALUES
          (1,1,'a',1,'sfp1','2026-11-20T02:00:00Z',-14.0,-0.35,'warn',-15.0,10.0,'average'),
          (2,1,'a',1,'sfp1','2026-11-20T03:00:00Z',-14.2,-0.36,'warn',-15.0,9.0,'average');
        INSERT INTO optical_hourly VALUES
          (1,'sfp1','2026-11-25T02:00:00Z',-15.4,-15.2,-15.0,-3,-3,-3,20,20,20,30);
        """
    )
    conn.commit()


def test_build_predictions_confirmed_with_lead_time(tmp_path):
    conn = pilot_report.connect_ro(_make_db(tmp_path, _seed_predictions))
    data = pilot_report.build_predictions(conn, "2026-11-19T17:00:00Z", "2026-11-26T17:00:00Z")
    assert data["summary"]["total"] == 1
    assert data["summary"]["confirmed"] == 1
    item = data["items"][0]
    assert item["status"] == "terkonfirmasi"
    assert item["eta_days"] == 10.0
    assert item["lead_days"] == pytest.approx(5.0)
    assert item["error_days"] == pytest.approx(-5.0)


def test_build_predictions_unconfirmed_when_no_crossing(tmp_path):
    conn = pilot_report.connect_ro(_make_db(tmp_path, _seed_predictions))
    data = pilot_report.build_predictions(conn, "2026-11-19T17:00:00Z", "2026-11-21T17:00:00Z")
    assert data["summary"]["unconfirmed"] == 1
    assert data["items"][0]["status"] == "belum terverifikasi"


def _seed_empty(conn):
    conn.executescript(
        """
        CREATE TABLE devices (id INTEGER PRIMARY KEY, name TEXT, ip TEXT,
            poll_interval INTEGER DEFAULT 60);
        CREATE TABLE device_checks (id INTEGER PRIMARY KEY, device_id INTEGER,
            checked_at TEXT, status TEXT, maintenance INTEGER DEFAULT 0);
        CREATE TABLE fiber_links (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE problems (id INTEGER PRIMARY KEY, device_id INTEGER, kind TEXT,
            severity TEXT, message TEXT, status TEXT, started_at TEXT, resolved_at TEXT,
            acked_by TEXT, acked_at TEXT, link_id INTEGER);
        CREATE TABLE fiber_prediction_log (id INTEGER PRIMARY KEY, link_id INTEGER,
            side TEXT, device_id INTEGER, if_name TEXT, evaluated_at TEXT, rx_dbm REAL,
            slope_db_per_week REAL, target TEXT, threshold_dbm REAL, days REAL, severity TEXT);
        CREATE TABLE optical_hourly (device_id INTEGER, if_name TEXT, hour_ts TEXT,
            rx_min REAL, rx_avg REAL, rx_max REAL, tx_min REAL, tx_avg REAL, tx_max REAL,
            temp_min REAL, temp_avg REAL, temp_max REAL, samples INTEGER);
        CREATE TABLE notification_channels (id INTEGER PRIMARY KEY, name TEXT, type TEXT);
        CREATE TABLE action_log (id INTEGER PRIMARY KEY, ts TEXT, rule_id INTEGER,
            channel_id INTEGER, device_id INTEGER, problem_id INTEGER,
            status TEXT, detail TEXT);
        """
    )
    conn.commit()


def _seed_alarms(conn):
    _seed_empty(conn)
    conn.executescript(
        """
        INSERT INTO devices VALUES (1,'Mikrotik','192.168.30.1',60);
        INSERT INTO fiber_links VALUES (1,'Link A');
        INSERT INTO problems VALUES
          (1,1,'device_down','high','mati','resolved','2026-11-24T02:00:00Z','2026-11-24T02:05:00Z',NULL,NULL,NULL),
          (2,1,'fiber_rx_low','warning','rx turun','open','2026-11-24T03:00:00Z',NULL,NULL,NULL,1);
        INSERT INTO notification_channels VALUES (3,'kopegtell','telegram');
        INSERT INTO action_log VALUES
          (1,'2026-11-24T02:00:00Z',1,3,1,1,'sent','ok'),
          (2,'2026-11-24T03:00:00Z',2,NULL,1,2,'failed','timeout');
        """
    )
    conn.commit()


def test_build_alarms_keeps_rows_without_channel_and_blank_useful(tmp_path):
    conn = pilot_report.connect_ro(_make_db(tmp_path, _seed_alarms))
    data = pilot_report.build_alarms(conn, "2026-11-23T17:00:00Z", "2026-11-25T17:00:00Z")
    assert data["summary"]["total"] == 2
    assert data["summary"]["sent"] == 1
    assert data["summary"]["failed"] == 1
    assert all(item["berguna"] == "" for item in data["items"])
    assert any(item["channel_name"] is None for item in data["items"])


def test_render_markdown_empty_db_says_belum_terverifikasi(tmp_path):
    conn = pilot_report.connect_ro(_make_db(tmp_path, _seed_empty))
    report = pilot_report.build_report(conn, "2026-11-23T17:00:00Z", "2026-11-25T17:00:00Z")
    text = pilot_report.render_markdown(report)
    assert "# Laporan Dampak Pilot NMS Kopegtel" in text
    assert "Belum terverifikasi" in text
    assert "Tidak ada data sintetis" in text


def test_write_csvs_creates_four_files_with_useful_column(tmp_path):
    conn = pilot_report.connect_ro(_make_db(tmp_path, _seed_alarms))
    report = pilot_report.build_report(conn, "2026-11-23T17:00:00Z", "2026-11-25T17:00:00Z")
    paths = pilot_report.write_csvs(report, tmp_path / "out")
    names = sorted(p.name for p in paths)
    assert names == ["alarm.csv", "cakupan-perangkat.csv", "insiden.csv", "prediksi.csv"]
    assert "berguna" in (tmp_path / "out" / "alarm.csv").read_text(encoding="utf-8").splitlines()[0]


def test_main_end_to_end_writes_markdown_and_csv(tmp_path, capsys):
    db = _make_db(tmp_path, _seed_alarms)
    out = tmp_path / "laporan"
    code = pilot_report.main(
        [
            "--db",
            str(db),
            "--since",
            "2026-11-20",
            "--until",
            "2026-11-25",
            "--out",
            str(out),
        ]
    )
    assert code == 0
    assert len(list(out.glob("*.md"))) == 1
    assert len(list(out.glob("*.csv"))) == 4


def _seed_mttd(conn, checks_sql, started_at):
    conn.executescript(
        """
        CREATE TABLE devices (id INTEGER PRIMARY KEY, name TEXT, ip TEXT,
            poll_interval INTEGER DEFAULT 60);
        CREATE TABLE fiber_links (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE problems (id INTEGER PRIMARY KEY, device_id INTEGER, kind TEXT,
            severity TEXT, message TEXT, status TEXT, started_at TEXT, resolved_at TEXT,
            acked_by TEXT, acked_at TEXT, link_id INTEGER);
        CREATE TABLE device_checks (id INTEGER PRIMARY KEY, device_id INTEGER,
            checked_at TEXT, status TEXT, maintenance INTEGER DEFAULT 0);
        INSERT INTO devices VALUES (1,'Mikrotik','192.168.30.1',60);
        """
        + checks_sql
        + f"""
        INSERT INTO problems VALUES
          (1,1,'device_down','high','mati','open','{started_at}',NULL,NULL,NULL,NULL);
        """
    )
    conn.commit()


def test_build_incidents_mttd_ignores_prior_outage(tmp_path):
    def seed(conn):
        _seed_mttd(
            conn,
            """
            INSERT INTO device_checks VALUES
              (1,1,'2026-11-24T09:59:00Z','online',0),
              (2,1,'2026-11-24T10:00:00Z','offline',0),
              (3,1,'2026-11-24T10:05:00Z','online',0),
              (4,1,'2026-11-24T10:59:00Z','online',0),
              (5,1,'2026-11-24T11:00:00Z','offline',0);
            """,
            "2026-11-24T11:00:30Z",
        )

    conn = pilot_report.connect_ro(_make_db(tmp_path, seed))
    data = pilot_report.build_incidents(conn, "2026-11-23T17:00:00Z", "2026-11-25T17:00:00Z")
    assert data["items"][0]["mttd_sec"] == 30.0


def test_build_incidents_mttd_none_when_gap_too_large(tmp_path):
    def seed(conn):
        _seed_mttd(
            conn,
            """
            INSERT INTO device_checks VALUES
              (1,1,'2026-11-24T09:00:00Z','online',0),
              (2,1,'2026-11-24T17:00:00Z','offline',0);
            """,
            "2026-11-24T17:00:00Z",
        )

    conn = pilot_report.connect_ro(_make_db(tmp_path, seed))
    data = pilot_report.build_incidents(conn, "2026-11-23T17:00:00Z", "2026-11-25T17:00:00Z")
    assert data["items"][0]["mttd_sec"] is None
    assert data["summary"]["mttd_samples"] == 0


def test_build_coverage_clips_office_hours_and_counts_missing_weekdays(tmp_path):
    def seed(conn):
        conn.executescript(
            """
            CREATE TABLE devices (id INTEGER PRIMARY KEY, name TEXT, ip TEXT);
            CREATE TABLE device_checks (id INTEGER PRIMARY KEY, device_id INTEGER,
                checked_at TEXT, status TEXT, maintenance INTEGER DEFAULT 0);
            INSERT INTO devices VALUES (1,'Mikrotik','192.168.30.1');
            """
        )
        _add_checks(conn, 1, "2026-11-23T00:00:00Z", "2026-11-23T11:00:00Z")
        conn.commit()

    conn = pilot_report.connect_ro(_make_db(tmp_path, seed))
    cov = pilot_report.build_coverage(
        conn,
        "2026-11-22T17:00:00Z",
        "2026-11-24T17:00:00Z",
        now=datetime(2026, 11, 26, 0, 0, tzinfo=timezone.utc),
    )
    assert cov["observed_hours"] == 9.0  # 08:00-17:00 WIB, bukan 00:00-18:00
    assert cov["expected_hours"] == 18.0  # Senin + Selasa
    assert cov["coverage_pct"] == pytest.approx(50.0, abs=0.1)
    assert cov["days_expected"] == 2
    assert cov["days_without_data"] == 1


def test_build_coverage_prorates_today_before_office_hours(tmp_path):
    def seed(conn):
        conn.executescript(
            """
            CREATE TABLE devices (id INTEGER PRIMARY KEY, name TEXT, ip TEXT);
            CREATE TABLE device_checks (id INTEGER PRIMARY KEY, device_id INTEGER,
                checked_at TEXT, status TEXT, maintenance INTEGER DEFAULT 0);
            INSERT INTO devices VALUES (1,'Mikrotik','192.168.30.1');
            """
        )
        _add_checks(conn, 1, "2026-11-23T02:00:00Z", "2026-11-23T03:00:00Z")
        conn.commit()

    conn = pilot_report.connect_ro(_make_db(tmp_path, seed))
    cov = pilot_report.build_coverage(
        conn,
        "2026-11-22T17:00:00Z",
        "2026-11-25T17:00:00Z",
        now=datetime(2026, 11, 24, 18, 0, tzinfo=timezone.utc),  # Rabu 01:00 WIB
    )
    assert cov["expected_hours"] == 18.0  # Senin + Selasa; Rabu belum jam kerja
    assert cov["coverage_pct"] == pytest.approx(5.6, abs=0.1)
    assert cov["days_expected"] == 2


def test_build_coverage_online_pct_none_without_non_maintenance(tmp_path):
    def seed(conn):
        conn.executescript(
            """
            CREATE TABLE devices (id INTEGER PRIMARY KEY, name TEXT, ip TEXT);
            CREATE TABLE device_checks (id INTEGER PRIMARY KEY, device_id INTEGER,
                checked_at TEXT, status TEXT, maintenance INTEGER DEFAULT 0);
            INSERT INTO devices VALUES (1,'Mikrotik','192.168.30.1');
            INSERT INTO device_checks VALUES
              (1,1,'2026-11-24T01:00:00Z','online',1),
              (2,1,'2026-11-24T02:00:00Z','offline',1);
            """
        )
        conn.commit()

    conn = pilot_report.connect_ro(_make_db(tmp_path, seed))
    cov = pilot_report.build_coverage(conn, "2026-11-23T17:00:00Z", "2026-11-24T17:00:00Z")
    assert cov["online_pct"] is None
    assert cov["per_device"][0]["online_pct"] is None
    assert cov["per_device"][0]["checks_non_maintenance"] == 0


def test_build_alarms_excludes_channel_tests_from_production_summary(tmp_path):
    def seed(conn):
        _seed_empty(conn)
        conn.executescript(
            """
            INSERT INTO problems VALUES
              (1,NULL,'device_down','high','mati','open','2026-11-24T02:00:00Z',NULL,NULL,NULL,NULL);
            INSERT INTO notification_channels VALUES (3,'kopegtell','telegram');
            INSERT INTO action_log VALUES
              (1,'2026-11-24T02:00:00Z',1,3,1,1,'sent','ok'),
              (2,'2026-11-24T03:00:00Z',2,3,1,NULL,'failed','Uji kirim: timeout');
            """
        )
        conn.commit()

    conn = pilot_report.connect_ro(_make_db(tmp_path, seed))
    data = pilot_report.build_alarms(conn, "2026-11-23T17:00:00Z", "2026-11-25T17:00:00Z")
    assert data["summary"]["total"] == 1
    assert data["summary"]["sent"] == 1
    assert data["summary"]["failed"] == 0
    assert data["summary"]["test_total"] == 1
    assert {item["is_test"] for item in data["items"]} == {False, True}


def test_parse_period_rejects_since_after_default_until():
    now = datetime(2026, 11, 25, 1, 0, tzinfo=timezone.utc)
    with pytest.raises(ValueError):
        pilot_report.parse_period(since="2026-11-26", now=now)


def test_main_invalid_timezone_returns_2(tmp_path, capsys):
    db = _make_db(tmp_path, _seed_empty)
    code = pilot_report.main(["--db", str(db), "--tz", "Bukan/Zona"])
    assert code == 2
    assert "zona waktu" in capsys.readouterr().out.lower()


def test_main_corrupt_db_returns_2(tmp_path, capsys):
    path = tmp_path / "rusak.db"
    path.write_text("bukan sqlite", encoding="utf-8")
    code = pilot_report.main(["--db", str(path)])
    assert code == 2
    assert "database" in capsys.readouterr().out.lower()


def test_write_csvs_neutralizes_formula_injection(tmp_path):
    conn = pilot_report.connect_ro(_make_db(tmp_path, _seed_alarms))
    report = pilot_report.build_report(conn, "2026-11-23T17:00:00Z", "2026-11-25T17:00:00Z")
    report["alarms"]["items"][0]["detail"] = '=HYPERLINK("http://contoh")'
    pilot_report.write_csvs(report, tmp_path / "out")
    text = (tmp_path / "out" / "alarm.csv").read_text(encoding="utf-8")
    assert "'=HYPERLINK" in text


def test_render_markdown_capitalizes_unverified_prediction_status(tmp_path):
    conn = pilot_report.connect_ro(_make_db(tmp_path, _seed_predictions))
    report = pilot_report.build_report(conn, "2026-11-19T17:00:00Z", "2026-11-21T17:00:00Z")
    text = pilot_report.render_markdown(report)
    assert "| Belum terverifikasi |" in text
    assert "| belum terverifikasi |" not in text


def test_build_coverage_excludes_weekend_from_numerator(tmp_path):
    def seed(conn):
        conn.executescript(
            """
            CREATE TABLE devices (id INTEGER PRIMARY KEY, name TEXT, ip TEXT);
            CREATE TABLE device_checks (id INTEGER PRIMARY KEY, device_id INTEGER,
                checked_at TEXT, status TEXT, maintenance INTEGER DEFAULT 0);
            INSERT INTO devices VALUES (1,'Mikrotik','192.168.30.1');
            """
        )
        # Jumat 08:00-17:00 WIB dan Sabtu 08:00-16:00 WIB, cek per menit.
        _add_checks(conn, 1, "2026-11-06T01:00:00Z", "2026-11-06T10:00:00Z")
        _add_checks(conn, 1, "2026-11-07T01:00:00Z", "2026-11-07T09:00:00Z")
        conn.commit()

    conn = pilot_report.connect_ro(_make_db(tmp_path, seed))
    cov = pilot_report.build_coverage(
        conn,
        "2026-11-05T17:00:00Z",
        "2026-11-07T17:00:00Z",
        now=datetime(2026, 11, 8, 0, 0, tzinfo=timezone.utc),
    )
    assert cov["observed_hours"] == 9.0  # hanya Jumat 08:00-17:00
    assert cov["expected_hours"] == 9.0
    assert cov["coverage_pct"] == 100.0
    assert cov["days_with_data"] == 2
    assert cov["days_with_office_data"] == 1


def test_build_coverage_weekday_with_only_night_checks_is_missing(tmp_path):
    def seed(conn):
        conn.executescript(
            """
            CREATE TABLE devices (id INTEGER PRIMARY KEY, name TEXT, ip TEXT);
            CREATE TABLE device_checks (id INTEGER PRIMARY KEY, device_id INTEGER,
                checked_at TEXT, status TEXT, maintenance INTEGER DEFAULT 0);
            INSERT INTO devices VALUES (1,'Mikrotik','192.168.30.1');
            INSERT INTO device_checks VALUES
              (1,1,'2026-11-22T17:00:00Z','online',0),
              (2,1,'2026-11-22T19:00:00Z','online',0),
              (3,1,'2026-11-24T02:00:00Z','online',0),
              (4,1,'2026-11-24T03:00:00Z','online',0);
            """
        )
        conn.commit()

    conn = pilot_report.connect_ro(_make_db(tmp_path, seed))
    cov = pilot_report.build_coverage(
        conn,
        "2026-11-22T17:00:00Z",
        "2026-11-24T17:00:00Z",
        now=datetime(2026, 11, 25, 0, 0, tzinfo=timezone.utc),
    )
    assert cov["observed_hours"] == 0.03  # hanya dua menit Selasa
    assert cov["expected_hours"] == 18.0
    assert cov["days_with_data"] == 2
    assert cov["days_with_office_data"] == 1
    assert cov["days_without_data"] == 1  # Senin hanya punya cek malam
