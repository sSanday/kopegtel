"""Blueprint Alerting: kanal, template, aturan aksi, action_log."""
from urllib.parse import urlsplit

from flask import Blueprint, jsonify, request

import alerts
import db
from core.guard import audit, roles
from core.validation import _parse_optional_id

bp = Blueprint("alerts", __name__)

_CHANNEL_TYPES = {"telegram", "smtp", "webhook"}


def _webhook_config_from_raw(raw, existing_id=None):
    """Validasi konfigurasi kanal webhook. Return (config, error)."""
    import webhook

    old = (db.channel_config(existing_id) or {}) if existing_id else {}

    url = str(raw.get("url") or "").strip()
    if not url and existing_id:
        url = str(old.get("url") or "").strip()  # kosong = pertahankan URL tersimpan
    if len(url) > 2048:
        return None, "URL webhook maksimal 2048 karakter."
    if not url:
        return None, "URL webhook wajib diisi."
    preset = str(raw.get("preset", old.get("preset", "generic")) or "generic").strip().lower()
    if preset not in ("slack", "discord", "generic"):
        return None, "Preset webhook harus slack, discord, atau generic."
    allow_http = bool(raw.get("allow_http", old.get("allow_http", False)))
    allow_private = bool(raw.get("allow_private", old.get("allow_private", False)))
    secret = str(raw.get("secret") or "").strip()
    if not secret:
        secret = str(old.get("secret") or "").strip()  # kosong = pertahankan secret tersimpan
    if len(secret) > 200:
        return None, "Secret webhook maksimal 200 karakter."
    if not secret and not existing_id:
        return None, "Secret webhook wajib diisi (dipakai untuk tandatangan HMAC)."
    headers = raw.get("headers") if isinstance(raw.get("headers"), dict) else {}
    headers = webhook._sanitize_headers(headers)
    try:
        max_response = int(
            raw.get("max_response_bytes") or old.get("max_response_bytes") or webhook.MAX_RESPONSE_BYTES
        )
    except (TypeError, ValueError):
        return None, "max_response_bytes harus berupa angka."
    max_response = min(webhook.MAX_RESPONSE_BYTES, max(256, max_response))
    allow_http_flag = allow_http or (urlsplit(url).scheme or "").lower() == "http"
    if allow_http_flag and not allow_http:
        return None, "http hanya boleh bila izin eksplisit diaktifkan."
    # URL baru wajib divalidasi; URL lama tidak perlu resolve DNS ulang kecuali
    # kebijakan allow_http/allow_private berubah.
    new_url = str(raw.get("url") or "").strip()
    policy_changed = (
        allow_http != bool(old.get("allow_http", False))
        or allow_private != bool(old.get("allow_private", False))
    )
    if new_url or not existing_id or policy_changed:
        try:
            webhook.validate_webhook_target(url, allow_private=allow_private, allow_http=allow_http)
        except ValueError as exc:
            return None, f"URL ditolak: {exc}"
    config = {
        "url": url, "preset": preset, "allow_http": allow_http,
        "allow_private": allow_private,
        "max_response_bytes": max_response,
    }
    if headers:
        config["headers"] = headers
    if secret:
        config["secret"] = secret
    return config, None


def _channel_config_from_payload(data, existing_id=None):
    channel_type = str(data.get("type") or "").strip().lower()
    if channel_type not in _CHANNEL_TYPES:
        return None, None, "Tipe kanal harus telegram atau smtp."
    name = str(data.get("name") or "").strip()
    if not name or len(name) > 80:
        return None, None, "Nama kanal wajib diisi (maks 80 karakter)."
    enabled = bool(data.get("enabled", True))
    raw = data.get("config") or {}
    if not isinstance(raw, dict):
        return None, None, "Konfigurasi kanal tidak valid."

    old = (db.channel_config(existing_id) or {}) if existing_id else {}

    if channel_type == "telegram":
        bot_token = str(raw.get("bot_token") or "").strip()
        chat_id = str(raw.get("chat_id") or "").strip()
        if not bot_token and existing_id:
            bot_token = str(old.get("bot_token") or "").strip()
        if not chat_id and existing_id:
            chat_id = str(old.get("chat_id") or "").strip()
        config = {"bot_token": bot_token, "chat_id": chat_id}
        if not config["chat_id"]:
            return None, None, "Chat ID Telegram wajib diisi."
        if not existing_id and not config["bot_token"]:
            return None, None, "Bot token Telegram wajib diisi."
    elif channel_type == "webhook":
        config, error = _webhook_config_from_raw(raw, existing_id=existing_id)
        if error:
            return None, None, error
    else:  # smtp
        try:
            port = int(raw.get("port", old.get("port", 587)) or 587)
        except (TypeError, ValueError):
            return None, None, "Port SMTP harus berupa angka."
        host = str(raw.get("host") or "").strip()
        username = str(raw.get("username") or "").strip()
        password = str(raw.get("password") or "").strip()
        if not host and existing_id:
            host = str(old.get("host") or "").strip()
        if not username and existing_id:
            username = str(old.get("username") or "").strip()
        if not password and existing_id:
            password = str(old.get("password") or "").strip()
        config = {
            "host": host,
            "port": port,
            "use_tls": bool(raw.get("use_tls", old.get("use_tls", True))),
            "username": username,
            "password": password,
            "from_addr": str(raw.get("from_addr") or "").strip(),
            "to_addr": str(raw.get("to_addr") or "").strip(),
        }
        if not config["host"] or not config["from_addr"] or not config["to_addr"]:
            return None, None, "Host, from_addr, dan to_addr SMTP wajib diisi."
    return {"name": name, "type": channel_type, "config": config, "enabled": enabled}, channel_type, None


