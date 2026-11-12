"""Pengaturan aplikasi (tabel settings), info sistem, retensi pesan."""

import os
from datetime import datetime, timedelta, timezone

from .connection import (
    DB_PATH,
    _days_ago,
    _lock,
    _now,
    get_conn,
)
from .fiber import (
    get_fiber_settings,
)

def purge_old_checks(days=30):
    cutoff = _days_ago(days)
    with _lock:
        conn = get_conn()
        cursor = conn.execute("DELETE FROM device_checks WHERE checked_at < ?", (cutoff,))
        conn.commit()
        return cursor.rowcount

def purge_old_data(days=None, hourly_days=None, events_days=30, audit_days=365):
    """Retensi: simpan rata-rata per jam (30 hari), buang data mentah > 7 hari.

    Nilai default diambil dari pengaturan (Administration → retensi data).
    Data optik ikut mekanisme yang sama: mentah > retention_raw_days dihapus
    setelah diagregasi ke optical_hourly (min/avg/max, disimpan 90 hari).
    """
    settings = get_settings({})
    fiber = get_fiber_settings()
    days = int(days if days is not None else settings.get("retention_raw_days", 7))
    hourly_days = int(hourly_days if hourly_days is not None else settings.get("retention_hourly_days", 30))
    events_days = int(events_days or settings.get("retention_events_days", 30))
    audit_days = int(audit_days or settings.get("retention_audit_days", 365))
    optical_hourly_days = int(fiber.get("retention_optical_hourly_days", 90))
    try:
        syslog_days = int(settings.get("retention_syslog_days", 14))
    except (TypeError, ValueError):
        syslog_days = 14
    try:
        trap_days = int(settings.get("retention_trap_days", 30))
    except (TypeError, ValueError):
        trap_days = 30

    raw_cutoff = _days_ago(days)
    hourly_cutoff = (datetime.now(timezone.utc) - timedelta(days=hourly_days)).strftime("%Y-%m-%dT%H:00:00Z")
    optical_hourly_cutoff = (datetime.now(timezone.utc) - timedelta(days=optical_hourly_days)).strftime("%Y-%m-%dT%H:00:00Z")
    events_cutoff = _days_ago(events_days)
    audit_cutoff = _days_ago(audit_days)
    with _lock:
        conn = get_conn()
        conn.execute(
            """
            INSERT INTO device_metrics_hourly (device_id, hour_ts, cpu_pct_avg, mem_pct_avg, disk_pct_avg, temp_c_avg, samples)
            SELECT device_id,
                   strftime('%Y-%m-%dT%H:00:00Z', replace(ts, 'Z', '')) AS hour_ts,
                   AVG(cpu_pct),
                   AVG(100.0 * mem_used / NULLIF(mem_total, 0)),
                   AVG(100.0 * disk_used / NULLIF(disk_total, 0)),
                   AVG(temp_c),
                   COUNT(*)
            FROM device_metrics
            WHERE ts < ?
            GROUP BY device_id, strftime('%Y-%m-%dT%H:00:00Z', replace(ts, 'Z', ''))
            ON CONFLICT(device_id, hour_ts) DO UPDATE SET
                cpu_pct_avg  = (device_metrics_hourly.samples * device_metrics_hourly.cpu_pct_avg + excluded.samples * excluded.cpu_pct_avg) / (device_metrics_hourly.samples + excluded.samples),
                mem_pct_avg  = (device_metrics_hourly.samples * device_metrics_hourly.mem_pct_avg + excluded.samples * excluded.mem_pct_avg) / (device_metrics_hourly.samples + excluded.samples),
                disk_pct_avg = (device_metrics_hourly.samples * device_metrics_hourly.disk_pct_avg + excluded.samples * excluded.disk_pct_avg) / (device_metrics_hourly.samples + excluded.samples),
                temp_c_avg   = (device_metrics_hourly.samples * device_metrics_hourly.temp_c_avg + excluded.samples * excluded.temp_c_avg) / (device_metrics_hourly.samples + excluded.samples),
                samples      = device_metrics_hourly.samples + excluded.samples
            """,
            (raw_cutoff,),
        )
        metrics_deleted = conn.execute("DELETE FROM device_metrics WHERE ts < ?", (raw_cutoff,)).rowcount
        interfaces_deleted = conn.execute("DELETE FROM interface_stats WHERE ts < ?", (raw_cutoff,)).rowcount
        # --- Roll-up optik: mentah → optical_hourly (min/avg/max), lalu buang ---
        conn.execute(
            """
            INSERT INTO optical_hourly (device_id, if_name, hour_ts,
                rx_min, rx_avg, rx_max, tx_min, tx_avg, tx_max, temp_min, temp_avg, temp_max, samples)
            SELECT device_id, if_name,
                   strftime('%Y-%m-%dT%H:00:00Z', replace(ts, 'Z', '')) AS hour_ts,
                   MIN(rx_dbm), AVG(rx_dbm), MAX(rx_dbm),
                   MIN(tx_dbm), AVG(tx_dbm), MAX(tx_dbm),
                   MIN(temp_c), AVG(temp_c), MAX(temp_c),
                   COUNT(*)
            FROM optical_readings
            WHERE ts < ?
            GROUP BY device_id, if_name, strftime('%Y-%m-%dT%H:00:00Z', replace(ts, 'Z', ''))
            ON CONFLICT(device_id, if_name, hour_ts) DO UPDATE SET
                rx_min = MIN(optical_hourly.rx_min, excluded.rx_min),
                rx_avg = (optical_hourly.samples * optical_hourly.rx_avg + excluded.samples * excluded.rx_avg) / (optical_hourly.samples + excluded.samples),
                rx_max = MAX(optical_hourly.rx_max, excluded.rx_max),
                tx_min = MIN(optical_hourly.tx_min, excluded.tx_min),
                tx_avg = (optical_hourly.samples * optical_hourly.tx_avg + excluded.samples * excluded.tx_avg) / (optical_hourly.samples + excluded.samples),
                tx_max = MAX(optical_hourly.tx_max, excluded.tx_max),
                temp_min = MIN(optical_hourly.temp_min, excluded.temp_min),
                temp_avg = (optical_hourly.samples * optical_hourly.temp_avg + excluded.samples * excluded.temp_avg) / (optical_hourly.samples + excluded.samples),
                temp_max = MAX(optical_hourly.temp_max, excluded.temp_max),
                samples = optical_hourly.samples + excluded.samples
            """,
            (raw_cutoff,),
        )
        optical_deleted = conn.execute("DELETE FROM optical_readings WHERE ts < ?", (raw_cutoff,)).rowcount
        optical_hourly_deleted = conn.execute("DELETE FROM optical_hourly WHERE hour_ts < ?", (optical_hourly_cutoff,)).rowcount
        hourly_deleted = conn.execute("DELETE FROM device_metrics_hourly WHERE hour_ts < ?", (hourly_cutoff,)).rowcount
        events_deleted = conn.execute("DELETE FROM events WHERE created_at < ?", (events_cutoff,)).rowcount
        audit_deleted = conn.execute("DELETE FROM audit_log WHERE ts < ?", (audit_cutoff,)).rowcount
        # --- Modul MikroTik: histori metrik, log, aksi, backup ---
        mt_metric_deleted = conn.execute("DELETE FROM mt_metric_history WHERE ts < ?", (raw_cutoff,)).rowcount
        try:
            mt_log_days = int(settings.get("mt_log_retention_days", 30))
        except (TypeError, ValueError):
            mt_log_days = 30
        mt_logs_deleted = conn.execute(
            "DELETE FROM mt_logs WHERE polled_at < ?", (_days_ago(mt_log_days),)
        ).rowcount
        mt_actions_deleted = conn.execute("DELETE FROM mt_actions WHERE ts < ?", (audit_cutoff,)).rowcount
        # E1: retensi kejadian masuk (default syslog 14 hari, trap 30 hari).
        syslog_deleted = conn.execute(
            "DELETE FROM syslog_events WHERE ts < ?", (_days_ago(syslog_days),)
        ).rowcount
        traps_deleted = conn.execute(
            "DELETE FROM snmp_traps WHERE ts < ?", (_days_ago(trap_days),)
        ).rowcount
        # E3: rollup jam flow terlebih dulu, baru buang data mentah & jam lama.
        try:
            flow_days = int(settings.get("retention_flow_days", 7))
        except (TypeError, ValueError):
            flow_days = 7
        try:
            flow_hourly_days = int(settings.get("retention_flow_hourly_days", 90))
        except (TypeError, ValueError):
            flow_hourly_days = 90
        try:
            from . import flow as _flow_db

            flow_result = _flow_db.purge_flow(flow_days, flow_hourly_days, conn=conn)
        except Exception:
            flow_result = {"rolled_hours": 0, "flow_raw_deleted": 0, "flow_hourly_deleted": 0}
        # Backup: simpan N versi terakhir per perangkat.
        try:
            backup_keep = int(settings.get("mt_backup_retention_versions", 30))
        except (TypeError, ValueError):
            backup_keep = 30
        backup_keep = max(1, backup_keep)
        conn.execute(
            """
            DELETE FROM mt_backups WHERE id IN (
                SELECT b.id FROM mt_backups b
                WHERE (SELECT COUNT(*) FROM mt_backups x
                       WHERE x.device_id = b.device_id AND x.created_at >= b.created_at) > ?
            )
            """,
            (backup_keep,),
        )
        conn.commit()
        return {
            "metrics_deleted": metrics_deleted,
            "interfaces_deleted": interfaces_deleted,
            "optical_deleted": optical_deleted,
            "optical_hourly_deleted": optical_hourly_deleted,
            "hourly_deleted": hourly_deleted,
            "events_deleted": events_deleted,
            "audit_deleted": audit_deleted,
            "mt_metric_deleted": mt_metric_deleted,
            "mt_logs_deleted": mt_logs_deleted,
            "mt_actions_deleted": mt_actions_deleted,
            "syslog_deleted": syslog_deleted,
            "traps_deleted": traps_deleted,
            **flow_result,
        }

