"""Backup basis data online (SQLite backup API) + rotasi + pruning.

Dipakai oleh poller (job harian) dan manage.py (backup-db). Tidak pernah
menyentuh berkas DB melalui salin file biasa — selalu lewat API backup
agar konsisten di tengah operasi tulis.
"""
import logging
import os
import re
import sqlite3
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_KEEP = 14
_BACKUP_RE = re.compile(r"^nms-(\d{8})-(\d{6})\.db$")

log = logging.getLogger("nms.dbbackup")

import db


def backup_settings():
    """(enabled, out_dir, keep). Env menimpa pengaturan bila diisi."""
    settings = db.get_settings()
    env_enabled = os.environ.get("DB_BACKUP_ENABLED", "").strip().lower()
    if env_enabled:
        enabled = env_enabled in ("1", "true", "yes", "on")
    else:
        enabled = str(settings.get("db_backup_enabled", "1")).strip() == "1"
    out_dir = (
        os.environ.get("DB_BACKUP_DIR", "").strip()
        or str(settings.get("db_backup_dir") or "").strip()
        or os.path.join(BASE_DIR, "backups")
    )
    raw_keep = os.environ.get("DB_BACKUP_KEEP", "").strip() or str(settings.get("db_backup_keep") or DEFAULT_KEEP).strip()
    try:
        keep = max(1, min(365, int(raw_keep)))
    except ValueError:
        keep = DEFAULT_KEEP
    return enabled, out_dir, keep


def run_backup(out_dir=None, keep=None, *, actor="system", ip="127.0.0.1"):
    """Backup online ke <out_dir>/nms-YYYYMMDD-HHMMSS.db. Verifikasi integritas, prune."""
    if out_dir is None or keep is None:
        _enabled, env_dir, env_keep = backup_settings()
        out_dir = out_dir or env_dir
        keep = keep or env_keep
    os.makedirs(out_dir, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    target = os.path.join(out_dir, f"nms-{ts}.db")

    src = sqlite3.connect(db.DB_PATH)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()

    # Backup memuat hash password + secret terenkripsi: batasi izin berkas.
    # Windows tidak mendukung mode POSIX penuh; kegagalan chmod tidak boleh
    # menggagalkan backup yang sudah valid.
    try:
        os.chmod(target, 0o600)
    except OSError:
        log.warning("Tidak dapat membatasi izin berkas backup %s", target, exc_info=True)

    check = sqlite3.connect(target)
    try:
        integrity = check.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        check.close()
    if integrity != "ok":
        try:
            os.remove(target)
        except OSError:
            pass
        raise RuntimeError(f"Hasil backup gagal verifikasi integrity_check ({integrity}); berkas dihapus.")

    pruned = prune_backups(out_dir, keep)
    db.add_audit_log(
        None, actor, ip, "db.backup", "database",
        f"Backup online tersimpan: {os.path.basename(target)} ({os.path.getsize(target)} byte); dihapus {pruned} berkas lama",
    )
    log.info("Backup basis data tersimpan di %s", target)
    return target


def prune_backups(out_dir, keep):
    candidates = []
    for name in os.listdir(out_dir):
        if _BACKUP_RE.match(name):
            path = os.path.join(out_dir, name)
            candidates.append((name, path))
    candidates.sort(key=lambda item: item[0])
    pruned = 0
    while len(candidates) > keep:
        name, path = candidates.pop(0)
        try:
            os.remove(path)
            pruned += 1
        except OSError:
            log.warning("Gagal menghapus backup lama %s", path)
    return pruned


def run_due_backup():
    """Dipanggil poller harian; tidak melakukan apa-apa bila dimatikan."""
    enabled, out_dir, keep = backup_settings()
    if not enabled:
        log.info("Backup harian dimatikan (db_backup_enabled=0).")
        return
    try:
        run_backup(out_dir, keep, actor="system", ip="127.0.0.1")
    except Exception:
        log.exception("Backup online harian gagal.")
        db.add_audit_log(None, "system", "127.0.0.1", "db.backup.failed", "database",
                         "Backup online harian gagal (lihat logs/nms.log)")
