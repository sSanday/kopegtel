"""Perangkat, host groups, profil, metrik, statistik interface, pemeliharaan, ringkasan, topologi."""

import json

from .connection import (
    _days_ago,
    _lock,
    transaction,
    _now,
    encrypt_secret,
    get_conn,
)
from .fiber import (
    optical_summary_counts,
)
from core.validation import (
    QUERY_PAGE_MAX,
    SQLITE_INT64_MAX,
    parse_query_int,
)

def _extract_check_reason(detail_json):
    """Ambil alasan manusiawi dari detail_json pemeriksaan terakhir (metode yang gagal)."""
    if not detail_json:
        return None
    try:
        detail = json.loads(detail_json)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(detail, dict):
        return None
    reason_label = {
        "unreachable": "tidak merespons ping",
        "timeout": "tidak merespons (timeout)",
        "error": "gagal memeriksa",
        "auth_failed": "autentikasi gagal",
        "api_disabled": "API tidak aktif",
        "snmp_no_response": "SNMP tidak merespons",
    }
    for method in ("ping", "snmp", "mikrotik"):
        entry = detail.get(method)
        if isinstance(entry, dict) and not entry.get("ok", True):
            reason = str(entry.get("reason") or "")
            detail_text = entry.get("detail")
            if reason in reason_label:
                return reason_label[reason]
            if detail_text:
                return str(detail_text)
            if reason:
                return reason
    return None


def row_to_device(row):
    """Bentuk dict publik - tanpa secret."""
    device = dict(row)
    has_snmp = bool(device.get("snmp_community") or device.get("snmp_v3_auth_password"))
    has_mikrotik = bool(device.get("mikrotik_password"))

    mem_pct = None
    mem_used, mem_total = device.get("mem_used"), device.get("mem_total")
    if mem_total and mem_used is not None and mem_total > 0:
        mem_pct = round(100.0 * mem_used / mem_total, 1)

    disk_pct = None
    disk_used, disk_total = device.get("disk_used"), device.get("disk_total")
    if disk_total and disk_used is not None and disk_total > 0:
        disk_pct = round(100.0 * disk_used / disk_total, 1)

    return {
        "id": device["id"],
        "name": device["name"],
        "ip": device["ip"],
        "type": device["type"],
        "location": device["location"],
        "group_id": device.get("group_id"),
        "group_name": device.get("group_name"),
        "parent_id": device.get("parent_id"),
        "parent_name": device.get("parent_name"),
        "maintenance": bool(device.get("maintenance")),
        "poll_interval": device["poll_interval"],
        "status": device["status"],
        "latency_ms": device["latency_ms"],
        "last_seen": device["last_seen"],
        "last_checked": device.get("last_checked"),
        "check_reason": _extract_check_reason(device.get("last_check_detail")),
        "snmp_enabled": bool(device["snmp_enabled"]),
        "snmp_version": device["snmp_version"],
        "snmp_port": device["snmp_port"],
        "snmp_v3_user": device["snmp_v3_user"],
        "snmp_v3_auth_proto": device["snmp_v3_auth_proto"],
        "snmp_v3_priv_proto": device["snmp_v3_priv_proto"],
        "has_snmp_credential": has_snmp,
        "mikrotik_enabled": bool(device["mikrotik_enabled"]),
        "mikrotik_port": device["mikrotik_port"],
        "mikrotik_user": device["mikrotik_user"],
        "mikrotik_ssl": bool(device["mikrotik_ssl"]),
        "mikrotik_ssl_fingerprint": device.get("mikrotik_ssl_fingerprint"),
        "has_mikrotik_credential": has_mikrotik,
        "routeros_version": device.get("routeros_version"),
        "mikrotik_model": device.get("mikrotik_model"),
        "mikrotik_board": device.get("mikrotik_board"),
        "mikrotik_architecture": device.get("mikrotik_architecture"),
        "mikrotik_serial": device.get("mikrotik_serial"),
        "mikrotik_firmware": device.get("mikrotik_firmware"),
        "cpu_count": device.get("cpu_count"),
        "cpu_frequency_mhz": device.get("cpu_frequency_mhz"),
        "metrics_error": device.get("metrics_error"),
        "threshold_cpu_warn": device.get("threshold_cpu_warn", 70),
        "threshold_cpu_crit": device.get("threshold_cpu_crit", 90),
        "threshold_mem_warn": device.get("threshold_mem_warn", 70),
        "threshold_mem_crit": device.get("threshold_mem_crit", 90),
        "threshold_storage_warn": device.get("threshold_storage_warn", 70),
        "threshold_storage_crit": device.get("threshold_storage_crit", 90),
        "threshold_temp_warn": device.get("threshold_temp_warn", 60),
        "threshold_temp_crit": device.get("threshold_temp_crit", 75),
        "cpu_pct": device.get("cpu_pct"),
        "mem_pct": mem_pct,
        "storage_pct": disk_pct,
        "temp_c": device.get("temp_c"),
        "uptime_s": device.get("uptime_s"),
        "metrics_ts": device.get("metrics_ts"),
        "created_at": device["created_at"],
        "updated_at": device["updated_at"],
    }