SETTING_DEFAULTS = {
    "org_name": "Koperasi Pegawai Telkom",
    "timezone": "Asia/Jakarta",
    "default_poll_interval": "60",
    "idle_timeout_minutes": "30",
    "retention_raw_days": "7",
    "retention_hourly_days": "30",
    "retention_events_days": "30",
    "retention_audit_days": "365",
    # Modul MikroTik
    "mt_write_actions_enabled": "0",
    "mt_bruteforce_threshold": "10",
    "mt_bruteforce_window_minutes": "10",
    "mt_pool_high_pct": "90",
    "mt_sessions_drop_pct": "30",
    "mt_sessions_drop_window_minutes": "5",
    "mt_time_drift_seconds": "120",
    "mt_stale_user_days": "90",
    "mt_log_retention_days": "30",
    "mt_backup_retention_versions": "30",
    # Backup basis data (T3)
    "db_backup_enabled": "1",
    "db_backup_dir": "",
    "db_backup_keep": "14",
    # E1 — penerima syslog/trap
    "retention_syslog_days": "14",
    # E3 — flow
    "retention_flow_days": "7",
    "retention_flow_hourly_days": "90",
    "flow_top_n": "100",
    "flow_hourly_rolled_until": "",
    "retention_trap_days": "30",
    "ingest_rate_limit_per_sec": "200",
    "ingest_rate_window_seconds": "10",
    "ingest_max_message_bytes": "8192",
    "ingest_queue_size": "10000",
    "ingest_batch_size": "200",
    "ingest_flush_seconds": "1",
    # E2 — badai notifikasi
    "notification_storm_threshold": "5",
    # E2 - kebijakan MFA
    "mfa_required_roles": "[]",
    "mfa_grace_days": "7",
    "mfa_policy_started_at": "",
    # E4 - metrik Prometheus
    "metrics_enabled": "0",
    "metrics_interfaces": "important",
    "metrics_series_limit": "20000",
}

