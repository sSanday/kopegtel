"""E2 — kebijakan eskalasi: CRUD, langkah, dan state mesin eskalasi."""

from .connection import (
    _lock,
    transaction,
    _now,
    get_conn,
)

_POLICY_FIELDS = (
    "name", "description", "enabled", "business_hours_enabled",
    "business_start", "business_end", "business_days", "repeat_minutes",
)


def list_policies():
    with _lock:
        rows = get_conn().execute(
            "SELECT * FROM escalation_policies ORDER BY name COLLATE NOCASE"
        ).fetchall()
        steps = get_conn().execute(
            "SELECT * FROM escalation_steps ORDER BY policy_id, step_order"
        ).fetchall()
    by_policy = {}
    for step in steps:
        by_policy.setdefault(step["policy_id"], []).append(dict(step))
    result = []
    for row in rows:
        item = dict(row)
        item["enabled"] = bool(item["enabled"])
        item["business_hours_enabled"] = bool(item["business_hours_enabled"])
        item["steps"] = by_policy.get(row["id"], [])
        result.append(item)
    return result


def get_policy(policy_id):
    with _lock:
        row = get_conn().execute(
            "SELECT * FROM escalation_policies WHERE id = ?", (policy_id,)
        ).fetchone()
        if row is None:
            return None
        steps = get_conn().execute(
            "SELECT * FROM escalation_steps WHERE policy_id = ? ORDER BY step_order",
            (policy_id,),
        ).fetchall()
    item = dict(row)
    item["enabled"] = bool(item["enabled"])
    item["business_hours_enabled"] = bool(item["business_hours_enabled"])
    item["steps"] = [dict(step) for step in steps]
    return item


def policy_name_exists(name, exclude_id=None):
    with _lock:
        row = get_conn().execute(
            "SELECT id FROM escalation_policies WHERE name = ? COLLATE NOCASE AND (? IS NULL OR id != ?)",
            (name, exclude_id, exclude_id),
        ).fetchone()
        return row is not None


def create_policy(data):
    now = _now()
    with transaction() as conn:
        cursor = conn.execute(
            "INSERT INTO escalation_policies (name, description, enabled, business_hours_enabled,"
            " business_start, business_end, business_days, repeat_minutes, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                data.get("name"), data.get("description", ""), 1 if data.get("enabled", True) else 0,
                1 if data.get("business_hours_enabled") else 0,
                data.get("business_start", "08:00"), data.get("business_end", "17:00"),
                data.get("business_days", "1,2,3,4,5"), int(data.get("repeat_minutes", 0) or 0),
                now, now,
            ),
        )
        policy_id = cursor.lastrowid
        _replace_steps(conn, policy_id, data.get("steps") or [])
        return policy_id


def update_policy(policy_id, data):
    now = _now()
    with transaction() as conn:
        cursor = conn.execute(
            "UPDATE escalation_policies SET name=?, description=?, enabled=?, business_hours_enabled=?,"
            " business_start=?, business_end=?, business_days=?, repeat_minutes=?, updated_at=?"
            " WHERE id=?",
            (
                data.get("name"), data.get("description", ""), 1 if data.get("enabled", True) else 0,
                1 if data.get("business_hours_enabled") else 0,
                data.get("business_start", "08:00"), data.get("business_end", "17:00"),
                data.get("business_days", "1,2,3,4,5"), int(data.get("repeat_minutes", 0) or 0),
                now, policy_id,
            ),
        )
        _replace_steps(conn, policy_id, data.get("steps") or [])
        return cursor.rowcount > 0


def _replace_steps(conn, policy_id, steps):
    conn.execute("DELETE FROM escalation_steps WHERE policy_id = ?", (policy_id,))
    for order, step in enumerate(steps, start=1):
        conn.execute(
            "INSERT INTO escalation_steps (policy_id, step_order, delay_minutes, target_type,"
            " target_id, business_hours_only) VALUES (?,?,?,?,?,?)",
            (
                policy_id, order, int(step.get("delay_minutes", 0) or 0),
                step.get("target_type", "channel"), int(step.get("target_id")),
                1 if step.get("business_hours_only") else 0,
            ),
        )


def delete_policy(policy_id):
    with transaction() as conn:
        cursor = conn.execute("DELETE FROM escalation_policies WHERE id = ?", (policy_id,))
        return cursor.rowcount > 0


# ---------------------------------------------------------
# State mesin eskalasi
# ---------------------------------------------------------

def register_escalation(problem_id, policy_id):
    now = _now()
    with transaction() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO escalation_state (problem_id, policy_id, last_step,"
            " created_at, updated_at) VALUES (?,?,0,?,?)",
            (problem_id, policy_id, now, now),
        )


def get_escalation_state(problem_id):
    with _lock:
        row = get_conn().execute(
            "SELECT * FROM escalation_state WHERE problem_id = ?", (problem_id,)
        ).fetchone()
    return dict(row) if row else None


def mark_escalation_step(problem_id, step, sent_at=None):
    stamp = sent_at or _now()
    with transaction() as conn:
        conn.execute(
            "UPDATE escalation_state SET last_step=?, last_sent_at=?, updated_at=? WHERE problem_id=?",
            (int(step), stamp, _now(), problem_id),
        )


def mark_escalation_recovered(problem_id):
    with transaction() as conn:
        conn.execute(
            "UPDATE escalation_state SET recovery_sent=1, updated_at=? WHERE problem_id=?",
            (_now(), problem_id),
        )


def stop_escalation(problem_id, reason):
    with transaction() as conn:
        conn.execute(
            "UPDATE escalation_state SET stopped_reason=?, updated_at=? WHERE problem_id=?",
            (str(reason)[:120], _now(), problem_id),
        )


def pending_escalations():
    with _lock:
        rows = get_conn().execute(
            """
            SELECT s.*, p.status AS problem_status, p.acked_at AS problem_acked_at,
                   p.started_at AS problem_started_at, p.device_id AS problem_device_id,
                   p.kind AS problem_kind, p.severity AS problem_severity,
                   p.message AS problem_message, p.suppressed_by_parent AS problem_suppressed,
                   pol.name AS policy_name, pol.enabled AS policy_enabled,
                   pol.business_hours_enabled AS policy_business_hours_enabled,
                   pol.business_start AS policy_business_start,
                   pol.business_end AS policy_business_end,
                   pol.business_days AS policy_business_days,
                   pol.repeat_minutes AS policy_repeat_minutes
            FROM escalation_state s
            JOIN problems p ON p.id = s.problem_id
            JOIN escalation_policies pol ON pol.id = s.policy_id
            WHERE s.recovery_sent = 0 AND s.stopped_reason IS NULL
            ORDER BY s.created_at
            """
        ).fetchall()
    return [dict(row) for row in rows]


def steps_for(policy_id):
    with _lock:
        rows = get_conn().execute(
            "SELECT * FROM escalation_steps WHERE policy_id = ? ORDER BY step_order",
            (policy_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def escalation_summary():
    with _lock:
        active = get_conn().execute(
            "SELECT COUNT(*) AS n FROM escalation_state WHERE recovery_sent=0 AND stopped_reason IS NULL"
        ).fetchone()["n"]
        stopped = get_conn().execute(
            "SELECT COUNT(*) AS n FROM escalation_state WHERE stopped_reason IS NOT NULL"
        ).fetchone()["n"]
    return {"active": active, "stopped": stopped}
