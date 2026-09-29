"""nms.config — konstanta dan konfigurasi global."""

import os

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SECRET_KEY = os.environ.get("SECRET_KEY", "")
DASHBOARD_USERNAME = os.environ.get("DASHBOARD_USERNAME", "admin")
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "admin123")

COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "0") == "1"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

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
