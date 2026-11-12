"""Blueprint Fiber Optik: ringkasan optik, port, link, pengukuran, profil, pengaturan."""
import csv
import io
from datetime import datetime, timedelta, timezone

from flask import Blueprint, g, jsonify, request, Response
import json

import db
import fiber as fiber_calc
import optical as optical_logic
from core.guard import audit, roles
from core.validation import _parse_optional_id

bp = Blueprint("fiber", __name__)

# ---------------------------------------------------------
# Modul fiber optik (F1–F5)
# ---------------------------------------------------------

_OPTICAL_RANGE_HOURS = {"24h": 24, "7d": 24 * 7, "30d": 24 * 30, "90d": 24 * 90}


def _optical_range_hours(range_name):
    return _OPTICAL_RANGE_HOURS.get((range_name or "").lower())


def _optical_history_since(hours):
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _port_status(row, link, profile):
    """Status port untuk tampilan: tanpa mengarang ambang saat profil tidak ada."""
    if not row.get("module_present", True):
        return "no_module"
    rx = row.get("rx_dbm")
    if row.get("rx_loss") or (rx is not None and rx <= -40.0):
        return "los"
    if not row.get("ddm_supported", True):
        return "no_ddm"
    if profile:
        warn_low, crit_low = profile.get("rx_warn_low"), profile.get("rx_crit_low")
        warn_high, crit_high = profile.get("rx_warn_high"), profile.get("rx_crit_high")
        if rx is not None:
            if (crit_low is not None and rx < crit_low) or (crit_high is not None and rx > crit_high):
                return "crit"
            if (warn_low is not None and rx < warn_low) or (warn_high is not None and rx > warn_high):
                return "warn"
        return "ok"
    return "unknown"


# ===== Ringkasan =====


@bp.get("/api/optical/summary")
def api_optical_summary():
    summary = db.optical_summary_counts()
    problem_links = []
    for link in db.list_fiber_links():
        status, metrics = fiber_calc.cached_or_live_metrics(link)
        if status in ("warn", "crit"):
            problem_links.append(
                {
                    "id": link["id"],
                    "name": link["name"],
                    "status": status,
                    "device_a_name": link["device_a_name"],
                    "port_a": link["port_a"],
                    "end_b": link["device_b_name"] or link.get("end_b_label") or "—",
                    "attenuation_ab_db": metrics.get("attenuation_ab_db"),
                    "margin_b_db": metrics.get("margin_b_db"),
                }
            )
    problem_links.sort(key=lambda item: (0 if item["status"] == "crit" else 1, -(item["attenuation_ab_db"] or 0)))
    summary["problem_links"] = problem_links[:10]
    return jsonify(summary)


# ===== Port optik =====


@bp.get("/api/optical/ports")
def api_optical_ports():
    device_param = (request.args.get("device_id") or "").strip()
    status_filter = (request.args.get("status") or "").strip().lower()
    device_id = None
    if device_param:
        try:
            device_id = int(device_param)
        except ValueError:
            return jsonify({"error": "Perangkat tidak valid."}), 400

    rows = db.latest_optical_readings()
    modules = {(m["device_id"], m["if_name"]): m for m in db.list_optical_modules()}
    states = {(s["device_id"], s["if_name"]): s for s in db.list_optical_port_states()}
    links = db.list_fiber_links()
    link_by_port = {}
    for link in links:
        link_by_port.setdefault((link["device_a_id"], link["port_a"]), link)
        if link.get("device_b_id") and link.get("port_b"):
            link_by_port.setdefault((link["device_b_id"], link["port_b"]), link)

    profiles = {}

    def profile_for(link):
        if not link or not link.get("profile_id"):
            return None
        pid = link["profile_id"]
        if pid not in profiles:
            profiles[pid] = db.get_optical_profile(pid)
        return profiles[pid]

    ports = []
    for row in rows:
        if device_id is not None and row["device_id"] != device_id:
            continue
        key = (row["device_id"], row["if_name"])
        module = modules.get(key)
        state = states.get(key)
        link = link_by_port.get(key)
        profile = profile_for(link)
        entry = {
            "id": state["id"] if state else None,
            "device_id": row["device_id"],
            "device_name": row["device_name"],
            "device_ip": row["device_ip"],
            "device_status": row["device_status"],
            "if_name": row["if_name"],
            "rx_dbm": row["rx_dbm"],
            "tx_dbm": row["tx_dbm"],
            "temp_c": row["temp_c"],
            "voltage_v": row["voltage_v"],
            "bias_ma": row["bias_ma"],
            "rx_loss": bool(row["rx_loss"]),
            "tx_fault": bool(row["tx_fault"]),
            "module_present": bool(row["module_present"]),
            "ddm_supported": bool(row["ddm_supported"]),
            "status": _port_status(row, link, profile),
            "link_id": link["id"] if link else None,
            "link_name": link["name"] if link else None,
            "last_check": row["ts"],
            "module": (
                {
                    "vendor": module["vendor"],
                    "part_no": module["part_no"],
                    "serial": module["serial"],
                    "type": module["type"],
                    "wavelength_nm": module["wavelength_nm"],
                    "link_length_m": module["link_length_m"],
                    "first_seen": module["first_seen"],
                    "last_seen": module["last_seen"],
                }
                if module
                else None
            ),
        }
        if status_filter and entry["status"] != status_filter:
            continue
        ports.append(entry)

    return jsonify({"ports": ports})


