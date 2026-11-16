"""Poller APScheduler: periksa perangkat per interval, tanpa memblokir request.

Status:
- offline       → ping gagal
- degraded      → latency tinggi atau CPU > 90%
- maintenance   → perangkat/jendela pemeliharaan aktif (problem & notifikasi ditekan)
- online        → selain itu

Metrik MikroTik (RouterOS API, fallback SNMP) disimpan per poll:
- device_metrics  (CPU, memori, storage, suhu, voltase, uptime)
- interface_stats (counter + bps hasil hitung selisih antar poll)

Masalah (tabel ``problems``) dibuka saat status memburuk / metrik melewati ambang
kritis-peringatan (hanya saat level berubah) dan ditutup otomatis saat pulih.
Notifikasi dikirim lewat ``alerts.dispatch_problem`` (aturan aksi → kanal).
Event ringkas dicatat ke tabel ``events`` (umpan "Aktivitas terbaru").
"""

import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

import db
import alerts
import fiber as fiber_calc
import backups
import dbbackup
from db.connection import _lock
from collectors import mikrotik as mikrotik_collector
from collectors import mikrotik_modules
from collectors import optical as optical_collector
from collectors import ping as ping_collector
from collectors import snmp as snmp_collector
from ingest import rules as ingest_rules

log = logging.getLogger("nms.poller")

MAX_WORKERS = int(os.environ.get("POLL_MAX_WORKERS", "8"))
MIN_INTERVAL_SECONDS = 10
DEFAULT_INTERVAL_SECONDS = 60
DEGRADED_LATENCY_MS = float(os.environ.get("DEGRADED_LATENCY_MS", "250"))
DEGRADED_CPU_PERCENT = 90

_scheduler = None
LAST_HEARTBEAT = None


def _beat():
    global LAST_HEARTBEAT
    LAST_HEARTBEAT = time.time()


def _decrypted_snmp_config(device):
    config = {
        "ip": device["ip"],
        "version": device["snmp_version"],
        "port": device["snmp_port"],
        "community": db.decrypt_secret(device["snmp_community"]),
        "v3_user": device["snmp_v3_user"],
        "v3_auth_proto": device["snmp_v3_auth_proto"],
        "v3_auth_password": db.decrypt_secret(device["snmp_v3_auth_password"]),
        "v3_priv_proto": device["snmp_v3_priv_proto"],
        "v3_priv_password": db.decrypt_secret(device["snmp_v3_priv_password"]),
    }
    return config


def _decrypted_mikrotik_config(device):
    return {
        "ip": device["ip"],
        "port": device["mikrotik_port"],
        "user": device["mikrotik_user"],
        "password": db.decrypt_secret(device["mikrotik_password"]),
        "ssl": bool(device["mikrotik_ssl"]),
        "ssl_fingerprint": device.get("mikrotik_ssl_fingerprint"),
    }


def _compute_status(ping_result, mikrotik_result):
    if not ping_result.get("ok"):
        return "offline"

    latency = ping_result.get("latency_ms")
    if latency is not None and latency > DEGRADED_LATENCY_MS:
        return "degraded"

    cpu_load = (mikrotik_result or {}).get("cpu_load")
    if cpu_load is not None and cpu_load > DEGRADED_CPU_PERCENT:
        return "degraded"

    return "online"


def _threshold_level(value, warn, crit):
    if value is None:
        return None
    if crit is not None and value >= crit:
        return "kritis"
    if warn is not None and value >= warn:
        return "peringatan"
    return None


def _thresholds(device):
    def pick(key, default):
        value = device.get(key)
        return default if value is None else value

    return {
        "cpu": (pick("threshold_cpu_warn", 70), pick("threshold_cpu_crit", 90)),
        "memori": (pick("threshold_mem_warn", 70), pick("threshold_mem_crit", 90)),
        "storage": (pick("threshold_storage_warn", 70), pick("threshold_storage_crit", 90)),
        "suhu": (pick("threshold_temp_warn", 60), pick("threshold_temp_crit", 75)),
    }


def _problem_this(device, kind, severity, message):
    """Buka/perbarui masalah; bila BARU terbuka, picu dispatch notifikasi."""
    problem_id, is_new = db.open_problem(device["id"], kind, severity, message)
    if is_new:
        try:
            alerts.dispatch_problem(
                {
                    "id": problem_id,
                    "device_id": device["id"],
                    "kind": kind,
                    "severity": severity,
                    "message": message,
                    "started_at": db._now(),
                }
            )
        except Exception:
            log.exception("Dispatch notifikasi untuk masalah %s gagal (tidak menghentikan poller).", problem_id)
    return problem_id


