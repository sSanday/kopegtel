"""Jejak audit (audit_log) + umpan aktivitas."""


from .connection import (
    _lock,
    _now,
    get_conn,
)
from .devices import (
    summary,
)

def add_audit_log(user_id, username, ip, action, obj, summary):
    with _lock:
        conn = get_conn()
        conn.execute(
            "INSERT INTO audit_log (ts, user_id, username, ip, action, object, summary) VALUES (?,?,?,?,?,?,?)",
            (_now(), user_id, username, ip, action, obj, (summary or "")[:400]),
        )
        conn.commit()

def list_audit_log(search=None, action=None, limit=100, offset=0):
    limit = min(500, max(1, int(limit or 100)))
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
            [*params, limit, int(offset or 0)],
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