@bp.get("/api/optical/ports/<int:port_id>/history")
def api_optical_port_history(port_id):
    state = db.get_optical_port_state_by_id(port_id)
    if state is None:
        return jsonify({"error": "Port optik tidak ditemukan."}), 404
    range_name = (request.args.get("range") or "24h").lower()
    hours = _optical_range_hours(range_name)
    if hours is None:
        return jsonify({"error": "Rentang tidak valid (24h|7d|30d|90d)."}), 400

    since = _optical_history_since(hours)
    if hours <= 24 * 7:
        points = db.optical_port_history_raw(state["device_id"], state["if_name"], since)
        points = _downsample(points, 300)
        series = [
            {"ts": p["ts"], "rx_dbm": p["rx_dbm"], "tx_dbm": p["tx_dbm"], "temp_c": p["temp_c"]}
            for p in points
        ]
        source = "raw"
    else:
        rows = db.optical_hourly_rows(state["device_id"], state["if_name"], since)
        series = [
            {
                "ts": r["hour_ts"], "rx_dbm": r["rx_avg"], "tx_dbm": r["tx_avg"],
                "temp_c": r["temp_avg"], "rx_min": r["rx_min"], "rx_max": r["rx_max"],
            }
            for r in rows
        ]
        source = "hourly"

    # Ambang untuk garis pada grafik (dari profil link bila port tergabung).
    thresholds = {}
    links = db.fiber_links_using_port(state["device_id"], state["if_name"])
    if links:
        profile = db.get_optical_profile(links[0]["profile_id"]) if links[0].get("profile_id") else None
        if profile:
            thresholds = {
                "rx_warn_low": profile.get("rx_warn_low"),
                "rx_crit_low": profile.get("rx_crit_low"),
                "rx_warn_high": profile.get("rx_warn_high"),
                "rx_crit_high": profile.get("rx_crit_high"),
            }

    return jsonify(
        {
            "port_id": port_id,
            "device_id": state["device_id"],
            "if_name": state["if_name"],
            "range": range_name,
            "source": source,
            "points": series,
            "thresholds": thresholds,
        }
    )


@bp.get("/api/optical/modules")
def api_optical_modules():
    return jsonify({"modules": db.list_optical_modules()})


# ===== Link fiber (CRUD operator/admin) =====