def _check_thresholds(device, previous, current):
    """Bandingkan level ambang metrik; kembalikan daftar aksi saat BERUBAH.

    ``previous``/``current``: dict berisi cpu_pct, mem_pct, disk_pct, temp_c.
    Hasil: list of dict(message, kind, severity|None, resolved: bool).
    """
    actions = []
    labels = {
        "cpu_pct": ("CPU", "cpu_high", "%"),
        "mem_pct": ("Memori", "mem_high", "%"),
        "disk_pct": ("Storage", "disk_high", "%"),
        "temp_c": ("Suhu", "temp_high", "°C"),
    }
    thresholds = _thresholds(device)
    metric_keys = {
        "cpu_pct": thresholds["cpu"],
        "mem_pct": thresholds["memori"],
        "disk_pct": thresholds["storage"],
        "temp_c": thresholds["suhu"],
    }
    for key, (warn, crit) in metric_keys.items():
        label, kind, unit = labels[key]
        level = _threshold_level(current.get(key), warn, crit)
        prev_level = _threshold_level((previous or {}).get(key), warn, crit)
        if level == prev_level:
            continue
        if level is None:
            actions.append(
                {
                    "message": f"Ambang: {label} kembali normal",
                    "kind": kind,
                    "severity": None,
                    "resolved": True,
                }
            )
            continue
        severity = "high" if level == "kritis" else "warning"
        value = current[key]
        actions.append(
            {
                "message": f"{device['name']}: {label} {value:.1f}{unit} melewati ambang {level} ({warn}/{crit}{unit})",
                "kind": kind,
                "severity": severity,
                "resolved": False,
            }
        )
    return actions


def _extract_metrics(result):
    """Normalisasi hasil collector (MikroTik API / fallback SNMP) → dict metrik."""
    if not result or not result.get("ok"):
        return None

    total_mem = result.get("total_memory")
    free_mem = result.get("free_memory")
    mem_used = (total_mem - free_mem) if (total_mem is not None and free_mem is not None) else None

    total_disk = result.get("total_hdd_space")
    free_disk = result.get("free_hdd_space")
    disk_used = (total_disk - free_disk) if (total_disk is not None and free_disk is not None) else None

    return {
        "cpu_pct": result.get("cpu_load"),
        "mem_used": mem_used,
        "mem_total": total_mem,
        "disk_used": disk_used,
        "disk_total": total_disk,
        "temp_c": result.get("temperature"),
        "cpu_temp_c": result.get("cpu_temperature"),
        "voltage": result.get("voltage"),
        "uptime_s": result.get("uptime_s"),
    }


def _record_metrics(device, metrics_source, mikrotik_result, ping_result, in_maintenance=False):
    """Simpan device_metrics + interface_stats dari satu siklus poll."""
    device_id = device["id"]
    now_ts = db._now()

    metrics = _extract_metrics(metrics_source)
    if metrics is None:
        if device["mikrotik_enabled"] or device["snmp_enabled"]:
            reason = (mikrotik_result or {}).get("reason") or "error"
            db.set_device_metrics_error(device_id, reason)
        return

    # Persen untuk ambang peringatan.
    mem_pct = (
        round(100.0 * metrics["mem_used"] / metrics["mem_total"], 1)
        if metrics["mem_used"] is not None and metrics["mem_total"]
        else None
    )
    disk_pct = (
        round(100.0 * metrics["disk_used"] / metrics["disk_total"], 1)
        if metrics["disk_used"] is not None and metrics["disk_total"]
        else None
    )

    previous_metric = db.latest_metric(device_id)
    previous_pct = None
    if previous_metric:
        prev_mem = None
        if previous_metric.get("mem_total") and previous_metric.get("mem_used") is not None:
            prev_mem = round(100.0 * previous_metric["mem_used"] / previous_metric["mem_total"], 1)
        prev_disk = None
        if previous_metric.get("disk_total") and previous_metric.get("disk_used") is not None:
            prev_disk = round(100.0 * previous_metric["disk_used"] / previous_metric["disk_total"], 1)
        previous_pct = {
            "cpu_pct": previous_metric.get("cpu_pct"),
            "mem_pct": prev_mem,
            "disk_pct": prev_disk,
            "temp_c": previous_metric.get("temp_c"),
        }

    db.record_metric(
        device_id,
        now_ts,
        cpu_pct=metrics["cpu_pct"],
        mem_used=metrics["mem_used"],
        mem_total=metrics["mem_total"],
        disk_used=metrics["disk_used"],
        disk_total=metrics["disk_total"],
        temp_c=metrics["temp_c"],
        cpu_temp_c=metrics["cpu_temp_c"],
        voltage=metrics["voltage"],
        uptime_s=metrics["uptime_s"],
        ping_ms=ping_result.get("latency_ms"),
    )

    current_pct = {
        "cpu_pct": metrics["cpu_pct"],
        "mem_pct": mem_pct,
        "disk_pct": disk_pct,
        "temp_c": metrics["temp_c"],
    }
    threshold_actions = _check_thresholds(device, previous_pct, current_pct)

    # Perbarui kolom snapshot di devices (hanya dari jalur RouterOS API / fallback lengkap).
    db.update_device_snapshot(
        device_id,
        version=metrics_source.get("version"),
        model=metrics_source.get("model"),
        board=metrics_source.get("board_name"),
        architecture=metrics_source.get("architecture_name"),
        serial=metrics_source.get("serial_number"),
        firmware=metrics_source.get("current_firmware"),
        cpu_count=metrics_source.get("cpu_count"),
        cpu_frequency_mhz=metrics_source.get("cpu_frequency"),
    )

    for action in threshold_actions:
        try:
            db.add_event(device_id, device["status"], device["status"], action["message"])
        except sqlite3.IntegrityError:
            unschedule_device(device_id)
            return
        if in_maintenance:
            continue  # masalah & notifikasi ditekan selama pemeliharaan
        if action["resolved"]:
            db.resolve_problem(device_id, action["kind"])
        elif action["severity"]:
            _problem_this(device, action["kind"], action["severity"], action["message"])

    # Statistik interface: hitung bps dari selisih counter antar poll.
    interfaces = metrics_source.get("interfaces") or []
    entries = []
    for iface in interfaces:
        name = iface.get("name")
        if not name:
            continue
        rx_bytes, tx_bytes = iface.get("rx_byte"), iface.get("tx_byte")
        rx_bps = tx_bps = 0.0
        previous_iface = db.interface_previous(device_id, name)
        if previous_iface and rx_bytes is not None and tx_bytes is not None:
            try:
                prev_ts = db.parse_ts(previous_iface["ts"])
                curr_ts = db.parse_ts(now_ts)
                elapsed = (curr_ts - prev_ts).total_seconds() if (curr_ts and prev_ts) else 0
            except (TypeError, ValueError):
                elapsed = 0
            if elapsed > 0:
                delta_rx = rx_bytes - (previous_iface["rx_bytes"] or 0)
                delta_tx = tx_bytes - (previous_iface["tx_bytes"] or 0)
                # Counter reset/wrap (delta negatif) → anggap 0 untuk siklus ini.
                rx_bps = (delta_rx * 8 / elapsed) if delta_rx >= 0 else 0.0
                tx_bps = (delta_tx * 8 / elapsed) if delta_tx >= 0 else 0.0
        entries.append(
            {
                "if_name": name,
                "running": bool(iface.get("running")),
                "disabled": bool(iface.get("disabled")),
                "rx_bytes": rx_bytes,
                "tx_bytes": tx_bytes,
                "rx_bps": rx_bps,
                "tx_bps": tx_bps,
            }
        )
    if entries:
        db.record_interface_stats(device_id, now_ts, entries)