_LATEST_METRICS_JOIN = """
    LEFT JOIN (
        SELECT device_id, MAX(id) AS last_id
        FROM device_metrics
        GROUP BY device_id
    ) AS latest ON latest.device_id = devices.id
    LEFT JOIN device_metrics AS m ON m.id = latest.last_id
    LEFT JOIN (
        SELECT device_id, MAX(id) AS last_check_id
        FROM device_checks
        GROUP BY device_id
    ) AS latest_check ON latest_check.device_id = devices.id
    LEFT JOIN device_checks AS c ON c.id = latest_check.last_check_id
    LEFT JOIN host_groups AS g ON g.id = devices.group_id
    LEFT JOIN devices AS parent ON parent.id = devices.parent_id
"""

_LATEST_METRICS_SELECT = (
    "devices.*, m.cpu_pct AS cpu_pct, m.mem_used AS mem_used, m.mem_total AS mem_total,"
    " m.disk_used AS disk_used, m.disk_total AS disk_total, m.ts AS metrics_ts,"
    " m.temp_c AS temp_c, m.uptime_s AS uptime_s,"
    " c.checked_at AS last_checked, c.detail_json AS last_check_detail,"
    " g.name AS group_name, parent.name AS parent_name"
)

def list_devices():
    with _lock:
        rows = get_conn().execute(
            f"SELECT {_LATEST_METRICS_SELECT} FROM devices {_LATEST_METRICS_JOIN}"
            f" ORDER BY name COLLATE NOCASE"
        ).fetchall()
        return [row_to_device(r) for r in rows]

def availability_map(days=1):
    """Peta device_id -> ketersediaan (persen) dalam N hari terakhir (satu query)."""
    with _lock:
        rows = get_conn().execute(
            """
            SELECT device_id, COUNT(*) AS total,
                   SUM(CASE WHEN status IN ('online', 'degraded') THEN 1 ELSE 0 END) AS up
            FROM device_checks
            WHERE checked_at >= ? AND COALESCE(maintenance, 0) = 0
            GROUP BY device_id
            """,
            (_days_ago(days),),
        ).fetchall()
    result = {}
    for row in rows:
        total = row["total"] or 0
        result[row["device_id"]] = round(100.0 * (row["up"] or 0) / total, 2) if total else None
    return result

def query_devices(search="", status="", device_type="", group_id=None, page=1, per_page=20,
                  routeros_version="", mikrotik_model=""):
    """List perangkat dengan filter server-side + pagination."""
    page = parse_query_int(page, 1, min_value=1, max_value=QUERY_PAGE_MAX)
    per_page = parse_query_int(per_page, 20, min_value=1, max_value=100)
    where, params = [], []
    if search:
        like = f"%{search.strip().lower()}%"
        where.append("(lower(devices.name) LIKE ? OR lower(devices.ip) LIKE ? OR lower(devices.location) LIKE ? OR lower(devices.type) LIKE ?)")
        params.extend([like, like, like, like])
    if status:
        where.append("devices.status = ?")
        params.append(status)
    if device_type:
        where.append("devices.type = ?")
        params.append(device_type)
    if routeros_version:
        where.append("COALESCE(devices.routeros_version, '') = ?")
        params.append(routeros_version)
    if mikrotik_model:
        where.append("COALESCE(devices.mikrotik_model, '') = ?")
        params.append(mikrotik_model)
    if group_id is not None:
        if str(group_id) == "none":
            where.append("devices.group_id IS NULL")
        else:
            where.append("devices.group_id = ?")
            params.append(
                parse_query_int(group_id, 0, min_value=0, max_value=SQLITE_INT64_MAX)
            )
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    with _lock:
        total = get_conn().execute(f"SELECT COUNT(*) AS n FROM devices {where_sql}", params).fetchone()["n"]
        rows = get_conn().execute(
            f"SELECT {_LATEST_METRICS_SELECT} FROM devices {_LATEST_METRICS_JOIN} {where_sql}"
            f" ORDER BY name COLLATE NOCASE LIMIT ? OFFSET ?",
            [*params, per_page, (page - 1) * per_page],
        ).fetchall()
    availability_24h = availability_map(1)
    devices = []
    for row in rows:
        device = row_to_device(row)
        device["availability_24h"] = availability_24h.get(device["id"])
        devices.append(device)
    return {
        "devices": devices,
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": max(1, -(-total // per_page)),
    }