def _validate_fiber_link(data, existing_id=None):
    if not isinstance(data, dict):
        return None, "Payload tidak valid."

    name = str(data.get("name") or "").strip()
    if not name:
        return None, "Nama link wajib diisi."
    if len(name) > 120:
        return None, "Nama link maksimal 120 karakter."
    if db.fiber_link_name_exists(name, existing_id):
        return None, "Nama link sudah dipakai."

    try:
        device_a_id = int(data.get("device_a_id"))
    except (TypeError, ValueError):
        return None, "Perangkat ujung A wajib dipilih."
    if db.get_device(device_a_id) is None:
        return None, "Perangkat ujung A tidak ditemukan."

    port_a = str(data.get("port_a") or "").strip()
    if not port_a:
        return None, "Port ujung A wajib diisi."
    if len(port_a) > 64:
        return None, "Nama port ujung A maksimal 64 karakter."

    device_b_id, err = _parse_optional_id(data.get("device_b_id"), "Perangkat ujung B")
    if err:
        return None, err
    if device_b_id == device_a_id:
        return None, "Ujung A dan ujung B tidak boleh perangkat yang sama."
    if device_b_id is not None:
        if db.get_device(device_b_id) is None:
            return None, "Perangkat ujung B tidak ditemukan."
        port_b = str(data.get("port_b") or "").strip()
        if not port_b:
            return None, "Port ujung B wajib diisi bila ujung B adalah perangkat terpantau."
        if len(port_b) > 64:
            return None, "Nama port ujung B maksimal 64 karakter."
        end_b_label = None
    else:
        port_b = None
        end_b_label = str(data.get("end_b_label") or "").strip()
        if not end_b_label:
            return None, "Bila ujung B bukan perangkat terpantau, isi label ujung B."
        if len(end_b_label) > 120:
            return None, "Label ujung B maksimal 120 karakter."

    try:
        length_km = float(data.get("length_km"))
    except (TypeError, ValueError):
        return None, "Panjang fiber harus berupa angka (km)."
    if not (0 < length_km <= 10000):
        return None, "Panjang fiber harus antara 0 dan 10000 km."

    fiber_type = str(data.get("fiber_type") or "SM G.652D").strip()[:60] or "SM G.652D"

    def _wavelength(value, field):
        if value in (None, ""):
            return None, None
        try:
            number = int(value)
        except (TypeError, ValueError):
            return None, f"{field} harus berupa angka (nm)."
        if not (800 <= number <= 1700):
            return None, f"{field} harus antara 800 dan 1700 nm."
        return number, None

    wavelength_nm, err = _wavelength(data.get("wavelength_nm"), "Panjang gelombang")
    if err:
        return None, err
    if wavelength_nm is None:
        wavelength_nm = 1310
    wavelength_b_nm, err = _wavelength(data.get("wavelength_b_nm"), "Panjang gelombang arah B→A")
    if err:
        return None, err

    def _small_int(value, field, default):
        if value in (None, ""):
            return default, None
        try:
            number = int(value)
        except (TypeError, ValueError):
            return None, f"{field} harus berupa angka."
        if number < 0 or number > 10000:
            return None, f"{field} harus antara 0 dan 10000."
        return number, None

    splices, err = _small_int(data.get("splices"), "Jumlah splice", 0)
    if err:
        return None, err
    connectors, err = _small_int(data.get("connectors"), "Jumlah konektor", 2)
    if err:
        return None, err

    splitter_ratio = str(data.get("splitter_ratio") or "").strip().lower().replace(" ", "")
    if splitter_ratio in ("", "-", "none", "tanpa"):
        splitter_ratio = None
    elif splitter_ratio not in {f"1:{n}" for n in (2, 4, 8, 16, 32, 64)}:
        return None, "Nisbah splitter tidak dikenal (diizinkan: 1:2, 1:4, 1:8, 1:16, 1:32, 1:64)."

    profile_id, err = _parse_optional_id(data.get("profile_id"), "Profil optik")
    if err:
        return None, err
    if profile_id is not None and db.get_optical_profile(profile_id) is None:
        return None, "Profil optik tidak ditemukan."

    notes = str(data.get("notes") or "").strip()[:500]

    return {
        "name": name,
        "device_a_id": device_a_id,
        "port_a": port_a,
        "device_b_id": device_b_id,
        "port_b": port_b,
        "end_b_label": end_b_label,
        "length_km": length_km,
        "fiber_type": fiber_type,
        "wavelength_nm": wavelength_nm,
        "wavelength_b_nm": wavelength_b_nm,
        "splices": splices,
        "connectors": connectors,
        "splitter_ratio": splitter_ratio,
        "profile_id": profile_id,
        "notes": notes,
    }, None


@bp.get("/api/fiber-links")
def api_list_fiber_links():
    links = db.list_fiber_links()
    items = []
    for link in links:
        status, metrics = fiber_calc.cached_or_live_metrics(link)
        items.append({**link, "status": status, "metrics": metrics})
    return jsonify({"links": items})


@bp.post("/api/fiber-links")
@roles("admin", "operator")
def api_create_fiber_link():
    data = request.get_json(silent=True) or {}
    clean, error = _validate_fiber_link(data)
    if error:
        return jsonify({"error": error}), 400
    link_id = db.create_fiber_link(clean)
    audit("fiber.link.create", f"fiber_link:{link_id}", f"Menambahkan link fiber {clean['name']!r}")
    return jsonify({"ok": True, "id": link_id}), 201


@bp.put("/api/fiber-links/<int:link_id>")
@roles("admin", "operator")
def api_update_fiber_link(link_id):
    if db.get_fiber_link(link_id) is None:
        return jsonify({"error": "Link tidak ditemukan."}), 404
    data = request.get_json(silent=True) or {}
    clean, error = _validate_fiber_link(data, existing_id=link_id)
    if error:
        return jsonify({"error": error}), 400
    db.update_fiber_link(link_id, clean)
    audit("fiber.link.update", f"fiber_link:{link_id}", f"Mengubah link fiber {clean['name']!r}")
    return jsonify({"ok": True})


