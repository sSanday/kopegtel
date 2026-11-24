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
import statistics
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


def _parse_utc(value):
    """Parse timestamp DB menjadi datetime UTC; None aman."""
    if value is None:
        return None
    if isinstance(value, datetime):
        result = value
    else:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def _fmt_local(value, tz=DEFAULT_TZ):
    local = to_local(value, tz)
    return local.strftime("%Y-%m-%d %H:%M:%S") if local else None


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


def to_local(ts_utc, tz=DEFAULT_TZ):
    """Konversi timestamp UTC (ISO string/datetime) ke zona tampilan."""
    if ts_utc is None:
        return None
    if isinstance(ts_utc, datetime):
        value = ts_utc
    else:
        value = datetime.fromisoformat(str(ts_utc).replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(_zone(tz))


def observe_windows(rows, tz=DEFAULT_TZ):
    """Kelompokkan timestamp menjadi jendela observasi per hari lokal."""
    grouped = {}
    for row in rows:
        local = to_local(row, tz)
        if local is None:
            continue
        grouped.setdefault(local.date(), []).append(local)
    windows = []
    for day in sorted(grouped):
        times = grouped[day]
        start, end = min(times), max(times)
        windows.append(
            {
                "tanggal": day.isoformat(),
                "mulai": start.strftime("%H:%M"),
                "selesai": end.strftime("%H:%M"),
                "jam": round((end - start).total_seconds() / 3600, 2),
            }
        )
    return windows


def build_coverage(conn, since_utc, until_utc, tz=DEFAULT_TZ, office=(8, 17)):
    """Ringkas cakupan pemeriksaan dan jam operasi pada periode laporan."""
    devices = conn.execute("SELECT id, name, ip FROM devices ORDER BY id").fetchall()
    checks = conn.execute(
        "SELECT device_id, checked_at, status, COALESCE(maintenance, 0) AS maintenance"
        " FROM device_checks WHERE checked_at >= ? AND checked_at < ?"
        " ORDER BY checked_at",
        (since_utc, until_utc),
    ).fetchall()

    checks_total = len(checks)
    checks_maintenance = sum(1 for check in checks if check["maintenance"])
    status_counts = {}
    per_device_raw = {}
    seen = set()
    for check in checks:
        device_id = check["device_id"]
        seen.add(device_id)
        local = to_local(check["checked_at"], tz)
        entry = per_device_raw.setdefault(
            device_id, {"checks": 0, "statuses": {}, "first": None, "last": None}
        )
        entry["checks"] += 1
        if local is not None:
            if entry["first"] is None or local < entry["first"]:
                entry["first"] = local
            if entry["last"] is None or local > entry["last"]:
                entry["last"] = local
        if not check["maintenance"]:
            status = check["status"] or "unknown"
            status_counts[status] = status_counts.get(status, 0) + 1
            entry["statuses"][status] = entry["statuses"].get(status, 0) + 1

    non_maintenance = sum(status_counts.values())
    online = status_counts.get("online", 0)
    online_pct = round(online / non_maintenance * 100, 1) if non_maintenance else 0.0

    windows = observe_windows([check["checked_at"] for check in checks], tz)
    days_with_data = len({window["tanggal"] for window in windows})
    observed_hours = round(sum(window["jam"] for window in windows), 2)
    office_hours = max(0, office[1] - office[0])
    expected_hours = float(days_with_data * office_hours)
    coverage_pct = (
        round(observed_hours / expected_hours * 100, 1) if expected_hours else 0.0
    )

    per_device = []
    for device in devices:
        entry = per_device_raw.get(
            device["id"], {"checks": 0, "statuses": {}, "first": None, "last": None}
        )
        statuses = entry["statuses"]
        total = sum(statuses.values())
        device_online = statuses.get("online", 0)
        per_device.append(
            {
                "device_id": device["id"],
                "name": device["name"],
                "ip": device["ip"],
                "checks": entry["checks"],
                "online": statuses.get("online", 0),
                "degraded": statuses.get("degraded", 0),
                "offline": statuses.get("offline", 0),
                "online_pct": round(device_online / total * 100, 1) if total else 0.0,
                "first_seen": entry["first"].strftime("%Y-%m-%d %H:%M:%S")
                if entry["first"]
                else None,
                "last_seen": entry["last"].strftime("%Y-%m-%d %H:%M:%S")
                if entry["last"]
                else None,
            }
        )

    return {
        "devices_registered": len(devices),
        "devices_seen": len(seen),
        "checks_total": checks_total,
        "checks_maintenance": checks_maintenance,
        "status_counts": status_counts,
        "online_pct": online_pct,
        "days_with_data": days_with_data,
        "observed_hours": observed_hours,
        "expected_hours": expected_hours,
        "coverage_pct": coverage_pct,
        "per_device": per_device,
    }


def _median(values):
    values = [value for value in values if value is not None]
    return float(statistics.median(values)) if values else None


def build_incidents(conn, since_utc, until_utc, tz=DEFAULT_TZ):
    """Ambil masalah yang beririsan dengan periode, plus MTTD/MTTR."""
    rows = conn.execute(
        "SELECT p.*, d.name AS device_name, d.ip AS device_ip, fl.name AS link_name"
        " FROM problems p"
        " LEFT JOIN devices d ON d.id = p.device_id"
        " LEFT JOIN fiber_links fl ON fl.id = p.link_id"
        " WHERE p.started_at < ?"
        " AND (p.resolved_at IS NULL OR p.resolved_at >= ?)"
        " ORDER BY p.started_at DESC",
        (until_utc, since_utc),
    ).fetchall()

    items = []
    for row in rows:
        started = _parse_utc(row["started_at"])
        resolved = _parse_utc(row["resolved_at"]) if row["resolved_at"] else None
        duration_min = None
        if started and resolved and resolved >= started:
            duration_min = round((resolved - started).total_seconds() / 60, 1)

        mttd_sec = None
        if row["kind"] in ("device_down", "high_latency") and row["device_id"] and started:
            lower = _fmt_utc(started - timedelta(hours=24))
            upper = _fmt_utc(started)
            first_bad = conn.execute(
                "SELECT MIN(checked_at) AS first_bad FROM device_checks"
                " WHERE device_id = ? AND status != 'online'"
                " AND COALESCE(maintenance, 0) = 0"
                " AND checked_at >= ? AND checked_at <= ?",
                (row["device_id"], lower, upper),
            ).fetchone()["first_bad"]
            if first_bad:
                mttd_sec = max(0.0, (started - _parse_utc(first_bad)).total_seconds())

        items.append(
            {
                "id": row["id"],
                "kind": row["kind"],
                "severity": row["severity"],
                "status": row["status"],
                "device_name": row["device_name"],
                "device_ip": row["device_ip"],
                "link_name": row["link_name"],
                "started_at": _fmt_local(row["started_at"], tz),
                "resolved_at": _fmt_local(row["resolved_at"], tz),
                "duration_min": duration_min,
                "acked_by": row["acked_by"],
                "acked_at": _fmt_local(row["acked_at"], tz),
                "mttd_sec": mttd_sec,
                "message": row["message"],
            }
        )

    by_severity = {}
    for item in items:
        severity = item["severity"] or "unknown"
        by_severity[severity] = by_severity.get(severity, 0) + 1
    mttr_values = [
        item["duration_min"]
        for item in items
        if item["status"] == "resolved" and item["duration_min"] is not None
    ]
    mttd_values = [item["mttd_sec"] for item in items if item["mttd_sec"] is not None]

    summary = {
        "total": len(items),
        "open": sum(1 for item in items if item["status"] == "open"),
        "resolved": sum(1 for item in items if item["status"] == "resolved"),
        "by_severity": by_severity,
        "mttr_median_min": _median(mttr_values),
        "mttr_avg_min": round(sum(mttr_values) / len(mttr_values), 1)
        if mttr_values
        else None,
        "mttd_median_sec": _median(mttd_values),
        "mttd_samples": len(mttd_values),
    }
    return {"items": items, "summary": summary}


def _split_episodes(rows):
    """Pecah log prediksi menjadi episode per link/ujung (jeda > 24 jam)."""
    episodes = []
    current = []
    last_key = None
    last_time = None
    for row in rows:
        key = (row["link_id"], row["side"])
        evaluated = _parse_utc(row["evaluated_at"])
        if current and (
            key != last_key
            or evaluated is None
            or last_time is None
            or evaluated - last_time > timedelta(hours=24)
        ):
            episodes.append(current)
            current = []
        current.append(row)
        last_key = key
        last_time = evaluated
    if current:
        episodes.append(current)
    return episodes


def build_predictions(conn, since_utc, until_utc, tz=DEFAULT_TZ):
    """Rangkum prediksi fiber dan bandingkan ETA dengan ambang nyata."""
    rows = conn.execute(
        "SELECT l.*, fl.name AS link_name"
        " FROM fiber_prediction_log l"
        " LEFT JOIN fiber_links fl ON fl.id = l.link_id"
        " WHERE l.evaluated_at IS NOT NULL AND l.evaluated_at < ?"
        " ORDER BY l.link_id, l.side, l.evaluated_at",
        (until_utc,),
    ).fetchall()

    items = []
    for episode in _split_episodes(rows):
        first = episode[0]
        last = episode[-1]
        if last["evaluated_at"] < since_utc:
            continue
        first_eval = _parse_utc(first["evaluated_at"])
        eta_days = float(first["days"]) if first["days"] is not None else None
        crossing = None
        if (
            first["device_id"] is not None
            and first["if_name"]
            and first["threshold_dbm"] is not None
            and first_eval is not None
        ):
            crossing_row = conn.execute(
                "SELECT MIN(hour_ts) AS crossing FROM optical_hourly"
                " WHERE device_id = ? AND if_name = ?"
                " AND hour_ts >= ? AND hour_ts < ?"
                " AND rx_avg IS NOT NULL AND rx_avg <= ?",
                (
                    first["device_id"],
                    first["if_name"],
                    _fmt_utc(first_eval),
                    until_utc,
                    first["threshold_dbm"],
                ),
            ).fetchone()
            crossing = crossing_row["crossing"] if crossing_row else None

        lead_days = None
        error_days = None
        if crossing and first_eval:
            lead_days = round(
                (_parse_utc(crossing) - first_eval).total_seconds() / 86400, 2
            )
            if eta_days is not None:
                error_days = round(lead_days - eta_days, 2)

        items.append(
            {
                "link_name": first["link_name"],
                "side": (first["side"] or "").upper(),
                "target": first["target"],
                "threshold_dbm": first["threshold_dbm"],
                "first_evaluated_at": _fmt_local(first["evaluated_at"], tz),
                "eta_days": eta_days,
                "rx_dbm": first["rx_dbm"],
                "slope_db_per_week": first["slope_db_per_week"],
                "crossing_at": _fmt_local(crossing, tz),
                "lead_days": lead_days,
                "error_days": error_days,
                "status": "terkonfirmasi" if crossing else "belum terverifikasi",
            }
        )

    items.sort(key=lambda item: item["first_evaluated_at"] or "", reverse=True)
    confirmed = [item for item in items if item["status"] == "terkonfirmasi"]
    summary = {
        "total": len(items),
        "confirmed": len(confirmed),
        "unconfirmed": len(items) - len(confirmed),
        "median_lead_days": _median([item["lead_days"] for item in confirmed]),
    }
    return {"items": items, "summary": summary}


def build_alarms(conn, since_utc, until_utc, tz=DEFAULT_TZ):
    """Rekap pengiriman alarm; kolom berguna dikosongkan untuk Tim IT."""
    rows = conn.execute(
        "SELECT al.*, p.kind AS problem_kind, p.severity AS problem_severity,"
        " nc.name AS channel_name"
        " FROM action_log al"
        " LEFT JOIN problems p ON p.id = al.problem_id"
        " LEFT JOIN notification_channels nc ON nc.id = al.channel_id"
        " WHERE al.ts >= ? AND al.ts < ?"
        " ORDER BY al.ts DESC",
        (since_utc, until_utc),
    ).fetchall()

    items = []
    by_channel = {}
    for row in rows:
        channel = row["channel_name"] or "(tanpa channel)"
        by_channel[channel] = by_channel.get(channel, 0) + 1
        items.append(
            {
                "ts": _fmt_local(row["ts"], tz),
                "problem_id": row["problem_id"],
                "kind": row["problem_kind"],
                "severity": row["problem_severity"],
                "channel_name": row["channel_name"],
                "status": row["status"],
                "detail": row["detail"],
                "berguna": "",
            }
        )

    summary = {
        "total": len(items),
        "sent": sum(1 for item in items if item["status"] == "sent"),
        "failed": sum(1 for item in items if item["status"] == "failed"),
        "skipped": sum(1 for item in items if item["status"] == "skipped"),
        "by_channel": by_channel,
    }
    return {"items": items, "summary": summary}


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
