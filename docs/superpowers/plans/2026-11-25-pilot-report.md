# Pilot Report (Alat Bukti) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Membuat `tools/pilot_report.py` yang mengubah data pilot nyata di `backend/nms.db` menjadi laporan Markdown + CSV (cakupan, insiden/MTTD/MTTR, prediksi fiber, alarm), tanpa data sintetis dan tanpa membocorkan kredensial.

**Architecture:** Satu CLI mandiri di `tools/` yang membuka database SQLite secara **read-only** (`mode=ro`) dan memakai fungsi murni yang mudah diuji. Logika dipecah per bagian laporan: periode/waktu, cakupan, insiden, prediksi, alarm, lalu renderer Markdown/CSV. Tidak ada halaman UI baru dan tidak ada perubahan skema.

**Tech Stack:** Python 3.13 stdlib (`argparse`, `sqlite3`, `csv`, `statistics`, `zoneinfo`), pytest.

**Spec:** `docs/superpowers/specs/2026-11-24-pilot-fiber-prediction-design.md` (§8.3) dan `ROADMAP.md` (P2).

## Global Constraints

- **Data nyata saja**: alat hanya membaca database; tidak boleh membuat/mengubah baris. Data sintetis hanya boleh di `tests/`.
- **Tanpa rahasia**: jangan pernah menampilkan community/password/token. Hanya path DB, nama perangkat/link, dan nama channel.
- **Zona waktu**: timestamp DB UTC (`...Z`); tampilan dan pengelompokan hari memakai `Asia/Jakarta` (UTC+7), dapat diubah lewat `--tz`.
- **Jujur soal batas**: bagian tanpa data harus berbunyi "Belum terverifikasi", bukan angka nol yang menyesatkan atau data contoh.
- **Read-only**: koneksi `sqlite3` memakai URI `file:<path>?mode=ro`; alat gagal dengan pesan jelas bila DB tidak ada.
- **Kompatibilitas**: semua tes lama tetap hijau; tidak mengubah `backend/`, UI, atau skema.
- **Bahasa**: judul bagian, label kolom, dan pesan CLI berbahasa Indonesia.

## Review Focus

- Database kosong atau periode tanpa baris: laporan tetap tampil lengkap dengan "Belum terverifikasi", exit code 0.
- Timestamp UTC yang jatuh di hari berbeda menurut Asia/Jakarta: jendela observasi harus mengikuti hari lokal.
- Masalah (problem) yang dimulai sebelum periode tetapi selesai di dalam periode (atau sebaliknya): harus ikut terhitung.
- Prediksi yang tidak pernah terkonfirmasi (slope membaik/link diputus): ditandai "belum terverifikasi", bukan dianggap benar.
- `action_log` tanpa channel atau tanpa problem: join harus aman (LEFT JOIN) dan tidak menghilangkan alarm.
- Nilai `NULL` (`resolved_at`, `acked_at`, `rx_avg`, `channel_name`): tidak boleh membuat crash atau "None" di laporan.

---

### Task 1: CLI, periode, dan koneksi read-only

**Files:**
- Create: `tools/pilot_report.py`
- Test: `tests/test_pilot_report.py`

**Interfaces:**
- Consumes: tidak ada.
- Produces:
  - `parse_period(days=7, since=None, until=None, now=None, tz="Asia/Jakarta") -> tuple[str, str]` — mengembalikan `(since_utc, until_utc)` dalam format `YYYY-MM-DDTHH:MM:SSZ`; `since` inklusif, `until` eksklusif.
  - `connect_ro(path) -> sqlite3.Connection` — `row_factory = sqlite3.Row`, mode read-only.
  - `main(argv=None) -> int`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_pilot_report.py
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
    sqlite3.connect(db).execute("CREATE TABLE t (id INTEGER)").connection.commit()
    conn = pilot_report.connect_ro(db)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO t (id) VALUES (1)")