@bp.delete("/api/fiber-links/<int:link_id>")
@roles("admin", "operator")
def api_delete_fiber_link(link_id):
    link = db.get_fiber_link(link_id)
    if link is None:
        return jsonify({"error": "Link tidak ditemukan."}), 404
    db.delete_fiber_link(link_id)
    audit("fiber.link.delete", f"fiber_link:{link_id}", f"Menghapus link fiber {link['name']!r}")
    return jsonify({"ok": True})


def _module_brief(modules, device_id, if_name):
    module = modules.get((device_id, if_name)) if device_id else None
    if not module:
        return None
    return {
        "vendor": module["vendor"],
        "part_no": module["part_no"],
        "serial": module["serial"],
        "wavelength_nm": module["wavelength_nm"],
        "type": module["type"],
    }


@bp.get("/api/fiber-links/<int:link_id>")
def api_get_fiber_link(link_id):
    link = db.get_fiber_link(link_id)
    if link is None:
        return jsonify({"error": "Link tidak ditemukan."}), 404
    range_name = (request.args.get("range") or "30d").lower()
    history_days = {"7d": 7, "30d": 30, "90d": 90}.get(range_name)
    if history_days is None:
        return jsonify({"error": "Rentang riwayat tidak valid (7d|30d|90d)."}), 400

    result = fiber_calc.evaluate_link(link)
    modules = {(m["device_id"], m["if_name"]): m for m in db.list_optical_modules()}

    # Riwayat redaman per jam (gabungan kedua ujung).
    since = _optical_history_since(history_days * 24)
    hourly_a = db.optical_hourly_rows(link["device_a_id"], link["port_a"], since)
    tx_a = {r["hour_ts"]: r["tx_avg"] for r in hourly_a}
    rx_a = {r["hour_ts"]: r["rx_avg"] for r in hourly_a}
    if link.get("device_b_id") and link.get("port_b"):
        hourly_b = db.optical_hourly_rows(link["device_b_id"], link["port_b"], since)
    else:
        hourly_b = []
    tx_b = {r["hour_ts"]: r["tx_avg"] for r in hourly_b}
    rx_b = {r["hour_ts"]: r["rx_avg"] for r in hourly_b}
    history = []
    for hour in sorted(set(tx_a) | set(rx_a) | set(tx_b) | set(rx_b)):
        entry = {"hour_ts": hour}
        if tx_a.get(hour) is not None and rx_b.get(hour) is not None:
            entry["attenuation_ab_db"] = round(tx_a[hour] - rx_b[hour], 2)
        if tx_b.get(hour) is not None and rx_a.get(hour) is not None:
            entry["attenuation_ba_db"] = round(tx_b[hour] - rx_a[hour], 2)
        history.append(entry)

    return jsonify(
        {
            "link": link,
            "evaluation": fiber_calc.public_eval(result),
            "baseline_rx_a_dbm": result.get("baseline_rx_a_dbm"),
            "baseline_rx_b_dbm": result.get("baseline_rx_b_dbm"),
            "baseline_samples_a": result.get("baseline_samples_a"),
            "baseline_samples_b": result.get("baseline_samples_b"),
            "trend_rx_a_db_per_week": result.get("trend_rx_a_db_per_week"),
            "trend_rx_b_db_per_week": result.get("trend_rx_b_db_per_week"),
            "profile": result.get("profile"),
            "modules": {
                "a": _module_brief(modules, link["device_a_id"], link["port_a"]),
                "b": _module_brief(modules, link.get("device_b_id"), link.get("port_b")),
            },
            "measurements": db.list_fiber_measurements(link_id, limit=100),
            "history": history,
        }
    )


# ===== Pengukuran manual (OTDR / power meter) =====


def _validate_fiber_measurement(data):
    if not isinstance(data, dict):
        return None, "Payload tidak valid."
    measured = str(data.get("measured_at") or "").strip()
    if not measured:
        return None, "Tanggal pengukuran wajib diisi."
    parsed = None
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            parsed = datetime.strptime(measured, fmt)
            break
        except ValueError:
            continue
    if parsed is None:
        return None, "Tanggal pengukuran tidak valid (gunakan YYYY-MM-DD)."
    measured_at = parsed.strftime("%Y-%m-%dT%H:%M:%SZ" if ("T" in measured or " " in measured) else "%Y-%m-%d")

    technician = str(data.get("technician") or "").strip()[:80]
    point = str(data.get("point") or "").strip()[:120]
    method = str(data.get("method") or "otdr").strip().lower()
    if method not in ("otdr", "power_meter"):
        return None, "Metode harus 'otdr' atau 'power_meter'."

    def _num(value, label):
        if value in (None, ""):
            return None, None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None, f"{label} harus berupa angka."
        if abs(number) > 300:
            return None, f"{label} di luar rentang wajar (±300)."
        return number, None

    value_db, err = _num(data.get("value_db"), "Nilai dB")
    if err:
        return None, err
    value_dbm, err = _num(data.get("value_dbm"), "Nilai dBm")
    if err:
        return None, err
    if value_db is None and value_dbm is None:
        return None, "Isi minimal satu nilai: redaman (dB) atau daya terima (dBm)."

    notes = str(data.get("notes") or "").strip()[:500]

    return {
        "measured_at": measured_at,
        "technician": technician,
        "point": point,
        "method": method,
        "value_db": value_db,
        "value_dbm": value_dbm,
        "notes": notes,
    }, None


