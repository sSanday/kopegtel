"""nms.config — konstanta dan konfigurasi global NMS Dashboard.

Dipindah dari app.py (modul 1, P3) tanpa perubahan perilaku: semua nilai
dibaca dari environment saat import, sama seperti sebelumnya. Modul ini
sengaja tanpa dependensi Flask/DB agar bisa diimpor dari mana saja.

Yang TIDAK dipindah ke sini:
- DB_PATH — test meng-override app.DB_PATH saat runtime, jadi tetap di app.py.
- app.config Flask — tetap di app.py (spesifik Flask).
"""

import os

from dotenv import load_dotenv

load_dotenv()

# ─── Direktori ────────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ─── Secret & Auth ────────────────────────────────────────────────────────────
SECRET_KEY = os.environ.get("SECRET_KEY", "")
DASHBOARD_USERNAME = os.environ.get("DASHBOARD_USERNAME", "admin")
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "admin123")

# ─── Cookie & Session ─────────────────────────────────────────────────────────
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "0") == "1"

# ─── Telegram ─────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# ─── Agent ────────────────────────────────────────────────────────────────────
AGENT_API_KEY = os.environ.get("AGENT_API_KEY", "")
if not AGENT_API_KEY:
    print(
        "[WARN] AGENT_API_KEY kosong — /api/agent/report TERBUKA tanpa auth "
        "(siapa pun bisa kirim metrik palsu). Set AGENT_API_KEY di .env untuk produksi."
    )

# ─── Batas Host ───────────────────────────────────────────────────────────────
try:
    MAX_HOSTS = max(1, int(os.environ.get("MAX_HOSTS", "200")))
except (ValueError, TypeError):
    MAX_HOSTS = 200

# ─── Monitoring Thresholds ────────────────────────────────────────────────────
HYSTERESIS = 5.0
DOWN_COOLDOWN_S = 600

# ─── Login Security ───────────────────────────────────────────────────────────
LOGIN_FAIL_TTL_S = 600