def test_main_missing_db_returns_2(tmp_path, capsys):
    code = pilot_report.main(["--db", str(tmp_path / "tidak-ada.db")])
    assert code == 2
    assert "tidak ditemukan" in capsys.readouterr().out.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_pilot_report.py -q`
Expected: FAIL karena modul `tools.pilot_report` belum ada.

- [ ] **Step 3: Implement Task 1**

Buat `tools/pilot_report.py` dengan docstring pemakaian, `parse_period`, `connect_ro`, dan `main` minimal yang memvalidasi `--db` lalu memanggil `build_report` (sementara `print("Belum ada data")`). Argumen CLI: `--db`, `--days` (default 7), `--since`, `--until`, `--tz`, `--out`.

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_pilot_report.py -q`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add tools/pilot_report.py tests/test_pilot_report.py
git commit -m "feat(pilot): kerangka pilot_report + koneksi read-only"
```

---

### Task 2: Jendela observasi & cakupan

**Files:**
- Modify: `tools/pilot_report.py`
- Test: `tests/test_pilot_report.py`

**Interfaces:**
- Consumes: `connect_ro`, `parse_period`.
- Produces:
  - `to_local(ts_utc, tz="Asia/Jakarta") -> datetime` — `None` aman.
  - `observe_windows(rows, tz="Asia/Jakarta") -> list[dict]` — `rows` adalah iterable timestamp ISO; hasil per hari lokal: `{tanggal, mulai, selesai, jam}`.
  - `build_coverage(conn, since_utc, until_utc, tz="Asia/Jakarta", office=(8, 17)) -> dict` dengan kunci:
    `devices_registered, devices_seen, checks_total, checks_maintenance, status_counts, online_pct, days_with_data, observed_hours, expected_hours, coverage_pct, per_device`.
    `per_device` item: `{device_id, name, ip, checks, online, degraded, offline, online_pct, first_seen, last_seen}`.

- [ ] **Step 1: Write the failing tests**

```python
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
        {"tanggal": "2026-11-24", "mulai": "08:00", "selesai": "23:59", "jam": 16.0}
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
```

`_make_db(tmp_path, seed)` adalah helper tes yang membuat DB sementara lalu mengembalikan path. `_seed_empty` membuat tabel yang sama dalam keadaan kosong; `_seed_alarms` membuat `action_log` + `notification_channels` (satu alarm dengan channel, satu tanpa).

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_pilot_report.py -q`
Expected: FAIL karena `observe_windows`/`build_coverage` belum ada.

- [ ] **Step 3: Implement Task 2**

`observe_windows`: konversi tiap timestamp ke `ZoneInfo(tz)`, kelompokkan per tanggal, hitung `jam` dari selisih max-min (2 desimal). `build_coverage`: hitung cek non-maintenance untuk status/persentase; cek maintenance hanya masuk `checks_maintenance`; `expected_hours` = jumlah hari lokal unik × (office_end - office_start); persentase dibulatkan 1 desimal. Jangan pakai jam dinding server.

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_pilot_report.py -q`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add tools/pilot_report.py tests/test_pilot_report.py
git commit -m "feat(pilot): cakupan dan jendela observasi jam kerja"
```

---

### Task 3: Insiden, MTTD, dan MTTR

**Files:**
- Modify: `tools/pilot_report.py`
- Test: `tests/test_pilot_report.py`

**Interfaces:**
- Consumes: `connect_ro`, `to_local`.
- Produces: `build_incidents(conn, since_utc, until_utc) -> dict`:
  - `items`: list `{id, kind, severity, status, device_name, device_ip, link_name, started_at, resolved_at, duration_min, acked_by, acked_at, mttd_sec, message}` (waktu lokal ISO `YYYY-MM-DD HH:MM:SS`).
  - `summary`: `{total, open, resolved, by_severity, mttr_median_min, mttr_avg_min, mttd_median_sec, mttd_samples}`.
  - `_median(values) -> float | None` helper.

- [ ] **Step 1: Write the failing tests**

```python
def _seed_problems(conn):
    conn.executescript(
        """
        CREATE TABLE devices (id INTEGER PRIMARY KEY, name TEXT, ip TEXT);
        CREATE TABLE fiber_links (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE problems (id INTEGER PRIMARY KEY, device_id INTEGER, kind TEXT,
            severity TEXT, message TEXT, status TEXT, started_at TEXT, resolved_at TEXT,
            acked_by TEXT, acked_at TEXT, link_id INTEGER);
        CREATE TABLE device_checks (id INTEGER PRIMARY KEY, device_id INTEGER,
            checked_at TEXT, status TEXT, maintenance INTEGER DEFAULT 0);
        INSERT INTO devices VALUES (1,'Mikrotik','192.168.30.1');
        INSERT INTO fiber_links VALUES (1,'Link A');
        INSERT INTO device_checks VALUES (1,1,'2026-11-24T00:00:00Z','offline',0);
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_pilot_report.py -q`
Expected: FAIL karena `build_incidents` belum ada.

- [ ] **Step 3: Implement Task 3**

