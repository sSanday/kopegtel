"""Blueprint E3: Traffic Flow — ringkasan, top-N, percakapan, status eksporter."""

import csv
import io

from flask import Blueprint, Response, jsonify, request

import db
from core.csvsafe import csv_cell
from core.guard import roles
from core.validation import SQLITE_INT64_MAX, parse_query_int
from flow import FLOW

bp = Blueprint("flow", __name__)

_RANGES = {"1h", "24h", "7d"}


def _range_key():
    key = (request.args.get("range") or "1h").lower()
    return key if key in _RANGES else "1h"


def _exporter_id():
    raw = request.args.get("exporter_id") or request.args.get("exporter")
    if not raw:
        return None
    return parse_query_int(raw, None, min_value=0, max_value=SQLITE_INT64_MAX)


def _limit(default=10, cap=1000):
    return parse_query_int(
        request.args.get("limit"), default, min_value=1, max_value=cap
    )


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


def _enrich_hosts(rows):
    mapping = db.flow.lease_host_map()
    for row in rows:
        key = row["key"]
        host = mapping.get(key)
        if host:
            row["host"] = host
        for part in str(key).split(">", 1):
            if part in mapping and "host" not in row:
                row["host"] = mapping[part]
                break
    return rows


def _split_interface(key):
    iface, sep, direction = str(key or "").rpartition(":")
    if direction in ("rx", "tx") and iface:
        return iface, direction
    return str(key), ""


@bp.get("/api/flow/summary")
@roles("admin", "operator", "viewer")
def api_flow_summary():
    data = db.flow.flow_summary(_range_key(), _exporter_id())
    exporters = data["exporters"]
    period = 300 if data.get("grain") == "raw" else 3600
    if request.args.get("with_hosts") in ("1", "true", "yes") or _exporter_id():
        for exporter in exporters:
            exporter["series_bps"] = [
                {"ts": point["ts"], "bps": int(point["bytes"] * 8 / period)}
                for point in exporter.get("series", [])
            ]
    if _is_viewer():
        exporters = [
            {
                "device_id": exporter["device_id"],
                "name": exporter["name"],
                "bytes": exporter["bytes"],
                "packets": exporter["packets"],
                "flows": exporter["flows"],
            }
            for exporter in exporters
        ]
        return jsonify({
            "range": data["range"], "grain": data["grain"],
            "exporters": exporters,
            "total_bytes": data["total_bytes"],
            "total_packets": data["total_packets"],
            "total_flows": data["total_flows"],
            "viewer": True,
        })
    return jsonify(data)


def _is_viewer():
    from flask import g

    user = getattr(g, "user", None)
    if user is None:
        return False
    try:
        role = dict(user).get("role")
    except Exception:
        role = user["role"]
    return role == "viewer"


@bp.get("/api/flow/talkers")
@roles("admin", "operator")
def api_flow_talkers():
    kind = (request.args.get("kind") or "src").lower()
    if kind not in ("src", "dst"):
        return jsonify({"error": "kind harus src atau dst."}), 400
    rows = db.flow.flow_top(kind, _range_key(), _exporter_id(), _limit(10))
    return jsonify({"kind": kind, "range": _range_key(), "entries": _enrich_hosts(rows)})


@bp.get("/api/flow/apps")
@roles("admin", "operator")
def api_flow_apps():
    apps = db.flow.flow_top("app", _range_key(), _exporter_id(), _limit(10))
    protos = db.flow.flow_top("proto", _range_key(), _exporter_id(), _limit(10))
    return jsonify({"range": _range_key(), "apps": _enrich_hosts(apps), "protos": protos})


@bp.get("/api/flow/conversations")
@roles("admin", "operator")
def api_flow_conversations():
    rows = db.flow.flow_conversations(_range_key(), _exporter_id(), _limit(10))
    return jsonify({"range": _range_key(), "entries": _enrich_hosts(rows)})


@bp.get("/api/flow/interfaces")
@roles("admin", "operator")
def api_flow_interfaces():
    rows = db.flow.flow_interfaces(_range_key(), _exporter_id())
    entries = []
    for row in rows:
        iface, direction = _split_interface(row["key"])
        entries.append({
            "key": row["key"], "interface": iface, "direction": direction,
            "bytes": row["bytes"], "packets": row["packets"], "flows": row["flows"],
        })
    return jsonify({"range": _range_key(), "entries": entries})


@bp.get("/api/flow/status")
@roles("admin", "operator")
def api_flow_status():
    status = FLOW.status()
    counts = db.flow.flow_table_counts()
    return jsonify({
        "receivers": status,
        "counts": counts,
        "exporters": db.flow.list_exporters(),
    })


def _csv(filename, entries):
    return _csv_response(
        filename,
        ["key", "bytes", "packets", "flows"],
        [(e.get("key"), e.get("bytes"), e.get("packets"), e.get("flows")) for e in entries],
    )


@bp.get("/api/flow/talkers.csv")
@roles("admin", "operator")
def api_flow_talkers_csv():
    kind = (request.args.get("kind") or "src").lower()
    if kind not in ("src", "dst"):
        return jsonify({"error": "kind harus src atau dst."}), 400
    rows = db.flow.flow_top(kind, _range_key(), _exporter_id(), 10000)
    return _csv(f"flow-top-{kind}.csv", rows)


@bp.get("/api/flow/apps.csv")
@roles("admin", "operator")
def api_flow_apps_csv():
    rows = db.flow.flow_top("app", _range_key(), _exporter_id(), 10000)
    return _csv("flow-apps.csv", rows)


@bp.get("/api/flow/conversations.csv")
@roles("admin", "operator")
def api_flow_conversations_csv():
    rows = db.flow.flow_conversations(_range_key(), _exporter_id(), 10000)
    return _csv("flow-conversations.csv", rows)