# ---------------------------------------------------------
# Koleksi optik + evaluasi link fiber
# ---------------------------------------------------------

_FIBER_EVAL_KINDS = (
    "fiber_los", "fiber_rx_low", "fiber_rx_high", "fiber_margin_low",
    "fiber_asymmetry", "fiber_drop", "fiber_trend",
)


def _optical_skip_ports(device_id):
    """Port yang terakhir dicek < 1 jam lalu dan BUKAN optik → lewati monitor (deteksi ulang tiap 1 jam)."""
    fresh = set()
    now = datetime.now(timezone.utc)
    for state in db.list_optical_port_states():
        if state["device_id"] != device_id or state["is_optical"]:
            continue  # port optik selalu dimonitor; yang lain dievaluasi tiap 1 jam
        last = db.parse_ts(state.get("last_check"))
        if last and (now - last).total_seconds() < 3600:
            fresh.add(state["if_name"])
    return fresh


def _dispatch_fiber_problem(device, kind, severity, message, problem_id, *, link=None,
                            port=None, rx=None, redaman=None):
    problem = {
        "id": problem_id,
        "device_id": device["id"],
        "kind": kind,
        "severity": severity,
        "message": message,
        "started_at": db._now(),
        "link_name": (link or {}).get("name"),
        "port": port,
        "rx": rx,
        "redaman": redaman,
    }
    try:
        alerts.dispatch_problem(problem)
    except Exception:
        log.exception("Dispatch notifikasi fiber untuk masalah %s gagal (tidak menghentikan poller).", problem_id)


def _apply_port_problems(device, port, ts, in_maintenance):
    """Deteksi per-port: tx_fault, sfp_removed, sfp_changed + upsert modul & state port."""
    settings = db.get_fiber_settings()
    required = int(settings["fiber_consec_required"])
    state = db.get_optical_port_state(device["id"], port["if_name"]) or {}
    try:
        consec = json.loads(state.get("consec") or "{}")
    except json.JSONDecodeError:
        consec = {}

    module = db.upsert_optical_module(
        device["id"], port["if_name"],
        vendor=port.get("vendor"), part_no=port.get("part_no"), serial=port.get("serial"),
        type_=port.get("type"), wavelength_nm=port.get("wavelength_nm"),
        link_length_m=port.get("link_length_m"), ts=ts,
    )
    if module["serial_changed"] and not in_maintenance:
        message = (
            f"{device['name']}: SFP pada {port['if_name']} berganti — "
            f"serial {module['serial_before']} → {module['serial_after']}"
        )
        db.add_event(device["id"], "sfp_changed", "sfp_changed", message)
        problem_id, is_new = db.open_problem(device["id"], "sfp_changed", "information", message)
        if is_new:
            _dispatch_fiber_problem(device, "sfp_changed", "information", message, problem_id, port=port["if_name"])

    if not in_maintenance:
        port_name = port["if_name"]
        for kind, bad, severity, text in (
            ("sfp_tx_fault", bool(port.get("tx_fault")), "high",
             f"{device['name']}: Tx fault pada {port_name} (modul SFP melaporkan kegagalan pemancar)"),
            ("sfp_removed", not bool(port.get("module_present", True)), "high",
             f"{device['name']}: modul SFP pada {port_name} terlepas/tidak terpasang"),
        ):
            count = int(consec.get(kind) or 0)
            count = count + 1 if bad else 0
            consec[kind] = count
            if bad and count >= required:
                problem_id, is_new = db.open_problem(device["id"], kind, severity, text)
                if is_new:
                    _dispatch_fiber_problem(device, kind, severity, text, problem_id, port=port_name)
            elif not bad:
                db.resolve_problem(device["id"], kind)

    db.upsert_optical_port_state(
        device["id"], port["if_name"],
        is_optical=True, ddm_supported=bool(port.get("ddm_supported")),
        ts=ts, consec=consec,
        last_serial=port.get("serial"),
    )


