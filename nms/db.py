"""nms.db — lapisan database SQLite."""

import glob
import os
import sqlite3
import threading
import time
from datetime import datetime

from nms.config import BASE_DIR

db_lock = threading.Lock()


def resolve_db_path():
    """Path DB aktif: env NMS_DB_PATH, default <BASE_DIR>/network.db."""
    return os.environ.get("NMS_DB_PATH", os.path.join(BASE_DIR, "network.db"))


def get_db():
    conn = sqlite3.connect(resolve_db_path(), timeout=30, check_same_thread=False)
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        # journal_mode bisa gagal saat disk penuh (pernah: disk I/O error
        # massal 23 Sep di semua job). Jangan matikan koneksi — SQLite tetap
        # bisa jalan dengan journal mode default.
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError as e:
        print(f"[DB] journal_mode=WAL gagal ({e}), lanjut mode default")
    conn.execute("PRAGMA synchronous=NORMAL")

    try:
        conn.execute("PRAGMA wal_autocheckpoint=1000")
        conn.execute("PRAGMA journal_size_limit=33554432")
    except Exception:
        pass
    conn.row_factory = sqlite3.Row
    return conn, conn.cursor()


def _commit_with_retry(conn, retries=5):
    for attempt in range(retries):
        try:
            conn.commit()
            return True
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower() and attempt < retries - 1:
                time.sleep(0.2 * (attempt + 1))
                continue
            raise
    return False


def _insert_system_log(c, event_type, host, message, timestamp=None):
    ts = timestamp or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    c.execute(
        "INSERT INTO system_logs (timestamp, event_type, host, message) VALUES (?, ?, ?, ?)",
        (ts, event_type, host, message),
    )