@bp.get("/api/channels")
@roles("admin", "operator")
def api_list_channels():
    return jsonify({"channels": db.list_channels()})


@bp.post("/api/channels")
@roles("admin", "operator")
def api_create_channel():
    parsed, channel_type, error = _channel_config_from_payload(request.get_json(silent=True) or {})
    if error:
        return jsonify({"error": error}), 400
    if db.channel_name_exists(parsed["name"]):
        return jsonify({"error": "Nama kanal sudah ada."}), 409
    channel_id = db.create_channel(parsed["name"], channel_type, parsed["config"], parsed["enabled"])
    audit("channel.create", f"channel:{channel_id}", f"Menambah kanal {parsed['name']}")
    return jsonify({"ok": True, "id": channel_id}), 201


@bp.put("/api/channels/<int:channel_id>")
@roles("admin", "operator")
def api_update_channel(channel_id):
    if db.get_channel(channel_id) is None:
        return jsonify({"error": "Kanal tidak ditemukan."}), 404
    parsed, channel_type, error = _channel_config_from_payload(request.get_json(silent=True) or {}, existing_id=channel_id)
    if error:
        return jsonify({"error": error}), 400
    if db.channel_name_exists(parsed["name"], exclude_id=channel_id):
        return jsonify({"error": "Nama kanal sudah ada."}), 409
    db.update_channel(channel_id, parsed["name"], channel_type, parsed["config"], parsed["enabled"])
    audit("channel.update", f"channel:{channel_id}", f"Mengubah kanal {parsed['name']}")
    return jsonify({"ok": True})


@bp.delete("/api/channels/<int:channel_id>")
@roles("admin", "operator")
def api_delete_channel(channel_id):
    if not db.delete_channel(channel_id):
        return jsonify({"error": "Kanal tidak ditemukan."}), 404
    audit("channel.delete", f"channel:{channel_id}", "Menghapus kanal")
    return jsonify({"ok": True})


@bp.post("/api/channels/<int:channel_id>/test")
@roles("admin", "operator")
def api_test_channel(channel_id):
    ok, detail = alerts.send_test(channel_id)
    channel = db.get_channel(channel_id)
    name = channel["name"] if channel else str(channel_id)
    audit("channel.test", f"channel:{channel_id}", f"Uji kirim kanal {name}: {'berhasil' if ok else 'gagal'}")
    return jsonify({"ok": ok, "detail": detail})


@bp.get("/api/templates")
def api_list_templates():
    return jsonify(
        {
            "templates": db.list_templates(),
            "placeholders": ["{perangkat}", "{ip}", "{severity}", "{pesan}", "{waktu}"],
        }
    )


def _validate_template(data):
    if not isinstance(data, dict):
        return {}, "Payload tidak valid."
    name = str(data.get("name") or "").strip()
    body = str(data.get("body") or "").strip()
    if not name or len(name) > 80:
        return {}, "Nama template wajib diisi (maks 80 karakter)."
    if not body or len(body) > 2000:
        return {}, "Isi template wajib diisi (maks 2000 karakter)."
    return {"name": name, "body": body}, None


@bp.post("/api/templates")
@roles("admin", "operator")
def api_create_template():
    clean, error = _validate_template(request.get_json(silent=True) or {})
    if error:
        return jsonify({"error": error}), 400
    if db.template_name_exists(clean["name"]):
        return jsonify({"error": "Nama template sudah ada."}), 409
    template_id = db.create_template(clean["name"], clean["body"])
    audit("template.create", f"template:{template_id}", f"Menambah template {clean['name']}")
    return jsonify({"ok": True, "id": template_id}), 201


