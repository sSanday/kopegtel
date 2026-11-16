"""Fiber optik: pengukuran DDM, modul optik, link, verifikasi."""

import json

from .connection import (
    _lock,
    transaction,
    _now,
    get_conn,
)

FIBER_SETTING_DEFAULTS = {
    "fiber_alpha_1310": "0.35",
    "fiber_alpha_1550": "0.25",
    "fiber_splice_loss": "0.1",
    "fiber_connector_loss": "0.5",
    "fiber_splitter_losses": '{"1:2": 3.5, "1:4": 6.5, "1:8": 9.5, "1:16": 12.5, "1:32": 15.5, "1:64": 18.5}',
    "fiber_deviation_warn": "3",
    "fiber_deviation_crit": "5",
    "fiber_margin_warn": "3",
    "fiber_margin_crit": "1",
    "fiber_asymmetry_warn": "3",
    "fiber_drop_db": "3",
    "fiber_drop_baseline_days": "7",
    "fiber_trend_db_per_week": "0.5",
    "fiber_trend_days": "14",
    "fiber_consec_required": "2",
    "fiber_hysteresis_db": "1",
    "fiber_avg_readings": "3",
    "retention_optical_hourly_days": "90",
}

_FIBER_INT_KEYS = {
    "fiber_drop_baseline_days", "fiber_trend_days", "fiber_consec_required",
    "fiber_avg_readings", "retention_optical_hourly_days",
}

def get_fiber_settings():
    """Parameter fiber ter-parse (float/int/dict); selalu lengkap (fallback default)."""
    keys = list(FIBER_SETTING_DEFAULTS)
    placeholders = ",".join("?" * len(keys))
    with _lock:
        rows = get_conn().execute(
            f"SELECT key, value FROM settings WHERE key IN ({placeholders})", keys
        ).fetchall()
    raw = dict(FIBER_SETTING_DEFAULTS)
    for row in rows:
        raw[row["key"]] = row["value"]
    parsed = {}
    for key, value in raw.items():
        default = FIBER_SETTING_DEFAULTS[key]
        if key == "fiber_splitter_losses":
            try:
                table = json.loads(value)
                parsed[key] = {str(k).strip().lower(): float(v) for k, v in (table or {}).items()}
            except (json.JSONDecodeError, TypeError, ValueError, AttributeError):
                parsed[key] = json.loads(default)
        elif key in _FIBER_INT_KEYS:
            try:
                parsed[key] = int(float(value))
            except (TypeError, ValueError):
                parsed[key] = int(default)
        else:
            try:
                parsed[key] = float(value)
            except (TypeError, ValueError):
                parsed[key] = float(default)
    return parsed

def set_fiber_settings(values):
    """values: dict kunci fiber_* → nilai (sudah tervalidasi API). Kembalikan jumlah tersimpan."""
    now = _now()
    with transaction() as conn:
        count = 0
        for key, value in values.items():
            if key not in FIBER_SETTING_DEFAULTS:
                continue
            conn.execute(
                "INSERT INTO settings (key, value, updated_at) VALUES (?,?,?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, str(value), now),
            )
            count += 1
    return count

def list_optical_profiles():
    with _lock:
        rows = get_conn().execute("SELECT * FROM optical_profiles ORDER BY name COLLATE NOCASE").fetchall()
    return [dict(r) for r in rows]

def get_optical_profile(profile_id):
    with _lock:
        row = get_conn().execute("SELECT * FROM optical_profiles WHERE id = ?", (profile_id,)).fetchone()
    return dict(row) if row else None

def optical_profile_name_exists(name, exclude_id=None):
    with _lock:
        row = get_conn().execute(
            "SELECT id FROM optical_profiles WHERE name = ? COLLATE NOCASE AND (? IS NULL OR id != ?)",
            (name, exclude_id, exclude_id),
        ).fetchone()
    return row is not None