Filter overlap: `started_at < until AND (resolved_at IS NULL OR resolved_at >= since)`. MTTD hanya untuk `device_down`/`high_latency`: cari `MIN(checked_at)` dengan status != 'online' pada 24 jam sebelum `started_at`; `mttd_sec = started_at - checked_at` (minimal 0). MTTR hanya problem resolved dan `resolved_at >= started_at`. `_median` memakai `statistics.median`, mengembalikan `None` untuk list kosong. Semua waktu lokal untuk tampilan.

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_pilot_report.py -q`
Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
git add tools/pilot_report.py tests/test_pilot_report.py
git commit -m "feat(pilot): insiden dengan MTTD dan MTTR"
```

---

### Task 4: Prediksi fiber dan alarm

**Files:**
- Modify: `tools/pilot_report.py`
- Test: `tests/test_pilot_report.py`

**Interfaces:**
- Consumes: `connect_ro`, `to_local`.
- Produces:
  - `build_predictions(conn, since_utc, until_utc) -> dict` — `{items, summary}`; item:
    `{link_name, side, target, threshold_dbm, first_evaluated_at, eta_days, rx_dbm, slope_db_per_week, crossing_at, lead_days, error_days, status}`; `status` = `"terkonfirmasi"` atau `"belum terverifikasi"`; summary `{total, confirmed, unconfirmed, median_lead_days}`.
  - `build_alarms(conn, since_utc, until_utc) -> dict` — `{items, summary}`; item:
    `{ts, problem_id, kind, severity, channel_name, status, detail, berguna}` (berguna selalu string kosong); summary `{total, sent, failed, skipped, by_channel}`.

- [ ] **Step 1: Write the failing tests**

```python
def _seed_predictions(conn):
    conn.executescript(
        """
        CREATE TABLE fiber_links (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE fiber_prediction_log (id INTEGER PRIMARY KEY, link_id INTEGER,
            side TEXT, device_id INTEGER, if_name TEXT, evaluated_at TEXT, rx_dbm REAL,
            slope_db_per_week REAL, target TEXT, threshold_dbm REAL, days REAL, severity TEXT);
        CREATE TABLE optical_hourly (device_id INTEGER, if_name TEXT, hour_ts TEXT,
            rx_min REAL, rx_avg REAL, rx_max REAL, tx_min REAL, tx_avg REAL, tx_max REAL,
            temp_min REAL, temp_avg REAL, temp_max REAL, samples INTEGER);
        INSERT INTO fiber_links VALUES (1,'Link A');
        INSERT INTO fiber_prediction_log VALUES
          (1,1,'a',1,'sfp1','2026-11-20T02:00:00Z',-14.0,-0.35,'warn',-15.0,10.0,'average'),
          (2,1,'a',1,'sfp1','2026-11-20T03:00:00Z',-14.2,-0.36,'warn',-15.0,9.0,'average');
        INSERT INTO optical_hourly VALUES
          (1,'sfp1','2026-11-25T02:00:00Z',-15.4,-15.2,-15.0,-3,-3,-3,20,20,20,30,3);
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


def test_build_alarms_keeps_rows_without_channel_and_blank_useful(tmp_path):
    conn = pilot_report.connect_ro(_make_db(tmp_path, _seed_alarms))
    data = pilot_report.build_alarms(conn, "2026-11-23T17:00:00Z", "2026-11-25T17:00:00Z")
    assert data["summary"]["total"] == 2
    assert data["summary"]["sent"] == 1
    assert data["summary"]["failed"] == 1
    assert all(item["berguna"] == "" for item in data["items"])
    assert any(item["channel_name"] is None for item in data["items"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_pilot_report.py -q`
Expected: FAIL karena `build_predictions`/`build_alarms` belum ada.

- [ ] **Step 3: Implement Task 4**

