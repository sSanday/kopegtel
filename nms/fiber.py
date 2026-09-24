"""nms.fiber — threshold, status Rx/Tx, dan link budget fiber."""

from nms.db import get_setting

FIBER_RX_OVERLOAD = -8.0
FIBER_RX_WARN = -25.0
FIBER_RX_CRIT = -27.0
FIBER_RX_TARGET = -18.0
FIBER_TX_MIN = 0.0
FIBER_TX_MAX = 5.0
FIBER_DEGRADE_DB = 3.0
FIBER_DEGRADE_DAYS = 7
FIBER_DEGRADE_MIN_SPAN_H = 24
FIBER_STALE_MIN = 60
FIBER_FLAP_FLIPS = 4
FIBER_FLAP_HOURS = 24


def _fiber_thresholds():
    """Baca threshold fiber dari settings (bisa diubah via UI)."""
    return {
        "overload": get_setting("fiber_rx_overload", FIBER_RX_OVERLOAD),
        "warn": get_setting("fiber_rx_warn", FIBER_RX_WARN),
        "crit": get_setting("fiber_rx_crit", FIBER_RX_CRIT),
        "target": get_setting("fiber_rx_target", FIBER_RX_TARGET),
        "tx_min": get_setting("fiber_tx_min", FIBER_TX_MIN),
        "tx_max": get_setting("fiber_tx_max", FIBER_TX_MAX),
    }


def _th_for_ont(o):
    """Threshold untuk satu ONT: global, dioverride rx_warn/rx_crit bila diisi.

    o: dict baris fiber_onts (boleh hanya berisi rx_warn/rx_crit).
    """
    th = _fiber_thresholds()
    try:
        w = o.get("rx_warn")
        w = None if w in (None, "") else float(w)
    except (ValueError, TypeError):
        w = None
    try:
        cr = o.get("rx_crit")
        cr = None if cr in (None, "") else float(cr)
    except (ValueError, TypeError):
        cr = None
    if w is not None:
        th["warn"] = w
    if cr is not None:
        th["crit"] = cr
    if not (th["crit"] < th["warn"] <= th["overload"]):
        return _fiber_thresholds()
    return th


def fiber_status_for_rx(rx, th=None):
    """Kembalikan (status, severity, saran, butuh_peredam_db).

    status: normal|warning|critical|overload|unknown
    severity: None|warning|high|disaster (mapping ke triggers)
    """
    th = th or _fiber_thresholds()
    if rx is None:
        return "unknown", None, "Belum ada data pengukuran.", 0
    try:
        rx = float(rx)
    except (ValueError, TypeError):
        return "unknown", None, "Data Rx tidak valid.", 0
    if rx > th["overload"]:
        need = round(max(rx - th["target"], 0.0), 1)
        if need <= 5:
            rec = "5dB"
        elif need <= 10:
            rec = "10dB"
        else:
            rec = "15dB"
        return (
            "overload",
            "high",
            f"Rx overload ({rx} dBm). Pasang PEREDAM/attenuator {rec} "
            f"(kebutuhan hitung ±{need} dB agar ke ~{th['target']} dBm).",
            need,
        )
    if rx < th["crit"]:
        return (
            "critical",
            "disaster",
            f"Redaman tinggi! Rx {rx} dBm (< {th['crit']} dBm). "
            "Cek bending, konektor kotor, splicing, ODP/ODC.",
            0,
        )
    if rx < th["warn"]:
        return (
            "warning",
            "warning",
            f"Rx {rx} dBm mendekati batas ({th['warn']} dBm). "
            "Jadwalkan cek jalur fiber.",
            0,
        )
    return ("normal", None, f"Rx {rx} dBm normal.", 0)


def fiber_status_for_tx(tx, th=None):
    """Kembalikan (status, severity, saran). status: tx_ok|tx_abnormal|tx_unknown."""
    th = th or _fiber_thresholds()
    if tx is None:
        return "tx_unknown", None, ""
    try:
        tx = float(tx)
    except (ValueError, TypeError):
        return "tx_unknown", None, ""
    if tx < th["tx_min"] or tx > th["tx_max"]:
        if tx <= -5:
            return (
                "tx_abnormal",
                "high",
                f"Tx {tx} dBm di luar batas ({th['tx_min']}..{th['tx_max']} dBm). "
                "Laser ONT kemungkinan mati/hang — cek ONT.",
            )
        return (
            "tx_abnormal",
            "warning",
            f"Tx {tx} dBm di luar batas normal ({th['tx_min']}..{th['tx_max']} dBm). "
            "Cek ONT/SFP.",
        )
    return "tx_ok", None, ""


