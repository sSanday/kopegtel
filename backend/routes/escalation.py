"""Blueprint E2 — kebijakan eskalasi (CRUD + validasi langkah)."""
import re

from flask import Blueprint, jsonify, request

import db
import netguard
from core.guard import audit, roles
from core.validation import parse_finite_int

bp = Blueprint("escalation", __name__)

_HHMM_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
_DAYS_RE = re.compile(r"^[1-7](,[1-7])*$")


def _validate_policy(data, policy_id=None):
    name = str(data.get("name") or "").strip()
    if not (1 <= len(name) <= 80):
        return None, "Nama kebijakan wajib 1-80 karakter."
    if policy_id is None and db.policy_name_exists(name):
        return None, "Nama kebijakan sudah dipakai."
    if policy_id is not None and db.policy_name_exists(name, exclude_id=policy_id):
        return None, "Nama kebijakan sudah dipakai."
    description = str(data.get("description") or "").strip()[:500]
    business_enabled = netguard.as_bool(data.get("business_hours_enabled"))
    start = str(data.get("business_start") or "08:00").strip()
    end = str(data.get("business_end") or "17:00").strip()
    days = str(data.get("business_days") or "1,2,3,4,5").strip()
    if not _HHMM_RE.match(start):
        return None, "Jam mulai harus format HH:MM (00:00-23:59)."
    if not _HHMM_RE.match(end):
        return None, "Jam selesai harus format HH:MM (00:00-23:59)."
    if not _DAYS_RE.match(days):
        return None, "business_days harus daftar angka 1-7 (1=Senin), mis. '1,2,3,4,5'."
    repeat_value = parse_finite_int(data.get("repeat_minutes", 0))
    if repeat_value is None:
        return None, "repeat_minutes harus berupa angka."
    repeat = min(10080, max(0, repeat_value))

    raw_steps = data.get("steps") or []
    if not isinstance(raw_steps, list) or not (1 <= len(raw_steps) <= 20):
        return None, "Kebijakan wajib memiliki 1-20 langkah."
    steps = []
    for index, raw in enumerate(raw_steps, start=1):
        if not isinstance(raw, dict):
            return None, f"Langkah {index} tidak valid."
        target_type = str(raw.get("target_type") or "").strip().lower()
        if target_type not in ("channel", "user"):
            return None, f"Langkah {index}: target_type harus 'channel' atau 'user'."
        target_id = parse_finite_int(raw.get("target_id"))
        delay_value = parse_finite_int(raw.get("delay_minutes", 0))
        if target_id is None or delay_value is None:
            return None, f"Langkah {index}: delay_minutes/target_id harus berupa angka."
        delay = min(10080, max(0, delay_value))
        if target_type == "channel":
            if db.get_channel(target_id) is None:
                return None, f"Langkah {index}: kanal #{target_id} tidak ditemukan."
        else:
            if db.find_user_by_id(target_id) is None:
                return None, f"Langkah {index}: pengguna #{target_id} tidak ditemukan."
        steps.append({
            "delay_minutes": delay,
            "target_type": target_type,
            "target_id": target_id,
            "business_hours_only": netguard.as_bool(raw.get("business_hours_only")),
        })

    return {
        "name": name, "description": description, "enabled": netguard.as_bool(data.get("enabled", True)),
        "business_hours_enabled": business_enabled,
        "business_start": start, "business_end": end, "business_days": days,
        "repeat_minutes": repeat, "steps": steps,
    }, None


@bp.get("/api/escalation-targets")
@roles("admin", "operator")
def api_targets():
    """Daftar target eskalasi (id + label) tanpa data sensitif."""
    users = [
        {"id": u["id"], "username": u["username"], "role": u["role"]}
        for u in db.list_users()
    ]
    channels = [
        {"id": c["id"], "name": c["name"], "type": c["type"], "enabled": c["enabled"]}
        for c in db.list_channels()
    ]
    return jsonify({"users": users, "channels": channels})


@bp.get("/api/escalation-policies")
@roles("admin", "operator")
def api_list_policies():
    return jsonify({"policies": db.list_policies()})


@bp.post("/api/escalation-policies")
@roles("admin", "operator")
def api_create_policy():
    data = request.get_json(silent=True) or {}
    clean, error = _validate_policy(data)
    if error:
        return jsonify({"error": error}), 400
    policy_id = db.create_policy(clean)
    audit("escalation.create", f"escalation_policy:{policy_id}",
          f"Membuat kebijakan eskalasi {clean['name']} ({len(clean['steps'])} langkah)")
    return jsonify({"ok": True, "id": policy_id}), 201


@bp.put("/api/escalation-policies/<int:policy_id>")
@roles("admin", "operator")
def api_update_policy(policy_id):
    if db.get_policy(policy_id) is None:
        return jsonify({"error": "Kebijakan tidak ditemukan."}), 404
    data = request.get_json(silent=True) or {}
    clean, error = _validate_policy(data, policy_id=policy_id)
    if error:
        return jsonify({"error": error}), 400
    db.update_policy(policy_id, clean)
    audit("escalation.update", f"escalation_policy:{policy_id}",
          f"Mengubah kebijakan eskalasi {clean['name']}")
    return jsonify({"ok": True})


@bp.delete("/api/escalation-policies/<int:policy_id>")
@roles("admin", "operator")
def api_delete_policy(policy_id):
    policy = db.get_policy(policy_id)
    if policy is None:
        return jsonify({"error": "Kebijakan tidak ditemukan."}), 404
    db.delete_policy(policy_id)
    audit("escalation.delete", f"escalation_policy:{policy_id}",
          f"Menghapus kebijakan eskalasi {policy['name']}")
    return jsonify({"ok": True})
