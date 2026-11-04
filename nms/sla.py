"""nms.sla — validasi definisi SLA dan perhitungan SLI/error budget.

Modul ini tidak bergantung pada Flask. Waktu disimpan sebagai string lokal
``YYYY-MM-DD HH:MM:SS`` seperti kolom ``service_history``. Zona waktu SLA
dipakai untuk batas hari/periode dan label jadwal; timestamp riwayat
diperlakukan sebagai wall-clock zona tersebut.
"""

import json
import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

REPORTING_PERIODS = ("daily", "weekly", "monthly", "quarterly", "annually")
REPORTING_LABELS = {
    "daily": "Daily",
    "weekly": "Weekly",
    "monthly": "Monthly",
    "quarterly": "Quarterly",
    "annually": "Annually",
}
TAG_OPERATORS = (
    "equals",
    "contains",
    "exists",
    "not_equals",
    "not_contains",
    "not_exists",
)
DAY_NAMES = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)
DAY_LABELS = {
    "monday": "Mon",
    "tuesday": "Tue",
    "wednesday": "Wed",
    "thursday": "Thu",
    "friday": "Fri",
    "saturday": "Sat",
    "sunday": "Sun",
}
DAY_INDEX = {name: index for index, name in enumerate(DAY_NAMES)}
TIMEZONE_LABELS = {
    "Asia/Jakarta": "WIB",
    "Asia/Pontianak": "WIB",
    "Asia/Makassar": "WITA",
    "Asia/Jayapura": "WIT",
    "Asia/Singapore": "SGT",
    "Asia/Bangkok": "ICT",
    "Asia/Ho_Chi_Minh": "ICT",
    "UTC": "UTC",
}
MAX_SAMPLE_GAP_SECONDS = 300
TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


class SLAValidationError(ValueError):
    """Payload SLA tidak valid; pesan aman untuk ditampilkan."""


def _as_text(value, limit=255):
    text = str(value or "").strip()
    if len(text) > limit:
        raise SLAValidationError(f"Teks terlalu panjang (maks {limit} karakter)")
    return text


def _parse_hhmm(value):
    match = TIME_RE.match(str(value or "").strip())
    if not match:
        raise SLAValidationError("Jam harus format HH:MM")
    return int(match.group(1)), int(match.group(2))


def _time_text(hour, minute):
    return f"{hour:02d}:{minute:02d}"


def parse_hours(value):
    """Ubah '8:00-17:00, 18:00-20:00' menjadi daftar (start, end)."""
    items = value if isinstance(value, (list, tuple)) else str(value or "").split(",")
    ranges = []
    for item in items:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            start, end = item
        else:
            text = str(item or "").strip()
            if not text:
                continue
            if "-" not in text:
                raise SLAValidationError("Rentang jam harus format HH:MM-HH:MM")
            start, end = text.split("-", 1)
        sh, sm = _parse_hhmm(start)
        eh, em = _parse_hhmm(end)
        if (sh, sm) >= (eh, em):
            raise SLAValidationError("Jam mulai harus lebih awal dari jam selesai")
        ranges.append((_time_text(sh, sm), _time_text(eh, em)))
    ranges.sort()
    for previous, current in zip(ranges, ranges[1:]):
        if current[0] < previous[1]:
            raise SLAValidationError("Rentang jam tidak boleh tumpang tindih")
    return ranges


def normalize_schedule(raw):
    """Kembalikan schedule kanonik: {'mode':'24x7'} atau days/ranges."""
    if raw in (None, "", "24x7"):
        return {"mode": "24x7"}
    if isinstance(raw, str):
        text = raw.strip()
        if text.lower() in ("24x7", "24/7", "always"):
            return {"mode": "24x7"}
        if text.lower() == "custom":
            raise SLAValidationError("Schedule custom harus menyertakan hari")
        try:
            raw = json.loads(text)
        except (TypeError, ValueError):
            raise SLAValidationError("Schedule tidak valid")
    if not isinstance(raw, dict):
        raise SLAValidationError("Schedule tidak valid")
    if str(raw.get("mode") or "").lower() not in ("custom", "24x7"):
        raise SLAValidationError("Mode schedule harus 24x7 atau custom")
    if str(raw.get("mode")).lower() == "24x7":
        return {"mode": "24x7"}

    days_raw = raw.get("days")
    days = {}
    if isinstance(days_raw, dict):
        items = list(days_raw.items())
    elif isinstance(days_raw, (list, tuple)):
        items = []
        for item in days_raw:
            if not isinstance(item, dict):
                raise SLAValidationError("Hari schedule tidak valid")
            day = str(item.get("day") or "").strip().lower()
            items.append((day, item.get("hours", item.get("ranges"))))
    else:
        raise SLAValidationError("Schedule custom harus menyertakan hari")

    for day, hours in items:
        day = str(day or "").strip().lower()
        if day not in DAY_INDEX:
            raise SLAValidationError(f"Hari schedule tidak valid: {day}")
        ranges = parse_hours(hours)
        if ranges:
            days[day] = ranges
    if not days:
        raise SLAValidationError("Pilih minimal satu hari dan jam untuk schedule custom")
    return {"mode": "custom", "days": {d: days[d] for d in DAY_NAMES if d in days}}


