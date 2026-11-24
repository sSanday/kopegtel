"""Laporan dampak pilot NMS Kopegtel dari database nyata (read-only).

Contoh:
    .\\.venv\\Scripts\\python.exe tools\\pilot_report.py --days 7
    .\\.venv\\Scripts\\python.exe tools\\pilot_report.py --since 2026-11-20 --until 2026-11-25 --out laporan

Alat ini tidak pernah menulis ke database dan tidak mencetak kredensial.
Bagian tanpa data ditampilkan sebagai "Belum terverifikasi".
"""

import argparse
import csv
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
    if since_date is None:
        since_date = (until_date or today) - timedelta(days=days - 1)
    if until_date is None:
        until_date = today
    if since_date > until_date:
        raise ValueError("since tidak boleh setelah until")
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


def _office_bounds(day, tz, office):
    zone = _zone(tz)
    return (
        datetime.combine(day, time(hour=office[0]), tzinfo=zone),
        datetime.combine(day, time(hour=office[1]), tzinfo=zone),
    )


def _expected_office_hours(since_utc, until_utc, tz, office, now=None):
    """Hitung jam kerja (Senin-Jumat) yang seharusnya teramati."""
    since_local = to_local(since_utc, tz)
    until_local = to_local(until_utc, tz)
    now_local = to_local(now or datetime.now(timezone.utc), tz)
    day = since_local.date()
    last_day = (until_local - timedelta(microseconds=1)).date()
    expected = 0.0
    expected_days = set()
    while day <= last_day:
        if day.weekday() < 5:
            day_start = datetime.combine(day, time.min, tzinfo=_zone(tz))
            day_end = day_start + timedelta(days=1)
            office_start, office_end = _office_bounds(day, tz, office)
            lower = max(since_local, day_start, office_start)
            upper = min(until_local, day_end, office_end)
            if day == now_local.date():
                upper = min(upper, now_local)
            if upper > lower:
                expected += (upper - lower).total_seconds() / 3600
                expected_days.add(day)
        day += timedelta(days=1)
    return round(expected, 2), expected_days


def _observed_office_hours(checks, tz, office):
    """Hitung menit unik ber-cek di dalam jam kerja Senin-Jumat.

    Satu menit dihitung teramati bila ada minimal satu cek pada menit itu.
    Jeda panjang (mis. laptop sleep) tidak dihitung sebagai waktu teramati.
    """
    minutes = set()
    office_start_time = time(hour=office[0])
    office_end_time = time(hour=office[1])
    for check in checks:
        local = to_local(check["checked_at"], tz)
        if local is None or local.weekday() >= 5:
            continue
        if office_start_time <= local.time() < office_end_time:
            minutes.add(local.replace(second=0, microsecond=0))
    return round(len(minutes) / 60, 2)


def build_coverage(conn, since_utc, until_utc, tz=DEFAULT_TZ, office=(8, 17), now=None):
    """Ringkas cakupan pemeriksaan dan jam kerja pada periode laporan."""
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
    check_days = set()
    office_days = set()
    office_start_time = time(hour=office[0])
    office_end_time = time(hour=office[1])
    for check in checks:
        device_id = check["device_id"]
        seen.add(device_id)
        local = to_local(check["checked_at"], tz)
        if local is not None:
            check_days.add(local.date())
            if (
                local.weekday() < 5
                and office_start_time <= local.time() < office_end_time
            ):
                office_days.add(local.date())
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
    online_pct = round(online / non_maintenance * 100, 1) if non_maintenance else None

    windows = observe_windows([check["checked_at"] for check in checks], tz)
    days_with_data = len(check_days)
    days_with_office_data = len(office_days)
    observed_hours = _observed_office_hours(checks, tz, office)
    expected_hours, expected_days = _expected_office_hours(
        since_utc, until_utc, tz, office, now=now
    )
    coverage_pct = (
        round(observed_hours / expected_hours * 100, 1) if expected_hours else None
    )
    days_without_data = len(expected_days - office_days)

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
                "checks_non_maintenance": total,
                "online": statuses.get("online", 0),
                "degraded": statuses.get("degraded", 0),
                "offline": statuses.get("offline", 0),
                "online_pct": round(device_online / total * 100, 1) if total else None,
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
        "days_with_office_data": days_with_office_data,
        "days_expected": len(expected_days),
        "days_without_data": days_without_data,
        "observed_hours": observed_hours,
        "expected_hours": expected_hours,
        "coverage_pct": coverage_pct,
        "windows": windows,
        "per_device": per_device,
    }


