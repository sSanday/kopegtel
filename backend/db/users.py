"""Pengguna & hak akses, batas percobaan login."""

from datetime import datetime, timedelta, timezone

from .connection import (
    _lock,
    _now,
    get_conn,
)

def find_user(username):
    with _lock:
        return get_conn().execute(
            "SELECT * FROM users WHERE username = ? COLLATE NOCASE", (username,)
        ).fetchone()

def find_user_by_id(user_id):
    with _lock:
        return get_conn().execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()

def create_user(username, password_hash, role="admin", must_change_password=0):
    with _lock:
        conn = get_conn()
        cursor = conn.execute(
            "INSERT INTO users (username, password_hash, role, must_change_password, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?)",
            (username, password_hash, role, int(must_change_password), _now(), _now()),
        )
        conn.commit()
        return cursor.lastrowid

def update_user(user_id, *, role=None, password_hash=None, must_change_password=None):
    assignments, values = ["updated_at=?"], [_now()]
    if role is not None:
        assignments.append("role=?")
        values.append(role)
    if password_hash is not None:
        assignments.append("password_hash=?")
        values.append(password_hash)
    if must_change_password is not None:
        assignments.append("must_change_password=?")
        values.append(int(must_change_password))
    values.append(user_id)
    with _lock:
        conn = get_conn()
        cursor = conn.execute(f"UPDATE users SET {', '.join(assignments)} WHERE id=?", values)
        conn.commit()
        return cursor.rowcount > 0

def delete_user(user_id):
    with _lock:
        conn = get_conn()
        cursor = conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        conn.commit()
        return cursor.rowcount > 0

def user_exists(username, exclude_id=None):
    with _lock:
        row = get_conn().execute(
            "SELECT id FROM users WHERE username = ? COLLATE NOCASE AND (? IS NULL OR id != ?)",
            (username, exclude_id, exclude_id),
        ).fetchone()
        return row is not None

def list_users():
    with _lock:
        rows = get_conn().execute(
            "SELECT id, username, role, must_change_password, created_at, updated_at,"
            " email, telegram_chat_id, mfa_enabled FROM users ORDER BY username COLLATE NOCASE"
        ).fetchall()
    return [dict(r) for r in rows]


def update_user_contacts(user_id, *, email=None, telegram_chat_id=None):
    assignments, values = ["updated_at=?"], [_now()]
    if email is not None:
        assignments.append("email=?")
        values.append(str(email).strip()[:255])
    if telegram_chat_id is not None:
        assignments.append("telegram_chat_id=?")
        values.append(str(telegram_chat_id).strip()[:64])
    values.append(user_id)
    with _lock:
        conn = get_conn()
        cursor = conn.execute(
            f"UPDATE users SET {', '.join(assignments)} WHERE id=?", values
        )
        conn.commit()
        return cursor.rowcount > 0

def user_count():
    with _lock:
        return get_conn().execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]

def count_recent_login_failures(username, ip, window_minutes):
    threshold = (datetime.now(timezone.utc) - timedelta(minutes=window_minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with _lock:
        row = get_conn().execute(
            "SELECT COUNT(*) AS n FROM login_attempts WHERE lower(username) = lower(?) AND ip = ? AND failed_at >= ?",
            (username, ip, threshold),
        ).fetchone()
    return row["n"]

def record_login_failure(username, ip):
    with _lock:
        conn = get_conn()
        conn.execute(
            "INSERT INTO login_attempts (username, ip, failed_at) VALUES (?,?,?)",
            (username, ip, _now()),
        )
        # Sapu catatan lebih dari 1 hari.
        conn.execute(
            "DELETE FROM login_attempts WHERE failed_at < ?",
            ((datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),),
        )
        conn.commit()

def clear_login_failures(username, ip):
    with _lock:
        conn = get_conn()
        conn.execute(
            "DELETE FROM login_attempts WHERE lower(username) = lower(?) AND ip = ?",
            (username, ip),
        )
        conn.commit()

