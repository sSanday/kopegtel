"""Jejak audit (audit_log) + umpan aktivitas."""

from datetime import datetime, timedelta, timezone

from .connection import (
    _lock,
    transaction,
    _now,
    get_conn,
)
from .devices import (
    summary,
)
from core.validation import SQLITE_INT64_MAX, parse_query_int

# Batas panjang field audit. SQLite tidak menegakkan VARCHAR, jadi nilai dari
# pemanggil (termasuk username login yang tidak terautentikasi) harus dipotong
# di sini agar satu request tidak bisa menulis data raksasa.
AUDIT_USERNAME_MAX = 64
AUDIT_IP_MAX = 64
AUDIT_ACTION_MAX = 64
AUDIT_OBJECT_MAX = 200
AUDIT_SUMMARY_MAX = 400


def _clip(value, limit, default=""):
    text = "" if value is None else str(value)
    if not text:
        text = default
    return text[:limit]


def add_audit_log(user_id, username, ip, action, obj, summary):
    with transaction() as conn:
        conn.execute(
            "INSERT INTO audit_log (ts, user_id, username, ip, action, object, summary) VALUES (?,?,?,?,?,?,?)",
            (
                _now(),
                user_id,
                _clip(username, AUDIT_USERNAME_MAX, "-"),
                _clip(ip, AUDIT_IP_MAX, "-"),
                _clip(action, AUDIT_ACTION_MAX, "unknown"),
                _clip(obj, AUDIT_OBJECT_MAX),
                _clip(summary, AUDIT_SUMMARY_MAX),
            ),
        )


def count_recent_audit(action, minutes):
    """Jumlah baris audit untuk aksi tertentu dalam jendela waktu terakhir."""
    threshold = (
        datetime.now(timezone.utc) - timedelta(minutes=max(1, int(minutes)))
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    with _lock:
        row = get_conn().execute(
            "SELECT COUNT(*) AS n FROM audit_log WHERE action = ? AND ts >= ?",
            (_clip(action, AUDIT_ACTION_MAX, "unknown"), threshold),
        ).fetchone()
    return int(row["n"] or 0)

def list_audit_log(search=None, action=None, limit=100, offset=0):
    limit = parse_query_int(limit, 100, min_value=1, max_value=500)
    offset = parse_query_int(offset, 0, min_value=0, max_value=SQLITE_INT64_MAX)
    where, params = [], []
    if search:
        like = f"%{search.strip().lower()}%"
        where.append("(lower(username) LIKE ? OR lower(object) LIKE ? OR lower(summary) LIKE ?)")
        params.extend([like, like, like])
    if action:
        where.append("action = ?")
        params.append(action)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    with _lock:
        total = get_conn().execute(f"SELECT COUNT(*) AS n FROM audit_log {where_sql}", params).fetchone()["n"]
        rows = get_conn().execute(
            f"SELECT * FROM audit_log {where_sql} ORDER BY id DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
    return {"total": total, "entries": [dict(r) for r in rows]}

def recent_activity(limit=10):
    with _lock:
        rows = get_conn().execute(
            """
            SELECT e.created_at, e.message, e.old_status, e.new_status, d.name AS device_name, d.id AS device_id
            FROM events e JOIN devices d ON d.id = e.device_id
            ORDER BY e.id DESC LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
    return [dict(r) for r in rows]

