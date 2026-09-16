from flask import Flask, jsonify, render_template, Response, request, redirect, url_for, flash, session
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from apscheduler.schedulers.background import BackgroundScheduler
import subprocess, re, csv, io
import sqlite3
import os
import shutil
import glob
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import requests
from dotenv import load_dotenv
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
from functools import wraps
from contextlib import contextmanager
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
    """True jika request datang dari proxy lokal (nginx di 127.0.0.1).

    Header X-Real-IP/X-Forwarded-For hanya dipercaya dari proxy lokal.
    Klien yang menembak langsung ke 5000/tcp bisa memalsukan header,
    jadi untuk koneksi non-lokal header diabaikan (pakai remote_addr).
    """
    try:
        return ipaddress.ip_address(request.remote_addr or "").is_loopback
    except ValueError:
        return False


def get_client_ip():
    """IP klien asli di belakang Nginx (lihat nginx-nms.conf)."""
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
    """Verifikasi kredensial admin.

    Urutan: (1) hash di tabel settings (bisa diganti via API tanpa edit .env),
    (2) fallback plaintext .env HANYA jika DB belum punya kredensial
    (instalasi baru). Jika DB tidak bisa dibaca -> fail-closed (tolak),
    agar password lama .env tidak hidup lagi saat DB lock/korup.
    """
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
    """Simpan hash password .env ke DB saat pertama kali (agar bisa diganti via UI)."""
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
    """Versi sesi admin (naik tiap ganti password -> sesi lama hangus)."""
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
    """Naikkan versi sesi (panggil setelah ganti password/username)."""
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
    """Decorator untuk API endpoint: kembalikan JSON 401 jika belum login."""
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
    """Buang entri throttle yang sudah kedaluwarsa + batasi ukuran dict."""
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
    """Commit dengan retry jika database terkunci (multi-thread + scheduler)."""
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
    """Insert system log memakai cursor yang sudah ada (tanpa commit/con baru).

    WAJIB dipakai dari dalam transaksi yang sudah pegang koneksi,
    agar tidak terjadi nested-connection -> 'database is locked'.
    """
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

def cleanup_old_data():
    """Dijadwalkan tiap tengah malam.

    - ping_logs >7 hari dihapus (retensi pendek, dipakai grafik/SLA).
    - agent_metrics >30 hari dihapus (tanpa ini tabel membengkak tanpa batas;
      2 host @60s = ~2880 baris/hari).
    - system_logs >90 hari dihapus (audit tidak perlu selamanya).
    - down_events yang resolved >90 hari dihapus (ongoing dipertahankan).
    """
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
    print(f"[CLEANUP] ping_logs={deleted} agent_metrics={deleted_agent} system_logs={deleted_logs} down_events={deleted_events} baris lama dihapus.")

def backup_database():
    """Backup SQLite database setiap hari ke folder backups/ (pakai API backup, aman WAL).

    TIDAK menahan db_lock selama backup: API sqlite3 backup konsisten walau DB
    sedang ditulis (mengunci per-halaman, bukan seluruh DB), sehingga writer
    ping/agent tidak antre lama tiap tengah malam.
    """
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
    """Menyimpan event ke dalam database system_logs (thread-safe + retry).

    JANGAN dipanggil dari dalam transaksi yang sudah pegang koneksi
    (mis. di dalam check_network) — pakai _insert_system_log() untuk itu.
    """
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
    """Catat aktivitas user ke system_logs (upgrade: dipakai API host/inventory/settings)."""
    try:
        log_system_event("AUDIT", username, f"{action} {detail}".strip())
    except Exception:
        pass

def send_startup_alert():
    """Kirim notif ke Telegram saat app pertama kali dijalankan."""
    targets = get_target_hosts()
    now   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    hosts = "\n".join([f"  • `{h}`" for h in targets])
    send_telegram_alert(
        f"🟢 *NMS Dashboard AKTIF*\n"
        f"Waktu  : {now}\n"
        f"Memantau {len(targets)} host:\n{hosts}"
    )

