"""Blueprint Perangkat: CRUD, metrik, snapshot, topology, grup, profil, pemeliharaan."""
from collections import defaultdict, deque

from flask import Blueprint, g, jsonify, request, session
import time

import db
import poller
import fiber as fiber_calc
from collectors import ping as ping_collector
from collectors import snmp as snmp_collector
from collectors import mikrotik as mikrotik_collector
from core.guard import audit, roles
from core.metrics import downsample as _downsample, range_since as _range_since
from core.validation import (
    DEVICE_TYPES,
    _SNMP_AUTH_PROTOS,
    _SNMP_PRIV_PROTOS,
    _SNMP_VERSIONS,
    _ip_allowed,
    _parse_interval,
    _parse_ip,
    _parse_optional_id,
    _parse_port,
)

bp = Blueprint("devices", __name__)

# ---------------------------------------------------------
# Rate limit untuk /api/devices/test: 8 panggilan / 60 detik per user
# ---------------------------------------------------------

_test_calls = defaultdict(deque)
RATE_LIMIT_MAX = 8
RATE_LIMIT_WINDOW = 60


def _rate_limited():
    key = session.get("user_id") or request.remote_addr or "anon"
    now = time.monotonic()
    calls = _test_calls[key]
    while calls and now - calls[0] > RATE_LIMIT_WINDOW:
        calls.popleft()
    if len(calls) >= RATE_LIMIT_MAX:
        return True
    calls.append(now)
    return False


# ---------------------------------------------------------
# Validasi perangkat → core/validation.py
# ---------------------------------------------------------