def _collect_optical(device, in_maintenance):
    """Satu siklus pengumpulan optik: baca DDM semua port optik aktif & simpan."""
    device_id = device["id"]
    skip = _optical_skip_ports(device_id)
    result = None
    if device["mikrotik_enabled"]:
        try:
            result = optical_collector.check_optical_mikrotik(
                _decrypted_mikrotik_config(device), skip_ports=skip
            )
        except Exception as exc:
            result = {"method": "mikrotik-optical", "ok": False, "reason": "error",
                      "detail": str(exc), "ports": [], "scanned": []}
    if (result is None or not result.get("ok")) and device["snmp_enabled"]:
        try:
            result = optical_collector.check_optical_snmp(_decrypted_snmp_config(device))
        except Exception as exc:
            result = {"method": "snmp-optical", "ok": False, "reason": "error",
                      "detail": str(exc), "ports": [], "scanned": []}
    if not result:
        return

    ts = db._now()
    ports = result.get("ports") or []
    scanned = set(result.get("scanned") or [p["if_name"] for p in ports])
    optical_names = {p["if_name"] for p in ports}

    if ports:
        db.record_optical_readings(device_id, ts, ports)
    # Tandai state: port optik → True; port yang dicek tapi bukan optik → False
    # (port yang di-skip tidak disentuh; deteksi ulang berjalan tiap 1 jam).
    for name in scanned - optical_names:
        state = db.get_optical_port_state(device_id, name)
        keep_optical = bool(state and state.get("is_optical"))  # sekali optik, tetap dimonitor
        db.upsert_optical_port_state(device_id, name, is_optical=keep_optical,
                                     ddm_supported=False, ts=ts)
    for port in ports:
        _apply_port_problems(device, port, ts, in_maintenance)


def _issue_device_id(link, issue):
    if issue.get("device_side") == "b" and link.get("device_b_id"):
        return link["device_b_id"]
    return link["device_a_id"]


def _evaluate_one_link(link, settings):
    state = db.get_link_state(link["id"]) or {}
    try:
        consec = json.loads(state.get("consec") or "{}")
    except json.JSONDecodeError:
        consec = {}
    required = int(settings["fiber_consec_required"])
    was_bad = fiber_calc.open_link_kinds(link["id"])
    result = fiber_calc.evaluate_link(link, was_bad=was_bad, settings=settings)

    issues_by_kind = {}
    for issue in result["issues"]:
        issues_by_kind.setdefault(issue["kind"], []).append(issue)

    for kind in _FIBER_EVAL_KINDS:
        bad_issues = issues_by_kind.get(kind) or []
        bad = bool(bad_issues)
        count = (int(consec.get(kind) or 0) + 1) if bad else 0
        consec[kind] = count
        if bad and count >= required:
            for issue in bad_issues:
                device_id = _issue_device_id(link, issue)
                device = db.get_device(device_id)
                if device is None or db.device_in_maintenance(device):
                    continue  # ditekan selama pemeliharaan
                pid, is_new = db.open_problem(
                    device_id, kind, issue["severity"], issue["message"], link_id=link["id"]
                )
                if is_new:
                    _dispatch_fiber_problem(
                        device, kind, issue["severity"], issue["message"], pid,
                        link=link, port=issue.get("port"),
                        rx=issue.get("rx"), redaman=issue.get("redaman"),
                    )
        elif not bad:
            fiber_calc.resolve_link_problems(link["id"], kind)

    metrics = {key: result.get(key) for key in fiber_calc.LINK_STATE_METRICS_KEYS}
    metrics["los_a"] = result.get("los_a")
    metrics["los_b"] = result.get("los_b")
    db.save_link_state(
        link["id"], consec=consec, status=result["status"],
        metrics=metrics, ts=db._now(),
    )


def _evaluate_fiber_links():
    """Satu evaluasi semua link fiber (dipanggil scheduler tiap 60 dtk)."""
    settings = db.get_fiber_settings()
    links = db.list_fiber_links()
    for link in links:
        try:
            _evaluate_one_link(link, settings)
        except Exception:
            log.exception("Evaluasi fiber link %r gagal (dilewati).", link.get("id"))


# ---------------------------------------------------------
# Modul MikroTik: layer, snapshot, dan problem
# ---------------------------------------------------------

_MT_TABLE_MODULES = {
    "interfaces": ("mt_interfaces", None),
    "addresses": ("mt_ip_addresses", None),
    "ppp": ("mt_ppp_sessions", None),
    "hotspot": ("mt_hotspot_sessions", None),
    "wireless": ("mt_wireless_clients", None),
    "dhcp": ("mt_dhcp_leases", None),
    "dhcp_pools": ("mt_dhcp_pools", None),
    "arp": ("mt_arp", None),
    "neighbors": ("mt_neighbors", None),
    "routes": ("mt_routes", "route-ipv4:"),
    "routes6": ("mt_routes", "route-ipv6:"),
    "bgp": ("mt_routing_neighbors", "bgp:"),
    "ospf": ("mt_routing_neighbors", "ospf:"),
    "firewall_filter": ("mt_firewall_rules", "rule-filter:"),
    "firewall_nat": ("mt_firewall_rules", "rule-nat:"),
    "firewall_mangle": ("mt_firewall_rules", "rule-mangle:"),
    "address_lists": ("mt_address_lists", None),
    "queue_simple": ("mt_queues", "queue-simple:"),
    "queue_tree": ("mt_queues", "queue-tree:"),
    "services": ("mt_services", None),
    "users": ("mt_users", None),
}