def send_heartbeat():
    """Kirim ringkasan status semua host ke Telegram (Laporan Harian)."""
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


_PING_TARGET_RE = re.compile(r"^[a-zA-Z0-9]([a-zA-Z0-9.\-]{0,253}[a-zA-Z0-9])?$")
_PING_RTT_RES = (
    re.compile(r"rtt min/avg/max/mdev = [\d.]+/([\d.]+)/"),
    re.compile(r"round-trip min/avg/max(?:/stddev)? = [\d.]+/([\d.]+)/"),
)

def ping_host(host):
    """Ping 3x. Return (avg_latency_ms, packet_loss_pct). latency=-1 jika total DOWN."""
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
    """Ping satu host dan kembalikan hasilnya. Dijalankan paralel di thread terpisah."""
    latency, packet_loss = ping_host(host)
    return host, latency, packet_loss
def check_agent_heartbeat():
    """Mengecek apakah agent berhenti mengirim data (lewat 2 menit)."""
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

def _encode_snmp_v1_get(community, oid_list):
    """Buat raw SNMP v1 GET packet sederhana (dengan validasi + length long-form)."""
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
    pdu = encode_tlv(0xa0, req_id + b'\x02\x01\x00\x02\x01\x00' + varbind_list)
    comm_bytes = str(community or "")[:128].encode()


    _ver = 1 if os.environ.get("SNMP_VERSION", "1").strip().lower() in ("2", "2c") else 0
    version = bytes([0x02, 0x01, _ver])
    comm_tlv = encode_tlv(0x04, comm_bytes)
    return encode_tlv(0x30, version + comm_tlv + pdu)




def _ber_read_tlv(data, pos):
    """Baca satu TLV BER pada posisi pos. Return (tag, value_bytes, next_pos) atau None."""
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
    """Ambil nilai varbind dari respons SNMP dengan walk BER rekursif.

    Hanya TLV nilai yang tepat mengikuti TLV OID (0x06) yang dikumpulkan,
    sehingga request-id / error-status tidak ikut terambil (bug scan mentah).
    """
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

def _parse_snmp_int(data, idx):
    """Parse satu integer dari TLV di posisi idx (kompat lama; kini via BER reader)."""
    try:
        t = _ber_read_tlv(bytes(data), idx)
        if t is None:
            return None
        tag, val, _ = t
        if tag not in _SNMP_VALUE_TAGS or not (1 <= len(val) <= 9):
            return None
        return int.from_bytes(val, "big", signed=(tag == 0x02))
    except Exception:
        return None