def create_optical_profile(data):
    now = _now()
    with transaction() as conn:
        cursor = conn.execute(
            """
            INSERT INTO optical_profiles
                (name, description, rx_warn_low, rx_crit_low, rx_warn_high, rx_crit_high,
                 temp_warn_c, temp_crit_c, rx_sensitivity_dbm, rx_overload_dbm, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                data["name"], data.get("description", ""),
                data.get("rx_warn_low"), data.get("rx_crit_low"),
                data.get("rx_warn_high"), data.get("rx_crit_high"),
                data.get("temp_warn_c"), data.get("temp_crit_c"),
                data.get("rx_sensitivity_dbm"), data.get("rx_overload_dbm"),
                now, now,
            ),
        )
        return cursor.lastrowid

def update_optical_profile(profile_id, data):
    now = _now()
    with transaction() as conn:
        cursor = conn.execute(
            """
            UPDATE optical_profiles SET
                name=?, description=?, rx_warn_low=?, rx_crit_low=?, rx_warn_high=?, rx_crit_high=?,
                temp_warn_c=?, temp_crit_c=?, rx_sensitivity_dbm=?, rx_overload_dbm=?, updated_at=?
            WHERE id=?
            """,
            (
                data["name"], data.get("description", ""),
                data.get("rx_warn_low"), data.get("rx_crit_low"),
                data.get("rx_warn_high"), data.get("rx_crit_high"),
                data.get("temp_warn_c"), data.get("temp_crit_c"),
                data.get("rx_sensitivity_dbm"), data.get("rx_overload_dbm"),
                now, profile_id,
            ),
        )
        return cursor.rowcount > 0

def delete_optical_profile(profile_id):
    """Hapus ditolak bila masih dipakai link fiber."""
    with transaction() as conn:
        used = conn.execute("SELECT COUNT(*) AS n FROM fiber_links WHERE profile_id = ?", (profile_id,)).fetchone()["n"]
        if used:
            return False, used
        cursor = conn.execute("DELETE FROM optical_profiles WHERE id = ?", (profile_id,))
        return cursor.rowcount > 0, 0

_LINK_SELECT = """
    SELECT l.*, da.name AS device_a_name, da.ip AS device_a_ip,
           dbx.name AS device_b_name, p.name AS profile_name
    FROM fiber_links l
    JOIN devices da ON da.id = l.device_a_id
    LEFT JOIN devices dbx ON dbx.id = l.device_b_id
    LEFT JOIN optical_profiles p ON p.id = l.profile_id
"""

def list_fiber_links():
    with _lock:
        rows = get_conn().execute(_LINK_SELECT + " ORDER BY l.name COLLATE NOCASE").fetchall()
    return [dict(r) for r in rows]

def get_fiber_link(link_id):
    with _lock:
        row = get_conn().execute(_LINK_SELECT + " WHERE l.id = ?", (link_id,)).fetchone()
    return dict(row) if row else None

def fiber_link_name_exists(name, exclude_id=None):
    with _lock:
        row = get_conn().execute(
            "SELECT id FROM fiber_links WHERE name = ? COLLATE NOCASE AND (? IS NULL OR id != ?)",
            (name, exclude_id, exclude_id),
        ).fetchone()
    return row is not None

def create_fiber_link(data):
    now = _now()
    with transaction() as conn:
        cursor = conn.execute(
            """
            INSERT INTO fiber_links
                (name, device_a_id, port_a, device_b_id, port_b, end_b_label,
                 length_km, fiber_type, wavelength_nm, wavelength_b_nm,
                 splices, connectors, splitter_ratio, profile_id, notes, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                data["name"], data["device_a_id"], data["port_a"],
                data.get("device_b_id"), data.get("port_b"), data.get("end_b_label"),
                float(data["length_km"]), data.get("fiber_type", "SM G.652D"),
                int(data.get("wavelength_nm") or 1310), int(data["wavelength_b_nm"]) if data.get("wavelength_b_nm") else None,
                int(data.get("splices") or 0), int(data.get("connectors") if data.get("connectors") is not None else 2),
                data.get("splitter_ratio") or None, data.get("profile_id"),
                data.get("notes", ""), now, now,
            ),
        )
        return cursor.lastrowid

def update_fiber_link(link_id, data):
    now = _now()
    with transaction() as conn:
        cursor = conn.execute(
            """
            UPDATE fiber_links SET
                name=?, device_a_id=?, port_a=?, device_b_id=?, port_b=?, end_b_label=?,
                length_km=?, fiber_type=?, wavelength_nm=?, wavelength_b_nm=?,
                splices=?, connectors=?, splitter_ratio=?, profile_id=?, notes=?, updated_at=?
            WHERE id=?
            """,
            (
                data["name"], data["device_a_id"], data["port_a"],
                data.get("device_b_id"), data.get("port_b"), data.get("end_b_label"),
                float(data["length_km"]), data.get("fiber_type", "SM G.652D"),
                int(data.get("wavelength_nm") or 1310), int(data["wavelength_b_nm"]) if data.get("wavelength_b_nm") else None,
                int(data.get("splices") or 0), int(data.get("connectors") if data.get("connectors") is not None else 2),
                data.get("splitter_ratio") or None, data.get("profile_id"),
                data.get("notes", ""), now, link_id,
            ),
        )
        return cursor.rowcount > 0