def get_device(device_id, include_secrets=False):
    with _lock:
        if include_secrets:
            row = get_conn().execute(
                "SELECT * FROM devices WHERE id = ?", (device_id,)
            ).fetchone()
        else:
            row = get_conn().execute(
                f"SELECT {_LATEST_METRICS_SELECT} FROM devices {_LATEST_METRICS_JOIN} WHERE devices.id = ?",
                (device_id,),
            ).fetchone()
    if row is None:
        return None
    if include_secrets:
        return dict(row)
    return row_to_device(row)

def ip_exists(ip, exclude_id=None):
    with _lock:
        row = get_conn().execute(
            "SELECT id FROM devices WHERE ip = ? AND (? IS NULL OR id != ?)",
            (ip, exclude_id, exclude_id),
        ).fetchone()
        return row is not None


def get_device_by_ip(ip):
    """Cari perangkat berdasarkan IP persis; tanpa secret."""
    with _lock:
        row = get_conn().execute(
            "SELECT * FROM devices WHERE ip = ?", (ip,)
        ).fetchone()
    if row is None:
        return None
    return row_to_device(row)


def name_exists(name, exclude_id=None):
    with _lock:
        row = get_conn().execute(
            "SELECT id FROM devices WHERE name = ? COLLATE NOCASE AND (? IS NULL OR id != ?)",
            (name, exclude_id, exclude_id),
        ).fetchone()
        return row is not None