@bp.get("/api/fiber-links/<int:link_id>/measurements")
def api_list_fiber_measurements(link_id):
    if db.get_fiber_link(link_id) is None:
        return jsonify({"error": "Link tidak ditemukan."}), 404
    return jsonify({"measurements": db.list_fiber_measurements(link_id, limit=300)})


@bp.post("/api/fiber-links/<int:link_id>/measurements")
@roles("admin", "operator")
def api_create_fiber_measurement(link_id):
    link = db.get_fiber_link(link_id)
    if link is None:
        return jsonify({"error": "Link tidak ditemukan."}), 404
    data = request.get_json(silent=True) or {}
    clean, error = _validate_fiber_measurement(data)
    if error:
        return jsonify({"error": error}), 400
    clean["link_id"] = link_id
    clean["created_by"] = g.user["username"]
    measurement_id = db.create_fiber_measurement(clean)
    audit("fiber.measurement.create", f"fiber_link:{link_id}", f"Mencatat pengukuran pada link {link['name']!r}")
    return jsonify({"ok": True, "id": measurement_id}), 201


@bp.put("/api/fiber-links/<int:link_id>/measurements/<int:measurement_id>")
@roles("admin", "operator")
def api_update_fiber_measurement(link_id, measurement_id):
    measurement = db.get_fiber_measurement(measurement_id)
    if measurement is None or measurement["link_id"] != link_id:
        return jsonify({"error": "Pengukuran tidak ditemukan pada link ini."}), 404
    data = request.get_json(silent=True) or {}
    clean, error = _validate_fiber_measurement(data)
    if error:
        return jsonify({"error": error}), 400
    db.update_fiber_measurement(measurement_id, clean)
    audit("fiber.measurement.update", f"fiber_link:{link_id}", f"Mengubah pengukuran #{measurement_id}")
    return jsonify({"ok": True})


@bp.delete("/api/fiber-links/<int:link_id>/measurements/<int:measurement_id>")
@roles("admin", "operator")
def api_delete_fiber_measurement(link_id, measurement_id):
    measurement = db.get_fiber_measurement(measurement_id)
    if measurement is None or measurement["link_id"] != link_id:
        return jsonify({"error": "Pengukuran tidak ditemukan pada link ini."}), 404
    db.delete_fiber_measurement(measurement_id)
    audit("fiber.measurement.delete", f"fiber_link:{link_id}", f"Menghapus pengukuran #{measurement_id}")
    return jsonify({"ok": True})


# ===== Profil optik (admin) =====


def _parse_optional_number(value, label, lo, hi):
    if value in (None, ""):
        return None, None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None, f"{label} harus berupa angka."
    if not (lo <= number <= hi):
        return None, f"{label} harus antara {lo} dan {hi}."
    return number, None


def _validate_optical_profile(data, existing_id=None):
    if not isinstance(data, dict):
        return None, "Payload tidak valid."
    name = str(data.get("name") or "").strip()
    if not name:
        return None, "Nama profil wajib diisi."
    if len(name) > 120:
        return None, "Nama profil maksimal 120 karakter."
    if db.optical_profile_name_exists(name, existing_id):
        return None, "Nama profil sudah dipakai."
    description = str(data.get("description") or "").strip()[:300]
    clean = {"name": name, "description": description}
    for key, label, lo, hi in (
        ("rx_warn_low", "Ambang Rx waspada (rendah)", -60.0, 60.0),
        ("rx_crit_low", "Ambang Rx kritis (rendah)", -60.0, 60.0),
        ("rx_warn_high", "Ambang Rx waspada (tinggi)", -60.0, 60.0),
        ("rx_crit_high", "Ambang Rx kritis (tinggi)", -60.0, 60.0),
        ("temp_warn_c", "Ambang suhu waspada (°C)", -40.0, 150.0),
        ("temp_crit_c", "Ambang suhu kritis (°C)", -40.0, 150.0),
        ("rx_sensitivity_dbm", "Sensitivitas penerima (dBm)", -60.0, 60.0),
        ("rx_overload_dbm", "Batas overload (dBm)", -60.0, 60.0),
    ):
        number, error = _parse_optional_number(data.get(key), label, lo, hi)
        if error:
            return None, error
        clean[key] = number
    if (
        clean["rx_warn_low"] is not None and clean["rx_crit_low"] is not None
        and clean["rx_crit_low"] > clean["rx_warn_low"]
    ):
        return None, "Ambang kritis rendah harus lebih kecil atau sama dengan ambang waspada rendah."
    if (
        clean["rx_warn_high"] is not None and clean["rx_crit_high"] is not None
        and clean["rx_crit_high"] < clean["rx_warn_high"]
    ):
        return None, "Ambang kritis tinggi harus lebih besar atau sama dengan ambang waspada tinggi."
    return clean, None