def delete_fiber_link(link_id):
    with transaction() as conn:
        cursor = conn.execute("DELETE FROM fiber_links WHERE id = ?", (link_id,))
        return cursor.rowcount > 0

def fiber_links_using_port(device_id, if_name):
    with _lock:
        rows = get_conn().execute(
            "SELECT * FROM fiber_links WHERE (device_a_id = ? AND port_a = ?) OR (device_b_id = ? AND port_b = ?)",
            (device_id, if_name, device_id, if_name),
        ).fetchall()
    return [dict(r) for r in rows]

def record_optical_readings(device_id, ts, readings):
    """readings: list dict per port optik (rx_dbm, tx_dbm, temp_c, voltage_v, bias_ma,
    rx_loss, tx_fault, module_present, ddm_supported)."""
    if not readings:
        return 0
    with transaction() as conn:
        conn.executemany(
            """
            INSERT INTO optical_readings
                (device_id, if_name, ts, rx_dbm, tx_dbm, temp_c, voltage_v, bias_ma,
                 rx_loss, tx_fault, module_present, ddm_supported)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            [
                (
                    device_id, r["if_name"], ts, r.get("rx_dbm"), r.get("tx_dbm"),
                    r.get("temp_c"), r.get("voltage_v"), r.get("bias_ma"),
                    1 if r.get("rx_loss") else 0, 1 if r.get("tx_fault") else 0,
                    1 if r.get("module_present") else 0, 1 if r.get("ddm_supported") else 0,
                )
                for r in readings
            ],
        )
        return len(readings)

def latest_optical_readings():
    """Pembacaan terakhir per (device_id, if_name) — dipakai daftar port & modul."""
    with _lock:
        rows = get_conn().execute(
            """
            SELECT r.*, d.name AS device_name, d.ip AS device_ip, d.status AS device_status
            FROM optical_readings r
            JOIN devices d ON d.id = r.device_id
            WHERE r.id = (
                SELECT MAX(id) FROM optical_readings
                WHERE device_id = r.device_id AND if_name = r.if_name
            )
            ORDER BY d.name COLLATE NOCASE, r.if_name
            """
        ).fetchall()
    return [dict(r) for r in rows]

def recent_optical_readings(device_id, if_name, limit=3):
    """Nilai N pembacaan terakhir (terbaru dulu) — untuk rata-rata & histeresis."""
    limit = max(1, min(20, int(limit or 3)))
    with _lock:
        rows = get_conn().execute(
            "SELECT ts, rx_dbm, tx_dbm, temp_c, voltage_v, bias_ma, rx_loss, tx_fault,"
            " module_present, ddm_supported"
            " FROM optical_readings WHERE device_id = ? AND if_name = ? ORDER BY id DESC LIMIT ?",
            (device_id, if_name, limit),
        ).fetchall()
    return [dict(r) for r in rows]

def optical_port_history_raw(device_id, if_name, since):
    with _lock:
        rows = get_conn().execute(
            "SELECT ts, rx_dbm, tx_dbm, temp_c FROM optical_readings"
            " WHERE device_id = ? AND if_name = ? AND ts >= ? ORDER BY ts ASC",
            (device_id, if_name, since),
        ).fetchall()
    return [dict(r) for r in rows]

def optical_hourly_rows(device_id, if_name, since):
    with _lock:
        rows = get_conn().execute(
            "SELECT hour_ts, rx_avg, rx_min, rx_max, tx_avg, temp_avg, samples"
            " FROM optical_hourly WHERE device_id = ? AND if_name = ? AND hour_ts >= ?"
            " ORDER BY hour_ts ASC",
            (device_id, if_name, since),
        ).fetchall()
    return [dict(r) for r in rows]

def optical_hourly_rx_values(device_id, if_name, since):
    with _lock:
        rows = get_conn().execute(
            "SELECT rx_avg FROM optical_hourly WHERE device_id = ? AND if_name = ? AND hour_ts >= ? AND rx_avg IS NOT NULL",
            (device_id, if_name, since),
        ).fetchall()
    return [row["rx_avg"] for row in rows]

def upsert_optical_module(device_id, if_name, *, vendor, part_no, serial, type_,
                          wavelength_nm, link_length_m, ts):
    """Upsert modul SFP; kembalikan dict {is_new, serial_changed, serial_before, serial_after}."""
    serial = (serial or "").strip() or None
    with transaction() as conn:
        existing = conn.execute(
            "SELECT * FROM optical_modules WHERE device_id = ? AND if_name = ?",
            (device_id, if_name),
        ).fetchone()
        if existing is not None:
            old_serial = (existing["serial"] or "").strip() or None
            serial_changed = bool(serial and old_serial and serial != old_serial)
            keep_serial = serial or old_serial
            conn.execute(
                """
                UPDATE optical_modules SET vendor=?, part_no=?, serial=?, type=?,
                    wavelength_nm=?, link_length_m=?, last_seen=?
                WHERE device_id=? AND if_name=?
                """,
                (vendor or existing["vendor"], part_no or existing["part_no"], keep_serial,
                 type_ or existing["type"], wavelength_nm if wavelength_nm is not None else existing["wavelength_nm"],
                 link_length_m if link_length_m is not None else existing["link_length_m"],
                 ts, device_id, if_name),
            )
            return {"is_new": False, "serial_changed": serial_changed,
                    "serial_before": old_serial, "serial_after": keep_serial}
        conn.execute(
            """
            INSERT INTO optical_modules
                (device_id, if_name, vendor, part_no, serial, type, wavelength_nm, link_length_m, first_seen, last_seen)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (device_id, if_name, vendor, part_no, serial, type_, wavelength_nm, link_length_m, ts, ts),
        )
        return {"is_new": True, "serial_changed": False, "serial_before": None, "serial_after": serial}