def _median(values):
    values = [value for value in values if value is not None]
    return float(statistics.median(values)) if values else None


MTTD_MAX_GAP_SECONDS = 180


def _poll_interval(conn, device_id):
    """Interval polling perangkat; default aman bila kolom tidak ada."""
    try:
        row = conn.execute(
            "SELECT poll_interval FROM devices WHERE id = ?", (device_id,)
        ).fetchone()
    except sqlite3.OperationalError:
        return 60
    try:
        return max(10, int(row["poll_interval"])) if row and row["poll_interval"] else 60
    except (TypeError, ValueError):
        return 60


def _mttd_seconds(conn, device_id, started):
    """Deteksi latency hanya bila transisi online->gagal teramati rapat.

    MTTD dihitung dari cek gagal pertama pada outage yang sama, yaitu cek
    setelah cek ``online`` terakhir. Bila jeda ke cek gagal pertama lebih besar
    dari 3x interval polling (minimal 3 menit), penyebabnya adalah NMS mati/
    pindah jaringan, bukan deteksi lambat; nilai dikembalikan ``None``.
    """
    last_online_row = conn.execute(
        "SELECT MAX(checked_at) AS ts FROM device_checks"
        " WHERE device_id = ? AND status = 'online'"
        " AND COALESCE(maintenance, 0) = 0 AND checked_at <= ?",
        (device_id, _fmt_utc(started)),
    ).fetchone()
    last_online = (
        _parse_utc(last_online_row["ts"])
        if last_online_row and last_online_row["ts"]
        else None
    )
    if last_online is None:
        return None

    first_bad_row = conn.execute(
        "SELECT MIN(checked_at) AS ts FROM device_checks"
        " WHERE device_id = ? AND status != 'online'"
        " AND COALESCE(maintenance, 0) = 0"
        " AND checked_at >= ? AND checked_at <= ?",
        (device_id, _fmt_utc(last_online), _fmt_utc(started)),
    ).fetchone()
    first_bad = (
        _parse_utc(first_bad_row["ts"])
        if first_bad_row and first_bad_row["ts"]
        else None
    )
    if first_bad is None:
        return None

    max_gap = max(3 * _poll_interval(conn, device_id), MTTD_MAX_GAP_SECONDS)
    if (first_bad - last_online).total_seconds() > max_gap:
        return None
    return max(0.0, (started - first_bad).total_seconds())


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
            mttd_sec = _mttd_seconds(conn, row["device_id"], started)

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
        is_test = row["problem_id"] is None or (row["detail"] or "").startswith(
            "Uji kirim:"
        )
        if not is_test:
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
                "is_test": is_test,
                "berguna": "",
            }
        )

    production = [item for item in items if not item["is_test"]]
    tests = [item for item in items if item["is_test"]]
    summary = {
        "total": len(production),
        "sent": sum(1 for item in production if item["status"] == "sent"),
        "failed": sum(1 for item in production if item["status"] == "failed"),
        "skipped": sum(1 for item in production if item["status"] == "skipped"),
        "by_channel": by_channel,
        "test_total": len(tests),
        "test_sent": sum(1 for item in tests if item["status"] == "sent"),
        "test_failed": sum(1 for item in tests if item["status"] == "failed"),
        "test_skipped": sum(1 for item in tests if item["status"] == "skipped"),
    }
    return {"items": items, "summary": summary}


