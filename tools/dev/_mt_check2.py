import os
import sys

from dotenv import load_dotenv

load_dotenv()
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(BASE_DIR, "backend"))
import db  # noqa: E402

with db._lock:
    row = db.get_conn().execute(
        "SELECT id,name,type,mikrotik_enabled,parent_id,routeros_version,mikrotik_model FROM devices"
    ).fetchone()
print(dict(row))
for name in ("interface_stats", "mt_interfaces"):
    exists = db.get_conn().execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    print(name, bool(exists))
