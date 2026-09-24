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
if not AGENT_API_KEY:
    print(
        "[WARN] AGENT_API_KEY kosong — /api/agent/report TERBUKA tanpa auth "
        "(siapa pun bisa kirim metrik palsu). Set AGENT_API_KEY di .env untuk produksi."
    )

try:
    MAX_HOSTS = max(1, int(os.environ.get("MAX_HOSTS", "200")))
except (ValueError, TypeError):
    MAX_HOSTS = 200

HYSTERESIS = 5.0
DOWN_COOLDOWN_S = 600

LOGIN_FAIL_TTL_S = 600