def _validate_device(data, existing_id=None):
    """Validasi & normalisasi payload perangkat.

    Mengembalikan (clean_dict, error_message). Jika error_message tidak None,
    clean_dict kosong.
    """
    if not isinstance(data, dict):
        return {}, "Payload tidak valid."

    existing_row = db.get_device(existing_id, include_secrets=True) if existing_id else None

    name = str(data.get("name") or "").strip()
    if not name:
        return {}, "Nama perangkat wajib diisi."
    if len(name) > 120:
        return {}, "Nama perangkat maksimal 120 karakter."

    addr = _parse_ip(data.get("ip"))
    if addr is None:
        return {}, "Alamat IP tidak valid."
    if not _ip_allowed(addr):
        return {}, "IP berada di luar ALLOWED_NETWORKS yang diizinkan."
    ip = str(addr)

    device_type = str(data.get("type") or (existing_row or {}).get("type") or "lainnya").strip().lower()
    if device_type not in DEVICE_TYPES:
        device_type = "lainnya"

    location = str(
        data.get("location", (existing_row or {}).get("location") if existing_row else "") or ""
    ).strip()[:160]

    group_id, error = _parse_optional_id(data.get("group_id"), "Grup")
    if error:
        return {}, error
    if group_id is not None and db.get_group(group_id) is None:
        return {}, "Grup tidak ditemukan."

    parent_id, error = _parse_optional_id(data.get("parent_id"), "Uplink")
    if error:
        return {}, error
    if parent_id is not None:
        if existing_id and parent_id == existing_id:
            return {}, "Perangkat tidak boleh menjadi uplink dirinya sendiri."
        parent = db.get_device(parent_id)
        if parent is None:
            return {}, "Perangkat uplink tidak ditemukan."
        # Cegah siklus: uplink tidak boleh berada di bawah dirinya.
        cursor_id, depth = parent_id, 0
        while cursor_id is not None and depth < 50:
            ancestor = db.get_device(cursor_id)
            cursor_id = (ancestor or {}).get("parent_id")
            if existing_id and cursor_id == existing_id:
                return {}, "Uplink ini membuat siklus rantai uplink."
            depth += 1

    settings = db.get_settings()
    default_interval = settings.get("default_poll_interval", "60")
    poll_interval, error = _parse_interval(data.get("poll_interval", default_interval))
    if error:
        return {}, error

    snmp_enabled = bool(data.get("snmp_enabled"))
    mikrotik_enabled = bool(data.get("mikrotik_enabled"))

    snmp_port, error = _parse_port(data.get("snmp_port", 161), "Port SNMP")
    if error:
        return {}, error

    mikrotik_port, error = _parse_port(data.get("mikrotik_port", 8728), "Port MikroTik API")
    if error:
        return {}, error

    snmp_version = str(data.get("snmp_version") or "v2c").strip().lower()
    if snmp_version not in _SNMP_VERSIONS:
        return {}, "Versi SNMP harus v2c atau v3."

    snmp_v3_auth_proto = str(data.get("snmp_v3_auth_proto") or "SHA").upper()
    if snmp_v3_auth_proto not in _SNMP_AUTH_PROTOS:
        return {}, "Protokol auth SNMP v3 tidak didukung."

    snmp_v3_priv_proto = str(data.get("snmp_v3_priv_proto") or "AES").upper()
    if snmp_v3_priv_proto not in _SNMP_PRIV_PROTOS:
        return {}, "Protokol priv SNMP v3 tidak didukung."

    snmp_v3_user = str(data.get("snmp_v3_user") or "").strip()[:120]

    # Konsistensi kredensial SNMP (khusus saat mengaktifkan/membuat baru).
    if snmp_enabled:
        if snmp_version == "v2c":
            community = str(data.get("snmp_community") or "").strip()
            has_existing = existing_row and existing_row.get("snmp_community")
            if not community and not has_existing:
                return {}, "Community SNMP wajib diisi untuk versi v2c."
        else:  # v3
            if not snmp_v3_user:
                return {}, "Username SNMP v3 wajib diisi."

    mikrotik_user = str(data.get("mikrotik_user") or "").strip()[:120]
    if mikrotik_enabled:
        if not mikrotik_user:
            return {}, "User MikroTik API wajib diisi."
        password = str(data.get("mikrotik_password") or "").strip()
        has_existing = existing_row and existing_row.get("mikrotik_password")
        if not password and not has_existing:
            return {}, "Password MikroTik API wajib diisi."

    if db.ip_exists(ip, exclude_id=existing_id):
        return {}, "IP sudah dipakai perangkat lain."
    if db.name_exists(name, exclude_id=existing_id):
        return {}, "Nama perangkat sudah ada."

    clean = {
        "name": name,
        "ip": ip,
        "type": device_type,
        "location": location,
        "group_id": group_id,
        "parent_id": parent_id,
        "maintenance": bool(data.get("maintenance")),
        "poll_interval": poll_interval,
        "snmp_enabled": snmp_enabled,
        "snmp_version": snmp_version,
        "snmp_port": snmp_port,
        "snmp_community": data.get("snmp_community"),
        "snmp_v3_user": snmp_v3_user or None,
        "snmp_v3_auth_proto": snmp_v3_auth_proto,
        "snmp_v3_auth_password": data.get("snmp_v3_auth_password"),
        "snmp_v3_priv_proto": snmp_v3_priv_proto,
        "snmp_v3_priv_password": data.get("snmp_v3_priv_password"),
        "mikrotik_enabled": mikrotik_enabled,
        "mikrotik_port": mikrotik_port,
        "mikrotik_user": mikrotik_user or None,
        "mikrotik_password": data.get("mikrotik_password"),
        "mikrotik_ssl": bool(data.get("mikrotik_ssl")),
    }
    return clean, None


# ---------------------------------------------------------
# Endpoint devices
# ---------------------------------------------------------

@bp.get("/api/devices")
def api_list_devices():
    search = request.args.get("search", "").strip()
    status = request.args.get("status", "").strip().lower()
    device_type = request.args.get("type", "").strip().lower()
    group = request.args.get("group")
    page = request.args.get("page", 1)
    per_page = request.args.get("per_page", 20)
    try:
        page = max(1, int(page))
        per_page = min(100, max(1, int(per_page)))
    except (TypeError, ValueError):
        page, per_page = 1, 20
    if status and status not in ("online", "degraded", "offline", "unknown", "maintenance"):
        return jsonify({"error": "Status tidak valid."}), 400
    return jsonify(
        db.query_devices(
            search=search, status=status, device_type=device_type,
            group_id=group, page=page, per_page=per_page,
            routeros_version=request.args.get("routeros_version", "").strip(),
            mikrotik_model=request.args.get("mikrotik_model", "").strip(),
        )
    )