def list_optical_modules():
    with _lock:
        rows = get_conn().execute(
            """
            SELECT m.*, d.name AS device_name, d.ip AS device_ip, d.status AS device_status
            FROM optical_modules m JOIN devices d ON d.id = m.device_id
            ORDER BY d.name COLLATE NOCASE, m.if_name
            """
        ).fetchall()
    return [dict(r) for r in rows]

def get_optical_port_state(device_id, if_name):
    with _lock:
        row = get_conn().execute(
            "SELECT * FROM optical_port_state WHERE device_id = ? AND if_name = ?",
            (device_id, if_name),
        ).fetchone()
    return dict(row) if row else None

def get_optical_port_state_by_id(state_id):
    with _lock:
        row = get_conn().execute("SELECT * FROM optical_port_state WHERE id = ?", (state_id,)).fetchone()
    return dict(row) if row else None

def upsert_optical_port_state(device_id, if_name, *, is_optical, ddm_supported, ts, consec=None, last_serial=None):
    consec_json = json.dumps(consec) if consec is not None else None
    with transaction() as conn:
        conn.execute(
            """
            INSERT INTO optical_port_state (device_id, if_name, is_optical, ddm_supported, last_check, consec, last_serial)
            VALUES (?,?,?,?,?,COALESCE(?, '{}'), ?)
            ON CONFLICT(device_id, if_name) DO UPDATE SET
                is_optical = excluded.is_optical,
                ddm_supported = excluded.ddm_supported,
                last_check = excluded.last_check,
                consec = COALESCE(excluded.consec, optical_port_state.consec),
                last_serial = COALESCE(excluded.last_serial, optical_port_state.last_serial)
            """,
            (device_id, if_name, 1 if is_optical else 0, 1 if ddm_supported else 0,
             ts, consec_json, last_serial),
        )

def list_optical_port_states():
    with _lock:
        rows = get_conn().execute("SELECT * FROM optical_port_state ORDER BY device_id, if_name").fetchall()
    return [dict(r) for r in rows]

def get_link_state(link_id):
    with _lock:
        row = get_conn().execute("SELECT * FROM optical_link_state WHERE link_id = ?", (link_id,)).fetchone()
    return dict(row) if row else None

