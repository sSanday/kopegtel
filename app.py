from flask import Flask, jsonify, render_template, Response, request, redirect, url_for, session
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from apscheduler.schedulers.background import BackgroundScheduler
import subprocess, re, csv, io
import sqlite3
import os
import glob
import time
import hashlib
import difflib
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import requests
from dotenv import load_dotenv
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
from functools import wraps
import socket
import ipaddress

load_dotenv()

app            = Flask(__name__)

SECRET_KEY = os.environ.get("SECRET_KEY", "")
if not SECRET_KEY:
    raise SystemExit(
        "[FATAL] SECRET_KEY belum diset. Buat .env berisi "
        "SECRET_KEY=<64 hex acak> (contoh: python3 -c "
        "\"import secrets; print(secrets.token_hex(32))\") lalu restart."
    )
app.secret_key = SECRET_KEY
app.config['TEMPLATES_AUTO_RELOAD'] = True

app.config['REMEMBER_COOKIE_DURATION'] = timedelta(days=7)
app.config['REMEMBER_COOKIE_HTTPONLY'] = True
app.config['REMEMBER_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'


_cookie_secure = os.environ.get("COOKIE_SECURE", "0") == "1"
app.config['SESSION_COOKIE_SECURE'] = _cookie_secure
app.config['REMEMBER_COOKIE_SECURE'] = _cookie_secure
app_start_time = datetime.now()

def _is_trusted_proxy():
    try:
        return ipaddress.ip_address(request.remote_addr or "").is_loopback
    except ValueError:
        return False


def get_client_ip():
    if _is_trusted_proxy():
        real = (request.headers.get("X-Real-IP") or "").strip()
        if real:
            return real[:45]
        xff = (request.headers.get("X-Forwarded-For") or "").strip()
        if xff:
            return xff.split(",")[0].strip()[:45]
    return request.remote_addr or "unknown"


login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view        = "login"
login_manager.login_message     = "Silakan login terlebih dahulu untuk mengakses dashboard."
login_manager.login_message_category = "warning"

DASHBOARD_USERNAME = os.environ.get("DASHBOARD_USERNAME", "admin")
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "admin123")

def _verify_admin(username, password):
    username = (username or "")[:50]
    password = (password or "")[:200]
    try:
        conn, c = get_db()
        try:
            c.execute("SELECT value FROM settings WHERE key='admin_user'")
            r = c.fetchone()
            db_user = r["value"] if r else None
            c.execute("SELECT value FROM settings WHERE key='admin_pass_hash'")
            r = c.fetchone()
            db_hash = r["value"] if r else None
        finally:
            conn.close()
    except Exception as e:
        print(f"[AUTH] verifikasi ditolak (DB tidak bisa dibaca): {e}")
        return False
    if db_user and db_hash:
        if username != db_user:
            return False
        try:
            from werkzeug.security import check_password_hash
            return check_password_hash(db_hash, password)
        except Exception:
            return False
    if db_user or db_hash:

        return False

    import hmac
    return hmac.compare_digest(username, DASHBOARD_USERNAME) and hmac.compare_digest(password, DASHBOARD_PASSWORD)

def _seed_admin_from_env():
    try:
        from werkzeug.security import generate_password_hash
        conn, c = get_db()
        try:
            c.execute("SELECT value FROM settings WHERE key='admin_pass_hash'")
            if c.fetchone():
                return
            c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('admin_user', ?)", (DASHBOARD_USERNAME,))
            c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('admin_pass_hash', ?)",
                      (generate_password_hash(DASHBOARD_PASSWORD),))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        print(f"[WARN] seed admin hash gagal: {e}")

class AdminUser(UserMixin):
    def __init__(self, username=None):
        self.id = "admin"
        self.username = username or DASHBOARD_USERNAME

def _get_session_version():
    try:
        conn, c = get_db()
        try:
            c.execute("SELECT value FROM settings WHERE key='admin_session_v'")
            r = c.fetchone()
            return int(r["value"]) if r else 0
        finally:
            conn.close()
    except Exception:
        return 0

def _bump_session_version():
    try:
        conn, c = get_db()
        try:
            try:
                cur = _get_session_version()
            except Exception:
                cur = 0
            c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('admin_session_v', ?)",
                      (str(cur + 1),))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        print(f"[WARN] bump session version gagal: {e}")

def _get_admin_username():
    try:
        conn, c = get_db()
        try:
            c.execute("SELECT value FROM settings WHERE key='admin_user'")
            r = c.fetchone()
            if r and r["value"]:
                return r["value"][:50]
        finally:
            conn.close()
    except Exception:
        pass
    return DASHBOARD_USERNAME

@login_manager.user_loader
def load_user(user_id):
    if user_id == "admin":

        try:
            if session.get("admin_v", 0) != _get_session_version():
                return None
        except Exception:
            return None
        return AdminUser(username=_get_admin_username())
    return None

def api_login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated:
            return jsonify({"error": "Unauthorized. Silakan login."}), 401


        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            is_json = (request.is_json or
                       (request.content_type or "").startswith("application/json"))
            has_xrw = request.headers.get("X-Requested-With") == "XMLHttpRequest"
            if not (is_json or has_xrw):
                return jsonify({"error": "CSRF check failed. Gunakan Content-Type: application/json atau X-Requested-With."}), 403
        return f(*args, **kwargs)
    return decorated


TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")
AGENT_API_KEY      = os.environ.get("AGENT_API_KEY", "")
if not AGENT_API_KEY:
    print("[WARN] AGENT_API_KEY kosong — /api/agent/report TERBUKA tanpa auth "
          "(siapa pun bisa kirim metrik palsu). Set AGENT_API_KEY di .env untuk produksi.")
try:
    MAX_HOSTS = max(1, int(os.environ.get("MAX_HOSTS", "200")))
except (ValueError, TypeError):
    MAX_HOSTS = 200

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DB_PATH  = os.environ.get("NMS_DB_PATH", os.path.join(BASE_DIR, "network.db"))


status_memory = {}
down_since    = {}
agent_status_memory = {}
agent_offline_memory = {}


HYSTERESIS = 5.0
DOWN_COOLDOWN_S = 600
last_down_telegram = {}


db_lock = threading.Lock()


login_failures = {}
LOGIN_FAIL_TTL_S = 600


def _prune_login_failures(now_ts):
    try:
        for ip, (_, blocked, last) in list(login_failures.items()):
            if blocked:

                if now_ts > blocked + LOGIN_FAIL_TTL_S:
                    login_failures.pop(ip, None)
            else:

                if now_ts - last > LOGIN_FAIL_TTL_S:
                    login_failures.pop(ip, None)


        if len(login_failures) > 5000:
            idle = sorted(
                ((ip, v[2]) for ip, v in login_failures.items() if not v[1]),
                key=lambda x: x[1],
            )
            drop = len(login_failures) - 5000
            for ip, _ in idle[:drop]:
                login_failures.pop(ip, None)
    except Exception:
        pass


def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=WAL")
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
    c.execute("INSERT INTO system_logs (timestamp, event_type, host, message) VALUES (?, ?, ?, ?)",
              (ts, event_type, host, message))

