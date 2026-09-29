import csv
import io
import ipaddress
import logging
import os
import re
import socket
import sqlite3
import threading
import time
from functools import wraps
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from apscheduler.schedulers.background import BackgroundScheduler
from flask import (
    Flask,
    Response,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
    g,
)

try:
    from flask_limiter import Limiter
    from flask_limiter.util import get_remote_address

    LIMITER_AVAILABLE = True
except ImportError:
    LIMITER_AVAILABLE = False
from flask_login import (
    LoginManager,
    UserMixin,
    current_user,
    login_required,
    login_user,
    logout_user,
)
from nms.config import (
    AGENT_API_KEY,
    BASE_DIR,
    COOKIE_SECURE,
    DASHBOARD_PASSWORD,
    DASHBOARD_USERNAME,
    HYSTERESIS,
    LOGIN_FAIL_TTL_S,
    MAX_HOSTS,
    SECRET_KEY,
)

from nms.db import (
    _commit_with_retry,
    _fmt_maint_time,
    _insert_system_log,
    _parse_maint_time,
    backup_database,
    bulk_import_hosts,
    cleanup_old_data,
    create_user,
    db_lock,
    delete_host_threshold,
    delete_user,
    generate_daily_report,
    get_active_alerts,
    get_alert_history,
    get_active_maintenance_map,
    get_db,
    get_host_threshold,
    get_setting,
    get_target_hosts,
    get_user,
    init_db,
    list_backups,
    list_host_thresholds,
    list_users,
    log_alert,
    log_system_event,
    resolve_alert,
    restore_database,
    set_host_threshold,
    update_user_role,
    update_alert_workflow,
    mark_alerts_escalated,
)
from nms.notify import audit, send_telegram_alert
from nms.validators import validate_backup_filename
from nms.query_helpers import (
    get_all_agent_metrics,
    get_all_host_stats,
    get_service_uptime_map,
)
from nms.crypto import encrypt_secret
from nms.monitor import (
    FIBER_PARENT_MIN,
    _fiber_oltkey,
    check_network,
    down_since,
    fiber_parent_down,
    last_down_telegram,
    status_memory,
)
from nms.exceptions import (
    AuthenticationError,
    DatabaseError,
    safe_db_operation,
    log_exception,
)
from nms.thread_safe import ThreadSafeDict
from nms.monitor import agent_offline_memory, check_agent_heartbeat
from nms.snmp import _valid_oid, discover_subnet
import json
from nms.fiber import (
    FIBER_DEGRADE_DAYS,
    FIBER_DEGRADE_DB,
    FIBER_FLAP_FLIPS,
    FIBER_FLAP_HOURS,
    FIBER_RX_CRIT,
    FIBER_RX_OVERLOAD,
    FIBER_RX_TARGET,
    FIBER_RX_WARN,
    FIBER_STALE_MIN,
    FIBER_TX_MAX,
    FIBER_TX_MIN,
    _th_for_ont,
    fiber_eval,
)
from nms.auth import api_login_required, require_role
from nms.format import _csv_safe, _fmt_duration
from nms.fiber_poll import (
    _fiber_degrade_settings,
    _fiber_flap_settings,
    _fiber_maintenance_info,
    _fiber_stale_advice,
    _fiber_stale_info,
    _is_mute_active,
    fiber_alarm_memory,
    fiber_degrade_memory,
    fiber_flap_memory,
    poll_fiber_monitor,
    poll_fiber_snmp,
    send_fiber_summary,
)
from nms.mikrotik import (
    MT_STALE_MIN,
    REBOOT_ALARM_WINDOW_S,
    _mt_evaluate,
    _mt_stale_info,
)
from nms.mikrotik_poll import (
    MT_DEFAULT_OIDS,
    iface_state,
    mt_alarm_memory,
    mt_iface_flaps,
    mt_iface_oper,
    mt_iface_tg,
    mt_is_mikrotik,
    mt_sysup,
    poll_mikrotik_health,
    poll_mikrotik_ifaces,
    poll_mt_backups,
    poll_snmp_bandwidth,
    auto_discover_interfaces,
    snmp_state,
)
from nms.mikrotik_routes import mikrotik_bp
from nms.fiber_routes import fiber_bp
from nms.blueprints import api_bp

app = Flask(__name__)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

if not SECRET_KEY:
    raise SystemExit(
        "[FATAL] SECRET_KEY belum diset. Buat .env berisi "
        "SECRET_KEY=<64 hex acak> (contoh: python3 -c "
        '"import secrets; print(secrets.token_hex(32))") lalu restart.'
    )
app.secret_key = SECRET_KEY
app.config["TEMPLATES_AUTO_RELOAD"] = True

app.config["REMEMBER_COOKIE_DURATION"] = timedelta(days=1)
app.config["REMEMBER_COOKIE_HTTPONLY"] = True
app.config["REMEMBER_COOKIE_SAMESITE"] = "Strict"
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Strict"

app.config["SESSION_COOKIE_SECURE"] = COOKIE_SECURE or True
app.config["REMEMBER_COOKIE_SECURE"] = COOKIE_SECURE or True
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=8)
app_start_time = datetime.now()

os.makedirs(os.path.join(BASE_DIR, "logs"), exist_ok=True)

if LIMITER_AVAILABLE:
    limiter = Limiter(
        app=app,
        key_func=get_remote_address,
        default_limits=["200 per day", "50 per hour"],
        storage_uri="memory://",
    )
else:

    class NoOpLimiter:
        def limit(self, *args, **kwargs):
            return lambda f: f

    limiter = NoOpLimiter()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(BASE_DIR, "logs/app.log")),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

app.register_blueprint(mikrotik_bp)
app.register_blueprint(fiber_bp)
app.register_blueprint(api_bp)


@app.before_request
def log_request():
    g.start_time = time.time()
    g.ip = get_client_ip()

    if current_user.is_authenticated:
        session.permanent = True
        app.permanent_session_lifetime = timedelta(hours=8)
        session.modified = True

        last_activity = session.get("_last_activity")
        now = time.time()

        if last_activity is None:
            session["_last_activity"] = now
        elif (now - last_activity) > 3600:
            logout_user()
            session.pop("admin_v", None)
            logger.warning(
                f"Session timeout for {current_user.username} (idle > 1 hour)"
            )
            return redirect(url_for("login"))
        else:
            session["_last_activity"] = now


@app.after_request
def log_response(response):
    if hasattr(g, "start_time"):
        elapsed = (time.time() - g.start_time) * 1000
        client_ip = getattr(g, "ip", "unknown")
        logger.info(
            f"{request.method} {request.path} - Status: {response.status_code} - "
            f"Time: {elapsed:.2f}ms - IP: {client_ip}"
        )
    return response


@app.route("/health", methods=["GET"])
@limiter.limit("60/minute")
def health_check():
    try:
        conn, c = get_db()
        c.execute("SELECT 1")
        c.fetchone()
        conn.close()
        return (
            jsonify(
                {
                    "status": "healthy",
                    "timestamp": datetime.now().isoformat(),
                    "uptime_seconds": (datetime.now() - app_start_time).total_seconds(),
                }
            ),
            200,
        )
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        return (
            jsonify(
                {
                    "status": "unhealthy",
                    "error": str(e),
                    "timestamp": datetime.now().isoformat(),
                }
            ),
            503,
        )


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
login_manager.login_view = "login"
login_manager.login_message = "Silakan login terlebih dahulu untuk mengakses dashboard."
login_manager.login_message_category = "warning"


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
    except (sqlite3.OperationalError, sqlite3.DatabaseError) as e:
        logger.error(f"[AUTH] database error during verification: {e}")
        return False
    except Exception as e:
        logger.error(f"[AUTH] unexpected error during verification: {e}")
        return False

    if db_user and db_hash:
        if username != db_user:
            return False
        try:
            from werkzeug.security import check_password_hash

            return check_password_hash(db_hash, password)
        except (ValueError, TypeError) as e:
            logger.warning(f"[AUTH] password hash check failed: {e}")
            return False
        except Exception as e:
            logger.error(f"[AUTH] unexpected error checking password: {e}")
            return False
    if db_user or db_hash:

        return False

    import hmac

    return hmac.compare_digest(username, DASHBOARD_USERNAME) and hmac.compare_digest(
        password, DASHBOARD_PASSWORD
    )


def _seed_admin_from_env():
    try:
        from werkzeug.security import generate_password_hash

        conn, c = get_db()
        try:
            c.execute("SELECT value FROM settings WHERE key='admin_pass_hash'")
            if c.fetchone():
                return
            c.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES ('admin_user', ?)",
                (DASHBOARD_USERNAME,),
            )
            c.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES ('admin_pass_hash', ?)",
                (generate_password_hash(DASHBOARD_PASSWORD),),
            )
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
            c.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES ('admin_session_v', ?)",
                (str(cur + 1),),
            )
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


DB_PATH = os.environ.get("NMS_DB_PATH", os.path.join(BASE_DIR, "network.db"))


agent_status_memory = ThreadSafeDict()


login_failures = ThreadSafeDict()


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


def send_startup_alert():
    targets = get_target_hosts()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    hosts = "\n".join([f"  • `{h}`" for h in targets])
    send_telegram_alert(
        f"🟢 *NMS Dashboard AKTIF*\n"
        f"Waktu  : {now}\n"
        f"Memantau {len(targets)} host:\n{hosts}"
    )


def send_daily_report():
    try:
        report = generate_daily_report()
        summary = report.get("summary", {})

        healthy = summary.get("healthy_hosts", 0)
        total = summary.get("total_hosts", 0)
        down_events = summary.get("down_events", 0)
        alerts = summary.get("total_alerts", 0)

        healthy_pct = round((healthy / total * 100), 1) if total else 0

        message = (
            f"📊 *LAPORAN HARIAN NMS*\n"
            f"Tanggal: {report.get('generated_at')}\n\n"
            f"📈 *Ringkasan (24 Jam)*\n"
            f"  Total Host: {total}\n"
            f"  Sehat: {healthy} ({healthy_pct}%)\n"
            f"  Down Events: {down_events}\n"
            f"  Total Alerts: {alerts}\n"
        )

        send_telegram_alert(message)
        logger.info(f"Daily report sent")
    except Exception as e:
        logger.error(f"Daily report error: {e}")


def send_heartbeat():
    targets = get_target_hosts()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = []

    conn, c = get_db()
    try:
        all_stats = get_all_host_stats(c, time_range_hours=24)
        for host in targets:
            stats = all_stats.get(host, {"total": 0, "up_count": 0, "avg_ms": 0})
            total = stats["total"] or 0
            up_count = stats["up_count"] or 0
            uptime_pct = round((up_count / total * 100) if total else 0, 1)
            avg_ms = round(stats["avg_ms"] or 0, 1)

            status_icon = (
                "✅" if uptime_pct >= 99 else ("⚠️" if uptime_pct >= 95 else "❌")
            )
            lines.append(
                f"{status_icon} `{host}` — Uptime: *{uptime_pct}%* ({avg_ms} ms)"
            )
    finally:
        conn.close()

    body = "\n".join(lines)
    send_telegram_alert(
        f"📊 *Laporan Harian NMS (24 Jam Terakhir)*\n" f"Waktu : {now}\n\n" f"{body}"
    )


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
    if host_part in (
        "169.254.169.254",
        "metadata.google.internal",
        "metadata.google.internal.",
    ):
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
            if (
                ip.is_loopback
                or ip.is_link_local
                or ip.is_multicast
                or ip.is_reserved
                or ip.is_unspecified
            ):
                return False
    except ValueError:
        return False
    return True


def check_single_service(svc):
    svc_id, ip, name, svc_type, port, url = svc
    start_time = time.time()
    status = "OFFLINE"

    if svc_type == "tcp":
        try:
            port_int = int(port)
            if not 1 <= port_int <= 65535:
                raise ValueError("port out of range")
            with socket.create_connection((ip, port_int), timeout=5):
                status = "ONLINE"
        except Exception:
            pass
    elif svc_type == "http":
        try:
            if not _is_url_allowed_for_monitoring(url):
                status = "OFFLINE"
            else:
                import urllib3

                urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

                r = requests.get(
                    url,
                    timeout=5,
                    verify=False,
                    headers={"User-Agent": "NMS-KOPEGTEL/1.0"},
                )
                if r.status_code < 400:
                    status = "ONLINE"
        except Exception:
            pass
    else:

        status = "PENDING"

    latency = (time.time() - start_time) * 1000
    if status == "OFFLINE":
        latency = 0
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
    import ssl as _ssl
    from urllib.parse import urlparse

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
                c.execute(
                    "SELECT id, ip, name, url, ssl_days_left, ssl_last_alert FROM services WHERE type='http' AND url LIKE 'https://%'"
                )
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
        escalated = bool(
            level
            and level != prev_level
            and (not prev_level or order.get(level, 0) > order.get(prev_level, 0))
        )
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
                        _insert_system_log(
                            c,
                            "SSL_EXPIRY" if level != "expired" else "SSL_EXPIRED",
                            s.get("ip") or "-",
                            f"{s.get('name')} {url} sisa {days} hari (exp {exp_str})",
                            timestamp,
                        )
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
            futures = [
                executor.submit(
                    check_single_service,
                    (s["id"], s["ip"], s["name"], s["type"], s["port"], s["url"]),
                )
                for s in services
            ]
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
                    c.execute(
                        "UPDATE services SET status=?, latency=?, last_checked=? WHERE id=?",
                        (status, latency, timestamp, svc_id),
                    )
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