@bp.post("/api/devices")
@roles("admin", "operator")
def api_create_device():
    clean, error = _validate_device(request.get_json(silent=True) or {})
    if error:
        return jsonify({"error": error}), 400
    device_id = db.create_device(clean)
    poller.schedule_device({"id": device_id, **clean})
    poller.schedule_immediate_check(device_id)
    audit("device.create", f"device:{device_id}", f"Menambah perangkat {clean['name']} ({clean['ip']})")
    return jsonify({"device": db.get_device(device_id)}), 201


@bp.get("/api/devices/<int:device_id>")
def api_get_device(device_id):
    device = db.get_device(device_id)
    if device is None:
        return jsonify({"error": "Perangkat tidak ditemukan."}), 404
    return jsonify({"device": device})


@bp.put("/api/devices/<int:device_id>")
@roles("admin", "operator")
def api_update_device(device_id):
    existing = db.get_device(device_id)
    if existing is None:
        return jsonify({"error": "Perangkat tidak ditemukan."}), 404
    clean, error = _validate_device(request.get_json(silent=True) or {}, existing_id=device_id)
    if error:
        return jsonify({"error": error}), 400
    db.update_device(device_id, clean)
    poller.schedule_device({"id": device_id, **clean})
    poller.schedule_immediate_check(device_id)
    audit("device.update", f"device:{device_id}", f"Mengubah perangkat {clean['name']} ({clean['ip']})")
    return jsonify({"device": db.get_device(device_id)})


@bp.delete("/api/devices/<int:device_id>")
@roles("admin", "operator")
def api_delete_device(device_id):
    existing = db.get_device(device_id)
    if existing is None:
        return jsonify({"error": "Perangkat tidak ditemukan."}), 404
    db.delete_device(device_id)
    poller.unschedule_device(device_id)
    audit("device.delete", f"device:{device_id}", f"Menghapus perangkat {existing['name']} ({existing['ip']})")
    return jsonify({"ok": True})

