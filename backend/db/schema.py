"""Skema basis data (DDL) + migrasi idempotent saat init_db()."""

import sqlite3

from .connection import (
    _lock,
    _now,
    get_conn,
)

# H-3: pekerjaan scan TIDAK menyimpan kredensial. Kredensial hidup hanya di
# memori selama thread pemindaian berjalan.
SCAN_JOB_COLUMNS = (
    "id",
    "cidr",
    "status",
    "total",
    "completed",
    "found",
    "created_by",
    "created_at",
    "finished_at",
    "results",
)
SCAN_JOB_SECRET_COLUMNS = ("community", "mikrotik_user", "mikrotik_password")


def _scan_jobs_table_columns(conn):
    return {row["name"] for row in conn.execute("PRAGMA table_info(scan_jobs)").fetchall()}


def rebuild_scan_jobs_without_secrets(conn):
    """Buang kolom kredensial scan lama dan hapus fisik sisa nilainya.

    Database lama (sebelum H-3) menyimpan community SNMP, user, dan password
    MikroTik di scan_jobs. Fungsi ini membuat ulang tabel tanpa kolom tersebut,
    menyalin data yang aman, lalu VACUUM + checkpoint WAL agar byte lama tidak
    tertinggal di nms.db, nms.db-wal, atau salinan backup yang diproses.

    Return True bila tabel diubah. Aman dipanggil berulang.
    """
    columns = _scan_jobs_table_columns(conn)
    if not (columns & set(SCAN_JOB_SECRET_COLUMNS)):
        return False
    missing = [name for name in SCAN_JOB_COLUMNS if name not in columns]
    if missing:
        raise RuntimeError(
            "Skema scan_jobs lama tidak dikenali; kolom aman tidak ditemukan: "
            + ", ".join(missing)
        )

    # secure_delete membuat SQLite menimpa isi halaman yang dihapus dengan nol,
    # bukan hanya menandainya bebas. VACUUM kemudian membangun ulang berkas DB.
    conn.execute("PRAGMA secure_delete = ON")
    conn.execute("DROP TABLE IF EXISTS scan_jobs_clean")
    conn.execute(
        """
        CREATE TABLE scan_jobs_clean (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cidr TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'running',
            total INTEGER NOT NULL DEFAULT 0,
            completed INTEGER NOT NULL DEFAULT 0,
            found INTEGER NOT NULL DEFAULT 0,
            created_by TEXT NOT NULL DEFAULT '-',
            created_at TEXT NOT NULL,
            finished_at TEXT,
            results TEXT NOT NULL DEFAULT '[]'
        )
        """
    )
    ordered = ", ".join(SCAN_JOB_COLUMNS)
    conn.execute(
        f"INSERT INTO scan_jobs_clean ({ordered}) SELECT {ordered} FROM scan_jobs"
    )
    conn.execute("DROP TABLE scan_jobs")
    conn.execute("ALTER TABLE scan_jobs_clean RENAME TO scan_jobs")
    conn.commit()
    conn.execute("VACUUM")
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.Error:
        # Bukan mode WAL / ada pembaca lain: VACUUM tetap sudah membangun ulang DB.
        pass
    conn.commit()
    return True