init_db()
_seed_admin_from_env()


def rebuild_alarm_memory():
    try:
        conn, c = get_db()
        try:
            c.execute("SELECT ip FROM hosts ORDER BY id ASC")
            valid = [r["ip"] for r in c.fetchall()]
            valid_set = set(valid)

            try:
                c.execute(
                    "SELECT host, started_at FROM down_events WHERE resolved_at IS NULL"
                )
                for r in c.fetchall():
                    h = r["host"]
                    if h not in valid_set:
                        continue
                    status_memory[h] = True
                    try:
                        down_since[h] = datetime.strptime(
                            r["started_at"], "%Y-%m-%d %H:%M:%S"
                        )
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

            all_metrics = get_all_agent_metrics(c, time_range_minutes=5)
            for h in valid:
                try:
                    if h not in all_metrics:
                        continue
                    last = all_metrics[h]
                    cpu_val = last.get("cpu", 0)
                    ram_val = last.get("ram", 0)
                    disk_val = last.get("disk", 0)
                    agent_status_memory[h] = {
                        "cpu": bool(cpu_val > cpu_thresh),
                        "ram": bool(ram_val > ram_thresh),
                        "disk": bool(disk_val > disk_thresh),
                    }

                    try:
                        last_time = datetime.strptime(
                            last["timestamp"], "%Y-%m-%d %H:%M:%S"
                        )
                        if (now - last_time).total_seconds() > 120:
                            agent_offline_memory[h] = True
                    except Exception:
                        pass
                except Exception as e:
                    print(f"[REBUILD] agent {h} gagal: {e}")
                    continue

            try:
                c.execute(
                    "SELECT id, status FROM fiber_onts WHERE status IN ('normal', 'warning', 'critical', 'overload', 'unknown', 'stale') LIMIT 10000"
                )
                for r in c.fetchall():
                    st = (r["status"] or "").strip().lower()
                    if st in (
                        "normal",
                        "warning",
                        "critical",
                        "overload",
                        "unknown",
                        "stale",
                    ):
                        fiber_alarm_memory[r["id"]] = st
            except Exception as e:
                print(f"[REBUILD] fiber gagal: {e}")
        finally:
            conn.close()
    except Exception as e:
        print(f"[REBUILD] gagal: {e}")


rebuild_alarm_memory()


def run_scheduled_snmp_discovery():
    network = get_setting("discovery_network", "", type_cast=str).strip()
    community = get_setting("discovery_community", "", type_cast=str).strip()
    if not network or not community:
        return {"status": "disabled", "added": [], "removed": []}
    try:
        found = discover_subnet(network, community, timeout=0.8, max_hosts=256)
    except Exception as exc:
        logger.warning(f"Scheduled SNMP discovery gagal: {exc}")
        return {"status": "error", "added": [], "removed": []}
    current = {item["ip"] for item in found}
    previous_raw = get_setting("discovery_last_hosts", "[]", type_cast=str)
    try:
        previous = set(json.loads(previous_raw))
    except (TypeError, ValueError):
        previous = set()
    added, removed = sorted(current - previous), sorted(previous - current)
    if added or removed:
        parts = ["[NMS DISCOVERY] Perubahan perangkat"]
        if added:
            parts.append("Baru: " + ", ".join(added[:20]))
        if removed:
            parts.append("Hilang: " + ", ".join(removed[:20]))
        send_telegram_alert("\n".join(parts))
    conn, c = get_db()
    c.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
        ("discovery_last_hosts", json.dumps(sorted(current))),
    )
    conn.commit()
    conn.close()
    return {"status": "ok", "added": added, "removed": removed}


def escalate_unacknowledged_alerts():
    now = datetime.now()
    candidates = []
    for alert in get_active_alerts():
        if alert.get("acknowledged_at") or alert.get("escalated_at"):
            continue
        try:
            triggered = datetime.strptime(alert["triggered_at"], "%Y-%m-%d %H:%M:%S")
        except (KeyError, TypeError, ValueError):
            continue
        if (now - triggered).total_seconds() < 900:
            continue
        candidates.append(alert)
    if not candidates:
        return 0
    lines = ["[NMS ESCALATION] Alert belum ditangani > 15 menit:"]
    for alert in candidates[:20]:
        lines.append(
            f"- {alert['severity'].upper()} {alert['host']}: {alert.get('message') or '-'}"
        )
    send_telegram_alert("\n".join(lines))
    mark_alerts_escalated([alert["id"] for alert in candidates])
    return len(candidates)


SCHEDULER_ENABLED = os.environ.get("NMS_DISABLE_SCHEDULER", "0") != "1"
try:
    LOCAL_TZ = ZoneInfo("Asia/Jakarta")
except Exception:
    LOCAL_TZ = None

scheduler = BackgroundScheduler(timezone=LOCAL_TZ)
if SCHEDULER_ENABLED:
    _job_defaults = {"max_instances": 1, "coalesce": True, "misfire_grace_time": 120}
    scheduler.add_job(
        func=check_network, trigger="interval", seconds=30, **_job_defaults
    )
    scheduler.add_job(
        func=escalate_unacknowledged_alerts,
        trigger="interval",
        minutes=5,
        **_job_defaults,
    )
    scheduler.add_job(
        func=run_scheduled_snmp_discovery, trigger="interval", hours=6, **_job_defaults
    )
    scheduler.add_job(
        func=check_services, trigger="interval", seconds=30, **_job_defaults
    )
    scheduler.add_job(
        func=check_agent_heartbeat, trigger="interval", seconds=60, **_job_defaults
    )
    scheduler.add_job(
        func=poll_snmp_bandwidth, trigger="interval", seconds=30, **_job_defaults
    )
    scheduler.add_job(
        func=auto_discover_interfaces, trigger="interval", hours=1, **_job_defaults
    )
    scheduler.add_job(
        func=poll_mikrotik_health, trigger="interval", seconds=60, **_job_defaults
    )
    scheduler.add_job(
        func=poll_mikrotik_ifaces, trigger="interval", seconds=60, **_job_defaults
    )
    scheduler.add_job(
        func=poll_fiber_monitor, trigger="interval", seconds=120, **_job_defaults
    )
    scheduler.add_job(
        func=poll_fiber_snmp, trigger="interval", seconds=300, **_job_defaults
    )
    scheduler.add_job(
        func=check_ssl_expiry, trigger="interval", hours=6, **_job_defaults
    )
    scheduler.add_job(
        func=send_heartbeat, trigger="cron", hour=8, minute=0, **_job_defaults
    )
    scheduler.add_job(
        func=send_fiber_summary, trigger="cron", hour=8, minute=5, **_job_defaults
    )
    scheduler.add_job(
        func=poll_mt_backups, trigger="cron", hour=2, minute=0, **_job_defaults
    )
    scheduler.add_job(
        func=cleanup_old_data, trigger="cron", hour=0, minute=0, **_job_defaults
    )
    scheduler.add_job(
        func=backup_database, trigger="cron", hour=0, minute=5, **_job_defaults
    )
    scheduler.add_job(
        func=send_daily_report, trigger="cron", hour=7, minute=0, **_job_defaults
    )
    try:
        scheduler.start()
    except Exception as e:
        print(f"[WARN] scheduler gagal start: {e}")

    def _startup_backup_catchup():
        try:
            date_str = datetime.now().strftime("%Y-%m-%d")
            backup_path = os.path.join(
                BASE_DIR, "backups", f"network_backup_{date_str}.db"
            )
            if os.path.exists(backup_path):
                return
            print(
                f"[BACKUP] backup hari ini belum ada ({backup_path}), "
                "catch-up saat startup.",
                flush=True,
            )
            backup_database()
        except Exception as e:
            print(f"[BACKUP] catch-up gagal: {e}", flush=True)

    try:
        _catchup_timer = threading.Timer(30.0, _startup_backup_catchup)
        _catchup_timer.daemon = True
        _catchup_timer.start()
    except Exception as e:
        print(f"[WARN] jadwal catch-up backup gagal: {e}")


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

            if (
                not next_page
                or not next_page.startswith("/")
                or next_page.startswith("//")
                or "\\" in next_page
            ):
                next_page = None
            return redirect(next_page or url_for("index"))
        else:
            fails += 1
            try:
                log_system_event(
                    "AUDIT",
                    username or "unknown",
                    f"auth.failed from {client_ip} ({fails}x)",
                )
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


@app.route("/dashboard")
@login_required
def dashboard_page():
    return render_template(
        "dashboard.html",
        sidebar_hosts=[],
        waktu_segar="",
        kesimpulan="",
        ringkas_angka={},
        pita=[],
        kelompok=[],
        gangguan=[],
        riwayat_labels=[],
        riwayat_series={},
    )


@app.route("/host/<path:ip>")
@login_required
def host_detail(ip):
    conn, c = get_db()
    c.execute("SELECT ip, alias FROM hosts WHERE ip=?", (ip,))
    row = c.fetchone()
    conn.close()
    if not row:
        return redirect(url_for("index"))
    return render_template(
        "host_detail.html", host_ip=row["ip"], host_alias=row["alias"] or row["ip"]
    )


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
            (ip, f"-{hours} hours"),
        )
        rows = c.fetchall()
        labels = [r["timestamp"] for r in rows]
        values = [
            round(r["val"], 2) if r["val"] is not None and r["val"] != -1 else None
            for r in rows
        ]
    else:
        valid_metrics = {
            "cpu": "cpu_percent",
            "ram": "ram_percent",
            "disk": "disk_percent",
            "net_in": "net_in",
            "net_out": "net_out",
        }
        col = valid_metrics.get(metric, "cpu_percent")

        extra = (
            " AND cpu_percent IS NOT NULL"
            if col in ("cpu_percent", "ram_percent", "disk_percent")
            else ""
        )
        c.execute(
            f"SELECT timestamp, {col} as val FROM agent_metrics "
            f"WHERE host=? AND timestamp > datetime('now','localtime','-{hours} hours'){extra} ORDER BY id ASC",
            (ip,),
        )
        rows = c.fetchall()
        labels = [r["timestamp"] for r in rows]
        values = [round(r["val"] or 0, 2) for r in rows]

    conn.close()
    return jsonify(
        {"labels": labels, "values": values, "metric": metric, "hours": hours}
    )


