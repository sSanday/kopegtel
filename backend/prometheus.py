"""E4 — /metrics (format teks Prometheus) dari snapshot database.

- Tidak pernah query ke perangkat saat scrape; semua dari DB/memori.
- Hasil di-cache 10 detik (proses ini).
- Nilai perangkat yang basi (> 3 interval tanpa poll) TIDAK dikeluarkan.
- Batas kardinalitas `metrics_series_limit` (default 20.000); bila terpotong,
  keluarkan `nms_metrics_series_truncated 1` dan berhenti menambah seri.
- Label tidak berisi secret maupun IP klien.
"""

import json
import logging
import sys
import threading
import time
from datetime import datetime, timezone

import db
import poller
from core.env import APP_VERSION

log = logging.getLogger("nms.prometheus")

try:
    from prometheus_client import CollectorRegistry, generate_latest
    from prometheus_client.core import GaugeMetricFamily, InfoMetricFamily
    HAVE_PROMETHEUS = True
except Exception:  # pragma: no cover - dependensi wajib di requirements.lock
    CollectorRegistry = None
    generate_latest = None
    GaugeMetricFamily = InfoMetricFamily = None
    HAVE_PROMETHEUS = False

_CACHE_LOCK = threading.Lock()
_CACHE = {"ts": 0.0, "payload": b"", "truncated": False}
CACHE_SECONDS = 10

_DEFAULT_LIMIT = 20000


def _now_ts():
    return datetime.now(timezone.utc).timestamp()


def _iso(ts):
    try:
        return datetime.strptime(str(ts), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        ).timestamp()
    except (TypeError, ValueError):
        return None


def _clean_label(value, max_len=120):
    text = str(value or "")
    text = "".join(ch for ch in text if ch.isprintable())
    return text[:max_len]


def series_limit():
    try:
        return max(10, min(100000, int(db.get_setting("metrics_series_limit", str(_DEFAULT_LIMIT)))))
    except (TypeError, ValueError):
        return _DEFAULT_LIMIT


def interfaces_mode():
    mode = str(db.get_setting("metrics_interfaces", "important") or "important").lower()
    return mode if mode in ("important", "all", "none") else "important"


# -------------------------------------------------------
# Pengumpul data (satu per scrape)
# -------------------------------------------------------

class _Collector:
    def __init__(self):
        self.series = 0
        self.limit = series_limit()
        self.truncated = False

    def add(self, n=1):
        self.series += n
        return self.series <= self.limit

    def gauge(self, name, documentation, labels=(), labelvalues=(), value=None):
        if not self.add(len(labelvalues) if isinstance(labelvalues, (list, tuple)) else 1):
            self.truncated = True
            return None
        family = GaugeMetricFamily(name, documentation, labels=list(labels))
        family.add_metric(list(labelvalues), float(value))
        return family

    def gauge_multi(self, name, documentation, labels, rows):
        """rows: list of (labelvalues, value). Satu family."""
        if not rows:
            return None
        accepted = []
        for labelvalues, value in rows:
            if not self.add(1):
                self.truncated = True
                break
            accepted.append((labelvalues, value))
        if not accepted:
            return None
        family = GaugeMetricFamily(name, documentation, labels=list(labels))
        for labelvalues, value in accepted:
            family.add_metric(list(labelvalues), float(value))
        return family

    def info(self, name, documentation, labels, labelvalues):
        if not self.add(1):
            self.truncated = True
            return None
        family = InfoMetricFamily(name, documentation, labels=list(labels))
        family.add_metric([], dict(zip(labels, labelvalues)))
        return family


def _get_important_interfaces():
    with db._lock:
        rows = db.get_conn().execute(
            "SELECT important_interfaces FROM mt_device_settings"
        ).fetchall()
    names = set()
    for row in rows:
        try:
            items = json.loads(row["important_interfaces"] or "[]")
        except (TypeError, ValueError):
            items = []
        for item in items:
            text = str(item).strip()
            if text:
                names.add(text)
    return names