def normalize_tags(raw):
    """Normalisasi tag SLA menjadi list {name, op, value}."""
    if raw in (None, ""):
        items = []
    elif isinstance(raw, str):
        items = []
        for item in raw.split(","):
            item = item.strip()
            if item:
                if ":" in item:
                    name, value = item.split(":", 1)
                    items.append({"name": name, "op": "equals", "value": value})
                else:
                    items.append({"name": item, "op": "exists", "value": ""})
    elif isinstance(raw, (list, tuple)):
        items = list(raw)
    else:
        raise SLAValidationError("Service tags tidak valid")

    result = []
    seen = set()
    for item in items:
        if isinstance(item, str):
            if ":" in item:
                name, value = item.split(":", 1)
                item = {"name": name, "op": "equals", "value": value}
            else:
                item = {"name": item, "op": "exists", "value": ""}
        if not isinstance(item, dict):
            raise SLAValidationError("Service tag tidak valid")
        name = _as_text(item.get("name"), 64)
        op = str(item.get("op") or "equals").strip().lower()
        value = _as_text(item.get("value"), 64)
        if op not in TAG_OPERATORS:
            raise SLAValidationError(f"Operator tag tidak valid: {op}")
        if not name:
            raise SLAValidationError("Nama service tag wajib diisi")
        if op not in ("exists", "not_exists") and not value:
            raise SLAValidationError("Value service tag wajib diisi")
        key = (name.casefold(), op, value.casefold())
        if key in seen:
            continue
        seen.add(key)
        result.append({"name": name, "op": op, "value": value})
    if len(result) > 20:
        raise SLAValidationError("Maksimal 20 service tag")
    if not result:
        raise SLAValidationError("Minimal satu service tag harus diisi")
    return result


def normalize_downtimes(raw):
    """Normalisasi excluded downtime dan cek overlap."""
    if raw in (None, ""):
        return []
    if not isinstance(raw, (list, tuple)):
        raise SLAValidationError("Excluded downtimes tidak valid")
    result = []
    for item in raw:
        if not isinstance(item, dict):
            raise SLAValidationError("Excluded downtime tidak valid")
        name = _as_text(item.get("name"), 128)
        if not name:
            raise SLAValidationError("Nama excluded downtime wajib diisi")
        start_text = str(item.get("start") or item.get("start_at") or "").strip()
        try:
            start = datetime.strptime(start_text[:16], "%Y-%m-%d %H:%M")
        except (TypeError, ValueError):
            raise SLAValidationError(
                "Start time excluded downtime harus format YYYY-MM-DD HH:MM"
            )
        try:
            duration = int(float(item.get("duration_seconds", item.get("duration")) or 0))
        except (TypeError, ValueError):
            raise SLAValidationError("Durasi excluded downtime tidak valid")
        if duration <= 0 or duration > 366 * 86400:
            raise SLAValidationError("Durasi excluded downtime harus 1 detik - 366 hari")
        result.append(
            {
                "name": name,
                "start": start.strftime("%Y-%m-%d %H:%M"),
                "duration_seconds": duration,
            }
        )
    result.sort(key=lambda row: row["start"])
    for previous, current in zip(result, result[1:]):
        previous_end = datetime.strptime(previous["start"], "%Y-%m-%d %H:%M") + timedelta(
            seconds=previous["duration_seconds"]
        )
        if datetime.strptime(current["start"], "%Y-%m-%d %H:%M") < previous_end:
            raise SLAValidationError("Excluded downtime tidak boleh tumpang tindih")
    return result


