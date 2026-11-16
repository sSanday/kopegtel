"""NMS Kopegtel — backend Flask + SQLite + APScheduler.

Menyajikan HANYA file *.html di root proyek dan folder assets/,
serta API JSON di bawah /api/* (kecuali /api/login & /api/public-config).

Keamanan: sesi cookie HttpOnly + SameSite=Lax, token CSRF untuk semua
POST/PUT/DELETE, batas percobaan login (5 gagal / 10 menit per akun+IP dan
50 gagal / 10 menit per IP), batas ukuran body request, idle timeout (default
30 menit, bisa diubah di Administration), izin per peran
(admin/operator/viewer) ditegakkan di server, header keamanan, dan
FLASK hanya menyajikan file yang memang milik aplikasi.
"""

import logging
import os
import re
import sys
import time
from datetime import timedelta

from dotenv import load_dotenv
from flask import Flask, abort, jsonify, request, send_from_directory
from werkzeug.security import generate_password_hash

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
ASSETS_DIR = os.path.join(BASE_DIR, "assets")
ROOT_HTML_RE = re.compile(r"[A-Za-z0-9_-]+\.html")

load_dotenv(os.path.join(BASE_DIR, ".env"))

import config  # noqa: E402

config.setup_logging()
config.validate_or_exit()

log = logging.getLogger("nms.app")
SECRET_KEY = os.environ.get("SECRET_KEY", "").strip()

import db  # noqa: E402  (env harus dimuat dulu)
from core import guard  # noqa: E402
from core.env import env_flag, env_int  # noqa: E402


def _fail_fast(message):
    log.error("Konfigurasi tidak valid: %s", message)
    sys.exit(1)


try:
    import core.validation  # noqa: E402,F401  (validasi ALLOWED_NETWORKS saat startup)
except ValueError as exc:
    _fail_fast(str(exc))


try:
    PORT = env_int("PORT", 5500, minimum=1, maximum=65535)
except ValueError as exc:
    _fail_fast(str(exc))
HOST = os.environ.get("HOST", "127.0.0.1")

import poller  # noqa: E402
import auth  # noqa: E402
from collectors import mikrotik_modules  # noqa: E402,F401  (re-ekspor kompatibilitas)

from routes import auth as auth_routes  # noqa: E402
from routes import problems as problems_routes  # noqa: E402
from routes import services as services_routes  # noqa: E402
from routes import discovery as discovery_routes  # noqa: E402
from routes import alerts as alerts_routes  # noqa: E402
from routes import devices as devices_routes  # noqa: E402
from routes import fiber as fiber_routes  # noqa: E402
from routes import mikrotik as mikrotik_routes  # noqa: E402
from routes import admin as admin_routes  # noqa: E402
from routes import events as events_routes  # noqa: E402
from routes import escalation as escalation_routes  # noqa: E402
from routes import mfa as mfa_routes  # noqa: E402
from routes import flow as flow_routes  # noqa: E402
from routes import metrics as metrics_routes  # noqa: E402


def _seed_admin():
    if db.user_count() > 0:
        return
    username = os.environ.get("ADMIN_USERNAME", "admin").strip() or "admin"
    password = os.environ.get("ADMIN_PASSWORD", "").strip()
    if not password:
        log.warning("tabel users kosong dan ADMIN_PASSWORD tidak diatur — belum ada akun yang bisa login. Isi ADMIN_PASSWORD di .env lalu restart.")
        return
    if len(password) < auth.PASSWORD_MIN_LENGTH:
        log.warning("ADMIN_PASSWORD minimal %d karakter; akun admin TIDAK dibuat.", auth.PASSWORD_MIN_LENGTH)
        return
    db.create_user(username, generate_password_hash(password), role="admin", must_change_password=1)
    log.info("Akun admin awal dibuat: %s (wajib mengganti kata sandi pada login pertama)", username)


def bootstrap():
    """Siapkan skema database dan akun admin awal.

    Sengaja TIDAK dipanggil saat modul diimpor: entrypoint (``serve.py``)
    harus mengambil lease instance terlebih dahulu, agar instance kedua tidak
    menjalankan migrasi skema saat instance pertama masih hidup. Tes memanggil
    fungsi ini sekali setelah database sementara dibuat.
    """
    db.init_db()
    _seed_admin()


def _positive_int_env(name, default, minimum=1):
    try:
        return env_int(name, default, minimum=minimum)
    except ValueError as exc:
        _fail_fast(str(exc))