def _mt_int_setting(key, default):
    try:
        return int(db.get_setting(key, default))
    except (TypeError, ValueError):
        return default


def _mt_due_layers(device, force=False):
    settings = db.get_mt_device_settings(device["id"])
    now = datetime.now(timezone.utc)
    layers = []
    for layer in ("fast", "medium", "slow"):
        state = db.get_mt_poll_state(device["id"], layer) or {}
        last = db.parse_ts(state.get("last_run"))
        interval = int(settings["intervals"].get(layer) or 60)
        if force or last is None or (now - last).total_seconds() >= interval:
            layers.append(layer)
    fast_state = db.get_mt_poll_state(device["id"], "fast") or {}
    return {
        "layers": layers,
        "modules": settings["modules"],
        "intervals": settings["intervals"],
        "important_interfaces": settings["important_interfaces"],
        "capabilities": db.get_mt_capabilities(device["id"]),
        "log_cursor": fast_state.get("cursor") or {},
        "max_rows": 5000,
        "stale_user_days": _mt_int_setting("mt_stale_user_days", 90),
    }


def _mt_persist_payload(device_id, key, payload, ts):
    table_info = _MT_TABLE_MODULES.get(key)
    if not table_info:
        return
    table, scope = table_info
    if not payload or payload.get("disabled"):
        db.mark_mt_table_stale(table, device_id, scope_prefix=scope)
        return
    if not payload.get("ok"):
        db.mark_mt_table_stale(table, device_id, scope_prefix=scope)
        return
    rows = payload.get("rows") or []
    if key in ("interfaces", "addresses"):
        enriched = []
        for row in rows:
            data = dict(row)
            if key == "interfaces":
                data["row_key"] = f"if:{data.get('name') or ''}"
            else:
                data["row_key"] = f"addr:{data.get('address') or ''}|{data.get('interface') or ''}"
            enriched.append(data)
        rows = enriched
    db.replace_mt_snapshot(
        table, device_id, rows, ts,
        truncated=bool(payload.get("truncated")),
        scope_prefix=scope,
    )


def _mt_apply_result(device, result, plan, in_maintenance):
    """Simpan semua modul yang terkumpul dan jalankan deteksi problem."""
    device_id = device["id"]
    ts = db._now()
    layers = set(plan.get("layers") or [])
    modules = result.get("modules") or {}

    capabilities = result.get("capabilities") or {}
    if capabilities:
        db.save_mt_capabilities(device_id, capabilities)

    system = result.get("system") or {}
    if system and ("fast" in layers or "slow" in layers):
        db.save_mt_system(device_id, system)

    for key in _MT_TABLE_MODULES:
        if key in modules:
            _mt_persist_payload(device_id, key, modules[key], ts)

    logs_payload = modules.get("logs") or {}
    if logs_payload.get("ok") and not logs_payload.get("disabled"):
        log_rows = logs_payload.get("rows") or []
        try:
            log_rows = db.filter_syslog_duplicates(device_id, log_rows, polled_at=ts)
        except Exception:
            log.debug("Dedup log MikroTik vs syslog gagal; log tetap disimpan.", exc_info=True)
        db.record_mt_logs(device_id, log_rows, polled_at=ts)

    security_payload = modules.get("security") or {}
    if security_payload.get("ok") and not security_payload.get("disabled") and "slow" in layers:
        db.replace_mt_findings(device_id, security_payload.get("rows") or [], detected_at=ts)

    counts = result.get("module_counts") or {}
    db.record_mt_metrics(device_id, ts, counts)

    layer_status = result.get("layer_status") or {}
    truncated = result.get("truncated") or {}
    for layer in ("fast", "medium", "slow"):
        if layer not in layers:
            continue
        status = layer_status.get(layer) or {"ok": False, "errors": [("layer", "error")]}
        errors = status.get("errors") or []
        error_text = "; ".join(f"{key}: {reason}" for key, reason in errors)[:500] or None
        db.set_mt_poll_state(
            device_id, layer,
            last_run=ts,
            ok=bool(status.get("ok")),
            error=error_text,
            cursor=result.get("log_cursor") if layer == "fast" else None,
            truncated=truncated,
        )

    _mt_run_problems(device, result, plan, in_maintenance)


def _mt_apply_failure(device, plan, reason):
    device_id = device["id"]
    for table in db.MT_SNAPSHOT_TABLES:
        db.mark_mt_table_stale(table, device_id)
    now_ts = db._now()
    for layer in plan.get("layers") or []:
        db.set_mt_poll_state(device_id, layer, last_run=now_ts, ok=False, error=reason)
    log.debug("Modul MikroTik %s tidak diperbarui: %s", device["name"], reason)


