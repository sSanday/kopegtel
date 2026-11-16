"""Koneksi SQLite + helper enkripsi (Fernet), lease instance, primitif bersama.

SQLite hanya punya SATU koneksi bersama (mode WAL, dipakai banyak thread).
Karena itu semua penulisan wajib dibungkus :func:`transaction`:

- commit hanya sekali, setelah blok selesai tanpa exception;
- exception apa pun me-rollback seluruh perubahan blok tersebut;
- transaksi bersarang ikut transaksi terluar (tidak ada commit di tengah);
- ``_lock`` juga menjadi jaring pengaman: transaksi yang masih terbuka saat
  lock dilepas akan di-rollback, sehingga commit thread lain tidak pernah
  menyimpan perubahan setengah jadi.

Pembacaan berat memakai :func:`readonly_connection` (koneksi terpisah) agar
tidak memegang lock global.
"""

import json
import logging
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from cryptography.fernet import Fernet, InvalidToken


BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DB_PATH = (
    os.environ.get("NMS_DB_PATH")
    or os.environ.get("DATABASE_PATH")
    or os.path.join(BASE_DIR, "backend", "nms.db")
)

log = logging.getLogger(__name__)

_conn = None

# Ditandai saat ada exception di dalam transaksi bersarang. Commit berikutnya
# harus dibatalkan agar sebagian perubahan tidak ikut tersimpan.
_tx_failed = False

# Kedalaman transaksi per thread (hanya pemilik _lock yang mengubahnya).
_tx_state = threading.local()

_fernet = None


class _TransactionConnection(sqlite3.Connection):
    """Koneksi bersama yang menolak commit setelah transaksi ditandai gagal."""

    def commit(self):
        global _tx_failed
        if _tx_failed:
            _tx_failed = False
            super().rollback()
            raise sqlite3.OperationalError(
                "Commit dibatalkan: transaksi memuat perubahan dari operasi yang gagal."
            )
        return super().commit()


class _DatabaseLock:
    """RLock yang menjaga batas transaksi SQLite.

    - Saat lock terluar diambil, sisa transaksi dari jalur luar di-rollback.
    - Saat exception keluar dari lock mana pun dan transaksi masih terbuka,
      transaksi ditandai gagal sehingga commit berikutnya dibatalkan.
    - Saat lock terluar dilepas, transaksi yang masih terbuka di-rollback
      (termasuk bila ada fungsi yang lupa commit).
    """

    def __init__(self):
        self._rlock = threading.RLock()
        self._depth = 0

    def __enter__(self):
        self._rlock.acquire()
        try:
            self._depth += 1
            if self._depth == 1:
                _rollback_orphan_transaction()
        except BaseException:
            self._depth -= 1
            self._rlock.release()
            raise
        return self

    def __exit__(self, exc_type, exc, tb):
        global _tx_failed
        try:
            conn = _conn
            if exc_type is not None:
                # Hanya tandai bila memang ada perubahan yang belum selesai.
                # Exception pada operasi baca murni tidak boleh memblokir commit
                # transaksi lain yang sah.
                if _has_open_transaction(conn):
                    _tx_failed = True
            self._depth -= 1
            if self._depth == 0 and conn is not None:
                if _has_open_transaction(conn):
                    if exc_type is None:
                        log.warning(
                            "Transaksi SQLite masih terbuka di akhir blok lock; "
                            "di-rollback agar tidak ikut commit thread lain."
                        )
                    _safe_rollback(conn)
                _tx_failed = False
        finally:
            self._rlock.release()
        return False


def _safe_rollback(conn):
    try:
        conn.rollback()
    except sqlite3.Error:
        log.exception("Gagal rollback transaksi SQLite")


def _has_open_transaction(conn):
    if conn is None:
        return False
    try:
        return bool(conn.in_transaction)
    except sqlite3.Error:
        return False


def _rollback_orphan_transaction():
    global _tx_failed
    conn = _conn
    if _has_open_transaction(conn):
        log.warning(
            "Sisa transaksi SQLite dari blok sebelumnya ditemukan; di-rollback."
        )
        _safe_rollback(conn)
    _tx_failed = False