def get_snmp_bandwidth(ip, community, if_index):
    """Ambil ifInOctets dan ifOutOctets via raw UDP SNMP v1.

    Mendukung INTEGER (0x02), Counter32 (0x41), Gauge32 (0x42),
    TimeTicks (0x43) dan Counter64 (0x46) — perangkat modern pakai 64-bit.
    """
    oid_in  = f'1.3.6.1.2.1.2.2.1.10.{if_index}'
    oid_out = f'1.3.6.1.2.1.2.2.1.16.{if_index}'
    sock = None
    try:
        try:
            if_index = int(if_index)
        except (ValueError, TypeError):
            return None, None
        if if_index < 1:
            return None, None
        pkt = _encode_snmp_v1_get(community, [oid_in, oid_out])
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(2.0)
        sock.sendto(pkt, (ip, 161))
        resp, _ = sock.recvfrom(4096)

        vals = _extract_snmp_values(resp)
        if len(vals) >= 2:
            return vals[-2], vals[-1]
        return None, None
    except Exception as e:
        print(f"[SNMP ERROR] {ip}: {e}")
        return None, None
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass

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
                    if net_in > 10000 or net_out > 10000:
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

                    if host_is_down and not was_down:

                        status_memory[host] = True
                        down_since[host]    = datetime.now()
                        c.execute(
                            "INSERT INTO down_events (host, started_at) VALUES (?, ?)",
                            (host, timestamp)
                        )
                        _insert_system_log(c, "NETWORK_DOWN", host, "Ping timeout/RTO", timestamp)


                        now_dt = datetime.now()
                        last_tg = last_down_telegram.get(host)
                        if last_tg is None or (now_dt - last_tg).total_seconds() >= DOWN_COOLDOWN_S:
                            last_down_telegram[host] = now_dt
                            telegram_queue.append(
                                f"🚨 *ALARM!*\nHost   : `{host}`\nStatus : *DOWN*\nWaktu  : {timestamp}"
                            )
                        else:
                            print(f"[COOLDOWN] Telegram DOWN {host} ditahan (flapping?)")

                    elif not host_is_down and was_down:

                        status_memory[host] = False
                        duration_str = ""
                        duration_s   = None
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
                        if duration_s is not None:
                            m, s       = divmod(duration_s, 60)
                            duration_str = f"\nDurasi DOWN : {m} menit {s} detik"
                        c.execute(
                            "UPDATE down_events SET resolved_at=?, duration_s=? WHERE host=? AND resolved_at IS NULL",
                            (timestamp, duration_s, host)
                        )
                        _insert_system_log(c, "NETWORK_UP", host,
                                           f"Pulih setelah {duration_str.replace(chr(10), '')}", timestamp)
                        telegram_queue.append(
                            f"✅ *PULIH!*\nHost    : `{host}`\nLatency : {latency:.2f} ms\nLoss    : {packet_loss:.0f}%{duration_str}"
                        )


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
    """Guard SSRF untuk service check HTTP (admin-only, tapi tetap dibatasi).

    Ditolak: skema selain http/https, userinfo, host kosong, cloud metadata,
    serta target yang resolve ke loopback/link-local/multicast/reserved/
    unspecified (127.x, ::1, 169.254.x, ...). IP privat (10/8, 192.168/16,
    ...) SENGAJA diizinkan karena NMS memang memonitor perangkat internal.
    Catatan: ada jeda TOCTOU antara resolve dan connect (DNS rebinding);
    untuk lingkungan hostile gunakan allowlist DNS internal.
    """
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


init_db()
_seed_admin_from_env()

def rebuild_alarm_memory():
    """Bangun ulang memory alarm dari DB agar /api/triggers tidak kosong setelah restart.

    Tanpa ini, status_memory & agent_* kosong sampai siklus scheduler berikutnya
    (30-60 detik), sehingga Monitoring terlihat ALL_CLEAR padahal ada host DOWN.
    """
    global status_memory, down_since, agent_status_memory, agent_offline_memory
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
    scheduler.add_job(func=send_heartbeat,         trigger="cron",     hour=8, minute=0, **_job_defaults)
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
    """Halaman detail per host."""
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
    """Data historis per host dengan berbagai time range."""
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
    """Statistik summary per host."""
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


@app.route("/api/hosts", methods=["GET"])
@api_login_required
def api_get_hosts():
    conn, c = get_db()
    c.execute("SELECT id, ip, snmp_community, if_index, alias, category FROM hosts ORDER BY id ASC")
    hosts = []
    for r in c.fetchall():
        hosts.append({"id": r["id"], "ip": r["ip"],
                      "snmp_community": r["snmp_community"] or "",
                      "if_index": r["if_index"] or 1,
                      "alias": r["alias"] or "",
                      "category": r["category"] or "Uncategorized"})
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
        c.execute("INSERT INTO hosts (ip, snmp_community, if_index, alias, category) VALUES (?, ?, ?, ?, ?)", (ip, snmp_community, if_index, alias, category))
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
        for mem in (status_memory, down_since, agent_status_memory, agent_offline_memory, last_down_telegram):
            try:
                if ip in mem:
                    del mem[ip]
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