def init_db():
    conn, c = get_db()

    c.execute('''CREATE TABLE IF NOT EXISTS ping_logs (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        host        TEXT    NOT NULL,
        latency     REAL    NOT NULL,
        packet_loss REAL    NOT NULL DEFAULT 0,
        timestamp   TEXT    NOT NULL
    )''')

    try:
        c.execute("ALTER TABLE ping_logs ADD COLUMN packet_loss REAL NOT NULL DEFAULT 0")
    except sqlite3.OperationalError:
        pass


    c.execute('''CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )''')


    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('cpu_threshold', '85.0')")
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('ram_threshold', '90.0')")
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('disk_threshold', '90.0')")
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('fiber_rx_overload', '-8.0')")
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('fiber_rx_warn', '-25.0')")
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('fiber_rx_crit', '-27.0')")
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('fiber_rx_target', '-18.0')")
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('fiber_degrade_db', '3.0')")
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('fiber_degrade_days', '7')")
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('fiber_stale_min', '60')")
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('fiber_flap_flips', '4')")
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('fiber_flap_hours', '24')")
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('fiber_parent_min', '5')")
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('fiber_tx_min', '0.0')")
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('fiber_tx_max', '5.0')")
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('temp_threshold', '60.0')")
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('temp_crit', '75.0')")
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('mt_cpu_oid', '1.3.6.1.4.1.14988.1.1.3.11.0')")
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('mt_mem_oid', '1.3.6.1.4.1.14988.1.1.3.12.0')")
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('mt_storage_oid', '1.3.6.1.4.1.14988.1.1.3.13.0')")
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('mt_temp_oid', '1.3.6.1.4.1.14988.1.1.3.10.0')")
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('mt_temp_div', '10')")
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('mt_stale_min', '15')")

    c.execute('''CREATE TABLE IF NOT EXISTS down_events (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        host        TEXT    NOT NULL,
        started_at  TEXT    NOT NULL,
        resolved_at TEXT,
        duration_s  INTEGER
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS hosts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ip TEXT UNIQUE NOT NULL
    )''')
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
    for _col in ("cpu_oid", "mem_oid", "storage_oid", "temp_oid"):
        try:
            c.execute(f"ALTER TABLE hosts ADD COLUMN {_col} TEXT DEFAULT ''")
        except sqlite3.OperationalError:
            pass
    for _col, _ddl in (
        ("ssh_user", "ALTER TABLE hosts ADD COLUMN ssh_user TEXT DEFAULT ''"),
        ("ssh_pass", "ALTER TABLE hosts ADD COLUMN ssh_pass TEXT DEFAULT ''"),
        ("ssh_port", "ALTER TABLE hosts ADD COLUMN ssh_port INTEGER DEFAULT 22"),
        ("backup_enable", "ALTER TABLE hosts ADD COLUMN backup_enable INTEGER NOT NULL DEFAULT 0"),
        ("backup_last", "ALTER TABLE hosts ADD COLUMN backup_last TEXT DEFAULT ''"),
        ("backup_ok", "ALTER TABLE hosts ADD COLUMN backup_ok INTEGER NOT NULL DEFAULT 0"),
    ):
        try:
            c.execute(_ddl)
        except sqlite3.OperationalError:
            pass
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('mt_backup_keep', '10')")

    c.execute('''CREATE TABLE IF NOT EXISTS mt_backups(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      host TEXT NOT NULL,
      taken_at TEXT NOT NULL,
      size INTEGER NOT NULL DEFAULT 0,
      sha256 TEXT NOT NULL DEFAULT '',
      content TEXT NOT NULL DEFAULT '',
      changed INTEGER NOT NULL DEFAULT 0
    )''')
    c.execute("CREATE INDEX IF NOT EXISTS idx_mt_backup ON mt_backups(host, id)")


    c.execute('''CREATE TABLE IF NOT EXISTS agent_metrics (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        host TEXT NOT NULL,
        cpu_percent REAL,
        ram_percent REAL,
        disk_percent REAL DEFAULT 0.0,
        net_in REAL DEFAULT 0.0,
        net_out REAL DEFAULT 0.0,
        timestamp TEXT NOT NULL,
        source TEXT DEFAULT 'agent'
    )''')


    c.execute('''CREATE TABLE IF NOT EXISTS services (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ip TEXT NOT NULL,
        name TEXT NOT NULL,
        type TEXT NOT NULL,
        port INTEGER,
        url TEXT,
        status TEXT DEFAULT 'PENDING',
        latency REAL,
        last_checked TEXT
    )''')


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
        c.execute("UPDATE agent_metrics SET source='snmp' WHERE source IS NULL AND cpu_percent IS NULL")
        c.execute("UPDATE agent_metrics SET source='agent' WHERE source IS NULL")
    except sqlite3.OperationalError:
        pass


    c.execute('''CREATE TABLE IF NOT EXISTS system_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT NOT NULL,
        event_type TEXT NOT NULL,
        host TEXT NOT NULL,
        message TEXT NOT NULL
    )''')


    c.execute("CREATE INDEX IF NOT EXISTS idx_ping_host_clock ON ping_logs(host, timestamp)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_agent_host_clock ON agent_metrics(host, timestamp)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_down_host ON down_events(host)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_syslog_type_host ON system_logs(event_type, host)")


    c.execute('''CREATE TABLE IF NOT EXISTS inventory(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      hostname TEXT NOT NULL, ip TEXT UNIQUE NOT NULL,
      device_type TEXT DEFAULT '', brand_model TEXT DEFAULT '',
      location TEXT DEFAULT '', pic_name TEXT DEFAULT '', pic_phone TEXT DEFAULT '',
      install_date TEXT DEFAULT '', asset_status TEXT DEFAULT 'aktif',
      asset_no TEXT DEFAULT '', notes TEXT DEFAULT '',
      created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    )''')
    c.execute("CREATE INDEX IF NOT EXISTS idx_inventory_ip ON inventory(ip)")

    c.execute('''CREATE TABLE IF NOT EXISTS maintenance_windows(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      host TEXT NOT NULL,
      start_at TEXT NOT NULL,
      end_at TEXT NOT NULL,
      reason TEXT DEFAULT '',
      created_by TEXT DEFAULT '',
      created_at TEXT NOT NULL
    )''')
    c.execute("CREATE INDEX IF NOT EXISTS idx_maint_host_window ON maintenance_windows(host, start_at, end_at)")
    try:
        c.execute("ALTER TABLE down_events ADD COLUMN is_maintenance INTEGER NOT NULL DEFAULT 0")
    except sqlite3.OperationalError:
        pass

    for _col, _ddl in (
        ("ssl_expires_at", "ALTER TABLE services ADD COLUMN ssl_expires_at TEXT"),
        ("ssl_days_left", "ALTER TABLE services ADD COLUMN ssl_days_left INTEGER"),
        ("ssl_last_alert", "ALTER TABLE services ADD COLUMN ssl_last_alert TEXT DEFAULT ''"),
        ("ssl_checked_at", "ALTER TABLE services ADD COLUMN ssl_checked_at TEXT"),
    ):
        try:
            c.execute(_ddl)
        except sqlite3.OperationalError:
            pass

    c.execute('''CREATE TABLE IF NOT EXISTS service_history(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      service_id INTEGER NOT NULL,
      status TEXT NOT NULL,
      latency REAL NOT NULL DEFAULT 0,
      timestamp TEXT NOT NULL
    )''')
    c.execute("CREATE INDEX IF NOT EXISTS idx_svc_hist ON service_history(service_id, timestamp)")


    # --- Fiber / Redaman (GPON ONT Rx/Tx power) ---
    c.execute('''CREATE TABLE IF NOT EXISTS fiber_onts(
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
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS fiber_history(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      ont_id INTEGER NOT NULL,
      rx_power REAL,
      tx_power REAL,
      timestamp TEXT NOT NULL
    )''')
    c.execute("CREATE INDEX IF NOT EXISTS idx_fiber_hist ON fiber_history(ont_id, timestamp)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_fiber_sn ON fiber_onts(ont_sn)")
    c.execute('''CREATE TABLE IF NOT EXISTS fiber_downtime(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      ont_id INTEGER NOT NULL,
      ont_sn TEXT NOT NULL DEFAULT '',
      status TEXT NOT NULL DEFAULT 'critical',
      rx_dbm REAL,
      started_at TEXT NOT NULL,
      resolved_at TEXT,
      duration_s INTEGER
    )''')
    c.execute("CREATE INDEX IF NOT EXISTS idx_fiber_down ON fiber_downtime(ont_id, resolved_at)")

    # --- MikroTik / SNMP device health (CPU/RAM/storage/suhu) ---
    c.execute('''CREATE TABLE IF NOT EXISTS device_health(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      host TEXT NOT NULL,
      cpu REAL,
      mem_used REAL,
      storage_used REAL,
      temp_c REAL,
      timestamp TEXT NOT NULL,
      source TEXT DEFAULT 'snmp-mikrotik'
    )''')
    c.execute("CREATE INDEX IF NOT EXISTS idx_devhealth_host_clock ON device_health(host, timestamp)")
    try:
        c.execute("ALTER TABLE device_health ADD COLUMN uptime_s REAL")
    except sqlite3.OperationalError:
        pass

    c.execute('''CREATE TABLE IF NOT EXISTS snmp_interfaces(
      host TEXT NOT NULL,
      if_index INTEGER NOT NULL,
      name TEXT DEFAULT '',
      oper INTEGER,
      monitor INTEGER NOT NULL DEFAULT 1,
      last_changed TEXT,
      PRIMARY KEY (host, if_index)
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS iface_traffic(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      host TEXT NOT NULL,
      if_index INTEGER NOT NULL,
      net_in REAL DEFAULT 0.0,
      net_out REAL DEFAULT 0.0,
      timestamp TEXT NOT NULL
    )''')
    c.execute("CREATE INDEX IF NOT EXISTS idx_iface_host_idx_clock ON iface_traffic(host, if_index, timestamp)")
    c.execute('''CREATE TABLE IF NOT EXISTS odps(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      name TEXT UNIQUE NOT NULL,
      olt_name TEXT DEFAULT '',
      capacity INTEGER DEFAULT 8,
      location TEXT DEFAULT '',
      lat REAL,
      lon REAL,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    )''')
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
        ("mute_alarm", "ALTER TABLE fiber_onts ADD COLUMN mute_alarm INTEGER NOT NULL DEFAULT 0"),
        ("mute_until", "ALTER TABLE fiber_onts ADD COLUMN mute_until TEXT DEFAULT ''"),
        ("mute_reason", "ALTER TABLE fiber_onts ADD COLUMN mute_reason TEXT DEFAULT ''"),
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
        # backfill sekali: ONT yang sudah punya pengukuran dianggap terpantau
        # pada cek terakhir (agar tak langsung 'stale' setelah upgrade)
        c.execute("UPDATE fiber_onts SET last_seen=COALESCE(NULLIF(last_checked, ''), updated_at) "
                  "WHERE (last_seen IS NULL OR last_seen='') "
                  "AND (rx_power IS NOT NULL OR tx_power IS NOT NULL)")
    except sqlite3.OperationalError:
        pass

    c.execute('''CREATE TABLE IF NOT EXISTS olts(
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
    )''')
    c.execute("CREATE INDEX IF NOT EXISTS idx_olt_name ON olts(name)")
    for _col, _ddl in (
        ("scale", "ALTER TABLE olts ADD COLUMN scale REAL NOT NULL DEFAULT 1.0"),
        ("offset", "ALTER TABLE olts ADD COLUMN offset REAL NOT NULL DEFAULT 0.0"),
        ("last_tested", "ALTER TABLE olts ADD COLUMN last_tested TEXT DEFAULT ''"),
        ("last_test_ok", "ALTER TABLE olts ADD COLUMN last_test_ok INTEGER NOT NULL DEFAULT 0"),
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

_MAINT_TIME_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d")

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
            c.execute("DELETE FROM ping_logs WHERE timestamp < datetime('now', 'localtime', '-7 days')")
            deleted = c.rowcount
            try:
                c.execute("DELETE FROM agent_metrics WHERE timestamp < datetime('now', 'localtime', '-30 days')")
                deleted_agent = c.rowcount
            except Exception as e:
                print(f"[CLEANUP] agent_metrics gagal: {e}")
                deleted_agent = 0
            try:
                c.execute("DELETE FROM system_logs WHERE timestamp < datetime('now', 'localtime', '-90 days')")
                deleted_logs = c.rowcount
            except Exception as e:
                print(f"[CLEANUP] system_logs gagal: {e}")
                deleted_logs = 0
            try:
                c.execute("DELETE FROM down_events WHERE resolved_at IS NOT NULL AND resolved_at < datetime('now', 'localtime', '-90 days')")
                deleted_events = c.rowcount
            except Exception as e:
                print(f"[CLEANUP] down_events gagal: {e}")
                deleted_events = 0
            try:
                c.execute("DELETE FROM maintenance_windows WHERE end_at < datetime('now', 'localtime', '-90 days')")
                deleted_maint = c.rowcount
            except Exception as e:
                print(f"[CLEANUP] maintenance gagal: {e}")
                deleted_maint = 0
            try:
                c.execute("DELETE FROM service_history WHERE timestamp < datetime('now', 'localtime', '-7 days')")
                deleted_svc_hist = c.rowcount
            except Exception as e:
                print(f"[CLEANUP] service_history gagal: {e}")
                deleted_svc_hist = 0
            try:
                c.execute("DELETE FROM fiber_history WHERE timestamp < datetime('now', 'localtime', '-30 days')")
                deleted_fiber = c.rowcount
            except Exception as e:
                print(f"[CLEANUP] fiber_history gagal: {e}")
                deleted_fiber = 0
            try:
                c.execute("DELETE FROM fiber_downtime WHERE resolved_at IS NOT NULL AND resolved_at < datetime('now', 'localtime', '-90 days')")
                deleted_fiber_down = c.rowcount
            except Exception as e:
                print(f"[CLEANUP] fiber_downtime gagal: {e}")
                deleted_fiber_down = 0
            try:
                c.execute("DELETE FROM device_health WHERE timestamp < datetime('now', 'localtime', '-14 days')")
                deleted_mt = c.rowcount
            except Exception as e:
                print(f"[CLEANUP] device_health gagal: {e}")
                deleted_mt = 0
            try:
                c.execute("DELETE FROM iface_traffic WHERE timestamp < datetime('now', 'localtime', '-14 days')")
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
            chk = sqlite3.connect(DB_PATH, timeout=30)
            chk.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            chk.close()
        except Exception as e:
            print(f"[CLEANUP] checkpoint gagal: {e}")
    print(f"[CLEANUP] ping_logs={deleted} agent_metrics={deleted_agent} system_logs={deleted_logs} down_events={deleted_events} maintenance={deleted_maint} svc_hist={deleted_svc_hist} fiber={deleted_fiber} fiber_down={deleted_fiber_down} mthealth={deleted_mt} ifacetraf={deleted_iface} baris lama dihapus.")

def backup_database():
    backup_dir = os.path.join(BASE_DIR, "backups")
    os.makedirs(backup_dir, exist_ok=True)
    date_str = datetime.now().strftime("%Y-%m-%d")
    backup_path = os.path.join(backup_dir, f"network_backup_{date_str}.db")


    backup_ok = False
    if os.path.exists(DB_PATH):
        src = dst = None
        try:
            src = sqlite3.connect(DB_PATH, timeout=30)
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
                chk = sqlite3.connect(DB_PATH, timeout=10)
                chk.execute("PRAGMA wal_checkpoint(PASSIVE)")
                chk.close()
            except Exception:
                pass


    backups = sorted(glob.glob(os.path.join(backup_dir, "network_backup_*.db")))
    if len(backups) > 30:
        for old_backup in backups[:-30]:
            try:
                os.remove(old_backup)
            except Exception:
                pass

    if backup_ok:
        print(f"[BACKUP] Database berhasil dibackup ke {backup_path}")
    else:
        print(f"[BACKUP] GAGAL membackup ke {backup_path}")


def send_telegram_alert(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[WARN] Token/Chat ID Telegram belum dikonfigurasi.")
        return
    url     = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        resp = requests.post(url, json=payload, timeout=5)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"[ERROR] Gagal kirim Telegram: {e}")

def log_system_event(event_type, host, message):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with db_lock:
        conn, c = get_db()
        try:
            c.execute("INSERT INTO system_logs (timestamp, event_type, host, message) VALUES (?, ?, ?, ?)",
                      (timestamp, event_type, host, message))
            _commit_with_retry(conn)
        except sqlite3.OperationalError as e:
            print(f"[DB LOCK] log_system_event gagal: {e}")
            try:
                conn.rollback()
            except Exception:
                pass
        finally:
            conn.close()

def audit(username, action, detail=""):
    try:
        log_system_event("AUDIT", username, f"{action} {detail}".strip())
    except Exception:
        pass

def send_startup_alert():
    targets = get_target_hosts()
    now   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    hosts = "\n".join([f"  • `{h}`" for h in targets])
    send_telegram_alert(
        f"🟢 *NMS Dashboard AKTIF*\n"
        f"Waktu  : {now}\n"
        f"Memantau {len(targets)} host:\n{hosts}"
    )

def send_heartbeat():
    targets = get_target_hosts()
    now  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = []

    conn, c = get_db()
    try:
        for host in targets:
            c.execute("""
                SELECT
                    COUNT(*)                                           AS total,
                    SUM(CASE WHEN latency != -1 THEN 1 ELSE 0 END)     AS up_count,
                    AVG(CASE WHEN latency != -1 THEN latency  END)     AS avg_ms,
                    AVG(CASE WHEN latency != -1 THEN packet_loss END)  AS avg_loss
                FROM ping_logs
                WHERE host=? AND timestamp > datetime('now','localtime','-24 hours')
            """, (host,))
            r = c.fetchone()
            total = r["total"] or 0
            up_count = r["up_count"] or 0
            uptime_pct = round((up_count / total * 100) if total else 0, 1)
            avg_ms = round(r["avg_ms"] or 0, 1)

            status_icon = "✅" if uptime_pct >= 99 else ("⚠️" if uptime_pct >= 95 else "❌")
            lines.append(f"{status_icon} `{host}` — Uptime: *{uptime_pct}%* ({avg_ms} ms)")
    finally:
        conn.close()

    body = "\n".join(lines)
    send_telegram_alert(
        f"📊 *Laporan Harian NMS (24 Jam Terakhir)*\n"
        f"Waktu : {now}\n\n"
        f"{body}"
    )


_FIBER_SUMMARY_ICON = {"overload": "🔊", "critical": "🔴", "warning": "🟡",
                       "stale": "🟣", "unknown": "⚪", "normal": "🟢"}
_FIBER_SUMMARY_RANK = {"overload": 0, "critical": 1, "warning": 2, "stale": 3}


def _fiber_summary_clean(s):
    return re.sub(r"[*_`\[\]]", "", str(s or "")).strip()[:80]


def send_fiber_summary():
    """Laporan harian fiber via Telegram (cron 08:05, setelah heartbeat host).

    Snapshot hitungan status + daftar perlu perhatian (maks 8, diurut
    overload > critical > warning > stale) + degradasi dini (maks 5).
    Baris mute/maintenance dihitung tapi tak masuk daftar perhatian.
    Tanpa ONT terdaftar -> diam (tak ada yang dilaporkan).
    """
    try:
        conn, c = get_db()
        try:
            c.execute("SELECT * FROM fiber_onts ORDER BY id ASC")
            rows = [dict(r) for r in c.fetchall()]
        finally:
            conn.close()
    except Exception as e:
        print(f"[FIBER-SUMMARY] load gagal: {e}")
        return
    if not rows:
        return
    maint_map = get_active_maintenance_map()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    counts = {"total": len(rows), "normal": 0, "warning": 0, "critical": 0,
              "overload": 0, "stale": 0, "unknown": 0,
              "degrading": 0, "muted": 0, "maintenance": 0, "flapping": 0}
    attention, degrading, flapping = [], [], []
    _pbuckets = {}
    for o in rows:
        status, severity, advice, _need, _stale, _age = _fiber_row_status(o)
        if status in counts:
            counts[status] += 1
        muted = _is_mute_active(o)
        if muted:
            counts["muted"] += 1
        in_maint = _fiber_maintenance_info(o, maint_map)[0]
        if in_maint:
            counts["maintenance"] += 1
        if muted or in_maint:
            continue
        _pkey = _fiber_oltkey(o)
        _parent = fiber_parent_down.get(_pkey) if _pkey else None
        if _parent:
            if severity:
                _b = _pbuckets.setdefault(_pkey, {"ent": _parent, "n": 0, "crit": 0})
                _b["n"] += 1
                if status in ("critical", "overload"):
                    _b["crit"] += 1
            continue
        if severity:
            try:
                rx_sort = float(o["rx_power"]) if o.get("rx_power") is not None else 99.0
            except (ValueError, TypeError):
                rx_sort = 99.0
            attention.append((_FIBER_SUMMARY_RANK.get(status, 9), rx_sort, o, status))
            continue
        dg = fiber_degrade_memory.get(o["id"]) or {}
        if dg.get("degrading"):
            counts["degrading"] += 1
            degrading.append((o, dg.get("drop_db")))
        fl = fiber_flap_memory.get(o["id"]) or {}
        if fl.get("flapping"):
            counts["flapping"] += 1
            flapping.append((o, fl.get("flips")))
    attention.sort(key=lambda t: (t[0], t[1], t[2]["id"]))
    top = attention[:8]
    rest = len(attention) - len(top)
    lines = []
    for _pkey, _pb in _pbuckets.items():
        if not _pb["n"]:
            continue
        _nm = ((_pb["ent"] or {}).get("name") or _pkey)
        lines.append(f"🔌 `{_nm}` — induk bermasalah, {_pb['n']} ONT "
                     f"({_pb['crit']} kritis/overload) disuppress")
    for _rank, _rx, o, status in top:
        icon = _FIBER_SUMMARY_ICON.get(status, "•")
        cust = _fiber_summary_clean(o.get("customer"))
        loc = "/".join([x for x in (o.get("olt_name"), o.get("odp_name")) if x])
        rx_txt = f"{o['rx_power']} dBm" if o.get("rx_power") is not None else "—"
        extra = f" ({cust})" if cust else ""
        loc_txt = f" [{_fiber_summary_clean(loc)}]" if loc else ""
        lines.append(f"{icon} `{o['ont_sn']}`{extra} — {status.upper()} Rx {rx_txt}{loc_txt}")
    if rest > 0:
        lines.append(f"  … +{rest} lainnya — lihat dashboard /triggers?cat=fiber")
    for o, drop in degrading[:5]:
        cust = _fiber_summary_clean(o.get("customer"))
        extra = f" ({cust})" if cust else ""
        lines.append(f"📉 `{o['ont_sn']}`{extra} — turun {drop} dB, cek sebelum kritis")
    _listed = {o["id"] for o, _ in degrading[:5]}
    try:
        _fth, _fh = _fiber_flap_settings()
    except Exception:
        _fth, _fh = FIBER_FLAP_FLIPS, FIBER_FLAP_HOURS
    for o, flips in flapping[:3]:
        if o["id"] in _listed:
            continue
        cust = _fiber_summary_clean(o.get("customer"))
        extra = f" ({cust})" if cust else ""
        lines.append(f"↔ `{o['ont_sn']}`{extra} — flapping {flips}x/{_fh} jam, cek konektor/ODP")
    if not lines:
        lines.append("✅ Semua ONT terpantau normal.")
    try:
        _dth, _dd = _fiber_degrade_settings()
    except Exception:
        _dth, _dd = FIBER_DEGRADE_DB, FIBER_DEGRADE_DAYS
    send_telegram_alert(
        f"📊 *Laporan Harian Fiber (snapshot)*\n"
        f"Waktu : {now}\n"
        f"Total {counts['total']} ONT — "
        f"🟢{counts['normal']} 🟡{counts['warning']} 🔴{counts['critical']} "
        f"🔊{counts['overload']} 🟣{counts['stale']} "
        f"📉degradasi {counts['degrading']} ↔flap {counts['flapping']} 🔇mute {counts['muted']}\n\n"
        + "\n".join(lines)
    )


_PING_TARGET_RE = re.compile(r"^[a-zA-Z0-9]([a-zA-Z0-9.\-]{0,253}[a-zA-Z0-9])?$")
_PING_RTT_RES = (
    re.compile(r"rtt min/avg/max/mdev = [\d.]+/([\d.]+)/"),
    re.compile(r"round-trip min/avg/max(?:/stddev)? = [\d.]+/([\d.]+)/"),
)

def ping_host(host):
    host = (host or "").strip()


    if not host or host.startswith("-") or not _PING_TARGET_RE.match(host):
        return -1, 100.0
    try:
        result = subprocess.run(
            ["ping", "-c", "3", "-W", "2", "-i", "0.5", host],
            capture_output=True, text=True, timeout=12
        )
        out = result.stdout or ""
        loss_match = re.search(r"(\d+(?:\.\d+)?)%\s*(?:packet\s+)?loss", out, re.IGNORECASE)
        loss_pct   = float(loss_match.group(1)) if loss_match else 100.0

        avg = None
        for rx in _PING_RTT_RES:
            m = rx.search(out)
            if m:
                avg = float(m.group(1))
                break
        if avg is not None and loss_pct < 100:
            return avg, loss_pct
        return -1, 100.0
    except Exception:
        return -1, 100.0

def check_host(host):
    latency, packet_loss = ping_host(host)
    return host, latency, packet_loss
def check_agent_heartbeat():
    global agent_offline_memory
    targets = get_target_hosts()

    conn, c = get_db()
    try:
        last_map = {}
        for host in targets:


            c.execute('''
                SELECT timestamp FROM agent_metrics 
                WHERE host=? AND cpu_percent IS NOT NULL ORDER BY id DESC LIMIT 1
            ''', (host,))
            row = c.fetchone()
            last_map[host] = row["timestamp"] if row else None
    finally:
        conn.close()


    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    newly_offline = []
    with db_lock:
        for host in targets:
            last_ts = last_map.get(host)
            if not last_ts:
                continue
            try:
                last_time = datetime.strptime(last_ts, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
            diff = (datetime.now() - last_time).total_seconds()


            if diff > 120 and not agent_offline_memory.get(host, False):

                agent_offline_memory[host] = True
                newly_offline.append(host)
    for host in newly_offline:
        msg = f"⚠️ *AGENT OFFLINE*\nHost: `{host}`\nTidak ada laporan dari Agent selama lebih dari 2 menit.\nWaktu: {now}"
        try:
            send_telegram_alert(msg)
        except Exception as e:
            print(f"[WARN] telegram offline-alert gagal: {e}")
        try:
            log_system_event("AGENT_OFFLINE", host, "Agent berhenti merespon (Heartbeat hilang)")
        except Exception as e:
            print(f"[WARN] log AGENT_OFFLINE gagal: {e}")


snmp_state = {}

def _encode_snmp_v1_get(community, oid_list, _pdu_tag=0xa0):
    def encode_oid(oid):
        try:
            parts = [int(x) for x in str(oid).split('.') if x != ""]
        except (ValueError, TypeError):
            raise ValueError(f"OID tidak valid: {oid!r}")
        if len(parts) < 2 or parts[0] < 0 or parts[0] > 2 or parts[1] < 0 or any(p < 0 for p in parts):
            raise ValueError(f"OID tidak valid: {oid!r}")
        first = parts[0]*40 + parts[1]
        encoded = bytes([first])
        for p in parts[2:]:
            if p == 0:
                encoded += bytes([0])
            else:
                segs = []
                while p > 0:
                    segs.append(p & 0x7f)
                    p >>= 7
                segs.reverse()
                for i, s in enumerate(segs):
                    encoded += bytes([s | (0x80 if i < len(segs)-1 else 0)])
        return b'\x06' + _encode_len(len(encoded)) + encoded

    def _encode_len(n):
        if n < 0:
            raise ValueError("length negatif")
        if n < 128:
            return bytes([n])
        lb = n.to_bytes((n.bit_length() + 7) // 8, "big")
        if len(lb) > 4:
            raise ValueError("length terlalu besar")
        return bytes([0x80 | len(lb)]) + lb

    def encode_tlv(tag, value):
        return bytes([tag]) + _encode_len(len(value)) + value

    req_id = b'\x02\x01\x01'
    varbinds = b''
    for oid in oid_list:
        oid_enc = encode_oid(oid)
        varbinds += encode_tlv(0x30, oid_enc + b'\x05\x00')
    varbind_list = encode_tlv(0x30, varbinds)
    pdu = encode_tlv(_pdu_tag, req_id + b'\x02\x01\x00\x02\x01\x00' + varbind_list)
    comm_bytes = str(community or "")[:128].encode()


    _ver = 1 if os.environ.get("SNMP_VERSION", "1").strip().lower() in ("2", "2c") else 0
    version = bytes([0x02, 0x01, _ver])
    comm_tlv = encode_tlv(0x04, comm_bytes)
    return encode_tlv(0x30, version + comm_tlv + pdu)


def _ber_read_tlv(data, pos):
    try:
        if pos + 2 > len(data):
            return None
        tag = data[pos]
        first = data[pos + 1]
        if first < 128:
            ln, hdr = first, 2
        else:
            n = first & 0x7f
            if n == 0 or n > 4 or pos + 2 + n > len(data):
                return None
            ln = int.from_bytes(data[pos + 2:pos + 2 + n], "big")
            hdr = 2 + n
        end = pos + hdr + ln
        if ln < 0 or end > len(data):
            return None
        return tag, bytes(data[pos + hdr:end]), end
    except Exception:
        return None

_SNMP_VALUE_TAGS = (0x02, 0x41, 0x42, 0x43, 0x46)

def _extract_snmp_values(resp):
    found = []

    def walk(buf):
        pos, last_was_oid = 0, False
        while pos < len(buf):
            t = _ber_read_tlv(buf, pos)
            if t is None:
                break
            tag, val, pos = t
            if tag == 0x06:
                last_was_oid = True
                continue
            if tag & 0x20:

                last_was_oid = False
                walk(val)
                continue
            if last_was_oid and tag in _SNMP_VALUE_TAGS and 1 <= len(val) <= 9:
                found.append(int.from_bytes(val, "big", signed=(tag == 0x02)))
            last_was_oid = False

    try:
        walk(bytes(resp))
    except Exception:
        pass
    return found


def _decode_oid(raw):
    """Bytes OID -> '1.3.6...'. None bila truncated/invalid."""
    try:
        raw = bytes(raw)
        if not raw:
            return None
        arcs = [raw[0] // 40, raw[0] % 40]
        val, complete = 0, True
        for byte in raw[1:]:
            val = (val << 7) | (byte & 0x7f)
            if byte & 0x80:
                complete = False
            else:
                arcs.append(val)
                val, complete = 0, True
        if not complete:
            return None
        return ".".join(str(a) for a in arcs)
    except Exception:
        return None


def _tlv_children(buf):
    pos, out = 0, []
    buf = bytes(buf)
    while pos < len(buf):
        t = _ber_read_tlv(buf, pos)
        if t is None:
            break
        out.append(t)
        pos = t[2]
    return out


def _extract_snmp_varbinds(resp):
    """Urai respons -> list (oid_str, tag, ival, sval) berurutan.

    ival untuk INTEGER/Counter/Gauge/TimeTicks (<=8 byte), sval untuk
    OCTET STRING. Tag error (noSuchObject/Instance/endOfMibView) -> nilai None.
    """
    out = []
    try:
        top = _tlv_children(resp)
        if len(top) != 1 or top[0][0] != 0x30:
            return out
        msg = _tlv_children(top[0][1])
        if len(msg) < 3:
            return out
        pdu = _tlv_children(msg[2][1])
        if len(pdu) < 4:
            return out
        for vb in _tlv_children(pdu[3][1]):
            if vb[0] != 0x30:
                continue
            parts = _tlv_children(vb[1])
            if len(parts) < 2 or parts[0][0] != 0x06:
                continue
            oid = _decode_oid(parts[0][1])
            if not oid:
                continue
            tag, raw = parts[1][0], bytes(parts[1][1])
            ival, sval = None, None
            if tag in (0x02, 0x41, 0x42, 0x43, 0x46) and 1 <= len(raw) <= 8:
                try:
                    ival = int.from_bytes(raw, "big", signed=(tag == 0x02))
                except Exception:
                    ival = None
            elif tag == 0x04 and len(raw) <= 256:
                try:
                    sval = raw.decode("utf-8", errors="replace").strip()
                except Exception:
                    sval = None
            out.append((oid, tag, ival, sval))
    except Exception:
        pass
    return out


def _oid_under(oid, base):
    base = base.strip().strip(".")
    oid = (oid or "").strip().strip(".")
    return oid == base or oid.startswith(base + ".")


def _snmp_getnext(ip, community, oid, timeout=2.5):
    """Satu GETNEXT. Kembalikan (oid_str, tag, ival, sval) atau (None, None, None, None)."""
    sock = None
    try:
        pkt = _encode_snmp_v1_get(community, [oid], _pdu_tag=0xa1)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(timeout)
        sock.sendto(pkt, (ip, 161))
        resp, _ = sock.recvfrom(8192)
        vbs = _extract_snmp_varbinds(resp)
        if vbs:
            return vbs[0]
        return None, None, None, None
    except Exception as e:
        print(f"[SNMP ERROR] {ip} getnext: {e}")
        return None, None, None, None
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass


def snmp_walk(ip, community, base, max_rows=64):
    """Walk satu kolom tabel. Kembalikan list (oid, tag, ival, sval)."""
    base = _valid_oid(base)
    if not base:
        return []
    out, cur = [], base
    seen = set()
    for _ in range(max(1, min(max_rows, 256))):
        oid, tag, ival, sval = _snmp_getnext(ip, community, cur)
        if not oid or not _oid_under(oid, base) or oid in seen:
            break
        seen.add(oid)
        if tag in (0x80, 0x81, 0x82):  # noSuch / endOfMibView
            break
        out.append((oid, tag, ival, sval))
        cur = oid
    return out


IF_DESCR_BASE = "1.3.6.1.2.1.2.2.1.2"
IF_OPER_BASE = "1.3.6.1.2.1.2.2.1.8"
DISCOVER_MAX_IF = 128  # batas interface per discover (walk berhenti sendiri di ujung tabel)


def discover_interfaces(ip, community, max_if=DISCOVER_MAX_IF):
    """Walk ifDescr + ifOperStatus. Kembalikan list {if_index, name, oper}."""
    try:
        descrs = snmp_walk(ip, community, IF_DESCR_BASE, max_rows=max_if + 8)
        opers = snmp_walk(ip, community, IF_OPER_BASE, max_rows=max_if + 8)
    except Exception as e:
        print(f"[SNMP] discover {ip} gagal: {e}")
        return []

    def _idx(oid, base):
        try:
            suffix = oid.strip().strip(".")[len(base.strip().strip(".")) + 1:]
            i = int(suffix)
            return i if i >= 1 else None
        except (ValueError, TypeError, IndexError):
            return None

    names, opermap = {}, {}
    for oid, _tag, _iv, sv in descrs:
        i = _idx(oid, IF_DESCR_BASE)
        if i is None:
            continue
        name = re.sub(r"[^\x20-\x7e]", "", sv or "")[:64] or f"if{i}"
        names[i] = name
    for oid, _tag, iv, _sv in opers:
        i = _idx(oid, IF_OPER_BASE)
        if i is None:
            continue
        opermap[i] = iv
    out = []
    for i in sorted(set(names) | set(opermap))[:max_if]:
        out.append({"if_index": i, "name": names.get(i, f"if{i}"),
                    "oper": opermap.get(i)})
    return out

_OID_RE = re.compile(r"^\.?([0-9]+\.)+[0-9]+$")


def _valid_oid(s):
    s = str(s or "").strip()
    if not s or len(s) > 128 or not _OID_RE.match(s):
        return None
    try:
        if any(int(p) > 2**32 - 1 for p in s.strip(".").split(".")):
            return None
    except ValueError:
        return None
    return s


def _snmp_get(ip, community, oids, timeout=2.0):
    """Satu request SNMP GET untuk banyak OID. Kembalikan list int/None.

    Posisi dipertahankan:(vals[i] untuk oids[i], None bila tak terjawab).
    Heuristik: bila jumlah nilai == jumlah OID, mapping posisional;
    bila kurang (error varbind), semua dianggap tak terjawab parsial -> None.
    """
    oids = [o for o in (oids or [])]
    if not oids:
        return []
    sock = None
    try:
        pkt = _encode_snmp_v1_get(community, oids)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(timeout)
        sock.sendto(pkt, (ip, 161))
        resp, _ = sock.recvfrom(8192)
        vals = _extract_snmp_values(resp)
        # hanya mapping posisional bila jumlah pas (respons error/varbind
        # parsial jumlahnya tak cocok -> anggap tak terjawab semua)
        if len(vals) == len(oids):
            return list(vals)
        return [None] * len(oids)
    except Exception as e:
        print(f"[SNMP ERROR] {ip}: {e}")
        return [None] * len(oids)
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass


def get_snmp_bandwidth(ip, community, if_index):
    try:
        if_index = int(if_index)
    except (ValueError, TypeError):
        return None, None
    if if_index < 1:
        return None, None
    # 64-bit dulu (IF-MIB HC, anti-wrap di link cepat), fallback 32-bit
    vals = _snmp_get(ip, community,
                     [f'1.3.6.1.2.1.31.1.1.1.6.{if_index}',
                      f'1.3.6.1.2.1.31.1.1.1.10.{if_index}'])
    if len(vals) == 2 and vals[0] is not None and vals[1] is not None:
        return vals[0], vals[1]
    vals = _snmp_get(ip, community,
                     [f'1.3.6.1.2.1.2.2.1.10.{if_index}',
                      f'1.3.6.1.2.1.2.2.1.16.{if_index}'])
    if len(vals) == 2 and vals[0] is not None and vals[1] is not None:
        return vals[0], vals[1]
    return None, None


MT_DEFAULT_OIDS = {
    "cpu": '1.3.6.1.4.1.14988.1.1.3.11.0',
    "mem": '1.3.6.1.4.1.14988.1.1.3.12.0',
    "storage": '1.3.6.1.4.1.14988.1.1.3.13.0',
    "temp": '1.3.6.1.4.1.14988.1.1.3.10.0',
}


def resolve_mt_oids(host_row):
    """OID per host (override) atau default global. Kembalikan dict key->oid/None."""
    out = {}
    for key, setting_key in (("cpu", "mt_cpu_oid"), ("mem", "mt_mem_oid"),
                             ("storage", "mt_storage_oid"), ("temp", "mt_temp_oid")):
        try:
            custom = (host_row[key + "_oid"] or "").strip()
        except (KeyError, IndexError, TypeError, AttributeError):
            custom = ""
        out[key] = _valid_oid(custom) or _valid_oid(
            get_setting(setting_key, MT_DEFAULT_OIDS[key], type_cast=str)) or MT_DEFAULT_OIDS[key]
    return out


def get_mikrotik_health(ip, community, oids):
    """Ambil CPU/mem/storage (%) + suhu mentah via SNMP.

    Kembalikan dict {cpu, mem, storage, temp_raw} (None bila tak terjawab).
    """
    keys = [k for k in ("cpu", "mem", "storage", "temp") if oids.get(k)]
    if not keys:
        return {"cpu": None, "mem": None, "storage": None, "temp_raw": None}
    vals = _snmp_get(ip, community, [oids[k] for k in keys])
    if len(vals) != len(keys):
        vals = [None] * len(keys)
    res = {"cpu": None, "mem": None, "storage": None, "temp_raw": None}
    for k, v in zip(keys, vals):
        if v is None:
            continue
        try:
            f = float(v)
        except (ValueError, TypeError):
            continue
        if k == "temp":
            res["temp_raw"] = f
        elif 0 <= f <= 100:
            res[k] = f
    return res

def poll_snmp_bandwidth():
    global snmp_state

    conn, c = get_db()
    try:
        c.execute("SELECT ip, snmp_community, if_index FROM hosts ORDER BY id ASC")
        hosts = c.fetchall()
        hosts = [(r["ip"], r["snmp_community"], r["if_index"]) for r in hosts]
    finally:
        conn.close()

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    now_time = time.time()


    pending_inserts = []
    for host, community, if_index in hosts:
        community = community or ""
        if_index = if_index or 1

        if not community or community.strip() == "":
            continue

        in_bytes, out_bytes = get_snmp_bandwidth(host, community, if_index)
        if in_bytes is not None and out_bytes is not None:
            if host in snmp_state:
                prev = snmp_state[host]
                time_diff = now_time - prev['time']

                diff_in = in_bytes - prev['in_bytes']
                diff_out = out_bytes - prev['out_bytes']


                if diff_in < 0 or diff_out < 0:
                    snmp_state[host] = {
                        'in_bytes': in_bytes,
                        'out_bytes': out_bytes,
                        'time': now_time
                    }
                    continue


                if time_diff > 0:
                    net_in = (diff_in * 8) / (1024 * 1024 * time_diff)
                    net_out = (diff_out * 8) / (1024 * 1024 * time_diff)
                    if net_in > 100000 or net_out > 100000:
                        snmp_state[host] = {
                            'in_bytes': in_bytes,
                            'out_bytes': out_bytes,
                            'time': now_time
                        }
                        continue
                else:
                    net_in, net_out = 0.0, 0.0


                pending_inserts.append(
                    (host, None, None, None, round(net_in, 2), round(net_out, 2), timestamp, 'snmp')
                )

            snmp_state[host] = {
                'in_bytes': in_bytes,
                'out_bytes': out_bytes,
                'time': now_time
            }


    if not pending_inserts:
        return
    with db_lock:
        conn, c = get_db()
        try:
            c.executemany(
                "INSERT INTO agent_metrics (host, cpu_percent, ram_percent, disk_percent, net_in, net_out, timestamp, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                pending_inserts,
            )
            _commit_with_retry(conn)
        except sqlite3.OperationalError as e:
            print(f"[DB LOCK] poll_snmp_bandwidth gagal: {e}")
            try:
                conn.rollback()
            except Exception:
                pass
        finally:
            conn.close()

# ============================================================
# MIKROTIK HEALTH via SNMP (CPU/RAM/storage/suhu, tanpa agent)
# OID default = penomoran baru; unit lama -> override per host
# (isi dari /system/health/print oid) atau ubah default global.
# ============================================================
MT_TEMP_HYST = 2.0

mt_alarm_memory = {}
mt_is_mikrotik = {}
mt_sysup = {}
mt_iface_oper = {}
iface_state = {}
# cooldown telegram oper port + penghitung flap dalam window cooldown
mt_iface_tg = {}
mt_iface_flaps = {}
MT_IFACE_TG_COOLDOWN_S = 600


SYSUP_OID = "1.3.6.1.2.1.1.3.0"
IF_OPER_OID = "1.3.6.1.2.1.2.2.1.8"
IF_HC_IN_OID = "1.3.6.1.2.1.31.1.1.1.6"
IF_HC_OUT_OID = "1.3.6.1.2.1.31.1.1.1.10"
REBOOT_ALARM_WINDOW_S = 900  # reboot <15 mnt lalu masih tampil di triggers
# TimeTicks 32-bit melimpah tiap ~497 hari; bila uptime sebelumnya dekat
# batas dan yang baru kecil, kemungkinan wrap (bukan reboot beneran).
_MT_TICK_MAX_S = 2 ** 32 / 100.0
_MT_WRAP_NEAR_S = 30 * 86400
_MT_WRAP_FRESH_S = 86400
# Stale: tanpa device_health baru lebih lama dari ini -> tak terpantau.
MT_STALE_MIN = 15


def _mt_reboot_kind(prev_up, sysup):
    """Klasifikasi penurunan sysUpTime: 'reboot' | 'wrap' | None."""
    try:
        prev = float(prev_up)
        cur = float(sysup)
    except (ValueError, TypeError):
        return None
    if cur >= prev:
        return None
    if prev > _MT_TICK_MAX_S - _MT_WRAP_NEAR_S and cur < _MT_WRAP_FRESH_S:
        return "wrap"
    return "reboot"


def _mt_stale_threshold():
    try:
        m = int(float(get_setting("mt_stale_min", MT_STALE_MIN)))
    except (ValueError, TypeError):
        m = MT_STALE_MIN
    return min(1440, max(3, m))


def _mt_stale_info(last_seen, now=None):
    """(is_stale, age_txt) bila device_health terakhir melewati ambang."""
    if not last_seen:
        return False, None
    try:
        seen = datetime.strptime(str(last_seen).strip(), "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return False, None
    now = now or datetime.now()
    age_min = (now - seen).total_seconds() / 60.0
    if age_min < 0:
        return False, None
    if age_min >= _mt_stale_threshold():
        return True, _fmt_age(age_min)
    return False, None


def _mt_check_one(args):
    host, community, oids = args
    try:
        res = get_mikrotik_health(host, community, oids)
    except Exception as e:
        print(f"[MT] {host}: {e}")
        res = {"cpu": None, "mem": None, "storage": None, "temp_raw": None}
    sysup = None
    try:
        vals = _snmp_get(host, community, [SYSUP_OID], timeout=2.0)
        if vals and vals[0] is not None and vals[0] >= 0:
            sysup = round(vals[0] / 100.0, 1)
    except Exception as e:
        print(f"[MT] {host} sysup: {e}")
    return host, res, sysup


def poll_mikrotik_health():
    global mt_alarm_memory, mt_is_mikrotik
    try:
        conn, c = get_db()
        try:
            c.execute("SELECT ip, snmp_community, snmp_profile, cpu_oid, mem_oid,"
                      " storage_oid, temp_oid FROM hosts ORDER BY id ASC")
            hosts = [dict(r) for r in c.fetchall()]
        finally:
            conn.close()
    except Exception as e:
        print(f"[MT] load hosts gagal: {e}")
        return
    cands = []
    for h in hosts:
        if not (h.get("snmp_community") or "").strip():
            continue
        if (h.get("snmp_profile") or "auto") == "generic":
            mt_is_mikrotik.pop(h["ip"], None)
            continue
        cands.append((h["ip"], h["snmp_community"], resolve_mt_oids(h)))
    if not cands:
        return

    results = {}
    with ThreadPoolExecutor(max_workers=min(len(cands), 10)) as ex:
        for host, res, sysup in ex.map(_mt_check_one, cands):
            results[host] = (res, sysup)

    try:
        temp_warn = get_setting("temp_threshold", 60.0)
        temp_crit = get_setting("temp_crit", 75.0)
        cpu_thresh = get_setting("cpu_threshold", 85.0)
        mem_thresh = get_setting("ram_threshold", 90.0)
        st_thresh = get_setting("disk_threshold", 90.0)
        temp_div = get_setting("mt_temp_div", 10, type_cast=float) or 10
    except Exception:
        temp_warn, temp_crit, cpu_thresh, mem_thresh, st_thresh, temp_div = \
            60.0, 75.0, 85.0, 90.0, 90.0, 10
    cpu_clear = max(cpu_thresh - HYSTERESIS, 0)
    mem_clear = max(mem_thresh - HYSTERESIS, 0)
    st_clear = max(st_thresh - HYSTERESIS, 0)

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    tg_queue = []
    with db_lock:
        conn, c = get_db()
        try:
            for host, (res, sysup) in results.items():
                cpu, mem, sto = res.get("cpu"), res.get("mem"), res.get("storage")
                temp = (res["temp_raw"] / temp_div
                        if res.get("temp_raw") is not None and temp_div else None)
                if temp is not None:
                    temp = round(temp, 1)
                if cpu is None and mem is None and sto is None and temp is None:
                    mt_is_mikrotik.pop(host, None)
                    continue
                mt_is_mikrotik[host] = True
                # deteksi reboot: sysUpTime turun dari poll sebelumnya
                prev_up = mt_sysup.get(host)
                if sysup is not None:
                    _kind = _mt_reboot_kind(prev_up, sysup) if prev_up is not None else None
                    if _kind:
                        if _kind == "wrap":
                            tg_queue.append(
                                f"ℹ️ *MIKROTIK UPTIME WRAP?*\nHost: `{host}`\n"
                                f"Uptime sebelumnya: {_fmt_duration(int(prev_up or 0))} → sekarang: {_fmt_duration(int(sysup or 0))}\n"
                                f"Kemungkinan wrap counter TimeTicks (>467 hari), bukan reboot beneran — verifikasi uptime.\n"
                                f"Waktu: {timestamp}")
                            try:
                                _insert_system_log(c, "MT_REBOOT", host,
                                                   f"kemungkinan wrap (uptime {int(prev_up or 0)}s -> {int(sysup or 0)}s)", timestamp)
                            except Exception:
                                pass
                        else:
                            tg_queue.append(
                                f"🔄 *MIKROTIK REBOOT TERDETEKSI*\nHost: `{host}`\n"
                                f"Uptime sebelumnya: {_fmt_duration(int(prev_up or 0))} → sekarang: {_fmt_duration(int(sysup or 0))}\n"
                                f"Waktu: {timestamp}")
                            try:
                                _insert_system_log(c, "MT_REBOOT", host,
                                                   f"reboot (uptime {int(prev_up or 0)}s -> {int(sysup or 0)}s)", timestamp)
                            except Exception:
                                pass
                    mt_sysup[host] = sysup
                try:
                    c.execute("INSERT INTO device_health (host, cpu, mem_used, storage_used,"
                              " temp_c, uptime_s, timestamp, source) VALUES (?,?,?,?,?,?,?,'snmp-mikrotik')",
                              (host, cpu, mem, sto, temp, sysup, timestamp))
                except sqlite3.OperationalError as e:
                    print(f"[DB LOCK] device_health {host} gagal: {e}")
                    continue

                prev = mt_alarm_memory.get(host) or {}
                cur = dict(prev)

                def _flip(key, is_bad, is_clear, name, unit, val, thresh, sev):
                    if val is None:
                        return
                    if is_bad and not prev.get(key):
                        cur[key] = True
                        tg_queue.append(
                            f"⚠️ *MIKROTIK {name} TINGGI*\nHost: `{host}`\n"
                            f"{name}: *{val}{unit}* (batas: {thresh}{unit})\nWaktu: {timestamp}")
                        log_queue.append((f"MT_{key.upper()}", f"{val}{unit} (batas {thresh}{unit})"))
                    elif is_clear and prev.get(key):
                        cur[key] = False
                        tg_queue.append(
                            f"✅ *MIKROTIK {name} NORMAL*\nHost: `{host}`\n"
                            f"{name}: {val}{unit}\nWaktu: {timestamp}")
                        log_queue.append((f"MT_{key.upper()}_OK", f"pulih: {val}{unit}"))

                log_queue = []
                _flip("cpu", cpu is not None and cpu > cpu_thresh,
                      cpu is not None and cpu <= cpu_clear,
                      "CPU", "%", cpu, cpu_thresh, "warning")
                _flip("mem", mem is not None and mem > mem_thresh,
                      mem is not None and mem <= mem_clear,
                      "Memory", "%", mem, mem_thresh, "warning")
                _flip("storage", sto is not None and sto > st_thresh,
                      sto is not None and sto <= st_clear,
                      "Storage", "%", sto, st_thresh, "warning")
                _flip("temp_warn", temp is not None and temp >= temp_warn,
                      temp is not None and temp <= temp_warn - MT_TEMP_HYST,
                      "Suhu", "°C", temp, temp_warn, "warning")
                _flip("temp_crit", temp is not None and temp >= temp_crit,
                      temp is not None and temp <= temp_crit - MT_TEMP_HYST,
                      "Suhu KRITIS", "°C", temp, temp_crit, "high")
                mt_alarm_memory[host] = cur
                for ev, msg in log_queue:
                    try:
                        _insert_system_log(c, ev, host, msg, timestamp)
                    except Exception:
                        pass
            _commit_with_retry(conn)
        except sqlite3.OperationalError as e:
            print(f"[DB LOCK] poll_mikrotik gagal: {e}")
            try:
                conn.rollback()
            except Exception:
                pass
        finally:
            conn.close()
    for msg in tg_queue:
        try:
            send_telegram_alert(msg)
        except Exception as e:
            print(f"[WARN] telegram mikrotik gagal: {e}")


def _mt_iface_check_one(args):
    """Poll satu host: oper-status + HC counters semua interface termonitor."""
    host, community, indices = args
    out = {"opers": {}, "counters": {}}
    try:
        if indices:
            vals = _snmp_get(host, community,
                             [f"{IF_OPER_OID}.{i}" for i in indices], timeout=3.0)
            for i, v in zip(indices, vals):
                out["opers"][i] = v
            oids, order = [], []
            for i in indices:
                oids += [f"{IF_HC_IN_OID}.{i}", f"{IF_HC_OUT_OID}.{i}"]
                order += [(i, "in"), (i, "out")]
            vals = _snmp_get(host, community, oids, timeout=3.0)
            if len(vals) == len(order):
                for (i, direction), v in zip(order, vals):
                    out["counters"].setdefault(i, {})[direction] = v
    except Exception as e:
        print(f"[MT-IFACE] {host}: {e}")
    return host, out


def poll_mikrotik_ifaces():
    """Traffic + oper-status per interface termonitor (tiap 60 dtk)."""
    global mt_iface_oper, iface_state, mt_iface_tg, mt_iface_flaps
    try:
        conn, c = get_db()
        try:
            c.execute("SELECT h.ip, h.snmp_community, h.snmp_profile,"
                      " i.if_index, i.name FROM snmp_interfaces i"
                      " JOIN hosts h ON h.ip = i.host"
                      " WHERE i.monitor=1 ORDER BY h.ip ASC, i.if_index ASC")
            rows = [dict(r) for r in c.fetchall()]
        finally:
            conn.close()
    except Exception as e:
        print(f"[MT-IFACE] load gagal: {e}")
        return
    by_host = {}
    for r in rows:
        if not (r.get("snmp_community") or "").strip():
            continue
        if (r.get("snmp_profile") or "auto") == "generic":
            continue
        try:
            idx = int(r["if_index"])
        except (ValueError, TypeError):
            continue
        by_host.setdefault(r["ip"], {"community": r["snmp_community"],
                                     "indices": [], "names": {}})
        by_host[r["ip"]]["indices"].append(idx)
        by_host[r["ip"]]["names"][idx] = r["name"] or f"if{idx}"
    if not by_host:
        return

    results = {}
    with ThreadPoolExecutor(max_workers=min(len(by_host), 10)) as ex:
        futs = {ex.submit(_mt_iface_check_one,
                          (h, v["community"], v["indices"])): h for h, v in by_host.items()}
        for fut in as_completed(futs):
            try:
                host, out = fut.result()
                results[host] = out
            except Exception as e:
                print(f"[MT-IFACE] {futs.get(fut)} gagal: {e}")

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    now_time = time.time()
    tg_queue, traffic_rows = [], []
    with db_lock:
        conn, c = get_db()
        try:
            for host, out in results.items():
                names = by_host[host]["names"]
                for idx in by_host[host]["indices"]:
                    name = names.get(idx, f"if{idx}")
                    oper = out["opers"].get(idx)
                    key = (host, idx)
                    prev_oper = mt_iface_oper.get(key)
                    if oper in (1, 2):
                        if prev_oper is None:
                            mt_iface_oper[key] = oper
                        elif oper != prev_oper:
                            mt_iface_oper[key] = oper
                            try:
                                c.execute("UPDATE snmp_interfaces SET oper=?, last_changed=?"
                                          " WHERE host=? AND if_index=?",
                                          (oper, timestamp, host, idx))
                            except sqlite3.OperationalError:
                                pass
                            # cooldown anti-spam port flapping (DB oper + log tetap dicatat)
                            _last_tg = mt_iface_tg.get(key, 0)
                            if now_time - _last_tg < MT_IFACE_TG_COOLDOWN_S:
                                _n = mt_iface_flaps.get(key, 0) + 1
                                mt_iface_flaps[key] = _n
                                print(f"[MT-IFACE] {host} if{idx} "
                                      f"{'down' if oper == 2 else 'up'} disuppress "
                                      f"(cooldown, flap x{_n})")
                                try:
                                    _insert_system_log(
                                        c, "MT_PORT_DOWN" if oper == 2 else "MT_PORT_UP",
                                        host, f"{name} (ifIndex {idx}) "
                                        f"{'down' if oper == 2 else 'up'} (telegram disuppress, flap x{_n})",
                                        timestamp)
                                except Exception:
                                    pass
                            else:
                                mt_iface_tg[key] = now_time
                                _flaps = mt_iface_flaps.pop(key, 0)
                                _note = f" (flapping {_flaps}x/10 mnt)" if _flaps else ""
                                if oper == 2:
                                    tg_queue.append(
                                        f"🔌 *MIKROTIK PORT DOWN*\nHost: `{host}`\n"
                                        f"Port: *{name}* (ifIndex {idx}){_note}\nWaktu: {timestamp}")
                                    try:
                                        _insert_system_log(c, "MT_PORT_DOWN", host,
                                                           f"{name} (ifIndex {idx}) down{_note}", timestamp)
                                    except Exception:
                                        pass
                                else:
                                    tg_queue.append(
                                        f"✅ *MIKROTIK PORT UP*\nHost: `{host}`\n"
                                        f"Port: *{name}* (ifIndex {idx}){_note}\nWaktu: {timestamp}")
                                    try:
                                        _insert_system_log(c, "MT_PORT_UP", host,
                                                           f"{name} (ifIndex {idx}) up{_note}", timestamp)
                                    except Exception:
                                        pass
                    cnt = out["counters"].get(idx, {})
                    in_b, out_b = cnt.get("in"), cnt.get("out")
                    if in_b is not None and out_b is not None:
                        st = iface_state.get(key)
                        if st is None:
                            iface_state[key] = {"in": in_b, "out": out_b, "time": now_time}
                        else:
                            dt = now_time - st["time"]
                            di, do = in_b - st["in"], out_b - st["out"]
                            if di < 0 or do < 0 or dt <= 0:
                                iface_state[key] = {"in": in_b, "out": out_b, "time": now_time}
                            else:
                                net_in = round((di * 8) / (1024 * 1024 * dt), 2)
                                net_out = round((do * 8) / (1024 * 1024 * dt), 2)
                                if net_in <= 100000 and net_out <= 100000:
                                    traffic_rows.append((host, idx, net_in, net_out, timestamp))
                                iface_state[key] = {"in": in_b, "out": out_b, "time": now_time}
            if traffic_rows:
                try:
                    c.executemany("INSERT INTO iface_traffic (host, if_index, net_in, net_out, timestamp)"
                                  " VALUES (?,?,?,?,?)", traffic_rows)
                except sqlite3.OperationalError as e:
                    print(f"[DB LOCK] iface_traffic gagal: {e}")
            _commit_with_retry(conn)
        except sqlite3.OperationalError as e:
            print(f"[DB LOCK] poll_mikrotik_ifaces gagal: {e}")
            try:
                conn.rollback()
            except Exception:
                pass
        finally:
            conn.close()
    for msg in tg_queue:
        try:
            send_telegram_alert(msg)
        except Exception as e:
            print(f"[WARN] telegram mikrotik-iface gagal: {e}")


# ---------------- BACKUP KONFIGURASI MIKROTIK via SSH ----------------
# Opt-in per host (backup_enable=1 + ssh_user/pass). Scheduler tiap 02:00
# + tombol manual. Baseline pertama sunyi; perubahan -> telegram + diff.
# Password tak pernah dikirim ke UI/API (ikuti konvensi community OLT).
MT_BACKUP_MAX_BYTES = 2 * 1024 * 1024


def _mt_backup_keep():
    try:
        k = int(float(get_setting("mt_backup_keep", 10)))
    except (ValueError, TypeError):
        k = 10
    return min(50, max(3, k))


def fetch_mikrotik_config(host, username, password, port=22, timeout=20):
    """Ambil /export via SSH. Kembalikan (ok, text_atau_error)."""
    try:
        import paramiko
    except ImportError:
        return False, "paramiko belum terinstal di server"
    if not (username or "").strip() or not (password or ""):
        return False, "SSH user/password belum diisi"
    try:
        port = int(port or 22)
    except (ValueError, TypeError):
        return False, "SSH port tidak valid"
    if not 1 <= port <= 65535:
        return False, "SSH port harus 1-65535"
    client = None
    try:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(host, port=port, username=username, password=password,
                       timeout=timeout, banner_timeout=timeout,
                       auth_timeout=timeout, look_for_keys=False,
                       allow_agent=False)
        _in, _out, _err = client.exec_command("/export", timeout=timeout)
        raw = _out.read()
        try:
            text = raw.decode("utf-8-sig").strip()
        except Exception:
            text = raw.decode("utf-8", errors="replace").strip()
        if not text:
            return False, "export kosong"
        return True, text
    except Exception as e:
        return False, f"SSH gagal: {e}"[:300]
    finally:
        try:
            if client is not None:
                client.close()
        except Exception:
            pass


def _store_mt_backup(c, host, ok, payload, timestamp):
    """Simpan hasil fetch (cursor milik transaksi caller yang pegang db_lock).

    Kembalikan pesan telegram|None. Baseline pertama & kegagalan tak
    bertelegram (kegagalan tampil di triggers + status host).
    """
    if not ok:
        try:
            c.execute("UPDATE hosts SET backup_last=?, backup_ok=0 WHERE ip=?",
                      (timestamp, host))
            _insert_system_log(c, "MT_BACKUP_FAIL", host, str(payload)[:200],
                               timestamp)
        except sqlite3.OperationalError:
            pass
        return None
    text = payload if isinstance(payload, str) else ""
    if len(text.encode("utf-8")) > MT_BACKUP_MAX_BYTES:
        try:
            c.execute("UPDATE hosts SET backup_last=?, backup_ok=0 WHERE ip=?",
                      (timestamp, host))
            _insert_system_log(c, "MT_BACKUP_FAIL", host,
                               "export >2MB, dilewati", timestamp)
        except sqlite3.OperationalError:
            pass
        return None
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    try:
        c.execute("SELECT id, sha256, content FROM mt_backups WHERE host=? "
                  "ORDER BY id DESC LIMIT 1", (host,))
        prev = c.fetchone()
    except sqlite3.OperationalError:
        return None
    if prev and prev["sha256"] == sha:
        try:
            c.execute("UPDATE hosts SET backup_last=?, backup_ok=1 WHERE ip=?",
                      (timestamp, host))
        except sqlite3.OperationalError:
            pass
        return None
    added = removed = 0
    if prev and prev["content"] is not None:
        for line in difflib.unified_diff((prev["content"] or "").splitlines(),
                                         text.splitlines(), n=0):
            if line.startswith("+") and not line.startswith("+++"):
                added += 1
            elif line.startswith("-") and not line.startswith("---"):
                removed += 1
    try:
        c.execute("INSERT INTO mt_backups (host, taken_at, size, sha256, content, changed)"
                  " VALUES (?,?,?,?,?,?)",
                  (host, timestamp, len(text.encode("utf-8")), sha, text,
                   1 if prev else 0))
        keep = _mt_backup_keep()
        c.execute("DELETE FROM mt_backups WHERE host=? AND id NOT IN "
                  "(SELECT id FROM mt_backups WHERE host=? ORDER BY id DESC LIMIT ?)",
                  (host, host, keep))
        c.execute("UPDATE hosts SET backup_last=?, backup_ok=1 WHERE ip=?",
                  (timestamp, host))
        _insert_system_log(c, "MT_BACKUP", host,
                           f"tersimpan {len(text.encode('utf-8')) // 1024} KB"
                           + (f" (+{added}/-{removed})" if prev else " (baseline)"),
                           timestamp)
    except sqlite3.OperationalError as e:
        print(f"[MT-BACKUP] simpan {host} gagal: {e}")
        return None
    if not prev:
        return None
    return (f"💾 *MIKROTIK BACKUP BERUBAH*\nHost: `{host}`\n"
            f"Ukuran: {len(text.encode('utf-8')) // 1024} KB · +{added}/-{removed} baris\n"
            f"Waktu: {timestamp}\nLihat diff di halaman mikrotik.")


def _mt_backup_candidates():
    try:
        conn, c = get_db()
        try:
            c.execute("SELECT ip, ssh_user, ssh_pass, ssh_port FROM hosts "
                      "WHERE backup_enable=1")
            return [dict(r) for r in c.fetchall()]
        finally:
            conn.close()
    except Exception as e:
        print(f"[MT-BACKUP] load kandidat gagal: {e}")
        return []


def poll_mt_backups():
    """Job scheduler: backup konfigurasi semua host opt-in (tiap 02:00)."""
    cands = _mt_backup_candidates()
    if not cands:
        return
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _one(h):
        try:
            return h["ip"], fetch_mikrotik_config(
                h["ip"], h.get("ssh_user") or "", h.get("ssh_pass") or "",
                h.get("ssh_port") or 22)
        except Exception as e:
            return h["ip"], (False, f"fetch gagal: {e}"[:200])

    results = {}
    with ThreadPoolExecutor(max_workers=min(len(cands), 5)) as ex:
        for host, res in ex.map(_one, cands):
            results[host] = res
    tg_queue = []
    with db_lock:
        conn, c = get_db()
        try:
            for host, (ok, payload) in results.items():
                try:
                    msg = _store_mt_backup(c, host, ok, payload, timestamp)
                except Exception as e:
                    print(f"[MT-BACKUP] {host} gagal: {e}")
                    continue
                if msg:
                    tg_queue.append(msg)
            _commit_with_retry(conn)
        except sqlite3.OperationalError as e:
            print(f"[DB LOCK] poll_mt_backups gagal: {e}")
            try:
                conn.rollback()
            except Exception:
                pass
        finally:
            conn.close()
    for msg in tg_queue:
        try:
            send_telegram_alert(msg)
        except Exception as e:
            print(f"[WARN] telegram backup gagal: {e}")


def check_network():
    global status_memory, down_since
    targets = get_target_hosts()
    if not targets:
        return

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")


    for t in targets:
        if t not in status_memory:
            status_memory[t] = False


    max_workers = min(len(targets), 20)
    results = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {executor.submit(check_host, h): h for h in targets}
        for future in as_completed(future_map):
            try:
                host, latency, packet_loss = future.result()
                results[host] = (latency, packet_loss)
            except Exception as e:
                h = future_map.get(future, "?")
                print(f"[WARN] ping {h} gagal: {e}")


    telegram_queue = []
    maint_map = get_active_maintenance_map()
    with db_lock:
        conn, c = get_db()
        try:
            for host in targets:
                if host not in results:
                    continue
                try:
                    latency, packet_loss = results[host]
                    host_is_down = (latency == -1)
                    was_down = status_memory.get(host, False)
                    in_maint = host in maint_map

                    if host_is_down and not was_down:

                        status_memory[host] = True
                        down_since[host]    = datetime.now()
                        c.execute(
                            "SELECT 1 FROM down_events WHERE host=? AND resolved_at IS NULL LIMIT 1",
                            (host,),
                        )
                        if c.fetchone() is None:
                            c.execute(
                                "INSERT INTO down_events (host, started_at, is_maintenance) VALUES (?, ?, ?)",
                                (host, timestamp, 1 if in_maint else 0)
                            )
                        if in_maint:
                            reason = (maint_map[host].get("reason") or "").strip()[:200]
                            _insert_system_log(c, "MAINTENANCE_DOWN", host,
                                               f"DOWN dalam maintenance{(' - ' + reason) if reason else ''}", timestamp)
                            print(f"[MAINT] Telegram DOWN {host} disuppress (maintenance)")
                        else:
                            _insert_system_log(c, "NETWORK_DOWN", host, "Ping timeout/RTO", timestamp)


                            now_dt = datetime.now()
                            last_tg = last_down_telegram.get(host)
                            if last_tg is None or (now_dt - last_tg).total_seconds() >= DOWN_COOLDOWN_S:
                                last_down_telegram[host] = now_dt
                                # korelasi induk fiber: OLT ber-IP ini -> alarm ONT disuppress
                                _aff, _onames = _fiber_parent_register(host, timestamp, c)
                                _impact = (f"\nOLT: `{', '.join(_onames)}` "
                                           f"(~{_aff} ONT, alarm ONT disuppress)") if _aff else ""
                                telegram_queue.append(
                                    f"🚨 *ALARM!*\nHost   : `{host}`\nStatus : *DOWN*\nWaktu  : {timestamp}{_impact}"
                                )
                            else:
                                print(f"[COOLDOWN] Telegram DOWN {host} ditahan (flapping?)")

                    elif not host_is_down and was_down:

                        status_memory[host] = False
                        duration_str = ""
                        duration_s   = None
                        was_maint_event = False
                        if host in down_since:
                            delta      = datetime.now() - down_since.pop(host)
                            duration_s = int(delta.total_seconds())
                        else:


                            try:
                                c.execute("SELECT started_at FROM down_events WHERE host=? AND resolved_at IS NULL ORDER BY id DESC LIMIT 1",
                                          (host,))
                                orow = c.fetchone()
                                if orow and orow["started_at"]:
                                    started = datetime.strptime(orow["started_at"], "%Y-%m-%d %H:%M:%S")
                                    duration_s = max(0, int((datetime.now() - started).total_seconds()))
                            except Exception:
                                pass
                        try:
                            c.execute("SELECT is_maintenance FROM down_events WHERE host=? AND resolved_at IS NULL ORDER BY id DESC LIMIT 1",
                                      (host,))
                            mrow = c.fetchone()
                            was_maint_event = bool(mrow and mrow["is_maintenance"])
                        except Exception:
                            was_maint_event = False
                        if duration_s is not None:
                            m, s       = divmod(duration_s, 60)
                            duration_str = f"\nDurasi DOWN : {m} menit {s} detik"
                        c.execute(
                            "UPDATE down_events SET resolved_at=?, duration_s=? WHERE host=? AND resolved_at IS NULL",
                            (timestamp, duration_s, host)
                        )
                        if in_maint or was_maint_event:
                            _insert_system_log(c, "MAINTENANCE_UP", host,
                                               f"Pulih dalam maintenance{ duration_str.replace(chr(10), '')}", timestamp)
                            print(f"[MAINT] Telegram PULIH {host} disuppress (maintenance)")
                        else:
                            _insert_system_log(c, "NETWORK_UP", host,
                                               f"Pulih setelah {duration_str.replace(chr(10), '')}", timestamp)
                            telegram_queue.append(
                                f"✅ *PULIH!*\nHost    : `{host}`\nLatency : {latency:.2f} ms\nLoss    : {packet_loss:.0f}%{duration_str}"
                            )
                            # induk ping pulih -> lepas supresi fiber OLT ini
                            try:
                                for _k in [k for k, v in list(fiber_parent_down.items())
                                           if not (v or {}).get("synthetic")
                                           and (v or {}).get("ip") == host]:
                                    fiber_parent_down.pop(_k, None)
                            except Exception:
                                pass


                    c.execute(
                        "INSERT INTO ping_logs (host, latency, packet_loss, timestamp) VALUES (?, ?, ?, ?)",
                        (host, latency, packet_loss, timestamp),
                    )
                    label = f"{latency:.2f} ms | loss {packet_loss:.0f}%" if not host_is_down else "DOWN"
                    print(f"[LOG] {timestamp} | {host:<16} | {label}")
                except Exception as e:
                    print(f"[WARN] proses hasil {host} gagal (tetap lanjut): {e}")
                    continue

            _commit_with_retry(conn)
        except sqlite3.OperationalError as e:
            print(f"[DB LOCK] check_network commit gagal: {e}")
            try:
                conn.rollback()
            except Exception:
                pass
        finally:
            conn.close()


    for msg in telegram_queue:
        try:
            send_telegram_alert(msg)
        except Exception as e:
            print(f"[WARN] telegram gagal: {e}")

def _is_url_allowed_for_monitoring(url):
    from urllib.parse import urlparse
    try:
        parsed = urlparse(url or "")
    except Exception:
        return False
    if (parsed.scheme or "").lower() not in ("http", "https"):
        return False
    if parsed.username or parsed.password:
        return False
    try:
        host_part = (parsed.hostname or "").lower()
    except Exception:
        return False
    if not host_part:
        return False
    if host_part in ("169.254.169.254", "metadata.google.internal",
                     "metadata.google.internal."):
        return False
    try:
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
        infos = socket.getaddrinfo(host_part, port, type=socket.SOCK_STREAM)
    except Exception:
        return False
    if not infos:
        return False
    try:
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if (ip.is_loopback or ip.is_link_local or ip.is_multicast
                    or ip.is_reserved or ip.is_unspecified):
                return False
    except ValueError:
        return False
    return True


def check_single_service(svc):
    svc_id, ip, name, svc_type, port, url = svc
    start_time = time.time()
    status = "OFFLINE"

    if svc_type == 'tcp':
        try:
            port_int = int(port)
            if not 1 <= port_int <= 65535:
                raise ValueError("port out of range")
            with socket.create_connection((ip, port_int), timeout=5):
                status = "ONLINE"
        except Exception:
            pass
    elif svc_type == 'http':
        try:
            if not _is_url_allowed_for_monitoring(url):
                status = "OFFLINE"
            else:
                import urllib3
                urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


                r = requests.get(url, timeout=5, verify=False,
                                 headers={"User-Agent": "NMS-KOPEGTEL/1.0"})
                if r.status_code < 400:
                    status = "ONLINE"
        except Exception:
            pass
    else:

        status = "PENDING"

    latency = (time.time() - start_time) * 1000
    if status == "OFFLINE": latency = 0
    return svc_id, status, latency

SSL_WARN_DAYS = 30
SSL_HIGH_DAYS = 7

def _ssl_level(days_left):
    if days_left is None:
        return None
    try:
        d = int(days_left)
    except (ValueError, TypeError):
        return None
    if d < 0:
        return "expired"
    if d <= SSL_HIGH_DAYS:
        return "high"
    if d <= SSL_WARN_DAYS:
        return "warning"
    return None

def get_ssl_expiry(url, timeout=5):
    from urllib.parse import urlparse
    import ssl as _ssl
    try:
        parsed = urlparse(url or "")
        if (parsed.scheme or "").lower() != "https":
            return None, None
        host = (parsed.hostname or "").strip()
        if not host:
            return None, None
        try:
            port = parsed.port or 443
        except ValueError:
            return None, None
        if not 1 <= port <= 65535:
            return None, None
        ctx = _ssl.create_default_context()
        with socket.create_connection((host, port), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as ssock:
                cert = ssock.getpeercert()
        not_after = (cert or {}).get("notAfter")
        if not not_after:
            return None, None
        exp = None
        for fmt in ("%b %d %H:%M:%S %Y %Z", "%b  %d %H:%M:%S %Y %Z"):
            try:
                exp = datetime.strptime(str(not_after), fmt)
                break
            except ValueError:
                continue
        if exp is None:
            return None, None
        days_left = (exp - datetime.now(timezone.utc).replace(tzinfo=None)).days
        return exp.strftime("%Y-%m-%d %H:%M:%S"), days_left
    except Exception as e:
        print(f"[SSL] {url}: {e}")
        return None, None

def check_ssl_expiry():
    try:
        conn, c = get_db()
        try:
            try:
                c.execute("SELECT id, ip, name, url, ssl_days_left, ssl_last_alert FROM services WHERE type='http' AND url LIKE 'https://%'")
            except sqlite3.OperationalError:
                return
            rows = [dict(r) for r in c.fetchall()]
        finally:
            conn.close()
    except Exception as e:
        print(f"[SSL] load services gagal: {e}")
        return
    if not rows:
        return
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for s in rows:
        sid = s["id"]
        url = s.get("url") or ""
        if not _is_url_allowed_for_monitoring(url):
            continue
        exp_str, days = get_ssl_expiry(url)
        if exp_str is None:
            continue
        level = _ssl_level(days)
        prev_level = (s.get("ssl_last_alert") or "") or None
        order = {"warning": 1, "high": 2, "expired": 3}
        escalated = bool(level and level != prev_level
                         and (not prev_level or order.get(level, 0) > order.get(prev_level, 0)))
        with db_lock:
            conn, c = get_db()
            try:
                try:
                    c.execute(
                        "UPDATE services SET ssl_expires_at=?, ssl_days_left=?, ssl_checked_at=?, ssl_last_alert=? WHERE id=?",
                        (exp_str, days, timestamp, level or "", sid),
                    )
                except sqlite3.OperationalError as e:
                    print(f"[DB LOCK] ssl update gagal: {e}")
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                    continue
                if level:
                    try:
                        _insert_system_log(c, "SSL_EXPIRY" if level != "expired" else "SSL_EXPIRED",
                                           s.get("ip") or "-", f"{s.get('name')} {url} sisa {days} hari (exp {exp_str})", timestamp)
                    except Exception:
                        pass
                _commit_with_retry(conn)
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
        if escalated:
            try:
                if level == "expired":
                    send_telegram_alert(
                        f"🚨 *SSL EXPIRED*\nService: `{s.get('name')}`\nURL: {url}\nExpired: {exp_str}\nWaktu: {timestamp}"
                    )
                elif level == "high":
                    send_telegram_alert(
                        f"⚠️ *SSL SEGERA EXPIRE*\nService: `{s.get('name')}`\nURL: {url}\nSisa: *{days} hari*\nExp: {exp_str}"
                    )
                else:
                    send_telegram_alert(
                        f"ℹ️ *SSL Expire H-{days}*\nService: `{s.get('name')}`\nURL: {url}\nExp: {exp_str}"
                    )
            except Exception as e:
                print(f"[WARN] telegram ssl gagal: {e}")

def trigger_async_ssl_check():
    threading.Thread(target=check_ssl_expiry, daemon=True).start()

def check_services():
    try:

        conn, c = get_db()
        try:
            c.execute("SELECT id, ip, name, type, port, url FROM services")
            services = [dict(s) for s in c.fetchall()]
        finally:
            conn.close()

        results = []
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(check_single_service, (s["id"], s["ip"], s["name"], s["type"], s["port"], s["url"])) for s in services]
            for future in as_completed(futures):
                try:
                    results.append(future.result())
                except Exception as e:
                    print(f"[WARN] check service gagal: {e}")

        if not results:
            return
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with db_lock:
            conn, c = get_db()
            try:
                for svc_id, status, latency in results:
                    c.execute("UPDATE services SET status=?, latency=?, last_checked=? WHERE id=?", (status, latency, timestamp, svc_id))
                try:
                    c.executemany(
                        "INSERT INTO service_history (service_id, status, latency, timestamp) VALUES (?, ?, ?, ?)",
                        [(sid, st, lat, timestamp) for sid, st, lat in results],
                    )
                except sqlite3.OperationalError as e:
                    print(f"[DB LOCK] service_history gagal: {e}")
                _commit_with_retry(conn)
            except sqlite3.OperationalError as e:
                print(f"[DB LOCK] check_services gagal: {e}")
                try:
                    conn.rollback()
                except Exception:
                    pass
            finally:
                conn.close()
    except Exception as e:
        print(f"Error checking services: {e}")


# ============================================================
# FIBER / REDAMAN (GPON ONT Rx/Tx power, satuan dBm)
# Standar acuan ITU-T G.984.2 Class B+:
#   Rx overload  : > -8 dBm  (terlalu kuat -> perlu PEREDAM/attenuator)
#   Rx normal    : -8 .. -25 dBm
#   Rx warning   : -25 .. -27 dBm (redaman mulai tinggi)
#   Rx critical  : < -27 dBm (redaman tinggi / fiber bermasalah)
# ============================================================
# Default bila settings belum ada (nilai sama dengan seed di init_db)
FIBER_RX_OVERLOAD = -8.0
FIBER_RX_WARN = -25.0
FIBER_RX_CRIT = -27.0
FIBER_RX_TARGET = -18.0  # target ideal untuk hitung kebutuhan peredam
FIBER_TX_MIN = 0.0
FIBER_TX_MAX = 5.0
# Deteksi degradasi bertahap: Rx turun >= FIBER_DEGRADE_DB dB dalam
# FIBER_DEGRADE_DAYS hari (butuh rentang history >= 24 jam).
FIBER_DEGRADE_DB = 3.0
FIBER_DEGRADE_DAYS = 7
FIBER_DEGRADE_MIN_SPAN_H = 24
# Batas data basi (stale): sumber otomatis (snmp/simulator) tanpa pengukuran
# baru lebih lama dari ini dianggap tak terpantau (kemungkinan LOS/ONT mati).
FIBER_STALE_MIN = 60
# Flap: status normal <-> terganggu (warning/critical/overload) bolak-balik
# >= FIBER_FLAP_FLIPS kali dalam FIBER_FLAP_HOURS jam terakhir.
FIBER_FLAP_FLIPS = 4
FIBER_FLAP_HOURS = 24

fiber_alarm_memory = {}
fiber_degrade_memory = {}
fiber_flap_memory = {}
# cooldown notifikasi degradasi per ONT (epoch detik telegram terakhir)
fiber_degrade_tg = {}
FIBER_DEGRADE_TG_COOLDOWN_S = 86400
# Korelasi induk: oltkey(lower) -> {ip|None, since, synthetic, count}.
# Entri ping = IP OLT sedang DOWN; sintetik = >=min ONT kritis/overload.
fiber_parent_down = {}
FIBER_PARENT_MIN = 5


def _fiber_parent_min():
    try:
        m = int(float(get_setting("fiber_parent_min", FIBER_PARENT_MIN)))
    except (ValueError, TypeError):
        m = FIBER_PARENT_MIN
    return min(50, max(2, m))


def _fiber_oltkey(o):
    try:
        return (o.get("olt_name") or "").strip().lower()
    except (AttributeError, TypeError):
        return ""


def refresh_fiber_parent_map():
    """Sinkronkan entri ping dari status_memory (sintetik dipertahankan).

    Kembalikan map (boleh dipakai langsung). Dipanggil tiap awal poll dan
    single check; check_network juga register/unregister langsung agar
    tak ada jeda ras.
    """
    global fiber_parent_down
    try:
        down_ips = {h for h, d in list(status_memory.items()) if d}
    except Exception:
        down_ips = set()
    if not down_ips:
        for k in [k for k, v in list(fiber_parent_down.items())
                  if not (v or {}).get("synthetic")]:
            fiber_parent_down.pop(k, None)
        return fiber_parent_down
    try:
        conn, c = get_db()
        try:
            c.execute("SELECT name, ip FROM olts")
            olts = [dict(r) for r in c.fetchall()]
        finally:
            conn.close()
    except Exception:
        return fiber_parent_down
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    by_ip = {}
    for o in olts:
        ip = (o.get("ip") or "").strip()
        if ip:
            by_ip.setdefault(ip, []).append((o.get("name") or "").strip().lower())
    live = set()
    for ip in down_ips:
        for key in by_ip.get(ip, []):
            if not key:
                continue
            live.add(key)
            if key not in fiber_parent_down:
                reg = next((o.get("name") for o in olts
                            if (o.get("name") or "").strip().lower() == key), "")
                fiber_parent_down[key] = {"ip": ip, "since": now, "name": reg or key,
                                          "synthetic": False, "count": 0}
    for k in [k for k, v in list(fiber_parent_down.items())
              if not (v or {}).get("synthetic") and k not in live]:
        fiber_parent_down.pop(k, None)
    return fiber_parent_down


def _fiber_parent_register(host_ip, timestamp, c):
    """Daftarkan OLT ber-IP ini sebagai induk + hitung ONT terdampak.

    Kembalikan (affected_count, [olt_names]). c = cursor aktif (baca saja).
    Dipakai transisi DOWN host agar supresi berlaku seketika.
    """
    try:
        c.execute("SELECT name FROM olts WHERE ip=?", (host_ip,))
        names = [(r["name"] or "").strip() for r in c.fetchall()
                 if (r["name"] or "").strip()]
    except sqlite3.OperationalError:
        return 0, []
    affected = 0
    for name in names:
        key = name.lower()
        if key and key not in fiber_parent_down:
            fiber_parent_down[key] = {"ip": host_ip, "since": timestamp,
                                      "name": name, "synthetic": False, "count": 0}
        try:
            affected += c.execute(
                "SELECT COUNT(*) FROM fiber_onts WHERE olt_name COLLATE NOCASE = ?",
                (name,)).fetchone()[0]
        except sqlite3.OperationalError:
            pass
    return affected, names


def _fiber_thresholds():
    """Baca threshold fiber dari settings (bisa diubah via UI)."""
    return {
        "overload": get_setting("fiber_rx_overload", FIBER_RX_OVERLOAD),
        "warn": get_setting("fiber_rx_warn", FIBER_RX_WARN),
        "crit": get_setting("fiber_rx_crit", FIBER_RX_CRIT),
        "target": get_setting("fiber_rx_target", FIBER_RX_TARGET),
        "tx_min": get_setting("fiber_tx_min", FIBER_TX_MIN),
        "tx_max": get_setting("fiber_tx_max", FIBER_TX_MAX),
    }


def _th_for_ont(o):
    """Threshold untuk satu ONT: global, dioverride rx_warn/rx_crit bila diisi.

    o: dict baris fiber_onts (boleh hanya berisi rx_warn/rx_crit).
    """
    th = _fiber_thresholds()
    try:
        w = o.get("rx_warn")
        w = None if w in (None, "") else float(w)
    except (ValueError, TypeError):
        w = None
    try:
        cr = o.get("rx_crit")
        cr = None if cr in (None, "") else float(cr)
    except (ValueError, TypeError):
        cr = None
    if w is not None:
        th["warn"] = w
    if cr is not None:
        th["crit"] = cr
    if not (th["crit"] < th["warn"] <= th["overload"]):
        return _fiber_thresholds()
    return th


def fiber_status_for_rx(rx, th=None):
    """Kembalikan (status, severity, saran, butuh_peredam_db).

    status: normal|warning|critical|overload|unknown
    severity: None|warning|high|disaster (mapping ke triggers)
    """
    th = th or _fiber_thresholds()
    if rx is None:
        return "unknown", None, "Belum ada data pengukuran.", 0
    try:
        rx = float(rx)
    except (ValueError, TypeError):
        return "unknown", None, "Data Rx tidak valid.", 0
    if rx > th["overload"]:
        need = round(max(rx - th["target"], 0.0), 1)
        # bulatkan ke pilihan attenuator pasaran 5/10/15 dB
        if need <= 5:
            rec = "5dB"
        elif need <= 10:
            rec = "10dB"
        else:
            rec = "15dB"
        return ("overload", "high",
                f"Rx overload ({rx} dBm). Pasang PEREDAM/attenuator {rec} "
                f"(kebutuhan hitung ±{need} dB agar ke ~{th['target']} dBm).", need)
    if rx < th["crit"]:
        return ("critical", "disaster",
                f"Redaman tinggi! Rx {rx} dBm (< {th['crit']} dBm). "
                "Cek bending, konektor kotor, splicing, ODP/ODC.", 0)
    if rx < th["warn"]:
        return ("warning", "warning",
                f"Rx {rx} dBm mendekati batas ({th['warn']} dBm). "
                "Jadwalkan cek jalur fiber.", 0)
    return ("normal", None, f"Rx {rx} dBm normal.", 0)


def fiber_status_for_tx(tx, th=None):
    """Kembalikan (status, severity, saran). status: tx_ok|tx_abnormal|tx_unknown."""
    th = th or _fiber_thresholds()
    if tx is None:
        return "tx_unknown", None, ""
    try:
        tx = float(tx)
    except (ValueError, TypeError):
        return "tx_unknown", None, ""
    if tx < th["tx_min"] or tx > th["tx_max"]:
        if tx <= -5:
            return ("tx_abnormal", "high",
                    f"Tx {tx} dBm di luar batas ({th['tx_min']}..{th['tx_max']} dBm). "
                    "Laser ONT kemungkinan mati/hang — cek ONT.")
        return ("tx_abnormal", "warning",
                f"Tx {tx} dBm di luar batas normal ({th['tx_min']}..{th['tx_max']} dBm). "
                "Cek ONT/SFP.")
    return "tx_ok", None, ""


_SEV_RANK = {"warning": 1, "high": 2, "disaster": 3}


def fiber_eval(rx, tx=None, th=None):
    """Gabungan Rx+Tx: kembalikan (status, severity, advice, need_peredam_db).

    status terburuk yang menang (critical > overload > warning > normal > unknown).
    """
    th = th or _fiber_thresholds()
    r_status, r_sev, r_adv, need = fiber_status_for_rx(rx, th)
    t_status, t_sev, t_adv = fiber_status_for_tx(tx, th)
    if r_status == "unknown":
        if t_sev:
            # status mengikuti severity agar label & badge konsisten
            st = "warning" if t_sev == "warning" else "critical"
            return st, t_sev, t_adv, 0
        return "unknown", None, r_adv, 0
    if not t_sev:
        return r_status, r_sev, r_adv, need
    # kedua sisi alarm: gabung saran, severity tertinggi menang
    advice = (r_adv + " " + t_adv).strip() if t_adv else r_adv
    if _SEV_RANK.get(t_sev, 0) > _SEV_RANK.get(r_sev or "", 0):
        # status mengikuti severity agar label & badge konsisten
        st = "warning" if t_sev == "warning" else "critical"
        return st, t_sev, advice, need
    return r_status, r_sev, advice, need


# Rugi-rugi tipikal jalur FTTH (dB) untuk kalkulator link budget
FIBER_SPLITTER_LOSS = {"1:2": 3.5, "1:4": 7.2, "1:8": 10.5, "1:16": 13.8, "1:32": 17.1}
FIBER_PER_KM_DB = 0.35      # serat G.652 @1310nm
FIBER_CONNECTOR_DB = 0.5    # per konektor
FIBER_SPLICE_DB = 0.1       # per fusion splice
FIBER_BUDGET_TOLERANCE_DB = 3.0  # selisih wajar aktual vs teori


def fiber_link_budget(tx_dbm, splitters, fiber_km, connectors, splices, margin_db=0.0):
    """Hitung Rx teoritis. Kembalikan dict rincian (stateless, tanpa DB).

    splitters: list seperti ["1:4", "1:8"] (ODC lalu ODP).
    """
    splitter_db = round(sum(FIBER_SPLITTER_LOSS[s] for s in splitters), 2)
    fiber_db = round(fiber_km * FIBER_PER_KM_DB, 2)
    conn_db = round(connectors * FIBER_CONNECTOR_DB, 2)
    splice_db = round(splices * FIBER_SPLICE_DB, 2)
    total = round(splitter_db + fiber_db + conn_db + splice_db + margin_db, 2)
    return {
        "tx_dbm": round(tx_dbm, 2),
        "losses": {"splitter_db": splitter_db, "fiber_db": fiber_db,
                   "connector_db": conn_db, "splice_db": splice_db,
                   "margin_db": round(margin_db, 2), "total_db": total},
        "expected_rx": round(tx_dbm - total, 2),
    }


def fiber_budget_verdict(expected_rx, actual_rx):
    """Bandingkan Rx teori vs aktual. Kembalikan (verdict, severity, saran)."""
    if actual_rx is None:
        return "no_data", None, "Pilih ONT yang sudah ada pengukuran Rx untuk pembanding."
    delta = round(actual_rx - expected_rx, 2)  # negatif = redaman berlebih
    if delta <= -FIBER_BUDGET_TOLERANCE_DB:
        return ("over_budget", "high",
                f"Redaman berlebih {abs(delta)} dB dari budget! "
                "Cek bending, konektor kotor, splice ulang, atau ODP basah/rusak.")
    if delta >= FIBER_BUDGET_TOLERANCE_DB:
        return ("under_budget", "warning",
                f"Rx aktual {abs(delta)} dB lebih bagus dari teori. "
                "Cek ulang input budget (mungkin splitter/jarak salah catat).")
    return ("ok", None, f"Selisih {delta} dB masih dalam toleransi ±{FIBER_BUDGET_TOLERANCE_DB} dB. Jalur sehat.")


def get_ont_optical_power_snmp(olt_ip, community, ont_index, vendor="zte"):
    """Hook integrasi OLT via SNMP (belum aktif, return None).

    vendor 'zte'    : ONT Rx/Tx umumnya di enterprise MIB 1.3.6.1.4.1.3902
                      (beda tipe OLT beda tabel, sesuaikan lewat uji snmpwalk).
    vendor 'huawei' : hwGpon MIB 1.3.6.1.4.1.2011.6.128.1.x (rx/tx optical power).
    Cara pakai: isi OLT IP + community di env / form, lalu ganti fungsi ini
    dengan snmpwalk asli memakai pola get_snmp_bandwidth() di atas.
    """
    _ = (olt_ip, community, ont_index, vendor)
    return None, None


def _is_mute_active(o, now=None):
    """True bila alarm ONT sedang di-mute (hormati mute_until).

    mute_alarm=0 -> tidak mute. mute_until kosong -> mute permanen.
    mute_until terlewati -> mute dianggap kedaluwarsa (alarm aktif lagi).
    Format mute_until fleksibel mengikuti _parse_maint_time.
    """
    try:
        if not o.get("mute_alarm"):
            return False
    except (AttributeError, TypeError):
        return False
    until_raw = (o.get("mute_until") or "").strip() if isinstance(o.get("mute_until"), str) else o.get("mute_until")
    if not until_raw:
        return True
    try:
        until = _parse_maint_time(str(until_raw))
    except Exception:
        return True
    if not until:
        return True
    return (now or datetime.now()) <= until


def _fiber_maintenance_info(o, maint_map):
    """Cek maintenance hierarki untuk satu ONT.

    Urutan: ONT SN persis -> ODP:<nama> -> OLT:<nama> (case-insensitive).
    Kembalikan (in_maint, reason, scope) dengan scope salah satu
    'ont'/'odp'/'olt'/None.
    """
    if not maint_map:
        return False, None, None
    try:
        norm = {str(k).strip().lower(): v for k, v in (maint_map or {}).items()}
    except Exception:
        return False, None, None
    sn = (o.get("ont_sn") or "").strip()
    if sn and sn.lower() in norm:
        e = norm[sn.lower()] or {}
        return True, (e.get("reason") or "").strip() or None, "ont"
    odp = (o.get("odp_name") or "").strip().lower()
    if odp and ("odp:" + odp) in norm:
        e = norm["odp:" + odp] or {}
        return True, (e.get("reason") or "").strip() or None, "odp"
    olt = (o.get("olt_name") or "").strip().lower()
    if olt and ("olt:" + olt) in norm:
        e = norm["olt:" + olt] or {}
        return True, (e.get("reason") or "").strip() or None, "olt"
    return False, None, None


def _fiber_degrade_settings():
    """(thresh_db, days) degradasi dari settings, di-clamp ke rentang valid."""
    try:
        thresh = float(get_setting("fiber_degrade_db", FIBER_DEGRADE_DB))
    except (ValueError, TypeError):
        thresh = FIBER_DEGRADE_DB
    try:
        days = int(float(get_setting("fiber_degrade_days", FIBER_DEGRADE_DAYS)))
    except (ValueError, TypeError):
        days = FIBER_DEGRADE_DAYS
    return min(10.0, max(0.5, thresh)), min(30, max(1, days))


def _fiber_stale_threshold_min():
    try:
        m = int(float(get_setting("fiber_stale_min", FIBER_STALE_MIN)))
    except (ValueError, TypeError):
        m = FIBER_STALE_MIN
    return min(10080, max(10, m))


def _fmt_age(age_min):
    try:
        m = float(age_min)
    except (ValueError, TypeError):
        return "?"
    if m < 1:
        return "baru saja"
    if m < 60:
        return f"{int(m)} mnt"
    if m < 60 * 48:
        return f"{int(m // 60)} jam"
    d = m / (60 * 24)
    return f"{int(d)} hari" if d >= 10 else f"{round(d, 1)} hari"


def _fiber_stale_info(o, now=None):
    """(is_stale, age_txt). Hanya sumber otomatis (snmp/simulator) yang punya
    pengukuran tapi tak ada data baru melewati ambang fiber_stale_min.
    Manual tak pernah stale (datanya input teknisi, bukan hasil polling)."""
    try:
        src = (o.get("source") or "manual").strip().lower()
    except (AttributeError, TypeError):
        src = "manual"
    if src not in ("snmp", "simulator"):
        return False, None
    if o.get("rx_power") is None and o.get("tx_power") is None:
        return False, None
    seen_raw = (o.get("last_seen") or "").strip()
    if not seen_raw:
        return False, None
    try:
        seen = datetime.strptime(seen_raw, "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return False, None
    now = now or datetime.now()
    age_min = (now - seen).total_seconds() / 60.0
    if age_min < 0:
        return False, None
    if age_min >= _fiber_stale_threshold_min():
        return True, _fmt_age(age_min)
    return False, None


def _fiber_stale_advice(o, age_txt):
    rx = o.get("rx_power")
    rx_txt = f"{rx} dBm" if rx is not None else "—"
    seen = (o.get("last_seen") or "—")
    return (f"Data tidak segar sejak {age_txt} (terakhir {seen}). "
            f"Rx terakhir {rx_txt}. Kemungkinan ONT LOS/mati atau SNMP OLT gagal — "
            "cek OLT, ONT, dan jalur fiber.")


def _fiber_degradation(fid, current_rx, now=None):
    """(degrading, drop_db). Bandingkan Rx terawal dalam window
    (fiber_degrade_days) vs Rx kini. drop positif = memburuk.
    Butuh rentang history >= 24 jam agar noise sesaat tak false-positive."""
    try:
        cur = float(current_rx)
    except (ValueError, TypeError):
        return False, None
    if fid is None:
        return False, None
    thresh, days = _fiber_degrade_settings()
    now = now or datetime.now()
    start = (now - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        conn, c = get_db()
        try:
            c.execute("SELECT rx_power, timestamp FROM fiber_history WHERE ont_id=? "
                      "AND rx_power IS NOT NULL AND timestamp >= ? "
                      "ORDER BY timestamp ASC, id ASC LIMIT 1", (fid, start))
            first = c.fetchone()
        finally:
            conn.close()
    except Exception:
        return False, None
    if not first:
        return False, None
    try:
        first_rx = float(first["rx_power"])
        first_ts = datetime.strptime(first["timestamp"], "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError, KeyError):
        return False, None
    if (now - first_ts).total_seconds() < FIBER_DEGRADE_MIN_SPAN_H * 3600:
        return False, None
    drop = round(first_rx - cur, 2)
    return drop >= thresh, drop


def _fiber_flap_settings():
    """(min_flips, hours) flap dari settings, di-clamp ke rentang valid."""
    try:
        flips = int(float(get_setting("fiber_flap_flips", FIBER_FLAP_FLIPS)))
    except (ValueError, TypeError):
        flips = FIBER_FLAP_FLIPS
    try:
        hours = int(float(get_setting("fiber_flap_hours", FIBER_FLAP_HOURS)))
    except (ValueError, TypeError):
        hours = FIBER_FLAP_HOURS
    return min(20, max(2, flips)), min(72, max(1, hours))


def _fiber_flap(fid, now=None):
    """(flapping, flips). Hitung bolak-balik normal<->terganggu dari history.

    Tiap titik dinilai via fiber_eval (threshold global); pindah kubu
    normal<->(warning/critical/overload) dihitung 1 flip. Titik tanpa data
    dilewati (tak memutus rangkaian).
    """
    if fid is None:
        return False, None
    thresh, hours = _fiber_flap_settings()
    now = now or datetime.now()
    start = (now - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        conn, c = get_db()
        try:
            c.execute("SELECT rx_power, tx_power FROM fiber_history WHERE ont_id=? "
                      "AND timestamp >= ? ORDER BY timestamp ASC, id ASC LIMIT 20000",
                      (fid, start))
            pts = c.fetchall()
        finally:
            conn.close()
    except Exception:
        return False, None
    if not pts:
        return False, 0
    th = _fiber_thresholds()
    flips, prev_bad = 0, None
    for r in pts:
        try:
            rx = None if r["rx_power"] is None else float(r["rx_power"])
        except (ValueError, TypeError):
            rx = None
        try:
            tx = None if r["tx_power"] is None else float(r["tx_power"])
        except (ValueError, TypeError):
            tx = None
        if rx is None and tx is None:
            continue
        status, _, _, _ = fiber_eval(rx, tx, th)
        bad = status in ("warning", "critical", "overload")
        if prev_bad is not None and bad != prev_bad:
            flips += 1
        prev_bad = bad
    return flips >= thresh, flips


# Status yang dianggap "gangguan" untuk catatan downtime (layak SLA).
_FIBER_DOWN_SEV = ("critical", "overload")


def _fiber_downtime_transition(c, fid, ont_sn, status, rx, timestamp):
    """Buka/tutup catatan downtime dalam transaksi milik caller.

    Buka saat critical/overload; tutup saat normal/warning/unknown.
    stale (tak terpantau) membiarkan catatan terbuka (gangguan dianggap
    berlanjut sampai ada data segar). Kembalikan durasi detik bila baru
    saja tertutup, else None. c = cursor aktif.
    """
    try:
        c.execute("SELECT id, status, started_at FROM fiber_downtime "
                  "WHERE ont_id=? AND resolved_at IS NULL ORDER BY id DESC LIMIT 1",
                  (fid,))
        open_row = c.fetchone()
    except sqlite3.OperationalError:
        return None
    if status in _FIBER_DOWN_SEV:
        if open_row:
            if open_row["status"] != status:
                try:
                    c.execute("UPDATE fiber_downtime SET status=? WHERE id=?",
                              (status, open_row["id"]))
                except sqlite3.OperationalError:
                    pass
            return None
        try:
            c.execute("INSERT INTO fiber_downtime (ont_id, ont_sn, status, rx_dbm, started_at)"
                      " VALUES (?,?,?,?,?)", (fid, ont_sn, status, rx, timestamp))
        except sqlite3.OperationalError:
            pass
        return None
    if not open_row or status == "stale":
        return None
    try:
        started = datetime.strptime(open_row["started_at"], "%Y-%m-%d %H:%M:%S")
        ended = datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S")
        dur = max(0, int((ended - started).total_seconds()))
    except (ValueError, TypeError):
        dur = 0
    try:
        c.execute("UPDATE fiber_downtime SET resolved_at=?, duration_s=? WHERE id=?",
                  (timestamp, dur, open_row["id"]))
    except sqlite3.OperationalError:
        return None
    return dur


# Tipe log yang boleh disuppress induk (mute/maintenance eksplisit selalu menang).
_FIBER_PARENT_LOGS = {"FIBER_CRITICAL", "FIBER_WARNING", "FIBER_OVERLOAD",
                      "FIBER_NORMAL", "FIBER_STALE", "FIBER_DEGRADE"}


def _fiber_parent_of(o):
    """Entri induk aktif untuk satu ONT (None bila tak ada)."""
    try:
        return fiber_parent_down.get(_fiber_oltkey(o))
    except Exception:
        return None


def _fiber_emit(c, o, rx, dec, log, tg, timestamp, tg_queue, closed_dur=None):
    """Tulis log + antre telegram dengan aturan supresi induk.

    Bila ONT di bawah parent aktif dan event bertipe alarm/pulih/stale/
    degradasi (bukan mute/maintenance eksplisit): telegram dibuang,
    log ditulis ulang sebagai FIBER_PARENT. Dipakai poll & single check.
    """
    parent = _fiber_parent_of(o)
    if log:
        etype, ehost, emsg = log
        if closed_dur is not None and etype == "FIBER_NORMAL":
            emsg = f"{emsg} Durasi gangguan: {_fmt_duration(closed_dur)}."
        if parent and etype in _FIBER_PARENT_LOGS:
            pname = (parent.get("name") or _fiber_oltkey(o) or "?")
            etype = "FIBER_PARENT"
            emsg = (f"{dec['status'].upper()} Rx {rx} Tx {o.get('tx_power')} — "
                    f"telegram disuppress (induk OLT '{pname}' bermasalah)")
        try:
            _insert_system_log(c, etype, ehost, emsg, timestamp)
        except Exception:
            pass
    if tg and not parent:
        if closed_dur is not None and dec["status"] == "normal":
            tg = f"{tg}\nDurasi gangguan: {_fmt_duration(closed_dur)}"
        tg_queue.append(tg)


def _fiber_decide(o, th, prev, maint_map, timestamp):
    """Satu keputusan evaluasi ONT. Kembalikan dict:
    {status, severity, advice, need, muted, in_maint, stale, tg_msg, log, mem}.
    tg_msg None bila tak perlu kirim (belum berubah / mute / maintenance).
    log = (event_type, host, message) atau None. mem = nilai memory baru.
    """
    status, severity, advice, need = fiber_eval(o.get("rx_power"), o.get("tx_power"), th)
    muted = _is_mute_active(o)
    in_maint, maint_reason, maint_scope = _fiber_maintenance_info(o, maint_map)
    stale, stale_age = _fiber_stale_info(o)
    if stale and status in ("normal", "warning", "unknown"):
        status, severity = "stale", "warning"
        advice = _fiber_stale_advice(o, stale_age or "?")
    rx, tx = o.get("rx_power"), o.get("tx_power")
    label = o.get("customer") or o.get("ont_sn")
    tx_txt = f"Tx: {tx} dBm" if tx is not None else "Tx: —"
    tg_msg, log, mem = None, None, prev
    if status in ("critical", "overload", "warning") and prev != status:
        mem = status
        if status == "overload":
            tg_msg = (
                f"🔊 *FIBER OVERLOAD — PERLU PEREDAM*\nONT: `{o['ont_sn']}` ({label})\n"
                f"Rx: *{rx} dBm* (> {th['overload']} dBm) · {tx_txt}\n{advice}\nWaktu: {timestamp}"
            )
        elif status == "critical":
            tg_msg = (
                f"🚨 *FIBER REDAMAN TINGGI*\nONT: `{o['ont_sn']}` ({label})\n"
                f"Rx: *{rx} dBm* · {tx_txt}\n{advice}\nWaktu: {timestamp}"
            )
        else:
            tg_msg = (
                f"⚠️ *FIBER WARNING*\nONT: `{o['ont_sn']}` ({label})\n"
                f"Rx: *{rx} dBm* · {tx_txt}\n{advice}\nWaktu: {timestamp}"
            )
        if in_maint:
            scope_txt = f" ({maint_scope.upper()} {(o.get('odp_name') or o.get('olt_name') or o['ont_sn'])})" if maint_scope and maint_scope != "ont" else ""
            log = ("FIBER_MAINT", o["ont_sn"],
                   f"Rx {rx} dBm Tx {tx} dalam maintenance{scope_txt} — telegram disuppress"
                   f"{(' - ' + maint_reason) if maint_reason else ''}")
            tg_msg = None
        elif muted:
            mute_note = ""
            try:
                if (o.get("mute_until") or "").strip():
                    mute_note = f" s/d {o.get('mute_until')}"
                if (o.get("mute_reason") or "").strip():
                    mute_note += f" ({(o.get('mute_reason') or '').strip()[:100]})"
            except Exception:
                pass
            log = ("FIBER_MUTED", o["ont_sn"], f"Rx {rx} dBm Tx {tx} — alarm dimute{mute_note}")
            tg_msg = None
        else:
            log = ("FIBER_" + status.upper(), o["ont_sn"],
                   f"Rx {rx} dBm Tx {tx} — {advice}")
    elif status == "normal":
        if prev not in (None, "normal"):
            if prev == "stale":
                log = ("FIBER_NORMAL", o["ont_sn"], f"Terpantau kembali (Rx {rx} dBm)")
            else:
                log = ("FIBER_NORMAL", o["ont_sn"], f"Rx kembali normal ({rx} dBm)")
            if not muted and not in_maint:
                tg_msg = (f"✅ *FIBER PULIH*\nONT: `{o['ont_sn']}`\n"
                          f"Rx: {rx} dBm\nWaktu: {timestamp}")
        mem = "normal"
    elif status == "stale":
        mem = "stale"
        if prev != "stale":
            if in_maint:
                log = ("FIBER_MAINT", o["ont_sn"],
                       "Stale dalam maintenance — telegram disuppress"
                       f"{(' - ' + maint_reason) if maint_reason else ''}")
            elif muted:
                log = ("FIBER_MUTED", o["ont_sn"], "Stale — alarm dimute")
            else:
                log = ("FIBER_STALE", o["ont_sn"],
                       f"Tak terpantau sejak {stale_age} — {advice}")
                tg_msg = (f"⚠️ *FIBER TAK TERPANTAU (STALE)*\nONT: `{o['ont_sn']}` ({label})\n"
                          f"{advice}\nWaktu: {timestamp}")
    elif status == "unknown":
        mem = "unknown"
    return {"status": status, "severity": severity, "advice": advice, "need": need,
            "muted": muted, "in_maint": in_maint, "stale": stale,
            "tg_msg": tg_msg, "log": log, "mem": mem}


def _fiber_single_check(fid):
    """Evaluasi 1 ONT secara sinkron (dipakai create/update agar alert cepat).

    Update status/memory/log + kirim telegram langsung tanpa menunggu
    scheduler, tanpa thread seluruh tabel (anti duplikat: memory di-set di sini
    sehingga poll 120s berikutnya tidak mengirim ulang).
    """
    try:
        conn, c = get_db()
        try:
            c.execute("SELECT * FROM fiber_onts WHERE id=?", (fid,))
            row = c.fetchone()
            if not row:
                return
            o = dict(row)
        finally:
            conn.close()
    except Exception as e:
        print(f"[FIBER] single check load gagal: {e}")
        return
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        refresh_fiber_parent_map()
    except Exception as e:
        print(f"[FIBER] parent refresh gagal: {e}")
    dec = _fiber_decide(o, _th_for_ont(o), fiber_alarm_memory.get(fid),
                        get_active_maintenance_map(), timestamp)
    closed_dur = None
    tg_out = []
    with db_lock:
        conn, c = get_db()
        try:
            c.execute("UPDATE fiber_onts SET status=?, last_checked=? WHERE id=?",
                      (dec["status"], timestamp, fid))
            try:
                closed_dur = _fiber_downtime_transition(
                    c, fid, o["ont_sn"], dec["status"], o.get("rx_power"), timestamp)
            except Exception as e:
                print(f"[FIBER] downtime single check gagal: {e}")
                closed_dur = None
            _fiber_emit(c, o, o.get("rx_power"), dec, dec["log"], dec["tg_msg"],
                        timestamp, tg_out, closed_dur)
            _commit_with_retry(conn)
        except sqlite3.OperationalError as e:
            print(f"[DB LOCK] fiber single check gagal: {e}")
            try:
                conn.rollback()
            except Exception:
                pass
        finally:
            conn.close()
    fiber_alarm_memory[fid] = dec["mem"]
    for tmsg in tg_out:
        try:
            send_telegram_alert(tmsg)
        except Exception as e:
            print(f"[WARN] telegram fiber gagal: {e}")
def poll_fiber_monitor():
    """Job scheduler: snapshot history semua ONT + cek threshold.

    ONT source=simulator nilainya digerakkan (random walk) agar grafik demo hidup.
    ONT source=manual/snmp nilainya tidak diubah, tapi tetap dicatat ke history
    agar grafik tidak kosong. ONT tanpa Rx & Tx dilewati (tak ada yang dicatat).
    Telegram hanya saat status berubah (anti spam); disuppress saat mute /
    maintenance (tetap dicatat di system_logs, memory di-set agar tak ada
    ledakan notifikasi setelah maintenance selesai).
    Korelasi induk: 2-pass. Pass 1 evaluasi murni semua ONT; pass 1.5
    mendeteksi insiden massal per OLT (>=fiber_parent_min kritis/overload)
    menjadi 1 alarm induk; pass 2 menerapkan log/telegram/status dengan
    supresi induk (alarm individual dibuang, log FIBER_PARENT).
    """
    import random
    try:
        conn, c = get_db()
        try:
            try:
                c.execute("SELECT * FROM fiber_onts ORDER BY id ASC")
            except sqlite3.OperationalError:
                return
            rows = [dict(r) for r in c.fetchall()]
        finally:
            conn.close()
    except Exception as e:
        print(f"[FIBER] load gagal: {e}")
        return
    if not rows:
        return
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    maint_map = get_active_maintenance_map()
    try:
        refresh_fiber_parent_map()
    except Exception as e:
        print(f"[FIBER] parent refresh gagal: {e}")
    try:
        parent_min = _fiber_parent_min()
    except Exception:
        parent_min = FIBER_PARENT_MIN
    tg_queue = []
    computed = []
    _clearing = []
    with db_lock:
        conn, c = get_db()
        try:
            for o in rows:
                oid = o["id"]
                rx = o["rx_power"]
                tx = o.get("tx_power")
                # simulator: gerakkan Rx pelan agar grafik hidup untuk demo
                # dibatasi zona normal agar data demo tidak memicu alarm palsu
                if (o.get("source") or "manual") == "simulator":
                    base = rx if rx is not None else -19.0
                    try:
                        base = float(base)
                    except (ValueError, TypeError):
                        base = -19.0
                    # clamp zona normal -22..-16 (di dalam -25..-8)
                    rx = max(-22.0, min(-16.0, round(base + random.uniform(-0.6, 0.6), 2)))
                    c.execute("UPDATE fiber_onts SET rx_power=?, last_checked=?, updated_at=?, last_seen=? WHERE id=?",
                              (rx, timestamp, timestamp, timestamp, oid))
                    o["rx_power"] = rx
                # lewati ONT tanpa data sama sekali (hemat DB, grafik tetap kosong wajar)
                if rx is None and tx is None:
                    continue
                # snapshot history tiap poll agar grafik terisi
                # (720 titik/hari/ONT @120s, dibersihkan retensi 30 hari)
                try:
                    c.execute("INSERT INTO fiber_history (ont_id, rx_power, tx_power, timestamp) VALUES (?, ?, ?, ?)",
                              (oid, rx, tx, timestamp))
                except sqlite3.OperationalError:
                    pass
                o["rx_power"], o["tx_power"] = rx, tx
                dec = _fiber_decide(o, _th_for_ont(o), fiber_alarm_memory.get(oid),
                                    maint_map, timestamp)
                fiber_alarm_memory[oid] = dec["mem"]
                try:
                    _was_degr = bool((fiber_degrade_memory.get(oid) or {}).get("degrading"))
                    _degr, _drop = _fiber_degradation(oid, rx)
                    fiber_degrade_memory[oid] = {"degrading": _degr, "drop_db": _drop}
                except Exception as e:
                    print(f"[FIBER] degradasi {oid} gagal: {e}")
                    _was_degr, _degr, _drop = False, False, None
                try:
                    _flap, _flips = _fiber_flap(oid)
                    fiber_flap_memory[oid] = {"flapping": _flap, "flips": _flips}
                except Exception as e:
                    print(f"[FIBER] flap {oid} gagal: {e}")
                computed.append((o, oid, rx, tx, dec, _was_degr, _degr, _drop))
            # PASS 1.5: korelasi induk — >=min ONT kritis/overload satu OLT
            # (non-mute/maint) menjadi 1 insiden; sintetik yang anggotanya
            # sudah habis dibersihkan + telegram pulih.
            try:
                bad_by_olt = {}
                for (_o, _oid, _rx, _tx, _dec, _w, _d, _dr) in computed:
                    if _dec["status"] in ("critical", "overload") \
                            and not _dec.get("muted") and not _dec.get("in_maint"):
                        bad_by_olt.setdefault(_fiber_oltkey(_o), []).append(_o)
                for _oltkey, _members in bad_by_olt.items():
                    if not _oltkey or _oltkey in fiber_parent_down \
                            or len(_members) < parent_min:
                        continue
                    _name = next(((m.get("olt_name") or "").strip() for m in _members
                                  if (m.get("olt_name") or "").strip()), _oltkey)
                    fiber_parent_down[_oltkey] = {"ip": None, "since": timestamp,
                                                  "name": _name, "synthetic": True,
                                                  "count": len(_members)}
                    tg_queue.append(
                        f"🔌 *INSIDEN MASSAL OLT*\nOLT: `{_name}`\n"
                        f"{len(_members)} ONT kritis/overload (ambang {parent_min}). "
                        f"Alarm individual disuppress mulai kini.\n"
                        f"Cek OLT/power/PON uplink!\nWaktu: {timestamp}")
                    try:
                        _insert_system_log(c, "FIBER_PARENT", _name,
                                           f"insiden massal: {len(_members)} ONT kritis/overload",
                                           timestamp)
                    except Exception:
                        pass
                for _key, _ent in list(fiber_parent_down.items()):
                    if not (_ent or {}).get("synthetic"):
                        continue
                    _still = sum(1 for (_o, _oid, _rx, _tx, _dec, _w, _d, _dr) in computed
                                 if _fiber_oltkey(_o) == _key
                                 and _dec["status"] in ("critical", "overload"))
                    (_ent or {}).update({"count": _still})
                    if not _still:
                        # ditandai dulu, di-pop setelah pass 2 agar recovery
                        # individual siklus ini tetap tersuppress (diwakili
                        # 1 telegram pulih induk)
                        _clearing.append(_key)
                        _pname = (_ent or {}).get("name") or _key
                        tg_queue.append(
                            f"✅ *INSIDEN MASSAL PULIH*\nOLT: `{_pname}`\n"
                            f"Seluruh ONT kembali normal.\nWaktu: {timestamp}")
                        try:
                            _insert_system_log(c, "FIBER_PARENT", _pname,
                                               "insiden massal pulih", timestamp)
                        except Exception:
                            pass
            except Exception as e:
                print(f"[FIBER] korelasi induk gagal: {e}")
            # PASS 2: terapkan (downtime, log, telegram dengan supresi induk, status)
            for (o, oid, rx, tx, dec, _was_degr, _degr, _drop) in computed:
                # notifikasi degradasi baru (hanya status normal; cooldown 24 jam)
                _dlog, _dtg = None, None
                if _degr and not _was_degr and dec["status"] == "normal":
                    try:
                        _dth, _dd = _fiber_degrade_settings()
                    except Exception:
                        _dth, _dd = FIBER_DEGRADE_DB, FIBER_DEGRADE_DAYS
                    if time.time() - fiber_degrade_tg.get(oid, 0) >= FIBER_DEGRADE_TG_COOLDOWN_S:
                        fiber_degrade_tg[oid] = time.time()
                        _label = o.get("customer") or o.get("ont_sn")
                        if dec.get("in_maint"):
                            _dlog = ("FIBER_MAINT", o["ont_sn"],
                                     f"Degradasi {_drop} dB dalam maintenance — telegram disuppress")
                        elif dec.get("muted"):
                            _dlog = ("FIBER_MUTED", o["ont_sn"],
                                     f"Degradasi {_drop} dB — alarm dimute")
                        else:
                            _dlog = ("FIBER_DEGRADE", o["ont_sn"],
                                     f"Rx turun {_drop} dB dalam {_dd} hari (kini {rx} dBm)")
                            _dtg = (
                                f"📉 *FIBER DEGRADASI TERDETEKSI*\nONT: `{o['ont_sn']}` ({_label})\n"
                                f"Rx turun *{_drop} dB* dalam {_dd} hari "
                                f"(kini {rx} dBm, ambang {_dth} dB).\n"
                                f"Cek bending/konektor/splicing sebelum kritis.\nWaktu: {timestamp}"
                            )
                try:
                    closed_dur = _fiber_downtime_transition(
                        c, oid, o["ont_sn"], dec["status"], rx, timestamp)
                except Exception as e:
                    print(f"[FIBER] downtime {oid} gagal: {e}")
                    closed_dur = None
                _fiber_emit(c, o, rx, dec, dec["log"], dec["tg_msg"],
                            timestamp, tg_queue, closed_dur)
                _fiber_emit(c, o, rx, dec, _dlog, _dtg, timestamp, tg_queue)
                c.execute("UPDATE fiber_onts SET status=?, last_checked=? WHERE id=?",
                          (dec["status"], timestamp, oid))
            for _ckey in _clearing:
                fiber_parent_down.pop(_ckey, None)
            _commit_with_retry(conn)
        except sqlite3.OperationalError as e:
            print(f"[DB LOCK] poll_fiber gagal: {e}")
            try:
                conn.rollback()
            except Exception:
                pass
        finally:
            conn.close()
    for msg in tg_queue:
        try:
            send_telegram_alert(msg)
        except Exception as e:
            print(f"[WARN] telegram fiber gagal: {e}")


def poll_fiber_snmp():
    """Tarik Rx/Tx ONT dari OLT via SNMP (source=snmp).

    Tiap ONT butuh olt_name (cocok ke tabel olts) + ont_index (sufiks OID).
    Nilai mentah dibagi `div` OLT (umumnya 100 = satuan 0.01 dBm).
    Status/alarm/history ditangani poll_fiber_monitor berikutnya.

    Catatan: didefinisikan di sini (sebelum blok scheduler) agar referensi
    scheduler.add_job saat import tak NameError.
    """
    try:
        conn, c = get_db()
        try:
            try:
                c.execute("SELECT * FROM olts ORDER BY id ASC")
                olts = [dict(r) for r in c.fetchall()]
            except sqlite3.OperationalError:
                return
            try:
                c.execute("SELECT id, ont_sn, olt_name, ont_index, rx_power, tx_power"
                          " FROM fiber_onts WHERE source='snmp'")
                onts = [dict(r) for r in c.fetchall()]
            except sqlite3.OperationalError:
                return
        finally:
            conn.close()
    except Exception as e:
        print(f"[FIBER-SNMP] load gagal: {e}")
        return
    if not olts or not onts:
        return
    by_name = {(o.get("name") or ""): o for o in olts}
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    updates = []
    for t in onts:
        idx = (t.get("ont_index") or "").strip()
        olt = by_name.get((t.get("olt_name") or "").strip())
        if not idx or not olt or not (olt.get("community") or "").strip() or not olt.get("ip"):
            continue
        oids, kinds = [], []
        if _valid_oid(olt.get("rx_base") or ""):
            oids.append(olt["rx_base"].strip().strip(".") + "." + idx)
            kinds.append("rx")
        if _valid_oid(olt.get("tx_base") or ""):
            oids.append(olt["tx_base"].strip().strip(".") + "." + idx)
            kinds.append("tx")
        if not oids:
            continue
        try:
            vals = _snmp_get(olt["ip"], olt["community"], oids, timeout=3.0)
        except Exception as e:
            print(f"[FIBER-SNMP] {t.get('ont_sn')}: {e}")
            continue
        new_rx, new_tx = t.get("rx_power"), t.get("tx_power")
        got_fresh = False
        for kind, v in zip(kinds, vals or []):
            if v is None:
                continue
            f = _olt_raw_to_dbm(v, olt)
            if f is None:
                continue
            if kind == "rx" and -40 <= f <= 10:
                new_rx = f
                got_fresh = True
            elif kind == "tx" and -10 <= f <= 10:
                new_tx = f
                got_fresh = True
        # last_seen maju di tiap poll sukses (walau nilai tak berubah) agar
        # ONT yang OLT-nya mati perlahan menjadi 'stale', bukan beku selamanya
        if got_fresh:
            updates.append((new_rx, new_tx, timestamp, timestamp, t["id"]))
    if not updates:
        return
    with db_lock:
        conn, c = get_db()
        try:
            c.executemany("UPDATE fiber_onts SET rx_power=?, tx_power=?,"
                          " last_seen=?, last_checked=? WHERE id=?", updates)
            _commit_with_retry(conn)
        except sqlite3.OperationalError as e:
            print(f"[DB LOCK] poll_fiber_snmp gagal: {e}")
            try:
                conn.rollback()
            except Exception:
                pass
        finally:
            conn.close()
    print(f"[FIBER-SNMP] {len(updates)} ONT diperbarui dari OLT")


init_db()
_seed_admin_from_env()

def rebuild_alarm_memory():
    global status_memory, down_since, agent_status_memory, agent_offline_memory, fiber_alarm_memory
    try:
        conn, c = get_db()
        try:
            c.execute("SELECT ip FROM hosts ORDER BY id ASC")
            valid = [r["ip"] for r in c.fetchall()]
            valid_set = set(valid)

            try:
                c.execute("SELECT host, started_at FROM down_events WHERE resolved_at IS NULL")
                for r in c.fetchall():
                    h = r["host"]
                    if h not in valid_set:
                        continue
                    status_memory[h] = True
                    try:
                        down_since[h] = datetime.strptime(r["started_at"], "%Y-%m-%d %H:%M:%S")
                    except Exception:
                        down_since[h] = datetime.now()
            except Exception as e:
                print(f"[REBUILD] down_events gagal: {e}")

            for h in valid:
                if h not in status_memory:
                    status_memory[h] = False

            try:
                c.execute("SELECT value FROM settings WHERE key='cpu_threshold'")
                row = c.fetchone()
                cpu_thresh = float(row["value"]) if row else 85.0
            except Exception:
                cpu_thresh = 85.0
            try:
                c.execute("SELECT value FROM settings WHERE key='ram_threshold'")
                row = c.fetchone()
                ram_thresh = float(row["value"]) if row else 90.0
            except Exception:
                ram_thresh = 90.0
            try:
                c.execute("SELECT value FROM settings WHERE key='disk_threshold'")
                row = c.fetchone()
                disk_thresh = float(row["value"]) if row else 90.0
            except Exception:
                disk_thresh = 90.0
            now = datetime.now()
            for h in valid:
                try:


                    c.execute("SELECT cpu_percent, ram_percent, disk_percent, timestamp FROM agent_metrics WHERE host=? AND cpu_percent IS NOT NULL ORDER BY id DESC LIMIT 1", (h,))
                    last = c.fetchone()
                    if not last:
                        continue
                    agent_status_memory[h] = {
                        "cpu": bool(last["cpu_percent"] is not None and last["cpu_percent"] > cpu_thresh),
                        "ram": bool(last["ram_percent"] is not None and last["ram_percent"] > ram_thresh),
                        "disk": bool(last["disk_percent"] is not None and last["disk_percent"] > disk_thresh),
                    }

                    try:
                        last_time = datetime.strptime(last["timestamp"], "%Y-%m-%d %H:%M:%S")
                        if (now - last_time).total_seconds() > 120:
                            agent_offline_memory[h] = True
                    except Exception:
                        pass
                except Exception as e:
                    print(f"[REBUILD] agent {h} gagal: {e}")
                    continue

            try:
                c.execute("SELECT id, status FROM fiber_onts")
                for r in c.fetchall():
                    st = (r["status"] or "").strip().lower()
                    if st in ("normal", "warning", "critical", "overload", "unknown", "stale"):
                        fiber_alarm_memory[r["id"]] = st
            except Exception as e:
                print(f"[REBUILD] fiber gagal: {e}")
        finally:
            conn.close()
    except Exception as e:
        print(f"[REBUILD] gagal: {e}")

rebuild_alarm_memory()


SCHEDULER_ENABLED = os.environ.get("NMS_DISABLE_SCHEDULER", "0") != "1"
try:
    LOCAL_TZ = ZoneInfo("Asia/Jakarta")
except Exception:
    LOCAL_TZ = None

scheduler = BackgroundScheduler(timezone=LOCAL_TZ)
if SCHEDULER_ENABLED:
    _job_defaults = {"max_instances": 1, "coalesce": True, "misfire_grace_time": 120}
    scheduler.add_job(func=check_network,          trigger="interval", seconds=30, **_job_defaults)
    scheduler.add_job(func=check_services,         trigger="interval", seconds=30, **_job_defaults)
    scheduler.add_job(func=check_agent_heartbeat,  trigger="interval", seconds=60, **_job_defaults)
    scheduler.add_job(func=poll_snmp_bandwidth,    trigger="interval", seconds=30, **_job_defaults)
    scheduler.add_job(func=poll_mikrotik_health,   trigger="interval", seconds=60, **_job_defaults)
    scheduler.add_job(func=poll_mikrotik_ifaces,    trigger="interval", seconds=60, **_job_defaults)
    scheduler.add_job(func=poll_fiber_monitor,     trigger="interval", seconds=120, **_job_defaults)
    scheduler.add_job(func=poll_fiber_snmp,        trigger="interval", seconds=300, **_job_defaults)
    scheduler.add_job(func=check_ssl_expiry,       trigger="interval", hours=6, **_job_defaults)
    scheduler.add_job(func=send_heartbeat,         trigger="cron",     hour=8, minute=0, **_job_defaults)
    scheduler.add_job(func=send_fiber_summary,    trigger="cron",     hour=8, minute=5, **_job_defaults)
    scheduler.add_job(func=poll_mt_backups,       trigger="cron",     hour=2, minute=0, **_job_defaults)
    scheduler.add_job(func=cleanup_old_data,       trigger="cron",     hour=0, minute=0, **_job_defaults)
    scheduler.add_job(func=backup_database,        trigger="cron",     hour=0, minute=5, **_job_defaults)
    try:
        scheduler.start()
    except Exception as e:
        print(f"[WARN] scheduler gagal start: {e}")


if SCHEDULER_ENABLED:
    send_startup_alert()


_manual_svc_lock = threading.Lock()
_last_manual_svc = 0.0

def trigger_manual_service_check():
    global _last_manual_svc
    with _manual_svc_lock:
        if time.time() - _last_manual_svc < 60:
            return
        _last_manual_svc = time.time()
    threading.Thread(target=check_services, daemon=True).start()


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("index"))
    error = None
    if request.method == "POST":
        client_ip = get_client_ip()
        now_ts = time.time()
        _prune_login_failures(now_ts)
        fails, blocked_until, _ = login_failures.get(client_ip, (0, 0, 0))
        if now_ts < blocked_until:
            error = f"Terlalu banyak percobaan gagal. Coba lagi {int(blocked_until - now_ts)} detik."
            return render_template("login.html", error=error)
        username = request.form.get("username", "").strip()[:50]
        password = request.form.get("password", "")[:200]
        if _verify_admin(username, password):
            login_failures.pop(client_ip, None)
            user = AdminUser(username=username)
            remember = request.form.get("remember") == "1"
            login_user(user, remember=remember, duration=timedelta(days=7))
            session["admin_v"] = _get_session_version()
            try:
                audit(user.username, "auth.login", f"from {client_ip}")
            except Exception:
                pass
            next_page = request.args.get("next")


            if not next_page or not next_page.startswith("/") or next_page.startswith("//") or "\\" in next_page:
                next_page = None
            return redirect(next_page or url_for("index"))
        else:
            fails += 1
            try:
                log_system_event("AUDIT", username or "unknown",
                                 f"auth.failed from {client_ip} ({fails}x)")
            except Exception:
                pass
            if fails >= 5:
                login_failures[client_ip] = (0, now_ts + 60, now_ts)
                error = "Terlalu banyak percobaan gagal. Diblokir 60 detik."
            else:
                login_failures[client_ip] = (fails, 0, now_ts)
                error = "Username atau password salah."
    return render_template("login.html", error=error)

@app.route("/logout", methods=["POST"])
@login_required
def logout():
    try:
        audit(current_user.username, "auth.logout", "")
    except Exception:
        pass
    logout_user()
    session.pop("admin_v", None)
    return redirect(url_for("login"))


@app.route("/")
@login_required
def index():
    return render_template("index.html")


@app.route("/host/<path:ip>")
@login_required
def host_detail(ip):
    conn, c = get_db()
    c.execute("SELECT ip, alias FROM hosts WHERE ip=?", (ip,))
    row = c.fetchone()
    conn.close()
    if not row:
        return redirect(url_for("index"))
    return render_template("host_detail.html", host_ip=row["ip"], host_alias=row["alias"] or row["ip"])


@app.route("/api/host/<path:ip>/history")
@login_required
def api_host_history(ip):
    try:
        hours = int(request.args.get("hours", 24))
    except (ValueError, TypeError):
        return jsonify({"error": "hours harus angka 1-168"}), 400
    hours = max(1, min(hours, 168))
    metric = request.args.get("metric", "latency")

    conn, c = get_db()

    if metric == "latency":
        c.execute(
            "SELECT timestamp, latency as val FROM ping_logs "
            "WHERE host=? AND timestamp > datetime('now','localtime',?) ORDER BY id ASC",
            (ip, f"-{hours} hours")
        )
        rows = c.fetchall()
        labels = [r["timestamp"] for r in rows]
        values = [round(r["val"], 2) if r["val"] is not None and r["val"] != -1 else None for r in rows]
    else:
        valid_metrics = {"cpu": "cpu_percent", "ram": "ram_percent", "disk": "disk_percent",
                         "net_in": "net_in", "net_out": "net_out"}
        col = valid_metrics.get(metric, "cpu_percent")

        extra = " AND cpu_percent IS NOT NULL" if col in ("cpu_percent", "ram_percent", "disk_percent") else ""
        c.execute(
            f"SELECT timestamp, {col} as val FROM agent_metrics "
            f"WHERE host=? AND timestamp > datetime('now','localtime','-{hours} hours'){extra} ORDER BY id ASC",
            (ip,)
        )
        rows = c.fetchall()
        labels = [r["timestamp"] for r in rows]
        values = [round(r["val"] or 0, 2) for r in rows]

    conn.close()
    return jsonify({"labels": labels, "values": values, "metric": metric, "hours": hours})


@app.route("/api/host/<path:ip>/stats")
@login_required
def api_host_stats(ip):
    conn, c = get_db()


    c.execute("""
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN latency != -1 THEN 1 ELSE 0 END) AS up_count,
            AVG(CASE WHEN latency != -1 THEN latency END) AS avg_ms,
            MIN(CASE WHEN latency != -1 THEN latency END) AS min_ms,
            MAX(CASE WHEN latency != -1 THEN latency END) AS max_ms,
            AVG(packet_loss) AS avg_loss
        FROM ping_logs
        WHERE host=? AND timestamp > datetime('now','localtime','-24 hours')
    """, (ip,))
    pr = c.fetchone()


    c.execute("SELECT latency FROM ping_logs WHERE host=? ORDER BY id DESC LIMIT 1", (ip,))
    latest = c.fetchone()


    c.execute("""
        SELECT cpu_percent, ram_percent, disk_percent, net_in, net_out, timestamp
        FROM agent_metrics WHERE host=? AND cpu_percent IS NOT NULL ORDER BY id DESC LIMIT 1
    """, (ip,))
    ag = c.fetchone()

    net_row = None
    if ag is None:
        c.execute("""
            SELECT net_in, net_out, timestamp FROM agent_metrics
            WHERE host=? ORDER BY id DESC LIMIT 1
        """, (ip,))
        net_row = c.fetchone()

    conn.close()
    total = pr["total"] or 0
    up_count = pr["up_count"] or 0
    is_pending = (total == 0 and latest is None)
    has_latest = latest is not None and latest["latency"] is not None
    is_up = bool(has_latest and latest["latency"] != -1)
    return jsonify({
        "ip": ip,
        "uptime_pct": round((up_count / total * 100), 1) if total else None,
        "avg_ms": round(pr["avg_ms"], 2) if pr["avg_ms"] is not None else None,
        "min_ms": round(pr["min_ms"], 2) if pr["min_ms"] is not None else None,
        "max_ms": round(pr["max_ms"], 2) if pr["max_ms"] is not None else None,
        "avg_loss": round(pr["avg_loss"], 1) if pr["avg_loss"] is not None else None,
        "latest_ms": round(latest["latency"], 2) if has_latest and latest["latency"] != -1 else None,
        "is_up": is_up,
        "is_pending": is_pending,
        "agent": {
            "cpu": round(ag["cpu_percent"] or 0, 1),
            "ram": round(ag["ram_percent"] or 0, 1),
            "disk": round(ag["disk_percent"] or 0, 1),
            "net_in": round(ag["net_in"] or 0, 2),
            "net_out": round(ag["net_out"] or 0, 2),
            "last_seen": ag["timestamp"]
        } if ag else ({
            "cpu": 0, "ram": 0, "disk": 0,
            "net_in": round(net_row["net_in"] or 0, 2),
            "net_out": round(net_row["net_out"] or 0, 2),
            "last_seen": net_row["timestamp"],
            "snmp_only": True
        } if net_row else None)
    })


SNMP_PROFILES = ("auto", "mikrotik", "generic")


def _parse_snmp_fields(data):
    """Validasi profil + OID override SNMP. Kembalikan (dict, err)."""
    profile = str(data.get("snmp_profile") or "auto").strip().lower()[:16]
    if profile not in SNMP_PROFILES:
        return None, "snmp_profile harus auto/mikrotik/generic"
    oids = {}
    for key in ("cpu_oid", "mem_oid", "storage_oid", "temp_oid"):
        raw = str(data.get(key) or "").strip()
        if raw and not _valid_oid(raw):
            return None, f"{key} bukan OID valid (contoh: 1.3.6.1.4.1.14988.1.1.3.11.0)"
        oids[key] = raw[:128]
    return {"snmp_profile": profile, **oids}, None


def _parse_ssh_fields(data):
    """Validasi kredensial SSH + flag backup. Hanya key yang ADA divalidasi.

    Kembalikan (dict, err). Password tak pernah dikembalikan ke UI.
    """
    out = {}
    if "ssh_user" in data:
        out["ssh_user"] = str(data.get("ssh_user") or "").strip()[:64]
    if "ssh_pass" in data:
        out["ssh_pass"] = str(data.get("ssh_pass") or "")[:128]
    if "ssh_port" in data:
        try:
            port = int(data.get("ssh_port", 22))
        except (ValueError, TypeError):
            return None, "ssh_port harus angka 1-65535"
        if not 1 <= port <= 65535:
            return None, "ssh_port harus 1-65535"
        out["ssh_port"] = port
    if "backup_enable" in data:
        v = data.get("backup_enable")
        if isinstance(v, str):
            v = v.strip().lower() in ("1", "true", "ya", "yes", "on")
        out["backup_enable"] = 1 if v else 0
    return out, None


@app.route("/api/hosts", methods=["GET"])
@api_login_required
def api_get_hosts():
    conn, c = get_db()
    c.execute("SELECT id, ip, snmp_community, if_index, alias, category,"
              " snmp_profile, cpu_oid, mem_oid, storage_oid, temp_oid,"
              " ssh_user, ssh_port, backup_enable, backup_last, backup_ok,"
              " CASE WHEN ssh_pass IS NOT NULL AND ssh_pass != '' THEN 1 ELSE 0 END AS ssh_pass_set"
              " FROM hosts ORDER BY id ASC")
    hosts = []
    for r in c.fetchall():
        hosts.append({"id": r["id"], "ip": r["ip"],
                      "snmp_community": r["snmp_community"] or "",
                      "if_index": r["if_index"] or 1,
                      "alias": r["alias"] or "",
                      "category": r["category"] or "Uncategorized",
                      "snmp_profile": (r["snmp_profile"] or "auto")
                      if (r["snmp_profile"] or "auto") in SNMP_PROFILES else "auto",
                      "cpu_oid": r["cpu_oid"] or "", "mem_oid": r["mem_oid"] or "",
                      "storage_oid": r["storage_oid"] or "", "temp_oid": r["temp_oid"] or "",
                      "ssh_user": r["ssh_user"] or "",
                      "ssh_pass_set": bool(r["ssh_pass_set"]),
                      "ssh_port": r["ssh_port"] or 22,
                      "backup_enable": bool(r["backup_enable"]),
                      "backup_last": r["backup_last"] or "",
                      "backup_ok": bool(r["backup_ok"])})
    conn.close()
    return jsonify(hosts)

@app.route("/api/hosts", methods=["POST"])
@api_login_required
def api_add_host():
    data = request.get_json(silent=True) or {}
    ip = str(data.get("ip") or "").strip()
    alias = str(data.get("alias") or "").strip()
    category = str(data.get("category") or "").strip() or "Uncategorized"
    snmp_community = str(data.get("snmp_community") or "").strip()
    snmp_vals, err = _parse_snmp_fields(data)
    if err:
        return jsonify({"error": err}), 400
    assert snmp_vals is not None
    ssh_vals, err = _parse_ssh_fields(data)
    if err:
        return jsonify({"error": err}), 400
    assert ssh_vals is not None
    try:
        if_index = int(data.get("if_index", 1))
    except (ValueError, TypeError):
        if_index = 1
    if if_index < 1:
        if_index = 1

    if not ip:
        return jsonify({"error": "IP required"}), 400

    ipv4_re = re.compile(r"^(\d{1,3}\.){3}\d{1,3}$")
    hostname_re = re.compile(r"^[a-zA-Z0-9]([a-zA-Z0-9.\-]{0,253}[a-zA-Z0-9])?$")
    if ipv4_re.match(ip):
        try:
            if any(int(p) > 255 for p in ip.split(".")):
                return jsonify({"error": "Format IP tidak valid"}), 400
        except ValueError:
            return jsonify({"error": "Format IP tidak valid"}), 400
    elif not hostname_re.match(ip):
        return jsonify({"error": "IP/hostname tidak valid"}), 400
    conn, c = get_db()
    try:
        c.execute("SELECT COUNT(*) AS cnt FROM hosts")
        if (c.fetchone()["cnt"] or 0) >= MAX_HOSTS:
            conn.close()
            return jsonify({"error": f"Batas maksimum {MAX_HOSTS} host tercapai"}), 400
        c.execute("INSERT INTO hosts (ip, snmp_community, if_index, alias, category,"
                  " snmp_profile, cpu_oid, mem_oid, storage_oid, temp_oid,"
                  " ssh_user, ssh_pass, ssh_port, backup_enable)"
                  " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                  (ip, snmp_community, if_index, alias, category,
                   snmp_vals["snmp_profile"], snmp_vals["cpu_oid"], snmp_vals["mem_oid"],
                   snmp_vals["storage_oid"], snmp_vals["temp_oid"],
                   ssh_vals.get("ssh_user", ""), ssh_vals.get("ssh_pass", ""),
                   ssh_vals.get("ssh_port", 22), ssh_vals.get("backup_enable", 0)))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        conn.close()
        return jsonify({"error": "Host sudah ada"}), 400
    conn.close()
    try:
        audit(current_user.username, "host.create", f"{ip} alias={alias} category={category}")
    except Exception:
        pass
    return jsonify({"status": "success"})

@app.route("/api/hosts/<path:ip>", methods=["DELETE"])
@api_login_required
def api_delete_host(ip):
    ip = (ip or "").strip()
    with db_lock:
        conn, c = get_db()
        try:
            c.execute("DELETE FROM hosts WHERE ip=?", (ip,))
            deleted = c.rowcount
            conn.commit()
        finally:
            conn.close()


        global status_memory, down_since, agent_status_memory, agent_offline_memory, last_down_telegram
        for mem in (status_memory, down_since, agent_status_memory, agent_offline_memory,
                    last_down_telegram, mt_alarm_memory, mt_is_mikrotik, mt_sysup,
                    snmp_state):
            try:
                if ip in mem:
                    del mem[ip]
            except Exception:
                pass
        for key in [k for k in list(mt_iface_oper) if k[0] == ip]:
            mt_iface_oper.pop(key, None)
        for key in [k for k in list(iface_state) if k[0] == ip]:
            iface_state.pop(key, None)
        for key in [k for k in list(mt_iface_tg) if k[0] == ip]:
            mt_iface_tg.pop(key, None)
        for key in [k for k in list(mt_iface_flaps) if k[0] == ip]:
            mt_iface_flaps.pop(key, None)
        # lock sudah dipegang blok luar -> jangan lock ulang (deadlock)
        try:
            conn2, c2 = get_db()
            try:
                c2.execute("DELETE FROM snmp_interfaces WHERE host=?", (ip,))
                c2.execute("DELETE FROM mt_backups WHERE host=?", (ip,))
                conn2.commit()
            finally:
                try:
                    conn2.close()
                except Exception:
                    pass
        except Exception:
            pass

    if not deleted:
        return jsonify({"error": "Host tidak ditemukan"}), 404
    try:
        audit(current_user.username, "host.delete", ip)
    except Exception:
        pass
    return jsonify({"status": "success"})


@app.route("/api/hosts/<path:ip>/alias", methods=["PATCH"])
@api_login_required
def api_update_host_alias(ip):
    ip = (ip or "").strip()
    data = request.get_json(silent=True) or {}
    alias = str(data.get("alias") or "").strip()
    raw_category = data.get("category")
    category = str(raw_category or "").strip() or "Uncategorized" if raw_category is not None else None
    conn, c = get_db()
    if category is not None:
        c.execute("UPDATE hosts SET alias=?, category=? WHERE ip=?", (alias, category, ip))
    else:
        c.execute("UPDATE hosts SET alias=? WHERE ip=?", (alias, ip))
    updated = c.rowcount
    conn.commit()
    conn.close()
    if not updated:
        return jsonify({"error": "Host tidak ditemukan"}), 404
    try:
        audit(current_user.username, "host.rename", f"{ip} alias={alias} category={category}")
    except Exception:
        pass
    return jsonify({"status": "success", "alias": alias, "category": category})


@app.route("/api/hosts/<path:ip>/snmp", methods=["PATCH"])
@api_login_required
def api_update_host_snmp(ip):
    ip = (ip or "").strip()
    data = request.get_json(silent=True) or {}
    snmp_vals, err = _parse_snmp_fields(data)
    if err:
        return jsonify({"error": err}), 400
    assert snmp_vals is not None
    ssh_vals, err = _parse_ssh_fields(data)
    if err:
        return jsonify({"error": err}), 400
    assert ssh_vals is not None
    sets, params = [], []
    if "snmp_community" in data:
        sets.append("snmp_community=?")
        params.append(str(data.get("snmp_community") or "").strip()[:128])
    if "if_index" in data:
        try:
            idx = int(data.get("if_index", 1))
        except (ValueError, TypeError):
            return jsonify({"error": "if_index harus angka >= 1"}), 400
        if idx < 1:
            return jsonify({"error": "if_index harus angka >= 1"}), 400
        sets.append("if_index=?")
        params.append(idx)
    for key in ("snmp_profile", "cpu_oid", "mem_oid", "storage_oid", "temp_oid"):
        if key in data:
            sets.append(f"{key}=?")
            params.append(snmp_vals[key])
    for key in ("ssh_user", "ssh_pass", "ssh_port", "backup_enable"):
        if key in ssh_vals:
            sets.append(f"{key}=?")
            params.append(ssh_vals[key])
    if not sets:
        return jsonify({"error": "Tidak ada field SNMP yang dikirim"}), 400
    conn, c = get_db()
    c.execute(f"UPDATE hosts SET {', '.join(sets)} WHERE ip=?", (*params, ip))
    updated = c.rowcount
    conn.commit()
    conn.close()
    if not updated:
        return jsonify({"error": "Host tidak ditemukan"}), 404
    try:
        mt_alarm_memory.pop(ip, None)
    except Exception:
        pass
    try:
        _changed = [k for k in list(snmp_vals) + list(ssh_vals)
                    if k in data and k != "ssh_pass"]
        if "ssh_pass" in data:
            _changed.append("ssh_pass=***")
        audit(current_user.username, "host.snmp", f"{ip} {','.join(_changed)}")
    except Exception:
        pass
    return jsonify({"status": "success"})


@app.route("/api/settings", methods=["GET"])
@api_login_required
def api_get_settings():
    return jsonify({
        "cpu_threshold": get_setting("cpu_threshold", 85.0),
        "ram_threshold": get_setting("ram_threshold", 90.0),
        "disk_threshold": get_setting("disk_threshold", 90.0),
        "fiber_rx_overload": get_setting("fiber_rx_overload", FIBER_RX_OVERLOAD),
        "fiber_rx_warn": get_setting("fiber_rx_warn", FIBER_RX_WARN),
        "fiber_rx_crit": get_setting("fiber_rx_crit", FIBER_RX_CRIT),
        "fiber_rx_target": get_setting("fiber_rx_target", FIBER_RX_TARGET),
        "fiber_tx_min": get_setting("fiber_tx_min", FIBER_TX_MIN),
        "fiber_tx_max": get_setting("fiber_tx_max", FIBER_TX_MAX),
        "fiber_degrade_db": get_setting("fiber_degrade_db", FIBER_DEGRADE_DB),
        "fiber_degrade_days": get_setting("fiber_degrade_days", FIBER_DEGRADE_DAYS),
        "fiber_stale_min": get_setting("fiber_stale_min", FIBER_STALE_MIN),
        "fiber_flap_flips": get_setting("fiber_flap_flips", FIBER_FLAP_FLIPS),
        "fiber_flap_hours": get_setting("fiber_flap_hours", FIBER_FLAP_HOURS),
        "fiber_parent_min": get_setting("fiber_parent_min", FIBER_PARENT_MIN),
        "temp_threshold": get_setting("temp_threshold", 60.0),
        "temp_crit": get_setting("temp_crit", 75.0),
        "mt_cpu_oid": get_setting("mt_cpu_oid", MT_DEFAULT_OIDS["cpu"], type_cast=str),
        "mt_mem_oid": get_setting("mt_mem_oid", MT_DEFAULT_OIDS["mem"], type_cast=str),
        "mt_storage_oid": get_setting("mt_storage_oid", MT_DEFAULT_OIDS["storage"], type_cast=str),
        "mt_temp_oid": get_setting("mt_temp_oid", MT_DEFAULT_OIDS["temp"], type_cast=str),
        "mt_temp_div": get_setting("mt_temp_div", 10, type_cast=float),
        "mt_stale_min": get_setting("mt_stale_min", MT_STALE_MIN),
        "mt_backup_keep": get_setting("mt_backup_keep", 10),
    })

FIBER_SETTING_RANGES = {
    "fiber_rx_overload": (-40.0, 10.0),
    "fiber_rx_warn": (-40.0, 10.0),
    "fiber_rx_crit": (-40.0, 10.0),
    "fiber_rx_target": (-40.0, 10.0),
    "fiber_tx_min": (-10.0, 10.0),
    "fiber_tx_max": (-10.0, 10.0),
}

@app.route("/api/settings", methods=["POST"])
@api_login_required
def api_save_settings():
    data = request.get_json(silent=True) or {}
    vals = {}
    for key in ("cpu_threshold", "ram_threshold", "disk_threshold"):
        v = data.get(key)
        if v is None:
            continue
        try:
            v = float(v)
        except (ValueError, TypeError):
            return jsonify({"error": f"{key} harus angka 1-100"}), 400
        if not 1 <= v <= 100:
            return jsonify({"error": f"{key} harus 1-100"}), 400
        vals[key] = v
    for key, (lo, hi) in FIBER_SETTING_RANGES.items():
        v = data.get(key)
        if v is None:
            continue
        try:
            v = float(v)
        except (ValueError, TypeError):
            return jsonify({"error": f"{key} harus angka {lo}..{hi} dBm"}), 400
        if not lo <= v <= hi:
            return jsonify({"error": f"{key} harus {lo}..{hi} dBm"}), 400
        vals[key] = v
    v = data.get("fiber_degrade_db")
    if v is not None:
        try:
            v = float(v)
        except (ValueError, TypeError):
            return jsonify({"error": "fiber_degrade_db harus angka 0.5-10 dB"}), 400
        if not 0.5 <= v <= 10:
            return jsonify({"error": "fiber_degrade_db harus 0.5-10 dB"}), 400
        vals["fiber_degrade_db"] = v
    for key, lo, hi in (("fiber_degrade_days", 1, 30), ("fiber_stale_min", 10, 10080)):
        v = data.get(key)
        if v is None:
            continue
        try:
            v = int(float(v))
        except (ValueError, TypeError):
            return jsonify({"error": f"{key} harus angka {lo}-{hi}"}), 400
        if not lo <= v <= hi:
            return jsonify({"error": f"{key} harus {lo}-{hi}"}), 400
        vals[key] = v
    for key, lo, hi in (("fiber_flap_flips", 2, 20), ("fiber_flap_hours", 1, 72)):
        v = data.get(key)
        if v is None:
            continue
        try:
            v = int(float(v))
        except (ValueError, TypeError):
            return jsonify({"error": f"{key} harus angka {lo}-{hi}"}), 400
        if not lo <= v <= hi:
            return jsonify({"error": f"{key} harus {lo}-{hi}"}), 400
        vals[key] = v
    v = data.get("fiber_parent_min")
    if v is not None:
        try:
            v = int(float(v))
        except (ValueError, TypeError):
            return jsonify({"error": "fiber_parent_min harus angka 2-50"}), 400
        if not 2 <= v <= 50:
            return jsonify({"error": "fiber_parent_min harus 2-50 ONT"}), 400
        vals["fiber_parent_min"] = v
    for key in ("temp_threshold", "temp_crit"):
        v = data.get(key)
        if v is None:
            continue
        try:
            v = float(v)
        except (ValueError, TypeError):
            return jsonify({"error": f"{key} harus angka 0-150 °C"}), 400
        if not 0 <= v <= 150:
            return jsonify({"error": f"{key} harus 0-150 °C"}), 400
        vals[key] = v
    for key in ("mt_cpu_oid", "mt_mem_oid", "mt_storage_oid", "mt_temp_oid"):
        v = data.get(key)
        if v is None:
            continue
        v = _valid_oid(v)
        if not v:
            return jsonify({"error": f"{key} bukan OID valid"}), 400
        vals[key] = v
    v = data.get("mt_temp_div")
    if v is not None:
        try:
            v = float(v)
        except (ValueError, TypeError):
            return jsonify({"error": "mt_temp_div harus angka 1-1000"}), 400
        if not 1 <= v <= 1000:
            return jsonify({"error": "mt_temp_div harus 1-1000"}), 400
        vals["mt_temp_div"] = v
    v = data.get("mt_stale_min")
    if v is not None:
        try:
            v = int(float(v))
        except (ValueError, TypeError):
            return jsonify({"error": "mt_stale_min harus angka 3-1440"}), 400
        if not 3 <= v <= 1440:
            return jsonify({"error": "mt_stale_min harus 3-1440 menit"}), 400
        vals["mt_stale_min"] = v
    v = data.get("mt_backup_keep")
    if v is not None:
        try:
            v = int(float(v))
        except (ValueError, TypeError):
            return jsonify({"error": "mt_backup_keep harus angka 3-50"}), 400
        if not 3 <= v <= 50:
            return jsonify({"error": "mt_backup_keep harus 3-50 versi"}), 400
        vals["mt_backup_keep"] = v
    # konsistensi: crit < warn <= overload, tx_min <= tx_max, temp warn < crit
    merged = {k: get_setting(k, d) for k, d in
              [("fiber_rx_overload", FIBER_RX_OVERLOAD), ("fiber_rx_warn", FIBER_RX_WARN),
               ("fiber_rx_crit", FIBER_RX_CRIT), ("fiber_rx_target", FIBER_RX_TARGET),
               ("fiber_tx_min", FIBER_TX_MIN),
               ("fiber_tx_max", FIBER_TX_MAX), ("temp_threshold", 60.0),
               ("temp_crit", 75.0)]}
    merged.update({k: v for k, v in vals.items() if k in merged})
    if not (merged["fiber_rx_crit"] < merged["fiber_rx_warn"] <= merged["fiber_rx_overload"]):
        return jsonify({"error": "Harus: crit < warn <= overload (mis. -27 < -25 <= -8)"}), 400
    if not (merged["fiber_rx_crit"] <= merged["fiber_rx_target"] <= merged["fiber_rx_overload"]):
        return jsonify({"error": "fiber_rx_target harus di antara crit..overload (mis. -27..-8)"}), 400
    if not (merged["fiber_tx_min"] <= merged["fiber_tx_max"]):
        return jsonify({"error": "fiber_tx_min harus <= fiber_tx_max"}), 400
    if not (merged["temp_threshold"] < merged["temp_crit"]):
        return jsonify({"error": "temp_threshold harus < temp_crit"}), 400

    if not vals:
        return jsonify({"error": "Tidak ada pengaturan yang dikirim"}), 400
    conn, c = get_db()
    for key, v in vals.items():


        c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(v)))
    conn.commit()
    conn.close()
    try:
        audit(current_user.username, "settings.update",
              " ".join(f"{k}={v}" for k, v in vals.items()))
    except Exception:
        pass
    return jsonify({"status": "success"})


@app.route("/api/settings/password", methods=["POST"])
@api_login_required
def api_change_password():
    from werkzeug.security import generate_password_hash
    data = request.get_json(silent=True) or {}
    cur = str(data.get("current_password") or "")[:200]
    new_user = str(data.get("new_username") or _get_admin_username()).strip()[:50] or _get_admin_username()
    new_pass = str(data.get("new_password") or "")
    if not _verify_admin(current_user.username, cur):
        return jsonify({"error": "Password saat ini salah"}), 400
    if len(new_pass) < 8 or len(new_pass) > 200:
        return jsonify({"error": "Password baru minimal 8 karakter"}), 400
    if not re.match(r"^[a-zA-Z0-9_.\-]{3,50}$", new_user):
        return jsonify({"error": "Username 3-50 karakter (huruf/angka/_.-)"}), 400
    conn, c = get_db()
    c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('admin_user', ?)", (new_user,))
    c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('admin_pass_hash', ?)",
              (generate_password_hash(new_pass),))
    conn.commit()
    conn.close()

    _bump_session_version()
    try:
        session["admin_v"] = _get_session_version()
    except Exception:
        pass
    try:
        audit(current_user.username, "auth.password_change", f"new_user={new_user}")
    except Exception:
        pass
    return jsonify({"status": "success"})


@app.route("/api/agent/report", methods=["POST"])
def agent_report():
    global agent_status_memory, agent_offline_memory


    if AGENT_API_KEY:
        import hmac
        provided = request.headers.get("X-API-Key") or ""
        if not provided or not hmac.compare_digest(provided, AGENT_API_KEY):
            return jsonify({"error": "Forbidden: API key agent salah"}), 403

    data = request.get_json(silent=True) or {}

    def _to_float(v, name, lo=0.0, hi=100.0):
        try:
            f = float(v)
        except (ValueError, TypeError):
            raise ValueError(f"{name} harus angka")
        if not (lo <= f <= hi):
            raise ValueError(f"{name} harus {lo}-{hi}")
        return f

    raw_host = str(data.get("host") or "").strip()[:255]
    if not raw_host:
        return jsonify({"error": "Host required"}), 400

    _ipv4_re = re.compile(r"^(\d{1,3}\.){3}\d{1,3}$")
    _hn_re = re.compile(r"^[a-zA-Z0-9]([a-zA-Z0-9.\-]{0,253}[a-zA-Z0-9])?$")
    if _ipv4_re.match(raw_host):
        try:
            if any(int(p) > 255 for p in raw_host.split(".")):
                return jsonify({"error": "Format host tidak valid"}), 400
        except ValueError:
            return jsonify({"error": "Format host tidak valid"}), 400
    elif not _hn_re.match(raw_host):
        return jsonify({"error": "Format host tidak valid"}), 400
    host = raw_host


    try:
        _hc, _cc = get_db()
        _cc.execute("SELECT 1 FROM hosts WHERE ip=?", (host,))
        _known = _cc.fetchone() is not None
        _hc.close()
    except Exception:

        return jsonify({"error": "Database sibuk, coba lagi"}), 503
    if not _known:
        return jsonify({"error": "Host belum terdaftar. Tambahkan dulu di dashboard."}), 404
    try:
        cpu = _to_float(data.get("cpu", 0.0), "cpu", 0, 100)
        ram = _to_float(data.get("ram", 0.0), "ram", 0, 100)
        disk = _to_float(data.get("disk", 0.0), "disk", 0, 100)
        net_in = _to_float(data.get("net_in", 0.0), "net_in", 0, 100000)
        net_out = _to_float(data.get("net_out", 0.0), "net_out", 0, 100000)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")


    cpu_thresh = get_setting("cpu_threshold", 85.0)
    ram_thresh = get_setting("ram_threshold", 90.0)
    disk_thresh = get_setting("disk_threshold", 90.0)

    cpu_clear = max(cpu_thresh - HYSTERESIS, 0)
    ram_clear = max(ram_thresh - HYSTERESIS, 0)
    disk_clear = max(disk_thresh - HYSTERESIS, 0)


    telegram_queue = []
    log_queue = []
    with db_lock:
        if agent_offline_memory.get(host, False):
            agent_offline_memory[host] = False
            telegram_queue.append(
                f"✅ *AGENT KEMBALI ONLINE*\nHost: `{host}`\nBerhasil terhubung kembali ke Dashboard.\nWaktu: {timestamp}"
            )
            log_queue.append(("AGENT_ONLINE", "Agent kembali terhubung"))


        if host not in agent_status_memory:
            agent_status_memory[host] = {'cpu': False, 'ram': False, 'disk': False}


        if cpu > cpu_thresh and not agent_status_memory[host]['cpu']:
            agent_status_memory[host]['cpu'] = True
            telegram_queue.append(f"⚠️ *HIGH CPU ALERT*\nHost: `{host}`\nCPU: *{cpu}%* (Batas: {cpu_thresh}%)\nWaktu: {timestamp}")
            log_queue.append(("HIGH_CPU", f"{cpu}% (Batas: {cpu_thresh}%)"))
        elif cpu <= cpu_clear and agent_status_memory[host]['cpu']:
            agent_status_memory[host]['cpu'] = False
            telegram_queue.append(f"✅ *CPU NORMAL*\nHost: `{host}`\nCPU: {cpu}%\nWaktu: {timestamp}")
            log_queue.append(("CPU_NORMAL", f"Kembali normal: {cpu}%"))


        if ram > ram_thresh and not agent_status_memory[host]['ram']:
            agent_status_memory[host]['ram'] = True
            telegram_queue.append(f"⚠️ *HIGH RAM ALERT*\nHost: `{host}`\nRAM: *{ram}%* (Batas: {ram_thresh}%)\nWaktu: {timestamp}")
            log_queue.append(("HIGH_RAM", f"{ram}% (Batas: {ram_thresh}%)"))
        elif ram <= ram_clear and agent_status_memory[host]['ram']:
            agent_status_memory[host]['ram'] = False
            telegram_queue.append(f"✅ *RAM NORMAL*\nHost: `{host}`\nRAM: {ram}%\nWaktu: {timestamp}")
            log_queue.append(("RAM_NORMAL", f"Kembali normal: {ram}%"))


        if disk > disk_thresh and not agent_status_memory[host].get('disk'):
            agent_status_memory[host]['disk'] = True
            telegram_queue.append(f"⚠️ *HIGH DISK ALERT*\nHost: `{host}`\nDISK Penuh: *{disk}%* (Batas: {disk_thresh}%)\nWaktu: {timestamp}")
            log_queue.append(("HIGH_DISK", f"{disk}% (Batas: {disk_thresh}%)"))
        elif disk <= disk_clear and agent_status_memory[host].get('disk'):
            agent_status_memory[host]['disk'] = False
            telegram_queue.append(f"✅ *DISK NORMAL*\nHost: `{host}`\nKapasitas terpakai: {disk}%\nWaktu: {timestamp}")
            log_queue.append(("DISK_NORMAL", f"Kembali normal: {disk}%"))

        conn, c = get_db()
        try:
            c.execute(
                "INSERT INTO agent_metrics (host, cpu_percent, ram_percent, disk_percent, net_in, net_out, timestamp, source) VALUES (?, ?, ?, ?, ?, ?, ?, 'agent')",
                (host, cpu, ram, disk, net_in, net_out, timestamp)
            )
            for ev, msg in log_queue:
                _insert_system_log(c, ev, host, msg, timestamp)
            _commit_with_retry(conn)
        except sqlite3.OperationalError as e:
            print(f"[DB LOCK] agent_report {host} gagal: {e}")
            try:
                conn.rollback()
            except Exception:
                pass
            return jsonify({"error": "Database sibuk, coba lagi"}), 503
        finally:
            conn.close()
    for msg in telegram_queue:
        try:
            send_telegram_alert(msg)
        except Exception as e:
            print(f"[WARN] telegram agent {host} gagal: {e}")
    return jsonify({"status": "success"})

def _align_series(per_host, label_fmt):
    try:
        stamps = sorted({ts for pts in per_host.values() for ts, _ in pts})
    except Exception:
        return [], {}
    idx = {ts: i for i, ts in enumerate(stamps)}
    labels = [label_fmt(ts) for ts in stamps]
    datasets = {}
    for host, pts in per_host.items():
        vals = [None] * len(stamps)
        for ts, v in pts:
            vals[idx[ts]] = v
        datasets[host] = vals
    return labels, datasets


@app.route("/api/agent/metrics")
@api_login_required
def get_agent_metrics():
    conn, c = get_db()
    metrics = {}
    for host in get_target_hosts():
        c.execute("""
            SELECT cpu_percent, ram_percent, disk_percent, net_in, net_out, timestamp FROM agent_metrics 
            WHERE host=? AND cpu_percent IS NOT NULL AND timestamp > datetime('now', 'localtime', '-5 minutes') 
            ORDER BY id DESC LIMIT 1
        """, (host,))
        row = c.fetchone()
        if row:
            metrics[host] = {
                "cpu": round(row["cpu_percent"] or 0, 1),
                "ram": round(row["ram_percent"] or 0, 1),
                "disk": round(row["disk_percent"] or 0, 1),
                "net_in": round(row["net_in"] or 0, 2),
                "net_out": round(row["net_out"] or 0, 2),
                "last_seen": row["timestamp"]
            }
    conn.close()
    return jsonify(metrics)


@app.route("/api/agent/history")
@api_login_required
def get_agent_history():
    try:
        hours = int(request.args.get("hours", 1))
    except (ValueError, TypeError):
        return jsonify({"error": "hours harus angka 1-168"}), 400
    hours = max(1, min(hours, 168))
    metric = request.args.get("metric", "cpu")

    valid_metrics = {"cpu": "cpu_percent", "ram": "ram_percent", "disk": "disk_percent",
                     "net_in": "net_in", "net_out": "net_out"}
    col = valid_metrics.get(metric, "cpu_percent")

    conn, c = get_db()
    try:

        extra = " AND cpu_percent IS NOT NULL" if col in ("cpu_percent", "ram_percent", "disk_percent") else ""
        per_host = {}
        for host in get_target_hosts():
            c.execute(f"""
                SELECT timestamp, {col} as val
                FROM agent_metrics
                WHERE host=? AND timestamp > datetime('now', 'localtime', '-{hours} hours'){extra}
                ORDER BY id ASC
            """, (host,))
            rows = c.fetchall()
            if rows:
                per_host[host] = [(r["timestamp"], round(r["val"] or 0, 2)) for r in rows]
    finally:
        conn.close()

    labels, datasets = _align_series(per_host, label_fmt=lambda t: t.split(" ")[1])
    return jsonify({"labels": labels, "datasets": datasets, "metric": metric})


@app.route("/api/metrics")
@api_login_required
def get_metrics():
    conn, c  = get_db()
    try:
        per_host = {}
        for host in get_target_hosts():
            c.execute(
                "SELECT timestamp, latency FROM ping_logs WHERE host=? ORDER BY id DESC LIMIT 10",
                (host,),
            )
            rows = list(reversed(c.fetchall()))
            if rows:
                per_host[host] = [(r["timestamp"], r["latency"]) for r in rows]
    finally:
        conn.close()
    labels, datasets = _align_series(per_host, label_fmt=lambda t: t.split(" ")[1])
    return jsonify({"labels": labels, "datasets": datasets})


@app.route("/api/history")
@api_login_required
def get_history():
    try:
        hours = int(request.args.get("hours", 1))
    except (ValueError, TypeError):
        return jsonify({"error": "hours harus angka 1-24"}), 400
    hours = max(1, min(hours, 24))
    conn, c  = get_db()
    try:
        per_host = {}
        for host in get_target_hosts():
            c.execute(
                "SELECT timestamp, latency FROM ping_logs "
                "WHERE host=? AND timestamp > datetime('now','localtime',?) ORDER BY id",
                (host, f"-{hours} hours"),
            )
            rows = c.fetchall()
            if rows:
                per_host[host] = [(r["timestamp"], r["latency"]) for r in rows]
    finally:
        conn.close()
    labels, datasets = _align_series(per_host, label_fmt=lambda t: t.split(" ")[1])
    return jsonify({"labels": labels, "datasets": datasets})


@app.route("/api/stats")
@api_login_required
def get_stats():
    conn, c = get_db()
    stats   = {}
    for host in get_target_hosts():
        c.execute("""
            SELECT
                COUNT(*)                                            AS total,
                SUM(CASE WHEN latency != -1 THEN 1 ELSE 0 END)    AS up_count,
                AVG(CASE WHEN latency != -1 THEN latency  END)     AS avg_ms,
                MIN(CASE WHEN latency != -1 THEN latency  END)     AS min_ms,
                MAX(CASE WHEN latency != -1 THEN latency  END)     AS max_ms,
                AVG(CASE WHEN latency != -1 THEN packet_loss END)  AS avg_loss
            FROM ping_logs
            WHERE host=? AND timestamp > datetime('now','localtime','-24 hours')
        """, (host,))
        r        = c.fetchone()
        total    = r["total"]    or 0
        up_count = r["up_count"] or 0
        if total == 0:

            stats[host] = {
                "uptime_pct": None,
                "avg_ms"    : None,
                "min_ms"    : None,
                "max_ms"    : None,
                "avg_loss"  : None,
                "is_down"   : False,
                "is_pending": True,
            }
        else:
            stats[host] = {
                "uptime_pct": round((up_count / total * 100), 1),
                "avg_ms"    : round(r["avg_ms"], 2) if r["avg_ms"] is not None else None,
                "min_ms"    : round(r["min_ms"], 2) if r["min_ms"] is not None else None,
                "max_ms"    : round(r["max_ms"], 2) if r["max_ms"] is not None else None,
                "avg_loss"  : round(r["avg_loss"], 1) if r["avg_loss"] is not None else None,
                "is_down"   : status_memory.get(host, False),
                "is_pending": False,
            }
    conn.close()
    return jsonify(stats)


@app.route("/api/events")
@api_login_required
def get_events():
    conn, c = get_db()
    try:
        c.execute("""
            SELECT id, host, started_at, resolved_at, duration_s, is_maintenance
            FROM down_events ORDER BY id DESC LIMIT 50
        """)
    except sqlite3.OperationalError:
        c.execute("""
            SELECT id, host, started_at, resolved_at, duration_s
            FROM down_events ORDER BY id DESC LIMIT 50
        """)
    events = []
    for r in c.fetchall():
        m, s = divmod(r["duration_s"] or 0, 60)
        try:
            is_maint = bool(r["is_maintenance"])
        except (KeyError, IndexError, TypeError):
            is_maint = False
        events.append({
            "id"         : r["id"],
            "host"       : r["host"],
            "started_at" : r["started_at"],
            "resolved_at": r["resolved_at"] or "Ongoing",
            "duration"   : f"{m}m {s}s" if r["duration_s"] else ("Ongoing" if not r["resolved_at"] else "—"),
            "status"     : "resolved" if r["resolved_at"] else "ongoing",
            "is_maintenance": is_maint,
        })
    conn.close()
    return jsonify(events)


@app.route("/api/events/<int:event_id>", methods=["DELETE"])
@api_login_required
def delete_event(event_id):
    with db_lock:
        conn, c = get_db()
        try:
            c.execute("SELECT host, resolved_at FROM down_events WHERE id=?", (event_id,))
            row = c.fetchone()
            if not row:
                return jsonify({"error": "Event tidak ditemukan"}), 404
            host = row["host"]
            was_ongoing = row["resolved_at"] is None
            c.execute("DELETE FROM down_events WHERE id=?", (event_id,))
            conn.commit()
            if was_ongoing:


                c.execute("SELECT 1 FROM down_events WHERE host=? AND resolved_at IS NULL LIMIT 1", (host,))
                still_ongoing = c.fetchone() is not None
                if not still_ongoing:
                    status_memory[host] = False
                    down_since.pop(host, None)
        finally:
            conn.close()
    try:
        audit(current_user.username, "events.delete", f"id={event_id} host={host}")
    except Exception:
        pass
    return jsonify({"status": "success"})


@app.route("/api/events/clear", methods=["POST"])
@api_login_required
def clear_events():
    with db_lock:
        conn, c = get_db()
        try:
            c.execute("DELETE FROM down_events")
            conn.commit()
        finally:
            conn.close()


        for h in list(status_memory.keys()):
            status_memory[h] = False
        down_since.clear()
    try:
        audit(current_user.username, "events.clear", "all")
    except Exception:
        pass
    return jsonify({"status": "success"})


@app.route("/api/maintenance", methods=["GET"])
@api_login_required
def api_maintenance_list():
    active_only = (request.args.get("active") or "").strip() == "1"
    host_filter = (request.args.get("host") or "").strip()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn, c = get_db()
    query = "SELECT id, host, start_at, end_at, reason, created_by, created_at FROM maintenance_windows WHERE 1=1"
    params = []
    if host_filter:
        query += " AND host=?"
        params.append(host_filter)
    if active_only:
        query += " AND start_at <= ? AND end_at >= ?"
        params.extend([now_str, now_str])
    query += " ORDER BY start_at ASC"
    c.execute(query, params)
    rows = []
    for r in c.fetchall():
        is_active = bool(r["start_at"] <= now_str <= r["end_at"])
        rows.append({**dict(r), "is_active": is_active})
    conn.close()
    return jsonify(rows)


@app.route("/api/maintenance", methods=["POST"])
@api_login_required
def api_maintenance_create():
    data = request.get_json(silent=True) or {}
    host = str(data.get("host") or "").strip()[:255]
    reason = str(data.get("reason") or "").strip()[:200]
    if not host:
        return jsonify({"error": "Host wajib diisi"}), 400
    start_dt = _parse_maint_time(str(data.get("start_at") or ""))
    end_dt = _parse_maint_time(str(data.get("end_at") or ""))
    if not start_dt or not end_dt:
        return jsonify({"error": "Format waktu harus YYYY-MM-DD HH:MM"}), 400
    if end_dt <= start_dt:
        return jsonify({"error": "Waktu selesai harus setelah mulai"}), 400
    if (end_dt - start_dt).total_seconds() > 30 * 86400:
        return jsonify({"error": "Durasi maintenance maksimal 30 hari"}), 400
    conn, c = get_db()
    try:
        c.execute("SELECT 1 FROM hosts WHERE ip=?", (host,))
        is_host = bool(c.fetchone())
        is_ont = False
        if not is_host:
            try:
                c.execute("SELECT 1 FROM fiber_onts WHERE ont_sn=?", (host,))
                is_ont = bool(c.fetchone())
            except sqlite3.OperationalError:
                pass
        # Maintenance hierarki fiber: "ODP:<nama>" / "OLT:<nama>" men-suppress
        # semua ONT di bawahnya (lihat _fiber_maintenance_info).
        lowered = host.lower()
        if not is_host and not is_ont and (lowered.startswith("odp:") or lowered.startswith("olt:")):
            scope, _, name = host.partition(":")
            name = name.strip()
            if not name:
                return jsonify({"error": "Format harus ODP:<nama> atau OLT:<nama>"}), 400
            try:
                if lowered.startswith("odp:"):
                    c.execute("SELECT name FROM odps WHERE name COLLATE NOCASE = ?", (name,))
                else:
                    c.execute("SELECT name FROM olts WHERE name COLLATE NOCASE = ?", (name,))
                row = c.fetchone()
            except sqlite3.OperationalError:
                row = None
            if not row:
                return jsonify({"error": f"{scope.upper()} '{name}' belum terdaftar."}), 404
            host = f"{scope.upper()}:{row['name']}"
        if not is_host and not is_ont and not (host.startswith("ODP:") or host.startswith("OLT:")):
            return jsonify({"error": "Host/ONT SN belum terdaftar. Tambahkan dulu di dashboard. Untuk fiber massal pakai ODP:<nama> atau OLT:<nama>."}), 404
        c.execute(
            "SELECT 1 FROM maintenance_windows WHERE host=? AND start_at <= ? AND end_at >= ? LIMIT 1",
            (host, _fmt_maint_time(end_dt), _fmt_maint_time(start_dt)),
        )
        if c.fetchone():
            return jsonify({"error": "Window maintenance bertabrakan dengan jadwal aktif host ini"}), 400
    finally:
        conn.close()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with db_lock:
        conn, c = get_db()
        try:
            c.execute(
                "INSERT INTO maintenance_windows (host, start_at, end_at, reason, created_by, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (host, _fmt_maint_time(start_dt), _fmt_maint_time(end_dt),
                 reason, current_user.username, now_str),
            )
            new_id = c.lastrowid
            _insert_system_log(c, "MAINTENANCE_CREATE", host,
                               f"{_fmt_maint_time(start_dt)} s/d {_fmt_maint_time(end_dt)}{(' - ' + reason) if reason else ''}",
                               now_str)
            _commit_with_retry(conn)
        finally:
            conn.close()
    try:
        audit(current_user.username, "maintenance.create", f"{host} id={new_id}")
    except Exception:
        pass
    return jsonify({"status": "success", "id": new_id}), 201


@app.route("/api/maintenance/<int:mid>", methods=["DELETE"])
@api_login_required
def api_maintenance_delete(mid):
    conn, c = get_db()
    c.execute("SELECT host FROM maintenance_windows WHERE id=?", (mid,))
    row = c.fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Jadwal maintenance tidak ditemukan"}), 404
    host = row["host"]
    conn.close()
    with db_lock:
        conn, c = get_db()
        try:
            c.execute("DELETE FROM maintenance_windows WHERE id=?", (mid,))
            _commit_with_retry(conn)
        finally:
            conn.close()
    try:
        audit(current_user.username, "maintenance.delete", f"id={mid} host={host}")
    except Exception:
        pass
    return jsonify({"status": "success"})


@app.route("/services")
@login_required
def services_page():
    return render_template("services.html")


@app.route("/api/services", methods=["GET"])
@api_login_required
def get_services_api():
    conn, c = get_db()
    c.execute("SELECT * FROM services")
    rows = c.fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/services", methods=["POST"])
@api_login_required
def add_service_api():
    data = request.get_json(silent=True) or {}
    ip = str(data.get("ip") or "").strip()
    name = str(data.get("name") or "").strip()
    svc_type = str(data.get("type") or "tcp").strip().lower()
    port_raw = data.get("port")
    url = str(data.get("url") or "").strip()

    if not ip or not name:
        return jsonify({"error": "IP dan Name diperlukan"}), 400
    if svc_type not in ("tcp", "http"):
        return jsonify({"error": "Type harus tcp atau http"}), 400
    if len(name) > 100 or len(ip) > 255:
        return jsonify({"error": "Name/IP terlalu panjang"}), 400

    port = None
    if svc_type == "tcp":
        try:
            port = int(port_raw or 0)
        except (ValueError, TypeError):
            return jsonify({"error": "TCP Port harus angka 1-65535"}), 400
        if not 1 <= port <= 65535:
            return jsonify({"error": "TCP Port harus 1-65535"}), 400
        url = None
    else:
        if not url or not re.match(r"^https?://[^\s/$.?#].[^\s]*$", url, re.IGNORECASE):
            return jsonify({"error": "URL http harus diawali http(s)://"}), 400
        if len(url) > 500:
            return jsonify({"error": "URL terlalu panjang"}), 400
        port = None

    conn, c = get_db()

    if svc_type == "tcp":
        c.execute("SELECT id FROM services WHERE ip=? AND type='tcp' AND port=?", (ip, port))
    else:
        c.execute("SELECT id FROM services WHERE ip=? AND type='http' AND url=?", (ip, url))
    if c.fetchone():
        conn.close()
        return jsonify({"error": "Service monitor ini sudah ada"}), 400
    c.execute(
        "INSERT INTO services (ip, name, type, port, url) VALUES (?, ?, ?, ?, ?)",
        (ip, name, svc_type, port, url)
    )
    new_id = c.lastrowid
    conn.commit()
    conn.close()

    try:
        audit(current_user.username, "service.create", f"{name} {svc_type} {ip}")
    except Exception:
        pass

    trigger_manual_service_check()
    if svc_type == "http" and (url or "").lower().startswith("https://"):
        trigger_async_ssl_check()
    return jsonify({"status": "success", "id": new_id}), 201


@app.route("/api/services/<int:svc_id>", methods=["DELETE"])
@api_login_required
def delete_service_api(svc_id):
    conn, c = get_db()
    c.execute("SELECT ip, name FROM services WHERE id=?", (svc_id,))
    row = c.fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Service tidak ditemukan"}), 404
    c.execute("DELETE FROM services WHERE id=?", (svc_id,))
    try:
        c.execute("DELETE FROM service_history WHERE service_id=?", (svc_id,))
    except sqlite3.OperationalError:
        pass
    conn.commit()
    conn.close()
    try:
        audit(current_user.username, "service.delete", f"{row['name']} {row['ip']} id={svc_id}")
    except Exception:
        pass
    return jsonify({"status": "success"})


@app.route("/api/services/<int:svc_id>/history")
@api_login_required
def api_service_history(svc_id):
    try:
        hours = int(request.args.get("hours", 24))
    except (ValueError, TypeError):
        return jsonify({"error": "hours harus angka 1-168"}), 400
    hours = max(1, min(hours, 168))
    conn, c = get_db()
    try:
        c.execute("SELECT id, name, type, ip, port, url, status FROM services WHERE id=?", (svc_id,))
        svc = c.fetchone()
        if not svc:
            return jsonify({"error": "Service tidak ditemukan"}), 404
        c.execute(
            "SELECT timestamp, latency, status FROM service_history "
            "WHERE service_id=? AND timestamp > datetime('now','localtime',?) ORDER BY id ASC",
            (svc_id, f"-{hours} hours"),
        )
        rows = c.fetchall()
    finally:
        conn.close()
    labels = [r["timestamp"].split(" ")[1] if " " in (r["timestamp"] or "") else r["timestamp"] for r in rows]
    values = [round(r["latency"], 2) if r["status"] == "ONLINE" else None for r in rows]
    return jsonify({
        "service": dict(svc),
        "hours": hours,
        "labels": labels,
        "values": values,
        "count": len(rows),
    })


@app.route("/api/services/<int:svc_id>/ssl-check", methods=["POST"])
@api_login_required
def api_service_ssl_check(svc_id):
    conn, c = get_db()
    c.execute("SELECT url FROM services WHERE id=?", (svc_id,))
    row = c.fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "Service tidak ditemukan"}), 404
    trigger_async_ssl_check()
    return jsonify({"status": "queued"})


# ---------------- FIBER / REDAMAN ----------------
@app.route("/fiber")
@login_required
def fiber_page():
    return render_template("fiber.html")


@app.route("/topo")
@login_required
def topo_page():
    return render_template("topo.html")


def _validate_fiber(d):
    ont_sn = (d.get("ont_sn") or "").strip()[:64]
    if not ont_sn or not re.match(r"^[A-Za-z0-9_.:\-]{3,64}$", ont_sn):
        return None, "ONT SN 3-64 karakter (huruf/angka/_-.:)"
    try:
        rx = d.get("rx_power", None)
        rx = None if rx in (None, "") else float(rx)
        if rx is not None and not -40 <= rx <= 10:
            return None, "Rx power harus -40..10 dBm"
    except (ValueError, TypeError):
        return None, "Rx power harus angka dBm"
    try:
        tx = d.get("tx_power", None)
        tx = None if tx in (None, "") else float(tx)
        if tx is not None and not -10 <= tx <= 10:
            return None, "Tx power harus -10..10 dBm"
    except (ValueError, TypeError):
        return None, "Tx power harus angka dBm"
    try:
        rw = d.get("rx_warn", None)
        rw = None if rw in (None, "") else float(rw)
        if rw is not None and not -40 <= rw <= 10:
            return None, "Override warn harus -40..10 dBm"
    except (ValueError, TypeError):
        return None, "Override warn harus angka dBm"
    try:
        rc = d.get("rx_crit", None)
        rc = None if rc in (None, "") else float(rc)
        if rc is not None and not -40 <= rc <= 10:
            return None, "Override crit harus -40..10 dBm"
    except (ValueError, TypeError):
        return None, "Override crit harus angka dBm"
    if rw is not None and rc is not None and not rc < rw:
        return None, "Override harus: crit < warn"
    mute = d.get("mute_alarm", 0)
    if isinstance(mute, str):
        mute = 1 if mute.strip().lower() in ("1", "true", "ya", "yes", "on") else 0
    try:
        mute = 1 if int(mute) else 0
    except (ValueError, TypeError):
        mute = 0
    mute_until_raw = d.get("mute_until", None)
    if mute_until_raw in (None, ""):
        mute_until = ""
    else:
        mute_until_dt = _parse_maint_time(str(mute_until_raw))
        if not mute_until_dt:
            return None, "mute_until harus format YYYY-MM-DD HH:MM (atau tanggal saja)"
        mute_until = _fmt_maint_time(mute_until_dt)
    mute_reason = str(d.get("mute_reason") or "").strip()[:200]
    ont_index = str(d.get("ont_index") or "").strip()[:64]
    if ont_index and not re.match(r"^[A-Za-z0-9_.\-:]{1,64}$", ont_index):
        return None, "ONT index 1-64 karakter (huruf/angka/_-.:)"
    source = str(d.get("source") or "manual").strip().lower()[:16]
    if source not in ("manual", "simulator", "snmp"):
        source = "manual"
    return {"ont_sn": ont_sn,
            "customer": str(d.get("customer") or "").strip()[:100],
            "olt_name": str(d.get("olt_name") or "").strip()[:100],
            "pon_port": str(d.get("pon_port") or "").strip()[:50],
            "odp_name": str(d.get("odp_name") or "").strip()[:100],
            "rx_power": rx, "tx_power": tx, "source": source,
            "mute_alarm": mute, "mute_until": mute_until, "mute_reason": mute_reason,
            "rx_warn": rw, "rx_crit": rc,
            "ont_index": ont_index}, None


def _fiber_row_status(o):
    """Status satu baris ONT: eval + overlay stale.

    Kembalikan (status, severity, advice, need, stale, stale_age).
    """
    status, severity, advice, need = fiber_eval(o.get("rx_power"), o.get("tx_power"),
                                               _th_for_ont(o))
    stale, stale_age = _fiber_stale_info(o)
    if stale and status in ("normal", "warning", "unknown"):
        status, severity = "stale", "warning"
        advice = _fiber_stale_advice(o, stale_age or "?")
    return status, severity, advice, need, stale, stale_age


def _fiber_enrich_row(o, degrade_days=7, degrade_thresh=3.0, down_map=None,
                      flap_hours=24):
    """Baris ONT + field terhitung untuk API (status, mute, stale, degradasi, flap, downtime)."""
    status, severity, advice, need, stale, stale_age = _fiber_row_status(o)
    dg = fiber_degrade_memory.get(o["id"]) or {}
    fl = fiber_flap_memory.get(o["id"]) or {}
    down_since = (down_map or {}).get(o["id"])
    return {**o, "calc_status": status, "severity": severity,
            "advice": advice, "need_attenuator_db": need,
            "mute_active": _is_mute_active(o),
            "stale": stale, "stale_age": stale_age,
            "degrading": bool(dg.get("degrading")),
            "degrade_drop_db": dg.get("drop_db"),
            "degrade_days": degrade_days, "degrade_thresh_db": degrade_thresh,
            "flapping": bool(fl.get("flapping")), "flap_count": fl.get("flips"),
            "flap_hours": flap_hours,
            "down_since": down_since, "down_ongoing": down_since is not None}


_FIBER_SORTS = ("olt", "rx_asc", "rx_desc", "sn_asc", "sn_desc",
                "checked_desc", "status")
_FIBER_STATUS_RANK = {"overload": 0, "critical": 1, "warning": 2, "stale": 3,
                      "unknown": 4, "normal": 5}


@app.route("/api/fiber", methods=["GET"])
@api_login_required
def api_fiber_list():
    conn, c = get_db()
    try:
        c.execute("SELECT * FROM fiber_onts ORDER BY olt_name ASC, id ASC")
        rows = [dict(r) for r in c.fetchall()]
    except sqlite3.OperationalError:
        rows = []
    finally:
        try:
            conn.close()
        except Exception:
            pass
    _dthresh, _ddays = _fiber_degrade_settings()
    _fthresh, _fhours = _fiber_flap_settings()
    down_map = _fiber_open_downtime_map()
    enriched = [_fiber_enrich_row(o, _ddays, _dthresh, down_map, _fhours) for o in rows]
    # mode legacy (tanpa param): kembalikan array penuh seperti dulu
    if not any(request.args.get(k) is not None
               for k in ("page", "per_page", "sort", "q", "status", "olt", "odp")):
        return jsonify(enriched)
    # mode paginasi: filter + sort di server
    status_f = (request.args.get("status") or "all").strip().lower()
    q = (request.args.get("q") or "").strip().lower()
    olt_f = (request.args.get("olt") or "").strip().lower()
    odp_f = (request.args.get("odp") or "").strip().lower()
    items = enriched
    if status_f != "all":
        items = [o for o in items if o["calc_status"] == status_f]
    if olt_f:
        items = [o for o in items if (o.get("olt_name") or "").strip().lower() == olt_f]
    if odp_f:
        items = [o for o in items if (o.get("odp_name") or "").strip().lower() == odp_f]
    if q:
        items = [o for o in items
                 if q in " ".join(str(o.get(k) or "") for k in
                                  ("ont_sn", "customer", "olt_name", "odp_name",
                                   "pon_port")).lower()]
    sort = (request.args.get("sort") or "olt").strip().lower()
    if sort not in _FIBER_SORTS:
        return jsonify({"error": f"sort harus salah satu {list(_FIBER_SORTS)}"}), 400
    if sort == "rx_asc":
        items.sort(key=lambda o: (o.get("rx_power") is None,
                                  o.get("rx_power") if o.get("rx_power") is not None else 0,
                                  o.get("id")))
    elif sort == "rx_desc":
        items.sort(key=lambda o: (o.get("rx_power") is None,
                                  -(o.get("rx_power") if o.get("rx_power") is not None else 0),
                                  o.get("id")))
    elif sort == "sn_asc":
        items.sort(key=lambda o: ((o.get("ont_sn") or "").lower(), o.get("id")))
    elif sort == "sn_desc":
        items.sort(key=lambda o: ((o.get("ont_sn") or "").lower(), o.get("id")),
                   reverse=True)
    elif sort == "checked_desc":
        items.sort(key=lambda o: (o.get("last_checked") or ""), reverse=True)
    elif sort == "status":
        items.sort(key=lambda o: (_FIBER_STATUS_RANK.get(o["calc_status"], 9),
                                  o.get("rx_power") if o.get("rx_power") is not None else 99,
                                  o.get("id")))
    else:
        items.sort(key=lambda o: ((o.get("olt_name") or "").lower(), o.get("id")))
    try:
        page = int(request.args.get("page", 1))
    except (ValueError, TypeError):
        return jsonify({"error": "page harus angka >= 1"}), 400
    try:
        per_page = int(request.args.get("per_page", 25))
    except (ValueError, TypeError):
        return jsonify({"error": "per_page harus angka 1-200"}), 400
    page = max(1, page)
    per_page = max(1, min(per_page, 200))
    total = len(items)
    pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, pages)
    start = (page - 1) * per_page
    return jsonify({"items": items[start:start + per_page], "total": total,
                    "page": page, "per_page": per_page, "pages": pages,
                    "sort": sort})


