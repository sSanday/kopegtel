"""Seed data demo prediksi tren Rx (fiber_trend).

Membuat 4 ONT demo ``DEMO-TREND-01..04`` (source="manual", pelanggan
"DEMO ...") lengkap dengan riwayat 21 hari di ``fiber_history`` (1 sampel
per 30 menit, noise ±0.3 dB):

- DEMO-TREND-01 : turun linear -19.0 -> -23.8 dBm (± -0.24 dB/hari) —
  harus terprediksi menyentuh Warn (-25) dalam beberapa hari.
- DEMO-TREND-02 : stabil -19 ± 0.3 dBm.
- DEMO-TREND-03 : turun lalu melompat +4.5 dB di hari ke-10 lalu datar —
  lompatan harus terdeteksi dan TIDAK salah diprediksi menurun
  (hanya data setelah lompatan yang dipakai).
- DEMO-TREND-04 : membaik pelan -24.0 -> -20.0 dBm.

``fiber_onts.rx_power`` diisi nilai terakhir tiap ONT.

CATATAN PENTING: ``poll_fiber_monitor`` menambah sampel datar setiap
~120 detik untuk ONT non-simulator (mengikuti nilai rx_power terakhir),
jadi uji segera setelah seeding lalu jalankan ``--cleanup``.

Pemakaian::

    python scripts/seed_fiber_trend_demo.py            # seed
    python scripts/seed_fiber_trend_demo.py --cleanup  # hapus data demo
"""

import argparse
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nms.db import _commit_with_retry, get_db, init_db  # noqa: E402

PREFIX = "DEMO-TREND-"
DAYS = 21
PER_DAY = 48  # 1 sampel / 30 menit
NOISE = 0.3


def _series_01():
    """Turun linear -19.0 -> -23.8 selama 21 hari."""
    return [(-19.0 + (i / (DAYS - 1)) * (-4.8), i) for i in range(DAYS)]


def _series_02():
    """Stabil konstan -19 dBm."""
    return [(-19.0, i) for i in range(DAYS)]


def _series_03():
    """Turun 10 hari, lompat +4.5 dB di hari ke-10, lalu datar."""
    out = []
    for i in range(DAYS):
        if i < 10:
            base = -19.0 + (i / 9) * (-1.5)  # -19.0 -> -20.5
        elif i == 10:
            base = -16.0  # lompatan mendadak +4.5 dB
        else:
            base = -16.0  # datar setelah lompatan
        out.append((base, i))
    return out


def _series_04():
    """Membaik pelan -24.0 -> -20.0."""
    return [(-24.0 + (i / (DAYS - 1)) * (+4.0), i) for i in range(DAYS)]


PROFILES = {
    "DEMO-TREND-01": ("DEMO Turun Linear", _series_01),
    "DEMO-TREND-02": ("DEMO Stabil", _series_02),
    "DEMO-TREND-03": ("DEMO Lompatan Perbaikan", _series_03),
    "DEMO-TREND-04": ("DEMO Membaik Pelan", _series_04),
}


def _demo_rows(conn, c):
    c.execute("SELECT id, ont_sn FROM fiber_onts WHERE ont_sn LIKE ?", (PREFIX + "%",))
    return {r["ont_sn"]: r["id"] for r in c.fetchall()}


def cleanup():
    conn, c = get_db()
    try:
        ids = _demo_rows(conn, c)
        if not ids:
            print("Tidak ada data demo; tidak ada yang dihapus.")
            return
        for fid in ids.values():
            c.execute("DELETE FROM fiber_history WHERE ont_id=?", (fid,))
        c.execute("DELETE FROM fiber_onts WHERE ont_sn LIKE ?", (PREFIX + "%",))
        _commit_with_retry(conn)
        print(f"Data demo dihapus: {len(ids)} ONT + riwayatnya.")
    finally:
        conn.close()


def seed():
    random.seed(42)
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    conn, c = get_db()
    try:
        existing = _demo_rows(conn, c)
        for sn, (cust, series_fn) in PROFILES.items():
            if sn in existing:
                c.execute("DELETE FROM fiber_history WHERE ont_id=?", (existing[sn],))
                c.execute("DELETE FROM fiber_onts WHERE id=?", (existing[sn],))
            series = series_fn()
            last_rx = round(series[-1][0], 2)
            stamp = now.strftime("%Y-%m-%d %H:%M:%S")
            c.execute(
                "INSERT INTO fiber_onts (ont_sn, customer, olt_name, pon_port, "
                "odp_name, rx_power, tx_power, status, source, created_at, "
                "updated_at, last_checked) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    sn,
                    cust,
                    "",
                    "",
                    "",
                    last_rx,
                    None,
                    "unknown",
                    "manual",
                    stamp,
                    stamp,
                    stamp,
                ),
            )
            c.execute("SELECT id FROM fiber_onts WHERE ont_sn=?", (sn,))
            fid = c.fetchone()["id"]
            rows = []
            for base, day_idx in series:
                day = now - timedelta(days=(DAYS - 1) - day_idx)
                dstr = day.strftime("%Y-%m-%d")
                for slot in range(PER_DAY):
                    ts = f"{dstr} {(slot // 2):02d}:{((slot % 2) * 30):02d}:00"
                    rx = round(base + random.uniform(-NOISE, NOISE), 2)
                    rows.append((fid, rx, None, ts))
            c.executemany(
                "INSERT INTO fiber_history (ont_id, rx_power, tx_power, timestamp) "
                "VALUES (?,?,?,?)",
                rows,
            )
            print(
                f"{sn} (id={fid}, {cust}): {len(rows)} sampel, rx terakhir {last_rx} dBm"
            )
        _commit_with_retry(conn)
        print(f"Selesai {today}; jalankan --cleanup setelah uji.")
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="hapus ONT demo dan seluruh riwayatnya",
    )
    args = parser.parse_args()
    # Pastikan skema tersedia (idempoten) agar demo bisa dijalankan pada
    # DB kosong/baru, bukan hanya DB produksi yang sudah terinisialisasi.
    init_db()
    try:
        if args.cleanup:
            cleanup()
        else:
            seed()
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