@bp.post("/api/devices/test")
@roles("admin", "operator")
def api_test_device():
    if _rate_limited():
        return jsonify({"error": "Terlalu banyak uji koneksi, coba lagi sebentar lagi."}), 429

    data = request.get_json(silent=True) or {}

    existing_id = data.get("id")
    existing = None
    if existing_id not in (None, ""):
        try:
            existing_id = int(existing_id)
        except (TypeError, ValueError):
            return jsonify({"error": "ID perangkat tidak valid."}), 400
        existing = db.get_device(existing_id, include_secrets=True)
        if existing is None:
            return jsonify({"error": "Perangkat tidak ditemukan."}), 404

    raw_ip = data.get("ip")
    addr = _parse_ip(raw_ip) if raw_ip not in (None, "") else None
    if existing is not None:
        # Kredensial tersimpan hanya boleh dipakai untuk perangkat tersimpan.
        # Tujuan uji tidak boleh ditentukan bebas oleh klien.
        stored_addr = _parse_ip(existing.get("ip"))
        if addr is None and raw_ip not in (None, ""):
            return jsonify({"error": "Alamat IP tidak valid."}), 400
        if stored_addr is None:
            return jsonify({"error": "IP perangkat tersimpan tidak valid."}), 500
        if addr is not None and addr != stored_addr:
            return jsonify(
                {"error": "IP uji harus sama dengan IP perangkat tersimpan. Simpan perubahan IP terlebih dahulu."}
            ), 400
        addr = stored_addr
    elif addr is None:
        return jsonify({"error": "Alamat IP tidak valid."}), 400
    if not _ip_allowed(addr):
        return jsonify({"error": "IP berada di luar ALLOWED_NETWORKS yang diizinkan."}), 400
    ip = str(addr)

    snmp_enabled = bool(data.get("snmp_enabled"))
    mikrotik_enabled = bool(data.get("mikrotik_enabled"))

    if existing is not None:
        snmp_port = int(existing.get("snmp_port") or 161)
        mikrotik_port = int(existing.get("mikrotik_port") or 8728)
        if snmp_enabled and data.get("snmp_port") not in (None, ""):
            requested_port, error = _parse_port(data.get("snmp_port"), "Port SNMP")
            if error:
                return jsonify({"error": error}), 400
            if requested_port != snmp_port:
                return jsonify(
                    {"error": "Port SNMP uji harus sama dengan port perangkat tersimpan."}
                ), 400
        if mikrotik_enabled and data.get("mikrotik_port") not in (None, ""):
            requested_port, error = _parse_port(data.get("mikrotik_port"), "Port MikroTik API")
            if error:
                return jsonify({"error": error}), 400
            if requested_port != mikrotik_port:
                return jsonify(
                    {"error": "Port MikroTik API uji harus sama dengan port perangkat tersimpan."}
                ), 400
    else:
        snmp_port, mikrotik_port = 161, 8728
        if snmp_enabled:
            snmp_port, error = _parse_port(data.get("snmp_port", 161), "Port SNMP")
            if error:
                return jsonify({"error": error}), 400
        if mikrotik_enabled:
            mikrotik_port, error = _parse_port(data.get("mikrotik_port", 8728), "Port MikroTik API")
            if error:
                return jsonify({"error": error}), 400

    result = {"ping": None, "snmp": None, "mikrotik": None}

    result["ping"] = ping_collector.check_ping(ip)

    if snmp_enabled:
        community = data.get("snmp_community")
        if not community and existing:
            community = db.decrypt_secret(existing.get("snmp_community"))
        snmp_config = {
            "ip": ip,
            "version": str(data.get("snmp_version") or "v2c").lower(),
            "port": snmp_port,
            "community": community,
            "v3_user": data.get("snmp_v3_user"),
            "v3_auth_proto": str(data.get("snmp_v3_auth_proto") or "SHA").upper(),
            "v3_auth_password": data.get("snmp_v3_auth_password")
            or (db.decrypt_secret(existing.get("snmp_v3_auth_password")) if existing else None),
            "v3_priv_proto": str(data.get("snmp_v3_priv_proto") or "AES").upper(),
            "v3_priv_password": data.get("snmp_v3_priv_password")
            or (db.decrypt_secret(existing.get("snmp_v3_priv_password")) if existing else None),
        }
        result["snmp"] = snmp_collector.check_snmp(snmp_config)

    if mikrotik_enabled:
        password = data.get("mikrotik_password")
        if not password and existing:
            password = db.decrypt_secret(existing.get("mikrotik_password"))
        mikrotik_config = {
            "ip": ip,
            "port": mikrotik_port,
            "user": data.get("mikrotik_user"),
            "password": password,
            "ssl": bool(data.get("mikrotik_ssl")),
        }
        result["mikrotik"] = mikrotik_collector.check_mikrotik(mikrotik_config)

    audit("device.test", f"device:{existing_id or ip}", f"Uji koneksi ke {ip}")
    return jsonify(result)


@bp.get("/api/devices/<int:device_id>/checks")
def api_device_checks(device_id):
    device = db.get_device(device_id)
    if device is None:
        return jsonify({"error": "Perangkat tidak ditemukan."}), 404
    try:
        limit = min(500, max(1, int(request.args.get("limit", "100"))))
    except ValueError:
        limit = 100
    return jsonify({"device": device, "checks": db.recent_checks(device_id, limit)})


@bp.get("/api/devices/<int:device_id>/availability")
def api_device_availability(device_id):
    device = db.get_device(device_id)
    if device is None:
        return jsonify({"error": "Perangkat tidak ditemukan."}), 404
    return jsonify(
        {
            "device_id": device_id,
            "h24": db.availability(device_id, 1),
            "h7d": db.availability(device_id, 7),
            "h30d": db.availability(device_id, 30),
        }
    )

# ---------------------------------------------------------
# Metrik MikroTik (Fase 3)
# ---------------------------------------------------------



@bp.get("/api/devices/<int:device_id>/metrics")
def api_device_metrics_series(device_id):
    device = db.get_device(device_id)
    if device is None:
        return jsonify({"error": "Perangkat tidak ditemukan."}), 404

    range_name = (request.args.get("range") or "1h").lower()
    since = _range_since(range_name)
    if since is None:
        return jsonify({"error": "Rentang tidak valid. Gunakan 1h, 24h, atau 7d."}), 400

    points = _downsample(db.metric_points(device_id, since))
    interfaces = [
        {"if_name": series["if_name"], "points": _downsample(series["points"])}
        for series in db.interface_points(device_id, since)
    ]
    return jsonify(
        {
            "device": {"id": device["id"], "name": device["name"], "status": device["status"]},
            "range": range_name,
            "points": points,
            "interfaces": interfaces,
        }
    )


