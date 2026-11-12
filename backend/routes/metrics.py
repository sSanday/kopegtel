"""Blueprint E4 — /metrics (Prometheus, Bearer token) + manajemen token API."""
import json

from flask import Blueprint, Response, g, jsonify, request

import db
from core.guard import audit, client_ip, roles
from ingest.service import RateLimiter
import prometheus

bp = Blueprint("metrics", __name__)

_RATE = RateLimiter(per_second=30, window_seconds=60)


def _metrics_enabled():
    return str(db.get_setting("metrics_enabled", "0")).lower() in ("1", "true", "yes", "on")


@bp.get("/metrics")
def metrics_endpoint():
    # Default MATI sampai admin mengaktifkan.
    if not _metrics_enabled():
        return jsonify({"error": "Metrik Prometheus tidak diaktifkan."}), 404

    ip = client_ip()
    if not _RATE.allow(f"metrics:{ip}"):
        return jsonify({"error": "Terlalu banyak scrape. Coba lagi nanti."}), 429

    auth = (request.headers.get("Authorization") or "").strip()
    scheme, _, token = auth.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return jsonify({"error": "Gunakan Authorization: Bearer <token>."}), 401

    record = db.verify_api_token(token.strip(), ip=ip)
    if record is None:
        return jsonify({"error": "Token tidak valid, kedaluwarsa, dicabut, atau IP tidak diizinkan."}), 401
    if "metrics" not in (record.get("scopes") or []):
        return jsonify({"error": "Cakupan token tidak mencakup metrics."}), 403

    payload, truncated = prometheus.render_metrics_text()
    return Response(
        payload,
        mimetype="text/plain; version=0.0.4; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )


@bp.get("/api/metrics/tokens")
@roles("admin")
def api_list_tokens():
    return jsonify({"tokens": db.list_api_tokens()})


@bp.post("/api/metrics/tokens")
@roles("admin")
def api_create_token():
    data = request.get_json(silent=True) or {}
    allowed = data.get("allowed_ips")
    if isinstance(allowed, str):
        allowed = [part.strip() for part in allowed.split(",") if part.strip()]
    if allowed is not None and not isinstance(allowed, list):
        return jsonify({"error": "allowed_ips harus daftar/string IP."}), 400
    if allowed:
        import ipaddress

        cleaned = []
        for item in allowed:
            text = str(item).strip()
            try:
                ipaddress.ip_address(text)
            except ValueError:
                return jsonify({"error": f"IP tidak valid: {text}"}), 400
            cleaned.append(text)
        allowed = cleaned[:10]
    token, record = db.create_api_token(
        data.get("name"),
        scopes=["metrics"],
        expires_in_days=data.get("expires_in_days"),
        allowed_ips=allowed,
        created_by=g.user["username"] if getattr(g, "user", None) else "-",
    )
    if record is None:
        return jsonify({"error": token}), 400
    audit("metrics.token_create", f"api_token:{record['id']}",
          f"Membuat token API {record['name']} (cakupan metrics)")
    return jsonify({
        "ok": True,
        "token": token,  # ditampilkan SEKALI
        "token_prefix": record["token_prefix"],
        "record": record,
    }), 201


@bp.delete("/api/metrics/tokens/<int:token_id>")
@roles("admin")
def api_revoke_token(token_id):
    token = db.get_api_token(token_id)
    if token is None:
        return jsonify({"error": "Token tidak ditemukan."}), 404
    if db.revoke_api_token(token_id):
        audit("metrics.token_revoke", f"api_token:{token_id}",
              f"Mencabut token API {token['name']}")
        return jsonify({"ok": True})
    return jsonify({"ok": True, "already_revoked": True})
