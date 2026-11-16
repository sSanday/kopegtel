"""E4 — Token API baca-saja untuk Prometheus (/metrics).

Token 32 byte acak, disimpan sebagai hash SHA-256, dibandingkan constant-time,
memiliki masa kedaluwarsa dan daftar IP opsional, serta dapat dicabut. Token
hanya berlaku untuk scrape /metrics — tidak bisa dipakai endpoint lain.
"""

import hashlib
import hmac
import json
import secrets
from datetime import datetime, timedelta, timezone

from .connection import (
    _lock,
    _now,
    get_conn,
    transaction,
)

TOKEN_PREFIX = "nms_"
TOKEN_BYTES = 32
DEFAULT_SCOPES = ["metrics"]


def _hash_token(token):
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(ts):
    if not ts:
        return None
    try:
        parsed = datetime.strptime(str(ts), "%Y-%m-%dT%H:%M:%SZ")
        return parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _row_to_dict(row):
    item = dict(row)
    item.pop("token_hash", None)
    item["scopes"] = _parse_json_list(item.get("scopes"))
    item["allowed_ips"] = _parse_allowed_ips(item.get("allowed_ips"))
    item["expires_at"] = item.get("expires_at") or None
    item["revoked"] = bool(item.get("revoked_at"))
    return item


def _parse_json_list(value):
    try:
        parsed = json.loads(value or "[]")
        return [str(item) for item in parsed if str(item)]
    except (TypeError, ValueError):
        return []


def _parse_allowed_ips(value):
    text = str(value or "").strip()
    return [part.strip() for part in text.split(",") if part.strip()]


def create_api_token(name, *, scopes=None, expires_in_days=None, allowed_ips=None,
                     created_by="-"):
    name = str(name or "").strip()
    if not name or len(name) > 80:
        return None, "Nama token wajib diisi (maks 80 karakter)."
    token = TOKEN_PREFIX + secrets.token_urlsafe(TOKEN_BYTES)
    token_hash = _hash_token(token)
    expires_at = None
    if expires_in_days:
        try:
            days = int(expires_in_days)
            if days < 1:
                return None, "Kedaluwarsa minimal 1 hari."
            expires_at = (
                datetime.now(timezone.utc) + timedelta(days=days)
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
        except (TypeError, ValueError):
            return None, "Kedaluwarsa harus berupa angka hari."
    scopes_list = [str(scope).strip() for scope in (scopes or []) if str(scope).strip()]
    if not scopes_list:
        scopes_list = DEFAULT_SCOPES
    ips = [str(item).strip() for item in (allowed_ips or []) if str(item).strip()]
    with transaction() as conn:
        exists = conn.execute(
            "SELECT id FROM api_tokens WHERE name = ? COLLATE NOCASE", (name,)
        ).fetchone()
        if exists:
            return None, "Nama token sudah dipakai."
        cursor = conn.execute(
            "INSERT INTO api_tokens (name, token_hash, token_prefix, scopes, expires_at,"
            " allowed_ips, created_at, created_by) VALUES (?,?,?,?,?,?,?,?)",
            (name, token_hash, token[:12],
             json.dumps(scopes_list, ensure_ascii=False), expires_at,
             ",".join(ips), _now(), str(created_by or "-")[:60]),
        )
        return token, {
            "id": cursor.lastrowid, "name": name, "token_prefix": token[:12],
            "scopes": scopes_list, "expires_at": expires_at, "allowed_ips": ips,
            "created_at": _now_iso(), "created_by": str(created_by or "-")[:60],
        }


def list_api_tokens():
    with _lock:
        rows = get_conn().execute(
            "SELECT id, name, token_prefix, scopes, expires_at, allowed_ips, created_at,"
            " last_used_at, created_by, revoked_at FROM api_tokens"
            " ORDER BY name COLLATE NOCASE"
        ).fetchall()
    return [_row_to_dict(row) for row in rows]


def get_api_token(token_id):
    with _lock:
        row = get_conn().execute("SELECT * FROM api_tokens WHERE id = ?", (token_id,)).fetchone()
    return _row_to_dict(row) if row else None


def revoke_api_token(token_id):
    with transaction() as conn:
        cursor = conn.execute(
            "UPDATE api_tokens SET revoked_at=? WHERE id=? AND revoked_at IS NULL",
            (_now(), int(token_id)),
        )
        return cursor.rowcount > 0


def verify_api_token(token, *, ip=None):
    """Verifikasi token. Return record (dict) atau None. Update last_used_at."""
    candidate = str(token or "").strip()
    if not candidate or not candidate.startswith(TOKEN_PREFIX):
        return None
    digest = _hash_token(candidate)
    with transaction() as conn:
        rows = conn.execute(
            "SELECT * FROM api_tokens WHERE revoked_at IS NULL"
        ).fetchall()
        match = None
        for row in rows:
            if hmac.compare_digest(row["token_hash"], digest):
                match = row
                break
        if match is None:
            return None
        if match["expires_at"] and _parse_iso(match["expires_at"]) < datetime.now(timezone.utc):
            return None
        allowed = _parse_allowed_ips(match["allowed_ips"])
        if allowed and ip not in allowed:
            return None
        now = _now_iso()
        conn.execute(
            "UPDATE api_tokens SET last_used_at=? WHERE id=?",
            (now, match["id"]),
        )
    item = _row_to_dict(match)
    item["last_used_at"] = now
    return item