def create_device(data):
    now = _now()
    with transaction() as conn:
        cursor = conn.execute(
            """
            INSERT INTO devices (
                name, ip, type, location, poll_interval, status, group_id, parent_id, maintenance,
                snmp_enabled, snmp_version, snmp_community, snmp_port,
                snmp_v3_user, snmp_v3_auth_proto, snmp_v3_auth_password,
                snmp_v3_priv_proto, snmp_v3_priv_password,
                mikrotik_enabled, mikrotik_port, mikrotik_user, mikrotik_password,
                mikrotik_ssl, mikrotik_ssl_fingerprint, created_at, updated_at
            ) VALUES (?,?,?,?,?,'unknown',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                data["name"], data["ip"], data["type"], data["location"], data["poll_interval"],
                data.get("group_id"), data.get("parent_id"), 1 if data.get("maintenance") else 0,
                1 if data["snmp_enabled"] else 0, data["snmp_version"],
                encrypt_secret(data.get("snmp_community")), data["snmp_port"],
                data.get("snmp_v3_user"), data["snmp_v3_auth_proto"],
                encrypt_secret(data.get("snmp_v3_auth_password")),
                data["snmp_v3_priv_proto"],
                encrypt_secret(data.get("snmp_v3_priv_password")),
                1 if data["mikrotik_enabled"] else 0, data["mikrotik_port"],
                data.get("mikrotik_user"), encrypt_secret(data.get("mikrotik_password")),
                1 if data["mikrotik_ssl"] else 0,
                data.get("mikrotik_ssl_fingerprint"), now, now,
            ),
        )
        return cursor.lastrowid

def update_device(device_id, data):
    """Update perangkat.

    Secret kosong berarti 'pertahankan yang tersimpan' HANYA bila IP tidak
    berubah. Saat IP berubah, kredensial lama tidak boleh ikut dikirim ke host
    baru: field secret yang kosong/tidak dikirim akan dikosongkan (fail-safe),
    dan lapisan route mewajibkan operator memasukkan ulang kredensial.
    """
    existing = get_device(device_id, include_secrets=True)
    if existing is None:
        return False

    ip_changed = str(data.get("ip") or "").strip() != str(existing.get("ip") or "").strip()

    def pick_secret(key, current):
        new = data.get(key)
        if new is None:
            return None if ip_changed else current
        new = str(new).strip()
        if new:
            return encrypt_secret(new)
        return None if ip_changed else current

    # Fingerprint bersifat opsional & tidak dienkripsi (bukan rahasia); field yang
    # tidak dikirim mempertahankan nilai tersimpan, string kosong (None) menghapus.
    # Pin milik host lama tidak berlaku untuk IP baru.
    if "mikrotik_ssl_fingerprint" in data:
        mikrotik_ssl_fingerprint = data.get("mikrotik_ssl_fingerprint")
    elif ip_changed:
        mikrotik_ssl_fingerprint = None
    else:
        mikrotik_ssl_fingerprint = existing.get("mikrotik_ssl_fingerprint")

    snmp_enabled = data.get("snmp_enabled", bool(existing["snmp_enabled"]))
    mikrotik_enabled = data.get("mikrotik_enabled", bool(existing["mikrotik_enabled"]))

    if not snmp_enabled:
        # Toggle dimatikan → kredensial SNMP dihapus.
        snmp_community = None
        snmp_v3_auth_password = None
        snmp_v3_priv_password = None
    else:
        snmp_community = pick_secret("snmp_community", existing["snmp_community"])
        snmp_v3_auth_password = pick_secret("snmp_v3_auth_password", existing["snmp_v3_auth_password"])
        snmp_v3_priv_password = pick_secret("snmp_v3_priv_password", existing["snmp_v3_priv_password"])

    if not mikrotik_enabled:
        mikrotik_password = None
    else:
        mikrotik_password = pick_secret("mikrotik_password", existing["mikrotik_password"])

    now = _now()
    with transaction() as conn:
        conn.execute(
            """
            UPDATE devices SET
                name=?, ip=?, type=?, location=?, poll_interval=?,
                group_id=?, parent_id=?, maintenance=?,
                snmp_enabled=?, snmp_version=?, snmp_community=?, snmp_port=?,
                snmp_v3_user=?, snmp_v3_auth_proto=?, snmp_v3_auth_password=?,
                snmp_v3_priv_proto=?, snmp_v3_priv_password=?,
                mikrotik_enabled=?, mikrotik_port=?, mikrotik_user=?, mikrotik_password=?,
                mikrotik_ssl=?, mikrotik_ssl_fingerprint=?, updated_at=?
            WHERE id=?
            """,
            (
                data["name"], data["ip"], data["type"], data["location"], data["poll_interval"],
                data.get("group_id"), data.get("parent_id"), 1 if data.get("maintenance") else 0,
                1 if snmp_enabled else 0, data["snmp_version"], snmp_community, data["snmp_port"],
                data.get("snmp_v3_user"), data["snmp_v3_auth_proto"], snmp_v3_auth_password,
                data["snmp_v3_priv_proto"], snmp_v3_priv_password,
                1 if mikrotik_enabled else 0, data["mikrotik_port"], data.get("mikrotik_user"),
                mikrotik_password, 1 if data["mikrotik_ssl"] else 0,
                mikrotik_ssl_fingerprint, now, device_id,
            ),
        )
    return True

def delete_device(device_id):
    with transaction() as conn:
        # Perangkat yang dihapus menjadi referensi ujung B → simpan namanya sebagai label teks.
        conn.execute(
            """
            UPDATE fiber_links
            SET end_b_label = CASE WHEN COALESCE(end_b_label, '') = '' THEN 'Perangkat dihapus: ' || (SELECT name FROM devices WHERE id = ?) ELSE end_b_label END
            WHERE device_b_id = ?
            """,
            (device_id, device_id),
        )
        cursor = conn.execute("DELETE FROM devices WHERE id = ?", (device_id,))
        return cursor.rowcount > 0

def record_check(device_id, status, latency_ms, detail, maintenance=False):
    now = _now()
    with transaction() as conn:
        conn.execute(
            "INSERT INTO device_checks (device_id, checked_at, status, latency_ms, detail_json, maintenance) VALUES (?,?,?,?,?,?)",
            (device_id, now, status, latency_ms, json.dumps(detail, ensure_ascii=False), 1 if maintenance else 0),
        )
        if status in ("online", "degraded"):
            conn.execute(
                "UPDATE devices SET status=?, latency_ms=?, last_seen=?, updated_at=? WHERE id=?",
                (status, latency_ms, now, now, device_id),
            )
        else:
            conn.execute(
                "UPDATE devices SET status=?, latency_ms=?, updated_at=? WHERE id=?",
                (status, latency_ms, now, device_id),
            )

def add_event(device_id, old_status, new_status, message):
    with transaction() as conn:
        conn.execute(
            "INSERT INTO events (device_id, old_status, new_status, message, created_at) VALUES (?,?,?,?,?)",
            (device_id, old_status, new_status, message, _now()),
        )

def recent_checks(device_id, limit=100):
    with _lock:
        rows = get_conn().execute(
            "SELECT * FROM device_checks WHERE device_id = ? ORDER BY checked_at DESC LIMIT ?",
            (device_id, limit),
        ).fetchall()
    checks = []
    for row in rows:
        detail = None
        if row["detail_json"]:
            try:
                detail = json.loads(row["detail_json"])
            except json.JSONDecodeError:
                detail = None
        checks.append(
            {
                "id": row["id"],
                "checked_at": row["checked_at"],
                "status": row["status"],
                "latency_ms": row["latency_ms"],
                "detail": detail,
            }
        )
    return checks

_STATUS_WEIGHT = {"offline": 0, "unknown": 1, "maintenance": 2, "degraded": 3, "online": 4}

def summary():
    with _lock:
        rows = get_conn().execute("SELECT status, COUNT(*) AS n FROM devices GROUP BY status").fetchall()
        maint_rows = get_conn().execute("SELECT maintenance, COUNT(*) AS n FROM devices GROUP BY maintenance").fetchall()
        problem_rows = get_conn().execute(
            "SELECT severity, COUNT(*) AS n FROM problems WHERE status = 'open' GROUP BY severity"
        ).fetchall()
        unacked = get_conn().execute(
            "SELECT COUNT(*) AS n FROM problems WHERE status = 'open' AND acked_at IS NULL"
        ).fetchone()["n"]
    counts = {row["status"]: row["n"] for row in rows}
    total = sum(counts.values())
    in_maintenance = sum(r["n"] for r in maint_rows if r["maintenance"])

    problems_active = sum(r["n"] for r in problem_rows)
    problems_high = sum(r["n"] for r in problem_rows if r["severity"] in ("high", "disaster"))

    return {
        "total": total,
        "online": counts.get("online", 0),
        "offline": counts.get("offline", 0),
        "degraded": counts.get("degraded", 0),
        "unknown": counts.get("unknown", 0),
        "maintenance": in_maintenance,
        "problems_active": problems_active,
        "problems_high": problems_high,
        "problems_unacked": unacked,
        "availability_24h": availability_all(1),
        "fiber": optical_summary_counts(),
    }

def record_metric(device_id, ts, *, cpu_pct, mem_used, mem_total, disk_used, disk_total,
                  temp_c, cpu_temp_c, voltage, uptime_s, ping_ms):
    with transaction() as conn:
        cursor = conn.execute(
            """
            INSERT INTO device_metrics (
                device_id, ts, cpu_pct, mem_used, mem_total, disk_used, disk_total,
                temp_c, cpu_temp_c, voltage, uptime_s, ping_ms
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                device_id, ts, cpu_pct, mem_used, mem_total, disk_used, disk_total,
                temp_c, cpu_temp_c, voltage, uptime_s, ping_ms,
            ),
        )
        return cursor.lastrowid

