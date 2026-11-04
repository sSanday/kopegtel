import csv
import io
import ipaddress
import logging
import os
import re
import socket
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from apscheduler.schedulers.background import BackgroundScheduler
from flask import (
    Flask,
    Response,
    abort,
    jsonify,
    redirect,
    render_template,
    stream_with_context,
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
    dashboard_password_is_weak,
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
    log_system_event,
    resolve_alert,
    resolve_db_path,
    restore_database,
    set_host_threshold,
    update_user_role,
    update_alert_workflow,
    mark_alerts_escalated,
)
from nms.notify import audit, channel_status, send_alert, send_telegram_alert
from nms.validators import validate_backup_filename, validate_dns_query_name
from nms.anomaly import summarize_anomalies
from nms import service_checks
from nms import sla as sla_engine
from nms import twofa as twofa
from nms.query_helpers import (
    get_all_agent_metrics,
    get_all_host_stats,
)
from nms.crypto import encrypt_secret
from nms.monitor import (
    FIBER_PARENT_MIN,
    check_network,
    down_since,
    fiber_parent_down,
    last_down_telegram,
    status_memory,
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
    FIBER_RX_SCALE_MAX,
    FIBER_RX_SCALE_MIN,
    FIBER_RX_WARN,
    FIBER_STALE_MIN,
    FIBER_TX_MAX,
    FIBER_TX_MIN,
    fiber_eval,
)
from nms.auth import api_login_required, require_role
from nms.format import _csv_safe
from nms.fiber_poll import (
    _fiber_stale_info,
    _is_mute_active,
    fiber_alarm_memory,
    fiber_degrade_memory,
    fiber_flap_memory,
    poll_fiber_monitor,
    poll_fiber_snmp,
    poll_fiber_trend,
    send_fiber_summary,
)
from nms.mikrotik import (
    MT_STALE_MIN,
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
from nms.snmp_trap import (
    SNMP_TRAP_COMMUNITY_MAX as NMS_SNMP_TRAP_COMMUNITY_MAX,
    start_trap_receiver,
)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024

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

app.config["REMEMBER_COOKIE_DURATION"] = timedelta(days=7)
app.config["REMEMBER_COOKIE_HTTPONLY"] = True
app.config["REMEMBER_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

app.config["SESSION_COOKIE_SECURE"] = COOKIE_SECURE
app.config["REMEMBER_COOKIE_SECURE"] = COOKIE_SECURE
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=8)
app_start_time = datetime.now()

os.makedirs(os.path.join(BASE_DIR, "logs"), exist_ok=True)

if LIMITER_AVAILABLE:
    limiter = Limiter(
        app=app,
        key_func=get_remote_address,
        default_limits=["20000 per day", "2000 per hour"],
        storage_uri="memory://",
    )
    # Gate untuk test (tests/conftest.py): matikan rate limit agar login
    # antar file test tidak tak kena 429.
    if os.environ.get("NMS_DISABLE_LIMITER", "0") == "1":
        limiter.enabled = False
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
            uname = getattr(current_user, "username", "unknown")
            logout_user()
            session.pop("admin_v", None)
            logger.warning(f"Session timeout for {uname} (idle > 1 hour)")
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
    # F-03: security headers
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    return response


@app.route("/favicon.ico", methods=["GET"])
def favicon():
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
        '<rect width="32" height="32" rx="7" fill="#0b0f14"/>'
        '<circle cx="16" cy="16" r="6.5" fill="none" stroke="#4ea3ff" stroke-width="3"/>'
        "</svg>"
    )
    resp = Response(svg, mimetype="image/svg+xml")
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


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
    pw_weak = dashboard_password_is_weak(DASHBOARD_PASSWORD)
    dev_mode = os.environ.get("FLASK_ENV", "production") != "production"
    try:
        from werkzeug.security import check_password_hash, generate_password_hash

        conn, c = get_db()
        try:
            c.execute("SELECT value FROM settings WHERE key='admin_user'")
            r = c.fetchone()
            db_user = r["value"] if r else None
            c.execute("SELECT value FROM settings WHERE key='admin_pass_hash'")
            r = c.fetchone()
            db_hash = r["value"] if r else None
            if (db_user or db_hash) and pw_weak:
                # Admin sudah ada di database: cukup peringatan, jangan paksa
                # menghentikan aplikasi yang sedang berjalan.
                logger.warning(
                    "[AUTH] DASHBOARD_PASSWORD di .env lemah (kosong, <10 "
                    "karakter, atau kata umum). Ubah password via UI atau set "
                    "password kuat di .env lalu jalankan NMS_RESET_ADMIN=1."
                )
            if not db_hash:
                if pw_weak:
                    _hint = (
                        "Set DASHBOARD_PASSWORD kuat (min 10 karakter, bukan "
                        "kata umum) di file .env lalu jalankan ulang aplikasi. "
                        "Untuk memulihkan admin, gunakan NMS_RESET_ADMIN=1 "
                        "sebelumnya."
                    )
                    if dev_mode:
                        print(
                            "[WARN] DASHBOARD_PASSWORD lemah (development, "
                            f"tidak dihentikan). {_hint}"
                        )
                    else:
                        print(
                            "[FATAL] DASHBOARD_PASSWORD lemah; menolak membuat "
                            f"admin pertama. {_hint}"
                        )
                        sys.exit(1)
                c.execute(
                    "INSERT OR IGNORE INTO settings (key, value) VALUES ('admin_user', ?)",
                    (DASHBOARD_USERNAME,),
                )
                c.execute(
                    "INSERT OR IGNORE INTO settings (key, value) VALUES ('admin_pass_hash', ?)",
                    (generate_password_hash(DASHBOARD_PASSWORD),),
                )
                conn.commit()
                print("[AUTH] admin awal dibuat dari .env")
                return
            # Jangan timpa password yang diganti via UI secara diam-diam.
            # Sinkron ulang hanya bila diminta eksplisit via NMS_RESET_ADMIN=1,
            # agar kredensial .env selalu bisa dipakai untuk pemulihan.
            if os.environ.get("NMS_RESET_ADMIN") == "1":
                c.execute(
                    "INSERT OR REPLACE INTO settings (key, value) VALUES ('admin_user', ?)",
                    (DASHBOARD_USERNAME,),
                )
                c.execute(
                    "INSERT OR REPLACE INTO settings (key, value) VALUES ('admin_pass_hash', ?)",
                    (generate_password_hash(DASHBOARD_PASSWORD),),
                )
                conn.commit()
                try:
                    _bump_session_version()
                except Exception:
                    pass
                print("[AUTH] admin direset dari .env (NMS_RESET_ADMIN=1)")
                return
            try:
                env_match = (
                    db_user or DASHBOARD_USERNAME
                ) == DASHBOARD_USERNAME and check_password_hash(
                    db_hash, DASHBOARD_PASSWORD
                )
            except Exception:
                env_match = False
            if not env_match:
                print(
                    "[WARN] password admin di DB beda dengan .env. "
                    "Login pakai password yang diset via UI/lama. "
                    "Untuk pakai password .env, jalankan sekali: "
                    "NMS_RESET_ADMIN=1 ./env_new/bin/python app.py"
                )
        finally:
            conn.close()
    except Exception as e:
        print(f"[WARN] seed admin hash gagal: {e}")


class AdminUser(UserMixin):
    def __init__(self, username=None):
        self.id = "admin"
        self.username = username or DASHBOARD_USERNAME
        self.role = "admin"


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
    """Pembungkus -> nms.service_checks. Signature/return tetap sama.

    Tuple: (id, ip, name, type, port, url[, query, expected]) — dua elemen
    terakhir opsional agar pemanggil/test lama (6-tuple) tetap jalan.
    """
    svc_id, ip, name, svc_type, port, url = svc[:6]
    query = str(svc[6] or "") if len(svc) > 6 else ""
    expected = str(svc[7] or "") if len(svc) > 7 else ""
    status, latency = service_checks.run_check(
        svc_type,
        ip,
        port,
        url,
        query=query,
        expected=expected,
        is_url_allowed=_is_url_allowed_for_monitoring,
    )
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


def check_ssl_expiry(service_id=None):
    try:
        conn, c = get_db()
        try:
            try:
                sql = (
                    "SELECT id, ip, name, url, ssl_days_left, ssl_last_alert "
                    "FROM services WHERE type='http' AND url LIKE 'https://%'"
                )
                params = ()
                if service_id is not None:
                    sql += " AND id=?"
                    params = (int(service_id),)
                c.execute(sql, params)
            except (sqlite3.OperationalError, ValueError, TypeError):
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


def trigger_async_ssl_check(service_id=None):
    threading.Thread(
        target=check_ssl_expiry, args=(service_id,), daemon=True
    ).start()


def check_services():
    try:

        conn, c = get_db()
        try:
            try:
                c.execute(
                    "SELECT id, ip, name, type, port, url, query, expected FROM services"
                )
            except Exception:
                # DB lama tanpa kolom query/expected -> tuple 6 elemen
                c.execute("SELECT id, ip, name, type, port, url FROM services")
            services = []
            for row in c.fetchall():
                s = dict(row)
                services.append(
                    (
                        s.get("id"),
                        s.get("ip"),
                        s.get("name"),
                        s.get("type"),
                        s.get("port"),
                        s.get("url"),
                        (s.get("query") or ""),
                        (s.get("expected") or ""),
                    )
                )
        finally:
            conn.close()

        results = []
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(check_single_service, s) for s in services]
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


_manual_svc_lock = threading.Lock()
_last_manual_svc = 0.0


def trigger_manual_service_check():
    global _last_manual_svc
    with _manual_svc_lock:
        if time.time() - _last_manual_svc < 60:
            return
        _last_manual_svc = time.time()
    threading.Thread(target=check_services, daemon=True).start()


def _run_service_check(svc_id):
    """Periksa satu service dan simpan hasilnya (dipakai endpoint per baris)."""
    try:
        conn, c = get_db()
        try:
            row = c.execute(
                "SELECT id, ip, name, type, port, url, query, expected "
                "FROM services WHERE id=?",
                (svc_id,),
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return None
        checked_id, status, latency = check_single_service(tuple(row))
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with db_lock:
            conn, c = get_db()
            try:
                c.execute(
                    "UPDATE services SET status=?, latency=?, last_checked=? "
                    "WHERE id=?",
                    (status, latency, timestamp, checked_id),
                )
                c.execute(
                    "INSERT INTO service_history "
                    "(service_id, status, latency, timestamp) VALUES (?, ?, ?, ?)",
                    (checked_id, status, latency, timestamp),
                )
                _commit_with_retry(conn)
            finally:
                conn.close()
        return status
    except Exception as e:
        logger.warning(f"Pemeriksaan service {svc_id} gagal: {e}")
        return None


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


_DISC_SCAN_LOCK = threading.Lock()


def _disc_normalize_network(value):
    """CIDR kanonik (alamat network), atau string asli bila tidak valid.

    Contoh: 192.168.30.1/24 -> 192.168.30.0/24. Dipakai agar satu jaringan
    tidak tercatat sebagai beberapa aturan hanya karena host bit berbeda.
    """
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        return str(ipaddress.ip_network(raw, strict=False))
    except ValueError:
        return raw


def _disc_record_events(c, source, network, found_devices):
    """Catat transisi status perangkat untuk satu aturan pemindaian.

    Perangkat yang belum tercatat (atau terakhir 'lost') -> event 'discovered';
    yang hilang dari hasil terbaru -> event 'lost' (mewarisi nama/vendor
    terakhir). Status yang tidak berubah tidak dicatat ulang sehingga durasi
    uptime/downtime tetap berjalan. Return: jumlah event baru.
    """
    c.execute(
        "SELECT e.ip, e.status, e.sys_name, e.vendor FROM discovery_events e"
        " WHERE e.id IN (SELECT MAX(id) FROM discovery_events"
        " WHERE source=? AND network=? GROUP BY ip)",
        (source, network),
    )
    last = {r["ip"]: dict(r) for r in c.fetchall()}
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    found_ips = set()
    added = 0
    for dev in found_devices:
        if not isinstance(dev, dict):
            continue
        ip = str(dev.get("ip") or "").strip()
        if not ip:
            continue
        found_ips.add(ip)
        prev = last.get(ip)
        if prev is not None and prev["status"] == "discovered":
            continue
        c.execute(
            "INSERT INTO discovery_events (source, network, ip, sys_name, vendor, status, ts)"
            " VALUES (?,?,?,?,?,?,?)",
            (
                source,
                network,
                ip,
                str(dev.get("sys_name") or "")[:128],
                str(dev.get("vendor") or "")[:64],
                "discovered",
                now,
            ),
        )
        added += 1
    lost = 0
    for ip, prev in last.items():
        if prev["status"] != "discovered" or ip in found_ips:
            continue
        c.execute(
            "INSERT INTO discovery_events (source, network, ip, sys_name, vendor, status, ts)"
            " VALUES (?,?,?,?,?,?,?)",
            (
                source,
                network,
                ip,
                str(prev.get("sys_name") or "")[:128],
                str(prev.get("vendor") or "")[:64],
                "lost",
                now,
            ),
        )
        lost += 1
    return added, lost


def run_scheduled_snmp_discovery():
    network = _disc_normalize_network(
        get_setting("discovery_network", "", type_cast=str)
    )
    community = get_setting("discovery_community", "", type_cast=str).strip()
    if not network or not community:
        return {"status": "disabled", "added": [], "removed": []}
    if not _DISC_SCAN_LOCK.acquire(blocking=False):
        return {"status": "busy", "added": [], "removed": []}
    try:
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
        try:
            try:
                _disc_record_events(c, "scheduled", network, found)
            except Exception as exc:
                logger.warning(f"Catat event discovery gagal: {exc}")
            c.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                ("discovery_last_hosts", json.dumps(sorted(current))),
            )
            c.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                (
                    "discovery_last_scan",
                    json.dumps(
                        {
                            "network": network,
                            "count": len(found),
                            "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            "source": "scheduled",
                        }
                    ),
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return {"status": "ok", "added": added, "removed": removed}
    finally:
        _DISC_SCAN_LOCK.release()


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


SCHEDULER_ENABLED = (
    os.environ.get("NMS_DISABLE_SCHEDULER", "0") != "1"
    and os.environ.get("NMS_ROLE", "worker") == "worker"
)
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
        func=run_scheduled_snmp_discovery,
        trigger="interval",
        hours=6,
        id="discovery_scheduled",
        **_job_defaults,
    )
    scheduler.add_job(
        func=check_agent_heartbeat, trigger="interval", seconds=60, **_job_defaults
    )
    scheduler.add_job(
        func=check_services, trigger="interval", seconds=30, **_job_defaults
    )
    scheduler.add_job(
        func=check_ssl_expiry, trigger="interval", hours=6, **_job_defaults
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
        func=poll_fiber_trend, trigger="interval", hours=3, **_job_defaults
    )
    scheduler.add_job(
        func=send_heartbeat,
        trigger="cron",
        hour=8,
        minute=0,
        id="send_heartbeat",
        **_job_defaults,
    )
    scheduler.add_job(
        func=send_fiber_summary,
        trigger="cron",
        hour=8,
        minute=5,
        id="send_fiber_summary",
        **_job_defaults,
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
        func=send_daily_report,
        trigger="cron",
        hour=7,
        minute=0,
        id="send_daily_report",
        **_job_defaults,
    )
    try:
        scheduler.start()
    except Exception as e:
        print(f"[WARN] scheduler gagal start: {e}")

    def _startup_fiber_trend():
        try:
            poll_fiber_trend()
        except Exception as e:
            print(f"[FIBER-TREND] startup gagal: {e}", flush=True)

    try:
        _trend_timer = threading.Timer(90.0, _startup_fiber_trend)
        _trend_timer.daemon = True
        _trend_timer.start()
    except Exception as e:
        print(f"[WARN] jadwal startup fiber trend gagal: {e}")

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

# Receiver SNMP trap (thread daemon UDP). start_trap_receiver sendiri mengecek
# env NMS_DISABLE_TRAP_RECEIVER + setting snmp_trap_enabled, dan gagal aman
# bila port sudah dipakai.
try:
    start_trap_receiver()
except Exception as _trap_err:
    print(f"[TRAP] receiver gagal start: {_trap_err}")


_manual_svc_lock = threading.Lock()
_last_manual_svc = 0.0


@app.route("/login", methods=["GET", "POST"])
@limiter.limit("10/minute")
def login():
    if current_user.is_authenticated:
        return redirect(url_for("index"))
    error = None
    blocked_seconds = None
    if request.method == "POST":
        client_ip = get_client_ip()
        now_ts = time.time()
        _prune_login_failures(now_ts)
        fails, blocked_until, _ = login_failures.get(client_ip, (0, 0, 0))
        if now_ts < blocked_until:
            blocked_seconds = int(blocked_until - now_ts)
            error = (
                f"Terlalu banyak percobaan gagal. Coba lagi {blocked_seconds} detik."
            )
            return render_template(
                "login.html", error=error, blocked_seconds=blocked_seconds
            )
        username = request.form.get("username", "").strip()[:50]
        password = request.form.get("password", "")[:200]
        if _verify_admin(username, password):
            login_failures.pop(client_ip, None)
            remember = request.form.get("remember") == "1"
            next_page = request.args.get("next")

            if (
                not next_page
                or not next_page.startswith("/")
                or next_page.startswith("//")
                or "\\" in next_page
            ):
                next_page = None

            if twofa.enabled_for(username):
                # Password benar, TAPI 2FA aktif: jangan login_user dulu.
                session["2fa_pending"] = {
                    "u": username,
                    "t": time.time(),
                    "remember": remember,
                    "next": next_page,
                }
                return redirect(url_for("login_2fa"))

            user = AdminUser(username=username)
            login_user(user, remember=remember, duration=timedelta(days=7))
            session["admin_v"] = _get_session_version()
            session["_last_activity"] = time.time()
            twofa.touch_last_login(username)
            try:
                audit(username, "auth.login", f"from {client_ip}")
            except Exception:
                pass
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
                blocked_seconds = 60
                error = "Terlalu banyak percobaan gagal. Diblokir 60 detik."
            else:
                login_failures[client_ip] = (fails, 0, now_ts)
                error = "Username atau password salah."
    return render_template("login.html", error=error, blocked_seconds=blocked_seconds)


@app.route("/login/2fa", methods=["GET", "POST"])
@limiter.limit("10/minute")
def login_2fa():
    """Langkah kedua login: kode TOTP 6 digit ATAU satu recovery code.

    Sesi "pending" dibuat di /login setelah password benar, kedaluwarsa 5
    menit (twofa.PENDING_TTL_S). Pembatas gagal PER USERNAME: 5 gagal ->
    diblokir 5 menit (penghitung in-memory per proses; gunicorn di repo
    ini 1 worker — lihat nms/twofa.py).
    """
    pend = session.get("2fa_pending")
    if not isinstance(pend, dict) or not pend.get("u"):
        return redirect(url_for("login"))
    username = str(pend.get("u") or "")[:50]

    if request.method == "GET":
        return render_template("login_2fa.html", error=None, blocked_seconds=None)

    now_ts = time.time()
    if now_ts - float(pend.get("t") or 0) > twofa.PENDING_TTL_S:
        session.pop("2fa_pending", None)
        return redirect(url_for("login"))

    blocked_seconds = twofa.fail_block_remaining(username, now_ts)
    if blocked_seconds > 0:
        try:
            audit(username, "auth.2fa.failed", f"diblokir {blocked_seconds}s")
        except Exception:
            pass
        return render_template(
            "login_2fa.html",
            error=f"Terlalu banyak percobaan gagal. Diblokir {blocked_seconds} detik.",
            blocked_seconds=blocked_seconds,
        )

    code = (request.form.get("code") or "")[:32]
    ok, reason = twofa.verify_login_code(username, code)
    if ok:
        twofa.clear_2fa_failures(username)
        user = AdminUser(username=username)
        login_user(
            user,
            remember=bool(pend.get("remember")),
            duration=timedelta(days=7),
        )
        session["admin_v"] = _get_session_version()
        session["_last_activity"] = time.time()
        session.pop("2fa_pending", None)
        client_ip = get_client_ip()
        twofa.touch_last_login(username)
        try:
            audit(username, "auth.login", f"from {client_ip}")
        except Exception:
            pass
        try:
            audit(username, "auth.2fa.success", reason)
        except Exception:
            pass
        if reason == "recovery":
            # Recovery code terpakai: audit khusus + notifikasi channel aktif.
            try:
                audit(username, "auth.2fa.recovery_used", f"from {client_ip}")
            except Exception:
                pass
            try:
                send_alert(
                    f"⚠️ Recovery code 2FA dipakai untuk {username} "
                    f"(sisa {twofa.recovery_left(username)}) di NMS KOPEGTEL",
                    severity="warning",
                    event="auth.2fa.recovery_used",
                )
            except Exception:
                pass
        next_page = pend.get("next") or None
        if (
            not next_page
            or not next_page.startswith("/")
            or next_page.startswith("//")
            or "\\" in next_page
        ):
            next_page = None
        return redirect(next_page or url_for("index"))

    fails, _ = twofa.record_2fa_fail(username, now_ts)
    try:
        audit(username, "auth.2fa.failed", f"{reason} ({fails}x)")
    except Exception:
        pass
    try:
        log_system_event("AUDIT", username, f"auth.2fa.failed {reason} ({fails}x)")
    except Exception:
        pass
    if reason == "replay":
        error = "Kode sudah dipakai. Gunakan kode terbaru."
    else:
        error = "Kode salah."
    return render_template("login_2fa.html", error=error, blocked_seconds=None)


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

        # Fallback perangkat SNMP (mis. MikroTik): cpu/ram/disk tersimpan di
        # device_health (poll_mikrotik_health); agent_metrics hanya net_in/net_out.
        if not rows and col in ("cpu_percent", "ram_percent", "disk_percent"):
            dh_col = {
                "cpu_percent": "cpu",
                "ram_percent": "mem_used",
                "disk_percent": "storage_used",
            }[col]
            c.execute(
                f"SELECT timestamp, {dh_col} as val FROM device_health "
                f"WHERE host=? AND {dh_col} IS NOT NULL "
                f"AND timestamp > datetime('now','localtime',?) ORDER BY id ASC",
                (ip, f"-{hours} hours"),
            )
            rows = c.fetchall()

        labels = [r["timestamp"] for r in rows]
        values = [round(r["val"] or 0, 2) for r in rows]

    conn.close()
    return jsonify(
        {"labels": labels, "values": values, "metric": metric, "hours": hours}
    )


@app.route("/api/host/<path:ip>/anomalies")
@login_required
def api_host_anomalies(ip):
    try:
        hours = int(request.args.get("hours", 24))
        sensitivity = float(request.args.get("sensitivity", 3.0))
    except (ValueError, TypeError):
        return jsonify({"error": "hours dan sensitivity harus angka"}), 400
    hours = max(1, min(hours, 168))
    sensitivity = max(2.0, min(sensitivity, 8.0))
    metric = request.args.get("metric", "latency")
    valid_metrics = {
        "latency": ("ping_logs", "latency"),
        "cpu": ("agent_metrics", "cpu_percent"),
        "ram": ("agent_metrics", "ram_percent"),
        "disk": ("agent_metrics", "disk_percent"),
        "net_in": ("agent_metrics", "net_in"),
        "net_out": ("agent_metrics", "net_out"),
    }
    source = valid_metrics.get(metric)
    if not source:
        return jsonify({"error": "metric tidak valid"}), 400
    table, column = source
    conn, c = get_db()
    c.execute(
        f"SELECT timestamp, {column} AS val FROM {table} WHERE host=? AND timestamp > datetime('now','localtime',?) ORDER BY id ASC",
        (ip, f"-{hours} hours"),
    )
    rows = c.fetchall()
    conn.close()
    labels = [row["timestamp"] for row in rows]
    values = [row["val"] for row in rows]
    result = summarize_anomalies(labels, values, sensitivity=sensitivity)
    return jsonify({"host": ip, "metric": metric, "hours": hours, **result})


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

    mt_row = None
    try:
        c.execute(
            "SELECT cpu, mem_used, storage_used, temp_c, timestamp "
            "FROM device_health WHERE host=? ORDER BY id DESC LIMIT 1",
            (ip,),
        )
        mt_row = c.fetchone()
    except Exception:
        mt_row = None

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
                        "cpu": (round((mt_row["cpu"] or 0), 1) if mt_row else 0),
                        "ram": (round((mt_row["mem_used"] or 0), 1) if mt_row else 0),
                        "disk": (
                            round((mt_row["storage_used"] or 0), 1) if mt_row else 0
                        ),
                        "net_in": round(net_row["net_in"] or 0, 2),
                        "net_out": round(net_row["net_out"] or 0, 2),
                        "last_seen": net_row["timestamp"],
                        "snmp_only": True,
                    }
                    if net_row
                    else (
                        {
                            "cpu": round((mt_row["cpu"] or 0), 1) if mt_row else 0,
                            "ram": round((mt_row["mem_used"] or 0), 1) if mt_row else 0,
                            "disk": (
                                round((mt_row["storage_used"] or 0), 1) if mt_row else 0
                            ),
                            "net_in": 0.0,
                            "net_out": 0.0,
                            "last_seen": (mt_row["timestamp"] if mt_row else None),
                            "snmp_only": True,
                        }
                        if mt_row
                        else None
                    )
                )
            ),
        }
    )


SNMP_PROFILES = ("auto", "mikrotik", "generic")
IPMI_AUTH_ALGOS = ("default", "none", "md2", "md5", "straight", "oem", "rmcp+")
IPMI_PRIVILEGE_LEVELS = ("callback", "user", "operator", "administrator", "oem")
HOST_TAG_LIMIT = 50
HOST_MACRO_LIMIT = 100
HOST_MACRO_TYPES = ("text", "secret", "vault")
HOST_MACRO_RE = re.compile(r"^\{\$[^{}]+\}$")
HOST_INVENTORY_MODES = ("disabled", "manual", "automatic")
HOST_INVENTORY_TEXT_LIMIT = 255
HOST_INVENTORY_TEXTAREA_LIMIT = 4000
HOST_INVENTORY_FIELDS = (
    "type",
    "type_full",
    "name",
    "alias",
    "os",
    "os_full",
    "os_short",
    "serialno_a",
    "serialno_b",
    "tag",
    "asset_tag",
    "macaddress_a",
    "macaddress_b",
    "hardware",
    "hardware_full",
    "software",
    "software_full",
    "software_app_a",
    "software_app_b",
    "software_app_c",
    "software_app_d",
    "software_app_e",
    "contact",
    "location",
    "location_lat",
    "location_lon",
    "notes",
    "chassis",
    "model",
    "hw_arch",
    "vendor",
    "contract_number",
    "installer_name",
    "deployment_status",
    "url_a",
    "url_b",
    "url_c",
    "host_networks",
    "host_netmask",
    "host_router",
    "oob_ip",
    "oob_netmask",
    "oob_router",
    "date_hw_purchase",
    "date_hw_install",
    "date_hw_expiry",
    "date_hw_decomm",
    "site_address_a",
    "site_address_b",
    "site_address_c",
    "site_city",
    "site_state",
    "site_country",
    "site_zip",
    "site_rack",
    "site_notes",
    "poc_1_name",
    "poc_1_email",
    "poc_1_phone_a",
    "poc_1_phone_b",
    "poc_1_cell",
    "poc_1_screen",
    "poc_1_notes",
    "poc_2_name",
    "poc_2_email",
    "poc_2_phone_a",
    "poc_2_phone_b",
    "poc_2_cell",
    "poc_2_screen",
    "poc_2_notes",
)
HOST_INVENTORY_TEXTAREA_FIELDS = frozenset(
    {
        "hardware_full",
        "software_full",
        "contact",
        "location",
        "notes",
        "host_networks",
        "site_notes",
        "poc_1_notes",
        "poc_2_notes",
    }
)
HOST_ENCRYPTION_METHODS = ("none", "psk", "certificate")