@bp.put("/api/templates/<int:template_id>")
@roles("admin", "operator")
def api_update_template(template_id):
    if db.get_template(template_id) is None:
        return jsonify({"error": "Template tidak ditemukan."}), 404
    clean, error = _validate_template(request.get_json(silent=True) or {})
    if error:
        return jsonify({"error": error}), 400
    if db.template_name_exists(clean["name"], exclude_id=template_id):
        return jsonify({"error": "Nama template sudah ada."}), 409
    db.update_template(template_id, clean["name"], clean["body"])
    audit("template.update", f"template:{template_id}", f"Mengubah template {clean['name']}")
    return jsonify({"ok": True})


@bp.delete("/api/templates/<int:template_id>")
@roles("admin", "operator")
def api_delete_template(template_id):
    if not db.delete_template(template_id):
        return jsonify({"error": "Template tidak ditemukan."}), 404
    audit("template.delete", f"template:{template_id}", "Menghapus template")
    return jsonify({"ok": True})


_SEVERITIES = {"information", "warning", "average", "high", "disaster"}


def _validate_rule(data):
    if not isinstance(data, dict):
        return {}, "Payload tidak valid."
    name = str(data.get("name") or "").strip()
    if not name or len(name) > 80:
        return {}, "Nama aturan wajib diisi (maks 80 karakter)."
    min_severity = str(data.get("min_severity") or "warning").strip().lower()
    if min_severity not in _SEVERITIES:
        return {}, "Severity minimum tidak valid."
    try:
        channel_id = int(data.get("channel_id"))
        template_id = int(data.get("template_id"))
    except (TypeError, ValueError):
        return {}, "Kanal dan template wajib dipilih."
    if db.get_channel(channel_id) is None:
        return {}, "Kanal tidak ditemukan."
    if db.get_template(template_id) is None:
        return {}, "Template tidak ditemukan."
    group_id, error = _parse_optional_id(data.get("group_id"), "Grup")
    if error:
        return {}, error
    if group_id and db.get_group(group_id) is None:
        return {}, "Grup tidak ditemukan."
    device_id, error = _parse_optional_id(data.get("device_id"), "Perangkat")
    if error:
        return {}, error
    if device_id and db.get_device(device_id) is None:
        return {}, "Perangkat tidak ditemukan."
    policy_id, error = _parse_optional_id(data.get("escalation_policy_id"), "Kebijakan eskalasi")
    if error:
        return {}, error
    if policy_id and db.get_policy(policy_id) is None:
        return {}, "Kebijakan eskalasi tidak ditemukan."
    return {
        "name": name,
        "min_severity": min_severity,
        "group_id": group_id,
        "device_id": device_id,
        "channel_id": channel_id,
        "template_id": template_id,
        "escalation_policy_id": policy_id,
        "enabled": bool(data.get("enabled", True)),
    }, None


@bp.get("/api/rules")
def api_list_rules():
    return jsonify({"rules": db.list_rules()})


@bp.post("/api/rules")
@roles("admin", "operator")
def api_create_rule():
    clean, error = _validate_rule(request.get_json(silent=True) or {})
    if error:
        return jsonify({"error": error}), 400
    if db.rule_name_exists(clean["name"]):
        return jsonify({"error": "Nama aturan sudah ada."}), 409
    rule_id = db.create_rule(clean)
    audit("rule.create", f"rule:{rule_id}", f"Menambah aturan aksi {clean['name']}")
    return jsonify({"ok": True, "id": rule_id}), 201


@bp.put("/api/rules/<int:rule_id>")
@roles("admin", "operator")
def api_update_rule(rule_id):
    if db.get_rule(rule_id) is None:
        return jsonify({"error": "Aturan tidak ditemukan."}), 404
    clean, error = _validate_rule(request.get_json(silent=True) or {})
    if error:
        return jsonify({"error": error}), 400
    if db.rule_name_exists(clean["name"], exclude_id=rule_id):
        return jsonify({"error": "Nama aturan sudah ada."}), 409
    db.update_rule(rule_id, clean)
    audit("rule.update", f"rule:{rule_id}", f"Mengubah aturan aksi {clean['name']}")
    return jsonify({"ok": True})


@bp.delete("/api/rules/<int:rule_id>")
@roles("admin", "operator")
def api_delete_rule(rule_id):
    if not db.delete_rule(rule_id):
        return jsonify({"error": "Aturan tidak ditemukan."}), 404
    audit("rule.delete", f"rule:{rule_id}", "Menghapus aturan aksi")
    return jsonify({"ok": True})


@bp.get("/api/action-log")
def api_action_log():
    try:
        limit = min(500, max(1, int(request.args.get("limit", "100"))))
        offset = max(0, int(request.args.get("offset", "0")))
    except ValueError:
        limit, offset = 100, 0
    status = (request.args.get("status") or "").strip().lower() or None
    if status and status not in ("sent", "failed", "skipped"):
        return jsonify({"error": "Status tidak valid."}), 400
    return jsonify(db.list_action_log(limit=limit, offset=offset, status=status))