def get_settings(overrides=None):
    with _lock:
        rows = get_conn().execute("SELECT key, value FROM settings").fetchall()
    values = dict(SETTING_DEFAULTS)
    for row in rows:
        values[row["key"]] = row["value"]
    if overrides:
        for key, value in overrides.items():
            try:
                values[key] = int(value)
            except (TypeError, ValueError):
                values[key] = value
    return values

def get_setting(key, default=None):
    return get_settings().get(key, SETTING_DEFAULTS.get(key, default))

def set_settings(values):
    now = _now()
    allowed = set(SETTING_DEFAULTS)
    with _lock:
        conn = get_conn()
        for key, value in values.items():
            if key not in allowed:
                continue
            conn.execute(
                "INSERT INTO settings (key, value, updated_at) VALUES (?,?,?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, str(value), now),
            )
        conn.commit()

def db_file_size():
    try:
        return os.path.getsize(DB_PATH)
    except OSError:
        return 0

def table_counts():
    tables = ["devices", "device_checks", "device_metrics", "interface_stats",
              "events", "problems", "audit_log", "services", "users",
              "optical_readings", "optical_modules", "optical_hourly",
              "fiber_links", "fiber_measurements"]
    counts = {}
    with _lock:
        for table in tables:
            counts[table] = get_conn().execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
    return counts
