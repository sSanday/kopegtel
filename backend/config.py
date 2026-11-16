"""Konfigurasi & validasi fail-fast + logging terpusat NMS Kopegtel.

Dipanggil saat start (serve.py / app impor). ``validate_or_exit`` mencetak
pesan Indonesia yang jelas ke stderr lalu keluar bila konfigurasi kurang.
Tidak pernah memuat atau mencetak isi .env (hanya nilai dari lingkungan).
"""
import logging
import logging.handlers
import os
import re
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGS_DIR = os.path.join(BASE_DIR, "logs")

PLACEHOLDER_SECRET_KEY = "change-me-dengan-string-acak"
PLACEHOLDER_ADMIN_PASSWORD = "change-me-min-10-karakter"

MIN_SECRET_KEY = 32
MIN_ADMIN_PASSWORD = 10


# ---------------------------------------------------------
# Redaksi secret untuk logging
# ---------------------------------------------------------

_REDACT_PATTERNS = [
    re.compile(
        r"(?P<key>(?:SECRET_KEY|ENCRYPTION_KEY|ADMIN_PASSWORD|SNMP_COMMUNITY|PASSWORD|PASSWD|PASSPHRASE|TOKEN|API[_-]?KEY)"
        r"[\"']?\s*[:=]\s*[\"']?)(?P<value>[^\"'\s,;]+)",
        re.IGNORECASE,
    ),
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----", re.DOTALL),
]


def redact(text):
    if not text:
        return text
    redacted = _REDACT_PATTERNS[0].sub(lambda m: m.group("key") + "***", text)
    redacted = _REDACT_PATTERNS[1].sub("[redacted private key]", redacted)
    return redacted


class RedactingFormatter(logging.Formatter):
    def format(self, record):
        return redact(super().format(record))


def setup_logging():
    """Logging ke logs/nms.log (berputar 5 x 10 MB) + stderr. Level dari LOG_LEVEL."""
    root = logging.getLogger()
    if getattr(root, "_nms_configured", False):
        return
    os.makedirs(LOGS_DIR, exist_ok=True)
    level_name = (os.environ.get("LOG_LEVEL") or "INFO").strip().upper()
    level = getattr(logging, level_name, logging.INFO)
    formatter = RedactingFormatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
    file_handler = logging.handlers.RotatingFileHandler(
        os.path.join(LOGS_DIR, "nms.log"),
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    root.setLevel(level)
    root.addHandler(file_handler)
    root.addHandler(stream_handler)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)  # kurangi derau job interval
    root._nms_configured = True  # singleton: aman dipanggil dua kali


# ---------------------------------------------------------
# Validasi konfigurasi (fail-fast)
# ---------------------------------------------------------

def _numeric_error(env, name, lo, hi, *, is_float=False):
    """Validasi satu variabel angka; return pesan error atau None."""
    raw = (env.get(name) or "").strip()
    if not raw:
        return None
    try:
        value = float(raw) if is_float else int(raw)
    except ValueError:
        return f"{name} harus berupa angka, bukan {raw!r}."
    if value != value or value in (float("inf"), float("-inf")):  # NaN/Inf
        return f"{name} harus berupa angka terhingga, bukan {raw!r}."
    if not (lo <= value <= hi):
        return f"{name} harus antara {lo} dan {hi}, bukan {raw!r}."
    return None


def config_errors(env=None):
    """Kembalikan daftar pesan kesalahan konfigurasi (kosong = valid)."""
    env = env if env is not None else os.environ
    errors = []

    for name, lo, hi in (
        ("PORT", 1, 65535),
        ("WAITRESS_THREADS", 1, 64),
        ("POLL_MAX_WORKERS", 1, 64),
        ("TRUSTED_PROXIES", 0, 16),
        ("MAX_REQUEST_BYTES", 1024, 64 * 1024 * 1024),
    ):
        message = _numeric_error(env, name, lo, hi)
        if message:
            errors.append(message)
    message = _numeric_error(env, "DEGRADED_LATENCY_MS", 0, 60000, is_float=True)
    if message:
        errors.append(message)

    secret = (env.get("SECRET_KEY") or "").strip()
    if not secret:
        errors.append("SECRET_KEY belum diatur. Salin .env.example menjadi .env lalu isi SECRET_KEY (acak, minimal 32 karakter).")
    elif secret == PLACEHOLDER_SECRET_KEY:
        errors.append("SECRET_KEY masih berisi nilai contoh dari .env.example. Buat nilai baru: py -c \"import secrets; print(secrets.token_hex(32))\"")
    elif len(secret) < MIN_SECRET_KEY:
        errors.append(f"SECRET_KEY minimal {MIN_SECRET_KEY} karakter (sekarang {len(secret)}). Buat nilai baru: py -c \"import secrets; print(secrets.token_hex(32))\"")

    encryption = (env.get("ENCRYPTION_KEY") or "").strip()
    if not encryption:
        errors.append("ENCRYPTION_KEY belum diatur. Generate dengan: py -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"")
    else:
        try:
            from cryptography.fernet import Fernet

            Fernet(encryption.encode("utf-8"))
        except Exception:
            errors.append("ENCRYPTION_KEY bukan kunci Fernet yang valid. Generate ulang: py -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"")

    admin_password = (env.get("ADMIN_PASSWORD") or "").strip()
    if not admin_password:
        errors.append("ADMIN_PASSWORD belum diatur. Minimal 10 karakter dan bukan nilai contoh; isi di .env.")
    elif admin_password == PLACEHOLDER_ADMIN_PASSWORD:
        errors.append("ADMIN_PASSWORD masih berisi nilai contoh dari .env.example. Ganti dengan kata sandi baru minimal 10 karakter.")
    elif len(admin_password) < MIN_ADMIN_PASSWORD:
        errors.append(f"ADMIN_PASSWORD minimal {MIN_ADMIN_PASSWORD} karakter.")

    return errors


def validate_or_exit(env=None):
    errors = config_errors(env)
    if not errors:
        return
    for error in errors:
        print(f"ERROR konfigurasi: {error}", file=sys.stderr)
    sys.exit(1)