@bp.get("/api/devices/<int:device_id>/snapshot")
def api_device_snapshot(device_id):
    public = db.get_device(device_id)
    if public is None:
        return jsonify({"error": "Perangkat tidak ditemukan."}), 404

    metric = db.latest_metric(device_id)

    metrics = None
    if metric:
        mem_pct = None
        if metric.get("mem_total") and metric.get("mem_used") is not None and metric["mem_total"] > 0:
            mem_pct = round(100.0 * metric["mem_used"] / metric["mem_total"], 1)
        disk_pct = None
        if metric.get("disk_total") and metric.get("disk_used") is not None and metric["disk_total"] > 0:
            disk_pct = round(100.0 * metric["disk_used"] / metric["disk_total"], 1)
        metrics = {
            "ts": metric["ts"],
            "cpu_pct": metric["cpu_pct"],
            "mem_pct": mem_pct,
            "mem_used": metric["mem_used"],
            "mem_total": metric["mem_total"],
            "storage_pct": disk_pct,
            "disk_used": metric["disk_used"],
            "disk_total": metric["disk_total"],
            "temp_c": metric["temp_c"],
            "cpu_temp_c": metric["cpu_temp_c"],
            "voltage": metric["voltage"],
            "uptime_s": metric["uptime_s"],
            "ping_ms": metric["ping_ms"],
        }

    public["availability"] = {
        "h24": db.availability(device_id, 1),
        "h7d": db.availability(device_id, 7),
        "h30d": db.availability(device_id, 30),
    }
    public["thresholds"] = {
        "cpu_warn": public["threshold_cpu_warn"],
        "cpu_crit": public["threshold_cpu_crit"],
        "mem_warn": public["threshold_mem_warn"],
        "mem_crit": public["threshold_mem_crit"],
        "storage_warn": public["threshold_storage_warn"],
        "storage_crit": public["threshold_storage_crit"],
        "temp_warn": public["threshold_temp_warn"],
        "temp_crit": public["threshold_temp_crit"],
    }
    return jsonify({"device": public, "metrics": metrics})


@bp.get("/api/devices/<int:device_id>/interfaces")
def api_device_interfaces(device_id):
    device = db.get_device(device_id)
    if device is None:
        return jsonify({"error": "Perangkat tidak ditemukan."}), 404
    rows = db.latest_interfaces(device_id)
    interfaces = [
        {
            "if_name": row["if_name"],
            "running": bool(row["running"]),
            "disabled": bool(row["disabled"]),
            "rx_bytes": row["rx_bytes"],
            "tx_bytes": row["tx_bytes"],
            "rx_bps": row["rx_bps"],
            "tx_bps": row["tx_bps"],
            "ts": row["ts"],
        }
        for row in rows
    ]
    return jsonify({"interfaces": interfaces})


_poll_requests = {}
POLL_COOLDOWN_SECONDS = 5


@bp.post("/api/devices/<int:device_id>/poll")
@roles("admin", "operator")
def api_device_poll(device_id):
    device = db.get_device(device_id)
    if device is None:
        return jsonify({"error": "Perangkat tidak ditemukan."}), 404
    now = time.monotonic()
    last = _poll_requests.get(device_id, 0.0)
    if now - last < POLL_COOLDOWN_SECONDS:
        remaining = int(POLL_COOLDOWN_SECONDS - (now - last)) + 1
        return jsonify({"error": f"Tunggu {remaining} detik sebelum memicu poll lagi."}), 429
    _poll_requests[device_id] = now
    poller.run_check(device_id)  # sinkron: satu perangkat, tidak menahan yang lain
    audit("device.poll", f"device:{device_id}", f"Poll manual {device['name']}")
    return jsonify({"ok": True})