def _parse_host_tags(data):
    """Validasi daftar tag host dari form New host. Kembalikan (list, err)."""
    raw = data.get("tags", [])
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        return None, "Tags harus berupa daftar"
    if len(raw) > HOST_TAG_LIMIT:
        return None, f"Maksimum {HOST_TAG_LIMIT} tag per host"
    tags = []
    for item in raw:
        if not isinstance(item, dict):
            return None, "Format tag tidak valid"
        name = str(item.get("name") or "").strip()[:255]
        value = str(item.get("value") or "").strip()[:255]
        if not name and not value:
            continue
        if not name:
            return None, "Nama tag wajib diisi"
        tags.append({"name": name, "value": value})
    return tags, None


def _decode_host_tags(raw):
    """Baca kolom JSON host_tags dengan aman; data rusak diabaikan."""
    if not raw:
        return []
    try:
        items = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(items, list):
        return []
    tags = []
    for item in items[:HOST_TAG_LIMIT]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()[:255]
        value = str(item.get("value") or "").strip()[:255]
        if name:
            tags.append({"name": name, "value": value})
    return tags


def _parse_host_macros(data):
    """Validasi macro host dari form New host. Kembalikan (list, err)."""
    raw = data.get("macros", [])
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        return None, "Macros harus berupa daftar"
    if len(raw) > HOST_MACRO_LIMIT:
        return None, f"Maksimum {HOST_MACRO_LIMIT} macro per host"
    macros = []
    seen = set()
    for item in raw:
        if not isinstance(item, dict):
            return None, "Format macro tidak valid"
        name = str(item.get("name") or item.get("macro") or "").strip()[:255]
        description = str(item.get("description") or "").strip()[:255]
        value = str(item.get("value") or "")[:2048]
        macro_type = str(item.get("type") or "text").strip().lower()
        if not name and not value and not description:
            continue
        if not name:
            return None, "Nama macro wajib diisi"
        if not HOST_MACRO_RE.match(name):
            return None, "Format macro harus {$MACRO}"
        if macro_type not in HOST_MACRO_TYPES:
            return None, "Tipe macro tidak valid"
        key = name.lower()
        if key in seen:
            return None, f"Macro {name} duplikat"
        seen.add(key)
        stored_value = encrypt_secret(value) if macro_type == "secret" else value
        macros.append(
            {
                "name": name,
                "value": stored_value,
                "description": description,
                "type": macro_type,
            }
        )
    return macros, None


def _decode_host_macros(raw):
    """Baca kolom JSON host_macros tanpa membocorkan nilai secret."""
    if not raw:
        return []
    try:
        items = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(items, list):
        return []
    macros = []
    for item in items[:HOST_MACRO_LIMIT]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()[:255]
        if not name or not HOST_MACRO_RE.match(name):
            continue
        description = str(item.get("description") or "").strip()[:255]
        macro_type = str(item.get("type") or "text").strip().lower()
        if macro_type not in HOST_MACRO_TYPES:
            macro_type = "text"
        stored_value = str(item.get("value") or "")[:2048]
        if macro_type == "secret":
            value = ""
            value_set = bool(stored_value)
        else:
            value = stored_value
            value_set = bool(value)
        macros.append(
            {
                "name": name,
                "value": value,
                "value_set": value_set,
                "description": description,
                "type": macro_type,
            }
        )
    return macros


def _parse_host_inventory(data):
    """Validasi mode + field inventory host dari form New host."""
    raw = data.get("inventory", {})
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        return None, "Inventory harus berupa objek"
    mode = str(raw.get("mode") or "disabled").strip().lower()
    if mode not in HOST_INVENTORY_MODES:
        return None, "Mode inventory tidak valid"
    raw_fields = raw.get("fields", {})
    if raw_fields is None:
        raw_fields = {}
    if not isinstance(raw_fields, dict):
        return None, "Format inventory tidak valid"
    fields = {}
    for key in HOST_INVENTORY_FIELDS:
        if key not in raw_fields:
            continue
        limit = (
            HOST_INVENTORY_TEXTAREA_LIMIT
            if key in HOST_INVENTORY_TEXTAREA_FIELDS
            else HOST_INVENTORY_TEXT_LIMIT
        )
        value = str(raw_fields.get(key) or "").strip()[:limit]
        if value:
            fields[key] = value
    return {"mode": mode, "fields": fields}, None


def _decode_host_inventory(raw):
    """Baca kolom JSON host_inventory dengan aman; data rusak diabaikan."""
    default = {"mode": "disabled", "fields": {}}
    if not raw:
        return default
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return default
    if not isinstance(data, dict):
        return default
    mode = str(data.get("mode") or "disabled").strip().lower()
    if mode not in HOST_INVENTORY_MODES:
        mode = "disabled"
    raw_fields = data.get("fields")
    if not isinstance(raw_fields, dict):
        raw_fields = {}
    fields = {}
    for key in HOST_INVENTORY_FIELDS:
        if key not in raw_fields:
            continue
        limit = (
            HOST_INVENTORY_TEXTAREA_LIMIT
            if key in HOST_INVENTORY_TEXTAREA_FIELDS
            else HOST_INVENTORY_TEXT_LIMIT
        )
        value = str(raw_fields.get(key) or "").strip()[:limit]
        if value:
            fields[key] = value
    return {"mode": mode, "fields": fields}


def _parse_host_encryption(data):
    """Validasi mode enkripsi host dari form New host."""
    raw = data.get("encryption", {})
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        return None, "Encryption harus berupa objek"
    to_host = str(raw.get("to_host") or "none").strip().lower()
    if to_host not in HOST_ENCRYPTION_METHODS:
        return None, "Mode encryption ke host tidak valid"
    raw_from = raw.get("from_host", ["none"])
    if raw_from is None:
        raw_from = []
    if not isinstance(raw_from, list):
        return None, "Format encryption dari host tidak valid"
    from_host = []
    for item in raw_from:
        method = str(item or "").strip().lower()
        if method not in HOST_ENCRYPTION_METHODS:
            return None, "Mode encryption dari host tidak valid"
        if method not in from_host:
            from_host.append(method)
    if not from_host or "none" in from_host:
        from_host = ["none"]
    result = {"to_host": to_host, "from_host": from_host}
    if to_host == "psk" or "psk" in from_host:
        psk_identity = str(raw.get("psk_identity") or "").strip()[:128]
        psk = str(raw.get("psk") or "").strip()
        if not psk_identity:
            return None, "PSK identity wajib diisi"
        if not psk:
            return None, "PSK wajib diisi"
        result["psk_identity"] = psk_identity
        result["psk"] = psk[:512]
    return result, None


def _decode_host_encryption(raw):
    """Baca kolom JSON host_encryption dengan aman; data rusak diabaikan."""
    default = {"to_host": "none", "from_host": ["none"]}
    if not raw:
        return default
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return default
    if not isinstance(data, dict):
        return default
    to_host = str(data.get("to_host") or "none").strip().lower()
    if to_host not in HOST_ENCRYPTION_METHODS:
        to_host = "none"
    raw_from = data.get("from_host")
    if not isinstance(raw_from, list):
        raw_from = ["none"]
    from_host = []
    for item in raw_from:
        method = str(item or "").strip().lower()
        if method in HOST_ENCRYPTION_METHODS and method not in from_host:
            from_host.append(method)
    if not from_host or "none" in from_host:
        from_host = ["none"]
    result = {"to_host": to_host, "from_host": from_host}
    psk_identity = str(data.get("psk_identity") or "").strip()[:128]
    if psk_identity:
        result["psk_identity"] = psk_identity
    if str(data.get("psk") or "").strip():
        result["psk_set"] = True
    return result


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
        " description, iface_type, iface_port,"
        " ipmi_auth, ipmi_privilege, ipmi_username, host_tags, host_macros,"
        " host_inventory, host_encryption,"
        " CASE WHEN ssh_pass IS NOT NULL AND ssh_pass != '' THEN 1 ELSE 0 END AS ssh_pass_set,"
        " CASE WHEN ipmi_password IS NOT NULL AND ipmi_password != '' THEN 1 ELSE 0 END AS ipmi_password_set"
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
                "description": r["description"] or "",
                "iface_type": r["iface_type"] or "",
                "iface_port": r["iface_port"] or 0,
                "ipmi_auth": (
                    (r["ipmi_auth"] or "default")
                    if (r["ipmi_auth"] or "default") in IPMI_AUTH_ALGOS
                    else "default"
                ),
                "ipmi_privilege": (
                    (r["ipmi_privilege"] or "user")
                    if (r["ipmi_privilege"] or "user") in IPMI_PRIVILEGE_LEVELS
                    else "user"
                ),
                "ipmi_username": r["ipmi_username"] or "",
                "ipmi_password_set": bool(r["ipmi_password_set"]),
                "tags": _decode_host_tags(r["host_tags"]),
                "macros": _decode_host_macros(r["host_macros"]),
                "inventory": _decode_host_inventory(r["host_inventory"]),
                "encryption": _decode_host_encryption(r["host_encryption"]),
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
@require_role("admin", "operator")
@limiter.limit("20/minute")
def api_scheduled_discovery_run():
    return jsonify(run_scheduled_snmp_discovery())


@app.route("/api/discovery/snmp", methods=["POST"])
@api_login_required
@require_role("admin", "operator")
@limiter.limit("20/minute")
def api_snmp_discovery():
    data = request.get_json(silent=True) or {}
    network = _disc_normalize_network(data.get("network"))
    community = str(data.get("community") or "").strip()
    if not _DISC_SCAN_LOCK.acquire(blocking=False):
        return (
            jsonify({"error": "Pemindaian lain sedang berjalan. Coba lagi sebentar."}),
            409,
        )
    try:
        try:
            results, diagnostics = discover_subnet(
                network,
                community,
                timeout=data.get("timeout", 0.8),
                max_hosts=256,
                return_diagnostics=True,
            )
        except (ValueError, TypeError) as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:
            logger.warning(f"SNMP discovery gagal: {exc}")
            return jsonify({"error": "Discovery gagal"}), 502
        try:
            conn, c = get_db()
            try:
                c.execute(
                    "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                    (
                        "discovery_last_scan",
                        json.dumps(
                            {
                                "network": network,
                                "count": len(results),
                                "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                "source": "manual",
                            }
                        ),
                    ),
                )
                _disc_record_events(c, "manual", network, results)
                conn.commit()
            finally:
                conn.close()
        except Exception as exc:
            logger.warning(f"Simpan riwayat discovery gagal: {exc}")
        message = f"Pemindaian selesai - {len(results)} perangkat ditemukan."
        if not results:
            if (
                diagnostics.get("timeout", 0) == diagnostics.get("hosts", 0)
                and diagnostics.get("hosts", 0) > 0
            ):
                message = (
                    "Pemindaian selesai - semua host timeout atau tidak ada balasan."
                )
            elif diagnostics.get("community_or_error", 0) > 0:
                message = (
                    "Pemindaian selesai - tidak ada perangkat valid yang merespons. "
                    "Community mungkin salah/ditolak, port 161 tertutup, firewall, host tidak aktif, atau host tidak mendukung SNMPv2c."
                )
            elif diagnostics.get("no_response", 0) > 0:
                message = (
                    "Pemindaian selesai - tidak ada balasan SNMP. "
                    "Periksa community, port 161, firewall, dan ketersediaan SNMP pada perangkat."
                )
            elif diagnostics.get("partial", 0) > 0:
                message = "Pemindaian selesai - balasan SNMP tidak lengkap."
            else:
                message = "Pemindaian selesai - tidak ada perangkat SNMP yang merespons."
        return jsonify(
            {
                "network": network,
                "count": len(results),
                "devices": results,
                "diagnostics": diagnostics,
                "message": message,
            }
        )
    finally:
        _DISC_SCAN_LOCK.release()


