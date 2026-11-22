"""Masalah (problems) terbuka/tertutup, severity, ack."""


from .connection import (
    _lock,
    transaction,
    _now,
    get_conn,
)

_SEVERITY_RANK = {"disaster": 5, "high": 4, "average": 3, "warning": 2, "information": 1}

def open_problem(device_id, kind, severity, message, link_id=None):
    """Buat masalah bila belum ada yang terbuka untuk (device_id, kind, link); else perbarui pesan."""
    now = _now()
    with transaction() as conn:
        row = conn.execute(
            "SELECT id FROM problems WHERE device_id = ? AND kind = ? AND status = 'open'"
            " AND COALESCE(link_id, 0) = COALESCE(?, 0)",
            (device_id, kind, link_id),
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE problems SET severity=?, message=? WHERE id=?",
                (severity, message[:300], row["id"]),
            )
            return row["id"], False
        cursor = conn.execute(
            "INSERT INTO problems (device_id, kind, severity, message, status, started_at, link_id) VALUES (?,?,?,?,'open',?,?)",
            (device_id, kind, severity, message[:300], now, link_id),
        )
        return cursor.lastrowid, True

def resolve_problem(device_id, kind, link_id=None):
    now = _now()
    with transaction() as conn:
        cursor = conn.execute(
            "UPDATE problems SET status='resolved', resolved_at=? WHERE device_id=? AND kind=? AND status='open'"
            " AND COALESCE(link_id, 0) = COALESCE(?, 0)",
            (now, device_id, kind, link_id),
        )
        return cursor.rowcount

def resolve_device_status_problems(device_id):
    return resolve_problem(device_id, "device_down") + resolve_problem(device_id, "high_latency")

def list_problems(status="open", limit=200):
    limit = min(500, max(1, int(limit or 200)))
    with _lock:
        rows = get_conn().execute(
            """
            SELECT p.*, d.name AS device_name, d.ip AS device_ip, d.location AS device_location, d.group_id AS device_group_id,
                   fl.name AS link_name
            FROM problems p
            JOIN devices d ON d.id = p.device_id
            LEFT JOIN fiber_links fl ON fl.id = p.link_id
            WHERE p.status = ?
            ORDER BY p.started_at DESC LIMIT ?
            """,
            (status, limit),
        ).fetchall()
    problems = [dict(r) for r in rows]
    problems.sort(key=lambda p: (_SEVERITY_RANK.get(p["severity"], 0), p["started_at"]), reverse=True)
    return problems

def list_problems_for_device(device_id, status="open", limit=100):
    with _lock:
        rows = get_conn().execute(
            "SELECT * FROM problems WHERE device_id = ? AND status = ?"
            " ORDER BY CASE severity WHEN 'disaster' THEN 0 WHEN 'high' THEN 1"
            " WHEN 'average' THEN 2 WHEN 'warning' THEN 3 ELSE 4 END, started_at DESC LIMIT ?",
            (device_id, status, min(500, max(1, int(limit)))),
        ).fetchall()
    return [dict(row) for row in rows]

def ack_problem(problem_id, username):
    now = _now()
    with transaction() as conn:
        cursor = conn.execute(
            "UPDATE problems SET acked_by=?, acked_at=? WHERE id=? AND status='open'",
            (username, now, problem_id),
        )
        return cursor.rowcount > 0

def unack_problem(problem_id):
    with transaction() as conn:
        cursor = conn.execute(
            "UPDATE problems SET acked_by=NULL, acked_at=NULL WHERE id=? AND status='open'",
            (problem_id,),
        )
        return cursor.rowcount > 0

def top_problems(since_iso, limit=20):
    with _lock:
        rows = get_conn().execute(
            """
            SELECT p.kind, p.device_id, COUNT(*) AS total, MAX(p.started_at) AS last_seen,
                   d.name AS device_name, d.ip AS device_ip
            FROM problems p JOIN devices d ON d.id = p.device_id
            WHERE p.started_at >= ?
            GROUP BY p.kind, p.device_id
            ORDER BY total DESC, last_seen DESC
            LIMIT ?
            """,
            (since_iso, int(limit)),
        ).fetchall()
    return [dict(r) for r in rows]

def open_problem_count(device_id):
    with _lock:
        row = get_conn().execute(
            "SELECT COUNT(*) AS n FROM problems WHERE device_id = ? AND status = 'open'",
            (device_id,),
        ).fetchone()
    return row["n"]