def save_link_state(link_id, *, consec, status, metrics, ts):
    with transaction() as conn:
        conn.execute(
            """
            INSERT INTO optical_link_state (link_id, consec, status, metrics, updated_at)
            VALUES (?,?,?,?,?)
            ON CONFLICT(link_id) DO UPDATE SET
                consec = excluded.consec, status = excluded.status,
                metrics = excluded.metrics, updated_at = excluded.updated_at
            """,
            (link_id, json.dumps(consec), status, json.dumps(metrics or {}), ts),
        )

def optical_summary_counts():
    """Ringkasan modul fiber untuk kartu Dashboard / /api/optical/summary."""
    with _lock:
        row = get_conn().execute(
            """
            WITH latest AS (
                SELECT MAX(id) AS id FROM optical_readings GROUP BY device_id, if_name
            )
            SELECT COUNT(*) AS total,
                   COALESCE(SUM(r.module_present), 0) AS module_present,
                   COALESCE(SUM(r.ddm_supported), 0) AS ddm_supported,
                   AVG(r.rx_dbm) AS avg_rx
            FROM optical_readings r JOIN latest ON latest.id = r.id
            """
        ).fetchone()
        links = get_conn().execute(
            """
            SELECT COUNT(*) AS total,
                   COALESCE(SUM(CASE WHEN s.status = 'warn' THEN 1 ELSE 0 END), 0) AS warn,
                   COALESCE(SUM(CASE WHEN s.status = 'crit' THEN 1 ELSE 0 END), 0) AS crit,
                   COALESCE(SUM(CASE WHEN s.status = 'ok' THEN 1 ELSE 0 END), 0) AS normal
            FROM fiber_links l LEFT JOIN optical_link_state s ON s.link_id = l.id
            """
        ).fetchone()
        states = get_conn().execute("SELECT metrics FROM optical_link_state").fetchall()
    max_att = None
    for state in states:
        try:
            metrics = json.loads(state["metrics"] or "{}")
        except json.JSONDecodeError:
            continue
        for key in ("attenuation_ab_db", "attenuation_ba_db"):
            value = metrics.get(key)
            if value is not None and (max_att is None or value > max_att):
                max_att = value
    avg_rx = row["avg_rx"]
    return {
        "ports_total": row["total"] or 0,
        "ports_with_module": row["module_present"] or 0,
        "ports_ddm": row["ddm_supported"] or 0,
        "links_total": links["total"] or 0,
        "links_normal": links["normal"] or 0,
        "links_warn": links["warn"] or 0,
        "links_crit": links["crit"] or 0,
        "avg_rx_dbm": round(avg_rx, 2) if avg_rx is not None else None,
        "max_attenuation_db": round(max_att, 2) if max_att is not None else None,
    }

def list_fiber_measurements(link_id, limit=200):
    limit = min(500, max(1, int(limit or 200)))
    with _lock:
        rows = get_conn().execute(
            "SELECT * FROM fiber_measurements WHERE link_id = ?"
            " ORDER BY measured_at DESC, id DESC LIMIT ?",
            (link_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]

def get_fiber_measurement(measurement_id):
    with _lock:
        row = get_conn().execute("SELECT * FROM fiber_measurements WHERE id = ?", (measurement_id,)).fetchone()
    return dict(row) if row else None

def create_fiber_measurement(data):
    now = _now()
    with transaction() as conn:
        cursor = conn.execute(
            """
            INSERT INTO fiber_measurements
                (link_id, measured_at, technician, point, method, value_db, value_dbm, notes, created_at, created_by)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                data["link_id"], data["measured_at"], data.get("technician", ""),
                data.get("point", ""), data.get("method", "otdr"),
                data.get("value_db"), data.get("value_dbm"),
                data.get("notes", ""), now, data.get("created_by", "-"),
            ),
        )
        return cursor.lastrowid

def update_fiber_measurement(measurement_id, data):
    with transaction() as conn:
        cursor = conn.execute(
            """
            UPDATE fiber_measurements SET
                measured_at=?, technician=?, point=?, method=?, value_db=?, value_dbm=?, notes=?
            WHERE id=?
            """,
            (
                data["measured_at"], data.get("technician", ""),
                data.get("point", ""), data.get("method", "otdr"),
                data.get("value_db"), data.get("value_dbm"),
                data.get("notes", ""), measurement_id,
            ),
        )
        return cursor.rowcount > 0

def delete_fiber_measurement(measurement_id):
    with transaction() as conn:
        cursor = conn.execute("DELETE FROM fiber_measurements WHERE id = ?", (measurement_id,))
        return cursor.rowcount > 0