@app.route("/api/host/<path:ip>/stats")
@login_required
def api_host_stats(ip):
    conn, c = get_db()

    c.execute(
        """
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN latency != -1 THEN 1 ELSE 0 END) AS up_count,
            AVG(CASE WHEN latency != -1 THEN latency END) AS avg_ms,
            MIN(CASE WHEN latency != -1 THEN latency END) AS min_ms,
            MAX(CASE WHEN latency != -1 THEN latency END) AS max_ms,
            AVG(packet_loss) AS avg_loss
        FROM ping_logs
        WHERE host=? AND timestamp > datetime('now','localtime','-24 hours')
    """,
        (ip,),
    )
    pr = c.fetchone()

    c.execute(
        "SELECT latency FROM ping_logs WHERE host=? ORDER BY id DESC LIMIT 1", (ip,)
    )
    latest = c.fetchone()

    c.execute(
        """
        SELECT cpu_percent, ram_percent, disk_percent, net_in, net_out, timestamp
        FROM agent_metrics WHERE host=? AND cpu_percent IS NOT NULL ORDER BY id DESC LIMIT 1
    """,
        (ip,),
    )
    ag = c.fetchone()

    net_row = None
    if ag is None:
        c.execute(
            """
            SELECT net_in, net_out, timestamp FROM agent_metrics
            WHERE host=? ORDER BY id DESC LIMIT 1
        """,
            (ip,),
        )
        net_row = c.fetchone()

    conn.close()
    total = pr["total"] or 0
    up_count = pr["up_count"] or 0
    is_pending = total == 0 and latest is None
    has_latest = latest is not None and latest["latency"] is not None
    is_up = bool(has_latest and latest["latency"] != -1)
    return jsonify(
        {
            "ip": ip,
            "uptime_pct": round((up_count / total * 100), 1) if total else None,
            "avg_ms": round(pr["avg_ms"], 2) if pr["avg_ms"] is not None else None,
            "min_ms": round(pr["min_ms"], 2) if pr["min_ms"] is not None else None,
            "max_ms": round(pr["max_ms"], 2) if pr["max_ms"] is not None else None,
            "avg_loss": (
                round(pr["avg_loss"], 1) if pr["avg_loss"] is not None else None
            ),
            "latest_ms": (
                round(latest["latency"], 2)
                if has_latest and latest["latency"] != -1
                else None
            ),
            "is_up": is_up,
            "is_pending": is_pending,
            "agent": (
                {
                    "cpu": round(ag["cpu_percent"] or 0, 1),
                    "ram": round(ag["ram_percent"] or 0, 1),
                    "disk": round(ag["disk_percent"] or 0, 1),
                    "net_in": round(ag["net_in"] or 0, 2),
                    "net_out": round(ag["net_out"] or 0, 2),
                    "last_seen": ag["timestamp"],
                }
                if ag
                else (
                    {
                        "cpu": 0,
                        "ram": 0,
                        "disk": 0,
                        "net_in": round(net_row["net_in"] or 0, 2),
                        "net_out": round(net_row["net_out"] or 0, 2),
                        "last_seen": net_row["timestamp"],
                        "snmp_only": True,
                    }
                    if net_row
                    else None
                )
            ),
        }
    )


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
    ssh_pass langsung dienkripsi (nms.crypto) agar yang tersimpan di DB
    tak pernah plaintext.
    """
    out = {}
    if "ssh_user" in data:
        out["ssh_user"] = str(data.get("ssh_user") or "").strip()[:64]
    if "ssh_pass" in data:
        out["ssh_pass"] = encrypt_secret(str(data.get("ssh_pass") or "")[:128])
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
@limiter.limit("100/minute")
def api_get_hosts():
    conn, c = get_db()
    c.execute(
        "SELECT id, ip, snmp_community, if_index, alias, category,"
        " snmp_profile, cpu_oid, mem_oid, storage_oid, temp_oid,"
        " ssh_user, ssh_port, backup_enable, backup_last, backup_ok,"
        " CASE WHEN ssh_pass IS NOT NULL AND ssh_pass != '' THEN 1 ELSE 0 END AS ssh_pass_set"
        " FROM hosts ORDER BY id ASC"
    )
    hosts = []
    for r in c.fetchall():
        hosts.append(
            {
                "id": r["id"],
                "ip": r["ip"],
                "snmp_community": r["snmp_community"] or "",
                "if_index": r["if_index"] or 1,
                "alias": r["alias"] or "",
                "category": r["category"] or "Uncategorized",
                "snmp_profile": (
                    (r["snmp_profile"] or "auto")
                    if (r["snmp_profile"] or "auto") in SNMP_PROFILES
                    else "auto"
                ),
                "cpu_oid": r["cpu_oid"] or "",
                "mem_oid": r["mem_oid"] or "",
                "storage_oid": r["storage_oid"] or "",
                "temp_oid": r["temp_oid"] or "",
                "ssh_user": r["ssh_user"] or "",
                "ssh_pass_set": bool(r["ssh_pass_set"]),
                "ssh_port": r["ssh_port"] or 22,
                "backup_enable": bool(r["backup_enable"]),
                "backup_last": r["backup_last"] or "",
                "backup_ok": bool(r["backup_ok"]),
            }
        )
    conn.close()
    return jsonify(hosts)


@app.route("/discovery")
@login_required
def discovery_page():
    return render_template("discovery.html")


@app.route("/api/discovery/scheduled/run", methods=["POST"])
@api_login_required
def api_scheduled_discovery_run():
    return jsonify(run_scheduled_snmp_discovery())


@app.route("/api/discovery/snmp", methods=["POST"])
@api_login_required
def api_snmp_discovery():
    data = request.get_json(silent=True) or {}
    network = str(data.get("network") or "").strip()
    community = str(data.get("community") or "").strip()
    try:
        results = discover_subnet(
            network, community, timeout=data.get("timeout", 0.8), max_hosts=256
        )
    except (ValueError, TypeError) as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        logger.warning(f"SNMP discovery gagal: {exc}")
        return jsonify({"error": "Discovery gagal"}), 502
    return jsonify({"network": network, "count": len(results), "devices": results})


@app.route("/api/discovery/import", methods=["POST"])
@api_login_required
def api_discovery_import():
    data = request.get_json(silent=True) or {}
    devices = data.get("devices")
    community = str(data.get("community") or "").strip()[:128]
    if not isinstance(devices, list) or not devices or len(devices) > 256:
        return jsonify({"error": "devices harus list 1-256 item"}), 400
    conn, c = get_db()
    added, skipped = [], []
    try:
        for device in devices:
            ip = str((device or {}).get("ip") or "").strip()
            if not ip:
                continue
            try:
                ipaddress.ip_address(ip)
            except ValueError:
                skipped.append({"ip": ip, "reason": "IP tidak valid"})
                continue
            try:
                c.execute(
                    "INSERT INTO hosts (ip, alias, category, snmp_community, snmp_profile) VALUES (?, ?, ?, ?, ?)",
                    (
                        ip,
                        str((device or {}).get("sys_name") or ip)[:128],
                        str((device or {}).get("vendor") or "Generic")[:64],
                        community,
                        (
                            "mikrotik"
                            if (device or {}).get("vendor") == "MikroTik"
                            else "generic"
                        ),
                    ),
                )
                added.append(ip)
            except sqlite3.IntegrityError:
                skipped.append({"ip": ip, "reason": "sudah terdaftar"})
        conn.commit()
    finally:
        conn.close()
    return jsonify({"added": added, "skipped": skipped, "count": len(added)}), 201


@app.route("/api/hosts", methods=["POST"])
@api_login_required
@limiter.limit("30/minute")
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
        c.execute(
            "INSERT INTO hosts (ip, snmp_community, if_index, alias, category,"
            " snmp_profile, cpu_oid, mem_oid, storage_oid, temp_oid,"
            " ssh_user, ssh_pass, ssh_port, backup_enable)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ip,
                snmp_community,
                if_index,
                alias,
                category,
                snmp_vals["snmp_profile"],
                snmp_vals["cpu_oid"],
                snmp_vals["mem_oid"],
                snmp_vals["storage_oid"],
                snmp_vals["temp_oid"],
                ssh_vals.get("ssh_user", ""),
                ssh_vals.get("ssh_pass", ""),
                ssh_vals.get("ssh_port", 22),
                ssh_vals.get("backup_enable", 0),
            ),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        conn.close()
        return jsonify({"error": "Host sudah ada"}), 400
    conn.close()
    try:
        audit(
            current_user.username,
            "host.create",
            f"{ip} alias={alias} category={category}",
        )
    except Exception:
        pass
    return jsonify({"status": "success"})


@app.route("/api/hosts/bulk/import", methods=["POST"])
@api_login_required
@require_role("admin")
@limiter.limit("10/minute")
def bulk_import():

    if "file" not in request.files:
        return jsonify({"error": "CSV file required"}), 400

    file = request.files["file"]
    if file.filename == "":
        return jsonify({"error": "No file selected"}), 400

    if not file.filename.endswith(".csv"):
        return jsonify({"error": "File harus CSV format"}), 400

    try:
        from io import TextIOWrapper
        import csv as csv_module

        stream = TextIOWrapper(file.stream, encoding="utf-8")
        reader = csv_module.DictReader(stream)

        hosts_data = []
        for row in reader:
            if row:
                hosts_data.append(row)

        if not hosts_data:
            return jsonify({"error": "CSV kosong"}), 400

        success, failed, errors = bulk_import_hosts(hosts_data)

        logger.info(f"Bulk import: {success} success, {failed} failed")
        try:
            audit(
                current_user.username,
                "hosts.bulk_import",
                f"{success} success, {failed} failed",
            )
        except Exception:
            pass

        return (
            jsonify(
                {
                    "status": "completed",
                    "success": success,
                    "failed": failed,
                    "errors": errors[:50],
                }
            ),
            200,
        )
    except Exception as e:
        logger.error(f"Bulk import error: {e}")
        return jsonify({"error": f"Import gagal: {str(e)}"}), 500


@app.route("/api/hosts/<path:ip>", methods=["DELETE"])
@api_login_required
@limiter.limit("30/minute")
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

        for mem in (
            status_memory,
            down_since,
            agent_status_memory,
            agent_offline_memory,
            last_down_telegram,
            mt_alarm_memory,
            mt_is_mikrotik,
            mt_sysup,
            snmp_state,
        ):
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
    category = (
        str(raw_category or "").strip() or "Uncategorized"
        if raw_category is not None
        else None
    )
    conn, c = get_db()
    if category is not None:
        c.execute(
            "UPDATE hosts SET alias=?, category=? WHERE ip=?", (alias, category, ip)
        )
    else:
        c.execute("UPDATE hosts SET alias=? WHERE ip=?", (alias, ip))
    updated = c.rowcount
    conn.commit()
    conn.close()
    if not updated:
        return jsonify({"error": "Host tidak ditemukan"}), 404
    try:
        audit(
            current_user.username,
            "host.rename",
            f"{ip} alias={alias} category={category}",
        )
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
    _ALLOWED_SNMP_COLS = {
        "snmp_profile",
        "cpu_oid",
        "mem_oid",
        "storage_oid",
        "temp_oid",
    }
    _ALLOWED_SSH_COLS = {"ssh_user", "ssh_pass", "ssh_port", "backup_enable"}
    for key in _ALLOWED_SNMP_COLS:
        if key in data:
            sets.append(f"{key}=?")
            params.append(snmp_vals[key])
    for key in _ALLOWED_SSH_COLS:
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
        _changed = [
            k for k in list(snmp_vals) + list(ssh_vals) if k in data and k != "ssh_pass"
        ]
        if "ssh_pass" in data:
            _changed.append("ssh_pass=***")
        audit(current_user.username, "host.snmp", f"{ip} {','.join(_changed)}")
    except Exception:
        pass
    return jsonify({"status": "success"})


@app.route("/api/settings", methods=["GET"])
@api_login_required
def api_get_settings():
    return jsonify(
        {
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
            "mt_cpu_oid": get_setting(
                "mt_cpu_oid", MT_DEFAULT_OIDS["cpu"], type_cast=str
            ),
            "mt_mem_oid": get_setting(
                "mt_mem_oid", MT_DEFAULT_OIDS["mem"], type_cast=str
            ),
            "mt_storage_oid": get_setting(
                "mt_storage_oid", MT_DEFAULT_OIDS["storage"], type_cast=str
            ),
            "mt_temp_oid": get_setting(
                "mt_temp_oid", MT_DEFAULT_OIDS["temp"], type_cast=str
            ),
            "mt_temp_div": get_setting("mt_temp_div", 10, type_cast=float),
            "mt_stale_min": get_setting("mt_stale_min", MT_STALE_MIN),
            "mt_backup_keep": get_setting("mt_backup_keep", 10),
            "discovery_network": get_setting("discovery_network", "", type_cast=str),
            "discovery_community": get_setting(
                "discovery_community", "", type_cast=str
            ),
        }
    )


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
    if "discovery_network" in data:
        network = str(data.get("discovery_network") or "").strip()
        if network:
            try:
                ipaddress.ip_network(network, strict=False)
            except ValueError:
                return jsonify({"error": "discovery_network harus CIDR valid"}), 400
        vals["discovery_network"] = network
    if "discovery_community" in data:
        community = str(data.get("discovery_community") or "").strip()
        if len(community) > 128:
            return jsonify({"error": "discovery_community maksimal 128 karakter"}), 400
        vals["discovery_community"] = community
    merged = {
        k: get_setting(k, d)
        for k, d in [
            ("fiber_rx_overload", FIBER_RX_OVERLOAD),
            ("fiber_rx_warn", FIBER_RX_WARN),
            ("fiber_rx_crit", FIBER_RX_CRIT),
            ("fiber_rx_target", FIBER_RX_TARGET),
            ("fiber_tx_min", FIBER_TX_MIN),
            ("fiber_tx_max", FIBER_TX_MAX),
            ("temp_threshold", 60.0),
            ("temp_crit", 75.0),
        ]
    }
    merged.update({k: v for k, v in vals.items() if k in merged})
    if not (
        merged["fiber_rx_crit"] < merged["fiber_rx_warn"] <= merged["fiber_rx_overload"]
    ):
        return (
            jsonify({"error": "Harus: crit < warn <= overload (mis. -27 < -25 <= -8)"}),
            400,
        )
    if not (
        merged["fiber_rx_crit"]
        <= merged["fiber_rx_target"]
        <= merged["fiber_rx_overload"]
    ):
        return (
            jsonify(
                {
                    "error": "fiber_rx_target harus di antara crit..overload (mis. -27..-8)"
                }
            ),
            400,
        )
    if not (merged["fiber_tx_min"] <= merged["fiber_tx_max"]):
        return jsonify({"error": "fiber_tx_min harus <= fiber_tx_max"}), 400
    if not (merged["temp_threshold"] < merged["temp_crit"]):
        return jsonify({"error": "temp_threshold harus < temp_crit"}), 400

    if not vals:
        return jsonify({"error": "Tidak ada pengaturan yang dikirim"}), 400
    conn, c = get_db()
    for key, v in vals.items():

        c.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(v))
        )
    conn.commit()
    conn.close()
    try:
        audit(
            current_user.username,
            "settings.update",
            " ".join(f"{k}={v}" for k, v in vals.items()),
        )
    except Exception:
        pass
    return jsonify({"status": "success"})


@app.route("/api/settings/password", methods=["POST"])
@api_login_required
@limiter.limit("5/minute")
def api_change_password():
    from werkzeug.security import generate_password_hash

    data = request.get_json(silent=True) or {}
    cur = str(data.get("current_password") or "")[:200]
    new_user = (
        str(data.get("new_username") or _get_admin_username()).strip()[:50]
        or _get_admin_username()
    )
    new_pass = str(data.get("new_password") or "")
    if not _verify_admin(current_user.username, cur):
        return jsonify({"error": "Password saat ini salah"}), 400
    if len(new_pass) < 8 or len(new_pass) > 200:
        return jsonify({"error": "Password baru minimal 8 karakter"}), 400
    if not re.match(r"^[a-zA-Z0-9_.\-]{3,50}$", new_user):
        return jsonify({"error": "Username 3-50 karakter (huruf/angka/_.-)"}), 400
    conn, c = get_db()
    c.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES ('admin_user', ?)",
        (new_user,),
    )
    c.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES ('admin_pass_hash', ?)",
        (generate_password_hash(new_pass),),
    )
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
@limiter.limit("60/minute")
def agent_report():

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

    def _to_bandwidth(v, name):
        """Validasi longgar untuk net_in/net_out agent.

        Spike transient (counter reset / jeda panjang / glitch psutil)
        di-clamp ke 0-100000 agar tak jadi 400 berulang — selaras dengan
        jalur SNMP yang me-skip nilai >100000. Non-angka tetap 400.
        """
        try:
            f = float(v)
        except (ValueError, TypeError):
            raise ValueError(f"{name} harus angka")
        if not (0 <= f <= 100000):
            print(
                f"[AGENT] {name} out-of-range {f!r} di-clamp ke 0-100000 "
                "(kemungkinan spike transient/counter-reset)"
            )
            return min(max(f, 0.0), 100000.0)
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
        return (
            jsonify({"error": "Host belum terdaftar. Tambahkan dulu di dashboard."}),
            404,
        )
    try:
        cpu = _to_float(data.get("cpu", 0.0), "cpu", 0, 100)
        ram = _to_float(data.get("ram", 0.0), "ram", 0, 100)
        disk = _to_float(data.get("disk", 0.0), "disk", 0, 100)
        net_in = _to_bandwidth(data.get("net_in", 0.0), "net_in")
        net_out = _to_bandwidth(data.get("net_out", 0.0), "net_out")
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
            agent_status_memory[host] = {"cpu": False, "ram": False, "disk": False}

        if cpu > cpu_thresh and not agent_status_memory[host]["cpu"]:
            agent_status_memory[host]["cpu"] = True
            telegram_queue.append(
                f"⚠️ *HIGH CPU ALERT*\nHost: `{host}`\nCPU: *{cpu}%* (Batas: {cpu_thresh}%)\nWaktu: {timestamp}"
            )
            log_queue.append(("HIGH_CPU", f"{cpu}% (Batas: {cpu_thresh}%)"))
        elif cpu <= cpu_clear and agent_status_memory[host]["cpu"]:
            agent_status_memory[host]["cpu"] = False
            telegram_queue.append(
                f"✅ *CPU NORMAL*\nHost: `{host}`\nCPU: {cpu}%\nWaktu: {timestamp}"
            )
            log_queue.append(("CPU_NORMAL", f"Kembali normal: {cpu}%"))

        if ram > ram_thresh and not agent_status_memory[host]["ram"]:
            agent_status_memory[host]["ram"] = True
            telegram_queue.append(
                f"⚠️ *HIGH RAM ALERT*\nHost: `{host}`\nRAM: *{ram}%* (Batas: {ram_thresh}%)\nWaktu: {timestamp}"
            )
            log_queue.append(("HIGH_RAM", f"{ram}% (Batas: {ram_thresh}%)"))
        elif ram <= ram_clear and agent_status_memory[host]["ram"]:
            agent_status_memory[host]["ram"] = False
            telegram_queue.append(
                f"✅ *RAM NORMAL*\nHost: `{host}`\nRAM: {ram}%\nWaktu: {timestamp}"
            )
            log_queue.append(("RAM_NORMAL", f"Kembali normal: {ram}%"))

        if disk > disk_thresh and not agent_status_memory[host].get("disk"):
            agent_status_memory[host]["disk"] = True
            telegram_queue.append(
                f"⚠️ *HIGH DISK ALERT*\nHost: `{host}`\nDISK Penuh: *{disk}%* (Batas: {disk_thresh}%)\nWaktu: {timestamp}"
            )
            log_queue.append(("HIGH_DISK", f"{disk}% (Batas: {disk_thresh}%)"))
        elif disk <= disk_clear and agent_status_memory[host].get("disk"):
            agent_status_memory[host]["disk"] = False
            telegram_queue.append(
                f"✅ *DISK NORMAL*\nHost: `{host}`\nKapasitas terpakai: {disk}%\nWaktu: {timestamp}"
            )
            log_queue.append(("DISK_NORMAL", f"Kembali normal: {disk}%"))

        conn, c = get_db()
        try:
            c.execute(
                "INSERT INTO agent_metrics (host, cpu_percent, ram_percent, disk_percent, net_in, net_out, timestamp, source) VALUES (?, ?, ?, ?, ?, ?, ?, 'agent')",
                (host, cpu, ram, disk, net_in, net_out, timestamp),
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
    metrics = get_all_agent_metrics(c, time_range_minutes=5)
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

    valid_metrics = {
        "cpu": "cpu_percent",
        "ram": "ram_percent",
        "disk": "disk_percent",
        "net_in": "net_in",
        "net_out": "net_out",
    }
    col = valid_metrics.get(metric, "cpu_percent")

    conn, c = get_db()
    try:

        extra = (
            " AND cpu_percent IS NOT NULL"
            if col in ("cpu_percent", "ram_percent", "disk_percent")
            else ""
        )
        per_host = {}
        for host in get_target_hosts():
            c.execute(
                f"""
                SELECT timestamp, {col} as val
                FROM agent_metrics
                WHERE host=? AND timestamp > datetime('now', 'localtime', '-{hours} hours'){extra}
                ORDER BY id ASC
            """,
                (host,),
            )
            rows = c.fetchall()
            if rows:
                per_host[host] = [
                    (r["timestamp"], round(r["val"] or 0, 2)) for r in rows
                ]
    finally:
        conn.close()

    labels, datasets = _align_series(per_host, label_fmt=lambda t: t.split(" ")[1])
    return jsonify({"labels": labels, "datasets": datasets, "metric": metric})


@app.route("/api/metrics")
@api_login_required
def get_metrics():
    conn, c = get_db()
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
    conn, c = get_db()
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
    stats = {}
    for host in get_target_hosts():
        c.execute(
            """
            SELECT
                COUNT(*)                                            AS total,
                SUM(CASE WHEN latency != -1 THEN 1 ELSE 0 END)    AS up_count,
                AVG(CASE WHEN latency != -1 THEN latency  END)     AS avg_ms,
                MIN(CASE WHEN latency != -1 THEN latency  END)     AS min_ms,
                MAX(CASE WHEN latency != -1 THEN latency  END)     AS max_ms,
                AVG(CASE WHEN latency != -1 THEN packet_loss END)  AS avg_loss
            FROM ping_logs
            WHERE host=? AND timestamp > datetime('now','localtime','-24 hours')
        """,
            (host,),
        )
        r = c.fetchone()
        total = r["total"] or 0
        up_count = r["up_count"] or 0
        if total == 0:

            stats[host] = {
                "uptime_pct": None,
                "avg_ms": None,
                "min_ms": None,
                "max_ms": None,
                "avg_loss": None,
                "is_down": False,
                "is_pending": True,
            }
        else:
            stats[host] = {
                "uptime_pct": round((up_count / total * 100), 1),
                "avg_ms": round(r["avg_ms"], 2) if r["avg_ms"] is not None else None,
                "min_ms": round(r["min_ms"], 2) if r["min_ms"] is not None else None,
                "max_ms": round(r["max_ms"], 2) if r["max_ms"] is not None else None,
                "avg_loss": (
                    round(r["avg_loss"], 1) if r["avg_loss"] is not None else None
                ),
                "is_down": status_memory.get(host, False),
                "is_pending": False,
            }
    conn.close()
    return jsonify(stats)


@app.route("/api/export/stats", methods=["GET"])
@api_login_required
@limiter.limit("10/minute")
def export_stats():
    format_type = request.args.get("format", "json").lower()
    if format_type not in ("json", "csv"):
        return jsonify({"error": "Format harus json atau csv"}), 400

    conn, c = get_db()
    stats = []
    for host in get_target_hosts():
        c.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN latency != -1 THEN 1 ELSE 0 END) AS up_count,
                AVG(CASE WHEN latency != -1 THEN latency END) AS avg_ms,
                MIN(CASE WHEN latency != -1 THEN latency END) AS min_ms,
                MAX(CASE WHEN latency != -1 THEN latency END) AS max_ms,
                AVG(CASE WHEN latency != -1 THEN packet_loss END) AS avg_loss
            FROM ping_logs
            WHERE host=? AND timestamp > datetime('now','localtime','-24 hours')
        """,
            (host,),
        )
        r = c.fetchone()
        total = r["total"] or 0
        up_count = r["up_count"] or 0
        stats.append(
            {
                "host": host,
                "uptime_pct": round((up_count / total * 100), 1) if total else None,
                "avg_ms": round(r["avg_ms"], 2) if r["avg_ms"] is not None else None,
                "min_ms": round(r["min_ms"], 2) if r["min_ms"] is not None else None,
                "max_ms": round(r["max_ms"], 2) if r["max_ms"] is not None else None,
                "avg_loss": (
                    round(r["avg_loss"], 1) if r["avg_loss"] is not None else None
                ),
                "is_down": status_memory.get(host, False),
                "total_checks": total,
                "up_checks": up_count,
            }
        )
    conn.close()

    if format_type == "json":
        return Response(
            jsonify(stats).get_json(force=True),
            mimetype="application/json",
            headers={"Content-Disposition": "attachment;filename=stats.json"},
        )
    else:
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=stats[0].keys() if stats else [])
        writer.writeheader()
        writer.writerows(stats)
        return Response(
            output.getvalue(),
            mimetype="text/csv",
            headers={"Content-Disposition": "attachment;filename=stats.csv"},
        )