@app.route("/api/fiber/summary", methods=["GET"])
@api_login_required
def api_fiber_summary():
    """Ringkasan fiber: hitungan per status + Rx terburuk/terbaik + opsi select."""
    conn, c = get_db()
    try:
        c.execute("SELECT * FROM fiber_onts ORDER BY id ASC")
        rows = [dict(r) for r in c.fetchall()]
    except sqlite3.OperationalError:
        rows = []
    finally:
        try:
            conn.close()
        except Exception:
            pass
    _dthresh, _ddays = _fiber_degrade_settings()
    _fthresh, _fhours = _fiber_flap_settings()
    down_map = _fiber_open_downtime_map()
    enriched = [_fiber_enrich_row(o, _ddays, _dthresh, down_map, _fhours) for o in rows]
    counts = {"total": len(enriched), "normal": 0, "warning": 0, "critical": 0,
              "overload": 0, "stale": 0, "unknown": 0, "degrading": 0, "muted": 0,
              "down_ongoing": 0, "flapping": 0}
    for o in enriched:
        if o["calc_status"] in counts:
            counts[o["calc_status"]] += 1
        if o["degrading"]:
            counts["degrading"] += 1
        if o["mute_active"]:
            counts["muted"] += 1
        if o["down_ongoing"]:
            counts["down_ongoing"] += 1
        if o["flapping"]:
            counts["flapping"] += 1
    with_rx = [o for o in enriched if o.get("rx_power") is not None]
    worst = sorted(with_rx, key=lambda o: (o["rx_power"], o["id"]))[:10]
    best = sorted(with_rx, key=lambda o: (-o["rx_power"], o["id"]))[:5]

    def _mini(o):
        return {"id": o["id"], "ont_sn": o.get("ont_sn"), "customer": o.get("customer"),
                "olt_name": o.get("olt_name"), "odp_name": o.get("odp_name"),
                "rx_power": o.get("rx_power"), "calc_status": o.get("calc_status")}

    opts = sorted(enriched, key=lambda o: (o.get("ont_sn") or "").lower())
    return jsonify({
        "counts": counts,
        "worst_rx": [_mini(o) for o in worst],
        "best_rx": [_mini(o) for o in best],
        "ont_options": [{"id": o["id"], "ont_sn": o.get("ont_sn"),
                         "customer": o.get("customer"), "rx_power": o.get("rx_power")}
                        for o in opts],
        "olt_names": sorted({(o.get("olt_name") or "").strip() for o in enriched} - {""}),
        "odp_names": sorted({(o.get("odp_name") or "").strip() for o in enriched} - {""}),
        "degrade_days": _ddays, "degrade_thresh_db": _dthresh,
    })