def latest_metric(device_id, before_id=None):
    """Snapshot metrik mentah terbaru sebuah perangkat (atau sebelum id tertentu)."""
    with _lock:
        if before_id is None:
            row = get_conn().execute(
                "SELECT * FROM device_metrics WHERE device_id = ? ORDER BY id DESC LIMIT 1",
                (device_id,),
            ).fetchone()
        else:
            row = get_conn().execute(
                "SELECT * FROM device_metrics WHERE device_id = ? AND id < ? ORDER BY id DESC LIMIT 1",
                (device_id, before_id),
            ).fetchone()
    return dict(row) if row else None

def update_device_snapshot(device_id, *, version=None, model=None, board=None, architecture=None,
                           serial=None, firmware=None, cpu_count=None, cpu_frequency_mhz=None):
    """Perbarui kolom snapshot perangkat (versi RouterOS, model, dll)."""
    now = _now()
    with transaction() as conn:
        conn.execute(
            """
            UPDATE devices SET
                routeros_version=?, mikrotik_model=?, mikrotik_board=?, mikrotik_architecture=?,
                mikrotik_serial=?, mikrotik_firmware=?, cpu_count=?, cpu_frequency_mhz=?,
                metrics_error=NULL, updated_at=?
            WHERE id=?
            """,
            (version, model, board, architecture, serial, firmware,
             cpu_count, cpu_frequency_mhz, now, device_id),
        )

def set_device_metrics_error(device_id, reason):
    with transaction() as conn:
        conn.execute("UPDATE devices SET metrics_error=? WHERE id=?", (reason, device_id))