def build_report(conn, since_utc, until_utc, tz=DEFAULT_TZ, db_path=""):
    """Susun seluruh bagian laporan dari koneksi read-only."""
    until_local = to_local(until_utc, tz) - timedelta(seconds=1)
    return {
        "generated_at": datetime.now(_zone(tz)).strftime("%Y-%m-%d %H:%M:%S %Z"),
        "tz": tz,
        "db_path": db_path,
        "period_since": to_local(since_utc, tz).strftime("%Y-%m-%d %H:%M"),
        "period_until": until_local.strftime("%Y-%m-%d %H:%M"),
        "coverage": build_coverage(conn, since_utc, until_utc, tz),
        "incidents": build_incidents(conn, since_utc, until_utc, tz),
        "predictions": build_predictions(conn, since_utc, until_utc, tz),
        "alarms": build_alarms(conn, since_utc, until_utc, tz),
    }


def _md_cell(value):
    if value is None:
        return "n/a"
    return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ").strip()


def _md_table(headers, rows):
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(_md_cell(cell) for cell in row) + " |")
    return "\n".join(lines)


def _num(value, digits=2, suffix=""):
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.{digits}f}{suffix}"
    return f"{value}{suffix}"


def _status_label(value):
    if value == "belum terverifikasi":
        return "Belum terverifikasi"
    if value == "terkonfirmasi":
        return "Terkonfirmasi"
    return value