def validate_payload(data):
    """Validasi payload Create/Update SLA; kembalikan dict siap simpan."""
    if not isinstance(data, dict):
        raise SLAValidationError("Payload harus JSON object")
    name = _as_text(data.get("name"), 255)
    if not name:
        raise SLAValidationError("Name wajib diisi")
    try:
        slo = round(float(data.get("slo")), 4)
    except (TypeError, ValueError):
        raise SLAValidationError("SLO harus angka 0-100")
    if not 0 <= slo <= 100:
        raise SLAValidationError("SLO harus angka 0-100")

    reporting = str(data.get("reporting_period") or "monthly").strip().lower()
    if reporting not in REPORTING_PERIODS:
        raise SLAValidationError(
            "Reporting period harus daily, weekly, monthly, quarterly, atau annually"
        )
    timezone_name = str(data.get("timezone") or "Asia/Jakarta").strip()
    try:
        ZoneInfo(timezone_name)
    except Exception:
        raise SLAValidationError("Time zone tidak valid")

    schedule = normalize_schedule(data.get("schedule"))
    try:
        effective = datetime.strptime(
            str(data.get("effective_date") or "").strip(), "%Y-%m-%d"
        ).date()
    except (TypeError, ValueError):
        raise SLAValidationError("Effective date harus format YYYY-MM-DD")

    tag_eval = str(data.get("tag_eval") or "and").strip().lower()
    if tag_eval not in ("and", "or"):
        raise SLAValidationError("Evaluasi service tag harus And atau Or")
    return {
        "name": name,
        "slo": slo,
        "reporting_period": reporting,
        "timezone": timezone_name,
        "schedule": json.dumps(schedule, ensure_ascii=False, separators=(",", ":")),
        "effective_date": effective.strftime("%Y-%m-%d"),
        "description": _as_text(data.get("description"), 2048),
        "enabled": 1 if data.get("enabled", True) else 0,
        "tag_eval": tag_eval,
        "tags": normalize_tags(data.get("tags")),
        "downtimes": normalize_downtimes(data.get("downtimes")),
    }


def timezone_label(name):
    suffix = TIMEZONE_LABELS.get(name)
    return f"{name} ({suffix})" if suffix else name


def reporting_label(period):
    return REPORTING_LABELS.get(period, str(period or "").title())


def schedule_label(schedule):
    if not schedule or schedule.get("mode") != "custom":
        return "24x7"
    days = schedule.get("days") or {}
    ordered = [day for day in DAY_NAMES if day in days]
    if not ordered:
        return "Custom"
    groups = {}
    for day in ordered:
        key = "; ".join(f"{start}-{end}" for start, end in days[day])
        groups.setdefault(key, []).append(day)
    parts = []
    for hours, group_days in groups.items():
        indices = [DAY_INDEX[day] for day in group_days]
        if len(indices) > 2 and indices == list(range(indices[0], indices[-1] + 1)):
            label = f"{DAY_LABELS[group_days[0]]}-{DAY_LABELS[group_days[-1]]}"
        else:
            label = ", ".join(DAY_LABELS[day] for day in group_days)
        parts.append(f"{label} {hours}")
    return "; ".join(parts)


def _zone(timezone_name):
    try:
        return ZoneInfo(timezone_name)
    except Exception:
        return ZoneInfo("Asia/Jakarta")