def _device_rows():
    with db._lock:
        rows = db.get_conn().execute(
            """
            SELECT d.id, d.name, d.type, d.status, d.latency_ms, d.last_seen,
                   d.poll_interval, COALESCE(g.name, 'none') AS group_name,
                   m.cpu_pct, m.mem_used, m.mem_total, m.disk_used, m.disk_total,
                   m.temp_c, m.uptime_s, m.ts AS metrics_ts
            FROM devices d
            LEFT JOIN host_groups g ON g.id = d.group_id
            LEFT JOIN (
                SELECT device_id, MAX(id) AS last_id FROM device_metrics GROUP BY device_id
            ) latest ON latest.device_id = d.id
            LEFT JOIN device_metrics m ON m.id = latest.last_id
            """
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["stale_cutoff"] = 3 * (int(item.get("poll_interval") or 60) or 60)
        newest = None
        for key in ("metrics_ts", "last_seen"):
            ts = _iso(item.get(key))
            if ts and (newest is None or ts > newest):
                newest = ts
        item["newest_ts"] = newest
        result.append(item)
    return result


def _is_fresh(item):
    if item["newest_ts"] is None:
        return False
    return (_now_ts() - item["newest_ts"]) <= item["stale_cutoff"]


def _latest_interface_rows():
    with db._lock:
        rows = db.get_conn().execute(
            """
            SELECT s.device_id, s.if_name, s.running, s.disabled, s.rx_bps, s.tx_bps
            FROM interface_stats s
            JOIN (
                SELECT device_id, if_name, MAX(id) AS last_id
                FROM interface_stats GROUP BY device_id, if_name
            ) latest ON latest.last_id = s.id
            """
        ).fetchall()
    return [dict(row) for row in rows]


def _latest_optical_rows():
    with db._lock:
        rows = db.get_conn().execute(
            """
            SELECT o.device_id, o.if_name, o.rx_dbm, o.tx_dbm, o.temp_c
            FROM optical_readings o
            JOIN (
                SELECT device_id, if_name, MAX(id) AS last_id
                FROM optical_readings GROUP BY device_id, if_name
            ) latest ON latest.last_id = o.id
            """
        ).fetchall()
    return [dict(row) for row in rows]


def _latest_link_rows():
    with db._lock:
        rows = db.get_conn().execute(
            """
            SELECT s.link_id, l.name AS link_name, s.status, s.metrics
            FROM optical_link_state s JOIN fiber_links l ON l.id = s.link_id
            """
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        try:
            item["metrics"] = json.loads(item.get("metrics") or "{}")
        except (TypeError, ValueError):
            item["metrics"] = {}
        result.append(item)
    return result


def _problem_counts():
    with db._lock:
        rows = db.get_conn().execute(
            "SELECT severity, COUNT(*) AS n FROM problems WHERE status='open' GROUP BY severity"
        ).fetchall()
    return {str(row["severity"]): int(row["n"]) for row in rows}


def _service_availability():
    with db._lock:
        services = db.get_conn().execute(
            "SELECT id, name FROM services"
        ).fetchall()
    result = []
    for row in services:
        try:
            stats = db.service_availability(row["id"], 1)
        except Exception:
            continue
        pct = (stats or {}).get("availability_pct")
        if pct is None:
            continue
        result.append((row["name"], float(pct) / 100.0))
    return result


def _build_metrics():
    collector = _Collector()
    families = []

    info = collector.info(
        "nms_build", "Build info aplikasi NMS.",
        ("version", "python_version"), (APP_VERSION, sys.version.split()[0]),
    )
    if info:
        families.append(info)

    devices = _device_rows()

    # --- jumlah perangkat per status/tipe
    counts = {}
    for item in devices:
        counts[(item["type"] or "lainnya", item["status"] or "unknown")] = \
            counts.get((item["type"] or "", item["status"] or ""), 0) + 1
    family = collector.gauge_multi(
        "nms_devices_total", "Jumlah perangkat per tipe dan status.",
        ("type", "status"), [([t, s], n) for (t, s), n in sorted(counts.items())],
    )
    if family:
        families.append(family)

    # --- metrik per perangkat (hanya yang segar)
    fresh_devices = [item for item in devices if _is_fresh(item)]
    DEVICE_LABELS = ("device", "group", "type")
    per_device = [
        ("nms_device_up", "Status perangkat (1=up/online, 0=down).", DEVICE_LABELS),
        ("nms_device_latency_ms", "Latensi ping terakhir (ms).", DEVICE_LABELS),
        ("nms_device_cpu_percent", "Utilisasi CPU (%).", DEVICE_LABELS),
        ("nms_device_memory_used_bytes", "Memori terpakai (byte).", DEVICE_LABELS),
        ("nms_device_memory_total_bytes", "Memori total (byte).", DEVICE_LABELS),
        ("nms_device_disk_used_bytes", "Disk terpakai (byte).", DEVICE_LABELS),
        ("nms_device_disk_total_bytes", "Disk total (byte).", DEVICE_LABELS),
        ("nms_device_temperature_celsius", "Suhu (Celsius).", DEVICE_LABELS),
        ("nms_device_uptime_seconds", "Uptime perangkat (detik).", DEVICE_LABELS),
        ("nms_device_last_poll_timestamp_seconds", "Waktu poll terakhir (unix).", DEVICE_LABELS),
    ]
    values = {key: [] for key, _, _ in per_device}
    for item in fresh_devices:
        label = [_clean_label(item["name"]), _clean_label(item.get("group_name") or "none"),
                 _clean_label(item.get("type") or "lainnya")]
        values["nms_device_up"].append((label, 1 if (item.get("status") or "") in ("online", "degraded", "maintenance") else 0))
        if item.get("latency_ms") is not None:
            values["nms_device_latency_ms"].append((label, float(item["latency_ms"])))
        for src, dst in (("cpu_pct", "nms_device_cpu_percent"),
                         ("mem_used", "nms_device_memory_used_bytes"),
                         ("mem_total", "nms_device_memory_total_bytes"),
                         ("disk_used", "nms_device_disk_used_bytes"),
                         ("disk_total", "nms_device_disk_total_bytes"),
                         ("temp_c", "nms_device_temperature_celsius"),
                         ("uptime_s", "nms_device_uptime_seconds")):
            if item.get(src) is not None:
                values[dst].append((label, float(item[src])))
        if item.get("metrics_ts"):
            ts = _iso(item["metrics_ts"])
            if ts:
                values["nms_device_last_poll_timestamp_seconds"].append((label, ts))
    for name, doc, label_name in per_device:
        family = collector.gauge_multi(name, doc, label_name, values[name])
        if family:
            families.append(family)

    # --- interface
    mode = interfaces_mode()
    if mode != "none":
        import_names = _get_important_interfaces() if mode == "important" else None
        by_device = {item["id"]: item for item in devices}
        fresh_ids = {item["id"] for item in fresh_devices}
        rows = _latest_interface_rows()
        rx_rows, tx_rows, up_rows = [], [], []
        for row in rows:
            device = by_device.get(row["device_id"])
            if not device or device["id"] not in fresh_ids:
                continue
            if import_names is not None and row["if_name"] not in import_names:
                continue
            label = [_clean_label(device["name"]), _clean_label(device.get("group_name") or "none"),
                     _clean_label(row["if_name"])]
            if row.get("rx_bps") is not None:
                rx_rows.append((label, float(row["rx_bps"])))
            if row.get("tx_bps") is not None:
                tx_rows.append((label, float(row["tx_bps"])))
            up_rows.append((label, 1 if row.get("running") and not row.get("disabled") else 0))
        for name, doc, rows_data in (
            ("nms_interface_rx_bps", "Throughput rx bps.", rx_rows),
            ("nms_interface_tx_bps", "Throughput tx bps.", tx_rows),
            ("nms_interface_up", "Status interface (1=up).", up_rows),
        ):
            family = collector.gauge_multi(name, doc, ("device", "group", "interface"), rows_data)
            if family:
                families.append(family)

    # --- fiber: optik + link
    optical = _latest_optical_rows()
    rx_rows, tx_rows, temp_rows = [], [], []
    by_device = {item["id"]: item for item in devices}
    fresh_ids = {item["id"] for item in fresh_devices}
    for row in optical:
        device = by_device.get(row["device_id"])
        if not device or device["id"] not in fresh_ids:
            continue
        label = [_clean_label(device["name"]), _clean_label(device.get("group_name") or "none"),
                 _clean_label(row["if_name"])]
        if row.get("rx_dbm") is not None:
            rx_rows.append((label, float(row["rx_dbm"])))
        if row.get("tx_dbm") is not None:
            tx_rows.append((label, float(row["tx_dbm"])))
        if row.get("temp_c") is not None:
            temp_rows.append((label, float(row["temp_c"])))
    for name, doc, rows_data in (
        ("nms_fiber_optical_rx_dbm", "Daya rx optik (dBm).", rx_rows),
        ("nms_fiber_optical_tx_dbm", "Daya tx optik (dBm).", tx_rows),
        ("nms_fiber_optical_temperature_celsius", "Suhu modul optik (Celsius).", temp_rows),
    ):
        family = collector.gauge_multi(name, doc, ("device", "group", "interface"), rows_data)
        if family:
            families.append(family)

    att_rows, margin_rows, status_rows = [], [], []
    for row in _latest_link_rows():
        label = [_clean_label(row["link_name"])]
        metrics = row["metrics"]
        for direction, key in (("a_to_b", "attenuation_ab_db"), ("b_to_a", "attenuation_ba_db")):
            if metrics.get(key) is not None:
                att_rows.append(([*label, direction], float(metrics[key])))
        for direction, key in (("a", "margin_a_db"), ("b", "margin_b_db")):
            if metrics.get(key) is not None:
                margin_rows.append(([*label, direction], float(metrics[key])))
        status_rows.append(([*label, _clean_label(row["status"] or "unknown")], 1))
    for name, doc, rows_data in (
        ("nms_fiber_link_attenuation_db", "Redaman link (dB) per arah.", att_rows),
        ("nms_fiber_link_margin_db", "Margin redaman (dB) per ujung.", margin_rows),
        ("nms_fiber_link_status", "Status link fiber (1=aktif; label status).", status_rows),
    ):
        family = collector.gauge_multi(name, doc, ("link", "direction") if name != "nms_fiber_link_status" else ("link", "status"), rows_data)
        if family:
            families.append(family)

    # --- problem terbuka per severity
    family = collector.gauge_multi(
        "nms_problems_open", "Jumlah problem terbuka per severity.",
        ("severity",), [([s], n) for s, n in sorted(_problem_counts().items())],
    )
    if family:
        families.append(family)

    # --- ketersediaan layanan
    family = collector.gauge_multi(
        "nms_service_availability_ratio", "Ketersediaan layanan (0-1) 24 jam terakhir.",
        ("service",), [([n], v) for n, v in _service_availability()],
    )
    if family:
        families.append(family)

    # --- poller
    stats = poller.poller_stats()
    running = 1 if poller._scheduler is not None else 0
    family = collector.gauge_multi(
        "nms_poller_running", "Poller sedang berjalan (1=ya).", (), [([], running)],
    )
    if family:
        families.append(family)
    family = collector.gauge_multi(
        "nms_poller_poll_duration_seconds", "Durasi poll terakhir (detik).", (),
        [([], stats.get("poll_last_duration_seconds") or 0.0)] if stats.get("poll_last_duration_seconds") is not None else [],
    )
    if family:
        families.append(family)
    family = collector.gauge_multi(
        "nms_poller_poll_success_total", "Total poll berhasil sejak proses berjalan.", (),
        [([], stats.get("poll_success_total", 0))],
    )
    if family:
        families.append(family)
    family = collector.gauge_multi(
        "nms_poller_poll_error_total", "Total poll gagal sejak proses berjalan.", (),
        [([], stats.get("poll_error_total", 0))],
    )
    if family:
        families.append(family)
    if stats.get("poll_last_success_ts"):
        family = collector.gauge_multi(
            "nms_poller_poll_last_success_timestamp_seconds",
            "Waktu poll berhasil terakhir (unix).", (), [([], stats["poll_last_success_ts"])],
        )
        if family:
            families.append(family)

    # --- ingest (syslog, trap, flow)
    try:
        from ingest import INGEST

        ingest_status = INGEST.status()
    except Exception:
        ingest_status = {}
    try:
        from flow import FLOW

        flow_status = FLOW.status()
    except Exception:
        flow_status = {}
    ingest_rows = []
    for kind, payload in ingest_status.items():
        if not isinstance(payload, dict):
            continue
        label_kind = {"syslog_udp": "syslog", "syslog_tcp": "syslog", "snmp_trap": "trap"}.get(kind, kind)
        ingest_rows.append(([label_kind, "accepted"], payload.get("accepted", 0)))
        for drop_key in ("dropped_unknown", "dropped_rate", "dropped_invalid", "dropped_auth", "dropped_queue"):
            ingest_rows.append(([label_kind, drop_key], payload.get(drop_key, 0)))
    for kind, payload in flow_status.items():
        if not isinstance(payload, dict) or kind in ("templates", "bucket"):
            continue
        label_kind = {"netflow_udp": "flow", "ipfix_udp": "flow"}.get(kind, kind)
        ingest_rows.append(([label_kind, "accepted"], payload.get("accepted", 0)))
        ingest_rows.append(([label_kind, "flows"], payload.get("flows", 0)))
        if payload.get("dropped_rate") is not None:
            ingest_rows.append(([label_kind, "dropped_rate"], payload.get("dropped_rate", 0)))
        if payload.get("dropped_invalid") is not None:
            ingest_rows.append(([label_kind, "dropped_invalid"], payload.get("dropped_invalid", 0)))
        if payload.get("dropped_no_template") is not None:
            ingest_rows.append(([label_kind, "dropped_no_template"], payload.get("dropped_no_template", 0)))
        if payload.get("dropped_unknown") is not None:
            ingest_rows.append(([label_kind, "dropped_unknown"], payload.get("dropped_unknown", 0)))
    family = collector.gauge_multi(
        "nms_ingest_messages_total",
        "Jumlah pesan/flow yang diterima atau dibuang (sejak proses berjalan).",
        ("kind", "result"), ingest_rows,
    )
    if family:
        families.append(family)

    # --- ukuran database
    family = collector.gauge_multi(
        "nms_database_size_bytes", "Ukuran berkas database (byte).", (), [([], float(db.db_file_size()))],
    )
    if family:
        families.append(family)

    if collector.truncated:
        # Sentinel tidak ikut batas kardinalitas: harus selalu lolos.
        family = GaugeMetricFamily(
            "nms_metrics_series_truncated",
            "1 bila keluaran metrik terpotong karena metrics_series_limit.",
        )
        family.add_metric([], 1)
        families.append(family)

    return families, bool(collector.truncated)


def render_metrics_text():
    """Return (payload_bytes, truncated). Cache 10 detik."""
    if not HAVE_PROMETHEUS:
        return b"# prometheus_client belum terpasang\n", False
    with _CACHE_LOCK:
        now = time.monotonic()
        if _CACHE["ts"] and (now - _CACHE["ts"]) < CACHE_SECONDS:
            return _CACHE["payload"], _CACHE["truncated"]
        registry = CollectorRegistry()
        families, truncated = _build_metrics()

        class _Once:
            def collect(self_inner):
                return families

        registry.register(_Once())
        payload = generate_latest(registry)
        _CACHE["ts"] = now
        _CACHE["payload"] = payload
        _CACHE["truncated"] = bool(truncated)
        return payload, _CACHE["truncated"]


def cache_status():
    with _CACHE_LOCK:
        return {"ts": _CACHE["ts"], "age_seconds": time.monotonic() - _CACHE["ts"] if _CACHE["ts"] else None}


def reset_cache():
    with _CACHE_LOCK:
        _CACHE["ts"] = 0.0
        _CACHE["payload"] = b""
        _CACHE["truncated"] = False