def render_markdown(report):
    """Render laporan sebagai Markdown yang siap ditempel ke dokumen."""
    coverage = report["coverage"]
    incidents = report["incidents"]
    predictions = report["predictions"]
    alarms = report["alarms"]
    lines = [
        "# Laporan Dampak Pilot NMS Kopegtel",
        "",
        f"- **Periode ({report['tz']}):** {report['period_since']} - {report['period_until']}",
        f"- **Dibuat:** {report['generated_at']}",
        f"- **Sumber:** `{report['db_path'] or 'database NMS'}` (dibaca read-only)",
        "",
        "> Laporan ini hanya memuat data yang benar-benar tercatat di database NMS."
        " Tidak ada data sintetis. Bagian yang kosong ditulis **Belum terverifikasi**;"
        " angka `n/a` berarti bukti belum tersedia, bukan nol.",
        "",
        "## 1. Cakupan & jendela observasi",
        "",
    ]

    if coverage["checks_total"] == 0:
        lines.append("Belum terverifikasi (belum ada pemeriksaan pada periode ini).")
    else:
        statuses = ", ".join(
            f"{key}={value}" for key, value in sorted(coverage["status_counts"].items())
        )
        lines.extend(
            [
                f"- Perangkat terdaftar: **{coverage['devices_registered']}**;"
                f" terpantau: **{coverage['devices_seen']}**",
                f"- Pemeriksaan: **{coverage['checks_total']}**"
                f" (maintenance: {coverage['checks_maintenance']})",
                f"- Status non-maintenance: {statuses or 'n/a'};"
                f" online: **{_num(coverage['online_pct'], 1, '%')}**",
                f"- Hari dengan data: **{coverage['days_with_data']}**;"
                f" hari dengan data di jam kerja: **{coverage['days_with_office_data']}**;"
                f" hari kerja yang diharapkan: **{coverage['days_expected']}**;"
                f" hari kerja tanpa data: **{coverage['days_without_data']}**",
                f"- Waktu teramati di dalam jam kerja (menit unik):"
                f" **{_num(coverage['observed_hours'], 2)} jam**;"
                f" cakupan terhadap {_num(coverage['expected_hours'], 1)} jam kerja"
                f" (Senin-Jumat): **{_num(coverage['coverage_pct'], 1, '%')}**",
                "",
                "### Jendela observasi per hari",
                "",
                _md_table(
                    ["Tanggal", "Mulai", "Selesai", "Jam"],
                    [
                        (window["tanggal"], window["mulai"], window["selesai"], window["jam"])
                        for window in coverage.get("windows", [])
                    ],
                ),
                "",
                "### Cakupan per perangkat",
                "",
                _md_table(
                    [
                        "Perangkat",
                        "IP",
                        "Cek",
                        "Cek non-maint",
                        "Online",
                        "Degraded",
                        "Offline",
                        "Online %",
                        "Pertama",
                        "Terakhir",
                    ],
                    [
                        (
                            device["name"],
                            device["ip"],
                            device["checks"],
                            device["checks_non_maintenance"],
                            device["online"],
                            device["degraded"],
                            device["offline"],
                            _num(device["online_pct"], 1),
                            device["first_seen"],
                            device["last_seen"],
                        )
                        for device in coverage["per_device"]
                    ],
                ),
            ]
        )

    lines.extend(
        [
            "",
            "## 2. Insiden",
            "",
            "**Definisi:** MTTD = selisih waktu dari cek gagal pertama pada outage"
            " yang sama sampai problem tercatat. Nilai hanya dihitung bila cek"
            " `online` terakhir berada <= 3x interval polling (minimal 3 menit)"
            " sebelum cek gagal pertama; bila NMS sempat mati atau pindah jaringan,"
            " hasilnya `n/a` - bukan nol. Karena poller menulis cek gagal dan"
            " problem pada siklus yang sama, nilai kecil mengukur latensi"
            " pencatatan, bukan durasi outage. Masalah fiber tidak punya gejala cek"
            " perangkat, sehingga MTTD-nya `n/a`."
            " MTTR = `resolved_at - started_at` untuk problem yang sudah selesai.",
            "",
        ]
    )
    incident_summary = incidents["summary"]
    severity_text = ", ".join(
        f"{key}={value}" for key, value in sorted(incident_summary["by_severity"].items())
    )
    lines.extend(
        [
            f"- Total: **{incident_summary['total']}**;"
            f" terbuka: **{incident_summary['open']}**;"
            f" selesai: **{incident_summary['resolved']}**",
            f"- Severity: {severity_text or 'n/a'}",
            f"- MTTD median: **{_num(incident_summary['mttd_median_sec'], 1, ' detik')}**"
            f" ({incident_summary['mttd_samples']} sampel);"
            f" MTTR median: **{_num(incident_summary['mttr_median_min'], 1, ' menit')}**;"
            f" rata-rata: **{_num(incident_summary['mttr_avg_min'], 1, ' menit')}**",
            "",
        ]
    )
    if not incidents["items"]:
        lines.append("Belum terverifikasi (tidak ada insiden tercatat pada periode ini).")
    else:
        lines.append(
            _md_table(
                [
                    "Mulai",
                    "Selesai",
                    "Durasi (menit)",
                    "Severity",
                    "Jenis",
                    "Perangkat",
                    "Link",
                    "MTTD (detik)",
                    "Status",
                    "Ack",
                ],
                [
                    (
                        item["started_at"],
                        item["resolved_at"],
                        _num(item["duration_min"], 1),
                        item["severity"],
                        item["kind"],
                        item["device_name"] or item["device_ip"],
                        item["link_name"],
                        _num(item["mttd_sec"], 1),
                        item["status"],
                        item["acked_by"],
                    )
                    for item in incidents["items"]
                ],
            )
        )

    lines.extend(["", "## 3. Prediksi degradasi fiber", ""])
    prediction_summary = predictions["summary"]
    lines.extend(
        [
            f"- Total prediksi: **{prediction_summary['total']}**;"
            f" terkonfirmasi: **{prediction_summary['confirmed']}**;"
            f" belum: **{prediction_summary['unconfirmed']}**",
            f"- Median lead time prediksi terkonfirmasi:"
            f" **{_num(prediction_summary['median_lead_days'], 1, ' hari')}**",
            "",
        ]
    )
    if not predictions["items"]:
        lines.append("Belum terverifikasi (belum ada prediksi pada periode ini).")
    else:
        lines.append(
            _md_table(
                [
                    "Link",
                    "Ujung",
                    "Target",
                    "Ambang (dBm)",
                    "Prediksi terbit",
                    "ETA (hari)",
                    "Rx (dBm)",
                    "Slope (dB/minggu)",
                    "Ambang terlewati",
                    "Lead (hari)",
                    "Selisih (hari; negatif = lebih awal dari ETA)",
                    "Status",
                ],
                [
                    (
                        item["link_name"],
                        item["side"],
                        item["target"],
                        _num(item["threshold_dbm"], 2),
                        item["first_evaluated_at"],
                        _num(item["eta_days"], 1),
                        _num(item["rx_dbm"], 2),
                        _num(item["slope_db_per_week"], 2),
                        item["crossing_at"],
                        _num(item["lead_days"], 1),
                        _num(item["error_days"], 1),
                        _status_label(item["status"]),
                    )
                    for item in predictions["items"]
                ],
            )
        )

    lines.extend(["", "## 4. Alarm & notifikasi", ""])
    alarm_summary = alarms["summary"]
    channels = ", ".join(
        f"{key}={value}" for key, value in sorted(alarm_summary["by_channel"].items())
    )
    lines.extend(
        [
            f"- Alarm produksi: **{alarm_summary['total']}**;"
            f" terkirim: **{alarm_summary['sent']}**;"
            f" gagal: **{alarm_summary['failed']}**;"
            f" dilewati: **{alarm_summary['skipped']}**",
            f"- Uji kanal (tidak dihitung sebagai alarm produksi):"
            f" **{alarm_summary['test_total']}**;"
            f" terkirim: **{alarm_summary['test_sent']}**;"
            f" gagal: **{alarm_summary['test_failed']}**",
            f"- Per channel (produksi): {channels or 'n/a'}",
            "",
        ]
    )
    if not alarms["items"]:
        lines.append("Belum terverifikasi (belum ada alarm terkirim pada periode ini).")
    else:
        lines.append(
            _md_table(
                [
                    "Waktu",
                    "Kategori",
                    "Problem",
                    "Jenis",
                    "Severity",
                    "Channel",
                    "Status",
                    "Detail",
                    "Berguna? (isi Tim IT)",
                ],
                [
                    (
                        item["ts"],
                        "uji kanal" if item["is_test"] else "produksi",
                        item["problem_id"],
                        item["kind"],
                        item["severity"],
                        item["channel_name"] or "(tanpa channel)",
                        item["status"],
                        item["detail"],
                        item["berguna"],
                    )
                    for item in alarms["items"]
                ],
            )
        )

    lines.extend(
        [
            "",
            "## 5. Batasan",
            "",
            "- Data hanya tercatat saat laptop NMS hidup; jam aktual dan jeda"
            " terlihat di tabel jendela observasi, bukan asumsi 24/7.",
            "- Cakupan dihitung terhadap jam kerja Senin-Jumat (08:00-17:00);"
            " hari libur tidak dikecualikan dan hari ini dihitung sampai waktu"
            " laporan dibuat.",
            "- Waktu teramati dihitung dari menit unik yang memiliki minimal"
            " satu cek di dalam jam kerja; jeda panjang tidak dihitung sebagai"
            " waktu teramati.",
            "- MTTD hanya dihitung bila transisi online->gagal teramati rapat;"
            " jeda karena NMS mati atau pindah jaringan menghasilkan `n/a`."
            " Nilai itu bukan nol.",
            "- Prediksi fiber adalah estimasi linier dari tren Rx; bukan jaminan."
            " Prediksi yang belum terlewati ditandai **Belum terverifikasi**.",
            "- Perangkat yang tidak terjangkau dari jaringan NMS (mis. laptop di"
            " WiFi lain) tercatat offline; gunakan `tools/pilot_connectivity.py`"
            " untuk membedakan gangguan jaringan NMS dari perangkat mati.",
            "- Uji kanal (`Uji kirim:`) tidak dihitung sebagai alarm produksi dan"
            " dilaporkan terpisah.",
            "- Angka `n/a` dan sel kosong di CSV berarti bukti belum tersedia,"
            " bukan nol.",
            "- Kolom **Berguna?** diisi manual oleh Tim IT; kosong berarti belum"
            " divalidasi.",
            "",
            "_Dokumen ini dihasilkan otomatis oleh `tools/pilot_report.py`._",
            "",
        ]
    )
    return "\n".join(lines)