def period_bounds(period, timezone_name, reference=None):
    """Batas periode berjalan (start, end) dalam wall-clock zona SLA."""
    tz = _zone(timezone_name)
    now = reference or datetime.now(tz)
    now = now.replace(tzinfo=tz) if now.tzinfo is None else now.astimezone(tz)
    today = now.date()
    if period == "daily":
        start_date = today
    elif period == "weekly":
        start_date = today - timedelta(days=today.weekday())
    elif period == "quarterly":
        start_date = date(today.year, ((today.month - 1) // 3) * 3 + 1, 1)
    elif period == "annually":
        start_date = date(today.year, 1, 1)
    else:
        start_date = date(today.year, today.month, 1)
    return datetime.combine(start_date, time.min), now.replace(tzinfo=None)


def report_bounds(date_from, date_to, timezone_name, reference=None):
    """Batas report custom; tanggal akhir inklusif, tidak melewati sekarang."""
    tz = _zone(timezone_name)
    now = reference or datetime.now(tz)
    now = now.replace(tzinfo=tz) if now.tzinfo is None else now.astimezone(tz)
    start = datetime.combine(date_from, time.min)
    end = datetime.combine(date_to + timedelta(days=1), time.min)
    if end > now.replace(tzinfo=None):
        end = now.replace(tzinfo=None)
    if end <= start:
        raise SLAValidationError("Rentang tanggal tidak memiliki waktu berjalan")
    return start, end


def schedule_segments(schedule, start, end):
    """Segmen waktu yang tercakup schedule di antara start dan end."""
    if not schedule or schedule.get("mode") != "custom":
        return [(start, end)] if end > start else []
    days = schedule.get("days") or {}
    segments = []
    current = start.date()
    while current <= end.date():
        for range_start, range_end in days.get(DAY_NAMES[current.weekday()]) or []:
            sh, sm = _parse_hhmm(range_start)
            eh, em = _parse_hhmm(range_end)
            seg_start = max(datetime.combine(current, time(sh, sm)), start)
            seg_end = min(datetime.combine(current, time(eh, em)), end)
            if seg_end > seg_start:
                segments.append((seg_start, seg_end))
        current += timedelta(days=1)
    return segments


def downtime_segments(downtimes, start, end):
    segments = []
    for row in downtimes or []:
        try:
            dt_start = datetime.strptime(str(row.get("start") or "")[:16], "%Y-%m-%d %H:%M")
        except (TypeError, ValueError):
            continue
        dt_end = dt_start + timedelta(seconds=int(row.get("duration_seconds") or 0))
        dt_start, dt_end = max(dt_start, start), min(dt_end, end)
        if dt_end > dt_start:
            segments.append((dt_start, dt_end))
    return segments


def subtract_segments(base, blocks):
    """Kurangi daftar segmen base dengan blok downtime."""
    result = list(base)
    for block_start, block_end in sorted(blocks):
        next_result = []
        for seg_start, seg_end in result:
            if block_end <= seg_start or block_start >= seg_end:
                next_result.append((seg_start, seg_end))
                continue
            if seg_start < block_start:
                next_result.append((seg_start, min(block_start, seg_end)))
            if block_end < seg_end:
                next_result.append((max(block_end, seg_start), seg_end))
        result = next_result
    return [(start, end) for start, end in result if end > start]


def _overlap_seconds(start, end, segments):
    total = 0.0
    for seg_start, seg_end in segments:
        left, right = max(start, seg_start), min(end, seg_end)
        if right > left:
            total += (right - left).total_seconds()
    return total


def _parse_history_ts(value):
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(str(value or ""), fmt)
        except ValueError:
            continue
    return None


def compute_service_seconds(rows, active_segments, window_end):
    """Hitung online/offline tertimbang waktu untuk satu service."""
    online = offline = tracked = 0.0
    parsed = []
    for row in rows or []:
        ts = _parse_history_ts(row.get("timestamp"))
        if ts is not None:
            parsed.append((ts, str(row.get("status") or "").upper()))
    parsed.sort(key=lambda item: item[0])
    for index, (ts, status) in enumerate(parsed):
        next_ts = parsed[index + 1][0] if index + 1 < len(parsed) else window_end
        interval_end = min(next_ts, ts + timedelta(seconds=MAX_SAMPLE_GAP_SECONDS), window_end)
        if interval_end <= ts or status not in ("ONLINE", "OFFLINE"):
            continue
        seconds = _overlap_seconds(ts, interval_end, active_segments)
        if seconds <= 0:
            continue
        tracked += seconds
        if status == "ONLINE":
            online += seconds
        else:
            offline += seconds
    return online, offline, tracked


def sql_totals(c, service_ids, start, end):
    """Hitung online/offline tertimbang waktu per service lewat SQL (SQLite).

    Dipakai hanya bila seluruh window tertutup satu segmen jadwal, karena
    hasilnya harus identik dengan compute_service_seconds. LEAD() dan
    datetime() tidak tersedia di PostgreSQL, jadi pemanggil wajib memeriksa
    backend lebih dulu.
    """
    if not service_ids:
        return {}
    placeholders = ",".join("?" for _ in service_ids)
    start_text = start.strftime("%Y-%m-%d %H:%M:%S")
    end_text = end.strftime("%Y-%m-%d %H:%M:%S")
    query_start = (start - timedelta(seconds=MAX_SAMPLE_GAP_SECONDS)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    gap = f"+{int(MAX_SAMPLE_GAP_SECONDS)} seconds"
    c.execute(
        f"""
        SELECT service_id,
               SUM(CASE WHEN status = 'ONLINE' THEN dur ELSE 0 END) AS online,
               SUM(CASE WHEN status = 'OFFLINE' THEN dur ELSE 0 END) AS offline,
               SUM(dur) AS tracked
        FROM (
            SELECT service_id, status,
                   (julianday(MIN(COALESCE(next_ts, ?), datetime(timestamp, ?), ?))
                    - julianday(MAX(timestamp, ?))) * 86400.0 AS dur
            FROM (
                SELECT service_id, status, timestamp,
                       LEAD(timestamp) OVER (PARTITION BY service_id ORDER BY timestamp) AS next_ts
                FROM service_history
                WHERE service_id IN ({placeholders})
                  AND timestamp >= ? AND timestamp <= ?
            )
            WHERE status IN ('ONLINE', 'OFFLINE')
        )
        WHERE dur > 0
        GROUP BY service_id
        """,
        (end_text, gap, end_text, start_text) + tuple(service_ids) + (query_start, end_text),
    )
    totals = {}
    for row in c.fetchall():
        totals[int(row["service_id"])] = (
            float(row["online"] or 0),
            float(row["offline"] or 0),
            float(row["tracked"] or 0),
        )
    return totals


def parse_service_tags(raw):
    tags = []
    for item in str(raw or "").split(","):
        text = item.strip()
        if not text:
            continue
        name, value = text.split(":", 1) if ":" in text else (text, "")
        if name.strip():
            tags.append((name.strip(), value.strip()))
    return tags


def _tag_condition(condition, tags):
    name = str(condition.get("name") or "").casefold()
    value = str(condition.get("value") or "").casefold()
    op = condition.get("op") or "equals"
    matched = False
    for tag_name, tag_value in tags:
        if tag_name.casefold() != name:
            continue
        if op in ("exists", "not_exists"):
            matched = True
            break
        if op in ("equals", "not_equals") and tag_value.casefold() == value:
            matched = True
            break
        if op in ("contains", "not_contains") and value in tag_value.casefold():
            matched = True
            break
    return not matched if op in ("not_equals", "not_contains", "not_exists") else matched


def service_matches_tags(service_tags, sla_tags, tag_eval="and"):
    tags = parse_service_tags(service_tags)
    results = [_tag_condition(condition, tags) for condition in sla_tags]
    if not results:
        return False
    return any(results) if str(tag_eval).lower() == "or" else all(results)


def _status_for(sli, slo, tracked, remaining, allowed):
    if tracked <= 0 or sli is None:
        return "no_data"
    if sli >= slo:
        if allowed > 0 and remaining <= allowed * 0.2:
            return "at_risk"
        return "met"
    return "breached"


def build_report(conn, c, sla, start, end, service_id=None):
    """Hitung SLI per service dan ringkasan report untuk satu SLA."""
    schedule = sla.get("schedule")
    if isinstance(schedule, str):
        try:
            schedule = json.loads(schedule)
        except (TypeError, ValueError):
            schedule = {"mode": "24x7"}
    c.execute(
        "SELECT id, name, type, ip, port, url, status, tags FROM services "
        "ORDER BY sort_order ASC, id ASC"
    )
    services = [dict(row) for row in c.fetchall()]
    matched = [
        svc
        for svc in services
        if service_matches_tags(svc.get("tags"), sla.get("tags") or [], sla.get("tag_eval"))
    ]
    if service_id:
        matched = [svc for svc in matched if int(svc["id"]) == int(service_id)]

    window_start = start
    effective_raw = str(sla.get("effective_date") or "").strip()
    if effective_raw:
        try:
            effective_start = datetime.strptime(effective_raw, "%Y-%m-%d")
            if effective_start > window_start:
                window_start = effective_start
        except ValueError:
            pass
    active = []
    if window_start < end:
        active = subtract_segments(
            schedule_segments(schedule, window_start, end),
            downtime_segments(sla.get("downtimes") or [], window_start, end),
        )
    scheduled_seconds = sum((b - a).total_seconds() for a, b in active)

    ids = [int(svc["id"]) for svc in matched]
    single_window = (
        len(active) == 1 and active[0][0] <= window_start and active[0][1] >= end
    )
    sql_path = bool(ids) and single_window and not getattr(conn, "_postgres", False)
    sql_by_service = sql_totals(c, ids, window_start, end) if sql_path else {}
    by_service = {}
    if ids and not sql_path:
        placeholders = ",".join("?" for _ in ids)
        query_start = (start - timedelta(seconds=MAX_SAMPLE_GAP_SECONDS)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        c.execute(
            "SELECT service_id, status, timestamp FROM service_history "
            f"WHERE service_id IN ({placeholders}) AND timestamp >= ? AND timestamp <= ? "
            "ORDER BY service_id ASC, timestamp ASC, id ASC",
            tuple(ids) + (query_start, end.strftime("%Y-%m-%d %H:%M:%S")),
        )
        for row in c.fetchall():
            by_service.setdefault(int(row["service_id"]), []).append(dict(row))

    rows = []
    total_online = total_offline = total_tracked = 0.0
    for svc in matched:
        if sql_path:
            online, offline, tracked = sql_by_service.get(int(svc["id"]), (0.0, 0.0, 0.0))
        else:
            online, offline, tracked = compute_service_seconds(
                by_service.get(int(svc["id"]), []), active, end
            )
        sli = round(online / tracked * 100, 4) if tracked > 0 else None
        allowed = tracked * (100 - float(sla["slo"])) / 100 if tracked > 0 else 0
        remaining = allowed - offline
        rows.append(
            {
                "id": svc["id"],
                "name": svc["name"],
                "type": svc.get("type"),
                "target": svc.get("ip"),
                "current_status": svc.get("status"),
                "has_data": tracked > 0,
                "sli": sli,
                "online_seconds": round(online, 2),
                "downtime_seconds": round(offline, 2),
                "tracked_seconds": round(tracked, 2),
                "allowed_downtime_seconds": round(allowed, 2),
                "remaining_budget_seconds": round(remaining, 2),
                "status": _status_for(sli, float(sla["slo"]), tracked, remaining, allowed),
            }
        )
        total_online += online
        total_offline += offline
        total_tracked += tracked

    sli = round(total_online / total_tracked * 100, 4) if total_tracked > 0 else None
    slo = float(sla["slo"])
    allowed = total_tracked * (100 - slo) / 100 if total_tracked > 0 else 0
    remaining = allowed - total_offline
    if not rows:
        status = "no_services"
    elif total_tracked <= 0:
        status = "no_data"
    else:
        status = _status_for(sli, slo, total_tracked, remaining, allowed)
    if status in ("no_data", "no_services") and sla.get("effective_date"):
        try:
            if datetime.strptime(sla["effective_date"], "%Y-%m-%d").date() > end.date():
                status = "not_started"
        except (TypeError, ValueError):
            pass

    # end bersifat eksklusif (mis. To+1 hari 00:00); tampilkan tanggal inklusif.
    display_end = (end - timedelta(seconds=1)).date() if end > start else end.date()
    return {
        "sla": {"id": sla.get("id"), "name": sla.get("name"), "slo": slo},
        "from": start.strftime("%Y-%m-%d"),
        "to": display_end.strftime("%Y-%m-%d"),
        "start": start.strftime("%Y-%m-%d %H:%M:%S"),
        "end": end.strftime("%Y-%m-%d %H:%M:%S"),
        "schedule_seconds": round(scheduled_seconds, 2),
        "online_seconds": round(total_online, 2),
        "downtime_seconds": round(total_offline, 2),
        "tracked_seconds": round(total_tracked, 2),
        "scheduled_seconds": round(total_tracked, 2),
        "coverage_pct": round(total_tracked / scheduled_seconds * 100, 2)
        if scheduled_seconds > 0
        else None,
        "sli": sli,
        "status": status,
        "allowed_downtime_seconds": round(allowed, 2),
        "remaining_budget_seconds": round(remaining, 2),
        "services_count": len(matched),
        "services_with_data": sum(1 for row in rows if row["has_data"]),
        "services": rows,
    }


def serialize_sla(row, tags, downtimes, matched_services=None):
    row = dict(row)
    try:
        schedule = json.loads(row.get("schedule") or '{"mode":"24x7"}')
    except (TypeError, ValueError):
        schedule = {"mode": "24x7"}
    data = {
        "id": row.get("id"),
        "name": row.get("name"),
        "slo": row.get("slo"),
        "reporting_period": row.get("reporting_period"),
        "reporting_label": reporting_label(row.get("reporting_period")),
        "timezone": row.get("timezone"),
        "timezone_label": timezone_label(row.get("timezone")),
        "schedule": schedule,
        "schedule_label": schedule_label(schedule),
        "effective_date": row.get("effective_date"),
        "description": row.get("description") or "",
        "enabled": bool(row.get("enabled")),
        "tag_eval": row.get("tag_eval") or "and",
        "tags": tags,
        "downtimes": downtimes,
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }
    if matched_services is not None:
        data["matched_services"] = matched_services
    return data


def format_duration(seconds):
    try:
        total = max(0, int(round(float(seconds or 0))))
    except (TypeError, ValueError):
        return "—"
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    return " ".join(parts) or f"{secs}s"