@app.route("/api/settings", methods=["GET"])
@api_login_required
def api_get_settings():
    return jsonify({
        "cpu_threshold": get_setting("cpu_threshold", 85.0),
        "ram_threshold": get_setting("ram_threshold", 90.0),
        "disk_threshold": get_setting("disk_threshold", 90.0)
    })

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
    """Ganti kredensial admin (tersimpan sebagai hash di DB, bukan .env).

    Body JSON: {current_password, new_username?, new_password}.
    """
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
    """Agent melapor penggunaan CPU dan RAM ke server ini."""
    global agent_status_memory, agent_offline_memory


    if AGENT_API_KEY:
        import hmac
        provided = request.headers.get("X-API-Key") or ""
        if not provided or not hmac.compare_digest(str(provided), str(AGENT_API_KEY)):
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
    """Samakan deret multi-host ke satu sumbu-X (union timestamp, terurut).

    per_host: {host: [(timestamp, value), ...]}. Return (labels, datasets)
    dengan datasets[host] sejajar labels (None untuk titik yang hilang).
    Tanpa ini label diambil dari host pertama yang ada datanya sehingga
    dataset host lain (panjang/rate beda) tampil pada waktu yang salah.
    """
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
    """Mengembalikan metrik agen terbaru untuk tiap host (max usia data 5 menit)."""
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
    """Mengembalikan data historis agent metrics (CPU/RAM/Disk/Bandwidth) per host."""
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
    """10 data point terbaru per host (sumbu-X disamakan antar host)."""
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
    """Data historis. ?hours=1|6|24 (default 1). Sumbu-X disamakan antar host."""
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
    """Statistik 24 jam per host: uptime%, avg/min/max ms, avg packet loss."""
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
    """50 DOWN events terbaru."""
    conn, c = get_db()
    c.execute("""
        SELECT id, host, started_at, resolved_at, duration_s
        FROM down_events ORDER BY id DESC LIMIT 50
    """)
    events = []
    for r in c.fetchall():
        m, s = divmod(r["duration_s"] or 0, 60)
        events.append({
            "id"         : r["id"],
            "host"       : r["host"],
            "started_at" : r["started_at"],
            "resolved_at": r["resolved_at"] or "Ongoing",
            "duration"   : f"{m}m {s}s" if r["duration_s"] else ("Ongoing" if not r["resolved_at"] else "—"),
            "status"     : "resolved" if r["resolved_at"] else "ongoing",
        })
    conn.close()
    return jsonify(events)


@app.route("/api/events/<int:event_id>", methods=["DELETE"])
@api_login_required
def delete_event(event_id):
    """Hapus satu event berdasarkan ID."""
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
    """Hapus semua log events."""
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
            port = int(port_raw)
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
    conn.commit()
    conn.close()
    try:
        audit(current_user.username, "service.delete", f"{row['name']} {row['ip']} id={svc_id}")
    except Exception:
        pass
    return jsonify({"status": "success"})



@app.route("/inventory")
@login_required
def inventory_page():
    return render_template("inventory.html")


@app.route("/api/inventory", methods=["GET"])
@api_login_required
def api_inventory_list():
    """Daftar inventory + status live dari ping terakhir + penanda termonitor."""
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
    """Netralkan formula injection CSV: sel string yang diawali = + - @ | %
    (atau berisi CR/LF/tab) diberi prefix `'`, dan CR/LF dibuang.

    Angka (int/float) dikembalikan apa adanya — Excel memperlakukannya
    sebagai angka, bukan formula.
    """
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
    """Mengambil semua alarm yang aktif saat ini (hanya untuk host terdaftar)."""
    global status_memory, agent_status_memory, agent_offline_memory


    valid_hosts = set(get_target_hosts())

    alarms = []


    for host, is_down in list(status_memory.items()):
        if host not in valid_hosts:
            continue
        if is_down:
            alarms.append({
                "host": host,
                "severity": "disaster",
                "message": "Host is DOWN (Unreachable)",
                "category": "availability"
            })


    for host, is_offline in list(agent_offline_memory.items()):
        if host not in valid_hosts:
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

    severity_order = {"disaster": 1, "high": 2, "warning": 3}
    alarms.sort(key=lambda x: severity_order.get(x["severity"], 4))


    raw_sev = (request.args.get("severity") or "").lower()
    if raw_sev:
        wanted = {s.strip() for s in raw_sev.split(",") if s.strip()} & set(severity_order)
        if wanted:
            alarms = [a for a in alarms if a["severity"] in wanted]

    return jsonify(alarms)