@app.route("/api/fiber", methods=["POST"])
@api_login_required
def api_fiber_create():
    vals, err = _validate_fiber(request.get_json(silent=True) or {})
    if err:
        return jsonify({"error": err}), 400
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    status, _, _, _ = fiber_eval(vals["rx_power"], vals["tx_power"], _th_for_ont(vals))
    has_measurement = vals["rx_power"] is not None or vals["tx_power"] is not None
    conn, c = get_db()
    try:
        try:
            c.execute("""INSERT INTO fiber_onts(ont_sn,customer,olt_name,pon_port,odp_name,
                       rx_power,tx_power,status,last_checked,source,created_at,updated_at,
                       mute_alarm,mute_until,mute_reason,rx_warn,rx_crit,ont_index,last_seen)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?, ?,?,?,?,?,?,?,?)""",
                      (vals["ont_sn"], vals["customer"], vals["olt_name"], vals["pon_port"],
                       vals["odp_name"], vals["rx_power"], vals["tx_power"], status,
                       now if has_measurement else None,
                       vals["source"], now, now,
                       vals["mute_alarm"], vals["mute_until"], vals["mute_reason"],
                       vals["rx_warn"], vals["rx_crit"], vals["ont_index"],
                       now if has_measurement else None))
            fid = c.lastrowid
            if has_measurement:
                c.execute("INSERT INTO fiber_history (ont_id, rx_power, tx_power, timestamp) VALUES (?,?,?,?)",
                          (fid, vals["rx_power"], vals["tx_power"], now))
            conn.commit()
        except sqlite3.IntegrityError:
            try:
                conn.rollback()
            except Exception:
                pass
            return jsonify({"error": "ONT SN sudah terdaftar"}), 400
    finally:
        try:
            conn.close()
        except Exception:
            pass
    try:
        audit(current_user.username, "fiber.create", f"{vals['ont_sn']} rx={vals['rx_power']}")
    except Exception:
        pass
    # evaluasi langsung 1 ONT: alert cepat + memory di-set (poll tak duplikat)
    try:
        _fiber_single_check(fid)
    except Exception as e:
        print(f"[FIBER] single check create gagal: {e}")
    return jsonify({"status": "success", "id": fid}), 201