@bp.get("/api/optical/profiles")
def api_list_optical_profiles():
    return jsonify({"profiles": db.list_optical_profiles()})


@bp.post("/api/optical/profiles")
@roles("admin")
def api_create_optical_profile():
    data = request.get_json(silent=True) or {}
    clean, error = _validate_optical_profile(data)
    if error:
        return jsonify({"error": error}), 400
    profile_id = db.create_optical_profile(clean)
    audit("optical.profile.create", f"optical_profile:{profile_id}", f"Menambahkan profil optik {clean['name']!r}")
    return jsonify({"ok": True, "id": profile_id}), 201


@bp.put("/api/optical/profiles/<int:profile_id>")
@roles("admin")
def api_update_optical_profile(profile_id):
    if db.get_optical_profile(profile_id) is None:
        return jsonify({"error": "Profil tidak ditemukan."}), 404
    data = request.get_json(silent=True) or {}
    clean, error = _validate_optical_profile(data, existing_id=profile_id)
    if error:
        return jsonify({"error": error}), 400
    db.update_optical_profile(profile_id, clean)
    audit("optical.profile.update", f"optical_profile:{profile_id}", f"Mengubah profil optik {clean['name']!r}")
    return jsonify({"ok": True})


@bp.delete("/api/optical/profiles/<int:profile_id>")
@roles("admin")
def api_delete_optical_profile(profile_id):
    ok, used = db.delete_optical_profile(profile_id)
    if not ok:
        if used:
            return jsonify({"error": f"Profil masih dipakai oleh {used} link fiber."}), 409
        return jsonify({"error": "Profil tidak ditemukan."}), 404
    audit("optical.profile.delete", f"optical_profile:{profile_id}", "Menghapus profil optik")
    return jsonify({"ok": True})


# ===== Pengaturan fiber (admin untuk tulis) =====


@bp.get("/api/optical/settings")
def api_get_optical_settings():
    return jsonify({"settings": db.get_fiber_settings()})