def update_device_thresholds(device_id, thresholds):
    allowed = {
        "threshold_cpu_warn", "threshold_cpu_crit",
        "threshold_mem_warn", "threshold_mem_crit",
        "threshold_storage_warn", "threshold_storage_crit",
        "threshold_temp_warn", "threshold_temp_crit",
    }
    assignments, values = [], []
    for key, value in thresholds.items():
        if key in allowed:
            assignments.append(f"{key}=?")
            values.append(value)
    if not assignments:
        return False
    values.append(device_id)
    with transaction() as conn:
        cursor = conn.execute(
            f"UPDATE devices SET {', '.join(assignments)} WHERE id=?",
            values,
        )
        return cursor.rowcount > 0

def metric_points(device_id, since, limit=10000):
    """Deretan titik metrik mentah dalam rentang waktu (untuk grafik)."""
    with _lock:
        rows = get_conn().execute(
            "SELECT * FROM device_metrics WHERE device_id = ? AND ts >= ? ORDER BY ts ASC LIMIT ?",
            (device_id, since, limit),
        ).fetchall()
    points = []
    for row in rows:
        mem_pct = None
        if row["mem_total"] and row["mem_used"] is not None and row["mem_total"] > 0:
            mem_pct = round(100.0 * row["mem_used"] / row["mem_total"], 1)
        disk_pct = None
        if row["disk_total"] and row["disk_used"] is not None and row["disk_total"] > 0:
            disk_pct = round(100.0 * row["disk_used"] / row["disk_total"], 1)
        points.append(
            {
                "ts": row["ts"],
                "cpu_pct": row["cpu_pct"],
                "mem_pct": mem_pct,
                "disk_pct": disk_pct,
                "temp_c": row["temp_c"],
                "cpu_temp_c": row["cpu_temp_c"],
                "voltage": row["voltage"],
                "uptime_s": row["uptime_s"],
                "ping_ms": row["ping_ms"],
            }
        )
    return points

def latest_interfaces(device_id):
    """Baris terbaru per interface sebuah perangkat (berdasarkan id, bukan ts)."""
    with _lock:
        rows = get_conn().execute(
            """
            SELECT s.* FROM interface_stats s
            WHERE s.device_id = ?
              AND s.id = (
                  SELECT MAX(id) FROM interface_stats
                  WHERE device_id = s.device_id AND if_name = s.if_name
              )
            ORDER BY s.if_name
            """,
            (device_id,),
        ).fetchall()
    return [dict(row) for row in rows]

def interface_points(device_id, since, limit=10000):
    """Deretan bps per interface dalam rentang waktu (untuk grafik trafik)."""
    with _lock:
        rows = get_conn().execute(
            "SELECT ts, if_name, rx_bps, tx_bps FROM interface_stats WHERE device_id = ? AND ts >= ? ORDER BY ts ASC LIMIT ?",
            (device_id, since, limit),
        ).fetchall()
    series = {}
    for row in rows:
        series.setdefault(row["if_name"], []).append(
            {"ts": row["ts"], "rx_bps": row["rx_bps"], "tx_bps": row["tx_bps"]}
        )
    return [{"if_name": name, "points": points} for name, points in sorted(series.items())]

def interface_previous(device_id, if_name):
    """Counter interface terakhir yang tersimpan (query selalu mendahului insert batch)."""
    with _lock:
        row = get_conn().execute(
            "SELECT ts, rx_bytes, tx_bytes FROM interface_stats WHERE device_id = ? AND if_name = ? ORDER BY id DESC LIMIT 1",
            (device_id, if_name),
        ).fetchone()
    return dict(row) if row else None

def record_interface_stats(device_id, ts, entries):
    """entries: list dict(if_name, running, disabled, rx_bytes, tx_bytes, rx_bps, tx_bps)."""
    with transaction() as conn:
        conn.executemany(
            """
            INSERT INTO interface_stats (device_id, ts, if_name, running, disabled, rx_bytes, tx_bytes, rx_bps, tx_bps)
            VALUES (?,?,?,?,?,?,?,?,?)
            """,
            [
                (
                    device_id, ts, entry["if_name"], 1 if entry["running"] else 0,
                    1 if entry["disabled"] else 0, entry["rx_bytes"], entry["tx_bytes"],
                    entry["rx_bps"], entry["tx_bps"],
                )
                for entry in entries
            ],
        )

def list_groups():
    with _lock:
        rows = get_conn().execute(
            "SELECT g.*, (SELECT COUNT(*) FROM devices d WHERE d.group_id = g.id) AS device_count"
            " FROM host_groups g ORDER BY g.name COLLATE NOCASE"
        ).fetchall()
    return [dict(r) for r in rows]