@app.route("/api/fiber/<int:fid>", methods=["PUT"])
@api_login_required
def api_fiber_update(fid):
    vals, err = _validate_fiber(request.get_json(silent=True) or {})
    if err:
        return jsonify({"error": err}), 400
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    status, _, _, _ = fiber_eval(vals["rx_power"], vals["tx_power"], _th_for_ont(vals))
    has_measurement = vals["rx_power"] is not None or vals["tx_power"] is not None
    conn, c = get_db()
    try:
        c.execute("SELECT id, last_seen FROM fiber_onts WHERE id=?", (fid,))
        _old = c.fetchone()
        if not _old:
            return jsonify({"error": "ONT tidak ditemukan"}), 404
        last_seen_new = now if has_measurement else (_old["last_seen"] or None)
        try:
            c.execute("""UPDATE fiber_onts SET ont_sn=?, customer=?, olt_name=?, pon_port=?, odp_name=?,
                         rx_power=?, tx_power=?, status=?, last_checked=?, source=?, updated_at=?,
                         mute_alarm=?, mute_until=?, mute_reason=?, rx_warn=?, rx_crit=?, ont_index=?,
                         last_seen=?
                         WHERE id=?""",
                      (vals["ont_sn"], vals["customer"], vals["olt_name"], vals["pon_port"],
                       vals["odp_name"], vals["rx_power"], vals["tx_power"], status,
                       now if has_measurement else None,
                       vals["source"], now,
                       vals["mute_alarm"], vals["mute_until"], vals["mute_reason"],
                       vals["rx_warn"], vals["rx_crit"], vals["ont_index"],
                       last_seen_new, fid))
            if has_measurement:
                c.execute("INSERT INTO fiber_history (ont_id, rx_power, tx_power, timestamp) VALUES (?,?,?,?)",
                          (fid, vals["rx_power"], vals["tx_power"], now))
            conn.commit()
        except sqlite3.IntegrityError:
            try:
                conn.rollback()
            except Exception:
                pass
            return jsonify({"error": "ONT SN dipakai data lain"}), 400
    finally:
        try:
            conn.close()
        except Exception:
            pass
    try:
        audit(current_user.username, "fiber.update", f"id={fid} rx={vals['rx_power']}")
    except Exception:
        pass
    # evaluasi langsung 1 ONT: alert cepat + memory di-set (poll tak duplikat).
    # (Jangan seed memory mentah di sini — transisi harus terdeteksi agar
    # telegram perubahan status tetap terkirim.)
    try:
        _fiber_single_check(fid)
    except Exception as e:
        print(f"[FIBER] single check update gagal: {e}")
    return jsonify({"status": "success"})


