"""Evaluasi link fiber dari data nyata di database.

Menggabungkan pembacaan DDM (rata-rata 3 terakhir), baseline (median 7 hari),
dan tren (regresi 14 hari) dengan mesin hitung murni di ``link_eval``.
Dipakai oleh collector/poller (untuk membuka/menutup masalah) dan oleh API
(untuk tampilan) — satu jalur perhitungan, tanpa duplikasi rumus.
"""

from datetime import datetime, timedelta, timezone

import db
import optical
import link_eval

FIBER_PROBLEM_KINDS = (
    "fiber_los",
    "fiber_rx_low",
    "fiber_rx_high",
    "fiber_margin_low",
    "fiber_asymmetry",
    "fiber_drop",
    "fiber_trend",
    "fiber_deviation",
    "fiber_predicted",
)

# Sehingga status bisa di-cache di optical_link_state dan /api/summary murah.
LINK_STATE_METRICS_KEYS = (
    "attenuation_ab_db", "attenuation_ba_db", "asymmetry_db",
    "budget_ab_db", "budget_ba_db",
    "deviation_ab_db", "deviation_ba_db",
    "margin_a_db", "margin_b_db",
    "rx_a_dbm", "rx_b_dbm",
    "predicted_days_a", "predicted_days_b",
    "predicted_target_a", "predicted_target_b",
    "predicted_threshold_a", "predicted_threshold_b",
    "predicted_slope_a", "predicted_slope_b",
)