@app.route("/api/export/events", methods=["GET"])
@api_login_required
@limiter.limit("10/minute")
def export_events():
    format_type = request.args.get("format", "json").lower()
    if format_type not in ("json", "csv"):
        return jsonify({"error": "Format harus json atau csv"}), 400

    conn, c = get_db()
    try:
        c.execute("""
            SELECT id, host, started_at, resolved_at, duration_s
            FROM down_events ORDER BY id DESC LIMIT 1000
        """)
    except sqlite3.OperationalError:
        return jsonify({"error": "Tabel events tidak ditemukan"}), 500

    events = []
    for r in c.fetchall():
        events.append(
            {
                "id": r["id"],
                "host": r["host"],
                "started_at": r["started_at"],
                "resolved_at": r["resolved_at"] or "Ongoing",
                "duration_s": r["duration_s"] or 0,
                "status": "resolved" if r["resolved_at"] else "ongoing",
            }
        )
    conn.close()

    if format_type == "json":
        return Response(
            jsonify(events).get_json(force=True),
            mimetype="application/json",
            headers={"Content-Disposition": "attachment;filename=events.json"},
        )
    else:
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=events[0].keys() if events else [])
        writer.writeheader()
        writer.writerows(events)
        return Response(
            output.getvalue(),
            mimetype="text/csv",
            headers={"Content-Disposition": "attachment;filename=events.csv"},
        )


@app.route("/api/audit/logs", methods=["GET"])
@api_login_required
@limiter.limit("20/minute")
def get_audit_logs():
    try:
        limit = int(request.args.get("limit", 100))
        offset = int(request.args.get("offset", 0))
    except (ValueError, TypeError):
        return jsonify({"error": "limit dan offset harus angka"}), 400

    limit = max(1, min(limit, 500))
    offset = max(0, offset)

    username = request.args.get("username", "").strip()
    action = request.args.get("action", "").strip()

    conn, c = get_db()

    query = "SELECT id, timestamp, username, action, target, detail, ip_address FROM audit_logs WHERE 1=1"
    params = []

    if username:
        query += " AND username LIKE ?"
        params.append(f"%{username}%")
    if action:
        query += " AND action LIKE ?"
        params.append(f"%{action}%")

    query += " ORDER BY id DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    c.execute(query, params)
    logs = [dict(r) for r in c.fetchall()]

    c.execute(
        "SELECT COUNT(*) AS cnt FROM audit_logs WHERE 1=1"
        + (" AND username LIKE ?" if username else "")
        + (" AND action LIKE ?" if action else ""),
        params[:-2],
    )
    total = c.fetchone()["cnt"] or 0

    conn.close()

    return jsonify(
        {
            "logs": logs,
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_next": (offset + limit) < total,
        }
    )


