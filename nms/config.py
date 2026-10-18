"""nms.config — konstanta dan konfigurasi global."""

import os

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SECRET_KEY = os.environ.get("SECRET_KEY", "")
DASHBOARD_USERNAME = os.environ.get("DASHBOARD_USERNAME", "admin")
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")

# Password lemah: kosong, terlalu pendek (<10), atau masuk daftar kata umum
# (dibanding case-insensitive). Dipakai saat admin pertama dibuat dari .env.
_DASHBOARD_WEAK_PASSWORDS = frozenset(
    {
        "admin",
        "admin123",
        "admin1234",
        "admin12345",
        "password",
        "password1",
        "password123",
        "passw0rd",
        "p@ssw0rd",
        "123456",
        "1234567",
        "12345678",
        "123456789",
        "1234567890",
        "qwerty",
        "qwerty123",
        "abc123",
        "letmein",
        "welcome",
        "welcome1",
        "changeme",
        "change-me",
        "changeit",
        "default",
        "root",
        "toor",
        "nms123",
        "nmsadmin",
        "kopegtel",
        "kopegtel123",
        "your_strong_password_here",
        "your-password-here",
        "ganti-dengan-password",
        "ganti-dengan-password-kuat",
        "ganti-dengan-password-kuat-min-10-karakter",
        "ganti_dengan_password_kuat",
    }
)


def dashboard_password_is_weak(password):
    """Benar bila password kosong, <10 karakter, atau kata umum."""
    password = str(password or "")
    if not password or len(password) < 10:
        return True
    return password.strip().lower() in _DASHBOARD_WEAK_PASSWORDS


COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "0") == "1"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# Notifikasi Email (SMTP). PORT 465 = SSL implisit, selain itu STARTTLS.
SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_FROM = os.environ.get("SMTP_FROM", "")
ALERT_EMAIL_TO = os.environ.get("ALERT_EMAIL_TO", "")
try:
    SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
except (ValueError, TypeError):
    SMTP_PORT = 587

# Notifikasi Webhook generik. WEBHOOK_SECRET opsional (HMAC-SHA256 body).
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")

AGENT_API_KEY = os.environ.get("AGENT_API_KEY", "")

# AGENT_API_KEY is required for security
if not AGENT_API_KEY:
    import sys

    env = os.environ.get("FLASK_ENV", "production")
    if env == "production":
        print("[FATAL] AGENT_API_KEY must be set in .env for production")
        sys.exit(1)
    else:
        print("[WARN] AGENT_API_KEY not set (development mode only)")
        # Generate a temporary key for development
        import secrets

        AGENT_API_KEY = secrets.token_urlsafe(32)
        print(f"[DEV] Generated temporary AGENT_API_KEY: {AGENT_API_KEY}")

try:
    MAX_HOSTS = max(1, int(os.environ.get("MAX_HOSTS", "200")))
except (ValueError, TypeError):
    MAX_HOSTS = 200

HYSTERESIS = 5.0
DOWN_COOLDOWN_S = 600

LOGIN_FAIL_TTL_S = 600