def _mt_run_problems(device, result, plan, in_maintenance):
    if in_maintenance:
        return
    device_id = device["id"]
    modules = result.get("modules") or {}
    counts = result.get("module_counts") or {}
    system = result.get("system") or {}
    modules_settings = plan.get("modules") or {}

    def enabled(key):
        return bool(modules_settings.get(key, True))

    def ok(key):
        payload = modules.get(key) or {}
        return bool(payload.get("ok")) and not payload.get("disabled")

    # 1. Percobaan login berulang (log).
    logs_payload = modules.get("logs") or {}
    if enabled("log") and logs_payload.get("ok") and not logs_payload.get("disabled"):
        window = _mt_int_setting("mt_bruteforce_window_minutes", 10)
        threshold = _mt_int_setting("mt_bruteforce_threshold", 10)
        since = (datetime.now(timezone.utc) - timedelta(minutes=max(1, window))).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows = db.mt_login_failure_counts(device_id, since)
        hit = [row for row in rows if (row.get("failures") or 0) >= threshold]
        if hit:
            top = ", ".join(
                f"{row['source_ip']} ({row['failures']}x)" for row in hit[:3]
            )
            _problem_this(
                device, "mt_login_bruteforce", "high",
                f"{device['name']}: {len(hit)} IP sumber login gagal berulang — {top}",
            )
        else:
            db.resolve_problem(device_id, "mt_login_bruteforce")
    elif enabled("log") and logs_payload.get("disabled"):
        db.resolve_problem(device_id, "mt_login_bruteforce")

    # 2. Layanan tidak aman / tanpa pembatasan alamat (audit keamanan).
    security_payload = modules.get("security") or {}
    if enabled("security") and security_payload.get("ok") and not security_payload.get("disabled"):
        findings = db.list_mt_findings(device_id, include_ignored=False)
        insecure = [
            finding for finding in findings
            if finding.get("category") in ("insecure_service", "service_open")
        ]
        if insecure:
            severity = "high" if any(f.get("severity") == "high" for f in insecure) else "average"
            titles = ", ".join(f.get("title") or "" for f in insecure[:3])
            _problem_this(
                device, "mt_service_insecure", severity,
                f"{device['name']}: {len(insecure)} layanan berisiko — {titles}",
            )
        else:
            db.resolve_problem(device_id, "mt_service_insecure")
    elif enabled("security") and security_payload.get("disabled"):
        db.resolve_problem(device_id, "mt_service_insecure")

    # 3. Firmware tertunda.
    if system.get("firmware_status") is not None:
        if system.get("firmware_status") == "pending":
            current = system.get("current_firmware") or "—"
            _problem_this(
                device, "mt_firmware_pending", "warning",
                f"{device['name']}: firmware {current} berbeda dari paket {system.get('version') or '—'}",
            )
        else:
            db.resolve_problem(device_id, "mt_firmware_pending")

    # 4. Selisih jam router.
    drift = system.get("drift_seconds")
    if drift is not None:
        threshold = _mt_int_setting("mt_time_drift_seconds", 120)
        if abs(int(drift)) > threshold:
            _problem_this(
                device, "mt_time_drift", "warning",
                f"{device['name']}: jam router bergeser {drift} detik dari server "
                f"(ambang {threshold} detik)",
            )
        else:
            db.resolve_problem(device_id, "mt_time_drift")

    # 5. Interface penting down.
    interfaces_payload = modules.get("interfaces") or {}
    important = plan.get("important_interfaces") or []
    if important and interfaces_payload.get("ok"):
        rows = {row.get("name"): row for row in interfaces_payload.get("rows") or []}
        down = [
            name for name in important
            if name not in rows or not rows[name].get("running") or rows[name].get("disabled")
        ]
        if down:
            _problem_this(
                device, "mt_interface_down", "average",
                f"{device['name']}: interface penting tidak aktif — {', '.join(down[:5])}",
            )
        else:
            db.resolve_problem(device_id, "mt_interface_down")

    # 6. Neighbor routing down.
    routing_rows = []
    if enabled("routing") and (ok("bgp") or ok("ospf")):
        routing_rows = (modules.get("bgp") or {}).get("rows", []) + (modules.get("ospf") or {}).get("rows", [])
        down = mikrotik_modules.neighbor_down(routing_rows)
        if down:
            labels = ", ".join(
                f"{row.get('protocol', '').upper()} {row.get('name') or row.get('peer_id') or '—'}"
                for row in down[:4]
            )
            _problem_this(
                device, "mt_routing_neighbor_down", "average",
                f"{device['name']}: {len(down)} neighbor routing tidak Established — {labels}",
            )
        else:
            db.resolve_problem(device_id, "mt_routing_neighbor_down")

    # 7. Pemakaian pool DHCP tinggi.
    pools_payload = modules.get("dhcp_pools") or {}
    if enabled("dhcp") and pools_payload.get("ok") and not pools_payload.get("disabled"):
        threshold = _mt_int_setting("mt_pool_high_pct", 90)
        high = mikrotik_modules.pool_high(pools_payload.get("rows") or [], threshold)
        if high:
            labels = ", ".join(
                f"{row.get('name')} {row.get('usage_pct')}%" for row in high[:4]
            )
            _problem_this(
                device, "mt_dhcp_pool_high", "average",
                f"{device['name']}: pool DHCP > {threshold}% — {labels}",
            )
        else:
            db.resolve_problem(device_id, "mt_dhcp_pool_high")

    # 8. Sesi PPPoE turun drastis.
    if enabled("ppp") and "ppp_active" in counts:
        window = _mt_int_setting("mt_sessions_drop_window_minutes", 5)
        threshold = _mt_int_setting("mt_sessions_drop_pct", 30)
        before_ts = (datetime.now(timezone.utc) - timedelta(minutes=max(1, window))).strftime("%Y-%m-%dT%H:%M:%SZ")
        previous = db.mt_metric_before(device_id, "ppp_active", before_ts)
        drop = mikrotik_modules.sessions_drop(
            (previous or {}).get("value"), counts.get("ppp_active"), threshold
        )
        if drop is not None:
            _problem_this(
                device, "mt_sessions_drop", "high",
                f"{device['name']}: sesi PPPoE turun {drop}% dalam {window} menit "
                f"({(previous or {}).get('value')} → {counts.get('ppp_active')})",
            )
        elif previous is not None:
            db.resolve_problem(device_id, "mt_sessions_drop")