_THRESHOLD_KEYS = {
    "cpu_warn": "threshold_cpu_warn",
    "cpu_crit": "threshold_cpu_crit",
    "mem_warn": "threshold_mem_warn",
    "mem_crit": "threshold_mem_crit",
    "storage_warn": "threshold_storage_warn",
    "storage_crit": "threshold_storage_crit",
    "temp_warn": "threshold_temp_warn",
    "temp_crit": "threshold_temp_crit",
}


@bp.put("/api/devices/<int:device_id>/thresholds")
@roles("admin", "operator")
def api_device_thresholds(device_id):
    device = db.get_device(device_id)
    if device is None:
        return jsonify({"error": "Perangkat tidak ditemukan."}), 404

    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({"error": "Payload tidak valid."}), 400

    clean = {}
    for short, column in _THRESHOLD_KEYS.items():
        value = data.get(short, data.get(column))
        if value is None:
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            return jsonify({"error": f"Ambang {short} harus berupa angka."}), 400
        if short.startswith("temp"):
            if number < 1 or number > 150:
                return jsonify({"error": f"Ambang {short} harus antara 1 dan 150 C."}), 400
        elif number < 1 or number > 100:
            return jsonify({"error": f"Ambang {short} harus antara 1 dan 100 %."}), 400
        clean[column] = number

    for prefix in ("cpu", "mem", "storage", "temp"):
        warn = clean.get(f"threshold_{prefix}_warn", (device or {}).get(f"threshold_{prefix}_warn", 70))
        crit = clean.get(f"threshold_{prefix}_crit", (device or {}).get(f"threshold_{prefix}_crit", 90))
        if warn > crit:
            return jsonify({"error": f"Ambang peringatan {prefix} tidak boleh melebihi ambang kritis."}), 400

    if not clean:
        return jsonify({"error": "Tidak ada ambang yang dikirim."}), 400

    db.update_device_thresholds(device_id, clean)
    updated = db.get_device(device_id)
    audit("device.thresholds", f"device:{device_id}", f"Mengubah ambang perangkat {updated['name']}")
    return jsonify(
        {
            "ok": True,
            "thresholds": {
                "cpu_warn": updated["threshold_cpu_warn"],
                "cpu_crit": updated["threshold_cpu_crit"],
                "mem_warn": updated["threshold_mem_warn"],
                "mem_crit": updated["threshold_mem_crit"],
                "storage_warn": updated["threshold_storage_warn"],
                "storage_crit": updated["threshold_storage_crit"],
                "temp_warn": updated["threshold_temp_warn"],
                "temp_crit": updated["threshold_temp_crit"],
            },
        }
    )


@bp.get("/api/summary")
def api_summary():
    summary = db.summary()
    summary["org_name"] = db.get_setting("org_name", "NMS Kopegtel")
    return jsonify(summary)

# ---------------------------------------------------------
# Problems & aktivitas
# ---------------------------------------------------------

# ---------------------------------------------------------
# Inventory ringkasan (devices domain — pindah ke blueprint devices)
# ---------------------------------------------------------

@bp.get("/api/inventory/summary")
def api_inventory_summary():
    return jsonify(db.inventory_summary())


# ---------------------------------------------------------
# Topologi
# ---------------------------------------------------------