@app.route("/api/fiber/<int:fid>", methods=["DELETE"])
@api_login_required
def api_fiber_delete(fid):
    conn, c = get_db()
    c.execute("DELETE FROM fiber_onts WHERE id=?", (fid,))
    deleted = c.rowcount
    try:
        c.execute("DELETE FROM fiber_history WHERE ont_id=?", (fid,))
        c.execute("DELETE FROM fiber_downtime WHERE ont_id=?", (fid,))
    except sqlite3.OperationalError:
        pass
    conn.commit()
    conn.close()
    for _mem in (fiber_alarm_memory, fiber_degrade_memory,
                 fiber_flap_memory, fiber_degrade_tg):
        try:
            _mem.pop(fid, None)
        except Exception:
            pass
    if not deleted:
        return jsonify({"error": "ONT tidak ditemukan"}), 404
    try:
        audit(current_user.username, "fiber.delete", f"id={fid}")
    except Exception:
        pass
    return jsonify({"status": "success"})


@app.route("/api/fiber/<int:fid>/history")
@api_login_required
def api_fiber_history(fid):
    try:
        hours = int(request.args.get("hours", 24))
    except (ValueError, TypeError):
        return jsonify({"error": "hours harus angka 1-720"}), 400
    hours = max(1, min(hours, 720))
    conn, c = get_db()
    try:
        c.execute("SELECT id, ont_sn, customer FROM fiber_onts WHERE id=?", (fid,))
        ont = c.fetchone()
        if not ont:
            return jsonify({"error": "ONT tidak ditemukan"}), 404
        c.execute("SELECT timestamp, rx_power, tx_power FROM fiber_history "
                  "WHERE ont_id=? AND timestamp > datetime('now','localtime',?) ORDER BY id ASC LIMIT 25000",
                  (fid, f"-{hours} hours"))
        rows = c.fetchall()
    finally:
        conn.close()
    # downsample agar chart tetap ringan (maks 500 titik)
    if len(rows) > 500:
        step = (len(rows) + 499) // 500
        rows = rows[::step]
    if hours <= 24:
        labels = [r["timestamp"].split(" ")[1] if r["timestamp"] and " " in r["timestamp"] else r["timestamp"] for r in rows]
    else:
        # rentang >24 jam: sertakan tanggal agar titik beda hari tak bertabrakan
        labels = [r["timestamp"][5:16] if r["timestamp"] and len(r["timestamp"]) >= 16 else r["timestamp"] for r in rows]
    return jsonify({"ont": dict(ont), "hours": hours, "labels": labels,
                    "rx": [r["rx_power"] for r in rows],
                    "tx": [r["tx_power"] for r in rows], "count": len(rows)})


@app.route("/api/fiber/<int:fid>/history/export")
@api_login_required
def api_fiber_history_export(fid):
    """Export history Rx/Tx satu ONT ke CSV (maks 720 jam / 30 hari)."""
    try:
        hours = int(request.args.get("hours", 168))
    except (ValueError, TypeError):
        return jsonify({"error": "hours harus angka 1-720"}), 400
    hours = max(1, min(hours, 720))
    conn, c = get_db()
    try:
        c.execute("SELECT id, ont_sn, customer FROM fiber_onts WHERE id=?", (fid,))
        ont = c.fetchone()
        if not ont:
            return jsonify({"error": "ONT tidak ditemukan"}), 404
        c.execute("SELECT timestamp, rx_power, tx_power FROM fiber_history "
                  "WHERE ont_id=? AND timestamp > datetime('now','localtime',?) ORDER BY id ASC LIMIT 25000",
                  (fid, f"-{hours} hours"))
        rows = [dict(r) for r in c.fetchall()]
    finally:
        try:
            conn.close()
        except Exception:
            pass
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["timestamp", "rx_dbm", "tx_dbm"])
    for r in rows:
        w.writerow([r["timestamp"], r["rx_power"], r["tx_power"]])
    try:
        audit(current_user.username, "fiber.history_export",
              f"{ont['ont_sn']} rows={len(rows)} hours={hours}")
    except Exception:
        pass
    safe_sn = re.sub(r"[^A-Za-z0-9_.\-]", "_", ont["ont_sn"] or "ont")[:48]
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition":
                             f"attachment; filename=history_{safe_sn}_{hours}h.csv"})


def _fiber_open_downtime_map():
    """{ont_id: started_at} catatan downtime yang masih terbuka (1 query)."""
    try:
        conn, c = get_db()
        try:
            c.execute("SELECT ont_id, started_at FROM fiber_downtime WHERE resolved_at IS NULL")
            return {r["ont_id"]: r["started_at"] for r in c.fetchall()}
        finally:
            conn.close()
    except Exception:
        return {}


@app.route("/api/fiber/<int:fid>/downtime", methods=["GET"])
@api_login_required
def api_fiber_downtime(fid):
    conn, c = get_db()
    try:
        c.execute("SELECT id FROM fiber_onts WHERE id=?", (fid,))
        if not c.fetchone():
            return jsonify({"error": "ONT tidak ditemukan"}), 404
        c.execute("SELECT id, ont_sn, status, rx_dbm, started_at, resolved_at, duration_s"
                  " FROM fiber_downtime WHERE ont_id=? ORDER BY id DESC LIMIT 100", (fid,))
        out = []
        for r in c.fetchall():
            out.append({
                "id": r["id"], "ont_sn": r["ont_sn"], "status": r["status"],
                "rx_dbm": r["rx_dbm"], "started_at": r["started_at"],
                "resolved_at": r["resolved_at"] or "Ongoing",
                "duration_s": r["duration_s"],
                "duration": _fmt_duration(r["duration_s"]),
                "state": "resolved" if r["resolved_at"] else "ongoing",
            })
    finally:
        conn.close()
    return jsonify(out)


@app.route("/api/fiber/<int:fid>/sla", methods=["GET"])
@api_login_required
def api_fiber_sla(fid):
    """SLA ONT dari catatan downtime. ?days=7|14|30|90 (default 30)."""
    try:
        days = int(request.args.get("days", 30))
    except (ValueError, TypeError):
        return jsonify({"error": "days harus angka"}), 400
    if days not in (7, 14, 30, 90):
        return jsonify({"error": "days harus salah satu 7/14/30/90"}), 400
    now = datetime.now()
    win_start = now - timedelta(days=days)
    win_s = days * 86400
    conn, c = get_db()
    try:
        c.execute("SELECT id, ont_sn FROM fiber_onts WHERE id=?", (fid,))
        ont = c.fetchone()
        if not ont:
            return jsonify({"error": "ONT tidak ditemukan"}), 404
        c.execute("SELECT started_at, resolved_at, duration_s FROM fiber_downtime"
                  " WHERE ont_id=? AND (resolved_at IS NULL OR resolved_at >= ?)"
                  " ORDER BY id ASC", (fid, win_start.strftime("%Y-%m-%d %H:%M:%S")))
        rows = [dict(r) for r in c.fetchall()]
    finally:
        conn.close()
    down_s, incidents, longest = 0, 0, 0
    for r in rows:
        try:
            s = datetime.strptime(r["started_at"], "%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            continue
        try:
            e = datetime.strptime(r["resolved_at"], "%Y-%m-%d %H:%M:%S") if r["resolved_at"] else now
        except (ValueError, TypeError):
            e = now
        s = max(s, win_start)
        overlap = max(0, int((min(e, now) - s).total_seconds()))
        if overlap <= 0:
            continue
        down_s += overlap
        incidents += 1
        longest = max(longest, overlap)
    uptime_pct = round(max(0.0, (win_s - down_s) / win_s * 100), 2)
    return jsonify({"ont": {"id": ont["id"], "ont_sn": ont["ont_sn"]},
                    "days": days, "window_s": win_s,
                    "uptime_pct": uptime_pct, "incidents": incidents,
                    "total_downtime_s": down_s,
                    "total_downtime_str": _fmt_duration(down_s),
                    "longest_s": longest, "longest_str": _fmt_duration(longest)})


@app.route("/api/fiber/export")
@api_login_required
def api_fiber_export():
    conn, c = get_db()
    try:
        c.execute("SELECT * FROM fiber_onts ORDER BY olt_name ASC, id ASC")
        rows = [dict(r) for r in c.fetchall()]
    except sqlite3.OperationalError:
        rows = []
    finally:
        try:
            conn.close()
        except Exception:
            pass
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["ont_sn", "customer", "olt", "pon_port", "odp", "rx_dbm", "tx_dbm",
                "status", "saran", "last_checked", "source", "mute_alarm",
                "mute_until", "mute_reason"])
    for o in rows:
        status, _, advice, _need = fiber_eval(o.get("rx_power"), o.get("tx_power"), _th_for_ont(o))
        w.writerow([_csv_safe(o.get("ont_sn")), _csv_safe(o.get("customer")),
                    _csv_safe(o.get("olt_name")), _csv_safe(o.get("pon_port")),
                    _csv_safe(o.get("odp_name")), o.get("rx_power"), o.get("tx_power"),
                    status, _csv_safe(advice), _csv_safe(o.get("last_checked")),
                    _csv_safe(o.get("source")), o.get("mute_alarm") or 0,
                    _csv_safe(o.get("mute_until")), _csv_safe(o.get("mute_reason"))])
    try:
        audit(current_user.username, "fiber.export", f"rows={len(rows)}")
    except Exception:
        pass
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=fiber_redaman.csv"})


@app.route("/api/fiber/import", methods=["POST"])
@api_login_required
def api_fiber_import():
    """Import massal ONT dari CSV (hindari input satu-satu).

    Kolom: ont_sn*,customer,olt_name,pon_port,odp_name,rx_power,tx_power,
    source,mute_alarm,mute_until,mute_reason,rx_warn,rx_crit,ont_index (*wajib).
    Mode via form/query 'mode': 'skip' (default, duplikat SN dilewati) atau
    'upsert'/'update' (duplikat SN diperbarui + snapshot history baru).
    Semantik upsert = merge: hanya kolom yang ADA dan TERISI di CSV yang
    ditimpa; sel kosong / kolom tak ada mempertahankan nilai lama (aman untuk
    update massal Rx hasil OPM tanpa menghapus customer/ODP).
    Memory alarm di-seed diam-diam agar import massal tak membanjiri Telegram;
    alarm tetap tampil di triggers dan telegram dikirim saat ada perubahan
    berikutnya.
    """
    mode = ((request.form.get("mode") if request.form else None)
            or request.args.get("mode") or "skip").strip().lower()
    if mode not in ("skip", "upsert", "update"):
        return jsonify({"error": "mode harus 'skip' atau 'upsert'"}), 400
    do_upsert = mode in ("upsert", "update")
    if "file" not in request.files:
        return jsonify({"error": "File CSV wajib diunggah (field 'file')"}), 400
    try:
        raw = request.files["file"].read(1000000 + 1)
    except Exception:
        return jsonify({"error": "Gagal membaca file"}), 400
    if not raw or len(raw) > 1000000:
        return jsonify({"error": "File kosong atau melebihi 1 MB"}), 400
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return jsonify({"error": "File harus CSV UTF-8"}), 400
    try:
        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames or "ont_sn" not in [h.strip() for h in reader.fieldnames]:
            return jsonify({"error": "Header CSV harus memuat kolom 'ont_sn'"}), 400
        rows = [r for _, r in zip(range(501), reader)]
    except Exception:
        return jsonify({"error": "Format CSV tidak valid"}), 400
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    created, updated, skipped, errors = 0, 0, 0, []
    with db_lock:
        conn, c = get_db()
        try:
            for i, r in enumerate(rows, start=2):
                vals, err = _validate_fiber({k.strip(): (v.strip() if isinstance(v, str) else v)
                                             for k, v in (r or {}).items() if k})
                if err:
                    errors.append(f"baris {i}: {err}")
                    continue
                try:
                    c.execute("SELECT id FROM fiber_onts WHERE ont_sn=?", (vals["ont_sn"],))
                    existing = c.fetchone()
                    if existing:
                        if not do_upsert:
                            skipped += 1
                            continue
                        fid = existing["id"]
                        # merge: hanya sel terisi yang menimpa nilai lama
                        raw = {(k.strip() if isinstance(k, str) else k): v
                               for k, v in (r or {}).items() if k}
                        c.execute("SELECT * FROM fiber_onts WHERE id=?", (fid,))
                        cur = dict(c.fetchone())
                        merged = dict(vals)
                        for _f in ("customer", "olt_name", "pon_port", "odp_name",
                                   "rx_power", "tx_power", "source", "mute_alarm",
                                   "mute_until", "mute_reason", "rx_warn", "rx_crit",
                                   "ont_index"):
                            _rv = raw.get(_f)
                            if _rv is None or (isinstance(_rv, str) and not _rv.strip()):
                                merged[_f] = cur.get(_f)
                        # normalisasi ulang tipe merge sisa DB (mute_until dkk.)
                        if merged.get("mute_until"):
                            try:
                                _dt = _parse_maint_time(str(merged["mute_until"]))
                                merged["mute_until"] = _fmt_maint_time(_dt) if _dt else ""
                            except Exception:
                                merged["mute_until"] = cur.get("mute_until") or ""
                        # last_seen maju hanya bila CSV membawa pengukuran baru
                        _raw_rx = raw.get("rx_power")
                        _raw_tx = raw.get("tx_power")
                        _has_new_meas = (
                            (_raw_rx is not None and not (isinstance(_raw_rx, str) and not _raw_rx.strip()))
                            or (_raw_tx is not None and not (isinstance(_raw_tx, str) and not _raw_tx.strip()))
                        )
                        merged["last_seen"] = now if _has_new_meas else (cur.get("last_seen") or None)
                        has_meas = (merged["rx_power"] is not None or merged["tx_power"] is not None)
                        status, _, _, _ = fiber_eval(merged["rx_power"], merged["tx_power"],
                                                     _th_for_ont(merged))
                        c.execute("""UPDATE fiber_onts SET customer=?, olt_name=?, pon_port=?, odp_name=?,
                                     rx_power=?, tx_power=?, status=?, last_checked=?, source=?,
                                     updated_at=?, mute_alarm=?, mute_until=?, mute_reason=?,
                                     rx_warn=?, rx_crit=?, ont_index=?, last_seen=? WHERE id=?""",
                                  (merged["customer"], merged["olt_name"], merged["pon_port"],
                                   merged["odp_name"], merged["rx_power"], merged["tx_power"], status,
                                   now if has_meas else None, merged["source"], now,
                                   merged["mute_alarm"], merged["mute_until"], merged["mute_reason"],
                                   merged["rx_warn"], merged["rx_crit"], merged["ont_index"],
                                   merged["last_seen"], fid))
                        if has_meas:
                            c.execute("INSERT INTO fiber_history (ont_id, rx_power, tx_power, timestamp) VALUES (?,?,?,?)",
                                      (fid, merged["rx_power"], merged["tx_power"], now))
                        fiber_alarm_memory[fid] = status
                        updated += 1
                        continue
                    status, _, _, _ = fiber_eval(vals["rx_power"], vals["tx_power"],
                                                 _th_for_ont(vals))
                    has_meas_imp = (vals["rx_power"] is not None or vals["tx_power"] is not None)
                    c.execute("""INSERT INTO fiber_onts(ont_sn,customer,olt_name,pon_port,odp_name,
                               rx_power,tx_power,status,last_checked,source,created_at,updated_at,
                               mute_alarm,mute_until,mute_reason,rx_warn,rx_crit,ont_index,last_seen)
                               VALUES(?,?,?,?,?,?,?,?,?,?,?, ?,?,?,?,?,?,?,?)""",
                              (vals["ont_sn"], vals["customer"], vals["olt_name"], vals["pon_port"],
                               vals["odp_name"], vals["rx_power"], vals["tx_power"], status,
                               now if has_meas_imp else None,
                               vals["source"], now, now,
                               vals["mute_alarm"], vals["mute_until"], vals["mute_reason"],
                               vals["rx_warn"], vals["rx_crit"], vals["ont_index"],
                               now if has_meas_imp else None))
                    fid = c.lastrowid
                    if vals["rx_power"] is not None or vals["tx_power"] is not None:
                        c.execute("INSERT INTO fiber_history (ont_id, rx_power, tx_power, timestamp) VALUES (?,?,?,?)",
                                  (fid, vals["rx_power"], vals["tx_power"], now))
                    fiber_alarm_memory[fid] = status
                    created += 1
                except sqlite3.IntegrityError:
                    skipped += 1
                except Exception as e:
                    errors.append(f"baris {i}: {e}")
            _commit_with_retry(conn)
        finally:
            conn.close()
    try:
        audit(current_user.username, "fiber.import", f"created={created} updated={updated} skipped={skipped}")
    except Exception:
        pass
    return jsonify({"status": "success", "created": created, "updated": updated,
                    "skipped": skipped, "errors": errors[:20]})


# ---------------- ODP (agregasi ONT per ODP) ----------------
def _parse_latlon(d):
    """Koordinat opsional: (lat, lon) atau (None, None). Kembalikan
    (lat, lon, err); pasangan tak lengkap / di luar rentang = err."""
    lat_raw, lon_raw = d.get("lat", None), d.get("lon", None)
    if lat_raw in (None, "") and lon_raw in (None, ""):
        return None, None, None
    try:
        lat = float(lat_raw)
        lon = float(lon_raw)
    except (ValueError, TypeError):
        return None, None, "lat/lon harus angka desimal"
    if not -90 <= lat <= 90:
        return None, None, "lat harus -90..90"
    if not -180 <= lon <= 180:
        return None, None, "lon harus -180..180"
    return round(lat, 6), round(lon, 6), None


def _validate_odp(d):
    name = (d.get("name") or "").strip()[:100]
    if len(name) < 2 or not re.match(r"^[A-Za-z0-9 _.\-/]{2,100}$", name):
        return None, "Nama ODP 2-100 karakter (huruf/angka/spasi/_-./)"
    try:
        capacity = int(d.get("capacity", 8))
    except (ValueError, TypeError):
        return None, "Kapasitas harus angka 1-128"
    if not 1 <= capacity <= 128:
        return None, "Kapasitas harus 1-128 port"
    lat, lon, err = _parse_latlon(d)
    if err:
        return None, err
    return {"name": name,
            "olt_name": str(d.get("olt_name") or "").strip()[:100],
            "capacity": capacity,
            "location": str(d.get("location") or "").strip()[:100],
            "lat": lat, "lon": lon}, None


def _odp_aggregation():
    """Kembalikan list ODP + hitungan ONT per status (link by odp_name)."""
    conn, c = get_db()
    try:
        try:
            c.execute("SELECT * FROM odps ORDER BY name ASC")
            odps = [dict(r) for r in c.fetchall()]
        except sqlite3.OperationalError:
            return []
        try:
            c.execute("SELECT odp_name, rx_power, tx_power, rx_warn, rx_crit, last_seen, source FROM fiber_onts")
            onts = [dict(r) for r in c.fetchall()]
        except sqlite3.OperationalError:
            onts = []
    finally:
        try:
            conn.close()
        except Exception:
            pass
    known = {o["name"].lower(): o["name"] for o in odps}
    buckets = {o["name"]: {"total": 0, "normal": 0, "warning": 0,
                            "critical": 0, "overload": 0, "stale": 0, "unknown": 0} for o in odps}
    buckets[""] = {"total": 0, "normal": 0, "warning": 0,
                   "critical": 0, "overload": 0, "stale": 0, "unknown": 0}
    for t in onts:
        # cocokkan ODP tanpa peduli kapital agar salah ketik tidak yatim
        key = known.get((t.get("odp_name") or "").strip().lower(), "")
        status, _, _, _ = fiber_eval(t.get("rx_power"), t.get("tx_power"), _th_for_ont(t))
        if status in ("normal", "warning", "unknown") and _fiber_stale_info(t)[0]:
            status = "stale"
        b = buckets[key]
        b["total"] += 1
        b[status if status in b else "unknown"] += 1
    rank = {"critical": 4, "overload": 3, "warning": 2, "stale": 2, "unknown": 1, "normal": 0}
    out = []
    for o in odps:
        b = buckets[o["name"]]
        worst = max((k for k in b if k != "total" and b[k] > 0),
                    key=lambda k: rank.get(k, 0), default="normal")
        fill = round(b["total"] / o["capacity"] * 100, 1) if o["capacity"] else 0
        out.append({**o, **b, "worst": worst, "fill_pct": fill})
    unb = buckets[""]
    if unb["total"]:
        worst = max((k for k in unb if k != "total" and unb[k] > 0),
                    key=lambda k: rank.get(k, 0), default="normal")
        out.append({"id": 0, "name": "(tanpa ODP)", "olt_name": "", "capacity": 0,
                    "location": "", **unb, "worst": worst, "fill_pct": 0})
    return out


@app.route("/api/odps", methods=["GET"])
@api_login_required
def api_odp_list():
    return jsonify(_odp_aggregation())


@app.route("/api/odps", methods=["POST"])
@api_login_required
def api_odp_create():
    vals, err = _validate_odp(request.get_json(silent=True) or {})
    if err:
        return jsonify({"error": err}), 400
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn, c = get_db()
    c.execute("SELECT id FROM odps WHERE name COLLATE NOCASE = ?", (vals["name"],))
    if c.fetchone():
        conn.close()
        return jsonify({"error": "Nama ODP sudah terdaftar"}), 400
    try:
        c.execute("INSERT INTO odps (name, olt_name, capacity, location, lat, lon, created_at, updated_at)"
                  " VALUES (?,?,?,?,?,?,?,?)",
                  (vals["name"], vals["olt_name"], vals["capacity"],
                   vals["location"], vals["lat"], vals["lon"], now, now))
        nid = c.lastrowid
        conn.commit()
    except sqlite3.IntegrityError:
        try:
            conn.rollback()
        except Exception:
            pass
        conn.close()
        return jsonify({"error": "Nama ODP sudah terdaftar"}), 400
    conn.close()
    try:
        audit(current_user.username, "odp.create", vals["name"])
    except Exception:
        pass
    return jsonify({"status": "success", "id": nid}), 201


@app.route("/api/odps/<int:oid>", methods=["PUT"])
@api_login_required
def api_odp_update(oid):
    vals, err = _validate_odp(request.get_json(silent=True) or {})
    if err:
        return jsonify({"error": err}), 400
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn, c = get_db()
    c.execute("SELECT name FROM odps WHERE id=?", (oid,))
    old = c.fetchone()
    if not old:
        conn.close()
        return jsonify({"error": "ODP tidak ditemukan"}), 404
    c.execute("SELECT id FROM odps WHERE name COLLATE NOCASE = ? AND id != ?",
              (vals["name"], oid))
    if c.fetchone():
        conn.close()
        return jsonify({"error": "Nama ODP dipakai data lain"}), 400
    try:
        c.execute("UPDATE odps SET name=?, olt_name=?, capacity=?, location=?, lat=?, lon=?, updated_at=? WHERE id=?",
                  (vals["name"], vals["olt_name"], vals["capacity"],
                   vals["location"], vals["lat"], vals["lon"], now, oid))
        # ONT yang menunjuk nama lama ikut pindah (case-insensitive,
        # selaras dengan agregasi yang mencocokkan tanpa peduli kapital)
        if old["name"].lower() != vals["name"].lower():
            c.execute("UPDATE fiber_onts SET odp_name=? WHERE odp_name COLLATE NOCASE = ?",
                      (vals["name"], old["name"]))
        conn.commit()
    except sqlite3.IntegrityError:
        try:
            conn.rollback()
        except Exception:
            pass
        conn.close()
        return jsonify({"error": "Nama ODP dipakai data lain"}), 400
    conn.close()
    try:
        audit(current_user.username, "odp.update", f"id={oid} {vals['name']}")
    except Exception:
        pass
    return jsonify({"status": "success"})


@app.route("/api/odps/<int:oid>", methods=["DELETE"])
@api_login_required
def api_odp_delete(oid):
    conn, c = get_db()
    c.execute("SELECT name FROM odps WHERE id=?", (oid,))
    row = c.fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "ODP tidak ditemukan"}), 404
    c.execute("DELETE FROM odps WHERE id=?", (oid,))
    # ONT yatim: kosongkan referensi (data ONT tetap aman, case-insensitive)
    c.execute("UPDATE fiber_onts SET odp_name='' WHERE odp_name COLLATE NOCASE = ?", (row["name"],))
    conn.commit()
    conn.close()
    try:
        audit(current_user.username, "odp.delete", f"id={oid} {row['name']}")
    except Exception:
        pass
    return jsonify({"status": "success"})


# ---------------- OLT (sumber SNMP untuk Rx/Tx ONT) ----------------
# Preset OID per vendor (hasil riset MIB + template lapangan):
# - ZTE C300/C320 (ZXGPON-ONTMGMT-MIB): Rx kolom .10, Tx kolom .14,
#   rumus raw*0.002-30 (sesuai template Zabbix community).
# - Huawei MA5600T/MA5800 (HUAWEI-XPON-MIB hwGponDeviceOntOpticalDdmInfoTable):
#   ONU Rx = .51.1.4, rumus (raw-10000)/100. Kolom Tx ONT tak ada di tabel
#   ini (kosongkan tx_base = pantau Rx saja, atau isi manual bila ketemu).
# Rumus umum: dBm = raw/div*scale + offset.
OLT_VENDOR_PRESETS = {
    "zte": {
        "rx_base": "1.3.6.1.4.1.3902.1012.3.50.12.1.1.10",
        "tx_base": "1.3.6.1.4.1.3902.1012.3.50.12.1.1.14",
        "div": 1.0, "scale": 0.002, "offset": -30.0,
        "note": "ZTE C300/C320. ont_index = sufiks hasil walk "
                "(<ponIfIndex>.<onuIdx>[.1]), mis. 268501248.5.1. "
                "Tx kolom .14 mengikuti encoding yang sama — verifikasi via tombol Test.",
    },
    "huawei": {
        "rx_base": "1.3.6.1.4.1.2011.6.128.1.1.2.51.1.4",
        "tx_base": "",
        "div": 1.0, "scale": 0.01, "offset": -100.0,
        "note": "Huawei MA5600T/MA5800 (rumus (raw-10000)/100). "
                "Tx ONT tidak tersedia di tabel ini — kosongkan (pantau Rx saja).",
    },
    "generic": {
        "rx_base": "", "tx_base": "",
        "div": 100.0, "scale": 1.0, "offset": 0.0,
        "note": "",
    },
}


def _olt_transform(olt):
    """(div, scale, offset) dengan default aman untuk baris lama."""
    try:
        div = float(olt.get("div") or 100.0) or 100.0
    except (ValueError, TypeError):
        div = 100.0
    try:
        scale = float(olt.get("scale", 1.0))
    except (ValueError, TypeError):
        scale = 1.0
    if scale == 0:
        scale = 1.0
    try:
        offset = float(olt.get("offset", 0.0))
    except (ValueError, TypeError):
        offset = 0.0
    return div, scale, offset


def _olt_raw_to_dbm(raw, olt):
    """Mentah SNMP -> dBm. None bila tak bisa dikonversi."""
    try:
        div, scale, offset = _olt_transform(olt)
        return round(float(raw) / div * scale + offset, 2)
    except (ValueError, TypeError, ZeroDivisionError):
        return None


def _validate_olt(d):
    name = (d.get("name") or "").strip()[:100]
    if len(name) < 2 or not re.match(r"^[A-Za-z0-9 _.\-/]{2,100}$", name):
        return None, "Nama OLT 2-100 karakter (huruf/angka/spasi/_-./)"
    ip = str(d.get("ip") or "").strip()[:64]
    if ip:
        try:
            ipaddress.ip_address(ip)
        except ValueError:
            return None, "IP OLT tidak valid"
    community = str(d.get("community") or "").strip()[:128]
    vendor = str(d.get("vendor") or "generic").strip().lower()[:16]
    if vendor not in ("zte", "huawei", "generic"):
        vendor = "generic"
    rx_base = str(d.get("rx_base") or "").strip().strip(".")[:128]
    tx_base = str(d.get("tx_base") or "").strip().strip(".")[:128]
    if rx_base and not _valid_oid(rx_base):
        return None, "rx_base bukan OID valid"
    if tx_base and not _valid_oid(tx_base):
        return None, "tx_base bukan OID valid"
    try:
        div = float(d.get("div", 100))
    except (ValueError, TypeError):
        return None, "div harus angka 1-10000"
    if not 1 <= div <= 10000:
        return None, "div harus 1-10000"
    try:
        scale = float(d.get("scale", 1.0))
    except (ValueError, TypeError):
        return None, "scale harus angka > 0"
    if not 1e-9 <= scale <= 1e6:
        return None, "scale harus 1e-9..1e6 (cth ZTE 0.002, Huawei 0.01)"
    try:
        offset = float(d.get("offset", 0.0))
    except (ValueError, TypeError):
        return None, "offset harus angka -10000..10000"
    if not -10000 <= offset <= 10000:
        return None, "offset harus -10000..10000 dB"
    lat, lon, err = _parse_latlon(d)
    if err:
        return None, err
    return {"name": name, "ip": ip, "community": community, "vendor": vendor,
            "rx_base": rx_base, "tx_base": tx_base, "div": div,
            "scale": scale, "offset": offset, "lat": lat, "lon": lon}, None


def _apply_olt_preset(vals, raw_data):
    """Isi field kosong dari preset vendor (dipakai saat create OLT).

    rx/tx yang kosong -> preset. div/scale/offset -> preset hanya bila user
    tak mengirim ketiganya sama sekali (transform utuh default vendor);
    bila user menyentuh salah satunya, seluruhnya dihormati apa adanya
    agar kustomisasi tak tertimpa diam-diam.
    """
    p = OLT_VENDOR_PRESETS.get(vals.get("vendor") or "generic")
    if not p or vals.get("vendor") == "generic":
        return vals
    vals = dict(vals)
    if not vals.get("rx_base") and p.get("rx_base"):
        vals["rx_base"] = p["rx_base"]
    if not vals.get("tx_base") and p.get("tx_base"):
        vals["tx_base"] = p["tx_base"]
    raw_data = raw_data or {}
    if all(raw_data.get(k) is None for k in ("div", "scale", "offset")):
        for k in ("div", "scale", "offset"):
            if p.get(k) is not None:
                vals[k] = p[k]
    return vals


@app.route("/api/olts", methods=["GET"])
@api_login_required
def api_olt_list():
    conn, c = get_db()
    try:
        try:
            c.execute("SELECT id, name, ip, vendor, rx_base, tx_base, div, scale, offset, lat, lon,"
                      " last_tested, last_test_ok, last_test_msg FROM olts ORDER BY name ASC")
            rows = [dict(r) for r in c.fetchall()]
        except sqlite3.OperationalError:
            rows = []
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return jsonify(rows)


@app.route("/api/olts", methods=["POST"])
@api_login_required
def api_olt_create():
    raw = request.get_json(silent=True) or {}
    vals, err = _validate_olt(raw)
    if err:
        return jsonify({"error": err}), 400
    vals = _apply_olt_preset(vals, raw)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn, c = get_db()
    try:
        c.execute("SELECT id FROM olts WHERE name COLLATE NOCASE = ?", (vals["name"],))
        if c.fetchone():
            return jsonify({"error": "Nama OLT sudah terdaftar"}), 400
        try:
            c.execute("INSERT INTO olts (name, ip, community, vendor, rx_base, tx_base, div,"
                      " scale, offset, lat, lon, created_at, updated_at)"
                      " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (vals["name"], vals["ip"], vals["community"], vals["vendor"],
                       vals["rx_base"], vals["tx_base"], vals["div"],
                       vals["scale"], vals["offset"], vals["lat"], vals["lon"], now, now))
            nid = c.lastrowid
            conn.commit()
        except sqlite3.IntegrityError:
            try:
                conn.rollback()
            except Exception:
                pass
            return jsonify({"error": "Nama OLT sudah terdaftar"}), 400
    finally:
        try:
            conn.close()
        except Exception:
            pass
    try:
        audit(current_user.username, "olt.create", vals["name"])
    except Exception:
        pass
    return jsonify({"status": "success", "id": nid}), 201


