"""Blueprint E1: syslog, trap, aturan kejadian, status penerima, ekspor CSV."""

import csv
import io
import ipaddress
import re

from flask import Blueprint, Response, jsonify, request

import db
from core.csvsafe import csv_cell
from core.guard import audit, roles
from ingest import INGEST
from ingest import rules as ingest_rules

bp = Blueprint("events", __name__)

_SEVERITIES = ("disaster", "high", "average", "warning", "information")
_MATCH_TYPES = ("contains", "startswith", "regex")
_OID_RE = re.compile(r"^[0-9]+(?:\.[0-9]+)*\.?$")


def _int_arg(name, default, low, high):
    try:
        value = int(request.args.get(name, default))
    except (TypeError, ValueError):
        value = default
    return min(high, max(low, value))


def _filters():
    return {
        "device_id": request.args.get("device_id") or None,
        "q": (request.args.get("q") or "").strip()[:200] or None,
        "start": request.args.get("from") or None,
        "end": request.args.get("to") or None,
    }


def _csv_response(filename, header, rows):
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(header)
    for row in rows:
        writer.writerow([csv_cell(value) for value in row])
    return Response(
        "\ufeff" + buffer.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@bp.get("/api/events/summary")
def api_events_summary():
    """Ringkasan jumlah (aman untuk viewer): tanpa pesan/alamat. """
    with db._lock:
        conn = db.get_conn()
        syslog_total = conn.execute("SELECT COUNT(*) AS n FROM syslog_events").fetchone()["n"]
        trap_total = conn.execute("SELECT COUNT(*) AS n FROM snmp_traps").fetchone()["n"]
        by_severity = conn.execute(
            "SELECT severity, COUNT(*) AS n FROM syslog_events GROUP BY severity"
        ).fetchall()
        last = conn.execute(
            "SELECT MAX(ts) AS ts FROM (SELECT ts FROM syslog_events UNION ALL SELECT ts FROM snmp_traps)"
        ).fetchone()["ts"]
    return jsonify({
        "syslog_total": syslog_total,
        "trap_total": trap_total,
        "by_severity": {str(row["severity"]): row["n"] for row in by_severity},
        "last_event_at": last,
    })


@bp.get("/api/events/syslog")
@roles("admin", "operator")
def api_syslog():
    filters = _filters()
    severity = request.args.get("severity")
    if severity in ("", None):
        severity = None
    try:
        severity = int(severity) if severity is not None else None
    except ValueError:
        return jsonify({"error": "Severity harus angka 0-7."}), 400
    if severity is not None and not (0 <= severity <= 7):
        return jsonify({"error": "Severity harus angka 0-7."}), 400
    result = db.list_syslog_events(
        **filters, severity=severity,
        page=_int_arg("page", 1, 1, 100000),
        per_page=_int_arg("per_page", 50, 1, 500),
    )
    return jsonify(result)


@bp.get("/api/events/syslog.csv")
@roles("admin", "operator")
def api_syslog_csv():
    filters = _filters()
    result = db.list_syslog_events(**filters, limit=10000)
    return _csv_response(
        "syslog.csv",
        ["waktu", "perangkat", "ip", "facility", "severity", "tag", "pesan"],
        [
            (row["ts"], row.get("device_name"), row.get("device_ip"), row.get("facility"),
             row.get("severity"), row.get("tag"), row.get("message"))
            for row in result["entries"]
        ],
    )


@bp.get("/api/events/traps")
@roles("admin", "operator")
def api_traps():
    filters = _filters()
    result = db.list_snmp_traps(
        **filters,
        page=_int_arg("page", 1, 1, 100000),
        per_page=_int_arg("per_page", 50, 1, 500),
    )
    return jsonify(result)


@bp.get("/api/events/traps.csv")
@roles("admin", "operator")
def api_traps_csv():
    filters = _filters()
    result = db.list_snmp_traps(**filters, limit=10000)
    return _csv_response(
        "snmp-traps.csv",
        ["waktu", "perangkat", "ip", "trap_oid", "nama", "varbind"],
        [
            (row["ts"], row.get("device_name"), row.get("device_ip"), row.get("trap_oid"),
             row.get("trap_name"), row.get("varbinds"))
            for row in result["entries"]
        ],
    )


@bp.get("/api/events/status")
@roles("admin", "operator")
def api_events_status():
    return jsonify({"receivers": INGEST.status(), "counts": db.ingest_table_counts()})


@bp.get("/api/events/rules")
@roles("admin", "operator")
def api_list_rules():
    return jsonify({"rules": db.list_ingest_rules()})


def _validate_rule(data, rule_id=None):
    name = str(data.get("name") or "").strip()
    if not (1 <= len(name) <= 80):
        return None, "Nama aturan wajib 1-80 karakter."
    kind = str(data.get("kind") or "syslog").lower()
    if kind not in ("syslog", "trap"):
        return None, "Jenis aturan harus syslog atau trap."
    match_type = str(data.get("match_type") or "contains").lower()
    if match_type not in _MATCH_TYPES:
        return None, "Tipe pencocokan tidak valid."
    pattern = str(data.get("pattern") or "").strip()
    if len(pattern) > ingest_rules.MAX_PATTERN_LENGTH:
        return None, f"Pola maksimal {ingest_rules.MAX_PATTERN_LENGTH} karakter."
    if kind == "syslog" and not pattern:
        return None, "Aturan syslog wajib memiliki pola pesan."
    if kind == "trap":
        pattern = ""
    if match_type == "regex" and pattern:
        try:
            ingest_rules.validate_regex_pattern(pattern)
        except ingest_rules.PatternError as exc:
            return None, str(exc)
    oid_prefix = str(data.get("oid_prefix") or "").strip()
    if kind == "trap":
        if not oid_prefix or not _OID_RE.match(oid_prefix):
            return None, "Awalan OID trap wajib berupa deretan angka bertitik."
        if len(oid_prefix) > 128:
            return None, "Awalan OID maksimal 128 karakter."
    else:
        oid_prefix = ""
    min_severity = str(data.get("min_severity") or "information").lower()
    problem_severity = str(data.get("problem_severity") or "warning").lower()
    if min_severity not in _SEVERITIES or problem_severity not in _SEVERITIES:
        return None, "Severity tidak valid."
    recovery = str(data.get("recovery_pattern") or "").strip()
    if len(recovery) > ingest_rules.MAX_PATTERN_LENGTH:
        return None, "Pola pemulihan terlalu panjang."
    if recovery and match_type == "regex":
        try:
            ingest_rules.validate_regex_pattern(recovery)
        except ingest_rules.PatternError as exc:
            return None, str(exc)
    try:
        cooldown = min(86400, max(0, int(data.get("cooldown_seconds", 60))))
        auto_resolve = min(10080, max(0, int(data.get("auto_resolve_minutes", 30))))
        device_id = int(data["device_id"]) if data.get("device_id") else None
        group_id = int(data["group_id"]) if data.get("group_id") else None
        resolves_rule_id = int(data["resolves_rule_id"]) if data.get("resolves_rule_id") else None
    except (TypeError, ValueError):
        return None, "Nilai angka aturan tidak valid."
    if resolves_rule_id and rule_id and resolves_rule_id == int(rule_id):
        return None, "Aturan tidak boleh menyelesaikan dirinya sendiri."
    if resolves_rule_id and db.get_ingest_rule(resolves_rule_id) is None:
        return None, "Aturan tujuan pemulihan tidak ditemukan."
    if device_id and db.get_device(device_id) is None:
        return None, "Perangkat tidak ditemukan."
    if group_id and db.get_group(group_id) is None:
        return None, "Grup tidak ditemukan."
    return {
        "name": name, "kind": kind, "match_type": match_type, "pattern": pattern,
        "oid_prefix": oid_prefix, "min_severity": min_severity, "device_id": device_id,
        "group_id": group_id, "problem_kind": "trap_rule" if kind == "trap" else "syslog_rule",
        "problem_severity": problem_severity, "cooldown_seconds": cooldown,
        "auto_resolve_minutes": auto_resolve, "recovery_pattern": recovery,
        "resolves_rule_id": resolves_rule_id,
        "enabled": 1 if data.get("enabled", True) else 0,
    }, None


@bp.post("/api/events/rules")
@roles("admin", "operator")
def api_create_rule():
    data = request.get_json(silent=True) or {}
    clean, error = _validate_rule(data)
    if error:
        return jsonify({"error": error}), 400
    if db.ingest_rule_name_exists(clean["name"]):
        return jsonify({"error": "Nama aturan sudah dipakai."}), 409
    rule_id = db.create_ingest_rule(clean)
    audit("events.rule_create", f"ingest_rule:{rule_id}", f"Membuat aturan kejadian {clean['name']}")
    return jsonify({"ok": True, "id": rule_id}), 201


@bp.put("/api/events/rules/<int:rule_id>")
@roles("admin", "operator")
def api_update_rule(rule_id):
    if db.get_ingest_rule(rule_id) is None:
        return jsonify({"error": "Aturan tidak ditemukan."}), 404
    data = request.get_json(silent=True) or {}
    clean, error = _validate_rule(data, rule_id=rule_id)
    if error:
        return jsonify({"error": error}), 400
    if db.ingest_rule_name_exists(clean["name"], exclude_id=rule_id):
        return jsonify({"error": "Nama aturan sudah dipakai."}), 409
    db.update_ingest_rule(rule_id, clean)
    audit("events.rule_update", f"ingest_rule:{rule_id}", f"Mengubah aturan kejadian {clean['name']}")
    return jsonify({"ok": True})


@bp.delete("/api/events/rules/<int:rule_id>")
@roles("admin", "operator")
def api_delete_rule(rule_id):
    rule = db.get_ingest_rule(rule_id)
    if rule is None:
        return jsonify({"error": "Aturan tidak ditemukan."}), 404
    db.delete_ingest_rule(rule_id)
    audit("events.rule_delete", f"ingest_rule:{rule_id}", f"Menghapus aturan kejadian {rule['name']}")
    return jsonify({"ok": True})


@bp.get("/api/events/receiver")
@roles("admin")
def api_get_receiver():
    receivers = db.list_ingest_receivers(include_secret=False)
    return jsonify({"receivers": receivers})


@bp.put("/api/events/receiver")
@roles("admin")
def api_update_receiver():
    data = request.get_json(silent=True) or {}
    kind = str(data.get("kind") or "").strip()
    if kind not in ("syslog_udp", "syslog_tcp", "snmp_trap", "netflow_udp", "ipfix_udp"):
        return jsonify({"error": "Jenis penerima tidak valid."}), 400
    try:
        port = int(data.get("port"))
    except (TypeError, ValueError):
        return jsonify({"error": "Port harus berupa angka."}), 400
    if not (1024 <= port <= 65535):
        return jsonify({"error": "Port harus 1024-65535 (tanpa hak istimewa)."}), 400
    bind = str(data.get("bind_address") or "0.0.0.0").strip()
    try:
        ipaddress.ip_address(bind)
    except ValueError:
        return jsonify({"error": "Bind address harus alamat IP."}), 400
    config = {}
    if kind == "snmp_trap":
        communities = data.get("communities")
        if communities is not None:
            if not isinstance(communities, list):
                return jsonify({"error": "communities harus daftar."}), 400
            cleaned = []
            for item in communities[:20]:
                text = str(item or "").strip()
                if text and len(text) <= 100 and text not in cleaned:
                    cleaned.append(text)
            config["communities"] = cleaned
        if "allow_v1" in data:
            config["allow_v1"] = bool(data.get("allow_v1"))
    db.update_ingest_receiver(
        kind, enabled=bool(data.get("enabled", False)), port=port,
        bind_address=bind, config=config or None,
    )
    audit("events.receiver_update", f"ingest_receiver:{kind}",
          f"Memperbarui penerima {kind} (port {port}, {'aktif' if data.get('enabled') else 'mati'})")
    try:
        status = INGEST.reload()
    except Exception as exc:
        return jsonify({"ok": True, "warning": f"Konfigurasi disimpan, tetapi penerima gagal dimuat: {exc}"}), 200
    try:
        from flow import FLOW

        flow_status = FLOW.reload()
        status = {**status, **flow_status}
    except Exception as exc:
        return jsonify({"ok": True,
                        "warning": f"Konfigurasi disimpan, tetapi penerima flow gagal dimuat: {exc}"}), 200
    return jsonify({"ok": True, "receivers": status})