def _mark_tx_failed():
    global _tx_failed
    _tx_failed = True


def _reset_tx_failed():
    global _tx_failed
    _tx_failed = False


_lock = _DatabaseLock()

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
            _conn = sqlite3.connect(
                DB_PATH, check_same_thread=False, factory=_TransactionConnection
            )
            _conn.row_factory = sqlite3.Row
            # Timpa isi halaman yang dihapus agar sisa kredensial tidak tertinggal
            # di ruang bebas SQLite/WAL setelah rotasi atau pembersihan data.
            _conn.execute("PRAGMA secure_delete=ON")
            _conn.execute("PRAGMA journal_mode=WAL")
            _conn.execute("PRAGMA foreign_keys=ON")
            _conn.execute("PRAGMA busy_timeout=5000")
        return _conn


@contextmanager
def transaction():
    """Satu unit kerja atomik pada koneksi SQLite bersama.

    Blok normal di-commit sekali saat keluar; exception apa pun me-rollback
    seluruh perubahan. Transaksi bersarang tidak commit sendiri: perubahan
    inner menjadi bagian transaksi terluar, dan kegagalan inner menandai
    transaksi terluar agar tidak pernah di-commit.
    """
    with _lock:
        conn = get_conn()
        depth = getattr(_tx_state, "depth", 0)
        if depth == 0:
            if _has_open_transaction(conn):
                _rollback_orphan_transaction()
            _reset_tx_failed()
        _tx_state.depth = depth + 1
        try:
            yield conn
        except BaseException:
            if depth == 0:
                _safe_rollback(conn)
                _reset_tx_failed()
            else:
                _mark_tx_failed()
            raise
        else:
            if depth == 0:
                try:
                    conn.commit()
                except BaseException:
                    _safe_rollback(conn)
                    _reset_tx_failed()
                    raise
        finally:
            _tx_state.depth = depth


@contextmanager
def readonly_connection():
    """Koneksi baca terpisah untuk query statistik berat.

    COUNT(*) pada tabel besar tidak boleh memegang ``_lock`` global: WAL
    mengizinkan pembaca paralel, sehingga poller dan request lain tetap jalan.
    Koneksi ini juga dipakai health check agar tidak menyentuh koneksi bersama
    milik thread lain. Hanya-baca agar tidak ada yang tidak sengaja menulis.
    """
    conn = sqlite3.connect(DB_PATH, timeout=5)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        yield conn
    finally:
        conn.close()

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
    token = uuid.uuid4().hex
    now = _now()
    with transaction() as conn:
        # Tabel lease adalah prasyarat untuk mengambil lease itu sendiri.
        # Dibuat minimal di sini agar database baru bisa mengambil lease
        # TANPA menjalankan migrasi skema penuh sebelum lease dipegang.
        conn.execute(
            "CREATE TABLE IF NOT EXISTS instance_lease ("
            " id INTEGER PRIMARY KEY CHECK (id = 1),"
            " pid INTEGER NOT NULL,"
            " token TEXT NOT NULL,"
            " heartbeat TEXT NOT NULL)"
        )
        row = conn.execute("SELECT pid, heartbeat FROM instance_lease WHERE id = 1").fetchone()
        if row is not None and _lease_fresh(row["heartbeat"]):
            return None
        conn.execute(
            "INSERT INTO instance_lease (id, pid, token, heartbeat) VALUES (1, ?, ?, ?)"
            " ON CONFLICT(id) DO UPDATE SET pid = excluded.pid, token = excluded.token,"
            " heartbeat = excluded.heartbeat",
            (os.getpid(), token, now),
        )
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
    with transaction() as conn:
        cur = conn.execute(
            "UPDATE instance_lease SET heartbeat = ? WHERE id = 1 AND token = ?",
            (_now(), token),
        )
        return cur.rowcount == 1

def release_instance_lease(token):
    """Lepaskan lease bila token cocok."""
    with transaction() as conn:
        conn.execute(
            "DELETE FROM instance_lease WHERE id = 1 AND token = ?", (token,)
        )

