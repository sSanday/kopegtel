"""Blueprint Administrasi: audit log, laporan, pengaturan, pengguna."""

import csv
import io
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone

from flask import Blueprint, Response, g, jsonify, request
from werkzeug.security import generate_password_hash

import auth
import db
import poller
from core.constants import SLA_PERIODS
from core.env import APP_VERSION, SERVER_STARTED
from core.guard import audit, roles

bp = Blueprint("admin", __name__)

# ---------------------------------------------------------
# Audit log & laporan
# ---------------------------------------------------------

@bp.get("/api/audit-log")
@roles("admin")
def api_audit_log():
    search = (request.args.get("search") or "").strip() or None
    action = (request.args.get("action") or "").strip() or None
    try:
        limit = min(500, max(1, int(request.args.get("limit", "100"))))
        offset = max(0, int(request.args.get("offset", "0")))
    except ValueError:
        limit, offset = 100, 0
    return jsonify(db.list_audit_log(search=search, action=action, limit=limit, offset=offset))


@bp.get("/api/audit-log.csv")
@roles("admin")
def api_audit_log_csv():
    search = (request.args.get("search") or "").strip() or None
    action = (request.args.get("action") or "").strip() or None
    data = db.list_audit_log(search=search, action=action, limit=5000, offset=0)
    audit("report.audit_csv", "report", "Mengunduh audit log")
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Waktu (UTC)", "Pengguna", "IP", "Aksi", "Objek", "Ringkasan"])
    for entry in data["entries"]:
        writer.writerow([entry["ts"], entry["username"], entry["ip"], entry["action"], entry["object"], entry["summary"]])
    return Response(
        "﻿" + output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=audit-log.csv"},
    )


@bp.get("/api/reports/availability")
def api_report_availability():
    period = (request.args.get("period") or "7").strip()
    days = SLA_PERIODS.get(period)
    if days is None:
        return jsonify({"error": "Periode tidak valid. Gunakan 1, 7, atau 30."}), 400
    rows = []
    for device in db.list_devices():
        stats = db.availability(device["id"], days)
        rows.append(
            {
                "device_id": device["id"],
                "device": device["name"],
                "ip": device["ip"],
                "type": device["type"],
                "group_name": device.get("group_name"),
                "checks": stats["checks"],
                "up": stats["up"],
                "pct": stats["pct"],
            }
        )
    return jsonify({"period_days": days, "rows": rows})


@bp.get("/api/reports/availability.csv")
def api_report_availability_csv():
    period = (request.args.get("period") or "7").strip()
    days = SLA_PERIODS.get(period)
    if days is None:
        return jsonify({"error": "Periode tidak valid. Gunakan 1, 7, atau 30."}), 400
    rows = []
    for device in db.list_devices():
        stats = db.availability(device["id"], days)
        rows.append((device, stats))
    audit("report.availability_csv", "report", f"Mengunduh laporan ketersediaan {days} hari")
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Perangkat", "IP", "Tipe", "Grup", "Pemeriksaan", "Up", "Ketersediaan %"])
    for device, stats in rows:
        writer.writerow([
            device["name"], device["ip"], device["type"], device.get("group_name") or "",
            stats["checks"], stats["up"], stats["pct"] if stats["pct"] is not None else "—",
        ])
    return Response(
        "﻿" + output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=ketersediaan-{days}h.csv"},
    )