METRICS = {
    "poll_success_total": 0,
    "poll_error_total": 0,
    "poll_last_duration_seconds": None,
    "poll_last_success_ts": None,
    "poll_last_error_ts": None,
    "poll_started_at": time.time(),
}


def poller_stats():
    with _lock:
        return dict(METRICS)


def _note_poll_metrics(duration_seconds, error):
    with _lock:
        METRICS["poll_last_duration_seconds"] = round(duration_seconds, 3)
        if error:
            METRICS["poll_error_total"] += 1
            METRICS["poll_last_error_ts"] = time.time()
        else:
            METRICS["poll_success_total"] += 1
            METRICS["poll_last_success_ts"] = time.time()


def run_check(device_id, force_mikrotik=False):
    _started = time.monotonic()
    try:
        device = db.get_device(device_id, include_secrets=True)
        if device is None:
            log.warning("Device %s tidak ditemukan, job dilewati.", device_id)
            return
        _run_check_body(device, force_mikrotik)
        _note_poll_metrics(time.monotonic() - _started, error=False)
    except Exception:
        _note_poll_metrics(time.monotonic() - _started, error=True)
        log.exception("run_check untuk perangkat %s gagal.", device_id)
        raise


def _run_check_body(device, force_mikrotik):
    device_id = device["id"]
    ping_result = ping_collector.check_ping(device["ip"])

    snmp_result = None
    if device["snmp_enabled"]:
        try:
            snmp_result = snmp_collector.check_snmp(_decrypted_snmp_config(device))
        except Exception as exc:  # hasil terstruktur, jangan jatuhkan job
            snmp_result = {"method": "snmp", "ok": False, "reason": "error", "detail": str(exc)}

    mikrotik_result = None
    mikrotik_plan = None
    if device["mikrotik_enabled"]:
        try:
            mikrotik_plan = _mt_due_layers(device, force=force_mikrotik)
            mikrotik_result = mikrotik_collector.check_mikrotik(
                _decrypted_mikrotik_config(device), plan=mikrotik_plan
            )
        except Exception as exc:
            mikrotik_result = {"method": "mikrotik", "ok": False, "reason": "error", "detail": str(exc)}

    # Fallback SNMP untuk metrik (hanya bila API MikroTik tidak aktif / gagal).
    metrics_source = mikrotik_result if (mikrotik_result and mikrotik_result.get("ok")) else None
    if metrics_source is None and device["snmp_enabled"]:
        try:
            fallback = snmp_collector.collect_snmp_metrics(_decrypted_snmp_config(device))
        except Exception as exc:
            fallback = {"method": "snmp", "ok": False, "reason": "error", "detail": str(exc)}
        if mikrotik_result is None:
            mikrotik_result = fallback  # simpan alasan gagal untuk diagnostik
        if fallback.get("ok"):
            metrics_source = fallback

    in_maintenance = db.device_in_maintenance(device)
    if in_maintenance:
        status = "maintenance"
    else:
        status = _compute_status(ping_result, mikrotik_result)
    previous = device["status"]
    latency = ping_result.get("latency_ms")

    try:
        db.record_check(
            device_id,
            status,
            latency,
            {"ping": ping_result, "snmp": snmp_result, "mikrotik": mikrotik_result},
            maintenance=in_maintenance,
        )
    except sqlite3.IntegrityError:
        # Perangkat dihapus saat pemeriksaan berlangsung - bersihkan jobnya.
        unschedule_device(device_id)
        return

    _record_metrics(device, metrics_source, mikrotik_result, ping_result, in_maintenance=in_maintenance)

    if device["mikrotik_enabled"] and mikrotik_plan is not None:
        if mikrotik_result and mikrotik_result.get("ok") and mikrotik_result.get("module_ok"):
            try:
                _mt_apply_result(device, mikrotik_result, mikrotik_plan, in_maintenance)
            except Exception:
                log.exception("Penyimpanan modul MikroTik %s gagal (tidak menghentikan poller).", device_id)
        elif mikrotik_result and not mikrotik_result.get("ok"):
            try:
                _mt_apply_failure(device, mikrotik_plan, mikrotik_result.get("reason") or "error")
            except Exception:
                log.exception("Penandaan data MikroTik basi untuk %s gagal.", device_id)

    # Sinkronisasi masalah berdasarkan status (ditekan selama pemeliharaan).
    if not in_maintenance:
        if status == "offline":
            _problem_this(device, "device_down", "high", f"{device['name']}: tidak merespons ping")
            db.resolve_problem(device_id, "high_latency")
        elif status == "degraded":
            detail = f"latensi {latency:.0f} ms" if latency is not None else "CPU tinggi"
            _problem_this(device, "high_latency", "average", f"{device['name']}: {detail}")
            db.resolve_problem(device_id, "device_down")
        else:  # online / unknown
            db.resolve_problem(device_id, "device_down")
            db.resolve_problem(device_id, "high_latency")

    if previous != status:
        try:
            message = f"{device['name']}: status berubah {previous} → {status}"
            db.add_event(device_id, previous, status, message)
        except sqlite3.IntegrityError:
            unschedule_device(device_id)

    # Koleksi optik (DDM) — tidak mengubah status perangkat, hanya membaca & merekam.
    try:
        _collect_optical(device, in_maintenance)
    except Exception:
        log.exception("Pengumpulan optik untuk perangkat %s gagal (tidak menghentikan poller).", device_id)