def init_db():
    with _lock:
        conn = get_conn()
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS devices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                ip TEXT NOT NULL UNIQUE,
                type TEXT NOT NULL DEFAULT 'lainnya',
                location TEXT NOT NULL DEFAULT '',
                poll_interval INTEGER NOT NULL DEFAULT 60,
                status TEXT NOT NULL DEFAULT 'unknown',
                latency_ms REAL,
                last_seen TEXT,
                snmp_enabled INTEGER NOT NULL DEFAULT 0,
                snmp_version TEXT NOT NULL DEFAULT 'v2c',
                snmp_community TEXT,
                snmp_port INTEGER NOT NULL DEFAULT 161,
                snmp_v3_user TEXT,
                snmp_v3_auth_proto TEXT NOT NULL DEFAULT 'SHA',
                snmp_v3_auth_password TEXT,
                snmp_v3_priv_proto TEXT NOT NULL DEFAULT 'AES',
                snmp_v3_priv_password TEXT,
                mikrotik_enabled INTEGER NOT NULL DEFAULT 0,
                mikrotik_port INTEGER NOT NULL DEFAULT 8728,
                mikrotik_user TEXT,
                mikrotik_password TEXT,
                mikrotik_ssl INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS device_checks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
                checked_at TEXT NOT NULL,
                status TEXT NOT NULL,
                latency_ms REAL,
                detail_json TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_checks_device_time
                ON device_checks (device_id, checked_at);

            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
                old_status TEXT,
                new_status TEXT NOT NULL,
                message TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_events_device_time
                ON events (device_id, created_at);

            CREATE TABLE IF NOT EXISTS device_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
                ts TEXT NOT NULL,
                cpu_pct REAL,
                mem_used INTEGER,
                mem_total INTEGER,
                disk_used INTEGER,
                disk_total INTEGER,
                temp_c REAL,
                cpu_temp_c REAL,
                voltage REAL,
                uptime_s INTEGER,
                ping_ms REAL
            );
            CREATE INDEX IF NOT EXISTS idx_metrics_device_time
                ON device_metrics (device_id, ts);

            CREATE TABLE IF NOT EXISTS device_metrics_hourly (
                device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
                hour_ts TEXT NOT NULL,
                cpu_pct_avg REAL,
                mem_pct_avg REAL,
                disk_pct_avg REAL,
                temp_c_avg REAL,
                samples INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (device_id, hour_ts)
            );

            CREATE TABLE IF NOT EXISTS interface_stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
                ts TEXT NOT NULL,
                if_name TEXT NOT NULL,
                running INTEGER NOT NULL DEFAULT 0,
                disabled INTEGER NOT NULL DEFAULT 0,
                rx_bytes INTEGER,
                tx_bytes INTEGER,
                rx_bps REAL,
                tx_bps REAL
            );
            CREATE INDEX IF NOT EXISTS idx_if_stats_device_time
                ON interface_stats (device_id, ts);
            CREATE INDEX IF NOT EXISTS idx_if_stats_device_name_time
                ON interface_stats (device_id, if_name, ts);

            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                user_id INTEGER,
                username TEXT NOT NULL DEFAULT '-',
                ip TEXT NOT NULL DEFAULT '-',
                action TEXT NOT NULL,
                object TEXT NOT NULL DEFAULT '',
                summary TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_audit_time ON audit_log (ts);
            CREATE INDEX IF NOT EXISTS idx_audit_action_time
                ON audit_log (action, ts);

            CREATE TABLE IF NOT EXISTS login_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL COLLATE NOCASE,
                ip TEXT NOT NULL DEFAULT '-',
                failed_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_login_attempts_time ON login_attempts (failed_at);
            CREATE INDEX IF NOT EXISTS idx_login_attempts_ip_time
                ON login_attempts (ip, failed_at);
            CREATE INDEX IF NOT EXISTS idx_login_attempts_user_ip_time
                ON login_attempts (username, ip, failed_at);

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS host_groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                description TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS monitoring_profiles (
                device_type TEXT PRIMARY KEY,
                interval_seconds INTEGER NOT NULL DEFAULT 60,
                thresholds TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS maintenance_windows (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                device_id INTEGER REFERENCES devices(id) ON DELETE CASCADE,
                group_id INTEGER REFERENCES host_groups(id) ON DELETE SET NULL,
                starts_at TEXT NOT NULL,
                ends_at TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                created_by TEXT NOT NULL DEFAULT '-'
            );
            CREATE INDEX IF NOT EXISTS idx_maintenance_time
                ON maintenance_windows (starts_at, ends_at);

            CREATE TABLE IF NOT EXISTS scan_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cidr TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'running',
                total INTEGER NOT NULL DEFAULT 0,
                completed INTEGER NOT NULL DEFAULT 0,
                found INTEGER NOT NULL DEFAULT 0,
                created_by TEXT NOT NULL DEFAULT '-',
                created_at TEXT NOT NULL,
                finished_at TEXT,
                results TEXT NOT NULL DEFAULT '[]'
            );

            CREATE TABLE IF NOT EXISTS services (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                division TEXT NOT NULL DEFAULT '',
                sla_target REAL NOT NULL DEFAULT 99.0,
                mode TEXT NOT NULL DEFAULT 'all',  -- all | any
                description TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS service_devices (
                service_id INTEGER NOT NULL REFERENCES services(id) ON DELETE CASCADE,
                device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
                PRIMARY KEY (service_id, device_id)
            );

            CREATE TABLE IF NOT EXISTS notification_channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                type TEXT NOT NULL,  -- telegram | smtp
                config TEXT NOT NULL DEFAULT '{}',  -- JSON terenkripsi (Fernet)
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS message_templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                body TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS action_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                min_severity TEXT NOT NULL DEFAULT 'warning',
                group_id INTEGER REFERENCES host_groups(id) ON DELETE SET NULL,
                device_id INTEGER REFERENCES devices(id) ON DELETE CASCADE,
                channel_id INTEGER NOT NULL REFERENCES notification_channels(id) ON DELETE CASCADE,
                template_id INTEGER NOT NULL REFERENCES message_templates(id) ON DELETE CASCADE,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS action_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                rule_id INTEGER,
                channel_id INTEGER,
                device_id INTEGER,
                problem_id INTEGER,
                status TEXT NOT NULL,  -- sent | failed | skipped
                detail TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_action_log_time ON action_log (ts);

            CREATE TABLE IF NOT EXISTS problems (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
                kind TEXT NOT NULL,
                severity TEXT NOT NULL DEFAULT 'warning',
                message TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'open',  -- open | resolved
                started_at TEXT NOT NULL,
                resolved_at TEXT,
                acked_by TEXT,
                acked_at TEXT,
                link_id INTEGER REFERENCES fiber_links(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_problems_status ON problems (status, device_id, kind);

            CREATE TABLE IF NOT EXISTS optical_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                description TEXT NOT NULL DEFAULT '',
                rx_warn_low REAL,
                rx_crit_low REAL,
                rx_warn_high REAL,
                rx_crit_high REAL,
                temp_warn_c REAL,
                temp_crit_c REAL,
                rx_sensitivity_dbm REAL,
                rx_overload_dbm REAL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS fiber_links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                device_a_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
                port_a TEXT NOT NULL,
                device_b_id INTEGER REFERENCES devices(id) ON DELETE SET NULL,
                port_b TEXT,
                end_b_label TEXT,
                length_km REAL NOT NULL,
                fiber_type TEXT NOT NULL DEFAULT 'SM G.652D',
                wavelength_nm INTEGER NOT NULL DEFAULT 1310,
                wavelength_b_nm INTEGER,
                splices INTEGER NOT NULL DEFAULT 0,
                connectors INTEGER NOT NULL DEFAULT 2,
                splitter_ratio TEXT,
                profile_id INTEGER REFERENCES optical_profiles(id) ON DELETE SET NULL,
                notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_fiber_links_device_a ON fiber_links (device_a_id);

            CREATE TABLE IF NOT EXISTS optical_readings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
                if_name TEXT NOT NULL,
                ts TEXT NOT NULL,
                rx_dbm REAL,
                tx_dbm REAL,
                temp_c REAL,
                voltage_v REAL,
                bias_ma REAL,
                rx_loss INTEGER NOT NULL DEFAULT 0,
                tx_fault INTEGER NOT NULL DEFAULT 0,
                module_present INTEGER NOT NULL DEFAULT 1,
                ddm_supported INTEGER NOT NULL DEFAULT 1
            );
            CREATE INDEX IF NOT EXISTS idx_optical_readings_port_time
                ON optical_readings (device_id, if_name, ts);
            CREATE INDEX IF NOT EXISTS idx_optical_readings_time ON optical_readings (ts);

            CREATE TABLE IF NOT EXISTS optical_modules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
                if_name TEXT NOT NULL,
                vendor TEXT,
                part_no TEXT,
                serial TEXT,
                type TEXT,
                wavelength_nm INTEGER,
                link_length_m INTEGER,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                UNIQUE (device_id, if_name)
            );

            CREATE TABLE IF NOT EXISTS optical_hourly (
                device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
                if_name TEXT NOT NULL,
                hour_ts TEXT NOT NULL,
                rx_min REAL,
                rx_avg REAL,
                rx_max REAL,
                tx_min REAL,
                tx_avg REAL,
                tx_max REAL,
                temp_min REAL,
                temp_avg REAL,
                temp_max REAL,
                samples INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (device_id, if_name, hour_ts)
            );

            CREATE TABLE IF NOT EXISTS fiber_measurements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                link_id INTEGER NOT NULL REFERENCES fiber_links(id) ON DELETE CASCADE,
                measured_at TEXT NOT NULL,
                technician TEXT NOT NULL DEFAULT '',
                point TEXT NOT NULL DEFAULT '',
                method TEXT NOT NULL DEFAULT 'otdr',  -- otdr | power_meter
                value_db REAL,
                value_dbm REAL,
                notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                created_by TEXT NOT NULL DEFAULT '-'
            );
            CREATE INDEX IF NOT EXISTS idx_fiber_measurements_link
                ON fiber_measurements (link_id, measured_at);

            CREATE TABLE IF NOT EXISTS optical_port_state (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
                if_name TEXT NOT NULL,
                is_optical INTEGER NOT NULL DEFAULT 1,
                ddm_supported INTEGER NOT NULL DEFAULT 1,
                last_check TEXT,
                consec TEXT NOT NULL DEFAULT '{}',
                last_serial TEXT,
                UNIQUE (device_id, if_name)
            );

            CREATE TABLE IF NOT EXISTS optical_link_state (
                link_id INTEGER PRIMARY KEY REFERENCES fiber_links(id) ON DELETE CASCADE,
                consec TEXT NOT NULL DEFAULT '{}',
                status TEXT,
                metrics TEXT NOT NULL DEFAULT '{}',
                updated_at TEXT
            );

            -- ============================================================
            -- Modul MikroTik (M1-M6)
            -- ============================================================
            CREATE TABLE IF NOT EXISTS mt_capabilities (
                device_id INTEGER PRIMARY KEY REFERENCES devices(id) ON DELETE CASCADE,
                routeros_version TEXT,
                major_version INTEGER,
                packages TEXT NOT NULL DEFAULT '[]',
                capabilities TEXT NOT NULL DEFAULT '{}',
                checked_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS mt_device_settings (
                device_id INTEGER PRIMARY KEY REFERENCES devices(id) ON DELETE CASCADE,
                modules TEXT NOT NULL DEFAULT '{}',
                intervals TEXT NOT NULL DEFAULT '{}',
                important_interfaces TEXT NOT NULL DEFAULT '[]',
                write_enabled INTEGER NOT NULL DEFAULT 0,
                write_user TEXT,
                write_password TEXT,
                write_port INTEGER,
                write_ssl INTEGER NOT NULL DEFAULT 0,
                ssh_enabled INTEGER NOT NULL DEFAULT 0,
                ssh_port INTEGER NOT NULL DEFAULT 22,
                ssh_user TEXT,
                ssh_password TEXT,
                ssh_host_key TEXT,
                backup_enabled INTEGER NOT NULL DEFAULT 0,
                backup_hour INTEGER NOT NULL DEFAULT 2,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS mt_poll_state (
                device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
                layer TEXT NOT NULL,
                last_run TEXT,
                last_ok TEXT,
                last_error TEXT,
                retry_after TEXT,
                cursor TEXT NOT NULL DEFAULT '{}',
                truncated TEXT NOT NULL DEFAULT '{}',
                PRIMARY KEY (device_id, layer)
            );

            CREATE TABLE IF NOT EXISTS mt_system (
                device_id INTEGER PRIMARY KEY REFERENCES devices(id) ON DELETE CASCADE,
                identity TEXT,
                model TEXT,
                serial TEXT,
                version TEXT,
                major INTEGER,
                board TEXT,
                architecture TEXT,
                current_firmware TEXT,
                upgrade_firmware TEXT,
                firmware_status TEXT,
                license_level TEXT,
                license_expires TEXT,
                clock_time TEXT,
                clock_date TEXT,
                gmt_offset TEXT,
                time_zone_name TEXT,
                router_utc TEXT,
                drift_seconds INTEGER,
                packages TEXT NOT NULL DEFAULT '[]',
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS mt_metric_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
                ts TEXT NOT NULL,
                metric TEXT NOT NULL,
                value REAL
            );
            CREATE INDEX IF NOT EXISTS idx_mt_metric_device_metric_time
                ON mt_metric_history (device_id, metric, ts);
            CREATE INDEX IF NOT EXISTS idx_mt_metric_time ON mt_metric_history (ts);

            CREATE TABLE IF NOT EXISTS mt_dhcp_leases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
                row_key TEXT NOT NULL,
                polled_at TEXT NOT NULL,
                stale INTEGER NOT NULL DEFAULT 0,
                data TEXT NOT NULL,
                UNIQUE (device_id, row_key)
            );
            CREATE INDEX IF NOT EXISTS idx_mt_leases_device ON mt_dhcp_leases (device_id, stale);

            CREATE TABLE IF NOT EXISTS mt_dhcp_pools (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
                row_key TEXT NOT NULL,
                polled_at TEXT NOT NULL,
                stale INTEGER NOT NULL DEFAULT 0,
                data TEXT NOT NULL,
                UNIQUE (device_id, row_key)
            );

            CREATE TABLE IF NOT EXISTS mt_arp (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
                row_key TEXT NOT NULL,
                polled_at TEXT NOT NULL,
                stale INTEGER NOT NULL DEFAULT 0,
                data TEXT NOT NULL,
                UNIQUE (device_id, row_key)
            );
            CREATE INDEX IF NOT EXISTS idx_mt_arp_device ON mt_arp (device_id, stale);

            CREATE TABLE IF NOT EXISTS mt_neighbors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
                row_key TEXT NOT NULL,
                polled_at TEXT NOT NULL,
                stale INTEGER NOT NULL DEFAULT 0,
                data TEXT NOT NULL,
                UNIQUE (device_id, row_key)
            );

            CREATE TABLE IF NOT EXISTS mt_routes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
                row_key TEXT NOT NULL,
                polled_at TEXT NOT NULL,
                stale INTEGER NOT NULL DEFAULT 0,
                data TEXT NOT NULL,
                UNIQUE (device_id, row_key)
            );

            CREATE TABLE IF NOT EXISTS mt_routing_neighbors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
                row_key TEXT NOT NULL,
                polled_at TEXT NOT NULL,
                stale INTEGER NOT NULL DEFAULT 0,
                data TEXT NOT NULL,
                UNIQUE (device_id, row_key)
            );

            CREATE TABLE IF NOT EXISTS mt_ppp_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
                row_key TEXT NOT NULL,
                polled_at TEXT NOT NULL,
                stale INTEGER NOT NULL DEFAULT 0,
                data TEXT NOT NULL,
                UNIQUE (device_id, row_key)
            );
            CREATE INDEX IF NOT EXISTS idx_mt_ppp_device ON mt_ppp_sessions (device_id, stale);

            CREATE TABLE IF NOT EXISTS mt_hotspot_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
                row_key TEXT NOT NULL,
                polled_at TEXT NOT NULL,
                stale INTEGER NOT NULL DEFAULT 0,
                data TEXT NOT NULL,
                UNIQUE (device_id, row_key)
            );

            CREATE TABLE IF NOT EXISTS mt_wireless_clients (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
                row_key TEXT NOT NULL,
                polled_at TEXT NOT NULL,
                stale INTEGER NOT NULL DEFAULT 0,
                data TEXT NOT NULL,
                UNIQUE (device_id, row_key)
            );

            CREATE TABLE IF NOT EXISTS mt_interfaces (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
                row_key TEXT NOT NULL,
                polled_at TEXT NOT NULL,
                stale INTEGER NOT NULL DEFAULT 0,
                data TEXT NOT NULL,
                UNIQUE (device_id, row_key)
            );

            CREATE TABLE IF NOT EXISTS mt_ip_addresses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
                row_key TEXT NOT NULL,
                polled_at TEXT NOT NULL,
                stale INTEGER NOT NULL DEFAULT 0,
                data TEXT NOT NULL,
                UNIQUE (device_id, row_key)
            );

            CREATE TABLE IF NOT EXISTS mt_firewall_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
                row_key TEXT NOT NULL,
                polled_at TEXT NOT NULL,
                stale INTEGER NOT NULL DEFAULT 0,
                data TEXT NOT NULL,
                UNIQUE (device_id, row_key)
            );

            CREATE TABLE IF NOT EXISTS mt_address_lists (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
                row_key TEXT NOT NULL,
                polled_at TEXT NOT NULL,
                stale INTEGER NOT NULL DEFAULT 0,
                data TEXT NOT NULL,
                UNIQUE (device_id, row_key)
            );

            CREATE TABLE IF NOT EXISTS mt_queues (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
                row_key TEXT NOT NULL,
                polled_at TEXT NOT NULL,
                stale INTEGER NOT NULL DEFAULT 0,
                data TEXT NOT NULL,
                UNIQUE (device_id, row_key)
            );

            CREATE TABLE IF NOT EXISTS mt_services (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
                row_key TEXT NOT NULL,
                polled_at TEXT NOT NULL,
                stale INTEGER NOT NULL DEFAULT 0,
                data TEXT NOT NULL,
                UNIQUE (device_id, row_key)
            );

            CREATE TABLE IF NOT EXISTS mt_users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
                row_key TEXT NOT NULL,
                polled_at TEXT NOT NULL,
                stale INTEGER NOT NULL DEFAULT 0,
                data TEXT NOT NULL,
                UNIQUE (device_id, row_key)
            );

            CREATE TABLE IF NOT EXISTS mt_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
                row_key TEXT NOT NULL,
                polled_at TEXT NOT NULL,
                log_time TEXT,
                topics TEXT,
                severity TEXT,
                message TEXT,
                is_login_failure INTEGER NOT NULL DEFAULT 0,
                username TEXT,
                source_ip TEXT,
                service TEXT,
                stale INTEGER NOT NULL DEFAULT 0,
                UNIQUE (device_id, row_key)
            );
            CREATE INDEX IF NOT EXISTS idx_mt_logs_device_time ON mt_logs (device_id, polled_at);
            CREATE INDEX IF NOT EXISTS idx_mt_logs_failure ON mt_logs (device_id, is_login_failure, source_ip);

            CREATE TABLE IF NOT EXISTS mt_security_findings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
                finding_key TEXT NOT NULL,
                category TEXT,
                severity TEXT,
                title TEXT,
                detail TEXT,
                remediation TEXT,
                detected_at TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                ignored INTEGER NOT NULL DEFAULT 0,
                ignored_by TEXT,
                ignored_at TEXT,
                ignore_reason TEXT,
                stale INTEGER NOT NULL DEFAULT 0,
                UNIQUE (device_id, finding_key)
            );
            CREATE INDEX IF NOT EXISTS idx_mt_findings_device ON mt_security_findings (device_id, ignored, stale);

            CREATE TABLE IF NOT EXISTS mt_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
                ts TEXT NOT NULL,
                username TEXT NOT NULL DEFAULT '-',
                action TEXT NOT NULL,
                target TEXT NOT NULL DEFAULT '',
                params TEXT NOT NULL DEFAULT '{}',
                result TEXT NOT NULL DEFAULT '',
                verified INTEGER,
                before_data TEXT,
                after_data TEXT,
                ip TEXT NOT NULL DEFAULT '-'
            );
            CREATE INDEX IF NOT EXISTS idx_mt_actions_device_time ON mt_actions (device_id, ts);

            CREATE TABLE IF NOT EXISTS mt_backups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
                created_at TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'manual',
                filename TEXT NOT NULL DEFAULT '',
                size INTEGER,
                sha256 TEXT,
                content TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_mt_backups_device_time ON mt_backups (device_id, created_at);

            -- ============================================================
            -- E2 — Eskalasi & kontak pengguna
            -- ============================================================
            CREATE TABLE IF NOT EXISTS escalation_policies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                description TEXT NOT NULL DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1,
                business_hours_enabled INTEGER NOT NULL DEFAULT 0,
                business_start TEXT NOT NULL DEFAULT '08:00',
                business_end TEXT NOT NULL DEFAULT '17:00',
                business_days TEXT NOT NULL DEFAULT '1,2,3,4,5',
                repeat_minutes INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS escalation_steps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                policy_id INTEGER NOT NULL REFERENCES escalation_policies(id) ON DELETE CASCADE,
                step_order INTEGER NOT NULL,
                delay_minutes INTEGER NOT NULL DEFAULT 0,
                target_type TEXT NOT NULL DEFAULT 'channel',
                target_id INTEGER NOT NULL,
                business_hours_only INTEGER NOT NULL DEFAULT 0,
                UNIQUE (policy_id, step_order)
            );
            CREATE INDEX IF NOT EXISTS idx_escalation_steps_policy ON escalation_steps (policy_id, step_order);

            CREATE TABLE IF NOT EXISTS escalation_state (
                problem_id INTEGER PRIMARY KEY REFERENCES problems(id) ON DELETE CASCADE,
                policy_id INTEGER NOT NULL REFERENCES escalation_policies(id) ON DELETE CASCADE,
                last_step INTEGER NOT NULL DEFAULT 0,
                last_sent_at TEXT,
                recovery_sent INTEGER NOT NULL DEFAULT 0,
                stopped_reason TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS syslog_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                received_at TEXT NOT NULL,
                device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
                source_ip TEXT NOT NULL,
                hostname TEXT,
                facility INTEGER,
                severity INTEGER,
                tag TEXT,
                message TEXT NOT NULL,
                raw TEXT,
                dedup_hash TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_syslog_time ON syslog_events (ts);
            CREATE INDEX IF NOT EXISTS idx_syslog_device_time ON syslog_events (device_id, ts);
            CREATE INDEX IF NOT EXISTS idx_syslog_severity_time ON syslog_events (severity, ts);
            CREATE INDEX IF NOT EXISTS idx_syslog_dedup ON syslog_events (device_id, dedup_hash);

            CREATE TABLE IF NOT EXISTS snmp_traps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                received_at TEXT NOT NULL,
                device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
                source_ip TEXT NOT NULL,
                trap_oid TEXT NOT NULL,
                trap_name TEXT,
                varbinds TEXT NOT NULL DEFAULT '[]',
                raw TEXT,
                dedup_hash TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_traps_time ON snmp_traps (ts);
            CREATE INDEX IF NOT EXISTS idx_traps_device_time ON snmp_traps (device_id, ts);
            CREATE INDEX IF NOT EXISTS idx_traps_oid_time ON snmp_traps (trap_oid, ts);

            CREATE TABLE IF NOT EXISTS ingest_receivers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL UNIQUE,
                enabled INTEGER NOT NULL DEFAULT 0,
                port INTEGER NOT NULL,
                bind_address TEXT NOT NULL DEFAULT '0.0.0.0',
                config TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS ingest_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                kind TEXT NOT NULL DEFAULT 'syslog',          -- syslog | trap
                match_type TEXT NOT NULL DEFAULT 'contains',  -- contains | startswith | regex
                pattern TEXT NOT NULL DEFAULT '',
                oid_prefix TEXT NOT NULL DEFAULT '',
                min_severity TEXT NOT NULL DEFAULT 'information',
                device_id INTEGER REFERENCES devices(id) ON DELETE CASCADE,
                group_id INTEGER REFERENCES host_groups(id) ON DELETE SET NULL,
                problem_kind TEXT NOT NULL DEFAULT 'syslog_rule',
                problem_severity TEXT NOT NULL DEFAULT 'warning',
                cooldown_seconds INTEGER NOT NULL DEFAULT 60,
                auto_resolve_minutes INTEGER NOT NULL DEFAULT 30,
                recovery_pattern TEXT NOT NULL DEFAULT '',
                resolves_rule_id INTEGER REFERENCES ingest_rules(id) ON DELETE SET NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_ingest_rules_kind ON ingest_rules (kind, enabled);

            -- ============================================================
            -- E3 — Traffic Flow (NetFlow/IPFIX)
            -- ============================================================
            CREATE TABLE IF NOT EXISTS flow_buckets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bucket_ts TEXT NOT NULL,
                exporter_device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
                kind TEXT NOT NULL,                 -- src | dst | pair | app | proto | interface
                key TEXT NOT NULL,
                bytes INTEGER NOT NULL DEFAULT 0,
                packets INTEGER NOT NULL DEFAULT 0,
                flows INTEGER NOT NULL DEFAULT 0,
                UNIQUE (bucket_ts, exporter_device_id, kind, key)
            );
            CREATE INDEX IF NOT EXISTS idx_flow_buckets_time ON flow_buckets (bucket_ts);
            CREATE INDEX IF NOT EXISTS idx_flow_buckets_dev_time ON flow_buckets (exporter_device_id, bucket_ts);
            CREATE INDEX IF NOT EXISTS idx_flow_buckets_kind_time ON flow_buckets (kind, bucket_ts);

            CREATE TABLE IF NOT EXISTS flow_buckets_hourly (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                hour_ts TEXT NOT NULL,
                exporter_device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
                kind TEXT NOT NULL,
                key TEXT NOT NULL,
                bytes INTEGER NOT NULL DEFAULT 0,
                packets INTEGER NOT NULL DEFAULT 0,
                flows INTEGER NOT NULL DEFAULT 0,
                UNIQUE (hour_ts, exporter_device_id, kind, key)
            );
            CREATE INDEX IF NOT EXISTS idx_flow_hourly_time ON flow_buckets_hourly (hour_ts);
            CREATE INDEX IF NOT EXISTS idx_flow_hourly_dev_time ON flow_buckets_hourly (exporter_device_id, hour_ts);

            -- ============================================================
            -- E4 — Token API Prometheus & metrik
            -- ============================================================
            CREATE TABLE IF NOT EXISTS api_tokens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                token_hash TEXT NOT NULL,
                token_prefix TEXT NOT NULL DEFAULT '',
                scopes TEXT NOT NULL DEFAULT '["metrics"]',
                expires_at TEXT,
                allowed_ips TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                last_used_at TEXT,
                created_by TEXT NOT NULL DEFAULT '-',
                revoked_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_api_tokens_hash ON api_tokens (token_hash);

            CREATE TABLE IF NOT EXISTS instance_lease (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                pid INTEGER NOT NULL,
                token TEXT NOT NULL,
                heartbeat TEXT NOT NULL
            );
            """
        )
        # Migrasi aman untuk database yang sudah ada: tambah kolom devices baru.
        existing_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(devices)").fetchall()
        }
        device_columns = [
            ("routeros_version", "TEXT"),
            ("mikrotik_model", "TEXT"),
            ("mikrotik_board", "TEXT"),
            ("mikrotik_architecture", "TEXT"),
            ("mikrotik_serial", "TEXT"),
            ("mikrotik_firmware", "TEXT"),
            ("cpu_count", "INTEGER"),
            ("cpu_frequency_mhz", "INTEGER"),
            ("metrics_error", "TEXT"),
            ("threshold_cpu_warn", "REAL NOT NULL DEFAULT 70"),
            ("threshold_cpu_crit", "REAL NOT NULL DEFAULT 90"),
            ("threshold_mem_warn", "REAL NOT NULL DEFAULT 70"),
            ("threshold_mem_crit", "REAL NOT NULL DEFAULT 90"),
            ("threshold_storage_warn", "REAL NOT NULL DEFAULT 70"),
            ("threshold_storage_crit", "REAL NOT NULL DEFAULT 90"),
            ("threshold_temp_warn", "REAL NOT NULL DEFAULT 60"),
            ("threshold_temp_crit", "REAL NOT NULL DEFAULT 75"),
            ("group_id", "INTEGER REFERENCES host_groups(id)"),
            ("parent_id", "INTEGER REFERENCES devices(id) ON DELETE SET NULL"),
            ("maintenance", "INTEGER NOT NULL DEFAULT 0"),
        ]
        for name, ddl in device_columns:
            if name not in existing_columns:
                conn.execute(f"ALTER TABLE devices ADD COLUMN {name} {ddl}")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_checks_time ON device_checks (checked_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_if_stats_device_name_id ON interface_stats (device_id, if_name, id)")

        user_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()
        }
        user_adds = [
            ("role", "TEXT NOT NULL DEFAULT 'admin'"),
            ("must_change_password", "INTEGER NOT NULL DEFAULT 0"),
            ("updated_at", "TEXT"),
        ]
        for name, ddl in user_adds:
            if name not in user_columns:
                conn.execute(f"ALTER TABLE users ADD COLUMN {name} {ddl}")

        check_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(device_checks)").fetchall()
        }
        if "maintenance" not in check_columns:
            conn.execute("ALTER TABLE device_checks ADD COLUMN maintenance INTEGER NOT NULL DEFAULT 0")

        problem_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(problems)").fetchall()
        }
        if "link_id" not in problem_columns:
            conn.execute("ALTER TABLE problems ADD COLUMN link_id INTEGER REFERENCES fiber_links(id) ON DELETE CASCADE")
        for name, ddl in (
            ("rule_id", "INTEGER"),
            ("event_count", "INTEGER NOT NULL DEFAULT 1"),
            ("last_seen_at", "TEXT"),
            ("notified_at", "TEXT"),
            ("suppressed_by_parent", "TEXT"),
        ):
            if name not in problem_columns:
                conn.execute(f"ALTER TABLE problems ADD COLUMN {name} {ddl}")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_problems_link ON problems (link_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_problems_rule ON problems (rule_id, status)")

        # E2: aturan aksi dapat memilih kebijakan eskalasi.
        rule_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(action_rules)").fetchall()
        }
        if "escalation_policy_id" not in rule_columns:
            conn.execute(
                "ALTER TABLE action_rules ADD COLUMN escalation_policy_id"
                " INTEGER REFERENCES escalation_policies(id) ON DELETE SET NULL"
            )

        # E2: kontak per pengguna untuk eskalasi target "pengguna".
        user_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()
        }
        if "email" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN email TEXT NOT NULL DEFAULT ''")
        if "telegram_chat_id" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN telegram_chat_id TEXT NOT NULL DEFAULT ''")
        # E2: MFA TOTP (RFC 6238). Secret terenkripsi Fernet; tidak pernah dicetak.
        for name, ddl in (
            ("mfa_secret", "TEXT"),
            ("mfa_pending_secret", "TEXT"),
            ("mfa_enabled", "INTEGER NOT NULL DEFAULT 0"),
            ("mfa_recovery_hashes", "TEXT NOT NULL DEFAULT '[]'"),
            ("mfa_last_step", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if name not in user_columns:
                conn.execute(f"ALTER TABLE users ADD COLUMN {name} {ddl}")
        # Profil optik awal (dapat diedit): nilai umum, WAJIB disesuaikan datasheet SFP.
        profile_count = conn.execute("SELECT COUNT(*) AS n FROM optical_profiles").fetchone()["n"]
        if profile_count == 0:
            now = _now()
            conn.executemany(
                """
                INSERT INTO optical_profiles
                    (name, description, rx_warn_low, rx_crit_low, rx_warn_high, rx_crit_high,
                     temp_warn_c, temp_crit_c, rx_sensitivity_dbm, rx_overload_dbm, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                [
                    (
                        "SFP umum",
                        "Nilai awal umum untuk SFP optik (bukan dari pengukuran) — WAJIB disesuaikan dengan datasheet SFP Anda: daya Tx, sensitivitas penerima, dan batas overload. Titik awal saja.",
                        -15.0, -19.0, -1.0, 1.0, 70.0, 75.0, -24.0, 0.0, now, now,
                    ),
                    (
                        "GPON ONT (class B+)",
                        "Nilai awal umum untuk GPON ONT class B+ (bukan dari pengukuran) — WAJIB disesuaikan dengan datasheet ONT/OLT Anda. Ambang atas mengikuti batas overload GPON B+ (sekitar -8 dBm); titik awal saja.",
                        -24.0, -27.0, -9.0, -8.0, 70.0, 75.0, -28.0, -8.0, now, now,
                    ),
                ],
            )

        # Penerima E1: default MATI, port >= 1024, bind dapat diubah admin.
        now = _now()
        for kind, port in (("syslog_udp", 5514), ("syslog_tcp", 5514), ("snmp_trap", 1162),
                           ("netflow_udp", 2055), ("ipfix_udp", 4739)):
            conn.execute(
                "INSERT OR IGNORE INTO ingest_receivers (kind, enabled, port, bind_address, config, updated_at)"
                " VALUES (?, 0, ?, '0.0.0.0', '', ?)",
                (kind, port, now),
            )

        # Aturan awal E1 (dapat diedit/dimatikan). Hanya dibuat bila tabel kosong.
        rule_count = conn.execute("SELECT COUNT(*) AS n FROM ingest_rules").fetchone()["n"]
        if rule_count == 0:
            base_rules = [
                ("Interface link down (trap)", "trap", "contains", "", "1.3.6.1.6.3.1.1.5.3",
                 "information", None, None, "trap_rule", "high", 60, 15, "", None, 1),
                ("Authentication failure (trap)", "trap", "contains", "", "1.3.6.1.6.3.1.1.5.5",
                 "information", None, None, "trap_rule", "high", 60, 30, "", None, 1),
                ("Reboot perangkat (trap)", "trap", "contains", "", "1.3.6.1.6.3.1.1.5.1",
                 "information", None, None, "trap_rule", "warning", 60, 15, "", None, 1),
                ("Login gagal (syslog)", "syslog", "contains", "login failure", "",
                 "information", None, None, "syslog_rule", "warning", 60, 30, "", None, 1),
                ("Perubahan konfigurasi (syslog)", "syslog", "contains", "changed", "",
                 "information", None, None, "syslog_rule", "warning", 60, 60, "", None, 1),
            ]
            down_id = None
            for rule in base_rules:
                cursor = conn.execute(
                    "INSERT INTO ingest_rules (name, kind, match_type, pattern, oid_prefix, min_severity,"
                    " device_id, group_id, problem_kind, problem_severity, cooldown_seconds,"
                    " auto_resolve_minutes, recovery_pattern, resolves_rule_id, enabled, created_at, updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (*rule, now, now),
                )
                if rule[0].startswith("Interface link down"):
                    down_id = cursor.lastrowid
            conn.execute(
                "INSERT INTO ingest_rules (name, kind, match_type, pattern, oid_prefix, min_severity,"
                " device_id, group_id, problem_kind, problem_severity, cooldown_seconds,"
                " auto_resolve_minutes, recovery_pattern, resolves_rule_id, enabled, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("Interface link up (trap)", "trap", "contains", "", "1.3.6.1.6.3.1.1.5.4",
                 "information", None, None, "trap_rule", "information", 0, 0, "", down_id, 1, now, now),
            )

        # Migrasi satu kali: timestamp naif (lokal) -> UTC 'Z'.
        row = conn.execute("PRAGMA user_version").fetchone()
        if (row[0] if row else 0) < 1:
            naive_cols = [
                ("device_checks", "checked_at"),
                ("device_metrics", "ts"),
                ("interface_stats", "ts"),
                ("events", "created_at"),
                ("device_metrics_hourly", "hour_ts"),
                ("devices", "last_seen"),
                ("devices", "created_at"),
                ("devices", "updated_at"),
                ("users", "created_at"),
            ]
            for table, column in naive_cols:
                conn.execute(
                    f"UPDATE {table} SET {column} = strftime('%Y-%m-%dT%H:%M:%SZ',"
                    f" replace({column}, 'T', ' '), '-7 hours')"
                    f" WHERE {column} IS NOT NULL AND instr({column}, 'Z') = 0"
                )
            conn.execute("PRAGMA user_version = 1")

        # H-3: hapus kolom kredensial scan dari database lama + bersihkan byte-nya.
        if rebuild_scan_jobs_without_secrets(conn):
            conn.execute("PRAGMA user_version = 2")
        conn.commit()