@bp.put("/api/optical/settings")
@roles("admin")
def api_update_optical_settings():
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({"error": "Payload tidak valid."}), 400
    clean = {}
    float_specs = {
        "fiber_alpha_1310": (0.01, 5.0),
        "fiber_alpha_1550": (0.01, 5.0),
        "fiber_splice_loss": (0.0, 5.0),
        "fiber_connector_loss": (0.0, 5.0),
        "fiber_deviation_warn": (0.5, 30.0),
        "fiber_deviation_crit": (0.5, 30.0),
        "fiber_margin_warn": (0.5, 30.0),
        "fiber_margin_crit": (0.0, 30.0),
        "fiber_asymmetry_warn": (0.5, 30.0),
        "fiber_drop_db": (0.5, 30.0),
        "fiber_trend_db_per_week": (0.05, 20.0),
        "fiber_hysteresis_db": (0.0, 6.0),
    }
    int_specs = {
        "fiber_drop_baseline_days": (1, 90),
        "fiber_trend_days": (3, 90),
        "fiber_consec_required": (1, 10),
        "fiber_avg_readings": (1, 10),
        "retention_optical_hourly_days": (1, 3650),
    }
    for key, (lo, hi) in float_specs.items():
        if key not in data:
            continue
        try:
            number = float(data[key])
        except (TypeError, ValueError):
            return jsonify({"error": f"{key} harus berupa angka."}), 400
        if not (lo <= number <= hi):
            return jsonify({"error": f"{key} harus antara {lo} dan {hi}."}), 400
        clean[key] = number
    for key, (lo, hi) in int_specs.items():
        if key not in data:
            continue
        try:
            number = int(float(data[key]))
        except (TypeError, ValueError):
            return jsonify({"error": f"{key} harus berupa angka."}), 400
        if not (lo <= number <= hi):
            return jsonify({"error": f"{key} harus antara {lo} dan {hi}."}), 400
        clean[key] = number
    splitter = data.get("fiber_splitter_losses")
    if splitter is not None:
        if not isinstance(splitter, dict):
            return jsonify({"error": "fiber_splitter_losses harus berupa objek {\"1:2\": 3.5, ...}."}), 400
        allowed = {f"1:{n}" for n in (2, 4, 8, 16, 32, 64)}
        table = {}
        for key, value in splitter.items():
            norm = str(key).strip().lower().replace(" ", "")
            if norm not in allowed:
                return jsonify({"error": f"Nisbah splitter tidak dikenal: {norm} (diizinkan: 1:2..1:64)."}), 400
            try:
                loss = float(value)
            except (TypeError, ValueError):
                return jsonify({"error": f"Redaman splitter {norm} harus berupa angka."}), 400
            if not (0.5 <= loss <= 40.0):
                return jsonify({"error": f"Redaman splitter {norm} harus antara 0.5 dan 40 dB."}), 400
            table[norm] = loss
        clean["fiber_splitter_losses"] = json.dumps(table)
    if not clean:
        return jsonify({"error": "Tidak ada parameter yang dikirim."}), 400
    if "fiber_deviation_warn" in clean and "fiber_deviation_crit" in clean:
        if clean["fiber_deviation_crit"] < clean["fiber_deviation_warn"]:
            return jsonify({"error": "Ambang deviasi kritis harus >= ambang waspada."}), 400
    db.set_fiber_settings(clean)
    audit("optical.settings.update", "settings", f"Mengubah parameter fiber: {', '.join(sorted(clean))}")
    return jsonify({"ok": True, "settings": db.get_fiber_settings()})


# ===== Kalkulator anggaran link =====


@bp.post("/api/fiber/calc")
def api_fiber_calc():
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({"error": "Payload tidak valid."}), 400
    settings = db.get_fiber_settings()

    try:
        length_km = float(data.get("length_km"))
    except (TypeError, ValueError):
        return jsonify({"error": "Panjang harus berupa angka (km)."}), 400
    if not (0 < length_km <= 10000):
        return jsonify({"error": "Panjang harus antara 0 dan 10000 km."}), 400

    wavelength_nm = data.get("wavelength_nm", 1310)
    try:
        wavelength_nm = int(wavelength_nm)
    except (TypeError, ValueError):
        return jsonify({"error": "Panjang gelombang harus berupa angka (nm)."}), 400
    if not (800 <= wavelength_nm <= 1700):
        return jsonify({"error": "Panjang gelombang harus antara 800 dan 1700 nm."}), 400

    def _small_int(value, field, default):
        if value in (None, ""):
            return default, None
        try:
            number = int(value)
        except (TypeError, ValueError):
            return None, f"{field} harus berupa angka."
        if number < 0 or number > 10000:
            return None, f"{field} harus antara 0 dan 10000."
        return number, None

    splices, err = _small_int(data.get("splices"), "Jumlah splice", 0)
    if err:
        return jsonify({"error": err}), 400
    connectors, err = _small_int(data.get("connectors"), "Jumlah konektor", 2)
    if err:
        return jsonify({"error": err}), 400

    splitter_ratio = str(data.get("splitter_ratio") or "").strip().lower().replace(" ", "")
    if splitter_ratio in ("", "-", "none", "tanpa"):
        splitter_ratio = None
    elif splitter_ratio not in {f"1:{n}" for n in (2, 4, 8, 16, 32, 64)}:
        return jsonify({"error": "Nisbah splitter tidak dikenal (diizinkan: 1:2, 1:4, 1:8, 1:16, 1:32, 1:64)."}), 400

    budget = optical_logic.link_budget_db(
        length_km, wavelength_nm,
        splices=splices, connectors=connectors, splitter_ratio=splitter_ratio,
        alpha_1310=settings["fiber_alpha_1310"], alpha_1550=settings["fiber_alpha_1550"],
        splice_loss=settings["fiber_splice_loss"], connector_loss=settings["fiber_connector_loss"],
        splitter_table=settings["fiber_splitter_losses"],
    )
    if budget is None:
        return jsonify({"error": "Nisbah splitter tidak ada di tabel redaman (Pengaturan fiber)."}), 400

    response = {
        "wavelength_nm": wavelength_nm,
        "length_km": length_km,
        "budget_db": budget["total_db"],
        "alpha_db_per_km": budget["alpha_db_per_km"],
        "steps": budget["steps"],
        "parameters": {
            "alpha_1310": settings["fiber_alpha_1310"],
            "alpha_1550": settings["fiber_alpha_1550"],
            "splice_loss": settings["fiber_splice_loss"],
            "connector_loss": settings["fiber_connector_loss"],
            "splitter_losses": settings["fiber_splitter_losses"],
        },
    }

    tx_power_dbm = data.get("tx_power_dbm")
    if tx_power_dbm not in (None, ""):
        try:
            tx_power_dbm = float(tx_power_dbm)
        except (TypeError, ValueError):
            return jsonify({"error": "Daya Tx harus berupa angka (dBm)."}), 400
        if not (-100 <= tx_power_dbm <= 100):
            return jsonify({"error": "Daya Tx di luar rentang wajar (±100 dBm)."}), 400
        rx_estimate = round(tx_power_dbm - budget["total_db"], 2)
        response["tx_power_dbm"] = tx_power_dbm
        response["rx_estimate_dbm"] = rx_estimate
        sensitivity = data.get("rx_sensitivity_dbm")
        if sensitivity not in (None, ""):
            try:
                sensitivity = float(sensitivity)
            except (TypeError, ValueError):
                return jsonify({"error": "Sensitivitas penerima harus berupa angka (dBm)."}), 400
            if not (-100 <= sensitivity <= 100):
                return jsonify({"error": "Sensitivitas penerima di luar rentang wajar (±100 dBm)."}), 400
            response["rx_sensitivity_dbm"] = sensitivity
            response["margin_db"] = round(rx_estimate - sensitivity, 2)

    return jsonify(response)