@bp.get("/api/topology")
def api_topology():
    rows = db.topology_rows()
    group_param = request.args.get("group")
    selected_group = None
    if group_param not in (None, "", "all"):
        try:
            selected_group = int(group_param)
        except ValueError:
            return jsonify({"error": "Grup tidak valid."}), 400
        rows = [r for r in rows if r["group_id"] == selected_group]

    total = len(rows)
    truncated = total > 60
    visible = rows[:60]
    visible_ids = {r["id"] for r in visible}

    nodes = [
        {
            "id": r["id"],
            "name": r["name"],
            "ip": r["ip"],
            "type": r["type"],
            "status": r["status"],
            "location": r["location"],
            "latency_ms": r["latency_ms"],
            "parent_id": r["parent_id"],
            "group_id": r["group_id"],
            "group_name": r["group_name"],
        }
        for r in visible
    ]
    edges = [
        {"from": r["parent_id"], "to": r["id"]}
        for r in visible
        if r["parent_id"] in visible_ids
    ]

    # Link fiber: untuk menggambar (kedua ujung terpantau & tampil) DAN untuk panel detail
    # (semua link yang melibatkan perangkat pada topologi, termasuk ujung B non-perangkat).
    fiber_links = []
    fiber_links_all = []
    visible_set = set(visible_ids)
    for link in db.list_fiber_links():
        other = link.get("device_b_id")
        status, metrics = fiber_calc.cached_or_live_metrics(link)
        brief = {
            "id": link["id"],
            "name": link["name"],
            "device_a_id": link["device_a_id"],
            "device_b_id": other,
            "device_a_name": link["device_a_name"],
            "device_b_name": link.get("device_b_name"),
            "end_b_label": link.get("end_b_label"),
            "port_a": link["port_a"],
            "port_b": link["port_b"],
            "length_km": link["length_km"],
            "status": status,
            "attenuation_ab_db": metrics.get("attenuation_ab_db"),
            "attenuation_ba_db": metrics.get("attenuation_ba_db"),
            "margin_b_db": metrics.get("margin_b_db"),
        }
        if (
            (link["device_a_id"] in visible_set or other in visible_set)
            or other is None  # ujung B label: tetap tampil di detail perangkat ujung A
        ):
            fiber_links_all.append(brief)
        if other and link["device_a_id"] in visible_set and other in visible_set:
            fiber_links.append(brief)

    return jsonify(
        {
            "nodes": nodes,
            "edges": edges,
            "fiber_links": fiber_links,
            "fiber_links_all": fiber_links_all,
            "total": total,
            "truncated": truncated,
            "group": selected_group,
        }
    )


# ---------------------------------------------------------
# Grup host & profil pemantauan per tipe
# ---------------------------------------------------------

@bp.get("/api/groups")
def api_list_groups():
    return jsonify({"groups": db.list_groups()})


@bp.post("/api/groups")
@roles("admin", "operator")
def api_create_group():
    data = request.get_json(silent=True) or {}
    name = str(data.get("name") or "").strip()
    description = str(data.get("description") or "").strip()
    if not name or len(name) > 80:
        return jsonify({"error": "Nama grup wajib diisi (maks 80 karakter)."}), 400
    if db.group_name_exists(name):
        return jsonify({"error": "Nama grup sudah ada."}), 409
    group_id = db.create_group(name, description)
    audit("group.create", f"group:{group_id}", f"Menambah grup {name}")
    return jsonify({"group": db.get_group(group_id)}), 201


@bp.put("/api/groups/<int:group_id>")
@roles("admin", "operator")
def api_update_group(group_id):
    if db.get_group(group_id) is None:
        return jsonify({"error": "Grup tidak ditemukan."}), 404
    data = request.get_json(silent=True) or {}
    name = str(data.get("name") or "").strip()
    description = str(data.get("description") or "").strip()
    if not name or len(name) > 80:
        return jsonify({"error": "Nama grup wajib diisi (maks 80 karakter)."}), 400
    if db.group_name_exists(name, exclude_id=group_id):
        return jsonify({"error": "Nama grup sudah ada."}), 409
    db.update_group(group_id, name, description)
    audit("group.update", f"group:{group_id}", f"Mengubah grup {name}")
    return jsonify({"group": db.get_group(group_id)})


@bp.delete("/api/groups/<int:group_id>")
@roles("admin", "operator")
def api_delete_group(group_id):
    if db.get_group(group_id) is None:
        return jsonify({"error": "Grup tidak ditemukan."}), 404
    deleted, used = db.delete_group(group_id)
    if not deleted:
        return jsonify({"error": f"Grup masih dipakai {used} perangkat. Pindahkan dulu perangkatnya.", "code": "group_in_use"}), 409
    audit("group.delete", f"group:{group_id}", "Menghapus grup")
    return jsonify({"ok": True})


@bp.get("/api/profiles")
def api_list_profiles():
    return jsonify({"profiles": db.list_profiles(), "types": sorted(DEVICE_TYPES)})