def get_group(group_id):
    with _lock:
        row = get_conn().execute("SELECT * FROM host_groups WHERE id = ?", (group_id,)).fetchone()
    return dict(row) if row else None

def create_group(name, description=""):
    with transaction() as conn:
        cursor = conn.execute(
            "INSERT INTO host_groups (name, description, created_at) VALUES (?,?,?)",
            (name.strip(), (description or "").strip()[:300], _now()),
        )
        return cursor.lastrowid

def update_group(group_id, name, description=""):
    with transaction() as conn:
        cursor = conn.execute(
            "UPDATE host_groups SET name=?, description=? WHERE id=?",
            (name.strip(), (description or "").strip()[:300], group_id),
        )
        return cursor.rowcount > 0

def delete_group(group_id):
    """Hapus ditolak bila masih dipakai perangkat."""
    with transaction() as conn:
        used = conn.execute("SELECT COUNT(*) AS n FROM devices WHERE group_id = ?", (group_id,)).fetchone()["n"]
        if used:
            return False, used
        cursor = conn.execute("DELETE FROM host_groups WHERE id = ?", (group_id,))
        return (cursor.rowcount > 0), 0

def group_name_exists(name, exclude_id=None):
    with _lock:
        row = get_conn().execute(
            "SELECT id FROM host_groups WHERE name = ? COLLATE NOCASE AND (? IS NULL OR id != ?)",
            (name, exclude_id, exclude_id),
        ).fetchone()
        return row is not None

PROFILES_DEFAULT_THRESHOLDS = {
    "cpu_warn": 70, "cpu_crit": 90,
    "mem_warn": 70, "mem_crit": 90,
    "storage_warn": 70, "storage_crit": 90,
    "temp_warn": 60, "temp_crit": 75,
}

def list_profiles():
    with _lock:
        rows = get_conn().execute("SELECT * FROM monitoring_profiles ORDER BY device_type").fetchall()
    profiles = []
    for row in rows:
        try:
            thresholds = json.loads(row["thresholds"] or "{}")
        except json.JSONDecodeError:
            thresholds = {}
        profiles.append(
            {
                "device_type": row["device_type"],
                "interval_seconds": row["interval_seconds"],
                "thresholds": {**PROFILES_DEFAULT_THRESHOLDS, **thresholds},
            }
        )
    return profiles

def upsert_profile(device_type, interval_seconds, thresholds):
    with transaction() as conn:
        conn.execute(
            "INSERT INTO monitoring_profiles (device_type, interval_seconds, thresholds) VALUES (?,?,?)"
            " ON CONFLICT(device_type) DO UPDATE SET interval_seconds=excluded.interval_seconds, thresholds=excluded.thresholds",
            (device_type, int(interval_seconds), json.dumps(thresholds, ensure_ascii=False)),
        )

def apply_profile_to_type(device_type, interval_seconds, thresholds):
    """Terapkan profil (interval + ambang) ke semua perangkat tipe ini.

    Kembalikan daftar perangkat terdampak agar pemanggil dapat menjadwalkan
    ulang job poller dengan interval baru tanpa menunggu restart.
    """
    key_map = {
        "cpu_warn": "threshold_cpu_warn",
        "cpu_crit": "threshold_cpu_crit",
        "mem_warn": "threshold_mem_warn",
        "mem_crit": "threshold_mem_crit",
        "storage_warn": "threshold_storage_warn",
        "storage_crit": "threshold_storage_crit",
        "temp_warn": "threshold_temp_warn",
        "temp_crit": "threshold_temp_crit",
    }
    assignments = ["poll_interval=?", "updated_at=?"]
    values = [int(interval_seconds), _now()]
    for short, column in key_map.items():
        if short in thresholds:
            assignments.append(f"{column}=?")
            values.append(float(thresholds[short]))
    values.append(device_type)
    with transaction() as conn:
        conn.execute(f"UPDATE devices SET {', '.join(assignments)} WHERE type=?", values)
        rows = conn.execute(
            "SELECT id, poll_interval FROM devices WHERE type=?", (device_type,)
        ).fetchall()
    return [{"id": row["id"], "poll_interval": row["poll_interval"]} for row in rows]