def init_db():
    conn, c = get_db()

    c.execute("""CREATE TABLE IF NOT EXISTS ping_logs (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        host        TEXT    NOT NULL,
        latency     REAL    NOT NULL,
        packet_loss REAL    NOT NULL DEFAULT 0,
        timestamp   TEXT    NOT NULL
    )""")

    try:
        c.execute(
            "ALTER TABLE ping_logs ADD COLUMN packet_loss REAL NOT NULL DEFAULT 0"
        )
    except sqlite3.OperationalError:
        pass

    c.execute("""CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'operator',
        created_at TEXT NOT NULL,
        last_login TEXT
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS host_thresholds (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        host TEXT UNIQUE NOT NULL,
        cpu_threshold REAL,
        ram_threshold REAL,
        disk_threshold REAL,
        created_at TEXT NOT NULL
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS alert_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        host TEXT NOT NULL,
        alert_type TEXT NOT NULL,
        severity TEXT NOT NULL,
        message TEXT,
        triggered_at TEXT NOT NULL,
        resolved_at TEXT,
        duration_s INTEGER,
        acknowledged_at TEXT,
        acknowledged_by TEXT,
        assigned_to TEXT,
        note TEXT
    )""")
    for _col, _ddl in (
        ("acknowledged_at", "ALTER TABLE alert_history ADD COLUMN acknowledged_at TEXT"),
        ("acknowledged_by", "ALTER TABLE alert_history ADD COLUMN acknowledged_by TEXT"),
        ("assigned_to", "ALTER TABLE alert_history ADD COLUMN assigned_to TEXT"),
        ("note", "ALTER TABLE alert_history ADD COLUMN note TEXT"),
        ("escalated_at", "ALTER TABLE alert_history ADD COLUMN escalated_at TEXT"),
    ):
        try:
            c.execute(_ddl)
        except sqlite3.OperationalError:
            pass

    c.execute(
        "INSERT OR IGNORE INTO settings (key, value) VALUES ('cpu_threshold', '85.0')"
    )
    c.execute(
        "INSERT OR IGNORE INTO settings (key, value) VALUES ('ram_threshold', '90.0')"
    )
    c.execute(
        "INSERT OR IGNORE INTO settings (key, value) VALUES ('disk_threshold', '90.0')"
    )
    c.execute(
        "INSERT OR IGNORE INTO settings (key, value) VALUES ('fiber_rx_overload', '-8.0')"
    )
    c.execute(
        "INSERT OR IGNORE INTO settings (key, value) VALUES ('fiber_rx_warn', '-25.0')"
    )
    c.execute(
        "INSERT OR IGNORE INTO settings (key, value) VALUES ('fiber_rx_crit', '-27.0')"
    )
    c.execute(
        "INSERT OR IGNORE INTO settings (key, value) VALUES ('fiber_rx_target', '-18.0')"
    )
    c.execute(
        "INSERT OR IGNORE INTO settings (key, value) VALUES ('fiber_degrade_db', '3.0')"
    )
    c.execute(
        "INSERT OR IGNORE INTO settings (key, value) VALUES ('fiber_degrade_days', '7')"
    )
    c.execute(
        "INSERT OR IGNORE INTO settings (key, value) VALUES ('fiber_stale_min', '60')"
    )
    c.execute(
        "INSERT OR IGNORE INTO settings (key, value) VALUES ('fiber_flap_flips', '4')"
    )
    c.execute(
        "INSERT OR IGNORE INTO settings (key, value) VALUES ('fiber_flap_hours', '24')"
    )
    c.execute(
        "INSERT OR IGNORE INTO settings (key, value) VALUES ('fiber_parent_min', '5')"
    )
    c.execute(
        "INSERT OR IGNORE INTO settings (key, value) VALUES ('fiber_tx_min', '0.0')"
    )
    c.execute(
        "INSERT OR IGNORE INTO settings (key, value) VALUES ('fiber_tx_max', '5.0')"
    )
    c.execute(
        "INSERT OR IGNORE INTO settings (key, value) VALUES ('temp_threshold', '60.0')"
    )
    c.execute(
        "INSERT OR IGNORE INTO settings (key, value) VALUES ('temp_crit', '75.0')"
    )
    c.execute(
        "INSERT OR IGNORE INTO settings (key, value) VALUES ('mt_cpu_oid', '1.3.6.1.4.1.14988.1.1.3.11.0')"
    )
    c.execute(
        "INSERT OR IGNORE INTO settings (key, value) VALUES ('mt_mem_oid', '1.3.6.1.4.1.14988.1.1.3.12.0')"
    )
    c.execute(
        "INSERT OR IGNORE INTO settings (key, value) VALUES ('mt_storage_oid', '1.3.6.1.4.1.14988.1.1.3.13.0')"
    )
    c.execute(
        "INSERT OR IGNORE INTO settings (key, value) VALUES ('mt_temp_oid', '1.3.6.1.4.1.14988.1.1.3.10.0')"
    )
    c.execute(
        "INSERT OR IGNORE INTO settings (key, value) VALUES ('mt_temp_div', '10')"
    )
    c.execute(
        "INSERT OR IGNORE INTO settings (key, value) VALUES ('mt_stale_min', '15')"
    )

    c.execute("""CREATE TABLE IF NOT EXISTS down_events (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        host        TEXT    NOT NULL,
        started_at  TEXT    NOT NULL,
        resolved_at TEXT,
        duration_s  INTEGER
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS hosts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ip TEXT UNIQUE NOT NULL
    )""")
    try:
        c.execute("ALTER TABLE hosts ADD COLUMN snmp_community TEXT DEFAULT ''")
        c.execute("ALTER TABLE hosts ADD COLUMN if_index INTEGER DEFAULT 1")
    except sqlite3.OperationalError:
        pass
    try:
        c.execute("ALTER TABLE hosts ADD COLUMN alias TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass
    try:
        c.execute("ALTER TABLE hosts ADD COLUMN category TEXT DEFAULT 'Uncategorized'")
    except sqlite3.OperationalError:
        pass
    try:
        c.execute("ALTER TABLE hosts ADD COLUMN snmp_profile TEXT DEFAULT 'auto'")
    except sqlite3.OperationalError:
        pass
    _ALLOWED_OID_COLS = {"cpu_oid", "mem_oid", "storage_oid", "temp_oid"}
    for _col in _ALLOWED_OID_COLS:
        try:
            c.execute(f"ALTER TABLE hosts ADD COLUMN {_col} TEXT DEFAULT ''")
        except sqlite3.OperationalError:
            pass
    for _col, _ddl in (
        ("ssh_user", "ALTER TABLE hosts ADD COLUMN ssh_user TEXT DEFAULT ''"),
        ("ssh_pass", "ALTER TABLE hosts ADD COLUMN ssh_pass TEXT DEFAULT ''"),
        ("ssh_port", "ALTER TABLE hosts ADD COLUMN ssh_port INTEGER DEFAULT 22"),
        (
            "backup_enable",
            "ALTER TABLE hosts ADD COLUMN backup_enable INTEGER NOT NULL DEFAULT 0",
        ),
        ("backup_last", "ALTER TABLE hosts ADD COLUMN backup_last TEXT DEFAULT ''"),
        (
            "backup_ok",
            "ALTER TABLE hosts ADD COLUMN backup_ok INTEGER NOT NULL DEFAULT 0",
        ),
    ):
        try:
            c.execute(_ddl)
        except sqlite3.OperationalError:
            pass
    c.execute(
        "INSERT OR IGNORE INTO settings (key, value) VALUES ('mt_backup_keep', '10')"
    )

    c.execute("""CREATE TABLE IF NOT EXISTS mt_backups(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      host TEXT NOT NULL,
      taken_at TEXT NOT NULL,
      size INTEGER NOT NULL DEFAULT 0,
      sha256 TEXT NOT NULL DEFAULT '',
      content TEXT NOT NULL DEFAULT '',
      changed INTEGER NOT NULL DEFAULT 0
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_mt_backup ON mt_backups(host, id)")

    c.execute("""CREATE TABLE IF NOT EXISTS agent_metrics (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        host TEXT NOT NULL,
        cpu_percent REAL,
        ram_percent REAL,
        disk_percent REAL DEFAULT 0.0,
        net_in REAL DEFAULT 0.0,
        net_out REAL DEFAULT 0.0,
        timestamp TEXT NOT NULL,
        source TEXT DEFAULT 'agent'
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS services (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ip TEXT NOT NULL,
        name TEXT NOT NULL,
        type TEXT NOT NULL,
        port INTEGER,
        url TEXT,
        status TEXT DEFAULT 'PENDING',
        latency REAL,
        last_checked TEXT
    )""")

    try:
        c.execute("ALTER TABLE agent_metrics ADD COLUMN disk_percent REAL DEFAULT 0.0")
    except sqlite3.OperationalError:
        pass
    try:
        c.execute("ALTER TABLE agent_metrics ADD COLUMN net_in REAL DEFAULT 0.0")
        c.execute("ALTER TABLE agent_metrics ADD COLUMN net_out REAL DEFAULT 0.0")
    except sqlite3.OperationalError:
        pass

    try:
        c.execute("ALTER TABLE agent_metrics ADD COLUMN source TEXT DEFAULT 'agent'")
    except sqlite3.OperationalError:
        pass
    try:
        c.execute(
            "UPDATE agent_metrics SET source='snmp' WHERE source IS NULL AND cpu_percent IS NULL"
        )
        c.execute("UPDATE agent_metrics SET source='agent' WHERE source IS NULL")
    except sqlite3.OperationalError:
        pass

    c.execute("""CREATE TABLE IF NOT EXISTS system_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT NOT NULL,
        event_type TEXT NOT NULL,
        host TEXT NOT NULL,
        message TEXT NOT NULL
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS audit_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT NOT NULL,
        username TEXT NOT NULL,
        action TEXT NOT NULL,
        target TEXT,
        detail TEXT,
        ip_address TEXT
    )""")

    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_ping_host_clock ON ping_logs(host, timestamp)"
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_host_clock ON agent_metrics(host, timestamp)"
    )
    c.execute("CREATE INDEX IF NOT EXISTS idx_down_host ON down_events(host)")
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_down_host_resolved ON down_events(host, resolved_at, id)"
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_syslog_type_host ON system_logs(event_type, host)"
    )

    c.execute("""CREATE TABLE IF NOT EXISTS inventory(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      hostname TEXT NOT NULL, ip TEXT UNIQUE NOT NULL,
      device_type TEXT DEFAULT '', brand_model TEXT DEFAULT '',
      location TEXT DEFAULT '', pic_name TEXT DEFAULT '', pic_phone TEXT DEFAULT '',
      install_date TEXT DEFAULT '', asset_status TEXT DEFAULT 'aktif',
      asset_no TEXT DEFAULT '', notes TEXT DEFAULT '',
      created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_inventory_ip ON inventory(ip)")

    c.execute("""CREATE TABLE IF NOT EXISTS maintenance_windows(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      host TEXT NOT NULL,
      start_at TEXT NOT NULL,
      end_at TEXT NOT NULL,
      reason TEXT DEFAULT '',
      created_by TEXT DEFAULT '',
      created_at TEXT NOT NULL
    )""")
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_maint_host_window ON maintenance_windows(host, start_at, end_at)"
    )
    try:
        c.execute(
            "ALTER TABLE down_events ADD COLUMN is_maintenance INTEGER NOT NULL DEFAULT 0"
        )
    except sqlite3.OperationalError:
        pass

    for _col, _ddl in (
        ("ssl_expires_at", "ALTER TABLE services ADD COLUMN ssl_expires_at TEXT"),
        ("ssl_days_left", "ALTER TABLE services ADD COLUMN ssl_days_left INTEGER"),
        (
            "ssl_last_alert",
            "ALTER TABLE services ADD COLUMN ssl_last_alert TEXT DEFAULT ''",
        ),
        ("ssl_checked_at", "ALTER TABLE services ADD COLUMN ssl_checked_at TEXT"),
    ):
        try:
            c.execute(_ddl)
        except sqlite3.OperationalError:
            pass

    c.execute("""CREATE TABLE IF NOT EXISTS service_history(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      service_id INTEGER NOT NULL,
      status TEXT NOT NULL,
      latency REAL NOT NULL DEFAULT 0,
      timestamp TEXT NOT NULL
    )""")
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_svc_hist ON service_history(service_id, timestamp)"
    )

    c.execute("""CREATE TABLE IF NOT EXISTS fiber_onts(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      ont_sn TEXT UNIQUE NOT NULL,
      customer TEXT DEFAULT '',
      olt_name TEXT DEFAULT '',
      pon_port TEXT DEFAULT '',
      odp_name TEXT DEFAULT '',
      rx_power REAL,
      tx_power REAL,
      status TEXT DEFAULT 'unknown',
      last_checked TEXT,
      source TEXT DEFAULT 'manual',
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS fiber_history(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      ont_id INTEGER NOT NULL,
      rx_power REAL,
      tx_power REAL,
      timestamp TEXT NOT NULL
    )""")
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_fiber_hist ON fiber_history(ont_id, timestamp)"
    )
    c.execute("CREATE INDEX IF NOT EXISTS idx_fiber_sn ON fiber_onts(ont_sn)")
    c.execute("""CREATE TABLE IF NOT EXISTS fiber_downtime(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      ont_id INTEGER NOT NULL,
      ont_sn TEXT NOT NULL DEFAULT '',
      status TEXT NOT NULL DEFAULT 'critical',
      rx_dbm REAL,
      started_at TEXT NOT NULL,
      resolved_at TEXT,
      duration_s INTEGER
    )""")
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_fiber_down ON fiber_downtime(ont_id, resolved_at)"
    )

    c.execute("""CREATE TABLE IF NOT EXISTS device_health(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      host TEXT NOT NULL,
      cpu REAL,
      mem_used REAL,
      storage_used REAL,
      temp_c REAL,
      timestamp TEXT NOT NULL,
      source TEXT DEFAULT 'snmp-mikrotik'
    )""")
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_devhealth_host_clock ON device_health(host, timestamp)"
    )
    try:
        c.execute("ALTER TABLE device_health ADD COLUMN uptime_s REAL")
    except sqlite3.OperationalError:
        pass

    c.execute("""CREATE TABLE IF NOT EXISTS snmp_interfaces(
      host TEXT NOT NULL,
      if_index INTEGER NOT NULL,
      name TEXT DEFAULT '',
      oper INTEGER,
      monitor INTEGER NOT NULL DEFAULT 1,
      last_changed TEXT,
      PRIMARY KEY (host, if_index)
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS iface_traffic(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      host TEXT NOT NULL,
      if_index INTEGER NOT NULL,
      net_in REAL DEFAULT 0.0,
      net_out REAL DEFAULT 0.0,
      timestamp TEXT NOT NULL
    )""")
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_iface_host_idx_clock ON iface_traffic(host, if_index, timestamp)"
    )
    c.execute("""CREATE TABLE IF NOT EXISTS odps(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      name TEXT UNIQUE NOT NULL,
      olt_name TEXT DEFAULT '',
      capacity INTEGER DEFAULT 8,
      location TEXT DEFAULT '',
      lat REAL,
      lon REAL,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_odp_name ON odps(name)")
    for _col, _ddl in (
        ("odp_lat", "ALTER TABLE odps ADD COLUMN lat REAL"),
        ("odp_lon", "ALTER TABLE odps ADD COLUMN lon REAL"),
        ("olt_lat", "ALTER TABLE olts ADD COLUMN lat REAL"),
        ("olt_lon", "ALTER TABLE olts ADD COLUMN lon REAL"),
    ):
        try:
            c.execute(_ddl)
        except sqlite3.OperationalError:
            pass
    try:
        c.execute("ALTER TABLE fiber_onts ADD COLUMN source TEXT DEFAULT 'manual'")
    except sqlite3.OperationalError:
        pass
    for _col, _ddl in (
        (
            "mute_alarm",
            "ALTER TABLE fiber_onts ADD COLUMN mute_alarm INTEGER NOT NULL DEFAULT 0",
        ),
        ("mute_until", "ALTER TABLE fiber_onts ADD COLUMN mute_until TEXT DEFAULT ''"),
        (
            "mute_reason",
            "ALTER TABLE fiber_onts ADD COLUMN mute_reason TEXT DEFAULT ''",
        ),
        ("last_seen", "ALTER TABLE fiber_onts ADD COLUMN last_seen TEXT DEFAULT ''"),
        ("rx_warn", "ALTER TABLE fiber_onts ADD COLUMN rx_warn REAL"),
        ("rx_crit", "ALTER TABLE fiber_onts ADD COLUMN rx_crit REAL"),
        ("ont_index", "ALTER TABLE fiber_onts ADD COLUMN ont_index TEXT DEFAULT ''"),
    ):
        try:
            c.execute(_ddl)
        except sqlite3.OperationalError:
            pass
    try:
        c.execute(
            "UPDATE fiber_onts SET last_seen=COALESCE(NULLIF(last_checked, ''), updated_at) "
            "WHERE (last_seen IS NULL OR last_seen='') "
            "AND (rx_power IS NOT NULL OR tx_power IS NOT NULL)"
        )
    except sqlite3.OperationalError:
        pass

    c.execute("""CREATE TABLE IF NOT EXISTS olts(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      name TEXT UNIQUE NOT NULL,
      ip TEXT NOT NULL DEFAULT '',
      community TEXT NOT NULL DEFAULT '',
      vendor TEXT NOT NULL DEFAULT 'generic',
      rx_base TEXT DEFAULT '',
      tx_base TEXT DEFAULT '',
      div REAL NOT NULL DEFAULT 100.0,
      scale REAL NOT NULL DEFAULT 1.0,
      offset REAL NOT NULL DEFAULT 0.0,
      lat REAL,
      lon REAL,
      last_tested TEXT DEFAULT '',
      last_test_ok INTEGER NOT NULL DEFAULT 0,
      last_test_msg TEXT DEFAULT '',
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_olt_name ON olts(name)")
    for _col, _ddl in (
        ("scale", "ALTER TABLE olts ADD COLUMN scale REAL NOT NULL DEFAULT 1.0"),
        ("offset", "ALTER TABLE olts ADD COLUMN offset REAL NOT NULL DEFAULT 0.0"),
        ("last_tested", "ALTER TABLE olts ADD COLUMN last_tested TEXT DEFAULT ''"),
        (
            "last_test_ok",
            "ALTER TABLE olts ADD COLUMN last_test_ok INTEGER NOT NULL DEFAULT 0",
        ),
        ("last_test_msg", "ALTER TABLE olts ADD COLUMN last_test_msg TEXT DEFAULT ''"),
    ):
        try:
            c.execute(_ddl)
        except sqlite3.OperationalError:
            pass

    c.execute("SELECT COUNT(*) as cnt FROM hosts")
    if c.fetchone()["cnt"] == 0:
        for h in ["192.168.110.167", "192.168.110.25", "192.168.110.251"]:
            c.execute("INSERT OR IGNORE INTO hosts (ip) VALUES (?)", (h,))

    conn.commit()
    conn.close()


def get_target_hosts():
    conn, c = get_db()
    try:
        c.execute("SELECT ip FROM hosts ORDER BY id ASC")
        hosts = [r["ip"] for r in c.fetchall()]
    finally:
        conn.close()
    return hosts


def get_setting(key, default_value, type_cast=float):
    conn, c = get_db()
    try:
        c.execute("SELECT value FROM settings WHERE key=?", (key,))
        row = c.fetchone()
    finally:
        conn.close()
    if row:
        try:
            return type_cast(row["value"])
        except ValueError:
            pass
    return default_value


_MAINT_TIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d",
)


def _parse_maint_time(s):
    s = (s or "").strip()[:19]
    if not s:
        return None
    for fmt in _MAINT_TIME_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _fmt_maint_time(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def get_active_maintenance_map(now=None):
    try:
        now_str = _fmt_maint_time(now or datetime.now())
        conn, c = get_db()
        try:
            c.execute(
                "SELECT host, start_at, end_at, reason FROM maintenance_windows "
                "WHERE start_at <= ? AND end_at >= ?",
                (now_str, now_str),
            )
            out = {}
            for r in c.fetchall():
                out[r["host"]] = dict(r)
            return out
        finally:
            conn.close()
    except Exception:
        return {}


def cleanup_old_data():
    with db_lock:
        conn, c = get_db()
        try:
            c.execute(
                "DELETE FROM ping_logs WHERE timestamp < datetime('now', 'localtime', '-7 days')"
            )
            deleted = c.rowcount
            try:
                c.execute(
                    "DELETE FROM agent_metrics WHERE timestamp < datetime('now', 'localtime', '-30 days')"
                )
                deleted_agent = c.rowcount
            except Exception as e:
                print(f"[CLEANUP] agent_metrics gagal: {e}")
                deleted_agent = 0
            try:
                c.execute(
                    "DELETE FROM system_logs WHERE timestamp < datetime('now', 'localtime', '-90 days')"
                )
                deleted_logs = c.rowcount
            except Exception as e:
                print(f"[CLEANUP] system_logs gagal: {e}")
                deleted_logs = 0
            try:
                c.execute(
                    "DELETE FROM down_events WHERE resolved_at IS NOT NULL AND resolved_at < datetime('now', 'localtime', '-90 days')"
                )
                deleted_events = c.rowcount
            except Exception as e:
                print(f"[CLEANUP] down_events gagal: {e}")
                deleted_events = 0
            try:
                c.execute(
                    "DELETE FROM maintenance_windows WHERE end_at < datetime('now', 'localtime', '-90 days')"
                )
                deleted_maint = c.rowcount
            except Exception as e:
                print(f"[CLEANUP] maintenance gagal: {e}")
                deleted_maint = 0
            try:
                c.execute(
                    "DELETE FROM service_history WHERE timestamp < datetime('now', 'localtime', '-7 days')"
                )
                deleted_svc_hist = c.rowcount
            except Exception as e:
                print(f"[CLEANUP] service_history gagal: {e}")
                deleted_svc_hist = 0
            try:
                c.execute(
                    "DELETE FROM fiber_history WHERE timestamp < datetime('now', 'localtime', '-30 days')"
                )
                deleted_fiber = c.rowcount
            except Exception as e:
                print(f"[CLEANUP] fiber_history gagal: {e}")
                deleted_fiber = 0
            try:
                c.execute(
                    "DELETE FROM fiber_downtime WHERE resolved_at IS NOT NULL AND resolved_at < datetime('now', 'localtime', '-90 days')"
                )
                deleted_fiber_down = c.rowcount
            except Exception as e:
                print(f"[CLEANUP] fiber_downtime gagal: {e}")
                deleted_fiber_down = 0
            try:
                c.execute(
                    "DELETE FROM device_health WHERE timestamp < datetime('now', 'localtime', '-14 days')"
                )
                deleted_mt = c.rowcount
            except Exception as e:
                print(f"[CLEANUP] device_health gagal: {e}")
                deleted_mt = 0
            try:
                c.execute(
                    "DELETE FROM iface_traffic WHERE timestamp < datetime('now', 'localtime', '-14 days')"
                )
                deleted_iface = c.rowcount
            except Exception as e:
                print(f"[CLEANUP] iface_traffic gagal: {e}")
                deleted_iface = 0
            _commit_with_retry(conn)
        except sqlite3.OperationalError as e:
            print(f"[DB LOCK] cleanup gagal: {e}")
            try:
                conn.rollback()
            except Exception:
                pass
            return
        finally:
            conn.close()

    if datetime.now().weekday() == 0:
        try:
            chk = sqlite3.connect(resolve_db_path(), timeout=30)
            chk.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            chk.close()
        except Exception as e:
            print(f"[CLEANUP] checkpoint gagal: {e}")
    print(
        f"[CLEANUP] ping_logs={deleted} agent_metrics={deleted_agent} system_logs={deleted_logs} down_events={deleted_events} maintenance={deleted_maint} svc_hist={deleted_svc_hist} fiber={deleted_fiber} fiber_down={deleted_fiber_down} mthealth={deleted_mt} ifacetraf={deleted_iface} baris lama dihapus."
    )


def backup_database():
    backup_dir = os.path.join(BASE_DIR, "backups")
    os.makedirs(backup_dir, exist_ok=True)
    date_str = datetime.now().strftime("%Y-%m-%d")
    backup_path = os.path.join(backup_dir, f"network_backup_{date_str}.db")

    backup_ok = False
    if os.path.exists(resolve_db_path()):
        src = dst = None
        try:
            src = sqlite3.connect(resolve_db_path(), timeout=30)
            dst = sqlite3.connect(backup_path, timeout=30)
            src.backup(dst)
            backup_ok = True
        except Exception as e:
            print(f"[BACKUP] gagal: {e}")
            backup_ok = False
        finally:
            try:
                if dst:
                    dst.close()
            except Exception:
                pass
            try:
                if src:
                    src.close()
            except Exception:
                pass

            try:
                chk = sqlite3.connect(resolve_db_path(), timeout=10)
                chk.execute("PRAGMA wal_checkpoint(PASSIVE)")
                chk.close()
            except Exception:
                pass

    # Retensi hanya untuk backup harian otomatis (network_backup_YYYY-MM-DD.db).
    # Backup manual (network_backup_manual_*) TIDAK dihitung/dihapus agar tak
    # terkuras jatah 30 file — hapus manual bila disk menipis.
    backups = sorted(
        glob.glob(os.path.join(backup_dir, "network_backup_[0-9]*.db"))
    )
    if len(backups) > 30:
        for old_backup in backups[:-30]:
            try:
                os.remove(old_backup)
            except Exception:
                pass

    if backup_ok:
        print(f"[BACKUP] Database berhasil dibackup ke {backup_path}", flush=True)
    else:
        print(f"[BACKUP] GAGAL membackup ke {backup_path}", flush=True)
        try:
            from nms.notify import send_telegram_alert

            send_telegram_alert(
                f"❌ *Backup DB GAGAL*\n"
                f"Waktu: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"Target: `{backup_path}`"
            )
        except Exception as e:
            print(f"[WARN] telegram backup-alert gagal: {e}")


def restore_database(backup_file_path):
    if not os.path.exists(backup_file_path):
        return False, "File backup tidak ditemukan"
    
    if not backup_file_path.endswith('.db'):
        return False, "File harus berformat .db"
    
    db_path = resolve_db_path()
    try:
        src = dst = None
        src = sqlite3.connect(backup_file_path, timeout=30)
        src.execute("SELECT 1 FROM hosts LIMIT 1")
        src.fetchone()
        
        dst = sqlite3.connect(db_path, timeout=30)
        src.backup(dst)
        dst.close()
        src.close()
        return True, "Database berhasil di-restore"
    except sqlite3.DatabaseError:
        return False, "File backup rusak atau bukan database SQLite valid"
    except Exception as e:
        return False, f"Restore gagal: {str(e)}"


def list_backups():
    backup_dir = os.path.join(BASE_DIR, "backups")
    if not os.path.exists(backup_dir):
        return []
    
    backups = []
    for backup_file in sorted(glob.glob(os.path.join(backup_dir, "*.db")), reverse=True):
        try:
            stat = os.stat(backup_file)
            backups.append({
                "filename": os.path.basename(backup_file),
                "path": backup_file,
                "size_mb": round(stat.st_size / (1024*1024), 2),
                "created_at": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
            })
        except Exception:
            pass
    return backups


def create_user(username, password_hash, role="operator"):
    conn, c = get_db()
    try:
        c.execute(
            "INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, ?, ?)",
            (username, password_hash, role, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        )
        conn.commit()
        conn.close()
        return True, "User berhasil dibuat"
    except sqlite3.IntegrityError:
        conn.close()
        return False, "Username sudah ada"
    except Exception as e:
        conn.close()
        return False, str(e)


def get_user(username):
    conn, c = get_db()
    c.execute("SELECT id, username, role, created_at FROM users WHERE username=?", (username,))
    user = c.fetchone()
    conn.close()
    return dict(user) if user else None


def list_users():
    conn, c = get_db()
    c.execute("SELECT id, username, role, created_at FROM users ORDER BY created_at DESC")
    users = [dict(r) for r in c.fetchall()]
    conn.close()
    return users


def delete_user(username):
    if username == "admin":
        return False, "Cannot delete admin user"
    
    conn, c = get_db()
    try:
        c.execute("DELETE FROM users WHERE username=?", (username,))
        deleted = c.rowcount
        conn.commit()
        conn.close()
        return deleted > 0, "User berhasil dihapus" if deleted else "User tidak ditemukan"
    except Exception as e:
        conn.close()
        return False, str(e)


def update_user_role(username, role):
    valid_roles = ("admin", "operator", "viewer")
    if role not in valid_roles:
        return False, f"Role harus: {', '.join(valid_roles)}"
    
    conn, c = get_db()
    try:
        c.execute("UPDATE users SET role=? WHERE username=?", (role, username))
        updated = c.rowcount
        conn.commit()
        conn.close()
        return updated > 0, "Role berhasil diperbarui" if updated else "User tidak ditemukan"
    except Exception as e:
        conn.close()
        return False, str(e)


def set_host_threshold(host, cpu=None, ram=None, disk=None):
    conn, c = get_db()
    try:
        c.execute(
            "INSERT OR REPLACE INTO host_thresholds (host, cpu_threshold, ram_threshold, disk_threshold, created_at) VALUES (?, ?, ?, ?, ?)",
            (host, cpu, ram, disk, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        )
        conn.commit()
        conn.close()
        return True, "Threshold berhasil disimpan"
    except Exception as e:
        conn.close()
        return False, str(e)


def get_host_threshold(host):
    conn, c = get_db()
    c.execute("SELECT cpu_threshold, ram_threshold, disk_threshold FROM host_thresholds WHERE host=?", (host,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None


def list_host_thresholds():
    conn, c = get_db()
    c.execute("SELECT host, cpu_threshold, ram_threshold, disk_threshold FROM host_thresholds ORDER BY host ASC")
    thresholds = [dict(r) for r in c.fetchall()]
    conn.close()
    return thresholds


def delete_host_threshold(host):
    conn, c = get_db()
    try:
        c.execute("DELETE FROM host_thresholds WHERE host=?", (host,))
        deleted = c.rowcount
        conn.commit()
        conn.close()
        return deleted > 0, "Threshold berhasil dihapus" if deleted else "Host tidak ditemukan"
    except Exception as e:
        conn.close()
        return False, str(e)


def log_alert(host, alert_type, severity, message):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn, c = get_db()
    try:
        c.execute(
            "INSERT INTO alert_history (host, alert_type, severity, message, triggered_at) VALUES (?, ?, ?, ?, ?)",
            (host, alert_type, severity, message, timestamp)
        )
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        conn.close()
        return False


def resolve_alert(alert_id):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn, c = get_db()
    try:
        c.execute("SELECT triggered_at FROM alert_history WHERE id=?", (alert_id,))
        row = c.fetchone()
        if not row:
            conn.close()
            return False
        
        triggered = datetime.strptime(row["triggered_at"], "%Y-%m-%d %H:%M:%S")
        now = datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S")
        duration = int((now - triggered).total_seconds())
        
        c.execute(
            "UPDATE alert_history SET resolved_at=?, duration_s=? WHERE id=?",
            (timestamp, duration, alert_id)
        )
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        conn.close()
        return False


def update_alert_workflow(alert_id, username, acknowledged=None, assigned_to=None, note=None):
    conn, c = get_db()
    try:
        c.execute("SELECT id FROM alert_history WHERE id=?", (alert_id,))
        if not c.fetchone():
            return False
        fields = []
        params = []
        if acknowledged is not None:
            fields.extend(["acknowledged_at", "acknowledged_by"])
            params.extend([datetime.now().strftime("%Y-%m-%d %H:%M:%S") if acknowledged else None, username if acknowledged else None])
        if assigned_to is not None:
            fields.append("assigned_to")
            params.append(assigned_to.strip() or None)
        if note is not None:
            fields.append("note")
            params.append(note.strip() or None)
        if not fields:
            return True
        params.append(alert_id)
        c.execute(f"UPDATE alert_history SET {', '.join(f + '=?' for f in fields)} WHERE id=?", params)
        conn.commit()
        return True
    finally:
        conn.close()


def get_alert_history(host=None, alert_type=None, severity=None, limit=100, offset=0):
    conn, c = get_db()
    
    query = "SELECT id, host, alert_type, severity, message, triggered_at, resolved_at, duration_s, acknowledged_at, acknowledged_by, assigned_to, note, escalated_at FROM alert_history WHERE 1=1"
    params = []
    
    if host:
        query += " AND host=?"
        params.append(host)
    if alert_type:
        query += " AND alert_type=?"
        params.append(alert_type)
    if severity:
        query += " AND severity=?"
        params.append(severity)
    
    query += " ORDER BY id DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    
    c.execute(query, params)
    alerts = [dict(r) for r in c.fetchall()]
    conn.close()
    return alerts


def mark_alerts_escalated(alert_ids):
    if not alert_ids:
        return
    conn, c = get_db()
    try:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        placeholders = ",".join("?" for _ in alert_ids)
        c.execute(
            f"UPDATE alert_history SET escalated_at=? WHERE id IN ({placeholders}) AND escalated_at IS NULL",
            [timestamp, *alert_ids],
        )
        conn.commit()
    finally:
        conn.close()


def get_active_alerts(host=None):
    conn, c = get_db()
    
    query = "SELECT id, host, alert_type, severity, message, triggered_at, acknowledged_at, acknowledged_by, assigned_to, note, escalated_at FROM alert_history WHERE resolved_at IS NULL"
    params = []
    
    if host:
        query += " AND host=?"
        params.append(host)
    
    query += " ORDER BY triggered_at DESC"
    
    c.execute(query, params)
    alerts = [dict(r) for r in c.fetchall()]
    conn.close()
    return alerts


def bulk_import_hosts(hosts_data):
    """Import multiple hosts from list of dicts. 
    Each dict should have: ip, alias (optional), category (optional), snmp_community (optional)
    Returns (success_count, error_count, errors_list)
    """
    success_count = 0
    error_count = 0
    errors = []
    
    conn, c = get_db()
    
    for idx, host_data in enumerate(hosts_data, 1):
        try:
            ip = str(host_data.get("ip") or "").strip()
            alias = str(host_data.get("alias") or "").strip()
            category = str(host_data.get("category") or "Uncategorized").strip()
            snmp_community = str(host_data.get("snmp_community") or "").strip()
            
            if not ip:
                errors.append(f"Row {idx}: IP required")
                error_count += 1
                continue
            
            import re
            ipv4_re = re.compile(r"^(\d{1,3}\.){3}\d{1,3}$")
            hostname_re = re.compile(r"^[a-zA-Z0-9]([a-zA-Z0-9.\-]{0,253}[a-zA-Z0-9])?$")
            
            if ipv4_re.match(ip):
                try:
                    if any(int(p) > 255 for p in ip.split(".")):
                        errors.append(f"Row {idx} ({ip}): Invalid IP format")
                        error_count += 1
                        continue
                except ValueError:
                    errors.append(f"Row {idx} ({ip}): Invalid IP format")
                    error_count += 1
                    continue
            elif not hostname_re.match(ip):
                errors.append(f"Row {idx} ({ip}): Invalid IP/hostname")
                error_count += 1
                continue
            
            c.execute(
                "INSERT INTO hosts (ip, snmp_community, alias, category) VALUES (?, ?, ?, ?)",
                (ip, snmp_community, alias, category)
            )
            success_count += 1
        except sqlite3.IntegrityError:
            errors.append(f"Row {idx} ({ip}): Host already exists")
            error_count += 1
        except Exception as e:
            errors.append(f"Row {idx}: {str(e)}")
            error_count += 1
    
    try:
        conn.commit()
    except Exception as e:
        conn.close()
        return 0, len(hosts_data), [f"Commit failed: {str(e)}"]
    
    conn.close()
    return success_count, error_count, errors


def generate_daily_report():
    """Generate daily monitoring report. Returns report dict."""
    conn, c = get_db()
    
    report = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "period": "24h",
        "hosts": {},
        "summary": {}
    }
    
    try:
        c.execute("SELECT COUNT(*) AS cnt FROM hosts")
        total_hosts = c.fetchone()["cnt"] or 0
        
        c.execute(
            """
            SELECT host, 
                COUNT(*) AS total,
                SUM(CASE WHEN latency != -1 THEN 1 ELSE 0 END) AS up_count
            FROM ping_logs
            WHERE timestamp > datetime('now', 'localtime', '-24 hours')
            GROUP BY host
            """
        )
        
        up_hosts = 0
        for row in c.fetchall():
            total = row["total"] or 0
            up_count = row["up_count"] or 0
            uptime = round((up_count / total * 100), 1) if total else None
            
            if uptime and uptime >= 99:
                up_hosts += 1
            
            report["hosts"][row["host"]] = {
                "uptime_pct": uptime,
                "checks": total,
                "up_checks": up_count
            }
        
        c.execute(
            """
            SELECT COUNT(*) AS cnt FROM down_events
            WHERE started_at > datetime('now', 'localtime', '-24 hours')
            """
        )
        down_events = c.fetchone()["cnt"] or 0
        
        c.execute(
            """
            SELECT COUNT(*) AS cnt FROM alert_history
            WHERE triggered_at > datetime('now', 'localtime', '-24 hours')
            """
        )
        total_alerts = c.fetchone()["cnt"] or 0
        
        report["summary"] = {
            "total_hosts": total_hosts,
            "healthy_hosts": up_hosts,
            "down_events": down_events,
            "total_alerts": total_alerts
        }
    finally:
        conn.close()
    
    return report


def log_system_event(event_type, host, message):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with db_lock:
        conn, c = get_db()
        try:
            c.execute(
                "INSERT INTO system_logs (timestamp, event_type, host, message) VALUES (?, ?, ?, ?)",
                (timestamp, event_type, host, message),
            )
            _commit_with_retry(conn)
        except sqlite3.OperationalError as e:
            print(f"[DB LOCK] log_system_event gagal: {e}")
            try:
                conn.rollback()
            except Exception:
                pass
        finally:
            conn.close()