CSV_SPECS = (
    (
        "cakupan-perangkat.csv",
        [
            "device_id",
            "name",
            "ip",
            "checks",
            "checks_non_maintenance",
            "online",
            "degraded",
            "offline",
            "online_pct",
            "first_seen",
            "last_seen",
        ],
        ("coverage", "per_device"),
    ),
    (
        "insiden.csv",
        [
            "id",
            "kind",
            "severity",
            "status",
            "device_name",
            "device_ip",
            "link_name",
            "started_at",
            "resolved_at",
            "duration_min",
            "acked_by",
            "acked_at",
            "mttd_sec",
            "message",
        ],
        ("incidents", "items"),
    ),
    (
        "prediksi.csv",
        [
            "link_name",
            "side",
            "target",
            "threshold_dbm",
            "first_evaluated_at",
            "eta_days",
            "rx_dbm",
            "slope_db_per_week",
            "crossing_at",
            "lead_days",
            "error_days",
            "status",
        ],
        ("predictions", "items"),
    ),
    (
        "alarm.csv",
        [
            "ts",
            "kategori",
            "problem_id",
            "kind",
            "severity",
            "channel_name",
            "status",
            "detail",
            "berguna",
        ],
        ("alarms", "items"),
    ),
)


def _csv_safe(value):
    """Cegah formula injection tanpa mengubah angka negatif (float)."""
    if isinstance(value, str) and value.startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def write_csvs(report, outdir):
    """Tulis empat CSV pendukung; kembalikan daftar path yang dibuat."""
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    paths = []
    for filename, headers, (section, key) in CSV_SPECS:
        path = outdir / filename
        rows = report[section][key]
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=headers, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                record = dict(row)
                if "kategori" in headers:
                    record["kategori"] = (
                        "uji kanal" if record.get("is_test") else "produksi"
                    )
                writer.writerow({name: _csv_safe(record.get(name)) for name in headers})
        paths.append(path)
    return paths


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
        since_utc, until_utc = parse_period(
            days=args.days, since=args.since, until=args.until, tz=args.tz
        )
    except KeyError:
        print(f"Zona waktu tidak valid: {args.tz}")
        return 2
    except ValueError as exc:
        print(f"Periode tidak valid: {exc}")
        return 2

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"Database tidak ditemukan: {db_path}")
        return 2

    try:
        conn = connect_ro(db_path)
    except sqlite3.Error as exc:
        print(f"Database tidak dapat dibuka: {exc}")
        return 2
    try:
        report = build_report(
            conn, since_utc, until_utc, tz=args.tz, db_path=str(db_path)
        )
    except sqlite3.Error as exc:
        print(f"Database tidak dapat dibaca: {exc}")
        return 2
    finally:
        conn.close()

    markdown = render_markdown(report)
    if args.out:
        outdir = Path(args.out)
        csv_paths = write_csvs(report, outdir)
        stamp = datetime.now(_zone(args.tz)).strftime("%Y%m%d-%H%M")
        md_path = outdir / f"pilot-report-{stamp}.md"
        md_path.write_text(markdown, encoding="utf-8")
        print(f"Laporan Markdown: {md_path}")
        for path in csv_paths:
            print(f"CSV: {path}")
    else:
        print(markdown)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