@app.route("/api/audit/export", methods=["GET"])
@api_login_required
@limiter.limit("5/minute")
def export_audit_logs():
    format_type = request.args.get("format", "json").lower()
    if format_type not in ("json", "csv"):
        return jsonify({"error": "Format harus json atau csv"}), 400

    days = request.args.get("days", 30)
    try:
        days = max(1, min(int(days), 90))
    except (ValueError, TypeError):
        days = 30

    conn, c = get_db()
    c.execute(f"""
        SELECT id, timestamp, username, action, target, detail, ip_address FROM audit_logs
        WHERE timestamp > datetime('now', 'localtime', '-{days} days')
        ORDER BY id DESC
        """)
    logs = [dict(r) for r in c.fetchall()]
    conn.close()

    if format_type == "json":
        return Response(
            jsonify(logs).get_json(force=True),
            mimetype="application/json",
            headers={"Content-Disposition": "attachment;filename=audit_logs.json"},
        )
    else:
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=logs[0].keys() if logs else [])
        writer.writeheader()
        writer.writerows(logs)
        return Response(
            output.getvalue(),
            mimetype="text/csv",
            headers={"Content-Disposition": "attachment;filename=audit_logs.csv"},
        )


@app.route("/api/backups", methods=["GET"])
@api_login_required
@limiter.limit("20/minute")
def get_backups():
    backups = list_backups()
    return jsonify({"backups": backups})


@app.route("/api/backups/restore", methods=["POST"])
@api_login_required
@limiter.limit("5/minute")
def restore_backup():
    data = request.get_json(silent=True) or {}
    backup_filename = str(data.get("filename") or "").strip()

    valid, sanitized, error = validate_backup_filename(backup_filename)
    if not valid:
        return jsonify({"error": error}), 400

    backup_path = os.path.join(BASE_DIR, "backups", sanitized)

    if not os.path.exists(backup_path):
        return jsonify({"error": "Backup file not found"}), 404

    success, message = restore_database(backup_path)

    if success:
        logger.info(
            f"Database restored from {backup_filename} by {current_user.username}"
        )
        try:
            audit(current_user.username, "backup.restore", backup_filename)
        except Exception:
            pass
        return jsonify({"status": "success", "message": message}), 200
    else:
        logger.error(f"Restore failed: {message}")
        return jsonify({"error": message}), 400


