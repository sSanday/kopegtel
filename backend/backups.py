"""Orkestrasi backup konfigurasi MikroTik (M6). Dipakai oleh poller dan API."""
import hashlib
import re
from datetime import datetime, timedelta, timezone

import db
from collectors import mikrotik_backup


def _audit(actor, ip, action, device_id, summary):
    db.add_audit_log(None, actor or "system", ip or "127.0.0.1", action, f"device:{device_id}", summary)


def run_backup(device_id, *, source="manual", actor="system", ip="127.0.0.1"):
    """Jalankan backup satu perangkat. Return dict backup tersimpan.

    Raise ValueError untuk konfigurasi kurang, BackupError untuk masalah SSH.
    """
    device = db.get_device(device_id)
    if device is None:
        raise ValueError("Perangkat tidak ditemukan.")
    if not (device.get("type") == "mikrotik" or device.get("mikrotik_enabled")):
        raise ValueError("Perangkat bukan tipe MikroTik.")

    settings = db.get_mt_device_settings(device_id, include_secrets=True)
    if not settings.get("ssh_enabled"):
        raise ValueError("SSH belum diaktifkan untuk perangkat ini.")
    if not settings.get("has_ssh_credentials"):
        raise ValueError("Kredensial SSH belum diatur untuk perangkat ini.")
    try:
        password = db.decrypt_secret(settings.get("ssh_password"))
    except RuntimeError as exc:
        raise ValueError(str(exc)) from exc

    try:
        content = mikrotik_backup.run_export(
            host=device["ip"],
            port=settings.get("ssh_port") or 22,
            user=settings.get("ssh_user") or "",
            password=password or "",
            host_key=settings.get("ssh_host_key") or "",
        )
    except mikrotik_backup.BackupError as exc:
        _audit(actor, ip, "mikrotik.backup.failed", device_id, f"Backup {source} gagal ({exc.code}): {exc}")
        raise
    except Exception as exc:  # noqa: BLE001
        _audit(actor, ip, "mikrotik.backup.failed", device_id, f"Backup {source} gagal: {exc}")
        raise

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", str(device.get("name") or "mikrotik")).strip("-") or "mikrotik"
    filename = f"{slug}-{stamp}.rsc"
    payload = content.encode("utf-8")
    sha256 = hashlib.sha256(payload).hexdigest()
    saved = db.create_mt_backup(
        device_id,
        source=source,
        filename=filename,
        size=len(payload),
        sha256=sha256,
        content_encrypted=db.encrypt_secret(content),
    )
    _audit(
        actor, ip, "mikrotik.backup", device_id,
        f"Backup {source} tersimpan: {filename} ({saved['size']} byte, sha256 {sha256[:12]})",
    )
    return saved


def run_due_backups(now_utc=None):
    """Backup terjadwal: perangkat dengan backup_enabled, di jam yang diatur.

    Dipanggil poller tiap 10 menit; hanya sekali per perangkat per hari (22 jam).
    """
    tz_name = (db.get_setting("timezone", "Asia/Jakarta") or "Asia/Jakarta").strip()
    offset_hours = 7 if tz_name == "Asia/Jakarta" else 0
    now = now_utc or datetime.now(timezone.utc)
    local_now = now + timedelta(hours=offset_hours)

    candidates = [
        device for device in db.list_devices()
        if device.get("type") == "mikrotik" or device.get("mikrotik_enabled")
    ]
    for device in candidates:
        settings = db.get_mt_device_settings(device["id"])
        if not settings.get("backup_enabled"):
            continue
        if not settings.get("ssh_enabled") or not settings.get("has_ssh_credentials"):
            continue
        raw_hour = settings.get("backup_hour")
        try:
            backup_hour = 2 if raw_hour is None else int(raw_hour)
        except (TypeError, ValueError):
            backup_hour = 2
        if local_now.hour != backup_hour:
            continue
        last = db.latest_mt_backup_at(device["id"])
        if last:
            try:
                last_at = datetime.strptime(last, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            except ValueError:
                last_at = None
            if last_at and (now - last_at) < timedelta(hours=22):
                continue
        try:
            run_backup(device["id"], source="scheduled", actor="system", ip="127.0.0.1")
        except Exception as exc:  # noqa: BLE001 — sudah diaudit; jangan hentikan perangkat lain
            db.add_audit_log(None, "system", "127.0.0.1", "mikrotik.backup.failed",
                             f"device:{device['id']}", f"Backup terjadwal gagal: {exc}")
    return True


def backup_diff(device_id, backup_id, other_id=None):
    """Unified diff antara dua versi. Return (diff_text, from_name, to_name) atau None bila tak ada pembanding."""
    current = db.get_mt_backup(device_id, backup_id)
    if current is None:
        return None
    if other_id is not None:
        previous = db.get_mt_backup(device_id, other_id)
        if previous is None:
            return {"error": "Versi pembanding tidak ditemukan.", "status": 404}
    else:
        previous = db.get_previous_mt_backup(device_id, current["created_at"], backup_id)
        if previous is None:
            return {"diff": "", "from": None, "to": current["filename"],
                    "note": "Belum ada versi pembanding."}
    diff_text = db.mt_backup_diff(
        previous["content"], current["content"],
        fromfile=previous["filename"], tofile=current["filename"],
    )
    return {"diff": diff_text, "from": previous["filename"], "to": current["filename"]}