@app.route("/api/discovery/import", methods=["POST"])
@api_login_required
@require_role("admin", "operator")
@limiter.limit("30/minute")
def api_discovery_import():
    data = request.get_json(silent=True) or {}
    devices = data.get("devices")
    community = str(data.get("community") or "").strip()[:128]
    save_community = data.get("save_community", True) is not False
    category_opt = str(data.get("category") or "auto").strip()[:64]
    stored_community = community if save_community else ""
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
            vendor = str((device or {}).get("vendor") or "Generic")[:64]
            if category_opt.lower() in ("", "auto"):
                category = vendor
            elif category_opt.lower() == "network":
                category = "Network"
            elif category_opt.lower() == "uncategorized":
                category = "Uncategorized"
            else:
                category = category_opt
            try:
                c.execute(
                    "INSERT INTO hosts (ip, alias, category, snmp_community, snmp_profile) VALUES (?, ?, ?, ?, ?)",
                    (
                        ip,
                        str((device or {}).get("sys_name") or ip)[:128],
                        category,
                        stored_community,
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


def _disc_rule_label(source, network):
    return ("Terjadwal" if source == "scheduled" else "Manual") + " · " + network


@app.route("/api/discovery/status", methods=["GET"])
@api_login_required
@limiter.limit("120/minute")
def api_discovery_status():
    """Status discovery: kondisi terkini tiap perangkat per aturan pemindaian.

    Satu baris per (sumber, jaringan, IP) dari event terakhir — status
    'discovered' atau 'lost' — plus flag 'monitored' bila IP sudah masuk
    daftar host. ``network`` dinormalkan sehingga 192.168.30.1/24 dan
    192.168.30.0/24 dihitung sebagai aturan yang sama.

    Filter ``rule`` (boleh berulang atau dipisah koma) menerima token
    ``sumber|jaringan`` (mis. ``scheduled|192.168.30.0/24``) atau substring
    jaringan/sumber untuk kompatibilitas. ``sort`` name_asc/name_desc.
    """
    rule_terms = [
        part.strip().lower()
        for value in request.args.getlist("rule")
        for part in value.split(",")
        if part.strip()
    ]
    sort = (request.args.get("sort") or "name_asc").lower()
    if sort not in ("name_asc", "name_desc"):
        sort = "name_asc"

    conn, c = get_db()
    try:
        c.execute(
            "SELECT e.id, e.source, e.network, e.ip, e.sys_name, e.vendor,"
            " e.status, e.ts FROM discovery_events e"
            " WHERE e.id IN (SELECT MAX(id) FROM discovery_events"
            " GROUP BY source, network, ip)"
        )
        raw = [dict(r) for r in c.fetchall()]
        c.execute("SELECT ip FROM hosts")
        monitored = {r["ip"] for r in c.fetchall()}
        c.execute(
            "SELECT key, value FROM settings"
            " WHERE key IN ('discovery_last_scan','discovery_network')"
        )
        setting_rows = {r["key"]: r["value"] for r in c.fetchall()}
    finally:
        conn.close()

    def norm(value):
        return _disc_normalize_network(value)

    # Gabungkan event per (sumber, jaringan ternormalisasi, IP); pakai id terbaru.
    latest = {}
    for r in raw:
        if r["status"] not in ("discovered", "lost"):
            continue
        net = norm(r["network"])
        key = (r["source"], net, r["ip"])
        prev = latest.get(key)
        if prev is None or r["id"] > prev["id"]:
            latest[key] = r
    merged = list(latest.values())

    rule_counts = {}
    for r in merged:
        net = norm(r["network"])
        key = f'{r["source"]}|{net}'
        entry = rule_counts.get(key)
        if entry is None:
            entry = {
                "key": key,
                "label": _disc_rule_label(r["source"], net),
                "source": r["source"],
                "network": net,
                "count": 0,
            }
            rule_counts[key] = entry
        entry["count"] += 1

    def matches(rule):
        if not rule_terms:
            return True
        net = norm(rule["network"]).lower()
        source = (rule["source"] or "").lower()
        label = _disc_rule_label(rule["source"], norm(rule["network"])).lower()
        for term in rule_terms:
            token = term.lower()
            if "|" in token:
                t_source, _, t_network = token.partition("|")
                if t_source.strip() and source != t_source.strip():
                    continue
                target = norm(t_network).lower()
                if not target or net == target:
                    return True
            else:
                token_net = norm(token).lower()
                if (
                    token_net in net
                    or token in net
                    or token in source
                    or token in label
                ):
                    return True
        return False

    rows = []
    for r in merged:
        if not matches(r):
            continue
        net = norm(r["network"])
        rows.append(
            {
                "id": r["id"],
                "source": r["source"],
                "network": net,
                "key": f'{r["source"]}|{net}',
                "rule": _disc_rule_label(r["source"], net),
                "ip": r["ip"],
                "name": (r["sys_name"] or "").strip() or r["ip"],
                "vendor": (r["vendor"] or "").strip(),
                "status": r["status"],
                "ts": r["ts"],
                "monitored": r["ip"] in monitored,
            }
        )
    rows.sort(
        key=lambda d: (d["name"].lower(), d["ip"]),
        reverse=(sort == "name_desc"),
    )

    schedule = {"network": "", "last": None, "next": None}
    schedule["network"] = norm(setting_rows.get("discovery_network") or "")
    last_raw = setting_rows.get("discovery_last_scan") or ""
    if last_raw:
        try:
            last = json.loads(last_raw)
            if isinstance(last, dict) and last.get("at"):
                schedule["last"] = {
                    "at": last.get("at"),
                    "network": norm(last.get("network")),
                    "count": last.get("count", 0),
                    "source": last.get("source") or "",
                }
                if not schedule["network"]:
                    schedule["network"] = norm(last.get("network"))
        except (TypeError, ValueError):
            pass
    if SCHEDULER_ENABLED:
        try:
            job = scheduler.get_job("discovery_scheduled")
            nxt = getattr(job, "next_run_time", None) if job else None
            schedule["next"] = nxt.isoformat() if nxt else None
        except Exception:
            pass

    return jsonify(
        {
            "rows": rows,
            "rules": sorted(rule_counts.values(), key=lambda item: item["label"]),
            "count": len(rows),
            "total": len(rows),
            "total_all": len(merged),
            "schedule": schedule,
        }
    )


@app.route("/api/hosts", methods=["POST"])
@api_login_required
@limiter.limit("30/minute")
def api_add_host():
    data = request.get_json(silent=True) or {}
    ip = str(data.get("ip") or "").strip()
    alias = str(data.get("alias") or "").strip()
    category = str(data.get("category") or "").strip() or "Uncategorized"
    snmp_community = str(data.get("snmp_community") or "").strip()
    description = str(data.get("description") or "").strip()[:2000]
    iface_type = str(data.get("iface_type") or "").strip().lower()
    if iface_type not in ("", "agent", "snmp"):
        return jsonify({"error": "Tipe interface tidak valid"}), 400
    try:
        iface_port = int(data.get("iface_port") or 0)
    except (ValueError, TypeError):
        iface_port = 0
    if iface_port and not (1 <= iface_port <= 65535):
        return jsonify({"error": "Port interface tidak valid"}), 400
    ipmi_auth = str(data.get("ipmi_auth") or "default").strip().lower()
    if ipmi_auth not in IPMI_AUTH_ALGOS:
        return jsonify({"error": "Authentication algorithm IPMI tidak valid"}), 400
    ipmi_privilege = str(data.get("ipmi_privilege") or "user").strip().lower()
    if ipmi_privilege not in IPMI_PRIVILEGE_LEVELS:
        return jsonify({"error": "Privilege level IPMI tidak valid"}), 400
    ipmi_username = str(data.get("ipmi_username") or "").strip()[:64]
    ipmi_password = encrypt_secret(str(data.get("ipmi_password") or "")[:128])
    host_tags_list, err = _parse_host_tags(data)
    if err:
        return jsonify({"error": err}), 400
    assert host_tags_list is not None
    host_tags = json.dumps(host_tags_list, ensure_ascii=False)
    host_macros_list, err = _parse_host_macros(data)
    if err:
        return jsonify({"error": err}), 400
    assert host_macros_list is not None
    host_macros = json.dumps(host_macros_list, ensure_ascii=False)
    host_inventory_data, err = _parse_host_inventory(data)
    if err:
        return jsonify({"error": err}), 400
    assert host_inventory_data is not None
    host_inventory = json.dumps(host_inventory_data, ensure_ascii=False)
    host_encryption_data, err = _parse_host_encryption(data)
    if err:
        return jsonify({"error": err}), 400
    assert host_encryption_data is not None
    host_encryption = json.dumps(host_encryption_data, ensure_ascii=False)
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
            " ssh_user, ssh_pass, ssh_port, backup_enable, description,"
            " iface_type, iface_port, ipmi_auth, ipmi_privilege,"
            " ipmi_username, ipmi_password, host_tags, host_macros,"
            " host_inventory, host_encryption)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,"
            " ?, ?, ?, ?, ?, ?, ?, ?)",
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
                description,
                iface_type,
                iface_port,
                ipmi_auth,
                ipmi_privilege,
                ipmi_username,
                ipmi_password,
                host_tags,
                host_macros,
                host_inventory,
                host_encryption,
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


@app.route("/hosts")
@login_required
def hosts_page():
    return render_template("hosts.html")


@app.route("/api/hosts/zbx", methods=["GET"])
@api_login_required
@limiter.limit("120/minute")
def api_hosts_zbx():
    """Daftar host gaya Zabbix: availability (ping/SNMP/agent), maintenance,
    masalah aktif, dan jumlah data terkini per host."""
    name_q = (request.args.get("name") or "").strip()
    ip_q = (request.args.get("ip") or "").strip()
    group_q = (request.args.get("group") or "").strip()
    status_f = (request.args.get("status") or "all").lower()
    if status_f not in ("all", "normal", "maintenance"):
        status_f = "all"
    only_problems = request.args.get("only_problems") == "1"
    severity_raw = (request.args.get("severity") or "").lower()
    severities = [
        s.strip() for s in severity_raw.split(",") if s.strip() in _PROBLEM_SEVERITIES
    ]
    sort = (request.args.get("sort") or "name_asc").lower()
    if sort not in ("name_asc", "name_desc", "problems_desc"):
        sort = "name_asc"

    maint_map = get_active_maintenance_map()
    try:
        ping_map = dict(status_memory.items())
    except Exception:
        ping_map = {}

    now = datetime.now()

    def _age_min(ts):
        try:
            return (
                now - datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
            ).total_seconds() / 60
        except (TypeError, ValueError):
            return None

    def _label_hit(label, ip, alias):
        lbl = (label or "").lower()
        ip_l = (ip or "").lower()
        alias_l = (alias or "").lower()
        if not lbl:
            return False
        return (
            lbl == ip_l
            or bool(alias_l and alias_l in lbl)
            or bool(ip_l and ip_l in lbl)
        )

    try:
        active_alerts = get_active_alerts()
    except Exception:
        active_alerts = []

    conn, c = get_db()
    try:
        c.execute(
            "SELECT host, MAX(timestamp) AS last, COUNT(*) AS cnt"
            " FROM device_health GROUP BY host"
        )
        health_map = {
            r["host"]: {"last": r["last"], "count": r["cnt"] or 0} for r in c.fetchall()
        }
        c.execute(
            "SELECT host, MAX(timestamp) AS last FROM agent_metrics GROUP BY host"
        )
        agent_map = {r["host"]: r["last"] for r in c.fetchall()}
        c.execute("SELECT DISTINCT category FROM hosts ORDER BY category ASC")
        categories = [r["category"] or "Uncategorized" for r in c.fetchall()]
        c.execute("SELECT ip, COUNT(*) AS cnt FROM services GROUP BY ip")
        service_map = {r["ip"]: r["cnt"] or 0 for r in c.fetchall()}

        c.execute(
            "SELECT id, ip, alias, category, snmp_community, snmp_profile,"
            " ssh_port, iface_type, iface_port, host_tags, host_macros,"
            " host_inventory, host_encryption FROM hosts ORDER BY id ASC"
        )
        rows = []
        for r in c.fetchall():
            d = dict(r)
            ip = d["ip"]
            alias = (d["alias"] or "").strip()
            category = d["category"] or "Uncategorized"
            display = alias or ip

            ping_view = ping_map.get(ip)
            ping_state = (
                "unknown" if ping_view is None else ("up" if ping_view else "down")
            )

            snmp_mon = (
                bool((d["snmp_community"] or "").strip())
                and (d["snmp_profile"] or "auto") != "generic"
            )
            health = health_map.get(ip) or {"last": None, "count": 0}
            age_m = _age_min(health["last"])
            if not snmp_mon:
                snmp_state = "none"
            elif age_m is None:
                snmp_state = "unknown"
            elif age_m <= 120:
                snmp_state = "ok"
            else:
                snmp_state = "stale"

            agent_last = agent_map.get(ip)
            agent_age = _age_min(agent_last)
            if agent_last is None:
                agent_state = "none"
            elif agent_age is not None and agent_age <= 5:
                agent_state = "ok"
            else:
                agent_state = "stale"

            maint = maint_map.get(ip)
            in_maint = maint is not None

            matched = [a for a in active_alerts if _label_hit(a.get("host"), ip, alias)]
            worst = None
            sev_hit = False
            host_sevs = []
            for a in matched:
                sev = (a.get("severity") or "").lower()
                if sev and sev not in host_sevs:
                    host_sevs.append(sev)
                rank = _PROBLEM_SEVERITIES.get(sev)
                if rank is None:
                    continue
                if worst is None or rank > _PROBLEM_SEVERITIES[worst]:
                    worst = sev
                if not severities or sev in severities:
                    sev_hit = True
            if severities and not sev_hit:
                continue

            rows.append(
                {
                    "id": d["id"],
                    "ip": ip,
                    "name": display,
                    "category": category,
                    "ping": ping_state,
                    "snmp": snmp_state,
                    "agent": agent_state,
                    "snmp_monitored": snmp_mon,
                    "maintenance": in_maint,
                    "maintenance_reason": (maint or {}).get("reason") or "",
                    "latest_count": health["count"],
                    "problems_count": len(matched),
                    "worst_severity": worst,
                    "severities": host_sevs,
                    "web_count": service_map.get(ip, 0),
                    "ssh_port": d["ssh_port"] or 22,
                    "iface_type": d["iface_type"] or "",
                    "iface_port": d["iface_port"] or 0,
                    "tags": _decode_host_tags(d["host_tags"]),
                    "macros": _decode_host_macros(d["host_macros"]),
                    "inventory": _decode_host_inventory(d["host_inventory"]),
                    "encryption": _decode_host_encryption(d["host_encryption"]),
                }
            )
    finally:
        conn.close()

    total = len(rows)
    low = (name_q or "").lower()
    if low:
        rows = [d for d in rows if low in d["name"].lower() or low in d["ip"].lower()]
    if ip_q:
        rows = [d for d in rows if ip_q.lower() in d["ip"].lower()]
    if group_q:
        rows = [d for d in rows if group_q.lower() in (d["category"] or "").lower()]
    if status_f == "maintenance":
        rows = [d for d in rows if d["maintenance"]]
    elif status_f == "normal":
        rows = [d for d in rows if not d["maintenance"]]
    if only_problems:
        rows = [d for d in rows if d["problems_count"] > 0]

    if sort == "name_desc":
        rows.sort(key=lambda d: (d["name"].lower(), d["ip"]), reverse=True)
    elif sort == "problems_desc":
        rows.sort(key=lambda d: (-d["problems_count"], d["name"].lower(), d["ip"]))
    else:
        rows.sort(key=lambda d: (d["name"].lower(), d["ip"]))

    return jsonify(
        {
            "hosts": rows,
            "count": len(rows),
            "total": total,
            "categories": categories,
        }
    )


@app.route("/latest-data")
@login_required
def latest_data_page():
    return render_template("latest_data.html")


@app.route("/api/latest-data", methods=["GET"])
@api_login_required
@limiter.limit("120/minute")
def api_latest_data():
    """Item terkini per host (gaya Zabbix): nilai terakhir + perubahan.

    Sumber nyata: device_health (SNMP), agent_metrics (agent), dan
    ping_logs (latensi). Satu item per metrik yang punya data; metrik yang
    hilang pada pemeriksaan terakhir ditandai state="unsupported".
    """
    host_q = (request.args.get("host") or "").strip()
    group_q = (request.args.get("group") or "").strip()
    name_q = (request.args.get("name") or "").strip()
    source_f = (request.args.get("source") or "all").lower()
    if source_f not in ("all", "snmp", "agent", "ping"):
        source_f = "all"
    sort = (request.args.get("sort") or "name_asc").lower()
    if sort not in ("name_asc", "name_desc", "check_desc", "check_asc"):
        sort = "name_asc"

    conn, c = get_db()
    try:
        c.execute("SELECT ip, alias, category FROM hosts ORDER BY id ASC")
        host_rows = [dict(r) for r in c.fetchall()]
        categories = sorted({r["category"] or "Uncategorized" for r in host_rows})

        items = []

        def push_item(
            *,
            host,
            ip,
            category,
            name,
            unit,
            value,
            check,
            source,
            prev=None,
            prev_ts=None,
            error=None,
        ):
            """Simpan satu item; value None berarti item Not supported."""
            delta = prev_val = None
            if prev is not None:
                try:
                    prev_val = round(float(prev), 2)
                    if value is not None:
                        delta = round(float(value) - prev_val, 2)
                except (TypeError, ValueError):
                    prev_val = None
                    prev_ts = None
            items.append(
                {
                    "host": host,
                    "ip": ip,
                    "category": category,
                    "name": name,
                    "unit": unit,
                    "value": None if value is None else round(float(value), 2),
                    "delta": delta,
                    "prev": prev_val,
                    "prev_check": prev_ts,
                    "check": check,
                    "source": source,
                    "state": "normal" if value is not None else "unsupported",
                    "error": error,
                }
            )

        for r in host_rows:
            ip = r["ip"]
            alias = (r["alias"] or "").strip()
            display = alias or ip
            category = r["category"] or "Uncategorized"

            c.execute(
                "SELECT timestamp, cpu, mem_used, storage_used, temp_c"
                " FROM device_health WHERE host=?"
                " ORDER BY timestamp DESC, id DESC LIMIT 2",
                (ip,),
            )
            health = c.fetchall()
            if health:
                new = health[0]
                prev = health[1] if len(health) > 1 else None
                for key, nm, unit in (
                    ("cpu", "CPU load", "%"),
                    ("mem_used", "Memory used", "%"),
                    ("storage_used", "Storage used", "%"),
                    ("temp_c", "Temperature", "°C"),
                ):
                    v = new[key]
                    prev_v = prev[key] if prev is not None else None
                    if v is None and prev_v is None:
                        continue
                    push_item(
                        host=display,
                        ip=ip,
                        category=category,
                        name=nm,
                        unit=unit,
                        value=v,
                        check=new["timestamp"],
                        prev=prev_v,
                        prev_ts=prev["timestamp"] if prev_v is not None else None,
                        source="snmp",
                        error=(
                            None
                            if v is not None
                            else "Nilai tidak dikembalikan pada pemeriksaan terakhir"
                        ),
                    )

            c.execute(
                "SELECT timestamp, cpu_percent, ram_percent, disk_percent,"
                " net_in, net_out FROM agent_metrics WHERE host=?"
                " ORDER BY timestamp DESC, id DESC LIMIT 2",
                (ip,),
            )
            agent = c.fetchall()
            if agent:
                new = agent[0]
                prev = agent[1] if len(agent) > 1 else None
                for key, nm, unit in (
                    ("cpu_percent", "CPU (agent)", "%"),
                    ("ram_percent", "Memory (agent)", "%"),
                    ("disk_percent", "Disk (agent)", "%"),
                    ("net_in", "Net in (agent)", "KB/s"),
                    ("net_out", "Net out (agent)", "KB/s"),
                ):
                    v = new[key]
                    prev_v = prev[key] if prev is not None else None
                    if v is None and prev_v is None:
                        continue
                    push_item(
                        host=display,
                        ip=ip,
                        category=category,
                        name=nm,
                        unit=unit,
                        value=v,
                        check=new["timestamp"],
                        prev=prev_v,
                        prev_ts=prev["timestamp"] if prev_v is not None else None,
                        source="agent",
                        error=(
                            None
                            if v is not None
                            else "Nilai tidak dikembalikan agen pada pemeriksaan terakhir"
                        ),
                    )

            c.execute(
                "SELECT timestamp, latency FROM ping_logs WHERE host=?"
                " ORDER BY timestamp DESC, id DESC LIMIT 2",
                (ip,),
            )
            pings = c.fetchall()
            if pings:
                new = pings[0]
                prev = pings[1] if len(pings) > 1 else None
                v = new["latency"]
                # -1 berarti ping gagal (lihat /api/history).
                if v is not None and float(v) < 0:
                    v = None
                prev_v = prev["latency"] if prev is not None else None
                if prev_v is not None and float(prev_v) < 0:
                    prev_v = None
                if v is not None or prev_v is not None:
                    push_item(
                        host=display,
                        ip=ip,
                        category=category,
                        name="Ping latency",
                        unit="ms",
                        value=v,
                        check=new["timestamp"],
                        prev=prev_v,
                        prev_ts=prev["timestamp"] if prev_v is not None else None,
                        source="ping",
                        error=(
                            None
                            if v is not None
                            else "Ping gagal pada pemeriksaan terakhir"
                        ),
                    )
    finally:
        conn.close()

    total = len(items)
    low_host, low_group, low_name = host_q.lower(), group_q.lower(), name_q.lower()
    if low_host:
        host_terms = [
            t.strip().lower() for t in host_q.replace(";", ",").split(",") if t.strip()
        ]
        items = [
            d
            for d in items
            if any(
                term in d["host"].lower() or term in d["ip"].lower()
                for term in host_terms
            )
        ]
    if low_group:
        group_terms = [
            t.strip().lower() for t in group_q.replace(";", ",").split(",") if t.strip()
        ]
        items = [
            d
            for d in items
            if any(term in d["category"].lower() for term in group_terms)
        ]
    if low_name:
        items = [d for d in items if low_name in d["name"].lower()]
    if source_f != "all":
        items = [d for d in items if d["source"] == source_f]

    if sort == "name_desc":
        items.sort(key=lambda d: (d["name"].lower(), d["host"].lower()), reverse=True)
    elif sort == "check_desc":
        items.sort(
            key=lambda d: (d["check"], d["host"].lower(), d["name"].lower()),
            reverse=True,
        )
    elif sort == "check_asc":
        items.sort(key=lambda d: (d["check"], d["host"].lower(), d["name"].lower()))
    else:
        items.sort(key=lambda d: (d["name"].lower(), d["host"].lower()))

    return jsonify(
        {
            "items": items,
            "count": len(items),
            "total": total,
            "categories": categories,
            "hosts": sorted(
                {((h["alias"] or "").strip() or h["ip"]) for h in host_rows}
                | {h["ip"] for h in host_rows}
            ),
        }
    )


# ------------------------------------------------------------------
# Maps: daftar peta topologi (gaya Zabbix) di atas hosts nyata
# ------------------------------------------------------------------


def _map_valid_name(name):
    name = str(name or "").strip()
    if not (1 <= len(name) <= 128):
        return None
    return name


def _map_name_taken(c, name, exclude_id=None):
    """Cek nama peta ganda (case-insensitive), opsional kecualikan id sendiri."""
    if exclude_id is None:
        c.execute("SELECT id FROM maps WHERE name = ? COLLATE NOCASE", (name,))
    else:
        c.execute(
            "SELECT id FROM maps WHERE name = ? COLLATE NOCASE AND id <> ?",
            (name, exclude_id),
        )
    return c.fetchone() is not None


def _map_valid_dim(v):
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    if not (100 <= n <= 65535):
        return None
    return n


def _map_valid_pos(v):
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    if not (0 <= n <= 100000):
        return None
    return n


_MAP_SETTINGS_DEFAULTS = {
    "background_image": "none",
    "background_scale": "proportional",
    "automatic_icon_mapping": "manual",
    "icon_highlight": False,
    "mark_elements": False,
    "display_problems": "expand_single",
    "label_type": "label",
    "advanced_labels": True,
    "host_group_label_type": "label",
    "host_label_type": "label",
    "trigger_label_type": "status",
    "map_label_type": "label",
    "image_label_type": "nothing",
    "label_location": "bottom",
    "show_element_labels": "always",
    "show_link_labels": "always",
    "problem_display": "all",
    "minimum_severity": "not_classified",
    "show_suppressed": False,
    "urls": [],
    "sharing": {"type": "public", "user_groups": [], "users": []},
}

_MAP_SETTINGS_ENUMS = {
    "background_image": ("none",),
    "background_scale": ("none", "proportional"),
    "automatic_icon_mapping": ("manual",),
    "display_problems": ("expand_single", "number", "number_most_critical"),
    "label_type": ("label", "ip", "name", "status", "nothing"),
    "host_group_label_type": ("label", "ip", "name", "status", "nothing"),
    "host_label_type": ("label", "ip", "name", "status", "nothing"),
    "trigger_label_type": ("label", "ip", "name", "status", "nothing"),
    "map_label_type": ("label", "ip", "name", "status", "nothing"),
    "image_label_type": ("label", "ip", "name", "status", "nothing"),
    "label_location": ("bottom", "left", "right", "top"),
    "show_element_labels": ("always", "auto_hide"),
    "show_link_labels": ("always", "auto_hide"),
    "problem_display": ("all", "separated", "unacknowledged"),
    "minimum_severity": (
        "not_classified",
        "information",
        "warning",
        "average",
        "high",
        "disaster",
    ),
}

_MAP_SETTINGS_BOOLS = (
    "icon_highlight",
    "mark_elements",
    "advanced_labels",
    "show_suppressed",
)

_MAP_URL_ELEMENTS = ("host", "host_group", "trigger", "map", "image")
_MAP_SHARE_PERMISSIONS = ("read-only", "read-write")


def _map_settings_defaults():
    """Salinan baru default pengaturan peta (hindari list/dict bersama)."""
    return json.loads(json.dumps(_MAP_SETTINGS_DEFAULTS))


def _map_settings_payload(data):
    """Ambil pengaturan dari payload API (nested ``settings`` atau field datar)."""
    if not isinstance(data, dict):
        return {}
    if "settings" in data:
        nested = data.get("settings")
        if not isinstance(nested, dict):
            raise ValueError("settings harus berupa objek")
        return nested
    return {k: data[k] for k in _MAP_SETTINGS_DEFAULTS if k in data}


def _map_setting_bool(raw, key, default):
    value = raw.get(key, default)
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    raise ValueError(f"{key} harus berupa boolean")


def _map_setting_enum(raw, key, allowed, default):
    value = raw.get(key, default)
    if value is None:
        return default
    value = str(value).strip().lower()
    if value not in allowed:
        raise ValueError(f"Nilai {key} tidak valid")
    return value


def _map_parse_urls(raw):
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("urls harus berupa list")
    if len(raw) > 100:
        raise ValueError("urls maksimal 100 item")
    urls = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("URL peta tidak valid")
        name = str(item.get("name") or "").strip()
        url = str(item.get("url") or "").strip()
        if not name and not url:
            continue
        if not name or not url:
            raise ValueError("Nama dan URL wajib diisi")
        if len(name) > 128:
            raise ValueError("Nama URL maksimal 128 karakter")
        if len(url) > 2048:
            raise ValueError("URL maksimal 2048 karakter")
        element = str(item.get("element") or "host").strip().lower()
        if element not in _MAP_URL_ELEMENTS:
            raise ValueError("Element URL tidak dikenal")
        urls.append({"name": name, "url": url, "element": element})
    return urls


def _map_parse_shares(raw, label):
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError(f"{label} harus berupa list")
    if len(raw) > 100:
        raise ValueError(f"{label} maksimal 100 item")
    shares = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError(f"{label} tidak valid")
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        if len(name) > 128:
            raise ValueError(f"Nama pada {label} maksimal 128 karakter")
        permission = str(item.get("permission") or "read-only").strip().lower()
        if permission not in _MAP_SHARE_PERMISSIONS:
            raise ValueError(f"Permission pada {label} tidak valid")
        shares.append({"name": name, "permission": permission})
    return shares


def _map_parse_sharing(raw, base):
    if raw is None:
        return base
    if not isinstance(raw, dict):
        raise ValueError("sharing harus berupa objek")
    out = {
        "type": base.get("type", "public"),
        "user_groups": list(base.get("user_groups") or []),
        "users": list(base.get("users") or []),
    }
    if "type" in raw:
        out["type"] = _map_setting_enum(raw, "type", ("private", "public"), out["type"])
    if "user_groups" in raw:
        out["user_groups"] = _map_parse_shares(raw.get("user_groups"), "user_groups")
    if "users" in raw:
        out["users"] = _map_parse_shares(raw.get("users"), "users")
    return out


def _map_parse_settings(raw, base=None):
    """Validasi + gabungkan pengaturan peta dengan default/nilai sebelumnya."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("settings harus berupa objek")

    out = _map_settings_defaults()
    if isinstance(base, dict):
        for key in _MAP_SETTINGS_DEFAULTS:
            if key in base and key not in ("urls", "sharing"):
                out[key] = base[key]
        if isinstance(base.get("urls"), list):
            out["urls"] = list(base["urls"])
        if isinstance(base.get("sharing"), dict):
            out["sharing"].update(base["sharing"])

    for key, allowed in _MAP_SETTINGS_ENUMS.items():
        if key in raw:
            out[key] = _map_setting_enum(raw, key, allowed, out.get(key))
    for key in _MAP_SETTINGS_BOOLS:
        if key in raw:
            out[key] = _map_setting_bool(raw, key, out.get(key, False))
    if "urls" in raw:
        out["urls"] = _map_parse_urls(raw.get("urls"))
    if "sharing" in raw:
        out["sharing"] = _map_parse_sharing(raw.get("sharing"), out["sharing"])
    return out


def _map_valid_owner(value):
    owner = str(value or "").strip()
    if not (1 <= len(owner) <= 150):
        return None
    return owner


def _map_role():
    return str(getattr(current_user, "role", "") or "").strip().lower()


def _map_sharing(settings):
    sharing = (settings or {}).get("sharing")
    return sharing if isinstance(sharing, dict) else {}


def _map_share_permission(settings, username):
    """Permission share untuk user (string kosong bila tidak dibagikan)."""
    if not username:
        return ""
    for item in _map_sharing(settings).get("users") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("name") or "").strip() == username:
            return str(item.get("permission") or "read-only").strip().lower()
    return ""


def _map_can_read(owner, settings, username, role):
    """Boleh melihat peta: admin, owner, peta publik, atau share user.

    ``user_groups`` belum bisa dievaluasi karena aplikasi ini belum punya
    model keanggotaan grup; share ke grup tetap disimpan apa adanya.
    """
    if role == "admin":
        return True
    if not username:
        return False
    if str(owner or "").strip() == username:
        return True
    if str(_map_sharing(settings).get("type") or "public").lower() == "public":
        return True
    return bool(_map_share_permission(settings, username))


def _map_can_write(owner, settings, username, role):
    """Boleh mengubah peta: admin, owner, atau share read-write."""
    if role == "admin":
        return True
    if not username:
        return False
    if str(owner or "").strip() == username:
        return True
    return _map_share_permission(settings, username) == "read-write"


def _map_load_settings(c, map_id):
    """Baca owner + settings peta; data lama/korup jatuh ke default."""
    c.execute("SELECT owner, settings FROM map_settings WHERE map_id=?", (map_id,))
    row = c.fetchone()
    if row is None:
        return "", _map_settings_defaults()
    owner = str(row["owner"] or "").strip()
    try:
        settings = _map_parse_settings(json.loads(row["settings"] or "{}"))
    except (TypeError, ValueError):
        settings = _map_settings_defaults()
    return owner, settings


def _map_save_settings(c, map_id, owner, settings):
    c.execute(
        "INSERT OR REPLACE INTO map_settings (map_id, owner, settings, updated_at)"
        " VALUES (?,?,?,datetime('now','localtime'))",
        (map_id, owner, json.dumps(settings, ensure_ascii=False)),
    )


def _map_parse_content(raw_elems, raw_links):
    """Validasi payload konten peta → (elements, links_kep presiduan uid)."""
    if not isinstance(raw_elems, list) or len(raw_elems) > 500:
        raise ValueError("elements harus berupa list (maksimal 500 item)")
    uids = set()
    elems = []
    for i, raw in enumerate(raw_elems):
        if not isinstance(raw, dict):
            raise ValueError("elemen peta tidak valid")
        uid = str(raw.get("uid") or "").strip()[:64] or f"e{i + 1}"
        if uid in uids:
            raise ValueError(f"uid elemen ganda: {uid}")
        etype = str(raw.get("etype") or "").strip()
        if etype not in ("host", "text"):
            raise ValueError(f"tipe elemen tidak dikenal pada {uid}")
        label = str(raw.get("label") or "").strip()[:128]
        if not label:
            raise ValueError(f"elemen {uid} tanpa label")
        ip = str(raw.get("ip") or "").strip()[:45] if etype == "host" else ""
        x = _map_valid_pos(raw.get("x", 40))
        y = _map_valid_pos(raw.get("y", 40))
        if x is None or y is None:
            raise ValueError(f"posisi elemen {uid} tidak valid")
        uids.add(uid)
        elems.append(
            {"uid": uid, "etype": etype, "label": label, "ip": ip, "x": x, "y": y}
        )

    links = []
    pairs = set()
    for raw in raw_links:
        if not isinstance(raw, dict):
            continue
        a = str(raw.get("from") or "").strip()
        b = str(raw.get("to") or "").strip()
        if a == b or a not in uids or b not in uids:
            continue
        pair = tuple(sorted((a, b)))
        if pair in pairs:
            continue
        pairs.add(pair)
        links.append(pair)
    return elems, links


def _map_insert_content(c, map_id, elems, links):
    """Ganti seluruh elemen+tautan peta (replace-semantics)."""
    c.execute("DELETE FROM map_links WHERE map_id=?", (map_id,))
    c.execute("DELETE FROM map_elements WHERE map_id=?", (map_id,))
    id_by_uid = {}
    for e in elems:
        c.execute(
            "INSERT INTO map_elements (map_id, uid, etype, label, ip, x, y)"
            " VALUES (?,?,?,?,?,?,?)",
            (map_id, e["uid"], e["etype"], e["label"], e["ip"], e["x"], e["y"]),
        )
        id_by_uid[e["uid"]] = c.lastrowid
    for a, b in links:
        c.execute(
            "INSERT INTO map_links (map_id, from_id, to_id) VALUES (?,?,?)",
            (map_id, id_by_uid[a], id_by_uid[b]),
        )
    return len(elems), len(links)


@app.route("/maps")
@login_required
def maps_page():
    return render_template(
        "maps.html", can_create=_map_role() in ("admin", "operator")
    )


_MAP_USER_GROUPS = (
    "Disabled",
    "Enabled debug mode",
    "Guests",
    "Internal",
    "No access to the frontend",
    "Zabbix administrators",
)

_MAP_SHARE_USER_META = {
    "admin": ("Admin", "Zabbix", "Administrator"),
    "guest": ("guest", "", ""),
}


@app.route("/maps/new")
@login_required
def map_new_page():
    """Form Create map lengkap (gaya Zabbix 7.4 Network maps)."""
    role_labels = {
        "admin": "Administrator",
        "operator": "Operator",
        "viewer": "Viewer",
    }
    users, seen = [], set()
    try:
        raw_users = list_users()
    except Exception:
        raw_users = []
    for item in raw_users:
        username = str((item or {}).get("username") or "").strip()
        if not username or username in seen:
            continue
        role = str((item or {}).get("role") or "operator").strip().lower()
        users.append(
            {
                "username": username,
                "role": role,
                "label": f"{username} ({role_labels.get(role, role.title() or 'User')})",
            }
        )
        seen.add(username)
    owner = str(current_user.username or DASHBOARD_USERNAME).strip()
    owner_role = str(getattr(current_user, "role", "admin") or "admin").lower()
    if owner not in seen:
        users.insert(
            0,
            {
                "username": owner,
                "role": owner_role,
                "label": f"{owner} ({role_labels.get(owner_role, owner_role.title() or 'User')})",
            },
        )
    owner_label = next(
        (u["label"] for u in users if u["username"] == owner),
        f"{owner} ({role_labels.get(owner_role, 'User')})",
    )
    share_users = []
    for item in users:
        username = item["username"]
        display, first_name, last_name = _MAP_SHARE_USER_META.get(
            username, (username, "", "")
        )
        share_users.append(
            {
                "username": username,
                "label": display,
                "name": first_name,
                "last_name": last_name,
            }
        )
    if not any(u["username"] == "guest" for u in share_users):
        share_users.append(
            {"username": "guest", "label": "guest", "name": "", "last_name": ""}
        )
    return render_template(
        "map_form.html",
        owner=owner,
        owner_label=owner_label,
        users=users,
        user_groups=list(_MAP_USER_GROUPS),
        share_users=share_users,
    )


@app.route("/maps/<int:map_id>")
@login_required
def map_view_page(map_id):
    """Tampilan peta read-only (dipakai juga oleh mode kiosk/fullscreen)."""
    conn, c = get_db()
    try:
        c.execute("SELECT id FROM maps WHERE id=?", (map_id,))
        if c.fetchone() is None:
            return "<p>Peta tidak ditemukan</p>", 404
        owner, settings = _map_load_settings(c, map_id)
    finally:
        conn.close()
    if not _map_can_read(owner, settings, current_user.username, _map_role()):
        return "<p>Peta tidak ditemukan</p>", 404
    return render_template("map_view.html", map_id=map_id)


@app.route("/maps/<int:map_id>/edit")
@login_required
def map_edit_page(map_id):
    conn, c = get_db()
    try:
        c.execute("SELECT id FROM maps WHERE id=?", (map_id,))
        exist = c.fetchone()
        owner, settings = _map_load_settings(c, map_id) if exist else ("", {})
    finally:
        conn.close()
    if exist is None:
        return "<p>Peta tidak ditemukan</p>", 404
    if not _map_can_write(owner, settings, current_user.username, _map_role()):
        return "<p>Anda tidak punya akses untuk mengubah peta ini.</p>", 403
    return render_template("map_edit.html", map_id=map_id)


@app.route("/api/maps", methods=["GET"])
@api_login_required
@limiter.limit("120/minute")
def api_maps_list():
    """Daftar peta yang boleh dilihat user, plus filter Name.

    ``total`` adalah jumlah peta yang terlihat sebelum filter, sehingga UI
    bisa menampilkan "N of M maps found" yang benar.
    """
    name_q = (request.args.get("name") or "").strip().lower()
    sort = (request.args.get("sort") or "name_asc").lower()
    if sort not in ("name_asc", "name_desc"):
        sort = "name_asc"
    username = str(current_user.username or "").strip()
    role = _map_role()

    conn, c = get_db()
    try:
        c.execute(
            "SELECT m.id, m.name, m.width, m.height, m.created_at,"
            " (SELECT COUNT(*) FROM map_elements e WHERE e.map_id = m.id) AS elements,"
            " s.owner AS owner, s.settings AS settings_json"
            " FROM maps m LEFT JOIN map_settings s ON s.map_id = m.id"
            + (
                " ORDER BY m.name COLLATE NOCASE"
                if sort == "name_asc"
                else " ORDER BY m.name COLLATE NOCASE DESC"
            )
        )
        visible = []
        for r in c.fetchall():
            try:
                settings = _map_parse_settings(json.loads(r["settings_json"] or "{}"))
            except (TypeError, ValueError):
                settings = _map_settings_defaults()
            if not _map_can_read(r["owner"] or "", settings, username, role):
                continue
            visible.append(
                {
                    "id": r["id"],
                    "name": r["name"],
                    "width": int(r["width"]),
                    "height": int(r["height"]),
                    "created_at": r["created_at"],
                    "elements": int(r["elements"]),
                    "owner": str(r["owner"] or ""),
                    "can_write": _map_can_write(
                        r["owner"] or "", settings, username, role
                    ),
                }
            )
    finally:
        conn.close()
    total = len(visible)
    if name_q:
        visible = [m for m in visible if name_q in str(m["name"]).lower()]
    return jsonify({"maps": visible, "count": len(visible), "total": total})


@app.route("/api/maps", methods=["POST"])
@api_login_required
@require_role("admin", "operator")
@limiter.limit("60/minute")
def api_maps_create():
    data = request.get_json(silent=True) or {}
    name = _map_valid_name(data.get("name"))
    width = _map_valid_dim(data.get("width", 960))
    height = _map_valid_dim(data.get("height", 540))
    if name is None or width is None or height is None:
        return (
            jsonify({"error": "Nama 1-128 karakter; dimensi 100-65535"}),
            400,
        )
    if "owner" in data:
        owner = _map_valid_owner(data.get("owner"))
    else:
        owner = str(current_user.username or DASHBOARD_USERNAME).strip()
    if owner is None:
        return jsonify({"error": "Owner wajib dipilih"}), 400
    try:
        settings = _map_parse_settings(_map_settings_payload(data))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    conn, c = get_db()
    try:
        if _map_name_taken(c, name):
            return jsonify({"error": "Nama peta sudah dipakai"}), 400
        c.execute(
            "INSERT INTO maps (name, width, height) VALUES (?,?,?)",
            (name, width, height),
        )
        new_id = c.lastrowid
        _map_save_settings(c, new_id, owner, settings)
        conn.commit()
    finally:
        conn.close()
    try:
        audit(current_user.username, "map.create", name, f"maps/{new_id}")
    except Exception:
        pass
    return (
        jsonify(
            {
                "id": new_id,
                "name": name,
                "width": width,
                "height": height,
                "owner": owner,
                "settings": settings,
            }
        ),
        201,
    )


@app.route("/api/maps/<int:map_id>", methods=["GET"])
@api_login_required
@limiter.limit("120/minute")
def api_maps_detail(map_id):
    """Rincian peta: elemen (uid, posisi, status ping nyata) + tautan."""
    conn, c = get_db()
    try:
        c.execute(
            "SELECT id, name, width, height, created_at FROM maps WHERE id=?",
            (map_id,),
        )
        mrow = c.fetchone()
        if mrow is None:
            return jsonify({"error": "Peta tidak ditemukan"}), 404
        m = {
            "id": mrow["id"],
            "name": mrow["name"],
            "width": int(mrow["width"]),
            "height": int(mrow["height"]),
            "created_at": mrow["created_at"],
        }
        owner, settings = _map_load_settings(c, map_id)
        if not _map_can_read(owner, settings, current_user.username, _map_role()):
            return jsonify({"error": "Peta tidak ditemukan"}), 404
        c.execute(
            "SELECT id, uid, etype, label, ip, x, y FROM map_elements"
            " WHERE map_id=? ORDER BY id ASC",
            (map_id,),
        )
        raw_elems = [dict(r) for r in c.fetchall()]
        by_id = {r["id"]: r for r in raw_elems}
        c.execute(
            "SELECT from_id, to_id FROM map_links WHERE map_id=? ORDER BY id ASC",
            (map_id,),
        )
        links = []
        for l in c.fetchall():
            a, b = by_id.get(l["from_id"]), by_id.get(l["to_id"])
            if a is None or b is None or a is b:
                continue
            links.append({"from": a["uid"], "to": b["uid"]})

        try:
            ping_map = dict(status_memory.items())
        except Exception:
            ping_map = {}
        try:
            maint_map = get_active_maintenance_map()
        except Exception:
            maint_map = {}

        elements = []
        for e in raw_elems:
            d = {
                "uid": e["uid"],
                "etype": e["etype"],
                "label": e["label"],
                "x": int(e["x"]),
                "y": int(e["y"]),
                "status": None,
                "maintenance": False,
            }
            if e["etype"] == "host":
                ip = e["ip"] or ""
                d["ip"] = ip
                pv = ping_map.get(ip)
                d["status"] = "unknown" if pv is None else ("up" if pv else "down")
                d["maintenance"] = ip in maint_map
            elements.append(d)

        hosts = []
        c.execute(
            "SELECT ip, alias FROM hosts ORDER BY alias COLLATE NOCASE ASC, ip ASC"
        )
        for r in c.fetchall():
            alias = (r["alias"] or "").strip()
            hosts.append({"ip": r["ip"], "alias": alias, "name": alias or r["ip"]})
    finally:
        conn.close()
    return jsonify(
        {
            "map": m,
            "elements": elements,
            "links": links,
            "hosts": hosts,
            "owner": owner,
            "settings": settings,
            "can_write": _map_can_write(
                owner, settings, current_user.username, _map_role()
            ),
        }
    )


@app.route("/api/maps/<int:map_id>", methods=["PATCH"])
@api_login_required
@limiter.limit("60/minute")
def api_maps_update(map_id):
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict) or not data:
        return jsonify({"error": "Tidak ada perubahan"}), 400
    sets, params = [], []
    if "name" in data:
        name = _map_valid_name(data.get("name"))
        if name is None:
            return jsonify({"error": "Nama 1-128 karakter"}), 400
        sets.append("name=?")
        params.append(name)
    if "width" in data:
        width = _map_valid_dim(data.get("width"))
        if width is None:
            return jsonify({"error": "Lebar 100-65535"}), 400
        sets.append("width=?")
        params.append(width)
    if "height" in data:
        height = _map_valid_dim(data.get("height"))
        if height is None:
            return jsonify({"error": "Tinggi 100-65535"}), 400
        sets.append("height=?")
        params.append(height)
    owner_update = None
    if "owner" in data:
        owner_update = _map_valid_owner(data.get("owner"))
        if owner_update is None:
            return jsonify({"error": "Owner wajib dipilih"}), 400
    settings_update = None
    has_settings = "settings" in data or any(
        key in data for key in _MAP_SETTINGS_DEFAULTS
    )
    if has_settings:
        try:
            settings_update = _map_parse_settings(_map_settings_payload(data))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
    if not sets and owner_update is None and settings_update is None:
        return jsonify({"error": "Tidak ada kolom yang dikenal"}), 400
    conn, c = get_db()
    try:
        c.execute("SELECT name FROM maps WHERE id=?", (map_id,))
        if c.fetchone() is None:
            return jsonify({"error": "Peta tidak ditemukan"}), 404
        current_owner, current_settings = _map_load_settings(c, map_id)
        if not _map_can_write(
            current_owner, current_settings, current_user.username, _map_role()
        ):
            return jsonify({"error": "Tidak punya akses mengubah peta ini"}), 403
        if "name" in data and _map_name_taken(c, name, map_id):
            return jsonify({"error": "Nama peta sudah dipakai"}), 400
        if sets:
            c.execute(
                f"UPDATE maps SET {', '.join(sets)} WHERE id=?", params + [map_id]
            )
        if owner_update is not None or settings_update is not None:
            owner = owner_update if owner_update is not None else current_owner
            if not owner:
                owner = str(current_user.username or DASHBOARD_USERNAME).strip()
            settings = current_settings
            if settings_update is not None:
                # Patch parsial: gabungkan dengan pengaturan yang sudah ada.
                settings = _map_parse_settings(
                    _map_settings_payload(data), current_settings
                )
            _map_save_settings(c, map_id, owner, settings)
        conn.commit()
        c.execute("SELECT id, name, width, height FROM maps WHERE id=?", (map_id,))
        r = c.fetchone()
        owner, settings = _map_load_settings(c, map_id)
        row = {
            "id": r["id"],
            "name": r["name"],
            "width": int(r["width"]),
            "height": int(r["height"]),
            "owner": owner,
            "settings": settings,
        }
    finally:
        conn.close()
    try:
        audit(current_user.username, "map.update", row["name"], f"maps/{map_id}")
    except Exception:
        pass
    return jsonify(row)


@app.route("/api/maps/<int:map_id>/content", methods=["PUT"])
@api_login_required
@limiter.limit("60/minute")
def api_maps_content(map_id):
    """Simpan isi kanvas: ganti seluruh elemen + tautan (uid stabil klien)."""
    data = request.get_json(silent=True) or {}
    try:
        elems, links = _map_parse_content(
            data.get("elements"),
            data.get("links") if isinstance(data.get("links"), list) else [],
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    conn, c = get_db()
    try:
        c.execute("SELECT id FROM maps WHERE id=?", (map_id,))
        if c.fetchone() is None:
            return jsonify({"error": "Peta tidak ditemukan"}), 404
        owner, settings = _map_load_settings(c, map_id)
        if not _map_can_write(owner, settings, current_user.username, _map_role()):
            return jsonify({"error": "Tidak punya akses mengubah peta ini"}), 403
        ne, nl = _map_insert_content(c, map_id, elems, links)
        conn.commit()
    finally:
        conn.close()
    try:
        audit(
            current_user.username,
            "map.content",
            f"{ne} elemen, {nl} tautan",
            f"maps/{map_id}",
        )
    except Exception:
        pass
    return jsonify({"elements": ne, "links": nl})


@app.route("/api/maps", methods=["DELETE"])
@api_login_required
@limiter.limit("60/minute")
def api_maps_delete():
    """Hapus peta terpilih (beserta elemen dan tautannya)."""
    data = request.get_json(silent=True) or {}
    raw = data.get("ids")
    if not isinstance(raw, list) or not raw:
        return jsonify({"error": "Tidak ada peta yang dipilih"}), 400
    if len(raw) > 200:
        return jsonify({"error": "Maksimal 200 peta per permintaan"}), 400
    ids, seen = [], set()
    for v in raw:
        try:
            n = int(v)
        except (TypeError, ValueError):
            return jsonify({"error": "ids berisi id tidak valid"}), 400
        if n not in seen:
            seen.add(n)
            ids.append(n)
    deleted, names = 0, []
    conn, c = get_db()
    try:
        for mid in ids:
            c.execute("SELECT name FROM maps WHERE id=?", (mid,))
            row = c.fetchone()
            if row is None:
                continue
            owner, settings = _map_load_settings(c, mid)
            if not _map_can_write(
                owner, settings, current_user.username, _map_role()
            ):
                return (
                    jsonify({"error": f"Tidak punya akses menghapus '{row['name']}'"}),
                    403,
                )
            c.execute("DELETE FROM map_elements WHERE map_id=?", (mid,))
            c.execute("DELETE FROM map_links WHERE map_id=?", (mid,))
            c.execute("DELETE FROM map_settings WHERE map_id=?", (mid,))
            c.execute("DELETE FROM maps WHERE id=?", (mid,))
            deleted += 1
            names.append(row["name"])
        conn.commit()
    finally:
        conn.close()
    if deleted:
        try:
            audit(
                current_user.username,
                "map.delete",
                ", ".join(names),
                f"{deleted} peta",
            )
        except Exception:
            pass
    return jsonify({"deleted": deleted})


@app.route("/api/maps/export", methods=["POST"])
@api_login_required
@limiter.limit("30/minute")
def api_maps_export():
    """Ekspor peta terpilih ke berkas JSON (round-trip dengan impor)."""
    data = request.get_json(silent=True) or {}
    raw = data.get("ids")
    if not isinstance(raw, list) or not raw:
        return jsonify({"error": "Pilih minimal satu peta"}), 400
    if len(raw) > 200:
        return jsonify({"error": "Maksimal 200 peta per permintaan"}), 400
    ids, seen = [], set()
    for v in raw:
        try:
            n = int(v)
        except (TypeError, ValueError):
            return jsonify({"error": "ids berisi id tidak valid"}), 400
        if n not in seen:
            seen.add(n)
            ids.append(n)
    conn, c = get_db()
    try:
        payload = {
            "version": 1,
            "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "maps": [],
        }
        for mid in ids:
            c.execute("SELECT name, width, height FROM maps WHERE id=?", (mid,))
            m = c.fetchone()
            if m is None:
                continue
            owner, settings = _map_load_settings(c, mid)
            if not _map_can_read(
                owner, settings, current_user.username, _map_role()
            ):
                continue
            c.execute(
                "SELECT uid, etype, label, ip, x, y FROM map_elements"
                " WHERE map_id=? ORDER BY id ASC",
                (mid,),
            )
            raw_elems = [dict(r) for r in c.fetchall()]
            id_to_uid = {
                r["id"]: r["uid"]
                for r in c.execute(
                    "SELECT id, uid FROM map_elements WHERE map_id=?", (mid,)
                ).fetchall()
            }
            c.execute(
                "SELECT from_id, to_id FROM map_links WHERE map_id=? ORDER BY id ASC",
                (mid,),
            )
            links = []
            for l in c.fetchall():
                a, b = id_to_uid.get(l["from_id"]), id_to_uid.get(l["to_id"])
                if not a or not b or a == b:
                    continue
                links.append({"from": a, "to": b})
            payload["maps"].append(
                {
                    "name": m["name"],
                    "width": int(m["width"]),
                    "height": int(m["height"]),
                    "owner": owner,
                    "settings": settings,
                    "elements": [
                        {
                            "uid": e["uid"],
                            "etype": e["etype"],
                            "label": e["label"],
                            "ip": e["ip"],
                            "x": int(e["x"]),
                            "y": int(e["y"]),
                        }
                        for e in raw_elems
                    ],
                    "links": links,
                }
            )
    finally:
        conn.close()
    body = json.dumps(payload, ensure_ascii=False, indent=2)
    resp = Response(body, mimetype="application/json")
    resp.headers["Content-Disposition"] = 'attachment; filename="maps_export.json"'
    return resp


@app.route("/api/maps/import", methods=["POST"])
@api_login_required
@require_role("admin", "operator")
@limiter.limit("30/minute")
def api_maps_import():
    """Impor JSON hasil ekspor: {version, maps:[name,width,elements,links]}.

    Tautan dengan uid yang tidak dikenal dilewati; selain itu elemen
    diterima apa adanya. Batas: 50 peta per permintaan.
    """
    data = request.get_json(silent=True) or {}
    raw_maps = data.get("maps")
    if not isinstance(raw_maps, list) or not raw_maps:
        return jsonify({"error": "Tidak ada peta untuk diimpor"}), 400
    if len(raw_maps) > 50:
        return jsonify({"error": "Maksimal 50 peta per permintaan"}), 400
    created, errs, seen_names = [], [], set()
    conn, c = get_db()
    try:
        for raw in raw_maps:
            if not isinstance(raw, dict):
                errs.append("format peta tidak valid")
                continue
            name = _map_valid_name(raw.get("name"))
            width = _map_valid_dim(raw.get("width", 960))
            height = _map_valid_dim(raw.get("height", 540))
            if name is None or width is None or height is None:
                errs.append(f"'{str(raw.get('name') or '')[:32]}' tidak valid")
                continue
            if name.lower() in seen_names or _map_name_taken(c, name):
                errs.append(f"'{name[:32]}': nama peta sudah dipakai")
                continue
            if "owner" in raw:
                owner = _map_valid_owner(raw.get("owner"))
            else:
                owner = str(current_user.username or DASHBOARD_USERNAME).strip()
            if owner is None:
                errs.append(f"'{name[:32]}': owner tidak valid")
                continue
            try:
                settings = _map_parse_settings(
                    raw.get("settings") if "settings" in raw else {}
                )
            except ValueError as exc:
                errs.append(f"'{name[:32]}': {exc}")
                continue
            try:
                elems, links = _map_parse_content(
                    raw.get("elements"),
                    raw.get("links") if isinstance(raw.get("links"), list) else [],
                )
            except ValueError as exc:
                errs.append(f"'{name[:32]}': {exc}")
                continue
            c.execute(
                "INSERT INTO maps (name, width, height) VALUES (?,?,?)",
                (name, width, height),
            )
            new_id = c.lastrowid
            seen_names.add(name.lower())
            _map_save_settings(c, new_id, owner, settings)
            _map_insert_content(c, new_id, elems, links)
            created.append({"id": new_id, "name": name})
        conn.commit()
    finally:
        conn.close()
    if created:
        try:
            audit(
                current_user.username,
                "map.import",
                ", ".join(d["name"] for d in created),
                f"{len(created)} peta",
            )
        except Exception:
            pass
    resp = {"created": created, "count": len(created)}
    if errs:
        resp["errors"] = errs
    if not created:
        return jsonify(resp), 400
    return jsonify(resp), 201


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
        if len(hosts_data) > 2000:
            return jsonify({"error": "Maksimal 2000 baris per impor"}), 400

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
            "fiber_rx_scale_min": get_setting("fiber_rx_scale_min", FIBER_RX_SCALE_MIN),
            "fiber_rx_scale_max": get_setting("fiber_rx_scale_max", FIBER_RX_SCALE_MAX),
            "fiber_tx_min": get_setting("fiber_tx_min", FIBER_TX_MIN),
            "fiber_tx_max": get_setting("fiber_tx_max", FIBER_TX_MAX),
            "fiber_degrade_db": get_setting("fiber_degrade_db", FIBER_DEGRADE_DB),
            "fiber_degrade_days": get_setting("fiber_degrade_days", FIBER_DEGRADE_DAYS),
            "fiber_stale_min": get_setting("fiber_stale_min", FIBER_STALE_MIN),
            "fiber_flap_flips": get_setting("fiber_flap_flips", FIBER_FLAP_FLIPS),
            "fiber_flap_hours": get_setting("fiber_flap_hours", FIBER_FLAP_HOURS),
            "fiber_parent_min": get_setting("fiber_parent_min", FIBER_PARENT_MIN),
            "fiber_trend_enabled": get_setting("fiber_trend_enabled", 1, type_cast=int),
            "fiber_trend_window_days": get_setting(
                "fiber_trend_window_days", 14, type_cast=int
            ),
            "fiber_trend_min_slope": get_setting(
                "fiber_trend_min_slope", 0.05, type_cast=float
            ),
            "fiber_trend_horizon_days": get_setting(
                "fiber_trend_horizon_days", 14, type_cast=int
            ),
            "snmp_trap_enabled": get_setting("snmp_trap_enabled", 1, type_cast=int),
            "snmp_trap_port": get_setting("snmp_trap_port", 1162, type_cast=int),
            "snmp_trap_community": get_setting(
                "snmp_trap_community", "", type_cast=str
            ),
            "notify_telegram_min_sev": get_setting(
                "notify_telegram_min_sev", "info", type_cast=str
            ),
            "notify_email_min_sev": get_setting(
                "notify_email_min_sev", "critical", type_cast=str
            ),
            "notify_webhook_min_sev": get_setting(
                "notify_webhook_min_sev", "warning", type_cast=str
            ),
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
            "discovery_last_scan": get_setting(
                "discovery_last_scan", "", type_cast=str
            ),
            "public_url": get_setting("public_url", "", type_cast=str),
        }
    )


FIBER_SETTING_RANGES = {
    "fiber_rx_overload": (-40.0, 10.0),
    "fiber_rx_warn": (-40.0, 10.0),
    "fiber_rx_crit": (-40.0, 10.0),
    "fiber_rx_target": (-40.0, 10.0),
    "fiber_rx_scale_min": (-80.0, 20.0),
    "fiber_rx_scale_max": (-80.0, 20.0),
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
    v = data.get("fiber_trend_enabled")
    if v is not None:
        try:
            v = 1 if int(float(v)) else 0
        except (ValueError, TypeError):
            return jsonify({"error": "fiber_trend_enabled harus 0 atau 1"}), 400
        vals["fiber_trend_enabled"] = v
    for key, lo, hi in (
        ("fiber_trend_window_days", 7, 30),
        ("fiber_trend_horizon_days", 3, 60),
    ):
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
    v = data.get("fiber_trend_min_slope")
    if v is not None:
        try:
            v = float(v)
        except (ValueError, TypeError):
            return (
                jsonify({"error": "fiber_trend_min_slope harus angka 0.01-1.0"}),
                400,
            )
        if not 0.01 <= v <= 1.0:
            return (
                jsonify({"error": "fiber_trend_min_slope harus 0.01-1.0 dB/hari"}),
                400,
            )
        vals["fiber_trend_min_slope"] = v
    v = data.get("snmp_trap_enabled")
    if v is not None:
        try:
            v = 1 if int(float(v)) else 0
        except (ValueError, TypeError):
            return jsonify({"error": "snmp_trap_enabled harus 0 atau 1"}), 400
        vals["snmp_trap_enabled"] = v
    v = data.get("snmp_trap_port")
    if v is not None:
        try:
            v = int(float(v))
        except (ValueError, TypeError):
            return jsonify({"error": "snmp_trap_port harus angka 1024-65535"}), 400
        if not 1024 <= v <= 65535:
            return jsonify({"error": "snmp_trap_port harus 1024-65535"}), 400
        vals["snmp_trap_port"] = v
    v = data.get("snmp_trap_community")
    if v is not None:
        v = str(v).strip()[:NMS_SNMP_TRAP_COMMUNITY_MAX]
        vals["snmp_trap_community"] = v
    for key in (
        "notify_telegram_min_sev",
        "notify_email_min_sev",
        "notify_webhook_min_sev",
    ):
        v = data.get(key)
        if v is None:
            continue
        v = str(v).strip().lower()
        if v not in ("info", "warning", "critical"):
            return (
                jsonify({"error": f"{key} harus info, warning, atau critical"}),
                400,
            )
        vals[key] = v
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
        vals["discovery_network"] = _disc_normalize_network(network)
    if "discovery_community" in data:
        community = str(data.get("discovery_community") or "").strip()
        if len(community) > 128:
            return jsonify({"error": "discovery_community maksimal 128 karakter"}), 400
        vals["discovery_community"] = community
    if "public_url" in data:
        from urllib.parse import urlparse as _urlparse

        public_url = str(data.get("public_url") or "").strip().rstrip("/")
        if public_url:
            try:
                _parsed = _urlparse(public_url)
            except Exception:
                return jsonify({"error": "public_url tidak valid"}), 400
            if (_parsed.scheme or "").lower() not in (
                "http",
                "https",
            ) or not (_parsed.hostname or ""):
                return (
                    jsonify(
                        {
                            "error": "public_url harus URL http(s) valid, mis. http://192.168.1.5:5000"
                        }
                    ),
                    400,
                )
            if len(public_url) > 256:
                return jsonify({"error": "public_url maksimal 256 karakter"}), 400
        vals["public_url"] = public_url
    merged = {
        k: get_setting(k, d)
        for k, d in [
            ("fiber_rx_overload", FIBER_RX_OVERLOAD),
            ("fiber_rx_warn", FIBER_RX_WARN),
            ("fiber_rx_crit", FIBER_RX_CRIT),
            ("fiber_rx_target", FIBER_RX_TARGET),
            ("fiber_rx_scale_min", FIBER_RX_SCALE_MIN),
            ("fiber_rx_scale_max", FIBER_RX_SCALE_MAX),
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
    if not (merged["fiber_rx_scale_min"] < merged["fiber_rx_scale_max"]):
        return jsonify({"error": "Skala Rx harus min < max (mis. -35 s/d 0 dBm)."}), 400
    if not (
        merged["fiber_rx_scale_min"]
        <= merged["fiber_rx_crit"]
        < merged["fiber_rx_warn"]
        <= merged["fiber_rx_overload"]
        <= merged["fiber_rx_scale_max"]
    ):
        return jsonify({"error": "Ambang Rx harus berada di dalam rentang skala."}), 400
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


# ---- 2FA TOTP (login web) ----------------------------------------------------


@app.route("/api/2fa/setup", methods=["POST"])
@api_login_required
@limiter.limit("5/minute")
def api_2fa_setup():
    """Buat secret TOTP baru (belum aktif). Wajib password saat ini."""
    import io
    import pyotp
    import segno

    data = request.get_json(silent=True) or {}
    cur = str(data.get("current_password") or "")[:200]
    if not cur:
        return jsonify({"error": "Password saat ini wajib diisi"}), 400
    if not _verify_admin(current_user.username, cur):
        return jsonify({"error": "Password saat ini salah"}), 400

    username = current_user.username
    secret = pyotp.random_base32()
    twofa.ensure_2fa_row(username)
    twofa.store_pending_secret(username, secret)

    otpauth = pyotp.TOTP(secret).provisioning_uri(
        name=username, issuer_name="NMS KOPEGTEL"
    )
    buf = io.BytesIO()
    segno.make(otpauth, error="m").save(
        buf, kind="svg", scale=5, border=2, dark="#10131a", light="#ffffff"
    )
    qr_svg = buf.getvalue().decode("utf-8")
    try:
        audit(username, "auth.2fa.setup", "")
    except Exception:
        pass
    return jsonify({"secret": secret, "otpauth_uri": otpauth, "qr_svg": qr_svg})


@app.route("/api/2fa/enable", methods=["POST"])
@api_login_required
@limiter.limit("10/minute")
def api_2fa_enable():
    """Aktifkan 2FA dengan kode valid dari secret setup; keluarkan recovery codes."""
    from nms.twofa import generate_recovery_codes, matched_totp_step

    username = current_user.username
    stored = twofa.state(username)["secret"]
    if not stored:
        return jsonify({"error": "Jalankan setup 2FA terlebih dahulu"}), 400

    data = request.get_json(silent=True) or {}
    step = matched_totp_step(stored, data.get("code", ""))
    if step is None:
        return jsonify({"error": "Kode TOTP salah"}), 400

    codes, hashes = generate_recovery_codes()
    twofa.replace_secret_and_recover(username, stored, hashes, last_step=step)
    try:
        audit(username, "auth.2fa.enabled", "")
    except Exception:
        pass
    try:
        send_alert(
            f"🔐 2FA DIAKTIFKAN untuk {username} di NMS KOPEGTEL",
            severity="warning",
            event="auth.2fa.enabled",
        )
    except Exception:
        pass
    return jsonify({"status": "success", "recovery_codes": codes})


@app.route("/api/2fa/disable", methods=["POST"])
@api_login_required
@limiter.limit("5/minute")
def api_2fa_disable():
    """Nonaktifkan 2FA. Wajib password DAN kode TOTP/recovery code."""
    username = current_user.username
    data = request.get_json(silent=True) or {}
    cur = str(data.get("current_password") or "")[:200]
    code = str(data.get("code") or "")[:32]
    if not cur:
        return jsonify({"error": "Password saat ini wajib diisi"}), 400
    if not code:
        return jsonify({"error": "Kode TOTP atau recovery code wajib diisi"}), 400
    if not _verify_admin(username, cur):
        return jsonify({"error": "Password saat ini salah"}), 400
    if not twofa.state(username)["enabled"]:
        return jsonify({"error": "2FA belum aktif"}), 400

    ok, _reason = twofa.consume_recovery_or_totp(username, code)
    if not ok:
        twofa.record_2fa_fail(username, time.time())
        try:
            audit(username, "auth.2fa.failed", "disable salah")
        except Exception:
            pass
        return jsonify({"error": "Kode salah"}), 400

    twofa.disable(username)
    twofa.clear_2fa_failures(username)
    try:
        audit(username, "auth.2fa.disabled", "")
    except Exception:
        pass
    try:
        send_alert(
            f"🔓 2FA DINONAKTIFKAN untuk {username} di NMS KOPEGTEL",
            severity="warning",
            event="auth.2fa.disabled",
        )
    except Exception:
        pass
    return jsonify({"status": "success"})


@app.route("/api/2fa/status", methods=["GET"])
@api_login_required
@limiter.limit("60/minute")
def api_2fa_status():
    username = current_user.username
    return jsonify(
        {
            "enabled": twofa.state(username)["enabled"],
            "recovery_left": twofa.recovery_left(username),
        }
    )


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


def _prometheus_label(value):
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


@app.route("/metrics")
@api_login_required
def prometheus_metrics():
    conn, c = get_db()
    dashboard_rows = []
    lines = [
        "# HELP nms_hosts_total Number of configured hosts.",
        "# TYPE nms_hosts_total gauge",
        f"nms_hosts_total {len(get_target_hosts())}",
        "# HELP nms_host_latency_ms Latest host latency in milliseconds.",
        "# TYPE nms_host_latency_ms gauge",
        "# HELP nms_host_up Host reachability, 1 when reachable.",
        "# TYPE nms_host_up gauge",
        "# HELP nms_agent_cpu_percent Latest agent CPU percentage.",
        "# TYPE nms_agent_cpu_percent gauge",
        "# HELP nms_agent_ram_percent Latest agent RAM percentage.",
        "# TYPE nms_agent_ram_percent gauge",
    ]
    for host in get_target_hosts():
        label = _prometheus_label(host)
        c.execute(
            "SELECT latency FROM ping_logs WHERE host=? ORDER BY id DESC LIMIT 1",
            (host,),
        )
        ping = c.fetchone()
        latency = ping["latency"] if ping and ping["latency"] != -1 else 0
        up = 1 if ping and ping["latency"] != -1 else 0
        c.execute(
            "SELECT cpu_percent, ram_percent FROM agent_metrics WHERE host=? ORDER BY id DESC LIMIT 1",
            (host,),
        )
        agent = c.fetchone()
        dashboard_rows.append(
            {
                "host": host,
                "latency": latency,
                "up": up,
                "cpu": (
                    agent["cpu_percent"]
                    if agent and agent["cpu_percent"] is not None
                    else None
                ),
                "ram": (
                    agent["ram_percent"]
                    if agent and agent["ram_percent"] is not None
                    else None
                ),
            }
        )
        lines.extend(
            [
                f'nms_host_latency_ms{{host="{label}"}} {latency or 0}',
                f'nms_host_up{{host="{label}"}} {up}',
                f'nms_agent_cpu_percent{{host="{label}"}} {(agent["cpu_percent"] if agent and agent["cpu_percent"] is not None else 0)}',
                f'nms_agent_ram_percent{{host="{label}"}} {(agent["ram_percent"] if agent and agent["ram_percent"] is not None else 0)}',
            ]
        )
    conn.close()
    if request.args.get("format") == "json":
        return jsonify({"hosts": dashboard_rows, "total": len(dashboard_rows)})
    if request.args.get("view") == "ui":
        return render_template("metrics.html", metrics=dashboard_rows)
    return Response("\\n".join(lines) + "\\n", mimetype="text/plain; version=0.0.4")


@app.route("/api/telemetry/stream")
@api_login_required
def telemetry_stream():
    def events():
        while True:
            conn, c = get_db()
            payload = []
            for host in get_target_hosts():
                c.execute(
                    "SELECT latency, timestamp FROM ping_logs WHERE host=? ORDER BY id DESC LIMIT 1",
                    (host,),
                )
                row = c.fetchone()
                if row:
                    payload.append(
                        {
                            "host": host,
                            "latency": row["latency"],
                            "timestamp": row["timestamp"],
                        }
                    )
            conn.close()
            yield f"event: telemetry\\ndata: {json.dumps(payload)}\\n\\n"
            time.sleep(10)

    return Response(
        stream_with_context(events()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


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


@app.route("/api/notify/test", methods=["POST"])
@api_login_required
@require_role("admin")
@limiter.limit("5/minute")
def api_notify_test():
    """Kirim pesan uji ke SEMUA channel aktif, lapor hasil per channel."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    results = send_alert(
        f"🔔 *PESAN UJI NOTIFIKASI*\nSumber : NMS KOPEGTEL\nWaktu  : {ts}",
        severity="critical",
        event="notify.test",
    )
    out = {}
    for name, active in channel_status().items():
        out[name] = {
            "active": active,
            "result": results.get(name, "skipped"),
        }
    try:
        audit(
            current_user.username,
            "notify.test",
            " ".join(f"{k}={v['result']}" for k, v in out.items()),
        )
    except Exception:
        pass
    return jsonify({"results": out})


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


@app.route("/api/users/<username>/2fa/reset", methods=["POST"])
@api_login_required
@require_role("admin")
@limiter.limit("10/minute")
def api_users_2fa_reset(username):
    """Admin mereset 2FA user lain (darurat perangkat hilang).

    Admin TIDAK boleh mereset dirinya lewat endpoint ini — pakai
    /api/2fa/disable (butuh password + kode) supaya lebih aman.
    """
    username = str(username or "").strip()[:50]
    if not username:
        return jsonify({"error": "Username wajib diisi"}), 400
    if username == current_user.username:
        return (
            jsonify(
                {
                    "error": "Gunakan /api/2fa/disable untuk menonaktifkan 2FA akun sendiri"
                }
            ),
            400,
        )
    if get_user(username) is None:
        return jsonify({"error": "User tidak ditemukan"}), 404

    if not twofa.disable(username):
        return jsonify({"error": "Gagal mereset 2FA"}), 500
    try:
        audit(
            current_user.username,
            "auth.2fa.reset",
            f"target={username}",
            target=username,
        )
    except Exception:
        pass
    try:
        send_alert(
            f"🔁 2FA user {username} direset oleh admin {current_user.username}",
            severity="warning",
            event="auth.2fa.reset",
        )
    except Exception:
        pass
    return jsonify({"status": "success"})


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


@app.route("/api/reports/scheduled/bulk", methods=["POST"])
@api_login_required
@require_role("admin")
@limiter.limit("20/minute")
def api_reports_scheduled_bulk():

    if not getattr(scheduler, "running", False):
        return jsonify({"error": "Penjadwal sedang tidak aktif."}), 409

    data = request.get_json(silent=True) or {}
    action = str(data.get("action") or "").strip().lower()
    job_ids = data.get("ids")
    if action not in ("enable", "disable"):
        return jsonify({"error": "Aksi tidak valid."}), 400
    if not isinstance(job_ids, list) or not job_ids:
        return jsonify({"error": "Pilih minimal satu laporan."}), 400

    updated = []
    for job_id in job_ids[:50]:
        if job_id not in _REPORT_JOB_META:
            continue
        if scheduler.get_job(job_id) is None:
            continue
        try:
            if action == "enable":
                scheduler.resume_job(job_id)
            else:
                scheduler.pause_job(job_id)
            updated.append(job_id)
        except Exception as exc:
            logger.warning("Gagal %s job laporan %s: %s", action, job_id, exc)

    if updated:
        try:
            audit(
                current_user.username,
                f"report.scheduled.{action}",
                ",".join(updated),
            )
        except Exception:
            pass

    return jsonify({"status": "success", "updated": updated}), 200


@app.route("/api/reports/scheduled/create", methods=["POST"])
@api_login_required
@require_role("admin")
@limiter.limit("20/minute")
def api_reports_scheduled_create():
    data = request.get_json(silent=True) or {}
    name = str(data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Report name is required."}), 400
    owner = str(data.get("owner") or current_user.username or "Admin").strip()
    period = str(data.get("period") or "Previous day").strip()
    cycle = str(data.get("cycle") or "Daily").strip()

    try:
        audit(current_user.username, "report.scheduled.create", f"name={name}, cycle={cycle}")
    except Exception:
        pass

    return jsonify({"status": "success", "message": f"Report '{name}' added successfully."}), 200


@app.route("/reports/scheduled/create")
@login_required
def reports_scheduled_create_alias():
    return redirect(url_for("reports_page", page="scheduled", action="create"))


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


def _svc_target(svc):
    svc_type = (svc.get("type") or "").lower()
    if svc_type == "http":
        return svc.get("url") or ""
    if svc_type == "tcp":
        port = svc.get("port")
        return f"{svc.get('ip') or ''}:{port}" if port else (svc.get("ip") or "")
    if svc_type == "dns":
        query = svc.get("query") or ""
        return f"{query} @ {svc.get('ip')}" if query else (svc.get("ip") or "")
    return svc.get("ip") or ""


def _svc_availability(online, offline):
    total = (online or 0) + (offline or 0)
    if total <= 0:
        return None
    return round((online or 0) / total * 100, 2)


def _svc_agg(rows):
    checks = len(rows)
    online = sum(1 for r in rows if (r.get("status") or "").upper() == "ONLINE")
    offline = sum(1 for r in rows if (r.get("status") or "").upper() == "OFFLINE")
    pending = checks - online - offline
    latencies = [
        float(r.get("latency"))
        for r in rows
        if (r.get("status") or "").upper() == "ONLINE" and r.get("latency") is not None
    ]
    return {
        "checks": checks,
        "online": online,
        "offline": offline,
        "pending": pending,
        "availability": _svc_availability(online, offline),
        "avg_latency": round(sum(latencies) / len(latencies), 2) if latencies else None,
    }


def _parse_svc_timestamp(value):
    try:
        return datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


try:
    SERVICE_STALE_SECONDS = max(
        60, int(os.environ.get("SERVICE_STALE_SECONDS", "180"))
    )
except (TypeError, ValueError):
    SERVICE_STALE_SECONDS = 180


def _service_last_checked_age(value):
    ts = _parse_svc_timestamp(value)
    if ts is None:
        return None
    return max(0, int((datetime.now() - ts).total_seconds()))


def _sla_bucket_plan(hours):
    hours = max(1, int(hours))
    if hours <= 24:
        return 3600, hours
    if hours <= 72:
        return 3600, hours
    if hours <= 168:
        return 21600, 28
    return 86400, min(max(1, hours // 24), 31)


def _sla_timeline(rows, hours):
    bucket_seconds, bucket_count = _sla_bucket_plan(hours)
    now = datetime.now()
    start = now - timedelta(hours=hours)
    buckets = [{"online": 0, "offline": 0} for _ in range(bucket_count)]
    for row in rows:
        ts = _parse_svc_timestamp(row.get("timestamp"))
        if not ts:
            continue
        idx = int((ts - start).total_seconds() // bucket_seconds)
        if idx < 0 or idx >= bucket_count:
            continue
        status = (row.get("status") or "").upper()
        if status == "ONLINE":
            buckets[idx]["online"] += 1
        elif status == "OFFLINE":
            buckets[idx]["offline"] += 1
    labels = []
    values = []
    for i in range(bucket_count):
        bucket_end = start + timedelta(seconds=(i + 1) * bucket_seconds)
        if bucket_seconds >= 86400:
            labels.append(bucket_end.strftime("%d/%m"))
        elif bucket_seconds >= 3600 and hours > 24:
            labels.append(bucket_end.strftime("%d/%m %H:%M"))
        else:
            labels.append(bucket_end.strftime("%H:%M"))
        values.append(_svc_availability(buckets[i]["online"], buckets[i]["offline"]))
    return {"labels": labels, "values": values}


@app.route("/services")
@login_required
def services_page():
    return render_template("services.html")


@app.route("/services/sla")
@login_required
def services_sla_page():
    return render_template("services_sla.html")


@app.route("/services/sla/report")
@login_required
def services_sla_report_page():
    return render_template("services_sla_report.html")


# ============================================================
# Inventory — aset & field inventory host (kolom host_inventory)
# ============================================================
# Field yang boleh dikelompokkan di halaman Inventory > Overview.
INVENTORY_OVERVIEW_FIELDS = (
    ("vendor", "Vendor"),
    ("type", "Type"),
    ("model", "Model"),
    ("os", "OS"),
    ("hardware", "Hardware"),
    ("software", "Software"),
    ("location", "Location"),
    ("site_rack", "Rack"),
    ("asset_tag", "Asset tag"),
    ("serialno_a", "Serial number"),
    ("deployment_status", "Deployment status"),
    ("installer_name", "Installer"),
    ("contact", "Contact"),
    ("poc_1_name", "PIC"),
)
INVENTORY_EMPTY_LABEL = "(belum diisi)"

# Kolom dan pilihan filter pada halaman Inventory > Hosts (gaya Zabbix).
INVENTORY_HOST_FIELDS = (
    ("alias", "Alias"),
    ("name", "Name"),
    ("type", "Type"),
    ("os", "OS"),
    ("serialno_a", "Serial number A"),
    ("tag", "Tag"),
    ("macaddress_a", "MAC address A"),
    ("vendor", "Vendor"),
    ("model", "Model"),
    ("location", "Location"),
    ("site_rack", "Rack"),
    ("asset_tag", "Asset tag"),
    ("serialno_b", "Serial number B"),
    ("macaddress_b", "MAC address B"),
    ("hardware", "Hardware"),
    ("software", "Software"),
    ("contact", "Contact"),
    ("deployment_status", "Deployment status"),
    ("poc_1_name", "PIC"),
)
INVENTORY_FILTER_OPERATORS = (
    "contains",
    "equals",
    "does_not_contain",
    "does_not_equal",
    "exists",
    "does_not_exist",
)
# Field yang bisa diubah lewat dialog Edit inventory di Inventory > Hosts.
INVENTORY_EDIT_FIELDS = ("name", "type", "os", "serialno_a", "tag", "macaddress_a")


def _inventory_field_value(host, field):
    """Nilai yang dipakai filter Field pada halaman Inventory > Hosts."""
    if field == "alias":
        return host.get("alias") or ""
    return host.get("fields", {}).get(field) or ""


def _inventory_filter_match(value, operator, wanted):
    """Cocokkan satu nilai inventory dengan operator filter Zabbix."""
    value_l = str(value or "").strip().lower()
    wanted_l = str(wanted or "").strip().lower()
    if operator == "exists":
        return bool(value_l)
    if operator == "does_not_exist":
        return not value_l
    if not wanted_l:
        return True
    if operator == "equals":
        return value_l == wanted_l
    if operator == "does_not_equal":
        return value_l != wanted_l
    if operator == "does_not_contain":
        return wanted_l not in value_l
    return wanted_l in value_l


@app.route("/inventory")
@login_required
def inventory_page():
    return render_template("inventory.html")


@app.route("/inventory/hosts")
@login_required
def inventory_hosts_page():
    return render_template("inventory_hosts.html")


def _inventory_hosts():
    """Ambil host aktif bersama field inventory-nya (JSON host_inventory)."""
    conn, c = get_db()
    c.execute(
        "SELECT id, ip, alias, category, host_inventory FROM hosts ORDER BY ip ASC"
    )
    rows = []
    for r in c.fetchall():
        inv = _decode_host_inventory(r["host_inventory"])
        fields = inv["fields"]
        rows.append(
            {
                "id": r["id"],
                "ip": r["ip"] or "",
                "alias": r["alias"] or "",
                "name": fields.get("name") or (r["alias"] or r["ip"] or ""),
                "category": r["category"] or "Uncategorized",
                "mode": inv["mode"],
                "fields": fields,
            }
        )
    conn.close()
    return rows


def _inventory_filter_options(hosts):
    opts = {key: set() for key in ("vendor", "type", "location", "deployment_status")}
    for host in hosts:
        for key, bucket in opts.items():
            value = host["fields"].get(key)
            if value:
                bucket.add(value)
    return {key: sorted(values, key=str.lower) for key, values in opts.items()}


@app.route("/api/inventory/overview", methods=["GET"])
@api_login_required
@limiter.limit("120/minute")
def api_inventory_overview():
    """Host inventory overview: hitungan host per nilai field inventory.

    ``group_by`` kosong berarti "not selected" — seperti Zabbix, tabel
    dibiarkan kosong sampai pengguna memilih field. ``groups`` menyaring
    host berdasarkan kategori/group-nya (dipisah koma).
    """
    labels = dict(INVENTORY_OVERVIEW_FIELDS)
    group_by = (request.args.get("group_by") or "").strip().lower()
    if group_by not in labels:
        group_by = ""
    group_filter = [
        item.strip().lower()
        for item in (request.args.get("groups") or "").split(",")
        if item.strip()
    ]

    all_hosts = _inventory_hosts()
    host_groups = sorted({host["category"] for host in all_hosts}, key=str.lower)
    hosts = (
        [h for h in all_hosts if h["category"].lower() in group_filter]
        if group_filter
        else all_hosts
    )
    counts = {}
    unique = {"vendor": set(), "type": set(), "location": set()}
    inventory_on = 0
    filled_hosts = 0
    filled_fields = 0
    for host in hosts:
        fields = host["fields"]
        if host["mode"] != "disabled":
            inventory_on += 1
        if fields:
            filled_hosts += 1
            filled_fields += len(fields)
        for key, bucket in unique.items():
            value = fields.get(key)
            if value:
                bucket.add(value)
        if not group_by:
            continue
        value = fields.get(group_by) or INVENTORY_EMPTY_LABEL
        counts[value] = counts.get(value, 0) + 1

    groups = [
        {"value": value, "count": count, "empty": value == INVENTORY_EMPTY_LABEL}
        for value, count in counts.items()
    ]
    groups.sort(key=lambda g: (g["empty"], -g["count"], g["value"].lower()))

    return jsonify(
        {
            "summary": {
                "hosts_total": len(hosts),
                "hosts_with_inventory": inventory_on,
                "hosts_filled": filled_hosts,
                "fields_filled": filled_fields,
                "vendors": len(unique["vendor"]),
                "locations": len(unique["location"]),
                "types": len(unique["type"]),
            },
            "group_by": group_by,
            "fields": [
                {"key": key, "label": label} for key, label in INVENTORY_OVERVIEW_FIELDS
            ],
            "groups": groups,
            "host_groups": host_groups,
        }
    )


@app.route("/api/inventory/hosts", methods=["GET"])
@api_login_required
@limiter.limit("120/minute")
def api_inventory_hosts():
    """Daftar host beserta field inventory, dengan filter gaya Zabbix."""
    q = (request.args.get("q") or "").strip().lower()
    filters = {
        key: (request.args.get(key) or "").strip().lower()
        for key in ("vendor", "type", "location", "deployment_status")
    }
    groups = [
        item.strip().lower()
        for item in (request.args.get("groups") or "").split(",")
        if item.strip()
    ]
    field_labels = dict(INVENTORY_HOST_FIELDS)
    field = (request.args.get("field") or "alias").strip().lower()
    if field not in field_labels:
        field = "alias"
    operator = (request.args.get("operator") or "contains").strip().lower()
    if operator not in INVENTORY_FILTER_OPERATORS:
        operator = "contains"
    filter_value = (request.args.get("value") or "").strip()

    hosts = _inventory_hosts()
    options = _inventory_filter_options(hosts)
    host_groups = sorted({host["category"] for host in hosts}, key=str.lower)
    rows = []
    for host in hosts:
        fields = host["fields"]
        if groups and host["category"].lower() not in groups:
            continue
        if q and q not in " ".join((host["name"], host["ip"], host["alias"])).lower():
            continue
        if any(
            (fields.get(key) or "").lower() != wanted
            for key, wanted in filters.items()
            if wanted
        ):
            continue
        if not _inventory_filter_match(
            _inventory_field_value(host, field), operator, filter_value
        ):
            continue
        rows.append(
            {
                "id": host["id"],
                "name": host["name"],
                "ip": host["ip"],
                "alias": host["alias"],
                "category": host["category"],
                "mode": host["mode"],
                "url": "/host/" + host["ip"],
                "fields": fields,
                "filled": len(fields),
            }
        )

    return jsonify(
        {
            "rows": rows,
            "total": len(hosts),
            "matched": len(rows),
            "options": options,
            "host_groups": host_groups,
            "fields": [
                {"key": key, "label": label} for key, label in INVENTORY_HOST_FIELDS
            ],
        }
    )


@app.route("/api/hosts/<path:ip>/inventory", methods=["PATCH"])
@api_login_required
@limiter.limit("60/minute")
def api_update_host_inventory(ip):
    """Simpan field inventory yang diedit dari halaman Inventory > Hosts."""
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({"error": "Format inventory tidak valid"}), 400
    raw_fields = data.get("fields")
    if not isinstance(raw_fields, dict):
        return jsonify({"error": "Format inventory tidak valid"}), 400

    parsed, err = _parse_host_inventory(
        {"inventory": {"mode": "manual", "fields": raw_fields}}
    )
    if err:
        return jsonify({"error": err}), 400
    assert parsed is not None
    incoming = parsed["fields"]

    conn, c = get_db()
    try:
        c.execute("SELECT id, host_inventory FROM hosts WHERE ip=?", (ip,))
        row = c.fetchone()
        if not row:
            return jsonify({"error": "Host tidak ditemukan"}), 404

        current = _decode_host_inventory(row["host_inventory"])
        merged = dict(current["fields"])
        for key in INVENTORY_EDIT_FIELDS:
            if key not in raw_fields:
                continue
            if key in incoming:
                merged[key] = incoming[key]
            else:
                merged.pop(key, None)
        mode = "manual" if merged else "disabled"
        payload = json.dumps({"mode": mode, "fields": merged}, ensure_ascii=False)
        c.execute(
            "UPDATE hosts SET host_inventory=? WHERE id=?",
            (payload, row["id"]),
        )
        conn.commit()
    finally:
        conn.close()

    try:
        audit(
            current_user.username,
            "host.inventory_update",
            f"{ip} fields={','.join(sorted(incoming))}",
        )
    except Exception:
        pass
    return jsonify({"status": "success", "mode": mode, "fields": merged})


@app.route("/api/services", methods=["GET"])
@api_login_required
def get_services_api():
    try:
        hours = int(request.args.get("hours", 24))
    except (ValueError, TypeError):
        hours = 24
    hours = max(1, min(hours, 720))
    conn, c = get_db()
    try:
        c.execute("SELECT * FROM services ORDER BY sort_order ASC, id ASC")
        rows = [dict(r) for r in c.fetchall()]
        stats = {}
        try:
            c.execute(
                """
                SELECT service_id,
                       COUNT(*) AS checks,
                       SUM(CASE WHEN status='ONLINE' THEN 1 ELSE 0 END) AS online,
                       SUM(CASE WHEN status='OFFLINE' THEN 1 ELSE 0 END) AS offline,
                       AVG(CASE WHEN status='ONLINE' THEN latency END) AS avg_latency
                FROM service_history
                WHERE timestamp > datetime('now','localtime',?)
                GROUP BY service_id
                """,
                (f"-{hours} hours",),
            )
            for r in c.fetchall():
                stats[r["service_id"]] = dict(r)
        except sqlite3.OperationalError:
            stats = {}
    finally:
        conn.close()
    by_id = {int(svc["id"]): svc for svc in rows}
    child_counts = {}
    for svc in rows:
        parent_id = svc.get("parent_id")
        if parent_id:
            child_counts[int(parent_id)] = child_counts.get(int(parent_id), 0) + 1

    out = []
    for svc in rows:
        st = stats.get(svc["id"], {})
        online = st.get("online") or 0
        offline = st.get("offline") or 0
        svc["target"] = _svc_target(svc)
        svc["checks"] = st.get("checks") or 0
        svc["availability"] = _svc_availability(online, offline)
        age = _service_last_checked_age(svc.get("last_checked"))
        svc["last_checked_age"] = age
        svc["stale"] = bool(
            age is not None
            and age > SERVICE_STALE_SECONDS
            and str(svc.get("status") or "").upper() in ("ONLINE", "OFFLINE")
        )
        svc["avg_latency"] = (
            round(st["avg_latency"], 2) if st.get("avg_latency") is not None else None
        )
        svc["ssl_level"] = _ssl_level(svc.get("ssl_days_left"))
        svc["problem_tags"] = svc.get("problem_tags") or ""
        svc["sort_order"] = svc.get("sort_order") or 0
        svc["status_rule"] = svc.get("status_rule") or "most_critical_child"
        svc["description"] = svc.get("description") or ""
        svc["propagation_rule"] = svc.get("propagation_rule") or "as_is"
        svc["weight"] = svc.get("weight") or 0
        svc["additional_rules"] = svc.get("additional_rules") or "[]"
        svc["child_count"] = child_counts.get(int(svc["id"]), 0)
        parent = by_id.get(svc.get("parent_id"))
        svc["parent_name"] = parent["name"] if parent else ""
        out.append(svc)
    return jsonify(out)


SERVICE_STATUS_RULES = {
    "most_critical_child",
    "most_critical_all",
    "all_children_problem",
    "any_child_problem",
}
SERVICE_PROPAGATION_RULES = {"as_is", "increase", "decrease", "ignore", "fixed"}


def _normalize_service_tag_list(raw, label):
    """Normalisasi daftar tag (list atau string dipisah koma)."""
    if isinstance(raw, (list, tuple)):
        items = raw
    else:
        items = str(raw or "").split(",")
    tags = []
    seen = set()
    for item in items:
        tag = re.sub(r"\s*:\s*", ": ", str(item or "").strip())
        if not tag:
            continue
        if len(tag) > 64:
            return None, f"{label} terlalu panjang (maks 64 karakter)"
        if tag.casefold() in seen:
            continue
        seen.add(tag.casefold())
        tags.append(tag)
    if len(tags) > 20:
        return None, f"Maksimal 20 {label.lower()} per service"
    text = ", ".join(tags)
    if len(text) > 500:
        return None, f"Total {label.lower()} terlalu panjang (maks 500)"
    return text, None


def _normalize_additional_rules(raw):
    """Normalisasi additional rules menjadi list teks."""
    if raw in (None, ""):
        return [], None
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            parsed = [raw]
        items = parsed if isinstance(parsed, list) else [parsed]
    elif isinstance(raw, (list, tuple)):
        items = raw
    else:
        return None, "Additional rules tidak valid"
    rules = []
    seen = set()
    for item in items:
        rule = str(item or "").strip()
        if not rule:
            continue
        if len(rule) > 128:
            return None, "Additional rule terlalu panjang (maks 128 karakter)"
        if rule.casefold() in seen:
            continue
        seen.add(rule.casefold())
        rules.append(rule)
    if len(rules) > 50:
        return None, "Maksimal 50 additional rules per service"
    return rules, None


def _validate_service_fields(data):
    """Validasi payload service dan kembalikan field yang sudah dinormalisasi."""
    ip = str(data.get("ip") or "").strip()
    name = str(data.get("name") or "").strip()
    svc_type = str(data.get("type") or "tcp").strip().lower()
    port_raw = data.get("port")
    url = str(data.get("url") or "").strip()
    query = str(data.get("query") or "").strip()
    expected = str(data.get("expected") or "").strip()

    tags_text, error = _normalize_service_tag_list(data.get("tags"), "Tag")
    if error:
        return None, error
    problem_tags_text, error = _normalize_service_tag_list(
        data.get("problem_tags"), "Problem tag"
    )
    if error:
        return None, error

    try:
        sort_order = int(data.get("sort_order", 0) or 0)
    except (ValueError, TypeError):
        return None, "Sort order harus angka 0-999"
    if not 0 <= sort_order <= 999:
        return None, "Sort order harus angka 0-999"

    try:
        weight = int(data.get("weight", 0) or 0)
    except (ValueError, TypeError):
        return None, "Weight harus angka 0-1000000"
    if not 0 <= weight <= 1000000:
        return None, "Weight harus angka 0-1000000"

    status_rule = str(data.get("status_rule") or "most_critical_child").strip().lower()
    if status_rule not in SERVICE_STATUS_RULES:
        return None, "Status calculation rule tidak valid"

    propagation_rule = str(data.get("propagation_rule") or "as_is").strip().lower()
    if propagation_rule not in SERVICE_PROPAGATION_RULES:
        return None, "Status propagation rule tidak valid"

    description = str(data.get("description") or "").strip()
    if len(description) > 2048:
        return None, "Description terlalu panjang (maks 2048)"

    parent_id = None
    parent_raw = data.get("parent_id")
    if parent_raw not in (None, "", 0, "0"):
        try:
            parent_id = int(parent_raw)
        except (ValueError, TypeError):
            return None, "Parent service tidak valid"
        if parent_id <= 0:
            return None, "Parent service tidak valid"

    child_ids = None
    if "child_ids" in data:
        raw_children = data.get("child_ids")
        if raw_children in (None, ""):
            child_ids = []
        elif not isinstance(raw_children, (list, tuple)):
            return None, "Child services tidak valid"
        else:
            child_ids = []
            for item in raw_children:
                try:
                    child_id = int(item)
                except (ValueError, TypeError):
                    return None, "Child services tidak valid"
                if child_id <= 0:
                    return None, "Child services tidak valid"
                if child_id not in child_ids:
                    child_ids.append(child_id)
            if len(child_ids) > 200:
                return None, "Maksimal 200 child service"

    additional_rules, error = _normalize_additional_rules(data.get("additional_rules"))
    if error:
        return None, error

    if not ip or not name:
        return None, "IP dan Name diperlukan"
    if svc_type not in ("tcp", "http", "icmp", "dns"):
        return None, "Type harus tcp, http, icmp, atau dns"
    if len(name) > 100 or len(ip) > 255:
        return None, "Name/IP terlalu panjang"
    if len(expected) > 200:
        return None, "Expected/keyword terlalu panjang (maks 200)"

    port = None
    if svc_type == "tcp":
        try:
            port = int(port_raw or 0)
        except (ValueError, TypeError):
            return None, "TCP Port harus angka 1-65535"
        if not 1 <= port <= 65535:
            return None, "TCP Port harus 1-65535"
        url = None
        query = ""
        expected = ""
    elif svc_type == "http":
        if not url or not re.match(r"^https?://[^\s/$.?#].[^\s]*$", url, re.IGNORECASE):
            return None, "URL http harus diawali http(s)://"
        if len(url) > 500:
            return None, "URL terlalu panjang"
        port = None
        query = ""
    elif svc_type == "icmp":
        port = None
        url = None
        query = ""
        expected = ""
    else:
        ok, err = validate_dns_query_name(query)
        if not ok:
            return None, err or "Query DNS bermasalah"
        port = None
        url = None

    return {
        "ip": ip,
        "name": name,
        "type": svc_type,
        "port": port,
        "url": url,
        "query": query,
        "expected": expected,
        "tags": tags_text,
        "problem_tags": problem_tags_text,
        "sort_order": sort_order,
        "status_rule": status_rule,
        "description": description,
        "propagation_rule": propagation_rule,
        "weight": weight,
        "additional_rules": json.dumps(additional_rules, ensure_ascii=False),
        "parent_id": parent_id,
        "child_ids": child_ids,
    }, None


def _find_duplicate_service(c, fields, exclude_id=None):
    svc_type = fields["type"]
    if svc_type == "tcp":
        sql = "SELECT id FROM services WHERE ip=? AND type='tcp' AND port=?"
        params = (fields["ip"], fields["port"])
    elif svc_type == "http":
        sql = "SELECT id FROM services WHERE ip=? AND type='http' AND url=?"
        params = (fields["ip"], fields["url"])
    elif svc_type == "icmp":
        sql = "SELECT id FROM services WHERE ip=? AND type='icmp'"
        params = (fields["ip"],)
    else:
        sql = "SELECT id FROM services WHERE ip=? AND type='dns' AND query=?"
        params = (fields["ip"], fields["query"])
    if exclude_id is not None:
        sql += " AND id != ?"
        params = params + (exclude_id,)
    c.execute(sql, params)
    return c.fetchone()


def _service_ancestor_ids(c, svc_id):
    """Kembalikan id parent dan leluhur sebuah service."""
    ancestors = []
    seen = set()
    current = int(svc_id)
    while current:
        if current in seen:
            break
        seen.add(current)
        row = c.execute(
            "SELECT parent_id FROM services WHERE id=?", (current,)
        ).fetchone()
        if not row:
            break
        current = row["parent_id"]
        if current:
            ancestors.append(int(current))
    return ancestors


def _service_hierarchy_error(c, svc_id, parent_id, child_ids):
    """Validasi relasi parent/child dan cegah siklus hierarki."""
    if parent_id is not None:
        row = c.execute("SELECT id FROM services WHERE id=?", (parent_id,)).fetchone()
        if not row:
            return "Parent service tidak ditemukan"
        if svc_id is not None:
            if int(parent_id) == int(svc_id):
                return "Parent service tidak boleh dirinya sendiri"
            if int(svc_id) in _service_ancestor_ids(c, parent_id):
                return "Parent service tidak boleh turunan dari service ini"

    if child_ids:
        placeholders = ",".join("?" for _ in child_ids)
        rows = c.execute(
            f"SELECT id FROM services WHERE id IN ({placeholders})",
            tuple(child_ids),
        ).fetchall()
        found = {int(row["id"]) for row in rows}
        if any(int(child_id) not in found for child_id in child_ids):
            return "Child service tidak ditemukan"
        if svc_id is not None:
            if int(svc_id) in child_ids:
                return "Child service tidak boleh dirinya sendiri"
            ancestors = set(_service_ancestor_ids(c, svc_id))
            if any(child_id in ancestors for child_id in child_ids):
                return "Child service tidak boleh induk dari service ini"
        if parent_id is not None:
            if parent_id in child_ids:
                return "Parent dan child service tidak boleh sama"
            parent_ancestors = set(_service_ancestor_ids(c, parent_id))
            if any(child_id in parent_ancestors for child_id in child_ids):
                return "Relasi parent/child membentuk siklus"
    return None


def _sync_service_children(c, svc_id, child_ids):
    """Samakan parent_id child service dengan daftar child yang dipilih."""
    wanted = {int(child_id) for child_id in (child_ids or [])}
    rows = c.execute("SELECT id FROM services WHERE parent_id=?", (svc_id,)).fetchall()
    current = {int(row["id"]) for row in rows}
    for child_id in current - wanted:
        c.execute("UPDATE services SET parent_id=NULL WHERE id=?", (child_id,))
    for child_id in wanted:
        c.execute("UPDATE services SET parent_id=? WHERE id=?", (svc_id, child_id))


@app.route("/api/services", methods=["POST"])
@api_login_required
@require_role("admin", "operator")
@limiter.limit("30/minute")
def add_service_api():
    data = request.get_json(silent=True) or {}
    fields, error = _validate_service_fields(data)
    if error:
        return jsonify({"error": error}), 400

    conn, c = get_db()
    if _find_duplicate_service(c, fields):
        conn.close()
        return jsonify({"error": "Service monitor ini sudah ada"}), 400
    hierarchy_error = _service_hierarchy_error(
        c, None, fields["parent_id"], fields["child_ids"] or []
    )
    if hierarchy_error:
        conn.close()
        return jsonify({"error": hierarchy_error}), 400
    c.execute(
        "INSERT INTO services"
        " (ip, name, type, port, url, query, expected, tags, created_at,"
        " parent_id, problem_tags, sort_order, status_rule, description,"
        " propagation_rule, weight, additional_rules)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            fields["ip"],
            fields["name"],
            fields["type"],
            fields["port"],
            fields["url"],
            fields["query"],
            fields["expected"],
            fields["tags"],
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            fields["parent_id"],
            fields["problem_tags"],
            fields["sort_order"],
            fields["status_rule"],
            fields["description"],
            fields["propagation_rule"],
            fields["weight"],
            fields["additional_rules"],
        ),
    )
    new_id = c.lastrowid
    if fields["child_ids"]:
        _sync_service_children(c, new_id, fields["child_ids"])
    conn.commit()
    conn.close()

    try:
        audit(
            current_user.username,
            "service.create",
            f"{fields['name']} {fields['type']} {fields['ip']}",
        )
    except Exception:
        pass

    trigger_manual_service_check()
    if fields["type"] == "http" and (fields["url"] or "").lower().startswith(
        "https://"
    ):
        trigger_async_ssl_check(new_id)
    return jsonify({"status": "success", "id": new_id}), 201


@app.route("/api/services/<int:svc_id>", methods=["PUT"])
@api_login_required
@require_role("admin", "operator")
@limiter.limit("60/minute")
def update_service_api(svc_id):
    data = request.get_json(silent=True) or {}
    fields, error = _validate_service_fields(data)
    if error:
        return jsonify({"error": error}), 400

    conn, c = get_db()
    c.execute("SELECT id, name FROM services WHERE id=?", (svc_id,))
    existing = c.fetchone()
    if not existing:
        conn.close()
        return jsonify({"error": "Service tidak ditemukan"}), 404
    if _find_duplicate_service(c, fields, exclude_id=svc_id):
        conn.close()
        return jsonify({"error": "Service monitor ini sudah ada"}), 400
    hierarchy_error = _service_hierarchy_error(
        c, svc_id, fields["parent_id"], fields["child_ids"]
    )
    if hierarchy_error:
        conn.close()
        return jsonify({"error": hierarchy_error}), 400

    c.execute(
        "UPDATE services SET ip=?, name=?, type=?, port=?, url=?, query=?,"
        " expected=?, tags=?, parent_id=?, problem_tags=?, sort_order=?,"
        " status_rule=?, description=?, propagation_rule=?, weight=?,"
        " additional_rules=? WHERE id=?",
        (
            fields["ip"],
            fields["name"],
            fields["type"],
            fields["port"],
            fields["url"],
            fields["query"],
            fields["expected"],
            fields["tags"],
            fields["parent_id"],
            fields["problem_tags"],
            fields["sort_order"],
            fields["status_rule"],
            fields["description"],
            fields["propagation_rule"],
            fields["weight"],
            fields["additional_rules"],
            svc_id,
        ),
    )
    if fields["child_ids"] is not None:
        _sync_service_children(c, svc_id, fields["child_ids"])
    conn.commit()
    conn.close()

    try:
        audit(
            current_user.username,
            "service.update",
            f"{fields['name']} {fields['type']} {fields['ip']} id={svc_id}",
        )
    except Exception:
        pass

    trigger_manual_service_check()
    if fields["type"] == "http" and (fields["url"] or "").lower().startswith(
        "https://"
    ):
        trigger_async_ssl_check(svc_id)
    return jsonify({"status": "success", "id": svc_id})


@app.route("/api/services/check", methods=["POST"])
@api_login_required
@require_role("admin", "operator")
@limiter.limit("10/minute")
def api_services_check_now():
    trigger_manual_service_check()
    return jsonify({"status": "queued"})


@app.route("/api/services/<int:svc_id>/check", methods=["POST"])
@api_login_required
@require_role("admin", "operator")
@limiter.limit("30/minute")
def api_service_check_one(svc_id):
    conn, c = get_db()
    try:
        row = c.execute("SELECT id FROM services WHERE id=?", (svc_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        return jsonify({"error": "Service tidak ditemukan"}), 404
    threading.Thread(
        target=_run_service_check, args=(svc_id,), daemon=True
    ).start()
    return jsonify({"status": "queued", "id": svc_id})


@app.route("/api/services/<int:svc_id>", methods=["DELETE"])
@api_login_required
@require_role("admin", "operator")
@limiter.limit("30/minute")
def delete_service_api(svc_id):
    conn, c = get_db()
    c.execute("SELECT ip, name FROM services WHERE id=?", (svc_id,))
    row = c.fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Service tidak ditemukan"}), 404
    c.execute("UPDATE services SET parent_id=NULL WHERE parent_id=?", (svc_id,))
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
@require_role("admin", "operator")
@limiter.limit("10/minute")
def api_service_ssl_check(svc_id):
    conn, c = get_db()
    c.execute("SELECT url FROM services WHERE id=?", (svc_id,))
    row = c.fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "Service tidak ditemukan"}), 404
    if not (row["url"] or "").lower().startswith("https://"):
        return jsonify({"error": "Service ini bukan HTTPS"}), 400
    trigger_async_ssl_check(svc_id)
    return jsonify({"status": "queued"})


def _sla_collect_children(c, sla_ids=None):
    """Ambil tag dan downtime sekaligus untuk menghindari N+1."""
    tags = {}
    downtimes = {}
    if sla_ids is None:
        tag_rows = c.execute(
            "SELECT sla_id, name, op, value FROM sla_tags ORDER BY id ASC"
        ).fetchall()
        downtime_rows = c.execute(
            "SELECT sla_id, name, start_at, duration_seconds"
            " FROM sla_downtimes ORDER BY start_at ASC, id ASC"
        ).fetchall()
    elif sla_ids:
        placeholders = ",".join("?" for _ in sla_ids)
        tag_rows = c.execute(
            f"SELECT sla_id, name, op, value FROM sla_tags WHERE sla_id IN ({placeholders}) ORDER BY id ASC",
            tuple(sla_ids),
        ).fetchall()
        downtime_rows = c.execute(
            f"SELECT sla_id, name, start_at, duration_seconds FROM sla_downtimes"
            f" WHERE sla_id IN ({placeholders}) ORDER BY start_at ASC, id ASC",
            tuple(sla_ids),
        ).fetchall()
    else:
        tag_rows, downtime_rows = [], []
    for row in tag_rows:
        tags.setdefault(int(row["sla_id"]), []).append(
            {"name": row["name"], "op": row["op"], "value": row["value"] or ""}
        )
    for row in downtime_rows:
        downtimes.setdefault(int(row["sla_id"]), []).append(
            {
                "name": row["name"],
                "start": row["start_at"],
                "duration_seconds": int(row["duration_seconds"] or 0),
            }
        )
    return tags, downtimes


def _sla_load(c, sla_id):
    row = c.execute("SELECT * FROM slas WHERE id=?", (sla_id,)).fetchone()
    if not row:
        return None
    tags, downtimes = _sla_collect_children(c, [int(sla_id)])
    return sla_engine.serialize_sla(row, tags.get(int(sla_id), []), downtimes.get(int(sla_id), []))


def _sla_all(c):
    rows = c.execute(
        "SELECT * FROM slas ORDER BY LOWER(name) ASC, id ASC"
    ).fetchall()
    if not rows:
        return []
    ids = [int(row["id"]) for row in rows]
    tags, downtimes = _sla_collect_children(c, ids)
    services = [
        dict(row)
        for row in c.execute("SELECT id, name, tags FROM services ORDER BY id ASC").fetchall()
    ]
    out = []
    for row in rows:
        sid = int(row["id"])
        matched = sum(
            1
            for svc in services
            if sla_engine.service_matches_tags(
                svc.get("tags"), tags.get(sid, []), row["tag_eval"]
            )
        )
        out.append(
            sla_engine.serialize_sla(
                row, tags.get(sid, []), downtimes.get(sid, []), matched
            )
        )
    return out


def _sla_name_taken(c, name, exclude_id=None):
    if exclude_id:
        row = c.execute(
            "SELECT id FROM slas WHERE LOWER(name)=LOWER(?) AND id<>?",
            (name, int(exclude_id)),
        ).fetchone()
    else:
        row = c.execute(
            "SELECT id FROM slas WHERE LOWER(name)=LOWER(?)", (name,)
        ).fetchone()
    return row is not None


def _sla_save_children(c, sla_id, fields):
    c.execute("DELETE FROM sla_tags WHERE sla_id=?", (sla_id,))
    c.execute("DELETE FROM sla_downtimes WHERE sla_id=?", (sla_id,))
    for tag in fields["tags"]:
        c.execute(
            "INSERT INTO sla_tags (sla_id, name, op, value) VALUES (?, ?, ?, ?)",
            (sla_id, tag["name"], tag["op"], tag.get("value") or ""),
        )
    for downtime in fields["downtimes"]:
        c.execute(
            "INSERT INTO sla_downtimes (sla_id, name, start_at, duration_seconds)"
            " VALUES (?, ?, ?, ?)",
            (
                sla_id,
                downtime["name"],
                downtime["start"],
                int(downtime["duration_seconds"]),
            ),
        )


@app.route("/api/sla", methods=["GET"])
@api_login_required
def api_sla_list():
    conn, c = get_db()
    try:
        rows = _sla_all(c)
    finally:
        conn.close()
    return jsonify({"slas": rows, "count": len(rows)})


@app.route("/api/sla", methods=["POST"])
@api_login_required
@require_role("admin", "operator")
@limiter.limit("30/minute")
def api_sla_create():
    data = request.get_json(silent=True) or {}
    try:
        fields = sla_engine.validate_payload(data)
    except sla_engine.SLAValidationError as exc:
        return jsonify({"error": str(exc)}), 400
    conn, c = get_db()
    try:
        if _sla_name_taken(c, fields["name"]):
            return jsonify({"error": "Nama SLA sudah dipakai"}), 409
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        c.execute(
            "INSERT INTO slas"
            " (name, slo, reporting_period, timezone, schedule, effective_date,"
            " description, enabled, tag_eval, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                fields["name"],
                fields["slo"],
                fields["reporting_period"],
                fields["timezone"],
                fields["schedule"],
                fields["effective_date"],
                fields["description"],
                fields["enabled"],
                fields["tag_eval"],
                now,
                now,
            ),
        )
        sla_id = c.lastrowid
        _sla_save_children(c, sla_id, fields)
        _commit_with_retry(conn)
        created = _sla_load(c, sla_id)
    finally:
        conn.close()
    try:
        audit(current_user.username, "sla.create", f"{fields['name']} id={sla_id}")
    except Exception:
        pass
    return jsonify({"status": "success", "sla": created}), 201


@app.route("/api/sla/<int:sla_id>", methods=["PUT"])
@api_login_required
@require_role("admin", "operator")
@limiter.limit("30/minute")
def api_sla_update(sla_id):
    data = request.get_json(silent=True) or {}
    try:
        fields = sla_engine.validate_payload(data)
    except sla_engine.SLAValidationError as exc:
        return jsonify({"error": str(exc)}), 400
    conn, c = get_db()
    try:
        existing = c.execute("SELECT id FROM slas WHERE id=?", (sla_id,)).fetchone()
        if not existing:
            return jsonify({"error": "SLA tidak ditemukan"}), 404
        if _sla_name_taken(c, fields["name"], exclude_id=sla_id):
            return jsonify({"error": "Nama SLA sudah dipakai"}), 409
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        c.execute(
            "UPDATE slas SET name=?, slo=?, reporting_period=?, timezone=?,"
            " schedule=?, effective_date=?, description=?, enabled=?, tag_eval=?,"
            " updated_at=? WHERE id=?",
            (
                fields["name"],
                fields["slo"],
                fields["reporting_period"],
                fields["timezone"],
                fields["schedule"],
                fields["effective_date"],
                fields["description"],
                fields["enabled"],
                fields["tag_eval"],
                now,
                sla_id,
            ),
        )
        _sla_save_children(c, sla_id, fields)
        _commit_with_retry(conn)
        updated = _sla_load(c, sla_id)
    finally:
        conn.close()
    try:
        audit(current_user.username, "sla.update", f"{fields['name']} id={sla_id}")
    except Exception:
        pass
    return jsonify({"status": "success", "sla": updated})


@app.route("/api/sla/<int:sla_id>/enabled", methods=["POST"])
@api_login_required
@require_role("admin", "operator")
@limiter.limit("60/minute")
def api_sla_enabled(sla_id):
    data = request.get_json(silent=True) or {}
    if "enabled" not in data:
        return jsonify({"error": "Field enabled wajib diisi"}), 400
    enabled = 1 if data.get("enabled") else 0
    conn, c = get_db()
    try:
        row = c.execute("SELECT name FROM slas WHERE id=?", (sla_id,)).fetchone()
        if not row:
            return jsonify({"error": "SLA tidak ditemukan"}), 404
        c.execute(
            "UPDATE slas SET enabled=?, updated_at=? WHERE id=?",
            (enabled, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), sla_id),
        )
        _commit_with_retry(conn)
    finally:
        conn.close()
    try:
        audit(
            current_user.username,
            "sla.enable" if enabled else "sla.disable",
            f"{row['name']} id={sla_id}",
        )
    except Exception:
        pass
    return jsonify({"status": "success", "id": sla_id, "enabled": bool(enabled)})


@app.route("/api/sla/<int:sla_id>", methods=["DELETE"])
@api_login_required
@require_role("admin", "operator")
@limiter.limit("30/minute")
def api_sla_delete(sla_id):
    conn, c = get_db()
    try:
        row = c.execute("SELECT name FROM slas WHERE id=?", (sla_id,)).fetchone()
        if not row:
            return jsonify({"error": "SLA tidak ditemukan"}), 404
        c.execute("DELETE FROM sla_tags WHERE sla_id=?", (sla_id,))
        c.execute("DELETE FROM sla_downtimes WHERE sla_id=?", (sla_id,))
        c.execute("DELETE FROM slas WHERE id=?", (sla_id,))
        _commit_with_retry(conn)
    finally:
        conn.close()
    try:
        audit(current_user.username, "sla.delete", f"{row['name']} id={sla_id}")
    except Exception:
        pass
    return jsonify({"status": "success"})


@app.route("/api/sla/<int:sla_id>/report", methods=["GET"])
@api_login_required
def api_sla_report(sla_id):
    date_from = (request.args.get("from") or "").strip()
    date_to = (request.args.get("to") or "").strip()
    if bool(date_from) != bool(date_to):
        return jsonify({"error": "Isi From dan To sekaligus"}), 400
    service_id = (request.args.get("service_id") or "").strip()
    if service_id:
        try:
            service_id = int(service_id)
        except (TypeError, ValueError):
            return jsonify({"error": "service_id tidak valid"}), 400
    else:
        service_id = None

    conn, c = get_db()
    try:
        row = c.execute("SELECT * FROM slas WHERE id=?", (sla_id,)).fetchone()
        if not row:
            return jsonify({"error": "SLA tidak ditemukan"}), 404
        tags, downtimes = _sla_collect_children(c, [sla_id])
        sla = dict(row)
        sla["tags"] = tags.get(sla_id, [])
        sla["downtimes"] = downtimes.get(sla_id, [])
        try:
            if date_from:
                try:
                    d_from = datetime.strptime(date_from, "%Y-%m-%d").date()
                    d_to = datetime.strptime(date_to, "%Y-%m-%d").date()
                except ValueError:
                    return jsonify({"error": "Format tanggal harus YYYY-MM-DD"}), 400
                if d_from > d_to:
                    return jsonify({"error": "Tanggal awal tidak boleh setelah tanggal akhir"}), 400
                if (d_to - d_from).days > 366:
                    return jsonify({"error": "Rentang maksimal 366 hari"}), 400
                start, end = sla_engine.report_bounds(
                    d_from, d_to, sla.get("timezone") or "Asia/Jakarta"
                )
            else:
                start, end = sla_engine.period_bounds(
                    sla.get("reporting_period") or "monthly",
                    sla.get("timezone") or "Asia/Jakarta",
                )
            report = sla_engine.build_report(conn, c, sla, start, end, service_id)
        except sla_engine.SLAValidationError as exc:
            return jsonify({"error": str(exc)}), 400
    finally:
        conn.close()

    report["sla"].update(
        {
            "reporting_period": sla.get("reporting_period"),
            "reporting_label": sla_engine.reporting_label(sla.get("reporting_period")),
            "timezone": sla.get("timezone"),
            "timezone_label": sla_engine.timezone_label(sla.get("timezone")),
            "schedule_label": sla_engine.schedule_label(
                json.loads(sla["schedule"]) if isinstance(sla.get("schedule"), str) else sla.get("schedule")
            ),
            "effective_date": sla.get("effective_date"),
        }
    )
    return jsonify(report)


@app.route("/api/services/sla")
@api_login_required
def api_services_sla():
    try:
        hours = int(request.args.get("hours", 24))
    except (ValueError, TypeError):
        hours = 24
    hours = max(1, min(hours, 720))
    service_id = request.args.get("service_id", "").strip()
    conn, c = get_db()
    try:
        c.execute("SELECT * FROM services ORDER BY id ASC")
        all_services = [dict(r) for r in c.fetchall()]
        services = all_services
        params = [f"-{hours} hours"]
        where = "timestamp > datetime('now','localtime',?)"
        if service_id:
            try:
                sid = int(service_id)
            except (ValueError, TypeError):
                return jsonify({"error": "service_id tidak valid"}), 400
            services = [s for s in all_services if s["id"] == sid]
            where += " AND service_id=?"
            params.append(sid)
        c.execute(
            "SELECT service_id, status, latency, timestamp FROM service_history "
            f"WHERE {where} ORDER BY id ASC",
            tuple(params),
        )
        rows = [dict(r) for r in c.fetchall()]
    finally:
        conn.close()

    by_service = {}
    for row in rows:
        by_service.setdefault(row["service_id"], []).append(row)

    out_services = []
    for svc in services:
        agg = _svc_agg(by_service.get(svc["id"], []))
        out_services.append(
            {
                "id": svc["id"],
                "name": svc["name"],
                "type": svc["type"],
                "target": _svc_target(svc),
                "status": svc.get("status"),
                "latency": svc.get("latency"),
                "last_checked": svc.get("last_checked"),
                "ssl_days_left": svc.get("ssl_days_left"),
                **agg,
            }
        )

    total = _svc_agg(rows)
    timeline = _sla_timeline(rows, hours)
    return jsonify(
        {
            "hours": hours,
            "service_id": service_id or None,
            "services": out_services,
            "service_options": [
                {"id": s["id"], "name": s["name"]} for s in all_services
            ],
            "totals": {
                **total,
                "services": len(services),
                "online_services": sum(
                    1 for s in services if (s.get("status") or "").upper() == "ONLINE"
                ),
                "offline_services": sum(
                    1 for s in services if (s.get("status") or "").upper() == "OFFLINE"
                ),
            },
            "timeline": timeline,
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
    )


@app.route("/api/services/sla/report")
@api_login_required
def api_services_sla_report():
    date_from = (request.args.get("from") or "").strip()
    date_to = (request.args.get("to") or "").strip()
    today = datetime.now().date()
    if not date_to:
        date_to = today.strftime("%Y-%m-%d")
    if not date_from:
        date_from = (today - timedelta(days=6)).strftime("%Y-%m-%d")
    try:
        d_from = datetime.strptime(date_from, "%Y-%m-%d").date()
        d_to = datetime.strptime(date_to, "%Y-%m-%d").date()
    except ValueError:
        return jsonify({"error": "Format tanggal harus YYYY-MM-DD"}), 400
    if d_from > d_to:
        return jsonify({"error": "Tanggal awal tidak boleh setelah tanggal akhir"}), 400
    if (d_to - d_from).days > 366:
        return jsonify({"error": "Rentang maksimal 366 hari"}), 400
    service_id = request.args.get("service_id", "").strip()

    conn, c = get_db()
    try:
        c.execute("SELECT * FROM services ORDER BY id ASC")
        all_services = [dict(r) for r in c.fetchall()]
        services = all_services
        params = [f"{date_from} 00:00:00", f"{date_to} 23:59:59"]
        where = "timestamp >= ? AND timestamp <= ?"
        if service_id:
            try:
                sid = int(service_id)
            except (ValueError, TypeError):
                return jsonify({"error": "service_id tidak valid"}), 400
            services = [s for s in all_services if s["id"] == sid]
            where += " AND service_id=?"
            params.append(sid)
        c.execute(
            "SELECT service_id, status, latency, timestamp FROM service_history "
            f"WHERE {where} ORDER BY id ASC",
            tuple(params),
        )
        rows = [dict(r) for r in c.fetchall()]
    finally:
        conn.close()

    by_service = {}
    by_day = {}
    for row in rows:
        by_service.setdefault(row["service_id"], []).append(row)
        day = str(row.get("timestamp") or "")[:10]
        by_day.setdefault(day, []).append(row)

    out_services = []
    for svc in services:
        agg = _svc_agg(by_service.get(svc["id"], []))
        out_services.append(
            {
                "id": svc["id"],
                "name": svc["name"],
                "type": svc["type"],
                "target": _svc_target(svc),
                "status": svc.get("status"),
                "last_checked": svc.get("last_checked"),
                "ssl_days_left": svc.get("ssl_days_left"),
                **agg,
            }
        )

    daily = []
    day = d_from
    while day <= d_to:
        key = day.strftime("%Y-%m-%d")
        agg = _svc_agg(by_day.get(key, []))
        daily.append({"date": key, **agg})
        day += timedelta(days=1)

    return jsonify(
        {
            "from": date_from,
            "to": date_to,
            "days": (d_to - d_from).days + 1,
            "service_id": service_id or None,
            "services": out_services,
            "service_options": [
                {"id": s["id"], "name": s["name"]} for s in all_services
            ],
            "daily": daily,
            "totals": {
                **_svc_agg(rows),
                "services": len(services),
                "online_services": sum(
                    1 for s in services if (s.get("status") or "").upper() == "ONLINE"
                ),
                "offline_services": sum(
                    1 for s in services if (s.get("status") or "").upper() == "OFFLINE"
                ),
            },
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
    )


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


@app.route("/problems")
@login_required
def problems_page():
    return render_template("problems.html")


def _like_escape(s):
    return str(s).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


_PROBLEM_SEVERITIES = {"info": 0, "warning": 1, "high": 2, "disaster": 3}


def _problem_row(r):
    """Normalisasi satu baris alert_history menjadi objek Problems."""
    d = dict(r)
    d["status"] = "RESOLVED" if d.get("resolved_at") else "PROBLEM"
    stamps = [
        s
        for s in (
            d.get("triggered_at"),
            d.get("acknowledged_at"),
            d.get("resolved_at"),
            d.get("escalated_at"),
        )
        if s
    ]
    d["update_at"] = max(stamps) if stamps else None
    return d


@app.route("/api/problems")
@api_login_required
@limiter.limit("120/minute")
def api_problems():
    """Daftar masalah gaya Zabbix dari alert_history.

    show: recent (aktif + pulih dalam jendela), problems (aktif saja),
    history (semua status dalam jendela). Filter host/problem (LIKE),
    severity (csv), recent_days (jendela Recent), age_less_days,
    ack=all|unack|ack, by_me, sort.
    format=csv mengembalikan CSV siap unduh.
    """
    show = (request.args.get("show") or "recent").lower()
    if show not in ("recent", "problems", "history"):
        show = "recent"
    host_q = (request.args.get("host") or "").strip()
    problem_q = (request.args.get("problem") or "").strip()
    type_q = (request.args.get("type") or "").strip()
    severity_raw = (request.args.get("severity") or "").lower()
    severities = [
        s.strip() for s in severity_raw.split(",") if s.strip() in _PROBLEM_SEVERITIES
    ]
    age_raw = (request.args.get("age_less_days") or "").strip()
    age_days = None
    if age_raw:
        try:
            age_days = max(1, int(age_raw))
        except (TypeError, ValueError):
            age_days = None
    recent_raw = (request.args.get("recent_days") or "").strip()
    recent_days = 7
    if recent_raw:
        try:
            recent_days = max(1, min(int(recent_raw), 3650))
        except (TypeError, ValueError):
            recent_days = 7
    ack = (request.args.get("ack") or "all").lower()
    if ack not in ("all", "unack", "ack"):
        ack = "all"
    by_me = request.args.get("by_me") == "1"
    fmt = (request.args.get("format") or "json").lower()
    if fmt not in ("json", "csv"):
        fmt = "json"
    sort = (request.args.get("sort") or "time_desc").lower()
    if sort not in ("time_desc", "time_asc"):
        sort = "time_desc"
    limit_raw = (request.args.get("limit") or "").strip()
    try:
        limit = max(1, min(int(limit_raw or 500), 1000))
    except (TypeError, ValueError):
        limit = 500

    now = datetime.now()
    where, params = [], []
    if show == "problems":
        where.append("resolved_at IS NULL")
    elif show == "recent":
        cut = (now - timedelta(days=recent_days)).strftime("%Y-%m-%d %H:%M:%S")
        where.append("(resolved_at IS NULL OR resolved_at >= ?)")
        params.append(cut)
    if age_days is not None:
        where.append("triggered_at >= ?")
        params.append((now - timedelta(days=age_days)).strftime("%Y-%m-%d %H:%M:%S"))
    if host_q:
        where.append("host LIKE ? ESCAPE '\\'")
        params.append("%" + _like_escape(host_q) + "%")
    if problem_q:
        where.append("message LIKE ? ESCAPE '\\'")
        params.append("%" + _like_escape(problem_q) + "%")
    if type_q:
        where.append("alert_type LIKE ? ESCAPE '\\'")
        params.append("%" + _like_escape(type_q) + "%")
    if severities:
        where.append("severity IN (%s)" % ",".join("?" for _ in severities))
        params.extend(severities)
    if ack == "unack":
        where.append("acknowledged_at IS NULL")
    elif ack == "ack":
        where.append("acknowledged_at IS NOT NULL")
    if by_me:
        where.append("acknowledged_by = ?")
        params.append(str(getattr(current_user, "username", "") or ""))

    order = "ASC" if sort == "time_asc" else "DESC"
    conn, c = get_db()
    try:
        sql = (
            "SELECT id, host, alert_type, severity, message, triggered_at,"
            " resolved_at, duration_s, acknowledged_at, acknowledged_by,"
            " assigned_to, note, escalated_at FROM alert_history"
        )
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY triggered_at %s, id %s LIMIT %d" % (order, order, limit)
        c.execute(sql, params)
        rows = [_problem_row(r) for r in c.fetchall()]

        if fmt == "csv":
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(
                [
                    "id",
                    "time",
                    "severity",
                    "status",
                    "recovery_time",
                    "host",
                    "problem",
                    "duration_s",
                    "update",
                    "acknowledged_by",
                    "acknowledged_at",
                    "type",
                    "note",
                ]
            )
            for d in rows:
                w.writerow(
                    [
                        d.get("id"),
                        d.get("triggered_at"),
                        d.get("severity"),
                        d.get("status"),
                        d.get("resolved_at") or "",
                        d.get("host"),
                        d.get("message"),
                        d.get("duration_s") if d.get("duration_s") is not None else "",
                        d.get("update_at") or "",
                        d.get("acknowledged_by") or "",
                        d.get("acknowledged_at") or "",
                        d.get("alert_type") or "",
                        d.get("note") or "",
                    ]
                )
            nama = now.strftime("problems-%Y%m%d-%H%M%S.csv")
            return Response(
                buf.getvalue(),
                mimetype="text/csv",
                headers={"Content-Disposition": f"attachment; filename={nama}"},
            )

        c.execute("SELECT DISTINCT host FROM alert_history ORDER BY host ASC LIMIT 500")
        host_options = [r["host"] for r in c.fetchall()]
        return jsonify(
            {
                "problems": rows,
                "count": len(rows),
                "host_options": host_options,
                "show": show,
            }
        )
    finally:
        conn.close()


@app.route("/audit_logs")
@login_required
def audit_logs_page():
    return render_template("audit.html")


REPORT_PAGES = [
    ("system", "System information", "Informasi runtime aplikasi, database, dan penjadwal."),
    ("scheduled", "Scheduled reports", "Laporan yang dikirim otomatis oleh penjadwal."),
    ("availability", "Availability report", "Ketersediaan host berdasarkan data ping."),
    ("triggers", "Top 100 triggers", "Trigger yang paling sering muncul beserta durasinya."),
    ("audit", "Audit log", "Riwayat perubahan konfigurasi dan aktivitas pengguna."),
    ("actions", "Action log", "Event operasional dan tindakan otomatis sistem."),
    ("notifications", "Notifications", "Status channel, ambang severity, dan riwayat uji notifikasi."),
]
_REPORT_PAGE_MAP = {key: (title, subtitle) for key, title, subtitle in REPORT_PAGES}

_REPORT_JOB_META = {
    "send_daily_report": (
        "Laporan harian NMS",
        "Ringkasan host, alert, dan down event 24 jam.",
        "Setiap hari, 07:00 WIB",
    ),
    "send_fiber_summary": (
        "Ringkasan fiber",
        "Status OLT/ODP/ONT dan alarm fiber.",
        "Setiap hari, 08:05 WIB",
    ),
    "send_heartbeat": (
        "Heartbeat harian",
        "Status konektivitas seluruh host.",
        "Setiap hari, 08:00 WIB",
    ),
}

def _report_fmt_duration(seconds):
    if seconds is None:
        return "—"
    try:
        total = max(0, int(float(seconds)))
    except (TypeError, ValueError):
        return "—"
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _report_fmt_bytes(size):
    try:
        value = float(size)
    except (TypeError, ValueError):
        return "—"
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def _report_cutoff(hours):
    return (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")


def _reports_system_context():
    """Data untuk halaman System information (tabel Parameter/Value/Details)."""
    db_path = resolve_db_path()
    now = datetime.now()
    release = os.environ.get("NMS_VERSION", "1.0.0")
    uptime_s = int((now - app_start_time).total_seconds())
    uptime = _report_fmt_duration(uptime_s)
    started_at = app_start_time.strftime("%Y-%m-%d %H:%M:%S")
    db_backend = (
        "PostgreSQL"
        if os.environ.get("DATABASE_BACKEND", "sqlite").lower() != "sqlite"
        else "SQLite"
    )
    try:
        db_size = os.path.getsize(db_path)
    except OSError:
        db_size = 0

    scheduler_running = False
    scheduler_jobs = 0
    try:
        scheduler_running = bool(getattr(scheduler, "running", False))
        scheduler_jobs = len(scheduler.get_jobs())
    except Exception:
        pass

    cutoff_5m = (now - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
    cutoff_15m = (now - timedelta(minutes=15)).strftime("%Y-%m-%d %H:%M:%S")
    cutoff_24h = (now - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")

    host_total = 0
    host_active = 0
    service_total = 0
    service_online = 0
    service_offline = 0
    service_pending = 0
    ping_items = 0
    agent_items = 0
    alert_total = 0
    alert_active = 0
    alert_resolved = 0
    user_total = 0
    user_online = 0
    rate_samples = 0

    try:
        conn, c = get_db()
        try:
            c.execute("SELECT COUNT(*) AS cnt FROM hosts")
            row = c.fetchone()
            host_total = int((dict(row).get("cnt") if row else 0) or 0)

            c.execute("SELECT ip FROM hosts")
            host_ips = {
                str(dict(r).get("ip"))
                for r in c.fetchall()
                if dict(r).get("ip")
            }
            c.execute(
                "SELECT DISTINCT host FROM ping_logs "
                "WHERE timestamp >= ? AND latency IS NOT NULL AND latency >= 0",
                (cutoff_5m,),
            )
            active_ips = {
                str(dict(r).get("host"))
                for r in c.fetchall()
                if dict(r).get("host")
            }
            host_active = len(host_ips & active_ips)

            c.execute("SELECT status, COUNT(*) AS cnt FROM services GROUP BY status")
            for row in c.fetchall():
                item = dict(row)
                status = str(item.get("status") or "").upper()
                count = int(item.get("cnt") or 0)
                service_total += count
                if status == "ONLINE":
                    service_online += count
                elif status == "OFFLINE":
                    service_offline += count
                else:
                    service_pending += count

            c.execute(
                "SELECT COUNT(DISTINCT host) AS cnt FROM ping_logs WHERE timestamp >= ?",
                (cutoff_24h,),
            )
            row = c.fetchone()
            ping_items = int((dict(row).get("cnt") if row else 0) or 0)

            c.execute(
                "SELECT COUNT(DISTINCT host) AS cnt FROM agent_metrics WHERE timestamp >= ?",
                (cutoff_24h,),
            )
            row = c.fetchone()
            agent_items = int((dict(row).get("cnt") if row else 0) or 0)

            c.execute(
                "SELECT COUNT(*) AS cnt, "
                "SUM(CASE WHEN resolved_at IS NULL THEN 1 ELSE 0 END) AS active "
                "FROM alert_history"
            )
            row = dict(c.fetchone() or {})
            alert_total = int(row.get("cnt") or 0)
            alert_active = int(row.get("active") or 0)
            alert_resolved = max(0, alert_total - alert_active)

            c.execute("SELECT COUNT(*) AS cnt FROM users")
            row = c.fetchone()
            user_total = int((dict(row).get("cnt") if row else 0) or 0)

            c.execute(
                "SELECT COUNT(*) AS cnt FROM ("
                " SELECT username FROM users "
                " WHERE last_login IS NOT NULL AND last_login >= ?"
                " UNION"
                " SELECT username FROM audit_logs "
                " WHERE timestamp >= ? AND username IS NOT NULL AND username != ''"
                ") AS online_users",
                (cutoff_15m, cutoff_15m),
            )
            row = c.fetchone()
            user_online = int((dict(row).get("cnt") if row else 0) or 0)

            for table, ts_column in (
                ("ping_logs", "timestamp"),
                ("agent_metrics", "timestamp"),
                ("snmp_traps", "received_at"),
            ):
                c.execute(
                    f"SELECT COUNT(*) AS cnt FROM {table} WHERE {ts_column} >= ?",
                    (cutoff_5m,),
                )
                row = c.fetchone()
                rate_samples += int((dict(row).get("cnt") if row else 0) or 0)
        finally:
            conn.close()
    except Exception:
        pass

    def part(text, css_class=""):
        return {"text": str(text), "class": css_class}

    host_inactive = max(0, host_total - host_active)
    item_total = ping_items + agent_items + service_total
    rate = rate_samples / 300.0 if rate_samples else 0.0

    rows = [
        {
            "label": "NMS server berjalan",
            "value": "Ya",
            "value_class": "ok",
            "details": [part(f"{request.host} · up {uptime}")],
        },
        {
            "label": "Uptime server",
            "value": uptime,
            "value_class": "",
            "details": [part(started_at)],
        },
        {
            "label": "Versi NMS server",
            "value": release,
            "value_class": "",
            "details": [part("Build lokal")],
        },
        {
            "label": "Versi frontend",
            "value": release,
            "value_class": "",
            "details": [part("Build lokal")],
        },
        {
            "label": "Pemeriksaan pembaruan terakhir",
            "value": "Belum pernah",
            "value_class": "muted",
            "details": [part("Pemeriksaan otomatis belum tersedia")],
        },
        {
            "label": "Rilis terbaru",
            "value": release,
            "value_class": "",
            "details": [part("Anda menggunakan versi terbaru", "ok")],
        },
        {
            "label": "Jumlah host (aktif/nonaktif)",
            "value": host_total,
            "value_class": "",
            "details": [
                part(host_active, "ok"),
                part(host_inactive, "bad"),
            ],
        },
        {
            "label": "Jumlah layanan (online/offline/pending)",
            "value": service_total,
            "value_class": "",
            "details": [
                part(service_online, "ok"),
                part(service_offline, "bad"),
                part(service_pending, "warn"),
            ],
        },
        {
            "label": "Jumlah item monitoring (ping/agen/layanan)",
            "value": item_total,
            "value_class": "",
            "details": [
                part(ping_items),
                part(agent_items),
                part(service_total),
            ],
        },
        {
            "label": "Jumlah trigger (problem/selesai)",
            "value": alert_total,
            "value_class": "",
            "details": [
                part(alert_active, "bad"),
                part(alert_resolved, "ok"),
            ],
        },
        {
            "label": "Jumlah pengguna (online)",
            "value": user_total,
            "value_class": "",
            "details": [part(user_online, "ok")],
        },
        {
            "label": "Performa server yang dibutuhkan, nilai baru per detik",
            "value": f"{rate:.2f}",
            "value_class": "",
            "details": [part(f"Rata-rata 5 menit terakhir ({rate_samples} sampel)")],
        },
        {
            "label": "Database",
            "value": db_backend,
            "value_class": "",
            "details": [part(_report_fmt_bytes(db_size))],
        },
        {
            "label": "Penjadwal",
            "value": "Aktif" if scheduler_running else "Nonaktif",
            "value_class": "ok" if scheduler_running else "bad",
            "details": [part(f"{scheduler_jobs} job")],
        },
        {
            "label": "Global scripts pada NMS server",
            "value": "Nonaktif",
            "value_class": "muted",
            "details": [part("Fitur tidak tersedia")],
        },
        {
            "label": "High availability cluster",
            "value": "Nonaktif",
            "value_class": "muted",
            "details": [part("Mode standalone")],
        },
    ]

    return {
        "system": {
            "rows": rows,
            "version": release,
            "year": now.year,
            "uptime": uptime,
            "started_at": started_at,
            "scheduler_running": scheduler_running,
            "scheduler_jobs": scheduler_jobs,
        }
    }


def _reports_scheduled_context():
    """Data halaman Scheduled reports (filter, tabel, dan status job)."""
    action = (request.args.get("action") or "").strip().lower()
    if action == "create":
        users_list = [
            {"username": "Admin", "name": "Zabbix", "last_name": "Administrator", "display": "Admin (Zabbix Administrator)"},
            {"username": "guest", "name": "", "last_name": "", "display": "guest"},
        ]
        try:
            conn, c = get_db()
            try:
                c.execute("SELECT username, role FROM users")
                existing = {u["username"].lower() for u in users_list}
                for row in c.fetchall():
                    uname = str(dict(row).get("username") or "")
                    if uname and uname.lower() not in existing:
                        users_list.append({
                            "username": uname,
                            "name": "",
                            "last_name": "",
                            "display": uname,
                        })
                        existing.add(uname.lower())
            finally:
                conn.close()
        except Exception:
            pass

        return {
            "scheduled_action": "create",
            "users_list": users_list,
            "app_version": os.environ.get("NMS_VERSION", "1.0.0"),
            "current_year": datetime.now().year,
        }

    scheduler_running = False
    jobs = {}
    try:
        scheduler_running = bool(getattr(scheduler, "running", False))
        jobs = {job.id: job for job in scheduler.get_jobs()}
    except Exception:
        pass

    name_q = (request.args.get("name") or "").strip()
    show = (request.args.get("show") or "all").strip().lower()
    if show not in ("all", "mine"):
        show = "all"
    status = (request.args.get("status") or "any").strip().lower()
    if status not in ("any", "enabled", "disabled", "expired"):
        status = "any"
    sort_dir = (request.args.get("dir") or "asc").strip().lower()
    if sort_dir not in ("asc", "desc"):
        sort_dir = "asc"

    last_sent_by_action = {}
    try:
        conn, c = get_db()
        try:
            c.execute(
                "SELECT action, MAX(timestamp) AS last_sent FROM audit_logs "
                "WHERE action IN ('report.send_daily', 'reports.send') "
                "GROUP BY action"
            )
            last_sent_by_action = {
                str(dict(row).get("action")): dict(row).get("last_sent")
                for row in c.fetchall()
            }
        finally:
            conn.close()
    except Exception:
        pass

    rows = []
    for job_id, (name, desc, schedule) in _REPORT_JOB_META.items():
        job = jobs.get(job_id)
        active = bool(
            scheduler_running
            and job is not None
            and getattr(job, "next_run_time", None) is not None
        )
        # Sesuai Zabbix: Period = rentang laporan, bukan jam kirim.
        period = "Previous day"
        last_sent = (
            last_sent_by_action.get("report.send_daily")
            if job_id == "send_daily_report"
            else None
        )
        rows.append(
            {
                "id": job_id,
                "name": name,
                "description": desc,
                "owner": "System",
                "repeats": "Daily",
                "period": period,
                "schedule": schedule,
                "last_sent": last_sent or "Never",
                "active": active,
                "status_label": "Enabled" if active else "Disabled",
            }
        )

    if name_q:
        needle = name_q.lower()
        rows = [
            row
            for row in rows
            if needle in row["name"].lower() or needle in row["description"].lower()
        ]
    if show == "mine":
        username = (current_user.username or "").lower()
        rows = [row for row in rows if row["owner"].lower() == username]
    if status == "enabled":
        rows = [row for row in rows if row["active"]]
    elif status == "disabled":
        rows = [row for row in rows if not row["active"]]
    elif status == "expired":
        rows = []

    rows.sort(key=lambda row: row["name"].lower(), reverse=(sort_dir == "desc"))

    return {
        "scheduler_running": scheduler_running,
        "report_jobs": rows,
        "filter_name": name_q,
        "filter_show": show,
        "filter_status": status,
        "sort_dir": sort_dir,
        "next_sort_dir": "desc" if sort_dir == "asc" else "asc",
        "scheduled_action": "",
        "app_version": os.environ.get("NMS_VERSION", "1.0.0"),
        "current_year": datetime.now().year,
    }


def _availability_parse_time(value, now=None):
    """Parse absolute or relative (now, now-15m, now-2h, now-7d) availability times."""
    import re

    now = now or datetime.now()
    value = (value or "").strip()
    if not value or value.lower() == "now":
        return now
    relative = re.fullmatch(r"now-(\d+)([mhd])", value.lower())
    if relative:
        amount = int(relative.group(1))
        unit = relative.group(2)
        delta = {"m": timedelta(minutes=amount), "h": timedelta(hours=amount), "d": timedelta(days=amount)}[unit]
        return now - delta
    for time_format in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(value, time_format)
        except ValueError:
            continue
    raise ValueError("Invalid availability time")


def _availability_calendar_range(key, now):
    """Return the calendar-aligned (start, end) range for a Zabbix-style preset."""
    from datetime import timedelta

    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if key == "yesterday":
        return today - timedelta(days=1), today - timedelta(seconds=1)
    if key == "day_before_yesterday":
        return today - timedelta(days=2), today - timedelta(days=1) - timedelta(seconds=1)
    if key == "this_day_last_week":
        start = today - timedelta(days=7)
        return start, start + timedelta(days=1) - timedelta(seconds=1)
    if key == "previous_week":
        start = today - timedelta(days=today.weekday() + 7)
        return start, start + timedelta(days=7) - timedelta(seconds=1)
    if key == "previous_month":
        first_this_month = today.replace(day=1)
        start = (first_this_month - timedelta(days=1)).replace(day=1)
        return start, first_this_month - timedelta(seconds=1)
    if key == "previous_year":
        start = today.replace(month=1, day=1, year=today.year - 1)
        return start, today.replace(month=1, day=1) - timedelta(seconds=1)
    if key == "today":
        return today, today + timedelta(days=1) - timedelta(seconds=1)
    if key == "today_so_far":
        return today, now
    if key == "this_week":
        start = today - timedelta(days=today.weekday())
        return start, start + timedelta(days=7) - timedelta(seconds=1)
    if key == "this_week_so_far":
        return today - timedelta(days=today.weekday()), now
    if key == "this_month":
        start = today.replace(day=1)
        next_month = (start + timedelta(days=32)).replace(day=1)
        return start, next_month - timedelta(seconds=1)
    if key == "this_month_so_far":
        return today.replace(day=1), now
    if key == "this_year":
        start = today.replace(month=1, day=1)
        return start, start.replace(year=start.year + 1) - timedelta(seconds=1)
    if key == "this_year_so_far":
        return today.replace(month=1, day=1), now
    return None


def _availability_relative_value(key):
    """Display value used by the From field for rolling presets."""
    month_days = {"3m": 90, "6m": 180}
    year_days = {"1y": 365, "2y": 730}
    if key in month_days:
        return "now-%dd" % month_days[key]
    if key in year_days:
        return "now-%dd" % year_days[key]
    return "now-" + key


def _merge_availability_intervals(intervals):
    merged = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            if end > merged[-1][1]:
                merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    return merged


def _availability_interval_seconds(intervals, start=None, end=None):
    total = 0.0
    for item_start, item_end in intervals:
        if end is not None and item_start >= end:
            continue
        if start is not None and item_end <= start:
            continue
        clipped_start = max(item_start, start) if start is not None else item_start
        clipped_end = min(item_end, end) if end is not None else item_end
        total += max(0.0, (clipped_end - clipped_start).total_seconds())
    return total


def _availability_host_ip(host):
    """Return the IP part of an alert host such as "Router (10.0.0.1)"."""
    value = str(host or "").strip()
    match = re.search(r"\((\d{1,3}(?:\.\d{1,3}){3})\)\s*$", value)
    return match.group(1) if match else value


def _availability_alert_intervals(alerts, window_start, window_end):
    """Group alert rows by host/trigger and merge their overlapping problem intervals."""
    from datetime import timedelta
    from collections import defaultdict

    grouped = defaultdict(lambda: {"count": 0, "intervals": []})
    for alert in alerts:
        trigger_name = str(alert.get("message") or alert.get("alert_type") or "Availability").strip()
        key = (alert.get("host"), trigger_name)
        grouped[key]["count"] += 1
        try:
            started = datetime.strptime(str(alert.get("triggered_at"))[:19], "%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            continue
        resolved = alert.get("resolved_at")
        if resolved:
            try:
                ended = datetime.strptime(str(resolved)[:19], "%Y-%m-%d %H:%M:%S")
            except (TypeError, ValueError):
                ended = window_end
        elif alert.get("duration_s") is not None:
            ended = started + timedelta(seconds=max(0, int(alert.get("duration_s") or 0)))
        else:
            ended = window_end
        clipped_start = max(started, window_start)
        clipped_end = min(ended, window_end)
        if clipped_end > clipped_start:
            grouped[key]["intervals"].append((clipped_start, clipped_end))
    return {
        key: {"count": value["count"], "intervals": _merge_availability_intervals(value["intervals"])}
        for key, value in grouped.items()
    }


def _reports_availability_context(mode_override=None):
    from datetime import timedelta

    presets = [
        {"key": "2d", "label": "Last 2 days", "seconds": 2 * 86400},
        {"key": "7d", "label": "Last 7 days", "seconds": 7 * 86400},
        {"key": "30d", "label": "Last 30 days", "seconds": 30 * 86400},
        {"key": "3m", "label": "Last 3 months", "seconds": 90 * 86400},
        {"key": "6m", "label": "Last 6 months", "seconds": 180 * 86400},
        {"key": "1y", "label": "Last 1 year", "seconds": 365 * 86400},
        {"key": "2y", "label": "Last 2 years", "seconds": 2 * 365 * 86400},
        {"key": "yesterday", "label": "Yesterday", "seconds": 86400},
        {"key": "day_before_yesterday", "label": "Day before yesterday", "seconds": 86400},
        {"key": "this_day_last_week", "label": "This day last week", "seconds": 86400},
        {"key": "previous_week", "label": "Previous week", "seconds": 7 * 86400},
        {"key": "previous_month", "label": "Previous month", "seconds": 30 * 86400},
        {"key": "previous_year", "label": "Previous year", "seconds": 365 * 86400},
        {"key": "today", "label": "Today", "seconds": 86400},
        {"key": "today_so_far", "label": "Today so far", "seconds": 86400},
        {"key": "this_week", "label": "This week", "seconds": 7 * 86400},
        {"key": "this_week_so_far", "label": "This week so far", "seconds": 7 * 86400},
        {"key": "this_month", "label": "This month", "seconds": 30 * 86400},
        {"key": "this_month_so_far", "label": "This month so far", "seconds": 30 * 86400},
        {"key": "this_year", "label": "This year", "seconds": 365 * 86400},
        {"key": "this_year_so_far", "label": "This year so far", "seconds": 365 * 86400},
        {"key": "5m", "label": "Last 5 minutes", "seconds": 5 * 60},
        {"key": "15m", "label": "Last 15 minutes", "seconds": 15 * 60},
        {"key": "30m", "label": "Last 30 minutes", "seconds": 30 * 60},
        {"key": "1h", "label": "Last 1 hour", "seconds": 3600},
        {"key": "3h", "label": "Last 3 hours", "seconds": 3 * 3600},
        {"key": "6h", "label": "Last 6 hours", "seconds": 6 * 3600},
        {"key": "12h", "label": "Last 12 hours", "seconds": 12 * 3600},
        {"key": "1d", "label": "Last 1 day", "seconds": 86400},
    ]
    preset_columns = [presets[0:7], presets[7:13], presets[13:21], presets[21:]]
    calendar_keys = {
        "yesterday", "day_before_yesterday", "this_day_last_week", "previous_week",
        "previous_month", "previous_year", "today", "today_so_far", "this_week",
        "this_week_so_far", "this_month", "this_month_so_far", "this_year",
        "this_year_so_far",
    }
    for item in presets:
        item["kind"] = "calendar" if item["key"] in calendar_keys else "rolling"

    legacy_range = {"24h": "1d", "7d": "7d", "30d": "30d"}
    selected_key = (request.args.get("time") or legacy_range.get(request.args.get("range"), "1h")).lower()
    now = datetime.now()

    raw_from = (request.args.get("from") or "").strip()
    raw_to = (request.args.get("to") or "").strip()
    custom = bool(raw_from or raw_to)
    range_error = ""
    if custom:
        raw_from = raw_from or "now-1h"
        raw_to = raw_to or "now"
        try:
            start_dt = _availability_parse_time(raw_from, now)
            end_dt = _availability_parse_time(raw_to, now)
            if end_dt <= start_dt:
                raise ValueError("End time must be after start time")
            selected_key = "custom"
            selected_label = raw_from + " — " + raw_to
            from_value, to_value = raw_from, raw_to
        except (TypeError, ValueError):
            custom = False
            range_error = "invalid"

    if not custom:
        selected = next((item for item in presets if item["key"] == selected_key), None)
        if selected is None:
            selected_key = "1h"
            selected = next(item for item in presets if item["key"] == selected_key)
        selected_label = selected["label"]
        if selected["kind"] == "calendar":
            start_dt, end_dt = _availability_calendar_range(selected_key, now)
            from_value = start_dt.strftime("%Y-%m-%d %H:%M")
            to_value = end_dt.strftime("%Y-%m-%d %H:%M")
        else:
            end_dt = now
            start_dt = end_dt - timedelta(seconds=int(selected["seconds"]))
            from_value = _availability_relative_value(selected_key)
            to_value = "now"

    if range_error:
        range_error = (
            'Invalid time range "%s — %s". Showing %s instead.'
            % (raw_from, raw_to, selected_label)
        )

    cutoff = start_dt.strftime("%Y-%m-%d %H:%M:%S")
    end_cutoff = end_dt.strftime("%Y-%m-%d %H:%M:%S")

    selected_time = {
        "selected_key": selected_key,
        "label": selected_label,
        "from_value": from_value,
        "to_value": to_value,
        "custom": custom,
    }

    mode = (mode_override or request.args.get("mode") or "host").lower()
    if mode not in ("host", "trigger"):
        mode = "host"

    stats = {}
    hosts = []
    problem_counts = {}
    alerts = []
    down_events = 0
    data_error = False
    try:
        conn, c = get_db()
        try:
            c.execute(
                "SELECT host, COUNT(*) AS samples, "
                "SUM(CASE WHEN latency IS NOT NULL AND latency >= 0 THEN 1 ELSE 0 END) AS ok, "
                "AVG(CASE WHEN latency IS NOT NULL AND latency >= 0 THEN latency END) AS avg_latency, "
                "AVG(packet_loss) AS avg_loss, MAX(timestamp) AS last_seen "
                "FROM ping_logs WHERE timestamp >= ? AND timestamp <= ? GROUP BY host",
                (cutoff, end_cutoff),
            )
            stats = {dict(r)["host"]: dict(r) for r in c.fetchall()}
            c.execute("SELECT ip, alias, category FROM hosts ORDER BY ip")
            hosts = [dict(r) for r in c.fetchall()]
            c.execute(
                "SELECT host, COUNT(*) AS problem_count FROM down_events "
                "WHERE started_at <= ? AND (resolved_at IS NULL OR resolved_at >= ?) "
                "GROUP BY host",
                (end_cutoff, cutoff),
            )
            problem_counts = {dict(r)["host"]: int(dict(r).get("problem_count") or 0) for r in c.fetchall()}
            c.execute("SELECT COUNT(*) AS cnt FROM down_events WHERE started_at >= ? AND started_at <= ?", (cutoff, end_cutoff))
            row = c.fetchone()
            down_events = int((dict(row).get("cnt") if row else 0) or 0)
            if mode == "trigger":
                c.execute(
                    "SELECT host, alert_type, message, triggered_at, resolved_at, duration_s "
                    "FROM alert_history WHERE triggered_at <= ? "
                    "AND (resolved_at IS NULL OR resolved_at >= ?)",
                    (end_cutoff, cutoff),
                )
                alerts = [dict(r) for r in c.fetchall()]
        finally:
            conn.close()
    except Exception:
        data_error = True
        app.logger.exception("Gagal memuat data availability report")

    alias_by_ip = {h.get("ip"): (h.get("alias") or h.get("ip")) for h in hosts}
    category_by_ip = {h.get("ip"): (h.get("category") or "—") for h in hosts}

    rows = []
    window_seconds = max(1.0, (end_dt - start_dt).total_seconds())
    if mode == "trigger":
        grouped = _availability_alert_intervals(alerts, start_dt, end_dt)
        for (host, trigger_name), info in grouped.items():
            host_ip = _availability_host_ip(host)
            problem_seconds = _availability_interval_seconds(info["intervals"])
            availability = round(max(0.0, window_seconds - problem_seconds) / window_seconds * 100, 2)
            if availability >= 99:
                status_class, status_label = "ok", "OK"
            elif availability >= 95:
                status_class, status_label = "warn", "Warning"
            else:
                status_class, status_label = "bad", "Critical"
            rows.append(
                {
                    "host": host,
                    "host_ip": host_ip,
                    "name": alias_by_ip.get(host_ip) or alias_by_ip.get(host) or host,
                    "item_name": trigger_name,
                    "category": category_by_ip.get(host_ip) or category_by_ip.get(host, "—"),
                    "trigger_name": trigger_name,
                    "graph_trigger": trigger_name,
                    "samples": int(info["count"]),
                    "ok": 0,
                    "availability": availability,
                    "problems": int(info["count"]),
                    "ok_duration": _report_fmt_duration(max(0.0, window_seconds - problem_seconds)),
                    "problem_duration": _report_fmt_duration(problem_seconds),
                    "avg_latency": None,
                    "avg_loss": None,
                    "last_seen": None,
                    "status_class": status_class,
                    "status_label": status_label,
                }
            )
    else:
        ips = list(dict.fromkeys([h.get("ip") for h in hosts if h.get("ip")] + list(stats.keys())))
        for ip in ips:
            s = stats.get(ip, {})
            samples = int(s.get("samples") or 0)
            ok = int(s.get("ok") or 0)
            availability = round(ok / samples * 100, 2) if samples else None
            if samples == 0:
                status_class, status_label = "muted", "No data"
            elif availability is not None and availability >= 99:
                status_class, status_label = "ok", "OK"
            elif availability is not None and availability >= 95:
                status_class, status_label = "warn", "Warning"
            else:
                status_class, status_label = "bad", "Critical"
            if samples:
                ok_seconds = window_seconds * ok / samples
                down_seconds = max(0.0, window_seconds - ok_seconds)
            else:
                ok_seconds = down_seconds = None
            rows.append(
                {
                    "host": ip,
                    "host_ip": ip,
                    "name": alias_by_ip.get(ip, ip),
                    "item_name": "ICMP ping",
                    "category": category_by_ip.get(ip, "—"),
                    "trigger_name": "Availability",
                    "graph_trigger": None,
                    "samples": samples,
                    "ok": ok,
                    "availability": availability,
                    "problems": problem_counts.get(ip, 0),
                    "ok_duration": _report_fmt_duration(ok_seconds) if samples else None,
                    "problem_duration": _report_fmt_duration(down_seconds) if samples else None,
                    "avg_latency": round(float(s.get("avg_latency") or 0), 1) if s.get("avg_latency") is not None else None,
                    "avg_loss": round(float(s.get("avg_loss") or 0), 1) if s.get("avg_loss") is not None else None,
                    "last_seen": s.get("last_seen"),
                    "status_class": status_class,
                    "status_label": status_label,
                }
            )

    rows.sort(key=lambda r: (r["availability"] is None, r["availability"] if r["availability"] is not None else 101, r["host"]))
    total_samples = sum(r["samples"] for r in rows)
    total_ok = sum(r["ok"] for r in rows)
    if mode == "trigger":
        overall = round(sum(r["availability"] for r in rows) / len(rows), 2) if rows else None
        monitored = len(rows)
        event_count = sum(int(r["problems"] or 0) for r in rows)
    else:
        overall = round(total_ok / total_samples * 100, 2) if total_samples else None
        monitored = sum(1 for r in rows if r["samples"] > 0)
        event_count = down_events
    below = sum(1 for r in rows if r["availability"] is not None and r["availability"] < 99)

    return {
        "availability_presets": presets,
        "availability_preset_columns": preset_columns,
        "availability_time": selected_time,
        "availability_mode": mode,
        "availability_rows": rows,
        "availability_overall": overall,
        "availability_total_samples": total_samples,
        "availability_monitored": monitored,
        "availability_below": below,
        "availability_down_events": down_events,
        "availability_event_count": event_count,
        "availability_cutoff": cutoff,
        "availability_range_error": range_error,
        "availability_data_error": data_error,
    }


def _reports_triggers_context():
    time_context = _reports_availability_context(mode_override="host")
    cutoff = time_context["availability_cutoff"]
    end_cutoff = _availability_parse_time(
        time_context["availability_time"]["to_value"], datetime.now()
    ).strftime("%Y-%m-%d %H:%M:%S")
    rows = []
    total_alerts = 0
    active_alerts = 0
    last_24h = 0
    active_total = 0
    try:
        conn, c = get_db()
        try:
            c.execute(
                "SELECT host, alert_type, severity, message, COUNT(*) AS total, "
                "SUM(CASE WHEN resolved_at IS NULL THEN 1 ELSE 0 END) AS active, "
                "MAX(triggered_at) AS last_seen, "
                "SUM(COALESCE(duration_s, 0)) AS total_duration "
                "FROM alert_history WHERE triggered_at >= ? AND triggered_at <= ? "
                "GROUP BY host, alert_type, severity, message "
                "ORDER BY total DESC, last_seen DESC LIMIT 100",
                (cutoff, end_cutoff),
            )
            rows = [dict(r) for r in c.fetchall()]
            c.execute("SELECT COUNT(*) AS cnt FROM alert_history")
            total_alerts = int((dict(c.fetchone()).get("cnt") or 0))
            c.execute("SELECT COUNT(*) AS cnt FROM alert_history WHERE resolved_at IS NULL")
            active_alerts = int((dict(c.fetchone()).get("cnt") or 0))
            c.execute("SELECT COUNT(*) AS cnt FROM alert_history WHERE triggered_at >= ? AND triggered_at <= ?", (cutoff, end_cutoff))
            last_24h = int((dict(c.fetchone()).get("cnt") or 0))
            active_total = sum(int(r.get("active") or 0) for r in rows)
        finally:
            conn.close()
    except Exception:
        app.logger.exception("Gagal memuat Top triggers report")

    for index, row in enumerate(rows, start=1):
        row["rank"] = index
        row["total"] = int(row.get("total") or 0)
        row["active"] = int(row.get("active") or 0)
        row["total_duration"] = _report_fmt_duration(row.get("total_duration"))
    return {
        "trigger_rows": rows,
        "trigger_total_alerts": total_alerts,
        "trigger_active_alerts": active_alerts,
        "trigger_last_24h": last_24h,
        "trigger_groups": len(rows),
        "trigger_active_total": active_total,
        "trigger_presets": time_context["availability_presets"],
        "trigger_preset_columns": time_context["availability_preset_columns"],
        "trigger_time": time_context["availability_time"],
        "availability_time": time_context["availability_time"],
        "availability_presets": time_context["availability_presets"],
        "availability_preset_columns": time_context["availability_preset_columns"],
        "availability_range_error": time_context["availability_range_error"],
        "trigger_range_error": time_context["availability_range_error"],
        "trigger_data_error": time_context["availability_data_error"],
    }


def _reports_audit_context():
    q = (request.args.get("q") or "").strip()
    action = (request.args.get("action") or "").strip()
    try:
        page_no = int(request.args.get("p", 1))
    except (TypeError, ValueError):
        page_no = 1
    per_page = 50

    where = []
    params = []
    if q:
        like = f"%{q}%"
        where.append("(username LIKE ? OR action LIKE ? OR target LIKE ? OR detail LIKE ?)")
        params.extend([like, like, like, like])
    if action:
        where.append("action = ?")
        params.append(action)
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    rows = []
    actions = []
    total = 0
    try:
        conn, c = get_db()
        try:
            c.execute("SELECT DISTINCT action FROM audit_logs ORDER BY action")
            actions = [dict(r)["action"] for r in c.fetchall() if dict(r).get("action")]
            c.execute("SELECT COUNT(*) AS cnt FROM audit_logs" + where_sql, params)
            total = int((dict(c.fetchone()).get("cnt") or 0))
            pages = max(1, (total + per_page - 1) // per_page)
            page_no = min(max(page_no, 1), pages)
            offset = (page_no - 1) * per_page
            c.execute(
                "SELECT id, timestamp, username, action, target, detail, ip_address "
                "FROM audit_logs" + where_sql + " ORDER BY id DESC LIMIT ? OFFSET ?",
                params + [per_page, offset],
            )
            rows = [dict(r) for r in c.fetchall()]
        finally:
            conn.close()
    except Exception:
        page_no = 1
        pages = 1
        offset = 0
    else:
        pages = max(1, (total + per_page - 1) // per_page)
        offset = (page_no - 1) * per_page

    return {
        "audit_rows": rows,
        "audit_total": total,
        "audit_page": page_no,
        "audit_pages": pages,
        "audit_q": q,
        "audit_action": action,
        "audit_actions": actions,
        "audit_per_page": per_page,
        "audit_from": offset + 1 if rows else 0,
        "audit_to": offset + len(rows),
    }


def _reports_actions_context():
    q = (request.args.get("q") or "").strip()
    event = (request.args.get("event") or "").strip()
    try:
        page_no = int(request.args.get("p", 1))
    except (TypeError, ValueError):
        page_no = 1
    per_page = 50

    where = []
    params = []
    if q:
        like = f"%{q}%"
        where.append("(event_type LIKE ? OR host LIKE ? OR message LIKE ?)")
        params.extend([like, like, like])
    if event:
        where.append("event_type = ?")
        params.append(event)
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    rows = []
    event_counts = []
    total = 0
    pages = 1
    offset = 0
    try:
        conn, c = get_db()
        try:
            c.execute(
                "SELECT event_type, COUNT(*) AS cnt FROM system_logs "
                "GROUP BY event_type ORDER BY cnt DESC, event_type"
            )
            event_counts = [dict(r) for r in c.fetchall()]
            c.execute("SELECT COUNT(*) AS cnt FROM system_logs" + where_sql, params)
            total = int((dict(c.fetchone()).get("cnt") or 0))
            pages = max(1, (total + per_page - 1) // per_page)
            page_no = min(max(page_no, 1), pages)
            offset = (page_no - 1) * per_page
            c.execute(
                "SELECT id, timestamp, event_type, host, message FROM system_logs"
                + where_sql
                + " ORDER BY id DESC LIMIT ? OFFSET ?",
                params + [per_page, offset],
            )
            rows = [dict(r) for r in c.fetchall()]
        finally:
            conn.close()
    except Exception:
        pass

    return {
        "action_rows": rows,
        "action_total": total,
        "action_page": page_no,
        "action_pages": pages,
        "action_q": q,
        "action_event": event,
        "action_events": event_counts,
        "action_from": offset + 1 if rows else 0,
        "action_to": offset + len(rows),
    }


def _reports_notifications_context():
    status = channel_status()
    channels = [
        {
            "key": "telegram",
            "label": "Telegram",
            "active": bool(status.get("telegram")),
            "min_sev": get_setting("notify_telegram_min_sev", "info", type_cast=str),
            "detail": "Bot token + chat ID terkonfigurasi"
            if status.get("telegram")
            else "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID belum lengkap",
        },
        {
            "key": "email",
            "label": "Email (SMTP)",
            "active": bool(status.get("email")),
            "min_sev": get_setting("notify_email_min_sev", "critical", type_cast=str),
            "detail": "SMTP dan penerima terkonfigurasi"
            if status.get("email")
            else "SMTP_HOST / SMTP_FROM / ALERT_EMAIL_TO belum lengkap",
        },
        {
            "key": "webhook",
            "label": "Webhook",
            "active": bool(status.get("webhook")),
            "min_sev": get_setting("notify_webhook_min_sev", "warning", type_cast=str),
            "detail": "URL webhook terkonfigurasi" if status.get("webhook") else "WEBHOOK_URL belum diisi",
        },
    ]

    recent_tests = []
    latest_alerts = []
    alert_counts = []
    try:
        conn, c = get_db()
        try:
            c.execute(
                "SELECT timestamp, username, detail FROM audit_logs "
                "WHERE action = 'notify.test' ORDER BY id DESC LIMIT 10"
            )
            recent_tests = [dict(r) for r in c.fetchall()]
            c.execute(
                "SELECT severity, COUNT(*) AS cnt FROM alert_history "
                "WHERE triggered_at >= ? GROUP BY severity ORDER BY cnt DESC",
                (_report_cutoff(24),),
            )
            alert_counts = [dict(r) for r in c.fetchall()]
            c.execute(
                "SELECT host, alert_type, severity, message, triggered_at, resolved_at "
                "FROM alert_history ORDER BY id DESC LIMIT 8"
            )
            latest_alerts = [dict(r) for r in c.fetchall()]
        finally:
            conn.close()
    except Exception:
        pass

    return {
        "notification_channels": channels,
        "notification_active": sum(1 for ch in channels if ch["active"]),
        "notification_tests": recent_tests,
        "notification_alerts": latest_alerts,
        "notification_alert_counts": alert_counts,
        "notification_last_test": recent_tests[0]["timestamp"] if recent_tests else None,
        "notification_total_24h": sum(int(r.get("cnt") or 0) for r in alert_counts),
    }


@app.route("/reports")
@login_required
def reports_index():
    return redirect(url_for("reports_page", page="system"))


def _availability_graph_buckets(start_dt, end_dt, points=None, problem_intervals=None):
    """Aggregate availability into chart buckets instead of drawing raw samples."""
    range_seconds = max(1.0, (end_dt - start_dt).total_seconds())
    bucket_count = max(12, min(60, int(range_seconds // 3600) + 1))
    bucket_seconds = range_seconds / bucket_count
    if range_seconds <= 2 * 86400:
        label_format = "%H:%M"
    elif range_seconds <= 7 * 86400:
        label_format = "%a %H:%M"
    elif range_seconds <= 60 * 86400:
        label_format = "%d %b"
    else:
        label_format = "%b %Y"

    buckets = []
    for index in range(bucket_count):
        bucket_start = start_dt + timedelta(seconds=bucket_seconds * index)
        bucket_end = start_dt + timedelta(seconds=bucket_seconds * (index + 1))
        buckets.append(
            {
                "label": bucket_start.strftime(label_format),
                "start": bucket_start.strftime("%Y-%m-%d %H:%M:%S"),
                "availability": None,
                "samples": 0,
            }
        )

    if problem_intervals is not None:
        for index, bucket in enumerate(buckets):
            bucket_start = start_dt + timedelta(seconds=bucket_seconds * index)
            bucket_end = start_dt + timedelta(seconds=bucket_seconds * (index + 1))
            problem_seconds = _availability_interval_seconds(problem_intervals, bucket_start, bucket_end)
            bucket["availability"] = round(max(0.0, 1.0 - problem_seconds / bucket_seconds) * 100, 2)
        return buckets

    for point in points or []:
        try:
            timestamp = datetime.strptime(str(point.get("timestamp"))[:19], "%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            continue
        index = int((timestamp - start_dt).total_seconds() // bucket_seconds)
        index = min(max(index, 0), bucket_count - 1)
        bucket = buckets[index]
        bucket["samples"] = int(bucket.get("samples") or 0) + 1
        latency = point.get("latency")
        ok = latency is not None and float(latency) >= 0
        bucket["_ok"] = int(bucket.get("_ok") or 0) + (1 if ok else 0)

    for bucket in buckets:
        samples = int(bucket.get("samples") or 0)
        bucket["availability"] = round(int(bucket.pop("_ok", 0)) / samples * 100, 2) if samples else None
    return buckets


@app.route("/reports/availability/graph")
@login_required
def reports_availability_graph():
    host = (request.args.get("host") or "").strip()
    trigger = (request.args.get("trigger") or "").strip()
    from_value = (request.args.get("from") or "").strip()
    to_value = (request.args.get("to") or "").strip()
    try:
        now = datetime.now()
        end_dt = _availability_parse_time(to_value or "now", now)
        start_dt = _availability_parse_time(from_value, now) if from_value else end_dt - timedelta(hours=1)
        if end_dt <= start_dt:
            raise ValueError("Invalid time range")
    except (TypeError, ValueError):
        abort(400, description="Invalid availability graph time range")

    points = []
    alerts = []
    host_name = host
    try:
        conn, c = get_db()
        try:
            c.execute("SELECT alias FROM hosts WHERE ip = ? LIMIT 1", (_availability_host_ip(host),))
            host_row = c.fetchone()
            if host_row and host_row["alias"]:
                host_name = host_row["alias"]
            if trigger:
                c.execute(
                    "SELECT host, alert_type, message, triggered_at, resolved_at, duration_s "
                    "FROM alert_history WHERE host = ? AND message = ? "
                    "AND triggered_at <= ? AND (resolved_at IS NULL OR resolved_at >= ?)",
                    (host, trigger, end_dt.strftime("%Y-%m-%d %H:%M:%S"), start_dt.strftime("%Y-%m-%d %H:%M:%S")),
                )
                alerts = [dict(row) for row in c.fetchall()]
            else:
                c.execute(
                    "SELECT timestamp, latency FROM ping_logs "
                    "WHERE host = ? AND timestamp >= ? AND timestamp <= ? ORDER BY timestamp",
                    (host, start_dt.strftime("%Y-%m-%d %H:%M:%S"), end_dt.strftime("%Y-%m-%d %H:%M:%S")),
                )
                points = [dict(row) for row in c.fetchall()]
        finally:
            conn.close()
    except Exception:
        app.logger.exception("Gagal mengambil data graph availability untuk host %s", host)

    range_seconds = max(1.0, (end_dt - start_dt).total_seconds())
    if trigger:
        grouped = _availability_alert_intervals(alerts, start_dt, end_dt)
        intervals = grouped.get((host, trigger), {}).get("intervals", [])
        problem_seconds = _availability_interval_seconds(intervals)
        availability = round(max(0.0, range_seconds - problem_seconds) / range_seconds * 100, 2)
        buckets = _availability_graph_buckets(start_dt, end_dt, problem_intervals=intervals)
        has_data = True
    else:
        ok = sum(1 for point in points if point.get("latency") is not None and float(point["latency"]) >= 0)
        total = len(points)
        availability = (ok * 100 / total) if total else 0
        buckets = _availability_graph_buckets(start_dt, end_dt, points=points)
        has_data = bool(points)

    return render_template(
        "availability_report_graph.html",
        graph_host=host,
        graph_host_name=host_name,
        graph_trigger=trigger,
        graph_mode="trigger" if trigger else "host",
        graph_from=start_dt,
        graph_to=end_dt,
        graph_buckets=buckets,
        graph_availability=availability,
        graph_has_data=has_data,
    )


@app.route("/reports/<page>")
@login_required
def reports_page(page):
    page = (page or "").lower()
    if page not in _REPORT_PAGE_MAP:
        abort(404)
    title, subtitle = _REPORT_PAGE_MAP[page]

    context = {}
    if page == "system":
        context.update(_reports_system_context())
    elif page == "scheduled":
        context.update(_reports_scheduled_context())
    elif page == "availability":
        context.update(_reports_availability_context())
    elif page == "triggers":
        context.update(_reports_triggers_context())
    elif page == "audit":
        context.update(_reports_audit_context())
    elif page == "actions":
        context.update(_reports_actions_context())
    elif page == "notifications":
        context.update(_reports_notifications_context())

    return render_template(
        "reports.html",
        report_page=page,
        report_title=title,
        report_subtitle=subtitle,
        report_pages=REPORT_PAGES,
        is_admin=getattr(current_user, "role", None) == "admin",
        **context,
    )


@app.route("/api/traps")

@api_login_required
def api_list_traps():
    """Riwayat SNMP trap terstruktur (tabel snmp_traps)."""
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
    source = (request.args.get("source") or "").strip()
    category = (request.args.get("category") or "").strip()
    only_invalid = request.args.get("invalid") == "1"

    conn, c = get_db()
    try:
        query = "SELECT * FROM snmp_traps WHERE 1=1"
        params = []
        if source:
            query += " AND source_ip=?"
            params.append(source)
        if category:
            query += " AND category=?"
            params.append(category)
        if only_invalid:
            query += " AND community_ok=0"
        c.execute(f"SELECT COUNT(*) as total FROM ({query})", params)
        total = c.fetchone()["total"]
        query += " ORDER BY id DESC LIMIT ? OFFSET ?"
        params.extend((limit, offset))
        c.execute(query, params)
        rows = c.fetchall()
        traps = []
        for r in rows:
            traps.append(
                {
                    "id": r["id"],
                    "received_at": r["received_at"],
                    "source_ip": r["source_ip"],
                    "source_port": r["source_port"],
                    "community_ok": r["community_ok"],
                    "snmp_version": r["snmp_version"],
                    "trap_oid": r["trap_oid"],
                    "generic": r["generic"],
                    "specific": r["specific"],
                    "if_index": r["if_index"],
                    "if_oper": r["if_oper"],
                    "sys_uptime": r["sys_uptime"],
                    "category": r["category"],
                    "message": r["message"],
                    "raw_vars": r["raw_vars"],
                }
            )
        return jsonify({"traps": traps, "total": total, "page": page, "limit": limit})
    finally:
        conn.close()


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