@app.route("/api/olts/<int:oid>", methods=["PUT"])
@api_login_required
def api_olt_update(oid):
    vals, err = _validate_olt(request.get_json(silent=True) or {})
    if err:
        return jsonify({"error": err}), 400
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn, c = get_db()
    try:
        c.execute("SELECT name, community FROM olts WHERE id=?", (oid,))
        old = c.fetchone()
        if not old:
            return jsonify({"error": "OLT tidak ditemukan"}), 404
        # community tak pernah dikirim balik ke UI; kosong = pertahankan lama
        if not vals["community"] and old["community"]:
            vals["community"] = old["community"]
        c.execute("SELECT id FROM olts WHERE name COLLATE NOCASE = ? AND id != ?",
                  (vals["name"], oid))
        if c.fetchone():
            return jsonify({"error": "Nama OLT dipakai data lain"}), 400
        try:
            c.execute("UPDATE olts SET name=?, ip=?, community=?, vendor=?, rx_base=?, tx_base=?,"
                      " div=?, scale=?, offset=?, lat=?, lon=?, updated_at=? WHERE id=?",
                      (vals["name"], vals["ip"], vals["community"], vals["vendor"],
                       vals["rx_base"], vals["tx_base"], vals["div"],
                       vals["scale"], vals["offset"], vals["lat"], vals["lon"], now, oid))
            if old["name"].lower() != vals["name"].lower():
                c.execute("UPDATE fiber_onts SET olt_name=? WHERE olt_name COLLATE NOCASE = ?",
                          (vals["name"], old["name"]))
            conn.commit()
        except sqlite3.IntegrityError:
            try:
                conn.rollback()
            except Exception:
                pass
            return jsonify({"error": "Nama OLT dipakai data lain"}), 400
    finally:
        try:
            conn.close()
        except Exception:
            pass
    try:
        audit(current_user.username, "olt.update", f"id={oid} {vals['name']}")
    except Exception:
        pass
    return jsonify({"status": "success"})


@app.route("/api/olts/<int:oid>", methods=["DELETE"])
@api_login_required
def api_olt_delete(oid):
    conn, c = get_db()
    try:
        c.execute("SELECT name FROM olts WHERE id=?", (oid,))
        row = c.fetchone()
        if not row:
            return jsonify({"error": "OLT tidak ditemukan"}), 404
        c.execute("DELETE FROM olts WHERE id=?", (oid,))
        conn.commit()
    finally:
        try:
            conn.close()
        except Exception:
            pass
    try:
        audit(current_user.username, "olt.delete", f"id={oid} {row['name']}")
    except Exception:
        pass
    return jsonify({"status": "success"})


@app.route("/api/olts/presets", methods=["GET"])
@api_login_required
def api_olt_presets():
    return jsonify({k: {kk: vv for kk, vv in v.items()} for k, v in OLT_VENDOR_PRESETS.items()})


def _olt_test_connection(olt, timeout=3.0):
    """Uji SNMP ke OLT: reachability + sampel satu entri Rx/Tx.

    Kembalikan dict {reachable, sysup_s, rx, tx, ok, message, elapsed_ms}.
    rx/tx berisi {ok, oid, index, raw, dbm} atau None bila base tak diisi.
    """
    import time as _t
    t0 = _t.time()
    ip = (olt.get("ip") or "").strip()
    comm = (olt.get("community") or "").strip()
    res: dict = {"reachable": False, "sysup_s": None, "rx": None, "tx": None,
           "ok": False, "message": "", "elapsed_ms": 0}
    if not ip or not comm:
        res["message"] = "IP/community OLT belum diisi"
        return res
    try:
        vals = _snmp_get(ip, comm, [SYSUP_OID], timeout=timeout)
    except Exception as e:
        res["message"] = f"SNMP error: {e}"
        res["elapsed_ms"] = int((_t.time() - t0) * 1000)
        return res
    if not vals or vals[0] is None or vals[0] < 0:
        res["message"] = "OLT tak menjawab (cek IP/community/SNMP aktif)"
        res["elapsed_ms"] = int((_t.time() - t0) * 1000)
        return res
    res["reachable"] = True
    try:
        res["sysup_s"] = round(vals[0] / 100.0, 1)
    except (ValueError, TypeError):
        pass
    for key, base in (("rx", (olt.get("rx_base") or "").strip().strip(".")),
                      ("tx", (olt.get("tx_base") or "").strip().strip("."))):
        if not _valid_oid(base):
            continue
        try:
            oid, _tag, ival, sval = _snmp_getnext(ip, comm, base, timeout=timeout)
        except Exception:
            oid, ival, sval = None, None, None
        if not oid:
            res[key] = {"ok": False, "error": "OID tak menjawab — cek rx/tx_base"}
            continue
        suffix = oid[len(base):].lstrip(".")
        raw = ival
        if raw is None and sval not in (None, ""):
            try:
                raw = float(sval)
            except (ValueError, TypeError):
                raw = None
        res[key] = {"ok": raw is not None, "oid": oid, "index": suffix, "raw": raw,
                    "dbm": _olt_raw_to_dbm(raw, olt) if raw is not None else None}
    res["elapsed_ms"] = int((_t.time() - t0) * 1000)
    bases = [k for k in ("rx", "tx") if res.get(k) is not None]
    if not bases:
        res["ok"] = True
        res["message"] = f"terjangkau (up {res['sysup_s']}s), OID belum dikonfigurasi"
    elif any(res[k].get("ok") for k in bases):
        res["ok"] = True
        parts = []
        for k in bases:
            e: dict = res[k]
            parts.append(f"{k}={e.get('dbm')}dBm(idx {e.get('index')})" if e.get("ok")
                         else f"{k} gagal")
        res["message"] = f"terjangkau (up {res['sysup_s']}s); " + ", ".join(parts)
    else:
        res["message"] = "terjangkau, tapi OID Rx/Tx tak menjawab — cek rx/tx_base"
    return res


@app.route("/api/olts/<int:oid>/test", methods=["POST"])
@api_login_required
def api_olt_test(oid):
    conn, c = get_db()
    try:
        c.execute("SELECT * FROM olts WHERE id=?", (oid,))
        row = c.fetchone()
        olt = dict(row) if row else None
    finally:
        try:
            conn.close()
        except Exception:
            pass
    if not olt:
        return jsonify({"error": "OLT tidak ditemukan"}), 404
    res = _olt_test_connection(olt)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with db_lock:
        conn, c = get_db()
        try:
            c.execute("UPDATE olts SET last_tested=?, last_test_ok=?, last_test_msg=? WHERE id=?",
                      (now, 1 if res["ok"] else 0, res["message"][:200], oid))
            _commit_with_retry(conn)
        finally:
            try:
                conn.close()
            except Exception:
                pass
    try:
        audit(current_user.username, "olt.test", f"{olt['name']} ok={res['ok']}")
    except Exception:
        pass
    return jsonify({**res, "olt": {"id": olt["id"], "name": olt["name"]}, "tested_at": now})


def _discover_sn(olt_name, suffix):
    """SN otomatis hasil discover: sanitasi + batasi 64 karakter.

    Ekor (sufiks index, bagian yang unik) dipertahankan; bila masih
    kepanjangan, pangkas dari kiri.
    """
    base = re.sub(r"[^A-Za-z0-9_.:\-]", "-", (olt_name or "OLT").strip()) or "OLT"
    suffix = re.sub(r"[^A-Za-z0-9_.:\-]", "-", (suffix or "").strip()) or "0"
    room = 64 - len(suffix) - 1
    sn = f"{base[:max(1, room)]}-{suffix}"
    return sn if len(sn) <= 64 else sn[-64:]


@app.route("/api/olts/<int:oid>/discover", methods=["POST"])
@api_login_required
def api_olt_discover(oid):
    """Enumerasi ONT dari OLT via walk rx_base, lalu bulk upsert.

    Body opsional: {limit (1-256, default 256), update (bool, default true)}.
    Baris baru -> source=snmp; baris existing source!=snmp dilewati aman
    (tak menimpa data manual). Memory alarm di-seed agar tak banjir telegram.
    """
    data = request.get_json(silent=True) or {}
    try:
        limit = int(data.get("limit", 256))
    except (ValueError, TypeError):
        return jsonify({"error": "limit harus angka 1-256"}), 400
    limit = max(1, min(limit, 256))
    do_update = str(data.get("update", "1")).strip().lower() not in ("0", "false", "tidak", "no")
    conn, c = get_db()
    try:
        c.execute("SELECT * FROM olts WHERE id=?", (oid,))
        row = c.fetchone()
        olt = dict(row) if row else None
    finally:
        try:
            conn.close()
        except Exception:
            pass
    if not olt:
        return jsonify({"error": "OLT tidak ditemukan"}), 404
    ip = (olt.get("ip") or "").strip()
    comm = (olt.get("community") or "").strip()
    base_rx = (olt.get("rx_base") or "").strip().strip(".")
    base_tx = (olt.get("tx_base") or "").strip().strip(".")
    if not ip or not comm:
        return jsonify({"error": "IP/community OLT belum diisi"}), 400
    if not _valid_oid(base_rx):
        return jsonify({"error": "rx_base belum diisi — pilih preset vendor dulu atau isi manual"}), 400
    try:
        up = _snmp_get(ip, comm, [SYSUP_OID], timeout=3.0)
    except Exception as e:
        return jsonify({"error": f"OLT tak terjangkau: {e}"}), 502
    if not up or up[0] is None:
        return jsonify({"error": "OLT tak menjawab SNMP (cek IP/community)"}), 502
    try:
        walked = snmp_walk(ip, comm, base_rx, max_rows=limit)
    except Exception as e:
        return jsonify({"error": f"walk gagal: {e}"}), 502
    suffixes, rx_map = [], {}
    for woid, _tag, ival, sval in walked or []:
        suffix = (woid or "")[len(base_rx):].lstrip(".")
        if not suffix:
            continue
        raw = ival
        if raw is None and sval not in (None, ""):
            try:
                raw = float(sval)
            except (ValueError, TypeError):
                continue
        dbm = _olt_raw_to_dbm(raw, olt)
        if dbm is None or not -40 <= dbm <= 10:
            continue
        suffixes.append(suffix)
        rx_map[suffix] = dbm
    tx_map = {}
    if _valid_oid(base_tx) and suffixes:
        for i in range(0, len(suffixes), 25):
            chunk = suffixes[i:i + 25]
            try:
                tvals = _snmp_get(ip, comm, [base_tx + "." + s for s in chunk], timeout=4.0)
            except Exception:
                tvals = [None] * len(chunk)
            for sfx, v in zip(chunk, tvals or []):
                if v is None:
                    continue
                dbm = _olt_raw_to_dbm(v, olt)
                if dbm is not None and -10 <= dbm <= 10:
                    tx_map[sfx] = dbm
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    created = updated = skipped_manual = skipped = 0
    with db_lock:
        conn, c = get_db()
        try:
            c.execute("SELECT id, source, ont_index FROM fiber_onts WHERE olt_name COLLATE NOCASE = ?",
                      (olt["name"],))
            existing = {}
            for r in c.fetchall():
                idx = (r["ont_index"] or "").strip()
                if idx:
                    existing[idx] = dict(r)
            for sfx in suffixes:
                rx_dbm = rx_map[sfx]
                tx_dbm = tx_map.get(sfx)
                ex = existing.get(sfx)
                if ex and (ex.get("source") or "manual") != "snmp":
                    skipped_manual += 1
                    continue
                status, _, _, _ = fiber_eval(rx_dbm, tx_dbm, _fiber_thresholds())
                if ex:
                    if not do_update:
                        skipped += 1
                        continue
                    c.execute("UPDATE fiber_onts SET rx_power=?, tx_power=?, status=?,"
                              " last_checked=?, last_seen=? WHERE id=?",
                              (rx_dbm, tx_dbm, status, now, now, ex["id"]))
                    c.execute("INSERT INTO fiber_history (ont_id, rx_power, tx_power, timestamp)"
                              " VALUES (?,?,?,?)", (ex["id"], rx_dbm, tx_dbm, now))
                    fiber_alarm_memory[ex["id"]] = status
                    updated += 1
                    continue
                sn = _discover_sn(olt["name"], sfx)
                try:
                    c.execute("""INSERT INTO fiber_onts(ont_sn,customer,olt_name,pon_port,odp_name,
                               rx_power,tx_power,status,last_checked,source,created_at,updated_at,
                               mute_alarm,rx_warn,rx_crit,ont_index,last_seen)
                               VALUES(?,?,?,?,?,?,?,?,?,?,?, ?,?,?,?,?,?)""",
                              (sn, "", olt["name"], "", "", rx_dbm, tx_dbm, status,
                               now, "snmp", now, now, 0, None, None, sfx, now))
                    nid = c.lastrowid
                except sqlite3.IntegrityError:
                    skipped += 1
                    continue
                c.execute("INSERT INTO fiber_history (ont_id, rx_power, tx_power, timestamp)"
                          " VALUES (?,?,?,?)", (nid, rx_dbm, tx_dbm, now))
                fiber_alarm_memory[nid] = status
                created += 1
            _commit_with_retry(conn)
        finally:
            conn.close()
    try:
        audit(current_user.username, "olt.discover",
              f"{olt['name']} created={created} updated={updated}")
    except Exception:
        pass
    return jsonify({"status": "success", "olt": olt["name"], "walked": len(suffixes),
                    "created": created, "updated": updated,
                    "skipped_manual": skipped_manual, "skipped": skipped})


_TOPO_RANK = {"critical": 5, "overload": 4, "warning": 3, "stale": 2,
              "unknown": 1, "normal": 0}


def _topo_counts(members):
    """(counts, worst) untuk sekelompok ONT yang sudah punya _status."""
    counts = {"total": len(members), "normal": 0, "warning": 0, "critical": 0,
              "overload": 0, "stale": 0, "unknown": 0}
    worst, rank = "normal", -1
    for m in members:
        st = m.get("_status", "unknown")
        if st in counts:
            counts[st] += 1
        if _TOPO_RANK.get(st, 0) > rank:
            worst, rank = st, _TOPO_RANK.get(st, 0)
    return counts, (worst if members else "normal")


@app.route("/api/fiber/topology", methods=["GET"])
@api_login_required
def api_fiber_topology():
    """Pohon OLT -> ODP -> ONT + koordinat untuk peta.

    Pengelompokan case-insensitive mengikuti agregasi ODP; nama liar
    (tak terdaftar) dan bucket tanpa ODP/OLT tetap ditampilkan.
    Field sensitif OLT (community) tak pernah dikirim.
    """
    conn, c = get_db()
    try:
        try:
            c.execute("SELECT * FROM olts ORDER BY name ASC")
            olts = [dict(r) for r in c.fetchall()]
        except sqlite3.OperationalError:
            olts = []
        try:
            c.execute("SELECT * FROM odps ORDER BY name ASC")
            odps = [dict(r) for r in c.fetchall()]
        except sqlite3.OperationalError:
            odps = []
        try:
            c.execute("SELECT * FROM fiber_onts ORDER BY id ASC")
            onts = [dict(r) for r in c.fetchall()]
        except sqlite3.OperationalError:
            onts = []
    finally:
        try:
            conn.close()
        except Exception:
            pass
    for o in onts:
        try:
            o["_status"] = _fiber_row_status(o)[0]
        except Exception:
            o["_status"] = "unknown"
    olt_by_key = {(o.get("name") or "").strip().lower(): o for o in olts}
    odp_groups = {}
    for d in odps:
        raw_olt = (d.get("olt_name") or "").strip()
        canon = olt_by_key.get(raw_olt.lower())
        name = (canon.get("name") if canon else raw_olt) or ""
        odp_groups.setdefault(name.strip().lower(), {"display": name, "items": []})["items"].append(d)
    ont_groups = {}
    for o in onts:
        raw_olt = (o.get("olt_name") or "").strip()
        canon = olt_by_key.get(raw_olt.lower())
        dkey = ((canon.get("name") if canon else raw_olt) or "").strip().lower()
        pkey = (o.get("odp_name") or "").strip().lower()
        ont_groups.setdefault((dkey, pkey), []).append(o)

    def _ont_node(o):
        return {"id": o["id"], "ont_sn": o.get("ont_sn"), "customer": o.get("customer"),
                "pon_port": o.get("pon_port"), "rx_power": o.get("rx_power"),
                "tx_power": o.get("tx_power"), "status": o.get("_status", "unknown")}

    def _sorted_onts(items):
        return sorted((_ont_node(o) for o in items),
                      key=lambda x: (-_TOPO_RANK.get(x["status"], 0),
                                     x["rx_power"] if x["rx_power"] is not None else 99,
                                     x["id"]))

    out = []
    reg_keys = [(o.get("name") or "").strip().lower() for o in olts]
    wild_keys = sorted({k for k in list(odp_groups) + [dk for dk, _ in ont_groups]
                        if k not in reg_keys})
    for dkey in reg_keys + wild_keys:
        canon = olt_by_key.get(dkey)
        display = (canon.get("name") if canon
                   else odp_groups.get(dkey, {}).get("display") or "")
        if not display:
            display = "(tanpa OLT)"
        olt_info = ({"id": canon["id"], "name": canon.get("name"), "ip": canon.get("ip"),
                     "vendor": canon.get("vendor"), "lat": canon.get("lat"),
                     "lon": canon.get("lon")} if canon else None)
        node_odps, all_onts = [], []
        for d in sorted(odp_groups.get(dkey, {}).get("items", []),
                        key=lambda x: (x.get("name") or "").lower()):
            members = ont_groups.pop((dkey, (d.get("name") or "").strip().lower()), [])
            all_onts.extend(members)
            counts, worst = _topo_counts(members)
            cap = d.get("capacity") or 0
            node_odps.append({
                "name": d.get("name"), "registered": True,
                "odp": {"id": d["id"], "name": d.get("name"), "olt_name": d.get("olt_name"),
                        "capacity": cap, "location": d.get("location"),
                        "lat": d.get("lat"), "lon": d.get("lon")},
                "counts": counts, "worst": worst,
                "fill_pct": round(len(members) / cap * 100, 1) if cap else 0,
                "onts": _sorted_onts(members),
            })
        leftovers = sorted(
            [(pk, ont_groups.pop((dkey, pk))) for (dk, pk) in
             [k for k in list(ont_groups) if k[0] == dkey]],
            key=lambda t: (t[0] != "", t[0]),
        )
        for pkey, members in leftovers:
            all_onts.extend(members)
            label = next((o.get("odp_name") or "" for o in members
                          if (o.get("odp_name") or "").strip()), "")
            counts, worst = _topo_counts(members)
            node_odps.append({
                "name": label.strip() or "(tanpa ODP)", "registered": False,
                "odp": {"id": 0, "name": label.strip(),
                        "olt_name": display if display != "(tanpa OLT)" else "",
                        "capacity": 0, "location": "", "lat": None, "lon": None},
                "counts": counts, "worst": worst, "fill_pct": 0,
                "onts": _sorted_onts(members),
            })
        if not node_odps and not canon:
            continue
        counts, worst = _topo_counts(all_onts)
        out.append({"name": display, "registered": bool(canon), "olt": olt_info,
                    "counts": counts, "worst": worst, "odps": node_odps})
    return jsonify({"olts": out})


@app.route("/api/fiber/link-budget", methods=["POST"])
@api_login_required
def api_fiber_link_budget():
    data = request.get_json(silent=True) or {}

    def _num(v, name, lo, hi):
        try:
            f = float(v)
        except (ValueError, TypeError):
            raise ValueError(f"{name} harus angka")
        if not lo <= f <= hi:
            raise ValueError(f"{name} harus {lo}..{hi}")
        return f

    try:
        tx = _num(data.get("tx_dbm"), "tx_dbm", -10, 10)
        fiber_km = _num(data.get("fiber_km", 0), "fiber_km", 0, 100)
        connectors = int(_num(data.get("connectors", 4), "connectors", 0, 50))
        splices = int(_num(data.get("splices", 0), "splices", 0, 100))
        margin = _num(data.get("margin_db", 0), "margin_db", 0, 10)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    splitters = data.get("splitters") or []
    if not isinstance(splitters, list) or not 1 <= len(splitters) <= 3:
        return jsonify({"error": "splitters harus list 1-3 tingkat (mis. [\"1:4\",\"1:8\"])"}), 400
    if any(s not in FIBER_SPLITTER_LOSS for s in splitters):
        return jsonify({"error": f"splitter harus salah satu {sorted(FIBER_SPLITTER_LOSS)}"}), 400

    result = fiber_link_budget(tx, splitters, fiber_km, connectors, splices, margin)
    actual_rx = None
    ont = None
    ont_id = data.get("ont_id")
    if ont_id is not None:
        try:
            ont_id = int(ont_id)
        except (ValueError, TypeError):
            return jsonify({"error": "ont_id harus angka"}), 400
        conn, c = get_db()
        try:
            c.execute("SELECT id, ont_sn, customer, rx_power FROM fiber_onts WHERE id=?", (ont_id,))
            row = c.fetchone()
        finally:
            conn.close()
        if not row:
            return jsonify({"error": "ONT tidak ditemukan"}), 404
        ont = {"id": row["id"], "ont_sn": row["ont_sn"], "customer": row["customer"]}
        actual_rx = row["rx_power"]
    verdict, severity, advice = fiber_budget_verdict(result["expected_rx"], actual_rx)
    return jsonify({**result, "splitters": splitters,
                    "fiber_km": fiber_km, "connectors": connectors,
                    "splices": splices, "ont": ont, "actual_rx": actual_rx,
                    "delta_db": round(actual_rx - result["expected_rx"], 2) if actual_rx is not None else None,
                    "verdict": verdict, "severity": severity, "advice": advice})


# ---------------- MIKROTIK ----------------
@app.route("/mikrotik")
@login_required
def mikrotik_page():
    return render_template("mikrotik.html")


def _mt_evaluate(host, cpu, mem, sto, temp):
    """Status gabungan device. Kembalikan (status, severity, advice)."""
    try:
        temp_warn = get_setting("temp_threshold", 60.0)
        temp_crit = get_setting("temp_crit", 75.0)
        cpu_thresh = get_setting("cpu_threshold", 85.0)
        mem_thresh = get_setting("ram_threshold", 90.0)
        st_thresh = get_setting("disk_threshold", 90.0)
    except Exception:
        temp_warn, temp_crit, cpu_thresh, mem_thresh, st_thresh = 60.0, 75.0, 85.0, 90.0, 90.0
    notes = []
    sev = None

    def _worse(s):
        nonlocal sev
        if _SEV_RANK.get(s, 0) > _SEV_RANK.get(sev or "", 0):
            sev = s

    if temp is not None:
        if temp >= temp_crit:
            notes.append(f"suhu kritis {temp}°C")
            _worse("high")
        elif temp >= temp_warn:
            notes.append(f"suhu tinggi {temp}°C")
            _worse("warning")
    if cpu is not None and cpu > cpu_thresh:
        notes.append(f"CPU {cpu}%")
        _worse("warning")
    if mem is not None and mem > mem_thresh:
        notes.append(f"memory {mem}%")
        _worse("warning")
    if sto is not None and sto > st_thresh:
        notes.append(f"storage {sto}%")
        _worse("warning")
    if cpu is None and mem is None and sto is None and temp is None:
        return "unknown", None, "Belum ada data SNMP."
    if not notes:
        return "normal", None, "CPU/RAM/storage/suhu normal."
    status = "critical" if sev == "high" else "warning"
    return status, sev, ", ".join(notes) + " melebihi batas."


@app.route("/api/mikrotik", methods=["GET"])
@api_login_required
def api_mikrotik_list():
    conn, c = get_db()
    try:
        c.execute("SELECT ip, alias, category, snmp_community, snmp_profile FROM hosts ORDER BY id ASC")
        hosts = [dict(r) for r in c.fetchall()]
        out = []
        for h in hosts:
            ip = h["ip"]
            if not (h.get("snmp_community") or "").strip():
                continue
            if (h.get("snmp_profile") or "auto") == "generic":
                continue
            c.execute("SELECT cpu, mem_used, storage_used, temp_c, uptime_s, timestamp"
                      " FROM device_health WHERE host=? ORDER BY id DESC LIMIT 1", (ip,))
            r = c.fetchone()
            if r:
                try:
                    uptime_s = None if r["uptime_s"] is None else float(r["uptime_s"])
                except (ValueError, TypeError, KeyError, IndexError):
                    uptime_s = None
                status, severity, advice = _mt_evaluate(
                    ip, r["cpu"], r["mem_used"], r["storage_used"], r["temp_c"])
                stale, stale_age = _mt_stale_info(r["timestamp"])
                if stale and status in ("normal", "warning"):
                    status, severity = "stale", "warning"
                    advice = (f"Tanpa data SNMP baru sejak {stale_age} "
                              f"(terakhir {r['timestamp'] or '—'}). Kemungkinan host mati, "
                              "community diganti, atau UDP 161 diblokir.")
                out.append({"host": ip, "alias": (h.get("alias") or "").strip() or ip,
                            "category": (h.get("category") or "").strip() or "Uncategorized",
                            "cpu": r["cpu"], "mem": r["mem_used"], "storage": r["storage_used"],
                            "temp_c": r["temp_c"], "uptime_s": uptime_s,
                            "uptime": _fmt_uptime(uptime_s), "last_seen": r["timestamp"],
                            "is_mikrotik": bool(mt_is_mikrotik.get(ip)),
                            "stale": stale, "stale_age": stale_age,
                            "status": status, "severity": severity, "advice": advice})
            else:
                out.append({"host": ip, "alias": (h.get("alias") or "").strip() or ip,
                            "category": (h.get("category") or "").strip() or "Uncategorized",
                            "cpu": None, "mem": None, "storage": None, "temp_c": None,
                            "last_seen": None, "is_mikrotik": bool(mt_is_mikrotik.get(ip)),
                            "status": "unknown", "severity": None,
                            "advice": "Menunggu poll SNMP berikutnya."})
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return jsonify(out)


@app.route("/api/mikrotik/<path:host>/history")
@api_login_required
def api_mikrotik_history(host):
    try:
        hours = int(request.args.get("hours", 24))
    except (ValueError, TypeError):
        return jsonify({"error": "hours harus angka 1-336"}), 400
    hours = max(1, min(hours, 336))
    metric = request.args.get("metric", "cpu")
    cols = {"cpu": "cpu", "mem": "mem_used", "storage": "storage_used", "temp": "temp_c"}
    col = cols.get(metric, "cpu")
    conn, c = get_db()
    try:
        c.execute(f"SELECT timestamp, {col} AS val FROM device_health"
                  " WHERE host=? AND timestamp > datetime('now','localtime',?) ORDER BY id ASC",
                  (host, f"-{hours} hours"))
        rows = c.fetchall()
    finally:
        conn.close()
    labels = [r["timestamp"].split(" ")[1] if r["timestamp"] and " " in r["timestamp"] else r["timestamp"]
              for r in rows]
    return jsonify({"host": host, "metric": metric, "hours": hours,
                    "labels": labels, "values": [r["val"] for r in rows],
                    "count": len(rows)})


def _fmt_uptime(s):
    if s is None:
        return None
    try:
        s = int(s)
    except (ValueError, TypeError):
        return None
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, _s = divmod(s, 60)
    if d:
        return f"{d}h {h}j"
    if h:
        return f"{h}j {m}m"
    return f"{m}m"


@app.route("/api/mikrotik/<path:host>/interfaces", methods=["GET"])
@api_login_required
def api_mt_iface_list(host):
    conn, c = get_db()
    try:
        c.execute("SELECT ip FROM hosts WHERE ip=?", (host,))
        if not c.fetchone():
            return jsonify({"error": "Host tidak ditemukan"}), 404
        c.execute("SELECT if_index, name, oper, monitor, last_changed FROM snmp_interfaces"
                  " WHERE host=? ORDER BY if_index ASC", (host,))
        out = []
        for r in c.fetchall():
            d = dict(r)
            c.execute("SELECT net_in, net_out, timestamp FROM iface_traffic"
                      " WHERE host=? AND if_index=? ORDER BY id DESC LIMIT 1",
                      (host, d["if_index"]))
            last = c.fetchone()
            d["last_in"] = last["net_in"] if last else None
            d["last_out"] = last["net_out"] if last else None
            d["last_seen"] = last["timestamp"] if last else None
            out.append(d)
    finally:
        conn.close()
    return jsonify(out)


@app.route("/api/mikrotik/<path:host>/interfaces/discover", methods=["POST"])
@api_login_required
def api_mt_iface_discover(host):
    conn, c = get_db()
    try:
        c.execute("SELECT snmp_community, snmp_profile FROM hosts WHERE ip=?", (host,))
        h = c.fetchone()
    finally:
        conn.close()
    if not h:
        return jsonify({"error": "Host tidak ditemukan"}), 404
    community = (h["snmp_community"] or "").strip()
    if not community:
        return jsonify({"error": "Host belum punya SNMP community"}), 400
    if (h["snmp_profile"] or "auto") == "generic":
        return jsonify({"error": "Profil host generic (SNMP health nonaktif)"}), 400
    found = discover_interfaces(host, community)
    if not found:
        return jsonify({"error": "Tidak ada interface terjawab (cek community/firewall/UDP 161)"}), 502
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with db_lock:
        conn, c = get_db()
        try:
            for f in found:
                c.execute("INSERT INTO snmp_interfaces (host, if_index, name, oper, monitor, last_changed)"
                          " VALUES (?,?,?,?,1,?)"
                          " ON CONFLICT(host, if_index) DO UPDATE SET name=excluded.name,"
                          " oper=excluded.oper, last_changed=excluded.last_changed",
                          (host, f["if_index"], f["name"][:64], f["oper"], now))
            conn.commit()
        finally:
            conn.close()
    try:
        audit(current_user.username, "mt.discover", f"{host} {len(found)} iface")
    except Exception:
        pass
    # langsung poll sekali agar trafik langsung terisi
    try:
        threading.Thread(target=poll_mikrotik_ifaces, daemon=True).start()
    except Exception:
        pass
    return jsonify({"status": "success", "count": len(found),
                    "truncated": len(found) >= DISCOVER_MAX_IF, "interfaces": found})


@app.route("/api/mikrotik/<path:host>/interfaces", methods=["PATCH"])
@api_login_required
def api_mt_iface_update(host):
    data = request.get_json(silent=True) or {}
    try:
        idx = int(data.get("if_index", 0))
    except (ValueError, TypeError):
        return jsonify({"error": "if_index harus angka"}), 400
    if idx < 1:
        return jsonify({"error": "if_index harus >= 1"}), 400
    sets, params = [], []
    if "monitor" in data:
        sets.append("monitor=?")
        params.append(1 if data.get("monitor") else 0)
    if "name" in data:
        name = str(data.get("name") or "").strip()[:64]
        if not name:
            return jsonify({"error": "Nama tidak boleh kosong"}), 400
        sets.append("name=?")
        params.append(name)
    if not sets:
        return jsonify({"error": "Tidak ada field yang dikirim"}), 400
    conn, c = get_db()
    c.execute("SELECT 1 FROM snmp_interfaces WHERE host=? AND if_index=?", (host, idx))
    if not c.fetchone():
        conn.close()
        return jsonify({"error": "Interface tidak ditemukan (discover dulu)"}), 404
    c.execute(f"UPDATE snmp_interfaces SET {', '.join(sets)} WHERE host=? AND if_index=?",
              (*params, host, idx))
    conn.commit()
    conn.close()
    try:
        audit(current_user.username, "mt.iface", f"{host} if{idx} {sets}")
    except Exception:
        pass
    return jsonify({"status": "success"})


@app.route("/api/mikrotik/<path:host>/interfaces/<int:idx>", methods=["DELETE"])
@api_login_required
def api_mt_iface_delete(host, idx):
    conn, c = get_db()
    c.execute("DELETE FROM snmp_interfaces WHERE host=? AND if_index=?", (host, idx))
    deleted = c.rowcount
    try:
        c.execute("DELETE FROM iface_traffic WHERE host=? AND if_index=?", (host, idx))
    except sqlite3.OperationalError:
        pass
    conn.commit()
    conn.close()
    try:
        mt_iface_oper.pop((host, idx), None)
        iface_state.pop((host, idx), None)
        mt_iface_tg.pop((host, idx), None)
        mt_iface_flaps.pop((host, idx), None)
    except Exception:
        pass
    if not deleted:
        return jsonify({"error": "Interface tidak ditemukan"}), 404
    try:
        audit(current_user.username, "mt.iface_delete", f"{host} if{idx}")
    except Exception:
        pass
    return jsonify({"status": "success"})


def _mt_backup_host_row(host):
    conn, c = get_db()
    try:
        c.execute("SELECT ip, ssh_user, ssh_pass, ssh_port, backup_enable,"
                  " backup_last, backup_ok FROM hosts WHERE ip=?", (host,))
        row = c.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


@app.route("/api/mikrotik/<path:host>/backups", methods=["GET"])
@api_login_required
def api_mt_backup_list(host):
    if not _mt_backup_host_row(host):
        return jsonify({"error": "Host tidak ditemukan"}), 404
    conn, c = get_db()
    try:
        c.execute("SELECT id, taken_at, size, sha256, changed FROM mt_backups"
                  " WHERE host=? ORDER BY id DESC LIMIT 50", (host,))
        out = [dict(r) for r in c.fetchall()]
    finally:
        conn.close()
    return jsonify(out)


@app.route("/api/mikrotik/<path:host>/backups/<int:bid>", methods=["GET"])
@api_login_required
def api_mt_backup_get(host, bid):
    conn, c = get_db()
    try:
        c.execute("SELECT id, host, taken_at, size, sha256, content FROM mt_backups"
                  " WHERE host=? AND id=?", (host, bid))
        row = c.fetchone()
    finally:
        conn.close()
    if not row:
        return jsonify({"error": "Backup tidak ditemukan"}), 404
    d = dict(row)
    if (request.args.get("download") or "").strip() == "1":
        safe = re.sub(r"[^A-Za-z0-9_.\-]", "_", host)[:48]
        return Response(d.get("content") or "", mimetype="text/plain",
                        headers={"Content-Disposition":
                                 f"attachment; filename={safe}_{d.get('taken_at','')[:10]}.rsc"})
    return jsonify({k: d.get(k) for k in ("id", "host", "taken_at", "size", "sha256", "content")})


@app.route("/api/mikrotik/<path:host>/backups/<int:bid>/diff", methods=["GET"])
@api_login_required
def api_mt_backup_diff(host, bid):
    conn, c = get_db()
    try:
        c.execute("SELECT id, taken_at, content FROM mt_backups"
                  " WHERE host=? AND id<=? ORDER BY id DESC LIMIT 2", (host, bid))
        rows = [dict(r) for r in c.fetchall()]
    finally:
        conn.close()
    if not rows or rows[0]["id"] != bid:
        return jsonify({"error": "Backup tidak ditemukan"}), 404
    cur = rows[0]
    prev = rows[1] if len(rows) > 1 else None
    if not prev:
        return jsonify({"id": bid, "vs": None, "diff": [],
                        "note": "versi pertama (baseline, tanpa pembanding)"})
    diff = list(difflib.unified_diff((prev.get("content") or "").splitlines(),
                                     (cur.get("content") or "").splitlines(),
                                     fromfile=f"#{prev['id']} {prev['taken_at']}",
                                     tofile=f"#{cur['id']} {cur['taken_at']}", n=3))
    if len(diff) > 500:
        diff = diff[:500] + [f"... dipotong, total {len(diff)} baris"]
    return jsonify({"id": bid, "vs": prev["id"], "diff": diff,
                    "lines": len(diff)})


@app.route("/api/mikrotik/<path:host>/backup", methods=["POST"])
@api_login_required
def api_mt_backup_now(host):
    row = _mt_backup_host_row(host)
    if not row:
        return jsonify({"error": "Host tidak ditemukan"}), 404
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        ok, payload = fetch_mikrotik_config(
            host, row.get("ssh_user") or "", row.get("ssh_pass") or "",
            row.get("ssh_port") or 22)
    except Exception as e:
        return jsonify({"error": f"fetch gagal: {e}"}), 502
    tg_msg = None
    with db_lock:
        conn, c = get_db()
        try:
            tg_msg = _store_mt_backup(c, host, ok, payload, timestamp)
            _commit_with_retry(conn)
        except sqlite3.OperationalError as e:
            try:
                conn.rollback()
            except Exception:
                pass
            return jsonify({"error": f"DB sibuk: {e}"}), 503
        finally:
            conn.close()
    if tg_msg:
        try:
            send_telegram_alert(tg_msg)
        except Exception as e:
            print(f"[WARN] telegram backup gagal: {e}")
    try:
        audit(current_user.username, "mt.backup_now", f"{host} ok={ok}")
    except Exception:
        pass
    if not ok:
        return jsonify({"error": payload}), 502
    return jsonify({"status": "success", "taken_at": timestamp,
                    "notified": bool(tg_msg)})


@app.route("/api/mikrotik/<path:host>/iface/<int:idx>/history")
@api_login_required
def api_mt_iface_history(host, idx):
    try:
        hours = int(request.args.get("hours", 24))
    except (ValueError, TypeError):
        return jsonify({"error": "hours harus angka 1-336"}), 400
    hours = max(1, min(hours, 336))
    conn, c = get_db()
    try:
        c.execute("SELECT name FROM snmp_interfaces WHERE host=? AND if_index=?", (host, idx))
        iface = c.fetchone()
        if not iface:
            return jsonify({"error": "Interface tidak ditemukan"}), 404
        c.execute("SELECT timestamp, net_in, net_out FROM iface_traffic"
                  " WHERE host=? AND if_index=? AND timestamp > datetime('now','localtime',?)"
                  " ORDER BY id ASC", (host, idx, f"-{hours} hours"))
        rows = c.fetchall()
    finally:
        conn.close()
    # downsample seperti fiber (14 hari = ~20rb titik bisa bekukan browser)
    if len(rows) > 500:
        step = (len(rows) + 499) // 500
        rows = rows[::step]
    labels = [r["timestamp"].split(" ")[1] if r["timestamp"] and " " in r["timestamp"] else r["timestamp"]
              for r in rows]
    return jsonify({"host": host, "if_index": idx, "name": iface["name"], "hours": hours,
                    "labels": labels, "rx": [r["net_in"] for r in rows],
                    "tx": [r["net_out"] for r in rows], "count": len(rows)})


@app.route("/inventory")
@login_required
def inventory_page():
    return render_template("inventory.html")


@app.route("/api/inventory", methods=["GET"])
@api_login_required
def api_inventory_list():
    conn, c = get_db()
    c.execute("SELECT * FROM inventory ORDER BY hostname ASC")
    rows = [dict(r) for r in c.fetchall()]
    c.execute("SELECT ip FROM hosts")
    monitored = {r["ip"] for r in c.fetchall()}

    last_map = {}
    try:
        c.execute("""
            SELECT p.host, p.latency, p.timestamp FROM ping_logs p
            INNER JOIN (SELECT host, MAX(id) AS mid FROM ping_logs GROUP BY host) m
            ON m.mid = p.id
        """)
        for r in c.fetchall():
            last_map[r["host"]] = (r["latency"], r["timestamp"])
    except Exception:
        last_map = {}
    out = []
    for inv in rows:
        is_monitored = inv["ip"] in monitored
        if inv["ip"] in last_map:
            lat, ts = last_map[inv["ip"]]
            live = "up" if lat != -1 else "down"
            last_latency = round(lat, 2) if lat != -1 else None
            last_seen = ts
        else:

            live = "pending" if is_monitored else "unmonitored"
            last_latency = None
            last_seen = None
        out.append({**inv, "live": live,
                    "last_latency": last_latency,
                    "last_seen": last_seen,
                    "monitored": is_monitored})
    conn.close()
    return jsonify(out)