# Batas ukuran body request. Endpoint login/MFA memakai batas lebih kecil di
# core.guard; nilai ini berlaku untuk seluruh API dan form.
MAX_REQUEST_BYTES = _positive_int_env("MAX_REQUEST_BYTES", 1024 * 1024)

app = Flask(__name__, static_folder=None)
app.secret_key = SECRET_KEY
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=env_flag("COOKIE_SECURE"),  # lokal: HTTP. Set 1 bila di belakang HTTPS.
    PERMANENT_SESSION_LIFETIME=timedelta(days=14),
    MAX_CONTENT_LENGTH=MAX_REQUEST_BYTES,
    MAX_FORM_MEMORY_SIZE=MAX_REQUEST_BYTES,
    MAX_FORM_PARTS=1000,
)

try:
    TRUSTED_PROXIES = env_int("TRUSTED_PROXIES", 0, minimum=0)
except ValueError:
    _fail_fast("TRUSTED_PROXIES harus berupa angka (jumlah proxy tepercaya di depan aplikasi).")
if TRUSTED_PROXIES > 0:
    from werkzeug.middleware.proxy_fix import ProxyFix

    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=TRUSTED_PROXIES, x_proto=TRUSTED_PROXIES, x_host=TRUSTED_PROXIES)
    log.info("Proxy tepercaya aktif: TRUSTED_PROXIES=%d (IP klien dari X-Forwarded-For)", TRUSTED_PROXIES)

guard.register_guard(app)
app.register_blueprint(auth_routes.bp)
app.register_blueprint(problems_routes.bp)
app.register_blueprint(services_routes.bp)
app.register_blueprint(discovery_routes.bp)
app.register_blueprint(alerts_routes.bp)
app.register_blueprint(devices_routes.bp)
app.register_blueprint(fiber_routes.bp)
app.register_blueprint(mikrotik_routes.bp)
app.register_blueprint(admin_routes.bp)
app.register_blueprint(events_routes.bp)
app.register_blueprint(escalation_routes.bp)
app.register_blueprint(mfa_routes.bp)
app.register_blueprint(flow_routes.bp)
app.register_blueprint(metrics_routes.bp)


# ---------------------------------------------------------
# Health check (tanpa login; minimal)
# ---------------------------------------------------------

@app.get("/healthz")
def healthz():
    try:
        # Koneksi baca terpisah: health check tidak boleh memakai koneksi
        # bersama (_conn) milik thread request/poller tanpa lock.
        with db.readonly_connection() as conn:
            conn.execute("SELECT 1").fetchone()
        db_ok = True
    except Exception:
        db_ok = False
    heartbeat = getattr(poller, "LAST_HEARTBEAT", None)
    poller_ok = heartbeat is not None and (time.time() - heartbeat) < 90
    if db_ok and poller_ok:
        return jsonify({"status": "ok"}), 200
    return jsonify({"status": "error"}), 503


# ---------------------------------------------------------
# Penyajian file statis (dibatasi)
# ---------------------------------------------------------

@app.get("/")
def serve_index():
    return send_from_directory(BASE_DIR, "index.html")


@app.get("/assets/<path:filename>")
def serve_assets(filename):
    if ".." in filename:
        abort(404)
    return send_from_directory(ASSETS_DIR, filename)


@app.get("/<path:filename>")
def serve_root_html(filename):
    if ROOT_HTML_RE.fullmatch(filename):
        path = os.path.join(BASE_DIR, filename)
        if os.path.isfile(path):
            return send_from_directory(BASE_DIR, filename)
    abort(404)


@app.errorhandler(404)
def not_found(_error):
    if request.path.startswith("/api"):
        return jsonify({"error": "Tidak ditemukan."}), 404
    return "404 — Tidak ditemukan", 404


@app.errorhandler(405)
def method_not_allowed(_error):
    return jsonify({"error": "Metode tidak diizinkan."}), 405


@app.errorhandler(413)
def request_too_large(_error):
    if request.path.startswith("/api"):
        return jsonify(
            {
                "error": "Ukuran permintaan terlalu besar.",
                "code": "request_too_large",
            }
        ), 413
    return "413 — Permintaan terlalu besar", 413


@app.errorhandler(500)
def internal_error(_error):
    return jsonify({"error": "Kesalahan internal server."}), 500


if __name__ == "__main__":
    # Mode dev. Produksi gunakan: python backend/serve.py (waitress).
    from ingest import INGEST
    from flow import FLOW

    bootstrap()
    poller.start_poller()
    INGEST.start()
    FLOW.start()
    try:
        app.run(host=HOST, port=PORT, debug=False, use_reloader=False)
    finally:
        FLOW.stop()
        INGEST.stop()
        poller.shutdown_poller()