Prediksi: ambil log dalam periode (atau mulai sebelum `since` tetapi masih relevan? cukup `evaluated_at < until`), urutkan per `(link_id, side, evaluated_at)`; episode baru bila jarak > 24 jam; pakai baris pertama episode. Cari crossing di `optical_hourly` dengan `rx_avg <= threshold_dbm` dan `hour_ts >= first_evaluated_at`; `lead_days = (crossing - first) / 86400`; `error_days = lead_days - eta_days`. Tanpa crossing → "belum terverifikasi". Alarm: `action_log LEFT JOIN problems LEFT JOIN notification_channels`, filter `ts` periode, urutkan terbaru; `berguna` = `""`.

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_pilot_report.py -q`
Expected: 11 passed.

- [ ] **Step 5: Commit**

```bash
git add tools/pilot_report.py tests/test_pilot_report.py
git commit -m "feat(pilot): prediksi fiber dan rekap alarm"
```

---

### Task 5: Render Markdown/CSV, CLI lengkap, dan template laporan

**Files:**
- Modify: `tools/pilot_report.py`
- Modify: `tests/test_pilot_report.py`
- Modify: `README.md` (daftar tools)
- Create: `docs/LAPORAN-DAMPAK.md`

**Interfaces:**
- Consumes: semua builder Task 1–4.
- Produces:
  - `build_report(conn, since_utc, until_utc, tz="Asia/Jakarta", db_path="") -> dict`.
  - `render_markdown(report) -> str`.
  - `write_csvs(report, outdir) -> list[Path]`.
  - `main` menulis Markdown ke stdout (default) atau `--out DIR` (MD + 4 CSV).
- Nama CSV: `cakupan-perangkat.csv`, `insiden.csv`, `prediksi.csv`, `alarm.csv`.

- [ ] **Step 1: Write the failing tests**

```python
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
    code = pilot_report.main(["--db", str(db), "--since", "2026-11-20", "--until", "2026-11-25", "--out", str(out)])
    assert code == 0
    assert len(list(out.glob("*.md"))) == 1
    assert len(list(out.glob("*.csv"))) == 4
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_pilot_report.py -q`
Expected: FAIL karena renderer/CLI belum lengkap.

- [ ] **Step 3: Implement Task 5**

Markdown wajib memuat:
1. Judul, periode, waktu dibuat, sumber DB, dan catatan "hanya data tercatat; tidak ada data sintetis".
2. `## 1. Cakupan & jendela observasi` — tabel per hari + tabel per perangkat; bila kosong → "Belum terverifikasi (belum ada pemeriksaan pada periode ini)."
3. `## 2. Insiden` — ringkasan MTTD/MTTR + tabel insiden; definisi MTTD/MTTR ditulis eksplisit.
4. `## 3. Prediksi degradasi fiber` — tabel prediksi + status; bila kosong → belum terverifikasi.
5. `## 4. Alarm & notifikasi` — tabel alarm dengan kolom "Berguna? (isi Tim IT)" kosong.
6. `## 5. Batasan` — jam kerja, data hanya saat laptop hidup, prediksi linier bukan jaminan, angka `n/a` berarti bukti belum ada.

CSV: header tetap (termasuk kolom `berguna`), UTF-8, `newline=""`. `--out` membuat direktori; nama file Markdown `pilot-report-YYYYMMDD-HHMM.md` (waktu lokal). Update README baris `tools/` untuk menyebut `pilot_report.py`. Buat `docs/LAPORAN-DAMPAK.md` sebagai template isian (placeholder jelas, tanpa angka palsu) yang menyuruh menjalankan alat ini.

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_pilot_report.py -q`
Expected: 14 passed.

- [ ] **Step 5: Jalankan pada DB nyata (bukan tes)**

Run: `python tools/pilot_report.py --days 7`
Expected: exit 0, laporan tampil, bagian tanpa data berbunyi "Belum terverifikasi". Simpan keluaran nyata hanya jika sudah ada data pilot; jangan mengarang.

- [ ] **Step 6: Commit**

```bash
git add tools/pilot_report.py tests/test_pilot_report.py README.md docs/LAPORAN-DAMPAK.md
git commit -m "feat(pilot): laporan Markdown/CSV + template laporan dampak"
```

---

## Self-Review

- **Spec coverage:** §8.3 (laporan dampak, cakupan, insiden, alarm, prediksi, batasan) → Task 2–5. `ROADMAP` P2 (MTTD/MTTR, lead time, kolom berguna) → Task 3–5. Tanpa UI baru → sesuai §8.3.
- **Step scan:** setiap langkah punya perintah/assertion yang bisa diperiksa; tidak ada "handle edge cases" tanpa nama.
- **Type consistency:** `since_utc`/`until_utc` dipakai konsisten; item prediksi memakai `lead_days`/`error_days` di tes dan renderer.
- **Review focus:** setiap baris punya tes di task yang memiliki kode (empty DB, hari lokal, overlap, prediksi tak terkonfirmasi, alarm tanpa channel, NULL).
- **Proportion:** rencana ±5 task; implementasi di satu file + tes + template dokumen.
