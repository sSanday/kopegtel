"""Pengaturan aplikasi (tabel settings), info sistem, retensi pesan."""

import os
from datetime import datetime, timedelta, timezone

from .connection import (
    DB_PATH,
    _days_ago,
    _lock,
    transaction,
    _now,
    get_conn,
    readonly_connection,
)
from .fiber import (
    get_fiber_settings,
)

# Nilai retensi yang dapat diubah dari Administration. Dipakai bersama oleh job
# pembersihan dan pratinjau agar angka yang ditampilkan sama dengan yang dihapus.
RETENTION_DEFAULTS = {
    "retention_checks_days": 30,
    "retention_raw_days": 7,
    "retention_hourly_days": 30,
    "retention_events_days": 30,
    "retention_audit_days": 365,
}
RETENTION_MAX_DAYS = 3650


def _clamp_days(value, default):
    try:
        days = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if days < 1:
        return default
    return min(days, RETENTION_MAX_DAYS)


def retention_days(settings=None):
    """Nilai retensi efektif (hari) dari pengaturan, sudah divalidasi.

    Satu sumber untuk job harian dan pratinjau UI. Nilai yang hilang atau tidak
    valid kembali ke default, sehingga database lama tidak membuat job gagal.
    """
    settings = get_settings({}) if settings is None else settings
    return {
        key: _clamp_days(settings.get(key), default)
        for key, default in RETENTION_DEFAULTS.items()
    }


def retention_cutoff(days, hourly=False):
    """Batas waktu efektif untuk retensi.

    ``hourly=True`` membulatkan ke awal jam, sama seperti kolom ``hour_ts``
    pada tabel ringkasan, sehingga pratinjau dan job menghitung baris yang
    sama.
    """
    moment = datetime.now(timezone.utc) - timedelta(days=days)
    return moment.strftime("%Y-%m-%dT%H:00:00Z" if hourly else "%Y-%m-%dT%H:%M:%SZ")


def rollup_optical_hourly(since_hour=None):
    """Agregasi pembacaan optik mentah yang jamnya sudah selesai ke optical_hourly.

    Idempoten: nilai jam yang sama diganti (bukan ditambah), jadi aman dijalankan
    berulang tiap jam dan saat startup. ``since_hour`` membatasi agregasi ke jam
    yang masih tercakup retensi mentah, sehingga jam yang datanya sudah sebagian
    dibuang tidak dihitung ulang dengan sampel parsial.
    """
    current_hour = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:00:00Z")
    where = "ts < ?"
    params = [current_hour]
    if since_hour:
        where += " AND ts >= ?"
        params.append(since_hour)
    with transaction() as conn:
        cursor = conn.execute(
            f"""
            INSERT INTO optical_hourly (device_id, if_name, hour_ts,
                rx_min, rx_avg, rx_max, tx_min, tx_avg, tx_max, temp_min, temp_avg, temp_max, samples)
            SELECT device_id, if_name,
                   strftime('%Y-%m-%dT%H:00:00Z', replace(ts, 'Z', '')) AS hour_ts,
                   MIN(rx_dbm), AVG(rx_dbm), MAX(rx_dbm),
                   MIN(tx_dbm), AVG(tx_dbm), MAX(tx_dbm),
                   MIN(temp_c), AVG(temp_c), MAX(temp_c),
                   COUNT(*)
            FROM optical_readings
            WHERE {where}
            GROUP BY device_id, if_name, strftime('%Y-%m-%dT%H:00:00Z', replace(ts, 'Z', ''))
            ON CONFLICT(device_id, if_name, hour_ts) DO UPDATE SET
                rx_min = excluded.rx_min,
                rx_avg = excluded.rx_avg,
                rx_max = excluded.rx_max,
                tx_min = excluded.tx_min,
                tx_avg = excluded.tx_avg,
                tx_max = excluded.tx_max,
                temp_min = excluded.temp_min,
                temp_avg = excluded.temp_avg,
                temp_max = excluded.temp_max,
                samples = excluded.samples
            """,
            params,
        )
        return cursor.rowcount


def purge_old_checks(days=None):
    """Hapus riwayat pemeriksaan perangkat yang lebih tua dari retensi.

    ``days=None`` memakai pengaturan ``retention_checks_days`` sehingga
    perubahan di UI berlaku pada pembersihan harian berikutnya.
    """
    if days is None:
        days = retention_days()["retention_checks_days"]
    else:
        days = _clamp_days(days, RETENTION_DEFAULTS["retention_checks_days"])
    cutoff = retention_cutoff(days)
    with transaction() as conn:
        cursor = conn.execute("DELETE FROM device_checks WHERE checked_at < ?", (cutoff,))
        return cursor.rowcount

def purge_old_data(days=None, hourly_days=None, events_days=None, audit_days=None):
    """Terapkan retensi data mentah, ringkasan, event, dan audit.

    Semua nilai ``None`` berarti memakai pengaturan (Administration -> retensi
    data); argumen eksplisit hanya dipertahankan untuk pemanggil lama. Data
    optik ikut retensi mentah setelah diagregasi ke optical_hourly, lalu
    ringkasan optik per jam disimpan sesuai pengaturan fiber.
    """
    settings = get_settings({})
    resolved = retention_days(settings)
    days = resolved["retention_raw_days"] if days is None else _clamp_days(days, resolved["retention_raw_days"])
    hourly_days = resolved["retention_hourly_days"] if hourly_days is None else _clamp_days(hourly_days, resolved["retention_hourly_days"])
    events_days = resolved["retention_events_days"] if events_days is None else _clamp_days(events_days, resolved["retention_events_days"])
    audit_days = resolved["retention_audit_days"] if audit_days is None else _clamp_days(audit_days, resolved["retention_audit_days"])
    fiber = get_fiber_settings()
    try:
        optical_hourly_days = int(fiber.get("retention_optical_hourly_days", 90))
    except (TypeError, ValueError, OverflowError):
        optical_hourly_days = 90
    syslog_days = _clamp_days(settings.get("retention_syslog_days", 14), 14)
    trap_days = _clamp_days(settings.get("retention_trap_days", 30), 30)

    raw_cutoff = retention_cutoff(days)
    hourly_cutoff = retention_cutoff(hourly_days, hourly=True)
    optical_hourly_cutoff = retention_cutoff(optical_hourly_days, hourly=True)
    events_cutoff = retention_cutoff(events_days)
    audit_cutoff = retention_cutoff(audit_days)
    with transaction() as conn:
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
        # Rollup yang sama dipakai job per jam; di sini dijalankan lebih dulu agar
        # jam yang datanya akan dihapus tetap punya ringkasan.
        rollup_optical_hourly()
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
        from . import flow as _flow_db

        # Jangan telan kegagalan flow: seluruh purge berada dalam satu
        # transaksi, jadi error di sini harus membatalkan semuanya agar tidak
        # ada rollup/DELETE flow yang tersimpan setengah jalan.
        flow_result = _flow_db.purge_flow(flow_days, flow_hourly_days, conn=conn)
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
    "idle_timeout_minutes": "120",
    **{key: str(value) for key, value in RETENTION_DEFAULTS.items()},
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
    with transaction() as conn:
        for key, value in values.items():
            if key not in allowed:
                continue
            conn.execute(
                "INSERT INTO settings (key, value, updated_at) VALUES (?,?,?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, str(value), now),
            )

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
    # Koneksi baca terpisah: COUNT(*) pada tabel besar tidak boleh menahan
    # _lock global yang dipakai poller dan request lain.
    with readonly_connection() as conn:
        for table in tables:
            counts[table] = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
    return counts
