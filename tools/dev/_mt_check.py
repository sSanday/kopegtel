import os
import sys

from dotenv import load_dotenv

load_dotenv()
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(BASE_DIR, "backend"))
import db  # noqa: E402

db.init_db()
print("init_db OK at", db.DB_PATH)
rows = db.get_conn().execute(
    "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'mt_%' ORDER BY name"
).fetchall()
print("tables:", [r[0] for r in rows])
print("settings:", {k: v for k, v in db.get_settings({}).items() if k.startswith("mt_")})
print("devices:", [(d["id"], d["name"], d["mikrotik_enabled"]) for d in db.list_devices()])
print("summary:", db.mt_summary())
print("filter options:", db.mt_filter_options())
print("caps default:", db.get_mt_capabilities(1))
print("settings mt 1:", db.get_mt_device_settings(1))
print("poll state:", db.get_mt_poll_state(1))
fleet = db.query_mt_devices()
print("fleet:", {k: v for k, v in fleet.items() if k != "devices"})
for item in fleet["devices"]:
    print("  ", item["id"], item["name"], item.get("firmware_status"), item.get("active_sessions"))
