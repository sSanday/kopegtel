"""Koneksi SQLite + helper enkripsi (Fernet), lease instance, primitif bersama."""

import json
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
import uuid
from cryptography.fernet import Fernet, InvalidToken


BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DB_PATH = (
    os.environ.get("NMS_DB_PATH")
    or os.environ.get("DATABASE_PATH")
    or os.path.join(BASE_DIR, "backend", "nms.db")
)

_lock = threading.RLock()

_conn = None

_fernet = None

def _get_fernet():
    global _fernet
    if _fernet is None:
        key = os.environ.get("ENCRYPTION_KEY", "").strip()
        if not key:
            raise RuntimeError(
                "ENCRYPTION_KEY belum diatur. Salin .env.example menjadi .env dan "
                "isi ENCRYPTION_KEY (py -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\")."
            )
        _fernet = Fernet(key.encode("utf-8"))
    return _fernet

def encrypt_secret(value):
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    return _get_fernet().encrypt(value.encode("utf-8")).decode("utf-8")

def decrypt_secret(token):
    if not token:
        return None
    try:
        return _get_fernet().decrypt(token.encode("utf-8")).decode("utf-8")
    except InvalidToken:
        raise RuntimeError("Kredensial tersimpan gagal didekripsi — ENCRYPTION_KEY berubah?")

def get_conn():
    global _conn
    with _lock:
        if _conn is None:
            _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
            _conn.row_factory = sqlite3.Row
            # Timpa isi halaman yang dihapus agar sisa kredensial tidak tertinggal
            # di ruang bebas SQLite/WAL setelah rotasi atau pembersihan data.
            _conn.execute("PRAGMA secure_delete=ON")
            _conn.execute("PRAGMA journal_mode=WAL")
            _conn.execute("PRAGMA foreign_keys=ON")
            _conn.execute("PRAGMA busy_timeout=5000")
        return _conn

def _now():
    """Waktu UTC dalam format 'YYYY-MM-DDTHH:MM:SSZ' (semua timestamp di DB)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def _after_minutes(minutes):
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")

def _days_ago(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")

def parse_ts(text):
    """Parse timestamp UTC ('YYYY-MM-DDTHH:MM:SSZ' atau naif) -> datetime aware (UTC)."""
    s = str(text or "").strip()
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        # Cadangan: format lama 'YYYY-MM-DD HH:MM:SS'
        try:
            dt = datetime.strptime(str(text).strip(), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt

def _json_loads(text, default):
    try:
        value = json.loads(text or "")
        return value if isinstance(value, type(default)) else default
    except (TypeError, ValueError):
        return default

_LEASE_STALE_SECONDS = 30

def _lease_fresh(heartbeat, now=None):
    try:
        beat = datetime.strptime(heartbeat, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return False
    current = now or datetime.now(timezone.utc)
    return (current - beat).total_seconds() < _LEASE_STALE_SECONDS

def acquire_instance_lease():
    """Ambil lease instance. Return token, atau None bila ada instance lain yang masih segar."""
    import uuid

    token = uuid.uuid4().hex
    now = _now()
    with _lock:
        conn = get_conn()
        row = conn.execute("SELECT pid, heartbeat FROM instance_lease WHERE id = 1").fetchone()
        if row is not None and _lease_fresh(row["heartbeat"]):
            return None
        conn.execute(
            "INSERT INTO instance_lease (id, pid, token, heartbeat) VALUES (1, ?, ?, ?)"
            " ON CONFLICT(id) DO UPDATE SET pid = excluded.pid, token = excluded.token,"
            " heartbeat = excluded.heartbeat",
            (os.getpid(), token, now),
        )
        conn.commit()
    return token

def instance_lease_status():
    """Info lease saat ini (tanpa token). None bila tidak ada."""
    with _lock:
        row = get_conn().execute(
            "SELECT pid, heartbeat FROM instance_lease WHERE id = 1"
        ).fetchone()
    return dict(row) if row else None

def heartbeat_instance_lease(token):
    """Perbarui heartbeat. Return False bila token tidak cocok (lease hilang)."""
    with _lock:
        cur = get_conn().execute(
            "UPDATE instance_lease SET heartbeat = ? WHERE id = 1 AND token = ?",
            (_now(), token),
        )
        get_conn().commit()
        return cur.rowcount == 1

def release_instance_lease(token):
    """Lepaskan lease bila token cocok."""
    with _lock:
        get_conn().execute(
            "DELETE FROM instance_lease WHERE id = 1 AND token = ?", (token,)
        )
        get_conn().commit()