def availability(device_id, days=1):
    with _lock:
        row = get_conn().execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN status IN ('online', 'degraded') THEN 1 ELSE 0 END) AS up
            FROM device_checks
            WHERE device_id = ? AND checked_at >= ? AND COALESCE(maintenance, 0) = 0
            """,
            (device_id, _days_ago(days)),
        ).fetchone()
    total = row["total"] or 0
    if total == 0:
        return {"checks": 0, "up": 0, "pct": None}
    return {"checks": total, "up": row["up"] or 0, "pct": round(100.0 * (row["up"] or 0) / total, 2)}

def inventory_summary():
    with _lock:
        by_type = get_conn().execute(
            """
            SELECT type,
                   COUNT(*) AS total,
                   SUM(CASE WHEN status = 'online' THEN 1 ELSE 0 END) AS online,
                   SUM(CASE WHEN status = 'degraded' THEN 1 ELSE 0 END) AS degraded,
                   SUM(CASE WHEN status = 'offline' THEN 1 ELSE 0 END) AS offline,
                   SUM(CASE WHEN status = 'unknown' THEN 1 ELSE 0 END) AS unknown,
                   SUM(CASE WHEN maintenance = 1 THEN 1 ELSE 0 END) AS maintenance
            FROM devices GROUP BY type ORDER BY total DESC, type
            """
        ).fetchall()
        attention = get_conn().execute(
            """
            SELECT id, name, ip, type, status, location, latency_ms, last_seen
            FROM devices WHERE status != 'online' AND maintenance = 0
            ORDER BY CASE status WHEN 'offline' THEN 0 WHEN 'degraded' THEN 1 ELSE 2 END, name
            LIMIT 50
            """
        ).fetchall()
    return {
        "by_type": [dict(r) for r in by_type],
        "attention": [dict(r) for r in attention],
        "total": sum(r["total"] for r in by_type),
    }

def topology_rows():
    """Semua perangkat untuk topologi (difilter/dipotong di lapisan API)."""
    with _lock:
        rows = get_conn().execute(
            """
            SELECT d.id, d.name, d.ip, d.type, d.status, d.location, d.latency_ms,
                   d.parent_id, d.group_id, g.name AS group_name
            FROM devices d LEFT JOIN host_groups g ON g.id = d.group_id
            ORDER BY d.name COLLATE NOCASE
            """
        ).fetchall()
    return [dict(r) for r in rows]

def list_maintenance(limit=100):
    with _lock:
        rows = get_conn().execute(
            """
            SELECT w.*, d.name AS device_name, g.name AS group_name
            FROM maintenance_windows w
            LEFT JOIN devices d ON d.id = w.device_id
            LEFT JOIN host_groups g ON g.id = w.group_id
            ORDER BY w.starts_at DESC LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
    return [dict(r) for r in rows]

def create_maintenance(name, device_id, group_id, starts_at, ends_at, note, created_by):
    with transaction() as conn:
        cursor = conn.execute(
            "INSERT INTO maintenance_windows (name, device_id, group_id, starts_at, ends_at, note, created_at, created_by)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (name.strip(), device_id, group_id, starts_at, ends_at, (note or "").strip()[:300], _now(), created_by),
        )
        return cursor.lastrowid

def delete_maintenance(window_id):
    with transaction() as conn:
        cursor = conn.execute("DELETE FROM maintenance_windows WHERE id = ?", (window_id,))
        return cursor.rowcount > 0

def device_in_maintenance(device, at_ts=None):
    """True bila perangkat dalam pemeliharaan (flag perangkat atau jendela waktu)."""
    if device.get("maintenance"):
        return True
    moment = at_ts or _now()
    device_id = device.get("id")
    group_id = device.get("group_id")
    with _lock:
        row = get_conn().execute(
            """
            SELECT COUNT(*) AS n FROM maintenance_windows
            WHERE starts_at <= ? AND ends_at >= ?
              AND (device_id = ? OR group_id IS NOT NULL AND group_id = ?
                   OR device_id IS NULL AND group_id IS NULL)
            """,
            (moment, moment, device_id, group_id),
        ).fetchone()
    return row["n"] > 0


def availability_all(days=1):
    """Rata-rata ketersediaan semua perangkat (persen) dalam N hari terakhir."""
    with _lock:
        row = get_conn().execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN status IN ('online', 'degraded') THEN 1 ELSE 0 END) AS up
            FROM device_checks
            WHERE checked_at >= ? AND COALESCE(maintenance, 0) = 0
            """,
            (_days_ago(days),),
        ).fetchone()
    total = row["total"] or 0
    if total == 0:
        return None
    return round(100.0 * (row["up"] or 0) / total, 1)