IPV4_RE = re.compile(r"^(\d{1,3}\.){3}\d{1,3}$")

INVENTORY_ASSET_STATUS = {"aktif", "cadangan", "rusak", "hilang"}

def _validate_inventory(d):
    hostname = (d.get("hostname") or "").strip()
    ip = (d.get("ip") or "").strip()
    if not hostname or not ip:
        return None, "Hostname & IP wajib diisi"
    if len(hostname) > 100 or len(ip) > 45:
        return None, "Hostname/IP terlalu panjang"
    if not IPV4_RE.match(ip):
        return None, "Format IP tidak valid"
    try:
        if any(int(p) > 255 for p in ip.split(".")):
            return None, "Format IP tidak valid"
    except ValueError:
        return None, "Format IP tidak valid"
    device_type = (d.get("device_type") or "").strip()
    brand_model = (d.get("brand_model") or "").strip()
    location = (d.get("location") or "").strip()
    pic_name = (d.get("pic_name") or "").strip()
    pic_phone = (d.get("pic_phone") or "").strip()
    install_date = (d.get("install_date") or "").strip()
    asset_status = (d.get("asset_status") or "aktif").strip().lower()
    asset_no = (d.get("asset_no") or "").strip()
    notes = (d.get("notes") or "").strip()
    if asset_status not in INVENTORY_ASSET_STATUS:
        return None, "Status aset harus aktif/cadangan/rusak/hilang"
    if len(device_type) > 50 or len(brand_model) > 100 or len(location) > 100:
        return None, "Tipe/merek/lokasi terlalu panjang"
    if len(pic_name) > 100 or len(pic_phone) > 30 or len(asset_no) > 50 or len(notes) > 500:
        return None, "Data PIC/aset/catatan terlalu panjang"
    if install_date and not re.match(r"^\d{4}-\d{2}-\d{2}$", install_date):
        return None, "Tanggal pasang harus YYYY-MM-DD"
    if install_date:
        try:
            datetime.strptime(install_date, "%Y-%m-%d")
        except ValueError:
            return None, "Tanggal pasang tidak valid"
    return {"hostname": hostname, "ip": ip,
            "device_type": device_type,
            "brand_model": brand_model,
            "location": location,
            "pic_name": pic_name,
            "pic_phone": pic_phone,
            "install_date": install_date,
            "asset_status": asset_status,
            "asset_no": asset_no,
            "notes": notes}, None


@app.route("/api/inventory", methods=["POST"])
@api_login_required
def api_inventory_create():
    vals, err = _validate_inventory(request.get_json(silent=True) or {})
    if err:
        return jsonify({"error": err}), 400
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn, c = get_db()
    try:
        c.execute("""INSERT INTO inventory(hostname,ip,device_type,brand_model,location,
                     pic_name,pic_phone,install_date,asset_status,asset_no,notes,created_at,updated_at)
                     VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (vals["hostname"], vals["ip"], vals["device_type"], vals["brand_model"],
                   vals["location"], vals["pic_name"], vals["pic_phone"], vals["install_date"],
                   vals["asset_status"], vals["asset_no"], vals["notes"], now, now))
        conn.commit()
        iid = c.lastrowid
    except sqlite3.IntegrityError:
        conn.rollback()
        conn.close()
        return jsonify({"error": "IP sudah terdaftar di inventory"}), 400
    conn.close()
    audit(current_user.username, "inventory.create", f"{vals['hostname']} {vals['ip']}")
    return jsonify({"status": "ok", "id": iid})


@app.route("/api/inventory/<int:iid>", methods=["PUT"])
@api_login_required
def api_inventory_update(iid):
    vals, err = _validate_inventory(request.get_json(silent=True) or {})
    if err:
        return jsonify({"error": err}), 400
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn, c = get_db()
    try:
        c.execute("""UPDATE inventory SET hostname=?, ip=?, device_type=?, brand_model=?, location=?,
                     pic_name=?, pic_phone=?, install_date=?, asset_status=?, asset_no=?, notes=?, updated_at=?
                     WHERE id=?""",
                  (vals["hostname"], vals["ip"], vals["device_type"], vals["brand_model"],
                   vals["location"], vals["pic_name"], vals["pic_phone"], vals["install_date"],
                   vals["asset_status"], vals["asset_no"], vals["notes"], now, iid))
        if c.rowcount == 0:
            conn.rollback()
            conn.close()
            return jsonify({"error": "Data tidak ditemukan"}), 404
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        conn.close()
        return jsonify({"error": "IP sudah dipakai data lain"}), 400
    conn.close()
    audit(current_user.username, "inventory.update", str(iid))
    return jsonify({"status": "ok"})


@app.route("/api/inventory/<int:iid>", methods=["DELETE"])
@api_login_required
def api_inventory_delete(iid):
    conn, c = get_db()
    c.execute("DELETE FROM inventory WHERE id=?", (iid,))
    deleted = c.rowcount
    conn.commit()
    conn.close()
    if not deleted:
        return jsonify({"error": "Data tidak ditemukan"}), 404
    audit(current_user.username, "inventory.delete", str(iid))
    return jsonify({"status": "ok"})


def _csv_safe(v):
    if v is None or isinstance(v, (int, float)):
        return v
    s = str(v).replace("\r", " ").replace("\n", " ")
    if s[:1] in ("=", "+", "-", "@", "|", "%") or s[:1] in ("\t",):
        return "'" + s
    return s


@app.route("/api/inventory/export")
@api_login_required
def api_inventory_export():
    conn, c = get_db()
    c.execute("SELECT * FROM inventory ORDER BY hostname ASC")
    rows = c.fetchall()
    conn.close()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "hostname", "ip", "tipe", "merek_model", "lokasi", "pic", "no_hp",
                "tgl_pasang", "status_aset", "no_aset", "catatan"])
    for r in rows:
        w.writerow([r["id"], _csv_safe(r["hostname"]), _csv_safe(r["ip"]), _csv_safe(r["device_type"]),
                    _csv_safe(r["brand_model"]), _csv_safe(r["location"]), _csv_safe(r["pic_name"]),
                    _csv_safe(r["pic_phone"]), _csv_safe(r["install_date"]), _csv_safe(r["asset_status"]),
                    _csv_safe(r["asset_no"]), _csv_safe(r["notes"])])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=inventory.csv"})


@app.route("/triggers")
@login_required
def triggers_page():
    return render_template("triggers.html")


@app.route("/api/triggers")
@api_login_required
def get_triggers():
    global status_memory, agent_status_memory, agent_offline_memory


    valid_hosts = set(get_target_hosts())
    maint_map = get_active_maintenance_map()

    alarms = []


    for host, is_down in list(status_memory.items()):
        if host not in valid_hosts:
            continue
        if is_down:
            if host in maint_map:
                reason = (maint_map[host].get("reason") or "").strip()[:200]
                alarms.append({
                    "host": host,
                    "severity": "warning",
                    "message": f"In maintenance (DOWN suppressed){(' - ' + reason) if reason else ''}",
                    "category": "maintenance"
                })
            else:
                alarms.append({
                    "host": host,
                    "severity": "disaster",
                    "message": "Host is DOWN (Unreachable)",
                    "category": "availability"
                })


    for host, is_offline in list(agent_offline_memory.items()):
        if host not in valid_hosts:
            continue
        if host in maint_map:
            continue
        if is_offline and not status_memory.get(host, False):
            alarms.append({
                "host": host,
                "severity": "high",
                "message": "NMS Agent is Offline / Not reporting",
                "category": "agent"
            })


    for host, status in list(agent_status_memory.items()):
        if host not in valid_hosts:
            continue
        if not status_memory.get(host, False) and not agent_offline_memory.get(host, False):
            if status.get("cpu", False):
                alarms.append({"host": host, "severity": "warning", "message": "High CPU Usage", "category": "resource"})
            if status.get("ram", False):
                alarms.append({"host": host, "severity": "warning", "message": "High RAM Usage", "category": "resource"})
            if status.get("disk", False):
                alarms.append({"host": host, "severity": "warning", "message": "High Disk Usage", "category": "resource"})

    try:
        conn, c = get_db()
        try:
            c.execute("SELECT id, ip, name, type, url, status, ssl_days_left, ssl_expires_at FROM services")
            for r in c.fetchall():
                d = dict(r)
                st = (d.get("status") or "").upper()
                label = f"{d.get('name')} ({d.get('ip')})"
                if st == "OFFLINE":
                    alarms.append({
                        "host": label,
                        "severity": "high",
                        "message": f"Service OFFLINE: {d.get('name')} [{d.get('type')}]",
                        "category": "service",
                    })
                lvl = _ssl_level(d.get("ssl_days_left"))
                if lvl and (d.get("url") or "").lower().startswith("https://"):
                    try:
                        days = int(d.get("ssl_days_left") or 0)
                    except (ValueError, TypeError):
                        days = None
                    if lvl == "expired":
                        alarms.append({
                            "host": label,
                            "severity": "disaster",
                            "message": f"SSL EXPIRED: {d.get('url')} (exp {d.get('ssl_expires_at') or '-'})",
                            "category": "service",
                        })
                    elif lvl == "high":
                        alarms.append({
                            "host": label,
                            "severity": "high",
                            "message": f"SSL expire H-{days}: {d.get('url')} (exp {d.get('ssl_expires_at') or '-'})",
                            "category": "service",
                        })
                    else:
                        alarms.append({
                            "host": label,
                            "severity": "warning",
                            "message": f"SSL expire H-{days}: {d.get('url')} (exp {d.get('ssl_expires_at') or '-'})",
                            "category": "service",
                        })
        finally:
            conn.close()
    except Exception as e:
        print(f"[TRIGGERS] service/ssl gagal: {e}")

    try:
        conn, c = get_db()
        try:
            c.execute("SELECT * FROM fiber_onts")
            rows = [dict(r) for r in c.fetchall()]
        finally:
            conn.close()
        maint_map = get_active_maintenance_map()
        sev_rows = []
        for d in rows:
            try:
                rx = None if d.get("rx_power") is None else float(d.get("rx_power"))
            except (ValueError, TypeError):
                continue
            try:
                tx = None if d.get("tx_power") is None else float(d.get("tx_power"))
            except (ValueError, TypeError):
                tx = None
            if rx is None and tx is None:
                continue
            in_maint, maint_reason, maint_scope = _fiber_maintenance_info(d, maint_map)
            if in_maint:
                scope_txt = f" ({maint_scope.upper()})" if maint_scope and maint_scope != "ont" else ""
                alarms.append({
                    "host": f"{d.get('ont_sn')}",
                    "severity": "warning",
                    "message": f"Fiber dalam maintenance{scope_txt} — alarm disuppress{(' - ' + maint_reason) if maint_reason else ''}",
                    "category": "maintenance",
                })
                continue
            if _is_mute_active(d):
                continue
            status, severity, advice, _need = fiber_eval(rx, tx, _th_for_ont(d))
            stale, stale_age = _fiber_stale_info(d)
            if stale and status in ("normal", "warning", "unknown"):
                status, severity = "stale", "warning"
                advice = _fiber_stale_advice(d, stale_age or "?")
            _okey = _fiber_oltkey(d)
            _parent = fiber_parent_down.get(_okey) if _okey else None
            if severity:
                sev_rows.append((_okey, d.get("ont_sn"), status))
                if _parent:
                    _pname = (_parent.get("name") or _okey)
                    alarms.append({
                        "host": f"{d.get('ont_sn')}",
                        "severity": "warning",
                        "message": f"Fiber {status.upper()} disuppress — induk OLT '{_pname}' bermasalah",
                        "category": "maintenance",
                    })
                    continue
                label = d.get("customer") or d.get("ont_sn")
                loc = " / ".join([x for x in (d.get("olt_name"), d.get("odp_name")) if x])
                rx_txt = f"{rx} dBm" if rx is not None else "—"
                tx_txt = f"{tx} dBm" if tx is not None else "—"
                alarms.append({
                    "host": f"{d.get('ont_sn')} ({label})",
                    "severity": severity,
                    "message": f"Fiber {status.upper()}: Rx {rx_txt} Tx {tx_txt} {('[' + loc + ']') if loc else ''} — {advice}",
                    "category": "fiber",
                })
                continue
            if _parent:
                # anggota insiden tanpa alarm sendiri: tak perlu baris tambahan
                continue
            # early warning degradasi: masih normal tapi Rx turun signifikan
            dg = fiber_degrade_memory.get(d["id"]) or {}
            if dg.get("degrading") and rx is not None:
                try:
                    _dth, _dd = _fiber_degrade_settings()
                except Exception:
                    _dth, _dd = FIBER_DEGRADE_DB, FIBER_DEGRADE_DAYS
                label = d.get("customer") or d.get("ont_sn")
                alarms.append({
                    "host": f"{d.get('ont_sn')} ({label})",
                    "severity": "warning",
                    "message": f"Fiber DEGRADASI: Rx turun {dg.get('drop_db')} dB dalam {_dd} hari "
                               f"(kini {rx} dBm, ambang {_dth} dB) — cek bending/konektor/splicing sebelum kritis.",
                    "category": "fiber",
                })
                continue
            # flap: bolak-balik normal<->terganggu (konektor longgar/ODP basah)
            fl = fiber_flap_memory.get(d["id"]) or {}
            if fl.get("flapping"):
                try:
                    _fth, _fh = _fiber_flap_settings()
                except Exception:
                    _fth, _fh = FIBER_FLAP_FLIPS, FIBER_FLAP_HOURS
                label = d.get("customer") or d.get("ont_sn")
                alarms.append({
                    "host": f"{d.get('ont_sn')} ({label})",
                    "severity": "warning",
                    "message": f"Fiber FLAPPING: {fl.get('flips')}x berubah normal↔terganggu dalam {_fh} jam "
                               f"(ambang {_fth}x) — cek konektor longgar, splicing, ODP basah/rusak.",
                    "category": "fiber",
                })
        # entri induk: 1 baris per OLT bermasalah + hitungan anggota
        try:
            _members = {}
            for (_ok, _sn, _st) in sev_rows:
                if _ok:
                    _members.setdefault(_ok, []).append((_sn, _st))
            for _key, _ent in fiber_parent_down.items():
                _mlist = _members.get(_key, [])
                if not _mlist:
                    continue
                _name = (_ent or {}).get("name") or _key
                _crit = sum(1 for _, _st in _mlist if _st in ("critical", "overload"))
                _warn = len(_mlist) - _crit
                if (_ent or {}).get("synthetic"):
                    _msg = (f"Insiden massal OLT '{_name}' — {len(_mlist)} ONT "
                            f"({_crit} kritis/overload, {_warn} warning), "
                            f"alarm individual disuppress")
                else:
                    _ip = (_ent or {}).get("ip") or "?"
                    _msg = (f"Induk OLT '{_name}' DOWN (mgmt {_ip}) — {len(_mlist)} ONT "
                            f"({_crit} kritis/overload, {_warn} warning), "
                            f"alarm individual disuppress")
                alarms.append({
                    "host": f"OLT {_name}",
                    "severity": "disaster",
                    "message": _msg,
                    "category": "fiber",
                })
        except Exception:
            pass
    except Exception as e:
        print(f"[TRIGGERS] fiber gagal: {e}")

    try:
        conn, c = get_db()
        try:
            c.execute("SELECT ip, alias FROM hosts WHERE snmp_community IS NOT NULL"
                      " AND snmp_community != '' AND (snmp_profile IS NULL OR snmp_profile != 'generic')"
                      " ORDER BY id ASC")
            for h in c.fetchall():
                ip = h["ip"]
                label = (h["alias"] or "").strip() or ip
                c2 = conn.execute("SELECT cpu, mem_used, storage_used, temp_c, timestamp FROM device_health"
                                  " WHERE host=? ORDER BY id DESC LIMIT 1", (ip,))
                r = c2.fetchone()
                stale_host = False
                if r:
                    stale_host, stale_age = _mt_stale_info(r["timestamp"])
                    if stale_host:
                        alarms.append({
                            "host": f"{label} ({ip})",
                            "severity": "warning",
                            "message": f"MikroTik STALE: tanpa data SNMP baru sejak {stale_age} — "
                                       f"port/reboot tak terpantau. Cek host/community/UDP 161.",
                            "category": "mikrotik",
                        })
                        continue
                    status, severity, advice = _mt_evaluate(
                        ip, r["cpu"], r["mem_used"], r["storage_used"], r["temp_c"])
                    if severity:
                        alarms.append({
                            "host": f"{label} ({ip})",
                            "severity": severity,
                            "message": f"MikroTik {status.upper()}: {advice}",
                            "category": "mikrotik",
                        })
                # port down + reboot baru (per host, tak tergantung device_health)
                try:
                    c.execute("SELECT if_index, name FROM snmp_interfaces"
                              " WHERE host=? AND monitor=1 AND oper=2", (ip,))
                    for prow in c.fetchall():
                        alarms.append({
                            "host": f"{label} ({ip})",
                            "severity": "high",
                            "message": f"MikroTik PORT DOWN: {prow['name'] or ('if' + str(prow['if_index']))}",
                            "category": "mikrotik",
                        })
                except sqlite3.OperationalError:
                    pass
                try:
                    c.execute("SELECT uptime_s FROM device_health WHERE host=? ORDER BY id DESC LIMIT 1",
                              (ip,))
                    urow = c.fetchone()
                    if urow and urow["uptime_s"] is not None and urow["uptime_s"] < REBOOT_ALARM_WINDOW_S:
                        alarms.append({
                            "host": f"{label} ({ip})",
                            "severity": "warning",
                            "message": f"MikroTik baru reboot {_fmt_duration(int(urow['uptime_s']))} lalu",
                            "category": "mikrotik",
                        })
                except sqlite3.OperationalError:
                    pass
        finally:
            conn.close()
    except Exception as e:
        print(f"[TRIGGERS] mikrotik gagal: {e}")

    try:
        conn, c = get_db()
        try:
            c.execute("SELECT ip, alias, backup_last, backup_ok FROM hosts"
                      " WHERE backup_enable=1 ORDER BY ip ASC")
            now = datetime.now()
            for r in c.fetchall():
                last = (r["backup_last"] or "").strip()
                try:
                    age_h = ((now - datetime.strptime(last, "%Y-%m-%d %H:%M:%S"))
                             .total_seconds() / 3600) if last else None
                except (ValueError, TypeError):
                    age_h = None
                if age_h is not None and r["backup_ok"] and age_h <= 48:
                    continue
                label = (r["alias"] or "").strip() or r["ip"]
                if age_h is None:
                    reason = "belum pernah"
                elif not r["backup_ok"]:
                    reason = "gagal"
                else:
                    reason = f"basi {int(age_h)} jam"
                alarms.append({
                    "host": f"{label} ({r['ip']})",
                    "severity": "warning",
                    "message": f"Backup konfigurasi {reason} (terakhir {last or '—'}) — "
                               f"cek kredensial SSH / jadwal backup.",
                    "category": "mikrotik",
                })
        finally:
            conn.close()
    except Exception as e:
        print(f"[TRIGGERS] mt-backup gagal: {e}")

    severity_order = {"disaster": 1, "high": 2, "warning": 3}
    alarms.sort(key=lambda x: severity_order.get(x["severity"], 4))


    raw_sev = (request.args.get("severity") or "").lower()
    if raw_sev:
        wanted = {s.strip() for s in raw_sev.split(",") if s.strip()} & set(severity_order)
        if wanted:
            alarms = [a for a in alarms if a["severity"] in wanted]

    return jsonify(alarms)


def _like_escape(s):
    return str(s).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@app.route("/logs")
@login_required
def logs_page():
    return render_template("logs.html")

@app.route("/api/system_logs")
@api_login_required
def get_system_logs():
    try:
        page = int(request.args.get("page", 1))
    except (ValueError, TypeError):
        page = 1
    try:
        limit = int(request.args.get("limit", 50))
    except (ValueError, TypeError):
        limit = 50
    page = max(page, 1)
    limit = min(max(limit, 1), 200)
    offset = (page - 1) * limit
    host_filter = (request.args.get("host") or "").strip()
    type_filter = (request.args.get("type") or "").strip()
    q = (request.args.get("q") or "").strip()

    conn, c = get_db()

    query = "SELECT * FROM system_logs WHERE 1=1"
    params = []
    if host_filter:
        query += " AND host=?"
        params.append(host_filter)
    if type_filter:
        query += " AND event_type=?"
        params.append(type_filter)
    if q:
        query += " AND (message LIKE ? ESCAPE '\\' OR host LIKE ? ESCAPE '\\' OR event_type LIKE ? ESCAPE '\\')"
        like = f"%{_like_escape(q)}%"
        params.extend([like, like, like])

    c.execute(f"SELECT COUNT(*) as total FROM ({query})", params)
    total_logs = c.fetchone()["total"]

    query += " ORDER BY id DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    c.execute(query, params)
    rows = c.fetchall()

    logs_data = []
    for r in rows:
        logs_data.append({
            "id": r["id"],
            "timestamp": r["timestamp"],
            "event_type": r["event_type"],
            "host": r["host"],
            "message": r["message"]
        })
    conn.close()

    return jsonify({
        "logs": logs_data,
        "total": total_logs,
        "page": page,
        "limit": limit
    })


@app.route("/api/system_logs/export")
@api_login_required
def export_system_logs():
    host_filter = (request.args.get("host") or "").strip()
    type_filter = (request.args.get("type") or "").strip()
    q = (request.args.get("q") or "").strip()
    conn, c = get_db()
    query = "SELECT timestamp, host, event_type, message FROM system_logs WHERE 1=1"
    params = []
    if host_filter:
        query += " AND host=?"
        params.append(host_filter)
    if type_filter:
        query += " AND event_type=?"
        params.append(type_filter)
    if q:
        query += " AND (message LIKE ? ESCAPE '\\' OR host LIKE ? ESCAPE '\\' OR event_type LIKE ? ESCAPE '\\')"
        like = f"%{_like_escape(q)}%"
        params.extend([like, like, like])
    query += " ORDER BY id DESC LIMIT 5000"
    c.execute(query, params)
    rows = c.fetchall()
    conn.close()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["timestamp", "host", "event_type", "message"])
    for r in rows:
        w.writerow([_csv_safe(r["timestamp"]), _csv_safe(r["host"]),
                    _csv_safe(r["event_type"]), _csv_safe(r["message"])])
    try:
        audit(current_user.username, "logs.export", f"host={host_filter or 'all'} type={type_filter or 'all'} q={q or '-'} rows={len(rows)}")
    except Exception:
        pass
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=nms_system_logs.csv"})


@app.route("/api/export")
@api_login_required
def export_csv():
    targets = get_target_hosts()
    host  = request.args.get("host", targets[0] if targets else "unknown")
    try:
        hours = int(request.args.get("hours", 24))
    except (ValueError, TypeError):
        return jsonify({"error": "hours harus angka 1-168"}), 400
    hours = max(1, min(hours, 168))
    conn, c = get_db()
    c.execute(
        "SELECT timestamp, latency, packet_loss FROM ping_logs "
        "WHERE host=? AND timestamp > datetime('now','localtime',?) ORDER BY id",
        (host, f"-{hours} hours"),
    )
    rows = c.fetchall()
    conn.close()

    out    = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["timestamp", "host", "latency_ms", "packet_loss_pct"])
    for r in rows:
        lat = r["latency"] if r["latency"] != -1 else "DOWN"
        writer.writerow([_csv_safe(r["timestamp"]), _csv_safe(host), lat, r["packet_loss"]])

    safe_host = re.sub(r"[^a-zA-Z0-9.\-]", "_", host)[:100].replace(".", "_") or "unknown"
    fname = f"nms_{safe_host}_{hours}h.csv"
    return Response(
        out.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={fname}"}
    )


REPORT_DAYS_CHOICES = (1, 3, 7)

def _report_days():
    try:
        d = int(request.args.get("days", 7))
    except (ValueError, TypeError):
        d = 7
    if d not in REPORT_DAYS_CHOICES:
        d = 7
    return d

def _fmt_duration(s):
    if s is None:
        return "Ongoing"
    try:
        s = int(s)
    except (ValueError, TypeError):
        return "—"
    m, sec = divmod(s, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {sec}s"
    return f"{sec}s"

@app.route("/reports")
@login_required
def reports_page():
    return render_template("reports.html")


@app.route("/api/reports/summary")
@api_login_required
def api_reports_summary():
    days = _report_days()
    conn, c = get_db()
    c.execute("SELECT ip, alias, category FROM hosts ORDER BY id ASC")
    hosts = [{"ip": r["ip"], "alias": (r["alias"] or "").strip() or r["ip"],
              "category": (r["category"] or "").strip() or "Uncategorized"} for r in c.fetchall()]
    host_rows = []
    outages = []
    total_downtime = 0
    total_outages = 0
    total_maint_downtime = 0
    total_maint_outages = 0
    sum_uptime = 0.0
    counted_uptime = 0
    for h in hosts:
        ip = h["ip"]
        c.execute("""
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN latency != -1 THEN 1 ELSE 0 END) AS up_count,
                   AVG(CASE WHEN latency != -1 THEN latency END) AS avg_ms,
                   MIN(CASE WHEN latency != -1 THEN latency END) AS min_ms,
                   MAX(CASE WHEN latency != -1 THEN latency END) AS max_ms,
                   AVG(CASE WHEN latency != -1 THEN packet_loss END) AS avg_loss
            FROM ping_logs
            WHERE host=? AND timestamp > datetime('now','localtime',?)
        """, (ip, f"-{days} days"))
        r = c.fetchone()
        total = r["total"] or 0
        up_count = r["up_count"] or 0

        try:
            c.execute("""
                SELECT id, started_at, resolved_at, duration_s, is_maintenance FROM down_events
                WHERE host=? AND started_at > datetime('now','localtime',?)
                ORDER BY id DESC
            """, (ip, f"-{days} days"))
        except sqlite3.OperationalError:
            c.execute("""
                SELECT id, started_at, resolved_at, duration_s FROM down_events
                WHERE host=? AND started_at > datetime('now','localtime',?)
                ORDER BY id DESC
            """, (ip, f"-{days} days"))
        evs = c.fetchall()
        def _is_maint(e):
            try:
                return bool(e["is_maintenance"])
            except (KeyError, IndexError, TypeError):
                return False
        real_evs = [e for e in evs if not _is_maint(e)]
        maint_evs = [e for e in evs if _is_maint(e)]
        down_count = len(real_evs)
        maint_count = len(maint_evs)
        total_outages += down_count
        total_maint_outages += maint_count
        durs = [e["duration_s"] for e in real_evs if e["duration_s"] is not None]
        mttr = round(sum(durs) / len(durs)) if durs else None
        h_downtime = sum(durs)
        total_downtime += h_downtime
        maint_durs = [e["duration_s"] for e in maint_evs if e["duration_s"] is not None]
        h_maint_downtime = sum(maint_durs)
        total_maint_downtime += h_maint_downtime
        if total == 0:
            host_rows.append({**h, "uptime_pct": None, "total_checks": 0,
                              "up_count": 0, "down_count": down_count,
                              "maintenance_count": maint_count,
                              "avg_ms": None, "min_ms": None, "max_ms": None,
                              "avg_loss": None, "mttr_s": mttr,
                              "mttr_str": _fmt_duration(mttr) if mttr is not None else "—",
                              "downtime_s": h_downtime,
                              "maintenance_downtime_s": h_maint_downtime})
        else:
            uptime = round(up_count / total * 100, 1)
            sum_uptime += uptime
            counted_uptime += 1
            host_rows.append({**h, "uptime_pct": uptime, "total_checks": total,
                              "up_count": up_count, "down_count": down_count,
                              "maintenance_count": maint_count,
                              "avg_ms": round(r["avg_ms"], 2) if r["avg_ms"] is not None else None,
                              "min_ms": round(r["min_ms"], 2) if r["min_ms"] is not None else None,
                              "max_ms": round(r["max_ms"], 2) if r["max_ms"] is not None else None,
                              "avg_loss": round(r["avg_loss"], 1) if r["avg_loss"] is not None else None,
                              "mttr_s": mttr,
                              "mttr_str": _fmt_duration(mttr) if mttr is not None else "—",
                              "downtime_s": h_downtime,
                              "maintenance_downtime_s": h_maint_downtime})
        for e in evs[:200]:
            outages.append({"id": e["id"], "host": ip, "alias": h["alias"],
                            "started_at": e["started_at"],
                            "resolved_at": e["resolved_at"] or "Ongoing",
                            "duration_s": e["duration_s"],
                            "duration_str": _fmt_duration(e["duration_s"]),
                            "status": "resolved" if e["resolved_at"] else "ongoing",
                            "is_maintenance": _is_maint(e)})
    conn.close()
    outages.sort(key=lambda x: x["started_at"], reverse=True)
    outages = outages[:200]
    return jsonify({
        "period_days": days,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "note": "ping_logs retensi 7 hari; outage maintenance tidak dihitung ke SLA",
        "hosts": host_rows,
        "outages": outages,
        "totals": {
            "hosts": len(hosts),
            "avg_uptime": round(sum_uptime / counted_uptime, 1) if counted_uptime else None,
            "total_outages": total_outages,
            "total_downtime_s": total_downtime,
            "total_downtime_str": _fmt_duration(total_downtime),
            "maintenance_outages": total_maint_outages,
            "maintenance_downtime_s": total_maint_downtime,
            "maintenance_downtime_str": _fmt_duration(total_maint_downtime),
        },
    })


@app.route("/api/reports/export")
@api_login_required
def api_reports_export():
    days = _report_days()
    table = (request.args.get("table") or "summary").lower()

    conn, c = get_db()
    c.execute("SELECT ip, alias, category FROM hosts ORDER BY id ASC")
    hosts = [{"ip": r["ip"], "alias": (r["alias"] or "").strip() or r["ip"],
              "category": (r["category"] or "").strip() or "Uncategorized"} for r in c.fetchall()]
    buf = io.StringIO()
    w = csv.writer(buf)
    if table == "outages":
        w.writerow(["id", "host", "alias", "started_at", "resolved_at", "duration_s", "duration", "status"])
        c.execute("""
            SELECT id, host, started_at, resolved_at, duration_s FROM down_events
            WHERE started_at > datetime('now','localtime',?) ORDER BY id DESC LIMIT 1000
        """, (f"-{days} days",))
        amap = {h["ip"]: h["alias"] for h in hosts}
        for r in c.fetchall():
            w.writerow([r["id"], _csv_safe(r["host"]), _csv_safe(amap.get(r["host"], r["host"])),
                        _csv_safe(r["started_at"]), _csv_safe(r["resolved_at"] or "Ongoing"),
                        r["duration_s"] if r["duration_s"] is not None else "",
                        _fmt_duration(r["duration_s"]), "resolved" if r["resolved_at"] else "ongoing"])
        fname = f"nms_outages_{days}d.csv"
    else:
        w.writerow(["ip", "alias", "category", "uptime_pct", "checks", "up", "down_events",
                    "avg_ms", "min_ms", "max_ms", "avg_loss_pct", "mttr_s", "downtime_s"])
        for h in hosts:
            ip = h["ip"]
            c.execute("""
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN latency != -1 THEN 1 ELSE 0 END) AS up_count,
                       AVG(CASE WHEN latency != -1 THEN latency END) AS avg_ms,
                       MIN(CASE WHEN latency != -1 THEN latency END) AS min_ms,
                       MAX(CASE WHEN latency != -1 THEN latency END) AS max_ms,
                       AVG(CASE WHEN latency != -1 THEN packet_loss END) AS avg_loss
                FROM ping_logs WHERE host=? AND timestamp > datetime('now','localtime',?)
            """, (ip, f"-{days} days"))
            r = c.fetchone()
            total = r["total"] or 0
            up_count = r["up_count"] or 0
            c.execute("SELECT COUNT(*) AS cnt, AVG(duration_s) AS mttr, SUM(duration_s) AS dt FROM down_events WHERE host=? AND started_at > datetime('now','localtime',?)",
                      (ip, f"-{days} days"))
            e = c.fetchone()
            w.writerow([_csv_safe(ip), _csv_safe(h["alias"]), _csv_safe(h["category"]),
                        round(up_count / total * 100, 1) if total else "",
                        total, up_count, e["cnt"] or 0,
                        round(r["avg_ms"], 2) if r["avg_ms"] is not None else "",
                        round(r["min_ms"], 2) if r["min_ms"] is not None else "",
                        round(r["max_ms"], 2) if r["max_ms"] is not None else "",
                        round(r["avg_loss"], 1) if r["avg_loss"] is not None else "",
                        round(e["mttr"]) if e["mttr"] is not None else "",
                        int(e["dt"] or 0)])
        fname = f"nms_summary_{days}d.csv"
    conn.close()
    try:
        audit(current_user.username, "reports.export", f"{table} {days}d")
    except Exception:
        pass
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={fname}"})


@app.route("/api/reports/send", methods=["POST"])
@api_login_required
def api_reports_send():
    data = request.get_json(silent=True) or {}
    try:
        days = int(data.get("days", request.args.get("days", 7)))
    except (ValueError, TypeError):
        days = 7
    if days not in REPORT_DAYS_CHOICES:
        days = 7
    conn, c = get_db()
    c.execute("SELECT ip FROM hosts ORDER BY id ASC")
    hosts = [r["ip"] for r in c.fetchall()]
    lines = []
    worst = []
    for ip in hosts:
        c.execute("""
            SELECT COUNT(*) AS total, SUM(CASE WHEN latency != -1 THEN 1 ELSE 0 END) AS up_count,
                   AVG(CASE WHEN latency != -1 THEN latency END) AS avg_ms
            FROM ping_logs WHERE host=? AND timestamp > datetime('now','localtime',?)
        """, (ip, f"-{days} days"))
        r = c.fetchone()
        total = r["total"] or 0
        up_count = r["up_count"] or 0
        uptime = round(up_count / total * 100, 1) if total else None
        c.execute("SELECT COUNT(*) AS cnt FROM down_events WHERE host=? AND started_at > datetime('now','localtime',?)",
                  (ip, f"-{days} days"))
        cnt = c.fetchone()["cnt"] or 0
        if uptime is None:
            icon, txt = "⚪", "no data"
        elif uptime >= 99:
            icon, txt = "✅", f"{uptime}%"
        elif uptime >= 95:
            icon, txt = "⚠️", f"{uptime}%"
        else:
            icon, txt = "❌", f"{uptime}%"
        lines.append(f"{icon} `{ip}` — *{txt}* · DOWN {cnt}x")
        worst.append((uptime if uptime is not None else 101, cnt, ip))
    conn.close()
    worst.sort(key=lambda x: (x[0], -x[1]))
    top = ", ".join([f"`{ip}`" for _, _, ip in worst[:3]]) if worst else "-"
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    body = "\n".join(lines) if lines else "Belum ada host."
    send_telegram_alert(
        f"📊 *Laporan NMS ({days} Hari)*\nWaktu: {now}\n\n{body}\n\nTop perhatian: {top}"
    )
    try:
        audit(current_user.username, "reports.send", f"{days}d to telegram")
    except Exception:
        pass
    return jsonify({"status": "success", "hosts": len(hosts)})


def _public_node_name(alias, ip, hid):
    a = (alias or "").strip()
    if a and a != (ip or ""):
        return a[:80]
    try:
        return f"node-{int(hid)}"
    except (ValueError, TypeError):
        return "node-?"


@app.route("/status")
def status_page():
    return render_template("status.html")


@app.route("/api/public/status")
def api_public_status():
    maint_map = get_active_maintenance_map()
    nodes = []
    up_n = down_n = pend_n = maint_n = 0
    sum_uptime = 0.0
    counted = 0
    try:
        conn, c = get_db()
        try:
            c.execute("SELECT id, ip, alias FROM hosts ORDER BY id ASC")
            hosts = [dict(r) for r in c.fetchall()]
            for h in hosts:
                ip = h["ip"]
                name = _public_node_name(h.get("alias"), ip, h["id"])
                c.execute(
                    "SELECT latency FROM ping_logs WHERE host=? ORDER BY id DESC LIMIT 1",
                    (ip,),
                )
                latest = c.fetchone()
                c.execute("""
                    SELECT COUNT(*) AS total,
                           SUM(CASE WHEN latency != -1 THEN 1 ELSE 0 END) AS up_count,
                           AVG(CASE WHEN latency != -1 THEN latency END) AS avg_ms
                    FROM ping_logs
                    WHERE host=? AND timestamp > datetime('now','localtime','-24 hours')
                """, (ip,))
                r = c.fetchone()
                total = r["total"] or 0
                up_count = r["up_count"] or 0
                uptime = round(up_count / total * 100, 1) if total else None
                if uptime is not None:
                    sum_uptime += uptime
                    counted += 1
                has_latest = latest is not None and latest["latency"] is not None
                if total == 0 and not has_latest:
                    st = "pending"
                    pend_n += 1
                elif ip in maint_map:
                    st = "maintenance"
                    maint_n += 1
                elif status_memory.get(ip, False) or (has_latest and latest["latency"] == -1):
                    st = "down"
                    down_n += 1
                else:
                    st = "up"
                    up_n += 1
                entry = {
                    "name": name,
                    "status": st,
                    "uptime_24h": uptime,
                    "avg_ms": round(r["avg_ms"], 1) if r["avg_ms"] is not None else None,
                }
                if st == "maintenance":
                    entry["note"] = ((maint_map[ip].get("reason") or "").strip()[:100]
                                     or "Scheduled maintenance")
                nodes.append(entry)

            c.execute("SELECT id, name, type, status FROM services ORDER BY id ASC")
            services = []
            for s in c.fetchall():
                d = dict(s)
                c.execute("""
                    SELECT COUNT(*) AS total,
                           SUM(CASE WHEN status='ONLINE' THEN 1 ELSE 0 END) AS up_count
                    FROM service_history
                    WHERE service_id=? AND timestamp > datetime('now','localtime','-24 hours')
                """, (d["id"],))
                sr = c.fetchone()
                stotal = sr["total"] or 0
                sup = sr["up_count"] or 0
                st_raw = (d.get("status") or "PENDING").upper()
                st = st_raw.lower() if st_raw in ("ONLINE", "OFFLINE") else "pending"
                services.append({
                    "name": (d.get("name") or "service")[:80],
                    "type": (d.get("type") or "").lower()[:10],
                    "status": st,
                    "uptime_24h": round(sup / stotal * 100, 1) if stotal else None,
                })
        finally:
            conn.close()
    except Exception as e:
        print(f"[PUBLIC STATUS] gagal: {e}")
        resp = jsonify({"error": "Status tidak tersedia, coba lagi."})
        resp.headers["Cache-Control"] = "no-store"
        return resp, 503
    resp = jsonify({
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "summary": {
            "total": len(nodes),
            "up": up_n,
            "down": down_n,
            "pending": pend_n,
            "maintenance": maint_n,
            "avg_uptime_24h": round(sum_uptime / counted, 1) if counted else None,
            "services": len(services),
            "services_online": sum(1 for s in services if s["status"] == "online"),
        },
        "nodes": nodes,
        "services": services,
    })
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/health")
def health():
    try:
        conn, c = get_db()
        try:
            c.execute("SELECT 1")
            c.fetchone()
        finally:
            conn.close()
        db_ok = True
    except Exception:
        db_ok = False
    uptime_s = int((datetime.now() - app_start_time).total_seconds())
    return jsonify({
        "status": "ok" if db_ok else "degraded",
        "uptime_seconds": uptime_s,
    }), 200 if db_ok else 503


if __name__ == "__main__":
    app.run(debug=False, port=5000, use_reloader=False)
