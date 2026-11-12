"""Backup konfigurasi MikroTik (M6) + rotasi kunci terenkripsi.

Modul ini berisi penyimpanan/muatan backup (mt_backups) dan helper
rotasi ENCRYPTION_KEY; dibungkus publik oleh `import db`.
"""

import difflib

from .connection import (
    _lock,
    get_conn,
    _now,
    decrypt_secret,
    _get_fernet,
    Fernet,
)
from .settings import get_setting

def create_mt_backup(device_id, *, source, filename, size, sha256, content_encrypted, created_at=None):
    """Simpan backup; payload sudah terenkripsi. Kembalikan baris yang tersimpan."""
    ts = created_at or _now()
    with _lock:
        conn = get_conn()
        cursor = conn.execute(
            "INSERT INTO mt_backups (device_id, created_at, source, filename, size, sha256, content)"
            " VALUES (?,?,?,?,?,?,?)",
            (device_id, ts, source, filename, int(size), sha256, content_encrypted),
        )
        backup_id = cursor.lastrowid
        try:
            keep = max(1, int(get_setting("mt_backup_retention_versions", 30)))
        except (TypeError, ValueError):
            keep = 30
        conn.execute(
            "DELETE FROM mt_backups WHERE device_id = ? AND id NOT IN ("
            "SELECT id FROM mt_backups WHERE device_id = ?"
            " ORDER BY created_at DESC, id DESC LIMIT ?)",
            (device_id, device_id, keep),
        )
        conn.commit()
        row = conn.execute(
            "SELECT id, device_id, created_at, source, filename, size, sha256"
            " FROM mt_backups WHERE id = ?",
            (backup_id,),
        ).fetchone()
    return dict(row)

def list_mt_backups(device_id, *, limit=30):
    with _lock:
        rows = get_conn().execute(
            "SELECT id, device_id, created_at, source, filename, size, sha256"
            " FROM mt_backups WHERE device_id = ?"
            " ORDER BY created_at DESC, id DESC LIMIT ?",
            (device_id, min(500, max(1, int(limit)))),
        ).fetchall()
    return [dict(row) for row in rows]

def latest_mt_backup_at(device_id):
    with _lock:
        row = get_conn().execute(
            "SELECT MAX(created_at) AS ts FROM mt_backups WHERE device_id = ?", (device_id,)
        ).fetchone()
    return row["ts"] if row and row["ts"] else None

def get_mt_backup(device_id, backup_id):
    """Ambil backup lengkap; konten didekripsi. Return None bila tidak ada."""
    with _lock:
        row = get_conn().execute(
            "SELECT * FROM mt_backups WHERE device_id = ? AND id = ?",
            (device_id, backup_id),
        ).fetchone()
    if row is None:
        return None
    item = dict(row)
    item["content"] = decrypt_secret(item.get("content")) if item.get("content") else ""
    return item

def get_previous_mt_backup(device_id, created_at, backup_id):
    """Backup terakhir sebelum `created_at` (untuk diff otomatis)."""
    with _lock:
        row = get_conn().execute(
            "SELECT * FROM mt_backups WHERE device_id = ? AND"
            " (created_at < ? OR (created_at = ? AND id < ?))"
            " ORDER BY created_at DESC, id DESC LIMIT 1",
            (device_id, created_at, created_at, backup_id),
        ).fetchone()
    if row is None:
        return None
    item = dict(row)
    item["content"] = decrypt_secret(item.get("content")) if item.get("content") else ""
    return item

def mt_backup_diff(previous_text, current_text, *, fromfile="sebelumnya", tofile="sekarang", context=3):
    """Unified diff antar dua teks ekspor (sudah didekripsi)."""
    import difflib

    before = (previous_text or "").splitlines()
    after = (current_text or "").splitlines()
    diff = list(
        difflib.unified_diff(
            before, after,
            fromfile=fromfile, tofile=tofile,
            lineterm="", n=max(0, int(context)),
        )
    )
    return "\n".join(diff)

ENCRYPTED_FIELDS = (
    ("devices", ("snmp_community", "snmp_v3_auth_password", "snmp_v3_priv_password", "mikrotik_password")),
    ("notification_channels", ("config",)),
    ("mt_device_settings", ("write_password", "ssh_password")),
    ("mt_backups", ("content",)),
    ("ingest_receivers", ("config",)),
    ("users", ("mfa_secret", "mfa_pending_secret")),
)

def encrypted_value_stats(decrypt_fn=None):
    """Statistik dekripsi per kolom. TIDAK mengembalikan nilai apa pun."""
    decrypt_fn = decrypt_fn or decrypt_secret
    stats = []
    with _lock:
        for table, cols in ENCRYPTED_FIELDS:
            for col in cols:
                rows = get_conn().execute(
                    f"SELECT rowid AS _rid, {col} FROM {table} WHERE {col} IS NOT NULL AND {col} != ''"
                ).fetchall()
                total, ok, failed = len(rows), 0, 0
                for row in rows:
                    try:
                        decrypt_fn(row[col])
                        ok += 1
                    except Exception:
                        failed += 1
                stats.append({"table": table, "column": col, "total": total, "ok": ok, "failed": failed})
    return stats

def reencrypt_all(new_key):
    """Dekripsi dengan kunci ENCRYPTION_KEY saat ini, enkripsi ulang dengan ``new_key``.

    Seluruh kolom dalam SATU transaksi; bila ada satu nilai yang gagal
    didekripsi, transaksi di-rollback seluruhnya dan kegagalan dilaporkan.
    Return (jumlah_tertulis, daftar_kegagalan).
    """
    from cryptography.fernet import Fernet

    new_fernet = Fernet(new_key)
    written, failed = 0, []
    with _lock:
        conn = get_conn()
        try:
            for table, cols in ENCRYPTED_FIELDS:
                for col in cols:
                    rows = conn.execute(
                        f"SELECT rowid AS _rid, {col} FROM {table} WHERE {col} IS NOT NULL AND {col} != ''"
                    ).fetchall()
                    for row in rows:
                        value = row[col]
                        try:
                            plain = _get_fernet().decrypt(value.encode("utf-8")).decode("utf-8")
                        except Exception:
                            failed.append(f"{table}.{col} (rowid {row['_rid']})")
                            continue
                        conn.execute(
                            f"UPDATE {table} SET {col} = ? WHERE rowid = ?",
                            (new_fernet.encrypt(plain.encode("utf-8")).decode("utf-8"), row["_rid"]),
                        )
                        written += 1
            if failed:
                conn.rollback()
                return 0, failed
            conn.commit()
            return written, []
        except Exception:
            conn.rollback()
            raise

