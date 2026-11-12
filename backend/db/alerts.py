"""Kanal notifikasi, template pesan, aturan aksi, action_log."""

import json

from .connection import (
    _lock,
    _now,
    decrypt_secret,
    encrypt_secret,
    get_conn,
)

def list_channels():
    with _lock:
        rows = get_conn().execute("SELECT * FROM notification_channels ORDER BY name COLLATE NOCASE").fetchall()
    channels = []
    for row in rows:
        try:
            config = json.loads(decrypt_secret(row["config"])) if row["config"] else {}
        except Exception:
            config = {}
        public = {}
        for key, value in config.items():
            if key == "headers":
                public["headers"] = {k: None for k in (value or {})} if isinstance(value, dict) else {}
                public["has_headers"] = bool(value)
                continue
            public[key] = None if key in ("bot_token", "password", "auth_password", "secret") else value
            public[f"has_{key}"] = bool(value)
        public.pop("bot_token", None)
        public.pop("password", None)
        public.pop("secret", None)
        channels.append(
            {
                "id": row["id"], "name": row["name"], "type": row["type"],
                "enabled": bool(row["enabled"]), "config": public,
                "created_at": row["created_at"], "updated_at": row["updated_at"],
            }
        )
    return channels

def get_channel(channel_id):
    with _lock:
        return get_conn().execute("SELECT * FROM notification_channels WHERE id = ?", (channel_id,)).fetchone()

def channel_config(channel_id):
    row = get_channel(channel_id)
    if row is None:
        return None
    try:
        return json.loads(decrypt_secret(row["config"])) if row["config"] else {}
    except Exception:
        return {}

def create_channel(name, type_, config, enabled=True):
    now = _now()
    with _lock:
        conn = get_conn()
        cursor = conn.execute(
            "INSERT INTO notification_channels (name, type, config, enabled, created_at, updated_at) VALUES (?,?,?,?,?,?)",
            (name.strip(), type_, encrypt_secret(json.dumps(config or {}, ensure_ascii=False)), 1 if enabled else 0, now, now),
        )
        conn.commit()
        return cursor.lastrowid

def update_channel(channel_id, name, type_, config, enabled):
    """config=None → pertahankan kredensial tersimpan."""
    now = _now()
    existing = get_channel(channel_id)
    if existing is None:
        return False
    if config is None:
        merged = channel_config(channel_id) or {}
    else:
        merged = dict(config)
        # Kosongkan secret yang dibiarkan kosong → pertahankan yang tersimpan.
        old = channel_config(channel_id) or {}
        for key in ("bot_token", "password", "auth_password", "secret"):
            if not merged.get(key) and old.get(key):
                merged[key] = old[key]
        # Daftar header dibiarkan apa adanya bila kanal diedit tanpa mengisinya.
        if "headers" not in merged and old.get("headers"):
            merged["headers"] = old["headers"]
    with _lock:
        conn = get_conn()
        cursor = conn.execute(
            "UPDATE notification_channels SET name=?, type=?, config=?, enabled=?, updated_at=? WHERE id=?",
            (name.strip(), type_, encrypt_secret(json.dumps(merged, ensure_ascii=False)), 1 if enabled else 0, now, channel_id),
        )
        conn.commit()
        return cursor.rowcount > 0

def delete_channel(channel_id):
    with _lock:
        conn = get_conn()
        cursor = conn.execute("DELETE FROM notification_channels WHERE id = ?", (channel_id,))
        conn.commit()
        return cursor.rowcount > 0

def channel_name_exists(name, exclude_id=None):
    with _lock:
        row = get_conn().execute(
            "SELECT id FROM notification_channels WHERE name = ? COLLATE NOCASE AND (? IS NULL OR id != ?)",
            (name, exclude_id, exclude_id),
        ).fetchone()
        return row is not None

def list_templates():
    with _lock:
        rows = get_conn().execute("SELECT * FROM message_templates ORDER BY name COLLATE NOCASE").fetchall()
    return [dict(r) for r in rows]

def get_template(template_id):
    with _lock:
        return get_conn().execute("SELECT * FROM message_templates WHERE id = ?", (template_id,)).fetchone()

def template_name_exists(name, exclude_id=None):
    with _lock:
        row = get_conn().execute(
            "SELECT id FROM message_templates WHERE name = ? COLLATE NOCASE AND (? IS NULL OR id != ?)",
            (name, exclude_id, exclude_id),
        ).fetchone()
        return row is not None

def create_template(name, body):
    now = _now()
    with _lock:
        conn = get_conn()
        cursor = conn.execute(
            "INSERT INTO message_templates (name, body, created_at, updated_at) VALUES (?,?,?,?)",
            (name.strip(), body.strip(), now, now),
        )
        conn.commit()
        return cursor.lastrowid