def _job_id(device_id):
    return f"device-{device_id}"


def schedule_device(device):
    if _scheduler is None:
        return
    interval = max(MIN_INTERVAL_SECONDS, int(device.get("poll_interval") or DEFAULT_INTERVAL_SECONDS))
    _scheduler.add_job(
        run_check,
        trigger=IntervalTrigger(seconds=interval),
        args=[device["id"]],
        id=_job_id(device["id"]),
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )


def unschedule_device(device_id):
    if _scheduler is None:
        return
    try:
        _scheduler.remove_job(_job_id(device_id))
    except Exception:
        pass


def schedule_immediate_check(device_id, delay_seconds=2):
    """Satu pemeriksaan sesaat setelah perangkat dibuat/diubah."""
    if _scheduler is None:
        return
    _scheduler.add_job(
        run_check,
        trigger="date",
        run_date=datetime.now(timezone.utc) + timedelta(seconds=delay_seconds),
        args=[device_id],
        id=f"device-check-now-{device_id}-{datetime.now().timestamp()}",
        misfire_grace_time=30,
    )


def start_poller():
    global _scheduler
    if _scheduler is not None:
        return _scheduler

    executors = {"default": ThreadPoolExecutor(MAX_WORKERS)}
    _scheduler = BackgroundScheduler(
        executors=executors,
        job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 30},
    )

    # Bersihkan data lama tiap hari 02:30/02:35. Nilai retensi dibaca dari
    # pengaturan setiap kali job berjalan, jadi perubahan UI langsung berlaku
    # pada pembersihan berikutnya tanpa restart.
    _scheduler.add_job(
        db.purge_old_checks,
        trigger=CronTrigger(hour=2, minute=30),
        id="purge-old-checks",
        replace_existing=True,
    )
    _scheduler.add_job(
        db.purge_old_data,
        trigger=CronTrigger(hour=2, minute=35),
        id="purge-old-data",
        replace_existing=True,
    )

    # Backup online basis data harian (T3): 02:45, folder & jumlah salinan
    # diatur via pengaturan (db_backup_dir/db_backup_keep) atau env
    # (DB_BACKUP_ENABLED/DB_BACKUP_DIR/DB_BACKUP_KEEP).
    _scheduler.add_job(
        dbbackup.run_due_backup,
        trigger=CronTrigger(hour=2, minute=45),
        id="db-backup-daily",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # Evaluasi semua link fiber tiap menit (konsisten untuk semua link;
    # 2 pembacaan berturut-turut + histeresis terjadi di sini).
    _scheduler.add_job(
        _evaluate_fiber_links,
        trigger=IntervalTrigger(seconds=60),
        id="fiber-links-eval",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # E1: auto-resolve aturan kejadian + lepas penekanan parent tiap menit.
    _scheduler.add_job(
        ingest_rules.run_housekeeping,
        trigger=IntervalTrigger(seconds=60),
        id="ingest-housekeeping",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # E3: rollup flow 5 menit -> per jam (idempoten; hanya jam lengkap).
    from flow import FLOW as _FLOW

    _scheduler.add_job(
        _FLOW.hourly_rollup,
        trigger=IntervalTrigger(minutes=30),
        id="flow-hourly-rollup",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # E2: mesin eskalasi tiap 30 detik (idempoten, tahan restart).
    import escalation as escalation_engine

    _scheduler.add_job(
        escalation_engine.job,
        trigger=IntervalTrigger(seconds=30),
        id="escalation-engine",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # Backup konfigurasi MikroTik terjadwal (M6): cek tiap 10 menit,
    # hanya jalan di jam yang diatur per perangkat, sekali per hari.
    _scheduler.add_job(
        backups.run_due_backups,
        trigger=IntervalTrigger(minutes=10),
        id="mt-backups-due",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # Heartbeat poller untuk /healthz: perbarui setiap 15 detik.
    _scheduler.add_job(
        _beat,
        trigger=IntervalTrigger(seconds=15),
        id="poller-heartbeat",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    _beat()

    for device in db.list_devices():
        schedule_device(device)

    _scheduler.start()
    log.info("Poller dimulai dengan %d worker.", MAX_WORKERS)
    return _scheduler


def shutdown_poller():
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