def _since(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:00:00Z")


def average_readings(device_id, if_name, n):
    """Rata-rata nilai N pembacaan terakhir + flag dari pembacaan terbaru."""
    rows = db.recent_optical_readings(device_id, if_name, n)
    if not rows:
        return None
    agg = {
        "if_name": if_name,
        "rx_dbm": optical.average([r.get("rx_dbm") for r in rows]),
        "tx_dbm": optical.average([r.get("tx_dbm") for r in rows]),
        "temp_c": optical.average([r.get("temp_c") for r in rows]),
        "voltage_v": optical.average([r.get("voltage_v") for r in rows]),
        "bias_ma": optical.average([r.get("bias_ma") for r in rows]),
    }
    latest = rows[0]
    agg["rx_loss"] = bool(latest.get("rx_loss"))
    agg["tx_fault"] = bool(latest.get("tx_fault"))
    agg["module_present"] = bool(latest.get("module_present"))
    agg["ddm_supported"] = bool(latest.get("ddm_supported"))
    agg["ts"] = latest.get("ts")
    return agg


def baseline_rx(device_id, if_name, days):
    values = db.optical_hourly_rx_values(device_id, if_name, _since(days))
    if len(values) < 24:  # butuh minimal ~sehari data per jam agar median bermakna
        return None, 0
    return optical.median(values), len(values)


def trend_rx(device_id, if_name, days):
    points = []
    for row in db.optical_hourly_rows(device_id, if_name, _since(days)):
        ts = db.parse_ts(row["hour_ts"])
        if ts is not None and row["rx_avg"] is not None:
            points.append((ts.timestamp() / 86400.0, row["rx_avg"]))
    if len(points) < 24:
        return None
    return optical.linear_trend_per_week(points)


def open_link_kinds(link_id):
    with db._lock:
        rows = db.get_conn().execute(
            "SELECT DISTINCT kind FROM problems WHERE link_id = ? AND status = 'open'",
            (link_id,),
        ).fetchall()
    return {row["kind"] for row in rows}


def resolve_link_problems(link_id, kind):
    now = db._now()
    with db.transaction() as conn:
        cursor = conn.execute(
            "UPDATE problems SET status='resolved', resolved_at=? WHERE link_id=? AND kind=? AND status='open'",
            (now, link_id, kind),
        )
        return cursor.rowcount


def evaluate_link(link, was_bad=None, settings=None):
    """Evaluasi penuh satu link fiber (dict dari get_fiber_link).

    Kembalikan dict link_eval + info baseline/tren untuk transparansi perhitungan.
    """
    settings = settings or db.get_fiber_settings()
    profile = db.get_optical_profile(link["profile_id"]) if link.get("profile_id") else None

    reading_a = average_readings(link["device_a_id"], link["port_a"], settings["fiber_avg_readings"])
    reading_b = (
        average_readings(link["device_b_id"], link["port_b"], settings["fiber_avg_readings"])
        if link.get("device_b_id") and link.get("port_b")
        else None
    )
    baseline_a, baseline_n_a = baseline_rx(link["device_a_id"], link["port_a"], settings["fiber_drop_baseline_days"])
    if link.get("device_b_id") and link.get("port_b"):
        baseline_b, baseline_n_b = baseline_rx(link["device_b_id"], link["port_b"], settings["fiber_drop_baseline_days"])
    else:
        baseline_b, baseline_n_b = None, 0
    trend_a = trend_rx(link["device_a_id"], link["port_a"], settings["fiber_trend_days"])
    trend_b = (
        trend_rx(link["device_b_id"], link["port_b"], settings["fiber_trend_days"])
        if link.get("device_b_id") and link.get("port_b")
        else None
    )

    if was_bad is None:
        was_bad = open_link_kinds(link["id"])

    result = link_eval.evaluate_link(
        link, profile, settings,
        reading_a, reading_b,
        baseline_rx_a=baseline_a, baseline_rx_b=baseline_b,
        trend_rx_a=trend_a, trend_rx_b=trend_b,
        was_bad=was_bad,
    )
    result["baseline_rx_a_dbm"] = baseline_a
    result["baseline_rx_b_dbm"] = baseline_b
    result["baseline_samples_a"] = baseline_n_a
    result["baseline_samples_b"] = baseline_n_b
    result["trend_rx_a_db_per_week"] = trend_a
    result["trend_rx_b_db_per_week"] = trend_b
    result["rx_a_dbm"] = (reading_a or {}).get("rx_dbm") if reading_a else None
    result["rx_b_dbm"] = (reading_b or {}).get("rx_dbm") if reading_b else None
    _preds = result.get("predictions") or {}
    _pa = _preds.get("a")
    _pb = _preds.get("b")
    result["predicted_days_a"] = _pa["days"] if _pa else None
    result["predicted_target_a"] = _pa["target"] if _pa else None
    result["predicted_threshold_a"] = _pa["threshold_dbm"] if _pa else None
    result["predicted_slope_a"] = _pa["slope_db_per_week"] if _pa else None
    result["predicted_days_b"] = _pb["days"] if _pb else None
    result["predicted_target_b"] = _pb["target"] if _pb else None
    result["predicted_threshold_b"] = _pb["threshold_dbm"] if _pb else None
    result["predicted_slope_b"] = _pb["slope_db_per_week"] if _pb else None
    result["profile"] = profile
    result["settings"] = settings
    result["reading_a"] = reading_a
    result["reading_b"] = reading_b
    return result


def public_eval(result):
    """Bagian aman hasil evaluasi untuk API (tanpa objek besar)."""
    return {
        key: result.get(key)
        for key in (
            "status", "attenuation_ab_db", "attenuation_ba_db", "asymmetry_db",
            "budget_ab_db", "budget_ba_db", "deviation_ab_db", "deviation_ba_db",
            "margin_a_db", "margin_b_db", "los_a", "los_b", "issues", "steps",
            "wavelength_ab_nm", "wavelength_ba_nm", "signals",
            "baseline_rx_a_dbm", "baseline_rx_b_dbm",
            "trend_rx_a_db_per_week", "trend_rx_b_db_per_week",
            "predictions",
            "predicted_days_a", "predicted_days_b",
            "predicted_target_a", "predicted_target_b",
            "predicted_threshold_a", "predicted_threshold_b",
            "predicted_slope_a", "predicted_slope_b",
        )
    }


def cached_or_live_metrics(link):
    """Metrik ringkas untuk daftar/kartu: baca cache, fallback hitung sekali."""
    state = db.get_link_state(link["id"])
    if state and state.get("metrics"):
        try:
            import json

            metrics = json.loads(state["metrics"] or "{}")
        except Exception:
            metrics = {}
        if state.get("status"):
            return state["status"], metrics
    result = evaluate_link(link)
    metrics = {k: result.get(k) for k in LINK_STATE_METRICS_KEYS}
    return result["status"], metrics
