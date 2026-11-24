"""Laporan dampak pilot NMS Kopegtel dari database nyata (read-only).

Contoh:
    .\\.venv\\Scripts\\python.exe tools\\pilot_report.py --days 7
    .\\.venv\\Scripts\\python.exe tools\\pilot_report.py --since 2026-11-20 --until 2026-11-25 --out laporan

Alat ini tidak pernah menulis ke database dan tidak mencetak kredensial.
Bagian tanpa data ditampilkan sebagai "Belum terverifikasi".
"""

import argparse
import os
import sqlite3
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

BASE_DIR = Path(__file__).resolve().parents[1]
DEFAULT_TZ = "Asia/Jakarta"


def _zone(tz):
    return ZoneInfo(tz)


def _as_utc(now):
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def _fmt_utc(value):
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_date(value, label):
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} harus berformat YYYY-MM-DD") from exc


def parse_period(days=7, since=None, until=None, now=None, tz=DEFAULT_TZ):
    """Hitung rentang UTC dari tanggal lokal (Asia/Jakarta secara default).

    ``since`` inklusif dan ``until`` inklusif per tanggal lokal; nilai yang
    dikembalikan memakai batas UTC dengan ``until`` eksklusif.
    """
    days = int(days)
    if days < 1:
        raise ValueError("days minimal 1")
    zone = _zone(tz)
    today = _as_utc(now).astimezone(zone).date()
    since_date = _parse_date(since, "since") if since else None
    until_date = _parse_date(until, "until") if until else None
    if since_date and until_date and since_date > until_date:
        raise ValueError("since tidak boleh setelah until")
    if since_date is None:
        since_date = (until_date or today) - timedelta(days=days - 1)
    if until_date is None:
        until_date = today
    since_dt = datetime.combine(since_date, time.min, tzinfo=zone)
    until_dt = datetime.combine(until_date + timedelta(days=1), time.min, tzinfo=zone)
    return _fmt_utc(since_dt), _fmt_utc(until_dt)


def connect_ro(path):
    """Buka SQLite mode read-only dengan row_factory sqlite3.Row."""
    path = Path(path).resolve()
    conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        default=os.environ.get("NMS_DB_PATH")
        or os.environ.get("DATABASE_PATH")
        or str(BASE_DIR / "backend" / "nms.db"),
        help="Path database NMS (default: backend/nms.db atau NMS_DB_PATH).",
    )
    parser.add_argument("--days", type=int, default=7, help="Jumlah hari terakhir (default 7).")
    parser.add_argument("--since", help="Tanggal awal lokal YYYY-MM-DD.")
    parser.add_argument("--until", help="Tanggal akhir lokal YYYY-MM-DD (inklusif).")
    parser.add_argument("--tz", default=DEFAULT_TZ, help="Zona waktu tampilan (default Asia/Jakarta).")
    parser.add_argument("--out", help="Direktori keluaran Markdown + CSV.")
    args = parser.parse_args(argv)

    try:
        parse_period(
            days=args.days, since=args.since, until=args.until, tz=args.tz
        )
    except ValueError as exc:
        print(f"Periode tidak valid: {exc}")
        return 2

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"Database tidak ditemukan: {db_path}")
        return 2

    conn = connect_ro(db_path)
    try:
        print("Belum ada data")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