# ===== Laporan redaman =====


def _fiber_report_rows():
    rows = []
    for link in db.list_fiber_links():
        result = fiber_calc.evaluate_link(link)
        margins = [v for v in (result.get("margin_a_db"), result.get("margin_b_db")) if v is not None]
        rows.append(
            {
                "link_id": link["id"],
                "link": link["name"],
                "device_a_name": link["device_a_name"],
                "port_a": link["port_a"],
                "end_b": link["device_b_name"] or link.get("end_b_label") or "—",
                "length_km": link["length_km"],
                "wavelength_nm": result.get("wavelength_ab_nm"),
                "attenuation_ab_db": result.get("attenuation_ab_db"),
                "attenuation_ba_db": result.get("attenuation_ba_db"),
                "budget_ab_db": result.get("budget_ab_db"),
                "deviation_ab_db": result.get("deviation_ab_db"),
                "deviation_ba_db": result.get("deviation_ba_db"),
                "margin_a_db": result.get("margin_a_db"),
                "margin_b_db": result.get("margin_b_db"),
                "margin_min_db": min(margins) if margins else None,
                "trend_rx_a_db_per_week": result.get("trend_rx_a_db_per_week"),
                "trend_rx_b_db_per_week": result.get("trend_rx_b_db_per_week"),
                "status": result.get("status"),
            }
        )
    rows.sort(key=lambda row: (row["margin_min_db"] if row["margin_min_db"] is not None else 999.0))
    return rows


@bp.get("/api/reports/fiber-attenuation")
def api_report_fiber_attenuation():
    return jsonify({"rows": _fiber_report_rows()})


@bp.get("/api/reports/fiber-attenuation.csv")
def api_report_fiber_attenuation_csv():
    rows = _fiber_report_rows()
    audit("report.fiber_csv", "report", "Mengunduh laporan redaman fiber")
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Link", "Ujung A", "Ujung B", "Panjang (km)", "λ (nm)",
        "Redaman A→B (dB)", "Redaman B→A (dB)", "Anggaran A→B (dB)",
        "Deviasi A→B (dB)", "Deviasi B→A (dB)", "Margin A (dB)", "Margin B (dB)",
        "Tren A (dB/minggu)", "Tren B (dB/minggu)", "Status",
    ])

    def _cell(value, unit=""):
        return ("—" if value is None else f"{value:.2f}{unit}")

    for row in rows:
        writer.writerow([
            row["link"],
            f"{row['device_a_name']} / {row['port_a']}",
            row["end_b"],
            row["length_km"],
            row["wavelength_nm"] or "—",
            _cell(row["attenuation_ab_db"]), _cell(row["attenuation_ba_db"]),
            _cell(row["budget_ab_db"]),
            _cell(row["deviation_ab_db"]), _cell(row["deviation_ba_db"]),
            _cell(row["margin_a_db"]), _cell(row["margin_b_db"]),
            _cell(row["trend_rx_a_db_per_week"]), _cell(row["trend_rx_b_db_per_week"]),
            row["status"],
        ])
    return Response("\uFEFF" + output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=laporan-redaman-fiber.csv"},
    )