@bp.put("/api/profiles/<string:device_type>")
@roles("admin", "operator")
def api_upsert_profile(device_type):
    device_type = device_type.strip().lower()
    if device_type not in DEVICE_TYPES:
        return jsonify({"error": "Tipe perangkat tidak valid."}), 400
    data = request.get_json(silent=True) or {}
    interval, error = _parse_interval(data.get("interval_seconds", 60))
    if error:
        return jsonify({"error": error}), 400
    thresholds = data.get("thresholds") or {}
    clean = {}
    for key in ("cpu_warn", "cpu_crit", "mem_warn", "mem_crit", "storage_warn", "storage_crit", "temp_warn", "temp_crit"):
        if key in thresholds and thresholds[key] is not None:
            try:
                value = float(thresholds[key])
            except (TypeError, ValueError):
                return jsonify({"error": f"Ambang {key} harus angka."}), 400
            if key.startswith("temp"):
                if value < 1 or value > 150:
                    return jsonify({"error": f"Ambang {key} harus antara 1-150."}), 400
            elif value < 1 or value > 100:
                return jsonify({"error": f"Ambang {key} harus antara 1-100."}), 400
            clean[key] = value
    for prefix in ("cpu", "mem", "storage", "temp"):
        warn = clean.get(f"{prefix}_warn", db.PROFILES_DEFAULT_THRESHOLDS.get(f"{prefix}_warn", 70))
        crit = clean.get(f"{prefix}_crit", db.PROFILES_DEFAULT_THRESHOLDS.get(f"{prefix}_crit", 90))
        if warn > crit:
            return jsonify({"error": f"Ambang peringatan {prefix} tidak boleh melebihi ambang kritis."}), 400
    db.upsert_profile(device_type, interval, clean)
    # Terapkan ke perangkat tipe ini (dokumentasikan di UI).
    db.apply_profile_to_type(device_type, interval, clean)
    audit("profile.update", f"profile:{device_type}", f"Mengubah profil pemantauan {device_type}")
    return jsonify({"ok": True})


# ---------------------------------------------------------
# Pemeliharaan
# ---------------------------------------------------------

def _parse_utc_ts(value):
    parsed = db.parse_ts(value)
    return parsed.strftime("%Y-%m-%dT%H:%M:%SZ") if parsed else None


@bp.get("/api/maintenance")
def api_list_maintenance():
    return jsonify({"windows": db.list_maintenance()})


@bp.post("/api/maintenance")
@roles("admin", "operator")
def api_create_maintenance():
    data = request.get_json(silent=True) or {}
    name = str(data.get("name") or "").strip()
    if not name or len(name) > 120:
        return jsonify({"error": "Nama pemeliharaan wajib diisi (maks 120 karakter)."}), 400
    starts_at = _parse_utc_ts(data.get("starts_at"))
    ends_at = _parse_utc_ts(data.get("ends_at"))
    if not starts_at or not ends_at:
        return jsonify({"error": "Waktu mulai dan selesai wajib diisi."}), 400
    if starts_at >= ends_at:
        return jsonify({"error": "Waktu mulai harus sebelum waktu selesai."}), 400
    device_id, error = _parse_optional_id(data.get("device_id"), "Perangkat")
    if error:
        return jsonify({"error": error}), 400
    if device_id and db.get_device(device_id) is None:
        return jsonify({"error": "Perangkat tidak ditemukan."}), 404
    group_id, error = _parse_optional_id(data.get("group_id"), "Grup")
    if error:
        return jsonify({"error": error}), 400
    if group_id and db.get_group(group_id) is None:
        return jsonify({"error": "Grup tidak ditemukan."}), 404
    note = str(data.get("note") or "").strip()
    window_id = db.create_maintenance(name, device_id, group_id, starts_at, ends_at, note, g.user["username"])
    audit("maintenance.create", f"maintenance:{window_id}", f"Menambah jendela pemeliharaan {name}")
    return jsonify({"window": {"id": window_id, "name": name, "starts_at": starts_at, "ends_at": ends_at}}), 201


@bp.delete("/api/maintenance/<int:window_id>")
@roles("admin", "operator")
def api_delete_maintenance(window_id):
    if not db.delete_maintenance(window_id):
        return jsonify({"error": "Jendela pemeliharaan tidak ditemukan."}), 404
    audit("maintenance.delete", f"maintenance:{window_id}", "Menghapus jendela pemeliharaan")
    return jsonify({"ok": True})