_SEV_RANK = {"warning": 1, "high": 2, "disaster": 3}


def fiber_eval(rx, tx=None, th=None):
    """Gabungan Rx+Tx: kembalikan (status, severity, advice, need_peredam_db).

    status terburuk yang menang (critical > overload > warning > normal > unknown).
    """
    th = th or _fiber_thresholds()
    r_status, r_sev, r_adv, need = fiber_status_for_rx(rx, th)
    t_status, t_sev, t_adv = fiber_status_for_tx(tx, th)
    if r_status == "unknown":
        if t_sev:
            st = "warning" if t_sev == "warning" else "critical"
            return st, t_sev, t_adv, 0
        return "unknown", None, r_adv, 0
    if not t_sev:
        return r_status, r_sev, r_adv, need
    advice = (r_adv + " " + t_adv).strip() if t_adv else r_adv
    if _SEV_RANK.get(t_sev, 0) > _SEV_RANK.get(r_sev or "", 0):
        st = "warning" if t_sev == "warning" else "critical"
        return st, t_sev, advice, need
    return r_status, r_sev, advice, need


FIBER_SPLITTER_LOSS = {"1:2": 3.5, "1:4": 7.2, "1:8": 10.5, "1:16": 13.8, "1:32": 17.1}
FIBER_PER_KM_DB = 0.35
FIBER_CONNECTOR_DB = 0.5
FIBER_SPLICE_DB = 0.1
FIBER_BUDGET_TOLERANCE_DB = 3.0


def fiber_link_budget(tx_dbm, splitters, fiber_km, connectors, splices, margin_db=0.0):
    """Hitung Rx teoritis. Kembalikan dict rincian (stateless, tanpa DB).

    splitters: list seperti ["1:4", "1:8"] (ODC lalu ODP).
    """
    splitter_db = round(sum(FIBER_SPLITTER_LOSS[s] for s in splitters), 2)
    fiber_db = round(fiber_km * FIBER_PER_KM_DB, 2)
    conn_db = round(connectors * FIBER_CONNECTOR_DB, 2)
    splice_db = round(splices * FIBER_SPLICE_DB, 2)
    total = round(splitter_db + fiber_db + conn_db + splice_db + margin_db, 2)
    return {
        "tx_dbm": round(tx_dbm, 2),
        "losses": {
            "splitter_db": splitter_db,
            "fiber_db": fiber_db,
            "connector_db": conn_db,
            "splice_db": splice_db,
            "margin_db": round(margin_db, 2),
            "total_db": total,
        },
        "expected_rx": round(tx_dbm - total, 2),
    }


def fiber_budget_verdict(expected_rx, actual_rx):
    """Bandingkan Rx teori vs aktual. Kembalikan (verdict, severity, saran)."""
    if actual_rx is None:
        return (
            "no_data",
            None,
            "Pilih ONT yang sudah ada pengukuran Rx untuk pembanding.",
        )
    delta = round(actual_rx - expected_rx, 2)
    if delta <= -FIBER_BUDGET_TOLERANCE_DB:
        return (
            "over_budget",
            "high",
            f"Redaman berlebih {abs(delta)} dB dari budget! "
            "Cek bending, konektor kotor, splice ulang, atau ODP basah/rusak.",
        )
    if delta >= FIBER_BUDGET_TOLERANCE_DB:
        return (
            "under_budget",
            "warning",
            f"Rx aktual {abs(delta)} dB lebih bagus dari teori. "
            "Cek ulang input budget (mungkin splitter/jarak salah catat).",
        )
    return (
        "ok",
        None,
        f"Selisih {delta} dB masih dalam toleransi ±{FIBER_BUDGET_TOLERANCE_DB} dB. Jalur sehat.",
    )