def update_template(template_id, name, body):
    now = _now()
    with _lock:
        conn = get_conn()
        cursor = conn.execute(
            "UPDATE message_templates SET name=?, body=?, updated_at=? WHERE id=?",
            (name.strip(), body.strip(), now, template_id),
        )
        conn.commit()
        return cursor.rowcount > 0

def delete_template(template_id):
    with _lock:
        conn = get_conn()
        cursor = conn.execute("DELETE FROM message_templates WHERE id = ?", (template_id,))
        conn.commit()
        return cursor.rowcount > 0

def list_rules():
    with _lock:
        rows = get_conn().execute(
            """
            SELECT r.*, c.name AS channel_name, c.type AS channel_type, t.name AS template_name,
                   g.name AS group_name, d.name AS device_name, p.name AS escalation_policy_name
            FROM action_rules r
            JOIN notification_channels c ON c.id = r.channel_id
            JOIN message_templates t ON t.id = r.template_id
            LEFT JOIN host_groups g ON g.id = r.group_id
            LEFT JOIN devices d ON d.id = r.device_id
            LEFT JOIN escalation_policies p ON p.id = r.escalation_policy_id
            ORDER BY r.name COLLATE NOCASE
            """
        ).fetchall()
    return [dict(r) for r in rows]

def get_rule(rule_id):
    with _lock:
        return get_conn().execute("SELECT * FROM action_rules WHERE id = ?", (rule_id,)).fetchone()

def rule_name_exists(name, exclude_id=None):
    with _lock:
        row = get_conn().execute(
            "SELECT id FROM action_rules WHERE name = ? COLLATE NOCASE AND (? IS NULL OR id != ?)",
            (name, exclude_id, exclude_id),
        ).fetchone()
        return row is not None

def create_rule(data):
    with _lock:
        conn = get_conn()
        cursor = conn.execute(
            "INSERT INTO action_rules (name, min_severity, group_id, device_id, channel_id, template_id, escalation_policy_id, enabled, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (data["name"], data.get("min_severity", "warning"), data.get("group_id"), data.get("device_id"),
             data["channel_id"], data["template_id"], data.get("escalation_policy_id"),
             1 if data.get("enabled", True) else 0, _now()),
        )
        conn.commit()
        return cursor.lastrowid

def update_rule(rule_id, data):
    with _lock:
        conn = get_conn()
        cursor = conn.execute(
            "UPDATE action_rules SET name=?, min_severity=?, group_id=?, device_id=?, channel_id=?, template_id=?, escalation_policy_id=?, enabled=? WHERE id=?",
            (data["name"], data.get("min_severity", "warning"), data.get("group_id"), data.get("device_id"),
             data["channel_id"], data["template_id"], data.get("escalation_policy_id"),
             1 if data.get("enabled", True) else 0, rule_id),
        )
        conn.commit()
        return cursor.rowcount > 0

def delete_rule(rule_id):
    with _lock:
        conn = get_conn()
        cursor = conn.execute("DELETE FROM action_rules WHERE id = ?", (rule_id,))
        conn.commit()
        return cursor.rowcount > 0

def list_action_log(limit=100, offset=0, status=None):
    limit = min(500, max(1, int(limit or 100)))
    where, params = [], []
    if status:
        where.append("status = ?")
        params.append(status)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    with _lock:
        total = get_conn().execute(f"SELECT COUNT(*) AS n FROM action_log {where_sql}", params).fetchone()["n"]
        rows = get_conn().execute(
            f"""
            SELECT a.*, r.name AS rule_name, c.name AS channel_name, c.type AS channel_type, d.name AS device_name
            FROM action_log a
            LEFT JOIN action_rules r ON r.id = a.rule_id
            LEFT JOIN notification_channels c ON c.id = a.channel_id
            LEFT JOIN devices d ON d.id = a.device_id
            {where_sql} ORDER BY a.id DESC LIMIT ? OFFSET ?
            """,
            [*params, limit, int(offset or 0)],
        ).fetchall()
    return {"total": total, "entries": [dict(r) for r in rows]}

def add_action_log(rule_id, channel_id, device_id, problem_id, status, detail):
    with _lock:
        conn = get_conn()
        conn.execute(
            "INSERT INTO action_log (ts, rule_id, channel_id, device_id, problem_id, status, detail) VALUES (?,?,?,?,?,?,?)",
            (_now(), rule_id, channel_id, device_id, problem_id, status, (detail or "")[:400]),
        )
        conn.commit()