def _like_escape(s):
    """Escape wildcard LIKE (persen, underscore, backslash) agar pencarian teks literal.

    Tanpa ini, mencari "100%" mencocokkan semua baris berawalan "100"
    dan "_" menjadi wildcard satu karakter.
    """
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
    """Export CSV log dengan filter yang sama. ?host=&type=&q= (max 5000 baris)."""
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
    """Export CSV. ?host=8.8.8.8&hours=24"""
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
    """Ringkasan SLA + outage per host. ?days=1|3|7 (default 7)."""
    days = _report_days()
    conn, c = get_db()
    c.execute("SELECT ip, alias, category FROM hosts ORDER BY id ASC")
    hosts = [{"ip": r["ip"], "alias": (r["alias"] or "").strip() or r["ip"],
              "category": (r["category"] or "").strip() or "Uncategorized"} for r in c.fetchall()]
    host_rows = []
    outages = []
    total_downtime = 0
    total_outages = 0
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

        c.execute("""
            SELECT id, started_at, resolved_at, duration_s FROM down_events
            WHERE host=? AND started_at > datetime('now','localtime',?)
            ORDER BY id DESC
        """, (ip, f"-{days} days"))
        evs = c.fetchall()
        down_count = len(evs)
        total_outages += down_count
        durs = [e["duration_s"] for e in evs if e["duration_s"] is not None]
        mttr = round(sum(durs) / len(durs)) if durs else None
        h_downtime = sum(durs)
        total_downtime += h_downtime
        if total == 0:
            host_rows.append({**h, "uptime_pct": None, "total_checks": 0,
                              "up_count": 0, "down_count": down_count,
                              "avg_ms": None, "min_ms": None, "max_ms": None,
                              "avg_loss": None, "mttr_s": mttr,
                              "mttr_str": _fmt_duration(mttr) if mttr is not None else "—",
                              "downtime_s": h_downtime})
        else:
            uptime = round(up_count / total * 100, 1)
            sum_uptime += uptime
            counted_uptime += 1
            host_rows.append({**h, "uptime_pct": uptime, "total_checks": total,
                              "up_count": up_count, "down_count": down_count,
                              "avg_ms": round(r["avg_ms"], 2) if r["avg_ms"] is not None else None,
                              "min_ms": round(r["min_ms"], 2) if r["min_ms"] is not None else None,
                              "max_ms": round(r["max_ms"], 2) if r["max_ms"] is not None else None,
                              "avg_loss": round(r["avg_loss"], 1) if r["avg_loss"] is not None else None,
                              "mttr_s": mttr,
                              "mttr_str": _fmt_duration(mttr) if mttr is not None else "—",
                              "downtime_s": h_downtime})
        for e in evs[:200]:
            outages.append({"id": e["id"], "host": ip, "alias": h["alias"],
                            "started_at": e["started_at"],
                            "resolved_at": e["resolved_at"] or "Ongoing",
                            "duration_s": e["duration_s"],
                            "duration_str": _fmt_duration(e["duration_s"]),
                            "status": "resolved" if e["resolved_at"] else "ongoing"})
    conn.close()
    outages.sort(key=lambda x: x["started_at"], reverse=True)
    outages = outages[:200]
    return jsonify({
        "period_days": days,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "note": "ping_logs retensi 7 hari",
        "hosts": host_rows,
        "outages": outages,
        "totals": {
            "hosts": len(hosts),
            "avg_uptime": round(sum_uptime / counted_uptime, 1) if counted_uptime else None,
            "total_outages": total_outages,
            "total_downtime_s": total_downtime,
            "total_downtime_str": _fmt_duration(total_downtime),
        },
    })


@app.route("/api/reports/export")
@api_login_required
def api_reports_export():
    """Export CSV. ?days=7&table=summary|outages"""
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
    """Kirim ringkasan laporan ke Telegram. Body JSON {days:7}."""
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


@app.route("/health")
def health():
    """Liveness probe untuk LB/Docker. Sengaja TANPA daftar host & jumlah baris
    agar tidak membocorkan IP internal ke publik."""
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
