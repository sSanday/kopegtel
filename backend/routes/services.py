"""Blueprint Layanan & SLA: CRUD layanan, perangkat penopang, laporan SLA."""
import csv
import io

from flask import Blueprint, Response, jsonify, request

import db
from core.csvsafe import csv_row
from core.guard import audit, roles
from core.constants import SLA_PERIODS

bp = Blueprint("services", __name__)



def _validate_service(data):
    if not isinstance(data, dict):
        return {}, "Payload tidak valid."
    name = str(data.get("name") or "").strip()
    if not name:
        return {}, "Nama layanan wajib diisi."
    if len(name) > 120:
        return {}, "Nama layanan maksimal 120 karakter."
    division = str(data.get("division") or "").strip()[:120]
    description = str(data.get("description") or "").strip()[:300]
    try:
        sla_target = float(data.get("sla_target", 99.0))
    except (TypeError, ValueError):
        return {}, "Target SLA harus berupa angka persen."
    if sla_target < 0 or sla_target > 100:
        return {}, "Target SLA harus antara 0 dan 100."
    mode = str(data.get("mode") or "all").strip().lower()
    if mode not in ("all", "any"):
        return {}, "Mode harus 'all' (semua perangkat up) atau 'any' (minimal satu up)."
    return {"name": name, "division": division, "description": description,
            "sla_target": sla_target, "mode": mode}, None


@bp.get("/api/services")
def api_list_services():
    return jsonify({"services": db.list_services()})


@bp.post("/api/services")
@roles("admin", "operator")
def api_create_service():
    clean, error = _validate_service(request.get_json(silent=True) or {})
    if error:
        return jsonify({"error": error}), 400
    if db.service_name_exists(clean["name"]):
        return jsonify({"error": "Nama layanan sudah ada."}), 409
    service_id = db.create_service(clean)
    audit("service.create", f"service:{service_id}", f"Menambah layanan {clean['name']}")
    return jsonify({"service": db.get_service(service_id)}), 201


@bp.put("/api/services/<int:service_id>")
@roles("admin", "operator")
def api_update_service(service_id):
    if db.get_service(service_id) is None:
        return jsonify({"error": "Layanan tidak ditemukan."}), 404
    clean, error = _validate_service(request.get_json(silent=True) or {})
    if error:
        return jsonify({"error": error}), 400
    if db.service_name_exists(clean["name"], exclude_id=service_id):
        return jsonify({"error": "Nama layanan sudah ada."}), 409
    db.update_service(service_id, clean)
    audit("service.update", f"service:{service_id}", f"Mengubah layanan {clean['name']}")
    return jsonify({"service": db.get_service(service_id)})


@bp.delete("/api/services/<int:service_id>")
@roles("admin", "operator")
def api_delete_service(service_id):
    existing = db.get_service(service_id)
    if existing is None:
        return jsonify({"error": "Layanan tidak ditemukan."}), 404
    db.delete_service(service_id)
    audit("service.delete", f"service:{service_id}", f"Menghapus layanan {existing['name']}")
    return jsonify({"ok": True})


@bp.put("/api/services/<int:service_id>/devices")
@roles("admin", "operator")
def api_set_service_devices(service_id):
    service = db.get_service(service_id)
    if service is None:
        return jsonify({"error": "Layanan tidak ditemukan."}), 404
    data = request.get_json(silent=True) or {}
    ids = data.get("device_ids") or []
    if not isinstance(ids, list):
        return jsonify({"error": "device_ids harus berupa daftar id perangkat."}), 400
    valid_ids = []
    for raw in ids:
        try:
            number = int(raw)
        except (TypeError, ValueError):
            return jsonify({"error": "device_ids berisi id yang tidak valid."}), 400
        if db.get_device(number) is None:
            return jsonify({"error": f"Perangkat id {number} tidak ditemukan."}), 404
        valid_ids.append(number)
    db.set_service_devices(service_id, valid_ids)
    audit("service.devices", f"service:{service_id}", f"Menetapkan {len(valid_ids)} perangkat penopang")
    return jsonify({"service": db.get_service(service_id)})


@bp.get("/api/services/<int:service_id>/availability")
def api_service_availability(service_id):
    try:
        days = min(30, max(1, int(request.args.get("days", "1"))))
    except ValueError:
        days = 1
    result = db.service_availability(service_id, days)
    if result is None:
        return jsonify({"error": "Layanan tidak ditemukan."}), 404
    return jsonify(result)


def _sla_report_rows(days):
    rows = []
    minutes_per_day = 24 * 60
    for service in db.list_services():
        stats = db.service_availability(service["id"], days)
        pct = stats["availability_pct"] if stats else None
        met = None if pct is None else pct >= service["sla_target"]
        downtime = None if pct is None else round((1 - pct / 100.0) * days * minutes_per_day, 1)
        rows.append(
            {
                "service_id": service["id"],
                "service": service["name"],
                "division": service["division"],
                "mode": service["mode"],
                "devices": service["device_count"],
                "sla_target": service["sla_target"],
                "availability_pct": pct,
                "met": met,
                "downtime_minutes": downtime,
                "cycles": stats["cycles"] if stats else 0,
            }
        )
    return rows


@bp.get("/api/services/sla-report")
def api_sla_report():
    period = (request.args.get("period") or "7").lower()
    days = SLA_PERIODS.get(period)
    if days is None:
        return jsonify({"error": "Periode tidak valid. Gunakan 1, 7, atau 30."}), 400
    return jsonify({"period_days": days, "rows": _sla_report_rows(days)})


@bp.get("/api/services/sla-report.csv")
def api_sla_report_csv():
    period = (request.args.get("period") or "7").lower()
    days = SLA_PERIODS.get(period)
    if days is None:
        return jsonify({"error": "Periode tidak valid. Gunakan 1, 7, atau 30."}), 400
    rows = _sla_report_rows(days)
    audit("report.sla_csv", "report", f"Mengunduh laporan SLA {days} hari")
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Layanan", "Divisi", "Mode", "Perangkat", "Target SLA %", "Ketersediaan %", "Tercapai", "Downtime (menit)"])
    for row in rows:
        writer.writerow(csv_row([
            row["service"], row["division"], row["mode"], row["devices"],
            row["sla_target"],
            row["availability_pct"] if row["availability_pct"] is not None else "—",
            ("Ya" if row["met"] else "Tidak") if row["met"] is not None else "—",
            row["downtime_minutes"] if row["downtime_minutes"] is not None else "—",
        ]))
    return Response(
        "﻿" + output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=laporan-sla-{days}h.csv"},
    )