@bp.get("/api/reports/top-problems")
def api_report_top_problems():
    try:
        period = min(90, max(1, int(request.args.get("period", "7"))))
    except ValueError:
        period = 7
    since = (datetime.now(timezone.utc) - timedelta(days=period)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = db.top_problems(since, limit=20)
    return jsonify({"period_days": period, "rows": rows})


@bp.get("/api/reports/system-info")
def api_system_info():
    import platform
    import flask as flask_module

    settings = db.get_settings()
    return jsonify(
        {
            "app_version": APP_VERSION,
            "org_name": settings["org_name"],
            "timezone": settings["timezone"],
            "python_version": platform.python_version(),
            "flask_version": flask_module.__version__,
            "server_uptime_s": int(time.monotonic() - SERVER_STARTED),
            "db_path": os.path.basename(db.DB_PATH),
            "db_size_bytes": db.db_file_size(),
            "table_counts": db.table_counts(),
            "poller": {
                "running": poller._scheduler is not None,
                "max_workers": poller.MAX_WORKERS,
            },
            "time_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
    )


# ---------------------------------------------------------
# Pengaturan & pengguna (administrasi)
# ---------------------------------------------------------

def _retention_preview():
    settings = db.get_settings()
    raw = _days_ago_safe(int(settings.get("retention_raw_days", 7)))
    hourly = _days_ago_safe(int(settings.get("retention_hourly_days", 30)))
    events = _days_ago_safe(int(settings.get("retention_events_days", 30)))
    audit = _days_ago_safe(int(settings.get("retention_audit_days", 365)))
    counts = {}
    with db._lock:
        conn = db.get_conn()
        counts["metrics"] = conn.execute("SELECT COUNT(*) AS n FROM device_metrics WHERE ts < ?", (raw,)).fetchone()["n"]
        counts["interfaces"] = conn.execute("SELECT COUNT(*) AS n FROM interface_stats WHERE ts < ?", (raw,)).fetchone()["n"]
        counts["optical"] = conn.execute("SELECT COUNT(*) AS n FROM optical_readings WHERE ts < ?", (raw,)).fetchone()["n"]
        counts["hourly"] = conn.execute("SELECT COUNT(*) AS n FROM device_metrics_hourly WHERE hour_ts < ?", (hourly,)).fetchone()["n"]
        counts["optical_hourly"] = conn.execute(
            "SELECT COUNT(*) AS n FROM optical_hourly WHERE hour_ts < ?",
            ((datetime.now(timezone.utc) - timedelta(days=int(db.get_fiber_settings().get("retention_optical_hourly_days", 90)))).strftime("%Y-%m-%dT%H:00:00Z"),),
        ).fetchone()["n"]
        counts["events"] = conn.execute("SELECT COUNT(*) AS n FROM events WHERE created_at < ?", (events,)).fetchone()["n"]
        counts["audit"] = conn.execute("SELECT COUNT(*) AS n FROM audit_log WHERE ts < ?", (audit,)).fetchone()["n"]
    return counts


def _days_ago_safe(days):
    from datetime import timedelta as _td

    return (datetime.now(timezone.utc) - _td(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


@bp.get("/api/settings")
def api_get_settings():
    settings = db.get_settings()
    settings["retention_preview"] = _retention_preview()
    if g.user["role"] != "admin":
        # Non-admin: hanya informasi yang dibutuhkan untuk menampilkan halaman.
        return jsonify({"settings": settings})
    return jsonify({"settings": settings})


@bp.put("/api/settings")
@roles("admin")
def api_update_settings():
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({"error": "Payload tidak valid."}), 400
    allowed = set(db.SETTING_DEFAULTS)
    clean = {}
    int_keys = {"default_poll_interval", "idle_timeout_minutes", "retention_raw_days",
                "retention_hourly_days", "retention_events_days", "retention_audit_days",
                "retention_syslog_days", "retention_trap_days",
                "ingest_rate_limit_per_sec", "ingest_rate_window_seconds",
                "ingest_max_message_bytes", "ingest_queue_size",
                "ingest_batch_size", "ingest_flush_seconds",
                "notification_storm_threshold",
                "mfa_grace_days",
                "retention_flow_days", "retention_flow_hourly_days", "flow_top_n",
                "metrics_series_limit",
                "mt_bruteforce_threshold", "mt_bruteforce_window_minutes",
                "mt_pool_high_pct", "mt_sessions_drop_pct", "mt_sessions_drop_window_minutes",
                "mt_time_drift_seconds", "mt_stale_user_days", "mt_log_retention_days",
                "mt_backup_retention_versions", "db_backup_keep"}
    int_ranges = {
        "mt_bruteforce_threshold": (2, 1000),
        "mt_bruteforce_window_minutes": (1, 1440),
        "mt_pool_high_pct": (50, 100),
        "mt_sessions_drop_pct": (5, 100),
        "mt_sessions_drop_window_minutes": (1, 120),
        "mt_time_drift_seconds": (10, 86400),
        "mt_stale_user_days": (7, 3650),
        "mt_log_retention_days": (1, 3650),
        "mt_backup_retention_versions": (1, 365),
        "db_backup_keep": (1, 365),
        "ingest_rate_limit_per_sec": (1, 10000),
        "ingest_rate_window_seconds": (1, 120),
        "ingest_max_message_bytes": (512, 65535),
        "ingest_queue_size": (100, 100000),
        "ingest_batch_size": (1, 1000),
        "ingest_flush_seconds": (1, 30),
        "notification_storm_threshold": (2, 100),
        "mfa_grace_days": (0, 365),
        "retention_flow_days": (1, 3650),
        "retention_flow_hourly_days": (1, 3650),
        "flow_top_n": (2, 1000),
        "metrics_series_limit": (10, 100000),
    }
    flag_keys = {"mt_write_actions_enabled", "db_backup_enabled", "metrics_enabled"}
    for key, value in data.items():
        if key not in allowed:
            continue
        if key in flag_keys:
            clean[key] = "1" if bool(value) else "0"
        elif key in int_keys:
            try:
                number = int(value)
            except (TypeError, ValueError):
                return jsonify({"error": f"{key} harus berupa angka."}), 400
            if key == "default_poll_interval" and not (10 <= number <= 86400):
                return jsonify({"error": "Interval default harus antara 10 dan 86400 detik."}), 400
            if key == "idle_timeout_minutes" and not (5 <= number <= 24 * 60):
                return jsonify({"error": "Idle timeout harus antara 5 dan 1440 menit."}), 400
            if key.startswith("retention_") and not (1 <= number <= 3650):
                return jsonify({"error": "Nilai retensi harus antara 1 dan 3650 hari."}), 400
            if key in int_ranges:
                lo, hi = int_ranges[key]
                if not (lo <= number <= hi):
                    return jsonify({"error": f"{key} harus antara {lo} dan {hi}."}), 400
            clean[key] = str(number)
        else:
            text = str(value).strip()
            if key == "timezone" and text not in ("Asia/Jakarta", "UTC"):
                return jsonify({"error": "Zona waktu harus Asia/Jakarta atau UTC."}), 400
            if key == "org_name" and not text:
                return jsonify({"error": "Nama organisasi tidak boleh kosong."}), 400
            if key == "mfa_required_roles":
                try:
                    roles_list = json.loads(text or "[]")
                    if not isinstance(roles_list, list) or any(
                        role not in ("admin", "operator", "viewer") for role in roles_list
                    ):
                        raise ValueError
                except ValueError:
                    return jsonify({"error": "mfa_required_roles harus daftar peran admin/operator/viewer, mis. [\"admin\"]."}), 400
                clean[key] = json.dumps(sorted(set(roles_list)))
                continue
            if key == "metrics_interfaces":
                if text not in ("important", "all", "none"):
                    return jsonify({"error": "metrics_interfaces harus important, all, atau none."}), 400
                clean[key] = text
                continue
            if key == "db_backup_dir":
                if len(text) > 500:
                    return jsonify({"error": "db_backup_dir maksimal 500 karakter."}), 400
                if text:
                    candidate = os.path.abspath(text)
                    if not os.path.isdir(candidate):
                        return jsonify({"error": f"Folder backup tidak ada: {candidate}"}), 400
                    text = candidate
            elif len(text) > 120:
                return jsonify({"error": f"{key} maksimal 120 karakter."}), 400
            clean[key] = text
    new_roles = clean.get("mfa_required_roles")
    if new_roles is not None:
        parsed_roles = json.loads(new_roles)
        started = db.get_setting("mfa_policy_started_at", "")
        if parsed_roles and not started:
            clean["mfa_policy_started_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        elif not parsed_roles:
            clean["mfa_policy_started_at"] = ""
    if not clean:
        return jsonify({"error": "Tidak ada pengaturan yang dikirim."}), 400
    db.set_settings(clean)
    audit("settings.update", "settings", f"Mengubah pengaturan: {', '.join(sorted(clean))}")
    return jsonify({"ok": True, "settings": db.get_settings()})

# ---------------------------------------------------------
# Pengguna (administrasi, khusus admin)
# ---------------------------------------------------------

_USER_ROLES = {"admin", "operator", "viewer"}
_USERNAME_RE = re.compile(r"[a-z0-9._-]{3,32}")
_EMAIL_RE = re.compile(r"^[^@\s]{1,128}@[^@\s]{1,128}\.[^@\s]{2,32}$")
_CHAT_ID_RE = re.compile(r"^-?[0-9]{1,32}$")


def _contact_fields(data, require_something=False):
    email = data.get("email")
    chat_id = data.get("telegram_chat_id")
    contacts = {}
    if email is not None:
        email = str(email).strip()
        if email and not _EMAIL_RE.match(email):
            return None, "Format email tidak valid."
        contacts["email"] = email
    if chat_id is not None:
        chat_id = str(chat_id).strip()
        if chat_id and not _CHAT_ID_RE.match(chat_id):
            return None, "Telegram chat id harus berupa angka (boleh diawali '-')."
        contacts["telegram_chat_id"] = chat_id
    if require_something and not contacts:
        return None, "Tidak ada kontak yang dikirim."
    return contacts, None


@bp.get("/api/users")
@roles("admin")
def api_list_users():
    return jsonify({"users": db.list_users()})


@bp.post("/api/users")
@roles("admin")
def api_create_user():
    data = request.get_json(silent=True) or {}
    username = str(data.get("username") or "").strip().lower()
    role = str(data.get("role") or "viewer").strip().lower()
    password = str(data.get("password") or "")

    if not _USERNAME_RE.fullmatch(username):
        return jsonify({"error": "Nama pengguna 3–32 karakter: huruf kecil, angka, titik, garis bawah, strip."}), 400
    if role not in _USER_ROLES:
        return jsonify({"error": "Peran harus admin, operator, atau viewer."}), 400
    problem = auth.password_problem(password)
    if problem:
        return jsonify({"error": problem}), 400
    if db.user_exists(username):
        return jsonify({"error": "Nama pengguna sudah ada."}), 409

    contacts, contact_error = _contact_fields(data)
    if contact_error:
        return jsonify({"error": contact_error}), 400
    user_id = db.create_user(username, generate_password_hash(password), role=role, must_change_password=1)
    if contacts:
        db.update_user_contacts(user_id, **contacts)
    audit("user.create", f"user:{username}", f"Menambah akun {username} ({auth.ROLE_LABELS.get(role)})")
    return jsonify({"ok": True}), 201


@bp.put("/api/users/<int:user_id>")
@roles("admin")
def api_update_user(user_id):
    target = db.find_user_by_id(user_id)
    if target is None:
        return jsonify({"error": "Pengguna tidak ditemukan."}), 404
    data = request.get_json(silent=True) or {}
    role = data.get("role")
    updates = {}

    if role is not None:
        role = str(role).strip().lower()
        if role not in _USER_ROLES:
            return jsonify({"error": "Peran harus admin, operator, atau viewer."}), 400
        if target["role"] == "admin" and role != "admin":
            admins = [u for u in db.list_users() if u["role"] == "admin"]
            if len(admins) <= 1:
                return jsonify({"error": "Minimal satu akun admin harus tetap ada."}), 400
        updates["role"] = role

    new_password = data.get("password")
    if new_password is not None and str(new_password).strip():
        new_password = str(new_password)
        problem = auth.password_problem(new_password)
        if problem:
            return jsonify({"error": problem}), 400
        updates["password_hash"] = generate_password_hash(new_password)
        updates["must_change_password"] = 1

    contacts, contact_error = _contact_fields(data)
    if contact_error:
        return jsonify({"error": contact_error}), 400

    if not updates and not contacts:
        return jsonify({"error": "Tidak ada perubahan yang dikirim."}), 400

    if updates:
        db.update_user(user_id, **updates)
    if contacts:
        db.update_user_contacts(user_id, **contacts)
    summary = []
    if "role" in updates:
        summary.append(f"peran → {auth.ROLE_LABELS.get(updates['role'], updates['role'])}")
    if "password_hash" in updates:
        summary.append("kata sandi direset (wajib ganti saat login berikutnya)")
    if contacts:
        summary.append("kontak diubah")
    audit("user.update", f"user:{target['username']}", "; ".join(summary))
    return jsonify({"ok": True})


@bp.delete("/api/users/<int:user_id>")
@roles("admin")
def api_delete_user(user_id):
    target = db.find_user_by_id(user_id)
    if target is None:
        return jsonify({"error": "Pengguna tidak ditemukan."}), 404
    if target["id"] == g.user["id"]:
        return jsonify({"error": "Anda tidak dapat menghapus akun Anda sendiri."}), 400
    if target["role"] == "admin":
        admins = [u for u in db.list_users() if u["role"] == "admin"]
        if len(admins) <= 1:
            return jsonify({"error": "Minimal satu akun admin harus tetap ada."}), 400
    db.delete_user(user_id)
    audit("user.delete", f"user:{target['username']}", f"Menghapus akun {target['username']}")
    return jsonify({"ok": True})