@app.route("/api/backups/manual", methods=["POST"])
@api_login_required
@limiter.limit("3/hour")
def create_manual_backup():
    backup_dir = os.path.join(BASE_DIR, "backups")
    os.makedirs(backup_dir, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = os.path.join(backup_dir, f"network_backup_manual_{timestamp}.db")

    try:
        src = dst = None
        src = sqlite3.connect(resolve_db_path(), timeout=30)
        dst = sqlite3.connect(backup_path, timeout=30)
        src.backup(dst)
        dst.close()
        src.close()

        stat = os.stat(backup_path)
        logger.info(f"Manual backup created: {os.path.basename(backup_path)}")
        try:
            audit(
                current_user.username,
                "backup.create_manual",
                os.path.basename(backup_path),
            )
        except Exception:
            pass

        return (
            jsonify(
                {
                    "status": "success",
                    "filename": os.path.basename(backup_path),
                    "size_mb": round(stat.st_size / (1024 * 1024), 2),
                }
            ),
            200,
        )
    except Exception as e:
        logger.error(f"Manual backup failed: {e}")
        return jsonify({"error": f"Backup gagal: {str(e)}"}), 500


@app.route("/api/users", methods=["GET"])
@api_login_required
@require_role("admin")
@limiter.limit("30/minute")
def get_users():

    users = list_users()
    return jsonify({"users": users})


@app.route("/api/users", methods=["POST"])
@api_login_required
@require_role("admin")
@limiter.limit("10/minute")
def add_user():

    data = request.get_json(silent=True) or {}
    username = str(data.get("username") or "").strip()[:50]
    password = str(data.get("password") or "")
    role = str(data.get("role") or "operator").lower()

    if not username or len(username) < 3:
        return jsonify({"error": "Username minimum 3 karakter"}), 400

    if not password or len(password) < 8:
        return jsonify({"error": "Password minimum 8 karakter"}), 400

    if role not in ("admin", "operator", "viewer"):
        return jsonify({"error": "Role harus: admin, operator, atau viewer"}), 400

    try:
        from werkzeug.security import generate_password_hash

        password_hash = generate_password_hash(password)
    except Exception:
        return jsonify({"error": "Password hash gagal"}), 500

    success, message = create_user(username, password_hash, role)

    if success:
        logger.info(f"User created: {username} (role: {role})")
        try:
            audit(current_user.username, "user.create", f"{username} role={role}")
        except Exception:
            pass
        return jsonify({"status": "success"}), 201
    else:
        return jsonify({"error": message}), 400


@app.route("/api/users/<username>", methods=["DELETE"])
@api_login_required
@require_role("admin")
@limiter.limit("10/minute")
def delete_user_endpoint(username):

    username = str(username or "").strip()[:50]
    success, message = delete_user(username)

    if success:
        logger.info(f"User deleted: {username}")
        try:
            audit(current_user.username, "user.delete", username)
        except Exception:
            pass
        return jsonify({"status": "success"}), 200
    else:
        return jsonify({"error": message}), 400


@app.route("/api/users/<username>/role", methods=["PATCH"])
@api_login_required
@require_role("admin")
@limiter.limit("10/minute")
def update_user_role_endpoint(username):

    username = str(username or "").strip()[:50]
    data = request.get_json(silent=True) or {}
    role = str(data.get("role") or "").lower()

    if not role:
        return jsonify({"error": "Role required"}), 400

    success, message = update_user_role(username, role)

    if success:
        logger.info(f"User role updated: {username} -> {role}")
        try:
            audit(current_user.username, "user.role_update", f"{username} -> {role}")
        except Exception:
            pass
        return jsonify({"status": "success"}), 200
    else:
        return jsonify({"error": message}), 400


@app.route("/api/dashboard/summary", methods=["GET"])
@api_login_required
@limiter.limit("60/minute")
def dashboard_summary():
    conn, c = get_db()

    try:
        c.execute("SELECT COUNT(*) AS cnt FROM hosts")
        total_hosts = c.fetchone()["cnt"] or 0

        up_count = 0
        down_count = 0
        pending_count = 0

        for host in get_target_hosts():
            if status_memory.get(host, False):
                down_count += 1
            elif host in status_memory:
                up_count += 1
            else:
                pending_count += 1

        c.execute("""
            SELECT COUNT(*) AS cnt FROM down_events 
            WHERE resolved_at IS NULL
            """)
        active_alerts = c.fetchone()["cnt"] or 0

        c.execute("""
            SELECT 
                COUNT(*) AS total,
                SUM(CASE WHEN latency != -1 THEN 1 ELSE 0 END) AS up_count
            FROM ping_logs
            WHERE timestamp > datetime('now', 'localtime', '-24 hours')
            """)
        r = c.fetchone()
        uptime_24h = None
        if r["total"] and r["total"] > 0:
            uptime_24h = round((r["up_count"] or 0) / r["total"] * 100, 1)

        c.execute("""
            SELECT 
                COUNT(*) AS cnt,
                SUM(CASE WHEN status='ONLINE' THEN 1 ELSE 0 END) AS online_cnt
            FROM services
            """)
        svc = c.fetchone()
        total_services = svc["cnt"] or 0
        online_services = svc["online_cnt"] or 0

        c.execute("""
            SELECT COUNT(*) AS cnt FROM system_logs
            WHERE event_type LIKE 'HIGH_%' AND timestamp > datetime('now', 'localtime', '-24 hours')
            """)
        resource_alerts = c.fetchone()["cnt"] or 0

    finally:
        conn.close()

    return jsonify(
        {
            "timestamp": datetime.now().isoformat(),
            "hosts": {
                "total": total_hosts,
                "up": up_count,
                "down": down_count,
                "pending": pending_count,
            },
            "alerts": {"active": active_alerts, "resource_24h": resource_alerts},
            "uptime_24h": uptime_24h,
            "services": {
                "total": total_services,
                "online": online_services,
                "offline": total_services - online_services,
            },
        }
    )


@app.route("/api/hosts/<path:ip>/thresholds", methods=["GET"])
@api_login_required
@limiter.limit("60/minute")
def get_host_threshold_endpoint(ip):
    ip = (ip or "").strip()
    thresholds = get_host_threshold(ip)

    if not thresholds:
        cpu_thresh = get_setting("cpu_threshold", 85.0)
        ram_thresh = get_setting("ram_threshold", 90.0)
        disk_thresh = get_setting("disk_threshold", 90.0)
        thresholds = {
            "cpu_threshold": cpu_thresh,
            "ram_threshold": ram_thresh,
            "disk_threshold": disk_thresh,
            "is_custom": False,
        }
    else:
        thresholds["is_custom"] = True

    return jsonify(thresholds)


@app.route("/api/hosts/<path:ip>/thresholds", methods=["POST"])
@api_login_required
@limiter.limit("30/minute")
def set_host_threshold_endpoint(ip):
    ip = (ip or "").strip()
    data = request.get_json(silent=True) or {}

    cpu = data.get("cpu_threshold")
    ram = data.get("ram_threshold")
    disk = data.get("disk_threshold")

    if cpu is not None:
        try:
            cpu = float(cpu)
            if not 1 <= cpu <= 100:
                return jsonify({"error": "cpu_threshold harus 1-100"}), 400
        except (ValueError, TypeError):
            return jsonify({"error": "cpu_threshold harus angka"}), 400

    if ram is not None:
        try:
            ram = float(ram)
            if not 1 <= ram <= 100:
                return jsonify({"error": "ram_threshold harus 1-100"}), 400
        except (ValueError, TypeError):
            return jsonify({"error": "ram_threshold harus angka"}), 400

    if disk is not None:
        try:
            disk = float(disk)
            if not 1 <= disk <= 100:
                return jsonify({"error": "disk_threshold harus 1-100"}), 400
        except (ValueError, TypeError):
            return jsonify({"error": "disk_threshold harus angka"}), 400

    success, message = set_host_threshold(ip, cpu, ram, disk)

    if success:
        logger.info(f"Host threshold set: {ip} cpu={cpu} ram={ram} disk={disk}")
        try:
            audit(
                current_user.username,
                "host.threshold_set",
                f"{ip} cpu={cpu} ram={ram} disk={disk}",
            )
        except Exception:
            pass
        return jsonify({"status": "success"}), 200
    else:
        return jsonify({"error": message}), 400


@app.route("/api/hosts/<path:ip>/thresholds", methods=["DELETE"])
@api_login_required
@limiter.limit("30/minute")
def delete_host_threshold_endpoint(ip):
    ip = (ip or "").strip()

    success, message = delete_host_threshold(ip)

    if success:
        logger.info(f"Host threshold deleted: {ip}")
        try:
            audit(current_user.username, "host.threshold_delete", ip)
        except Exception:
            pass
        return jsonify({"status": "success"}), 200
    else:
        return jsonify({"error": message}), 400


@app.route("/api/thresholds/list", methods=["GET"])
@api_login_required
@limiter.limit("30/minute")
def list_thresholds():
    thresholds = list_host_thresholds()
    return jsonify({"thresholds": thresholds})


@app.route("/api/alerts/history", methods=["GET"])
@api_login_required
@limiter.limit("30/minute")
def get_alerts_history():
    try:
        limit = int(request.args.get("limit", 100))
        offset = int(request.args.get("offset", 0))
    except (ValueError, TypeError):
        return jsonify({"error": "limit dan offset harus angka"}), 400

    limit = max(1, min(limit, 500))
    offset = max(0, offset)

    host = request.args.get("host", "").strip()
    alert_type = request.args.get("type", "").strip()
    severity = request.args.get("severity", "").strip()

    alerts = get_alert_history(
        host=host if host else None,
        alert_type=alert_type if alert_type else None,
        severity=severity if severity else None,
        limit=limit,
        offset=offset,
    )

    return jsonify({"alerts": alerts, "limit": limit, "offset": offset})


@app.route("/api/alerts/active", methods=["GET"])
@api_login_required
@limiter.limit("60/minute")
def get_active_alerts_endpoint():
    host = request.args.get("host", "").strip()

    alerts = get_active_alerts(host=host if host else None)

    return jsonify({"active_alerts": alerts, "count": len(alerts)})


@app.route("/incidents")
@login_required
def incidents_page():
    return render_template("incidents.html")


@app.route("/api/alerts/incidents", methods=["GET"])
@api_login_required
def get_alert_incidents():
    alerts = get_active_alerts()
    groups = {}
    for alert in alerts:
        host = str(alert.get("host") or "").strip()
        if host.upper().startswith(("OLT:", "ODP:")):
            key = host.upper()
        else:
            key = host
        incident = groups.setdefault(
            key, {"key": key, "severity": "warning", "alerts": []}
        )
        incident["alerts"].append(alert)
        rank = {"disaster": 3, "high": 2, "warning": 1, "info": 0}
        if rank.get(alert.get("severity"), 0) > rank.get(incident["severity"], 0):
            incident["severity"] = alert.get("severity")
    incidents = sorted(
        groups.values(),
        key=lambda x: (
            -{"disaster": 3, "high": 2, "warning": 1}.get(x["severity"], 0),
            x["key"],
        ),
    )
    for incident in incidents:
        incident["count"] = len(incident["alerts"])
    return jsonify({"incidents": incidents, "count": len(incidents)})


@app.route("/api/alerts/<int:alert_id>/workflow", methods=["PATCH"])
@api_login_required
@limiter.limit("30/minute")
def update_alert_workflow_endpoint(alert_id):
    data = request.get_json(silent=True) or {}
    allowed = {"acknowledged", "assigned_to", "note"}
    if not any(key in data for key in allowed):
        return jsonify({"error": "Tidak ada perubahan"}), 400
    acknowledged = data.get("acknowledged") if "acknowledged" in data else None
    if acknowledged is not None and not isinstance(acknowledged, bool):
        return jsonify({"error": "acknowledged harus boolean"}), 400
    assigned_to = data.get("assigned_to") if "assigned_to" in data else None
    note = data.get("note") if "note" in data else None
    if assigned_to is not None and not isinstance(assigned_to, str):
        return jsonify({"error": "assigned_to harus teks"}), 400
    if note is not None and not isinstance(note, str):
        return jsonify({"error": "note harus teks"}), 400
    if note is not None and len(note) > 1000:
        return jsonify({"error": "note maksimal 1000 karakter"}), 400
    success = update_alert_workflow(
        alert_id,
        current_user.username,
        acknowledged=acknowledged,
        assigned_to=assigned_to,
        note=note,
    )
    if not success:
        return jsonify({"error": "Alert tidak ditemukan"}), 404
    audit(current_user.username, "alert.workflow_update", f"alert_id={alert_id}")
    return jsonify({"status": "success"}), 200


@app.route("/api/alerts/<int:alert_id>/resolve", methods=["POST"])
@api_login_required
@limiter.limit("30/minute")
def resolve_alert_endpoint(alert_id):
    success = resolve_alert(alert_id)

    if success:
        logger.info(f"Alert resolved: {alert_id}")
        try:
            audit(current_user.username, "alert.resolve", f"alert_id={alert_id}")
        except Exception:
            pass
        return jsonify({"status": "success"}), 200
    else:
        return jsonify({"error": "Alert tidak ditemukan"}), 404


@app.route("/api/reports/daily", methods=["GET"])
@api_login_required
@limiter.limit("20/minute")
def get_daily_report():
    report = generate_daily_report()
    return jsonify(report)


@app.route("/api/reports/daily/send", methods=["POST"])
@api_login_required
@require_role("admin")
@limiter.limit("5/minute")
def send_daily_report_endpoint():

    try:
        send_daily_report()
        logger.info(f"Manual daily report sent by {current_user.username}")
        try:
            audit(current_user.username, "report.send_daily", "manual")
        except Exception:
            pass
        return jsonify({"status": "success", "message": "Report sent"}), 200
    except Exception as e:
        logger.error(f"Send report error: {e}")
        return jsonify({"error": str(e)}), 500


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
        events.append(
            {
                "id": r["id"],
                "host": r["host"],
                "started_at": r["started_at"],
                "resolved_at": r["resolved_at"] or "Ongoing",
                "duration": (
                    f"{m}m {s}s"
                    if r["duration_s"]
                    else ("Ongoing" if not r["resolved_at"] else "—")
                ),
                "status": "resolved" if r["resolved_at"] else "ongoing",
                "is_maintenance": is_maint,
            }
        )
    conn.close()
    return jsonify(events)


@app.route("/api/events/<int:event_id>", methods=["DELETE"])
@api_login_required
def delete_event(event_id):
    with db_lock:
        conn, c = get_db()
        try:
            c.execute(
                "SELECT host, resolved_at FROM down_events WHERE id=?", (event_id,)
            )
            row = c.fetchone()
            if not row:
                return jsonify({"error": "Event tidak ditemukan"}), 404
            host = row["host"]
            was_ongoing = row["resolved_at"] is None
            c.execute("DELETE FROM down_events WHERE id=?", (event_id,))
            conn.commit()
            if was_ongoing:

                c.execute(
                    "SELECT 1 FROM down_events WHERE host=? AND resolved_at IS NULL LIMIT 1",
                    (host,),
                )
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
        lowered = host.lower()
        if (
            not is_host
            and not is_ont
            and (lowered.startswith("odp:") or lowered.startswith("olt:"))
        ):
            scope, _, name = host.partition(":")
            name = name.strip()
            if not name:
                return (
                    jsonify({"error": "Format harus ODP:<nama> atau OLT:<nama>"}),
                    400,
                )
            try:
                if lowered.startswith("odp:"):
                    c.execute(
                        "SELECT name FROM odps WHERE name COLLATE NOCASE = ?", (name,)
                    )
                else:
                    c.execute(
                        "SELECT name FROM olts WHERE name COLLATE NOCASE = ?", (name,)
                    )
                row = c.fetchone()
            except sqlite3.OperationalError:
                row = None
            if not row:
                return (
                    jsonify({"error": f"{scope.upper()} '{name}' belum terdaftar."}),
                    404,
                )
            host = f"{scope.upper()}:{row['name']}"
        if (
            not is_host
            and not is_ont
            and not (host.startswith("ODP:") or host.startswith("OLT:"))
        ):
            return (
                jsonify(
                    {
                        "error": "Host/ONT SN belum terdaftar. Tambahkan dulu di dashboard. Untuk fiber massal pakai ODP:<nama> atau OLT:<nama>."
                    }
                ),
                404,
            )
        c.execute(
            "SELECT 1 FROM maintenance_windows WHERE host=? AND start_at <= ? AND end_at >= ? LIMIT 1",
            (host, _fmt_maint_time(end_dt), _fmt_maint_time(start_dt)),
        )
        if c.fetchone():
            return (
                jsonify(
                    {
                        "error": "Window maintenance bertabrakan dengan jadwal aktif host ini"
                    }
                ),
                400,
            )
    finally:
        conn.close()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with db_lock:
        conn, c = get_db()
        try:
            c.execute(
                "INSERT INTO maintenance_windows (host, start_at, end_at, reason, created_by, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    host,
                    _fmt_maint_time(start_dt),
                    _fmt_maint_time(end_dt),
                    reason,
                    current_user.username,
                    now_str,
                ),
            )
            new_id = c.lastrowid
            _insert_system_log(
                c,
                "MAINTENANCE_CREATE",
                host,
                f"{_fmt_maint_time(start_dt)} s/d {_fmt_maint_time(end_dt)}{(' - ' + reason) if reason else ''}",
                now_str,
            )
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
        c.execute(
            "SELECT id FROM services WHERE ip=? AND type='tcp' AND port=?", (ip, port)
        )
    else:
        c.execute(
            "SELECT id FROM services WHERE ip=? AND type='http' AND url=?", (ip, url)
        )
    if c.fetchone():
        conn.close()
        return jsonify({"error": "Service monitor ini sudah ada"}), 400
    c.execute(
        "INSERT INTO services (ip, name, type, port, url) VALUES (?, ?, ?, ?, ?)",
        (ip, name, svc_type, port, url),
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
        audit(
            current_user.username,
            "service.delete",
            f"{row['name']} {row['ip']} id={svc_id}",
        )
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
        c.execute(
            "SELECT id, name, type, ip, port, url, status FROM services WHERE id=?",
            (svc_id,),
        )
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
    labels = [
        (
            r["timestamp"].split(" ")[1]
            if " " in (r["timestamp"] or "")
            else r["timestamp"]
        )
        for r in rows
    ]
    values = [round(r["latency"], 2) if r["status"] == "ONLINE" else None for r in rows]
    return jsonify(
        {
            "service": dict(svc),
            "hours": hours,
            "labels": labels,
            "values": values,
            "count": len(rows),
        }
    )


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


@app.route("/sla")
@login_required
def sla_page():
    return render_template("sla.html")


@app.route("/api/interfaces/traffic")
@api_login_required
def api_interface_traffic():
    host = request.args.get("host", "").strip()
    try:
        hours = max(1, min(int(request.args.get("hours", 24)), 168))
    except (TypeError, ValueError):
        return jsonify({"error": "hours harus angka"}), 400
    if not host:
        return jsonify({"error": "host wajib diisi"}), 400
    conn, c = get_db()
    c.execute(
        "SELECT if_index, name FROM snmp_interfaces WHERE host=? ORDER BY if_index",
        (host,),
    )
    interfaces = [dict(row) for row in c.fetchall()]
    c.execute(
        "SELECT if_index, timestamp, net_in, net_out FROM iface_traffic WHERE host=? AND timestamp > datetime('now','localtime',?) ORDER BY timestamp ASC",
        (host, f"-{hours} hours"),
    )
    traffic = [dict(row) for row in c.fetchall()]
    conn.close()
    return jsonify(
        {"host": host, "hours": hours, "interfaces": interfaces, "traffic": traffic}
    )


@app.route("/api/sla/history")
@api_login_required
def api_sla_history():
    days = max(1, min(int(request.args.get("days", 30)), 90))
    conn, c = get_db()
    c.execute(
        "SELECT date(timestamp) AS day, COUNT(*) AS checks, SUM(CASE WHEN latency != -1 THEN 1 ELSE 0 END) AS up FROM ping_logs WHERE timestamp > datetime('now','localtime',?) GROUP BY date(timestamp) ORDER BY day",
        (f"-{days} days",),
    )
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return jsonify(
        {
            "days": days,
            "history": [
                {
                    "date": r["day"],
                    "uptime_pct": (
                        round((r["up"] or 0) / r["checks"] * 100, 2)
                        if r["checks"]
                        else None
                    ),
                    "checks": r["checks"],
                }
                for r in rows
            ],
        }
    )


@app.route("/api/sla/sites")
@api_login_required
def api_sla_sites():
    days = max(1, min(int(request.args.get("days", 7)), 90))
    conn, c = get_db()
    c.execute(
        "SELECT location, ip, hostname FROM inventory WHERE location IS NOT NULL AND location != ''"
    )
    sites = {}
    for row in c.fetchall():
        site = row["location"]
        stats = sites.setdefault(
            site, {"site": site, "hosts": 0, "checks": 0, "up": 0, "outages": 0}
        )
        stats["hosts"] += 1
        c.execute(
            "SELECT COUNT(*) AS checks, SUM(CASE WHEN latency != -1 THEN 1 ELSE 0 END) AS up FROM ping_logs WHERE host=? AND timestamp > datetime('now','localtime',?)",
            (row["ip"], f"-{days} days"),
        )
        check = c.fetchone()
        stats["checks"] += check["checks"] or 0
        stats["up"] += check["up"] or 0
        c.execute(
            "SELECT COUNT(*) AS n FROM down_events WHERE host=? AND started_at > datetime('now','localtime',?) AND (is_maintenance IS NULL OR is_maintenance=0)",
            (row["ip"], f"-{days} days"),
        )
        stats["outages"] += c.fetchone()["n"] or 0
    conn.close()
    for stats in sites.values():
        stats["uptime_pct"] = (
            round(stats["up"] / stats["checks"] * 100, 2) if stats["checks"] else None
        )
    return jsonify(
        {"days": days, "sites": sorted(sites.values(), key=lambda x: x["site"])}
    )


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
        out.append(
            {
                **inv,
                "live": live,
                "last_latency": last_latency,
                "last_seen": last_seen,
                "monitored": is_monitored,
            }
        )
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
    if (
        len(pic_name) > 100
        or len(pic_phone) > 30
        or len(asset_no) > 50
        or len(notes) > 500
    ):
        return None, "Data PIC/aset/catatan terlalu panjang"
    if install_date and not re.match(r"^\d{4}-\d{2}-\d{2}$", install_date):
        return None, "Tanggal pasang harus YYYY-MM-DD"
    if install_date:
        try:
            datetime.strptime(install_date, "%Y-%m-%d")
        except ValueError:
            return None, "Tanggal pasang tidak valid"
    return {
        "hostname": hostname,
        "ip": ip,
        "device_type": device_type,
        "brand_model": brand_model,
        "location": location,
        "pic_name": pic_name,
        "pic_phone": pic_phone,
        "install_date": install_date,
        "asset_status": asset_status,
        "asset_no": asset_no,
        "notes": notes,
    }, None


@app.route("/api/inventory", methods=["POST"])
@api_login_required
def api_inventory_create():
    vals, err = _validate_inventory(request.get_json(silent=True) or {})
    if err:
        return jsonify({"error": err}), 400
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn, c = get_db()
    try:
        c.execute(
            """INSERT INTO inventory(hostname,ip,device_type,brand_model,location,
                     pic_name,pic_phone,install_date,asset_status,asset_no,notes,created_at,updated_at)
                     VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                vals["hostname"],
                vals["ip"],
                vals["device_type"],
                vals["brand_model"],
                vals["location"],
                vals["pic_name"],
                vals["pic_phone"],
                vals["install_date"],
                vals["asset_status"],
                vals["asset_no"],
                vals["notes"],
                now,
                now,
            ),
        )
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
        c.execute(
            """UPDATE inventory SET hostname=?, ip=?, device_type=?, brand_model=?, location=?,
                     pic_name=?, pic_phone=?, install_date=?, asset_status=?, asset_no=?, notes=?, updated_at=?
                     WHERE id=?""",
            (
                vals["hostname"],
                vals["ip"],
                vals["device_type"],
                vals["brand_model"],
                vals["location"],
                vals["pic_name"],
                vals["pic_phone"],
                vals["install_date"],
                vals["asset_status"],
                vals["asset_no"],
                vals["notes"],
                now,
                iid,
            ),
        )
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


@app.route("/api/inventory/export")
@api_login_required
def api_inventory_export():
    conn, c = get_db()
    c.execute("SELECT * FROM inventory ORDER BY hostname ASC")
    rows = c.fetchall()
    conn.close()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(
        [
            "id",
            "hostname",
            "ip",
            "tipe",
            "merek_model",
            "lokasi",
            "pic",
            "no_hp",
            "tgl_pasang",
            "status_aset",
            "no_aset",
            "catatan",
        ]
    )
    for r in rows:
        w.writerow(
            [
                r["id"],
                _csv_safe(r["hostname"]),
                _csv_safe(r["ip"]),
                _csv_safe(r["device_type"]),
                _csv_safe(r["brand_model"]),
                _csv_safe(r["location"]),
                _csv_safe(r["pic_name"]),
                _csv_safe(r["pic_phone"]),
                _csv_safe(r["install_date"]),
                _csv_safe(r["asset_status"]),
                _csv_safe(r["asset_no"]),
                _csv_safe(r["notes"]),
            ]
        )
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=inventory.csv"},
    )


@app.route("/triggers")
@login_required
def triggers_page():
    return render_template("triggers.html")


@app.route("/api/triggers")
@api_login_required
def get_triggers():

    valid_hosts = set(get_target_hosts())
    maint_map = get_active_maintenance_map()

    alarms = []

    for host, is_down in list(status_memory.items()):
        if host not in valid_hosts:
            continue
        if is_down:
            if host in maint_map:
                reason = (maint_map[host].get("reason") or "").strip()[:200]
                alarms.append(
                    {
                        "host": host,
                        "severity": "warning",
                        "message": f"In maintenance (DOWN suppressed){(' - ' + reason) if reason else ''}",
                        "category": "maintenance",
                    }
                )
            else:
                alarms.append(
                    {
                        "host": host,
                        "severity": "disaster",
                        "message": "Host is DOWN (Unreachable)",
                        "category": "availability",
                    }
                )

    for host, is_offline in list(agent_offline_memory.items()):
        if host not in valid_hosts:
            continue
        if host in maint_map:
            continue
        if is_offline and not status_memory.get(host, False):
            alarms.append(
                {
                    "host": host,
                    "severity": "high",
                    "message": "NMS Agent is Offline / Not reporting",
                    "category": "agent",
                }
            )

    for host, status in list(agent_status_memory.items()):
        if host not in valid_hosts:
            continue
        if not status_memory.get(host, False) and not agent_offline_memory.get(
            host, False
        ):
            if status.get("cpu", False):
                alarms.append(
                    {
                        "host": host,
                        "severity": "warning",
                        "message": "High CPU Usage",
                        "category": "resource",
                    }
                )
            if status.get("ram", False):
                alarms.append(
                    {
                        "host": host,
                        "severity": "warning",
                        "message": "High RAM Usage",
                        "category": "resource",
                    }
                )
            if status.get("disk", False):
                alarms.append(
                    {
                        "host": host,
                        "severity": "warning",
                        "message": "High Disk Usage",
                        "category": "resource",
                    }
                )

    try:
        conn, c = get_db()
        try:
            c.execute(
                "SELECT id, ip, name, type, url, status, ssl_days_left, ssl_expires_at FROM services"
            )
            for r in c.fetchall():
                d = dict(r)
                st = (d.get("status") or "").upper()
                label = f"{d.get('name')} ({d.get('ip')})"
                if st == "OFFLINE":
                    alarms.append(
                        {
                            "host": label,
                            "severity": "high",
                            "message": f"Service OFFLINE: {d.get('name')} [{d.get('type')}]",
                            "category": "service",
                        }
                    )
                lvl = _ssl_level(d.get("ssl_days_left"))
                if lvl and (d.get("url") or "").lower().startswith("https://"):
                    try:
                        days = int(d.get("ssl_days_left") or 0)
                    except (ValueError, TypeError):
                        days = None
                    if lvl == "expired":
                        alarms.append(
                            {
                                "host": label,
                                "severity": "disaster",
                                "message": f"SSL EXPIRED: {d.get('url')} (exp {d.get('ssl_expires_at') or '-'})",
                                "category": "service",
                            }
                        )
                    elif lvl == "high":
                        alarms.append(
                            {
                                "host": label,
                                "severity": "high",
                                "message": f"SSL expire H-{days}: {d.get('url')} (exp {d.get('ssl_expires_at') or '-'})",
                                "category": "service",
                            }
                        )
                    else:
                        alarms.append(
                            {
                                "host": label,
                                "severity": "warning",
                                "message": f"SSL expire H-{days}: {d.get('url')} (exp {d.get('ssl_expires_at') or '-'})",
                                "category": "service",
                            }
                        )
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
                scope_txt = (
                    f" ({maint_scope.upper()})"
                    if maint_scope and maint_scope != "ont"
                    else ""
                )
                alarms.append(
                    {
                        "host": f"{d.get('ont_sn')}",
                        "severity": "warning",
                        "message": f"Fiber dalam maintenance{scope_txt} — alarm disuppress{(' - ' + maint_reason) if maint_reason else ''}",
                        "category": "maintenance",
                    }
                )
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
                    _pname = _parent.get("name") or _okey
                    alarms.append(
                        {
                            "host": f"{d.get('ont_sn')}",
                            "severity": "warning",
                            "message": f"Fiber {status.upper()} disuppress — induk OLT '{_pname}' bermasalah",
                            "category": "maintenance",
                        }
                    )
                    continue
                label = d.get("customer") or d.get("ont_sn")
                loc = " / ".join(
                    [x for x in (d.get("olt_name"), d.get("odp_name")) if x]
                )
                rx_txt = f"{rx} dBm" if rx is not None else "—"
                tx_txt = f"{tx} dBm" if tx is not None else "—"
                alarms.append(
                    {
                        "host": f"{d.get('ont_sn')} ({label})",
                        "severity": severity,
                        "message": f"Fiber {status.upper()}: Rx {rx_txt} Tx {tx_txt} {('[' + loc + ']') if loc else ''} — {advice}",
                        "category": "fiber",
                    }
                )
                continue
            if _parent:
                continue
            dg = fiber_degrade_memory.get(d["id"]) or {}
            if dg.get("degrading") and rx is not None:
                try:
                    _dth, _dd = _fiber_degrade_settings()
                except Exception:
                    _dth, _dd = FIBER_DEGRADE_DB, FIBER_DEGRADE_DAYS
                label = d.get("customer") or d.get("ont_sn")
                alarms.append(
                    {
                        "host": f"{d.get('ont_sn')} ({label})",
                        "severity": "warning",
                        "message": f"Fiber DEGRADASI: Rx turun {dg.get('drop_db')} dB dalam {_dd} hari "
                        f"(kini {rx} dBm, ambang {_dth} dB) — cek bending/konektor/splicing sebelum kritis.",
                        "category": "fiber",
                    }
                )
                continue
            fl = fiber_flap_memory.get(d["id"]) or {}
            if fl.get("flapping"):
                try:
                    _fth, _fh = _fiber_flap_settings()
                except Exception:
                    _fth, _fh = FIBER_FLAP_FLIPS, FIBER_FLAP_HOURS
                label = d.get("customer") or d.get("ont_sn")
                alarms.append(
                    {
                        "host": f"{d.get('ont_sn')} ({label})",
                        "severity": "warning",
                        "message": f"Fiber FLAPPING: {fl.get('flips')}x berubah normal↔terganggu dalam {_fh} jam "
                        f"(ambang {_fth}x) — cek konektor longgar, splicing, ODP basah/rusak.",
                        "category": "fiber",
                    }
                )
        try:
            _members = {}
            for _ok, _sn, _st in sev_rows:
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
                    _msg = (
                        f"Insiden massal OLT '{_name}' — {len(_mlist)} ONT "
                        f"({_crit} kritis/overload, {_warn} warning), "
                        f"alarm individual disuppress"
                    )
                else:
                    _ip = (_ent or {}).get("ip") or "?"
                    _msg = (
                        f"Induk OLT '{_name}' DOWN (mgmt {_ip}) — {len(_mlist)} ONT "
                        f"({_crit} kritis/overload, {_warn} warning), "
                        f"alarm individual disuppress"
                    )
                alarms.append(
                    {
                        "host": f"OLT {_name}",
                        "severity": "disaster",
                        "message": _msg,
                        "category": "fiber",
                    }
                )
        except Exception:
            pass
    except Exception as e:
        print(f"[TRIGGERS] fiber gagal: {e}")

    try:
        conn, c = get_db()
        try:
            c.execute(
                "SELECT ip, alias FROM hosts WHERE snmp_community IS NOT NULL"
                " AND snmp_community != '' AND (snmp_profile IS NULL OR snmp_profile != 'generic')"
                " ORDER BY id ASC"
            )
            for h in c.fetchall():
                ip = h["ip"]
                label = (h["alias"] or "").strip() or ip
                c2 = conn.execute(
                    "SELECT cpu, mem_used, storage_used, temp_c, timestamp FROM device_health"
                    " WHERE host=? ORDER BY id DESC LIMIT 1",
                    (ip,),
                )
                r = c2.fetchone()
                stale_host = False
                if r:
                    stale_host, stale_age = _mt_stale_info(r["timestamp"])
                    if stale_host:
                        alarms.append(
                            {
                                "host": f"{label} ({ip})",
                                "severity": "warning",
                                "message": f"MikroTik STALE: tanpa data SNMP baru sejak {stale_age} — "
                                f"port/reboot tak terpantau. Cek host/community/UDP 161.",
                                "category": "mikrotik",
                            }
                        )
                        continue
                    status, severity, advice = _mt_evaluate(
                        ip, r["cpu"], r["mem_used"], r["storage_used"], r["temp_c"]
                    )
                    if severity:
                        alarms.append(
                            {
                                "host": f"{label} ({ip})",
                                "severity": severity,
                                "message": f"MikroTik {status.upper()}: {advice}",
                                "category": "mikrotik",
                            }
                        )
                try:
                    c.execute(
                        "SELECT if_index, name FROM snmp_interfaces"
                        " WHERE host=? AND monitor=1 AND oper=2",
                        (ip,),
                    )
                    for prow in c.fetchall():
                        alarms.append(
                            {
                                "host": f"{label} ({ip})",
                                "severity": "high",
                                "message": f"MikroTik PORT DOWN: {prow['name'] or ('if' + str(prow['if_index']))}",
                                "category": "mikrotik",
                            }
                        )
                except sqlite3.OperationalError:
                    pass
                try:
                    c.execute(
                        "SELECT uptime_s FROM device_health WHERE host=? ORDER BY id DESC LIMIT 1",
                        (ip,),
                    )
                    urow = c.fetchone()
                    if (
                        urow
                        and urow["uptime_s"] is not None
                        and urow["uptime_s"] < REBOOT_ALARM_WINDOW_S
                    ):
                        alarms.append(
                            {
                                "host": f"{label} ({ip})",
                                "severity": "warning",
                                "message": f"MikroTik baru reboot {_fmt_duration(int(urow['uptime_s']))} lalu",
                                "category": "mikrotik",
                            }
                        )
                except sqlite3.OperationalError:
                    pass
        finally:
            conn.close()
    except Exception as e:
        print(f"[TRIGGERS] mikrotik gagal: {e}")

    try:
        conn, c = get_db()
        try:
            c.execute(
                "SELECT ip, alias, backup_last, backup_ok FROM hosts"
                " WHERE backup_enable=1 ORDER BY ip ASC"
            )
            now = datetime.now()
            for r in c.fetchall():
                last = (r["backup_last"] or "").strip()
                try:
                    age_h = (
                        (
                            (
                                now - datetime.strptime(last, "%Y-%m-%d %H:%M:%S")
                            ).total_seconds()
                            / 3600
                        )
                        if last
                        else None
                    )
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
                alarms.append(
                    {
                        "host": f"{label} ({r['ip']})",
                        "severity": "warning",
                        "message": f"Backup konfigurasi {reason} (terakhir {last or '—'}) — "
                        f"cek kredensial SSH / jadwal backup.",
                        "category": "mikrotik",
                    }
                )
        finally:
            conn.close()
    except Exception as e:
        print(f"[TRIGGERS] mt-backup gagal: {e}")

    severity_order = {"disaster": 1, "high": 2, "warning": 3}
    alarms.sort(key=lambda x: severity_order.get(x["severity"], 4))

    raw_sev = (request.args.get("severity") or "").lower()
    if raw_sev:
        wanted = {s.strip() for s in raw_sev.split(",") if s.strip()} & set(
            severity_order
        )
        if wanted:
            alarms = [a for a in alarms if a["severity"] in wanted]

    try:
        active_alerts = get_active_alerts()
        workflow_map = {(a["host"], a["message"]): a for a in active_alerts}
        for alarm in alarms:
            workflow = workflow_map.get((alarm.get("host"), alarm.get("message")))
            if workflow:
                alarm.update(
                    {
                        "id": workflow["id"],
                        "acknowledged_at": workflow.get("acknowledged_at"),
                        "acknowledged_by": workflow.get("acknowledged_by"),
                        "assigned_to": workflow.get("assigned_to"),
                        "note": workflow.get("note"),
                    }
                )
    except Exception as e:
        logger.warning(f"Alert workflow enrichment gagal: {e}")
    return jsonify(alarms)


def _like_escape(s):
    return str(s).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@app.route("/logs")
@login_required
def logs_page():
    return render_template("logs.html")


@app.route("/audit_logs")
@login_required
def audit_logs_page():
    return render_template("audit.html")


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
        logs_data.append(
            {
                "id": r["id"],
                "timestamp": r["timestamp"],
                "event_type": r["event_type"],
                "host": r["host"],
                "message": r["message"],
            }
        )
    conn.close()

    return jsonify(
        {"logs": logs_data, "total": total_logs, "page": page, "limit": limit}
    )


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
        w.writerow(
            [
                _csv_safe(r["timestamp"]),
                _csv_safe(r["host"]),
                _csv_safe(r["event_type"]),
                _csv_safe(r["message"]),
            ]
        )
    try:
        audit(
            current_user.username,
            "logs.export",
            f"host={host_filter or 'all'} type={type_filter or 'all'} q={q or '-'} rows={len(rows)}",
        )
    except Exception:
        pass
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=nms_system_logs.csv"},
    )


@app.route("/api/export")
@api_login_required
def export_csv():
    targets = get_target_hosts()
    host = request.args.get("host", targets[0] if targets else "unknown")
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

    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["timestamp", "host", "latency_ms", "packet_loss_pct"])
    for r in rows:
        lat = r["latency"] if r["latency"] != -1 else "DOWN"
        writer.writerow(
            [_csv_safe(r["timestamp"]), _csv_safe(host), lat, r["packet_loss"]]
        )

    safe_host = (
        re.sub(r"[^a-zA-Z0-9.\-]", "_", host)[:100].replace(".", "_") or "unknown"
    )
    fname = f"nms_{safe_host}_{hours}h.csv"
    return Response(
        out.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={fname}"},
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
    hosts = [
        {
            "ip": r["ip"],
            "alias": (r["alias"] or "").strip() or r["ip"],
            "category": (r["category"] or "").strip() or "Uncategorized",
        }
        for r in c.fetchall()
    ]
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
        c.execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN latency != -1 THEN 1 ELSE 0 END) AS up_count,
                   AVG(CASE WHEN latency != -1 THEN latency END) AS avg_ms,
                   MIN(CASE WHEN latency != -1 THEN latency END) AS min_ms,
                   MAX(CASE WHEN latency != -1 THEN latency END) AS max_ms,
                   AVG(CASE WHEN latency != -1 THEN packet_loss END) AS avg_loss
            FROM ping_logs
            WHERE host=? AND timestamp > datetime('now','localtime',?)
        """,
            (ip, f"-{days} days"),
        )
        r = c.fetchone()
        total = r["total"] or 0
        up_count = r["up_count"] or 0

        try:
            c.execute(
                """
                SELECT id, started_at, resolved_at, duration_s, is_maintenance FROM down_events
                WHERE host=? AND started_at > datetime('now','localtime',?)
                ORDER BY id DESC
            """,
                (ip, f"-{days} days"),
            )
        except sqlite3.OperationalError:
            c.execute(
                """
                SELECT id, started_at, resolved_at, duration_s FROM down_events
                WHERE host=? AND started_at > datetime('now','localtime',?)
                ORDER BY id DESC
            """,
                (ip, f"-{days} days"),
            )
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
            host_rows.append(
                {
                    **h,
                    "uptime_pct": None,
                    "total_checks": 0,
                    "up_count": 0,
                    "down_count": down_count,
                    "maintenance_count": maint_count,
                    "avg_ms": None,
                    "min_ms": None,
                    "max_ms": None,
                    "avg_loss": None,
                    "mttr_s": mttr,
                    "mttr_str": _fmt_duration(mttr) if mttr is not None else "—",
                    "downtime_s": h_downtime,
                    "maintenance_downtime_s": h_maint_downtime,
                }
            )
        else:
            uptime = round(up_count / total * 100, 1)
            sum_uptime += uptime
            counted_uptime += 1
            host_rows.append(
                {
                    **h,
                    "uptime_pct": uptime,
                    "total_checks": total,
                    "up_count": up_count,
                    "down_count": down_count,
                    "maintenance_count": maint_count,
                    "avg_ms": (
                        round(r["avg_ms"], 2) if r["avg_ms"] is not None else None
                    ),
                    "min_ms": (
                        round(r["min_ms"], 2) if r["min_ms"] is not None else None
                    ),
                    "max_ms": (
                        round(r["max_ms"], 2) if r["max_ms"] is not None else None
                    ),
                    "avg_loss": (
                        round(r["avg_loss"], 1) if r["avg_loss"] is not None else None
                    ),
                    "mttr_s": mttr,
                    "mttr_str": _fmt_duration(mttr) if mttr is not None else "—",
                    "downtime_s": h_downtime,
                    "maintenance_downtime_s": h_maint_downtime,
                }
            )
        for e in evs[:200]:
            outages.append(
                {
                    "id": e["id"],
                    "host": ip,
                    "alias": h["alias"],
                    "started_at": e["started_at"],
                    "resolved_at": e["resolved_at"] or "Ongoing",
                    "duration_s": e["duration_s"],
                    "duration_str": _fmt_duration(e["duration_s"]),
                    "status": "resolved" if e["resolved_at"] else "ongoing",
                    "is_maintenance": _is_maint(e),
                }
            )
    conn.close()
    outages.sort(key=lambda x: x["started_at"], reverse=True)
    outages = outages[:200]
    return jsonify(
        {
            "period_days": days,
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "note": "ping_logs retensi 7 hari; outage maintenance tidak dihitung ke SLA",
            "hosts": host_rows,
            "outages": outages,
            "totals": {
                "hosts": len(hosts),
                "avg_uptime": (
                    round(sum_uptime / counted_uptime, 1) if counted_uptime else None
                ),
                "total_outages": total_outages,
                "total_downtime_s": total_downtime,
                "total_downtime_str": _fmt_duration(total_downtime),
                "maintenance_outages": total_maint_outages,
                "maintenance_downtime_s": total_maint_downtime,
                "maintenance_downtime_str": _fmt_duration(total_maint_downtime),
            },
        }
    )


@app.route("/api/reports/export")
@api_login_required
def api_reports_export():
    days = _report_days()
    table = (request.args.get("table") or "summary").lower()

    conn, c = get_db()
    c.execute("SELECT ip, alias, category FROM hosts ORDER BY id ASC")
    hosts = [
        {
            "ip": r["ip"],
            "alias": (r["alias"] or "").strip() or r["ip"],
            "category": (r["category"] or "").strip() or "Uncategorized",
        }
        for r in c.fetchall()
    ]
    buf = io.StringIO()
    w = csv.writer(buf)
    if table == "outages":
        w.writerow(
            [
                "id",
                "host",
                "alias",
                "started_at",
                "resolved_at",
                "duration_s",
                "duration",
                "status",
            ]
        )
        c.execute(
            """
            SELECT id, host, started_at, resolved_at, duration_s FROM down_events
            WHERE started_at > datetime('now','localtime',?) ORDER BY id DESC LIMIT 1000
        """,
            (f"-{days} days",),
        )
        amap = {h["ip"]: h["alias"] for h in hosts}
        for r in c.fetchall():
            w.writerow(
                [
                    r["id"],
                    _csv_safe(r["host"]),
                    _csv_safe(amap.get(r["host"], r["host"])),
                    _csv_safe(r["started_at"]),
                    _csv_safe(r["resolved_at"] or "Ongoing"),
                    r["duration_s"] if r["duration_s"] is not None else "",
                    _fmt_duration(r["duration_s"]),
                    "resolved" if r["resolved_at"] else "ongoing",
                ]
            )
        fname = f"nms_outages_{days}d.csv"
    else:
        w.writerow(
            [
                "ip",
                "alias",
                "category",
                "uptime_pct",
                "checks",
                "up",
                "down_events",
                "avg_ms",
                "min_ms",
                "max_ms",
                "avg_loss_pct",
                "mttr_s",
                "downtime_s",
            ]
        )
        for h in hosts:
            ip = h["ip"]
            c.execute(
                """
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN latency != -1 THEN 1 ELSE 0 END) AS up_count,
                       AVG(CASE WHEN latency != -1 THEN latency END) AS avg_ms,
                       MIN(CASE WHEN latency != -1 THEN latency END) AS min_ms,
                       MAX(CASE WHEN latency != -1 THEN latency END) AS max_ms,
                       AVG(CASE WHEN latency != -1 THEN packet_loss END) AS avg_loss
                FROM ping_logs WHERE host=? AND timestamp > datetime('now','localtime',?)
            """,
                (ip, f"-{days} days"),
            )
            r = c.fetchone()
            total = r["total"] or 0
            up_count = r["up_count"] or 0
            c.execute(
                "SELECT COUNT(*) AS cnt, AVG(duration_s) AS mttr, SUM(duration_s) AS dt FROM down_events WHERE host=? AND started_at > datetime('now','localtime',?)",
                (ip, f"-{days} days"),
            )
            e = c.fetchone()
            w.writerow(
                [
                    _csv_safe(ip),
                    _csv_safe(h["alias"]),
                    _csv_safe(h["category"]),
                    round(up_count / total * 100, 1) if total else "",
                    total,
                    up_count,
                    e["cnt"] or 0,
                    round(r["avg_ms"], 2) if r["avg_ms"] is not None else "",
                    round(r["min_ms"], 2) if r["min_ms"] is not None else "",
                    round(r["max_ms"], 2) if r["max_ms"] is not None else "",
                    round(r["avg_loss"], 1) if r["avg_loss"] is not None else "",
                    round(e["mttr"]) if e["mttr"] is not None else "",
                    int(e["dt"] or 0),
                ]
            )
        fname = f"nms_summary_{days}d.csv"
    conn.close()
    try:
        audit(current_user.username, "reports.export", f"{table} {days}d")
    except Exception:
        pass
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={fname}"},
    )


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
        c.execute(
            """
            SELECT COUNT(*) AS total, SUM(CASE WHEN latency != -1 THEN 1 ELSE 0 END) AS up_count,
                   AVG(CASE WHEN latency != -1 THEN latency END) AS avg_ms
            FROM ping_logs WHERE host=? AND timestamp > datetime('now','localtime',?)
        """,
            (ip, f"-{days} days"),
        )
        r = c.fetchone()
        total = r["total"] or 0
        up_count = r["up_count"] or 0
        uptime = round(up_count / total * 100, 1) if total else None
        c.execute(
            "SELECT COUNT(*) AS cnt FROM down_events WHERE host=? AND started_at > datetime('now','localtime',?)",
            (ip, f"-{days} days"),
        )
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
                c.execute(
                    """
                    SELECT COUNT(*) AS total,
                           SUM(CASE WHEN latency != -1 THEN 1 ELSE 0 END) AS up_count,
                           AVG(CASE WHEN latency != -1 THEN latency END) AS avg_ms
                    FROM ping_logs
                    WHERE host=? AND timestamp > datetime('now','localtime','-24 hours')
                """,
                    (ip,),
                )
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
                elif status_memory.get(ip, False) or (
                    has_latest and latest["latency"] == -1
                ):
                    st = "down"
                    down_n += 1
                else:
                    st = "up"
                    up_n += 1
                entry = {
                    "name": name,
                    "status": st,
                    "uptime_24h": uptime,
                    "avg_ms": (
                        round(r["avg_ms"], 1) if r["avg_ms"] is not None else None
                    ),
                }
                if st == "maintenance":
                    entry["note"] = (maint_map[ip].get("reason") or "").strip()[
                        :100
                    ] or "Scheduled maintenance"
                nodes.append(entry)

            c.execute("SELECT id, name, type, status FROM services ORDER BY id ASC")
            services = []
            for s in c.fetchall():
                d = dict(s)
                c.execute(
                    """
                    SELECT COUNT(*) AS total,
                           SUM(CASE WHEN status='ONLINE' THEN 1 ELSE 0 END) AS up_count
                    FROM service_history
                    WHERE service_id=? AND timestamp > datetime('now','localtime','-24 hours')
                """,
                    (d["id"],),
                )
                sr = c.fetchone()
                stotal = sr["total"] or 0
                sup = sr["up_count"] or 0
                st_raw = (d.get("status") or "PENDING").upper()
                st = st_raw.lower() if st_raw in ("ONLINE", "OFFLINE") else "pending"
                services.append(
                    {
                        "name": (d.get("name") or "service")[:80],
                        "type": (d.get("type") or "").lower()[:10],
                        "status": st,
                        "uptime_24h": round(sup / stotal * 100, 1) if stotal else None,
                    }
                )
        finally:
            conn.close()
    except Exception as e:
        print(f"[PUBLIC STATUS] gagal: {e}")
        resp = jsonify({"error": "Status tidak tersedia, coba lagi."})
        resp.headers["Cache-Control"] = "no-store"
        return resp, 503
    resp = jsonify(
        {
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
        }
    )
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
    return jsonify(
        {
            "status": "ok" if db_ok else "degraded",
            "uptime_seconds": uptime_s,
        }
    ), (200 if db_ok else 503)


if __name__ == "__main__":
    app.run(debug=False, port=5000, use_reloader=False)
