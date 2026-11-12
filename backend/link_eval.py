"""Evaluasi satu link fiber — fungsi murni, tanpa akses database/perangkat.

Masukan sengaja "sudah disiapkan" (rata-rata pembacaan terakhir, baseline,
hasil tren) agar mudah diuji deterministik. Lapisan db/poller yang
menyiapkan data ini dan menerapkan hasilnya ke tabel problems.
"""

import optical


def fmt_dbm(value):
    return f"{value:.2f} dBm" if value is not None else "—"


def fmt_db(value):
    return f"{value:.2f} dB" if value is not None else "—"


# ---------------------------------------------------------
# Helper pembacaan port
# ---------------------------------------------------------

def port_los(reading):
    """LOS bila flag rx_loss atau Rx ≤ -40 dBm (RFC/practical floor)."""
    return optical.is_los(reading)


def worst_rx(reading):
    if not reading:
        return None
    return reading.get("rx_dbm")


# ---------------------------------------------------------
# Evaluasi lengkap satu link
# ---------------------------------------------------------

def evaluate_link(link, profile, settings, reading_a, reading_b,
                  baseline_rx_a=None, baseline_rx_b=None,
                  trend_rx_a=None, trend_rx_b=None,
                  was_bad=None):
    """Hitung semua besaran link dan deteksi kondisi bermasalah.

    link: dict kolom fiber_links (length_km, wavelength_nm, wavelength_b_nm,
          splices, connectors, splitter_ratio, name, device_a_id ...).
    profile: dict kolom optical_profiles (rx_warn_low, rx_crit_low, ...).
    settings: dict fiber_* yang sudah di-parse (angka/dBm).
    reading_a / reading_b: dict pembacaan RATA-RATA terakhir (None bila ujung tidak terpantau):
        {rx_dbm, tx_dbm, temp_c, voltage_v, bias_ma, rx_loss, tx_fault,
         module_present, ddm_supported, if_name}
    was_bad: set kind yang saat ini terbuka (untuk histeresis pemulihan).
    Kembalikan dict siap dipakai API & poller.
    """
    was_bad = was_bad or set()
    wavelength_a = link.get("wavelength_nm") or 1310
    wavelength_b = link.get("wavelength_b_nm") or wavelength_a  # BiDi: panjang gelombang arah B→A

    alpha_1310 = settings["fiber_alpha_1310"]
    alpha_1550 = settings["fiber_alpha_1550"]
    splice_loss = settings["fiber_splice_loss"]
    connector_loss = settings["fiber_connector_loss"]
    splitter_table = settings["fiber_splitter_losses"]

    budget_a = optical.link_budget_db(
        link.get("length_km"), wavelength_a,
        splices=link.get("splices"), connectors=link.get("connectors"),
        splitter_ratio=link.get("splitter_ratio"),
        alpha_1310=alpha_1310, alpha_1550=alpha_1550,
        splice_loss=splice_loss, connector_loss=connector_loss,
        splitter_table=splitter_table,
    )
    budget_b = optical.link_budget_db(
        link.get("length_km"), wavelength_b,
        splices=link.get("splices"), connectors=link.get("connectors"),
        splitter_ratio=link.get("splitter_ratio"),
        alpha_1310=alpha_1310, alpha_1550=alpha_1550,
        splice_loss=splice_loss, connector_loss=connector_loss,
        splitter_table=splitter_table,
    )

    tx_a = (reading_a or {}).get("tx_dbm")
    rx_a = (reading_a or {}).get("rx_dbm")
    tx_b = (reading_b or {}).get("tx_dbm")
    rx_b = (reading_b or {}).get("rx_dbm")

    att_ab = optical.attenuation_db(tx_a, rx_b)
    att_ba = optical.attenuation_db(tx_b, rx_a)
    asymmetry = optical.asymmetry_db(att_ab, att_ba)

    budget_ab = (budget_a or {}).get("total_db")
    budget_ba = (budget_b or {}).get("total_db")
    dev_ab = optical.deviation_db(att_ab, budget_ab)
    dev_ba = optical.deviation_db(att_ba, budget_ba)

    sensitivity = profile.get("rx_sensitivity_dbm") if profile else None
    margin_a = optical.margin_db(rx_a, sensitivity)
    margin_b = optical.margin_db(rx_b, sensitivity)

    rx_warn_low = profile.get("rx_warn_low") if profile else None
    rx_crit_low = profile.get("rx_crit_low") if profile else None
    rx_warn_high = profile.get("rx_warn_high") if profile else None
    rx_crit_high = profile.get("rx_crit_high") if profile else None
    hyst = settings["fiber_hysteresis_db"]

    # LOS per ujung.
    los_a = port_los(reading_a)
    los_b = port_los(reading_b)
    los_only_a = los_a and not los_b
    los_only_b = los_b and not los_a

    # Level Rx per ujung (histeresis bila sedang bermasalah).
    rx_level_a = optical.hysteresis_level_low(
        rx_a, rx_warn_low, rx_crit_low, hyst, "fiber_rx_low" in was_bad
    )
    rx_level_b = optical.hysteresis_level_low(
        rx_b, rx_warn_low, rx_crit_low, hyst, "fiber_rx_low" in was_bad
    )
    rx_high_level_a = optical.hysteresis_level_high(
        rx_a, rx_warn_high, rx_crit_high, hyst, "fiber_rx_high" in was_bad
    )
    rx_high_level_b = optical.hysteresis_level_high(
        rx_b, rx_warn_high, rx_crit_high, hyst, "fiber_rx_high" in was_bad
    )

    # Margin operasi (makin kecil makin waspada).
    margin_warn = settings["fiber_margin_warn"]
    margin_crit = settings["fiber_margin_crit"]
    margin_level_a = optical.hysteresis_level_low(
        margin_a, margin_warn, margin_crit, hyst, "fiber_margin_low" in was_bad
    )
    margin_level_b = optical.hysteresis_level_low(
        margin_b, margin_warn, margin_crit, hyst, "fiber_margin_low" in was_bad
    )

    # Asimetri (makin besar makin waspada) — hanya bila kedua arah diketahui.
    asym_level = optical.hysteresis_level_high(
        asymmetry, settings["fiber_asymmetry_warn"], None, hyst,
        "fiber_asymmetry" in was_bad,
    )

    # Deviasi terhadap anggaran rancangan.
    dev_warn = settings["fiber_deviation_warn"]
    dev_crit = settings["fiber_deviation_crit"]
    dev_level_ab = optical.hysteresis_level_high(
        dev_ab, dev_warn, dev_crit, hyst, "fiber_deviation" in was_bad
    )
    dev_level_ba = optical.hysteresis_level_high(
        dev_ba, dev_warn, dev_crit, hyst, "fiber_deviation" in was_bad
    )

    # Penurunan mendadak: rata-rata terakhir < baseline (median 7 hari) − drop_db.
    drop_db = settings["fiber_drop_db"]
    drop_a = (
        rx_a is not None and baseline_rx_a is not None
        and rx_a < baseline_rx_a - drop_db
    )
    drop_b = (
        rx_b is not None and baseline_rx_b is not None
        and rx_b < baseline_rx_b - drop_db
    )

    # Tren memburuk: slope per minggu ≤ −ambang (default 0,5 dB/minggu).
    trend_limit = settings["fiber_trend_db_per_week"]
    trend_bad_a = trend_rx_a is not None and trend_rx_a <= -abs(trend_limit)
    trend_bad_b = trend_rx_b is not None and trend_rx_b <= -abs(trend_limit)

    issues = []

    def add(kind, severity, message, port=None, rx=None, redaman=None, device_side=None):
        issues.append(
            {
                "kind": kind,
                "severity": severity,
                "message": message,
                "port": port,
                "rx": rx,
                "redaman": redaman,
                "device_side": device_side,  # "a" | "b" | None (link-wide)
            }
        )

    link_label = link.get("name") or f"Link #{link.get('id')}"

    if los_a or los_b:
        sides = []
        if los_a:
            port_a = (reading_a or {}).get("if_name")
            sides.append(f"port ujung A ({port_a or '—'})")
            add("fiber_los", "high",
                f"{link_label}: LOS pada port ujung A ({port_a or '—'})",
                port=port_a, rx=rx_a, redaman=att_ab, device_side="a")
        if los_b:
            port_b = (reading_b or {}).get("if_name")
            sides.append(f"port ujung B ({port_b or '—'})")
            add("fiber_los", "high",
                f"{link_label}: LOS pada port ujung B ({port_b or '—'})",
                port=port_b, rx=rx_b, redaman=att_ba, device_side="b")

    for side, rx, level in (("A", rx_a, rx_level_a), ("B", rx_b, rx_level_b)):
        if level in ("warn", "crit") and not (los_a if side == "A" else los_b):
            severity = "high" if level == "crit" else "warning"
            reading = reading_a if side == "A" else reading_b
            add("fiber_rx_low", severity,
                f"{link_label}: Rx ujung {side} {fmt_dbm(rx)} melewati ambang rendah profil ({rx_warn_low}/{rx_crit_low} dBm)",
                port=(reading or {}).get("if_name"), rx=rx,
                redaman=att_ab if side == "A" else att_ba, device_side=side.lower())

    for side, rx, level in (("A", rx_a, rx_high_level_a), ("B", rx_b, rx_high_level_b)):
        if level in ("warn", "crit"):
            severity = "high" if level == "crit" else "warning"
            reading = reading_a if side == "A" else reading_b
            add("fiber_rx_high", severity,
                f"{link_label}: Rx ujung {side} {fmt_dbm(rx)} melewati ambang atas profil ({rx_warn_high}/{rx_crit_high} dBm)",
                port=(reading or {}).get("if_name"), rx=rx,
                redaman=att_ab if side == "A" else att_ba, device_side=side.lower())

    for side, margin, level in (("A", margin_a, margin_level_a), ("B", margin_b, margin_level_b)):
        if level in ("warn", "crit"):
            severity = "high" if level == "crit" else "warning"
            add("fiber_margin_low", severity,
                f"{link_label}: margin operasi ujung {side} {fmt_db(margin)} (batas {margin_warn}/{margin_crit} dB)",
                rx=(rx_a if side == "A" else rx_b), device_side=side.lower())

    if asym_level in ("warn", "crit"):
        add("fiber_asymmetry", "average",
            f"{link_label}: asimetri redaman {fmt_db(asymmetry)} (> {settings['fiber_asymmetry_warn']} dB) — indikasi konektor kotor/tekukan satu arah",
            redaman=asymmetry, device_side=None)

    for side, rx, drop in (("A", rx_a, drop_a), ("B", rx_b, drop_b)):
        if drop:
            baseline = baseline_rx_a if side == "A" else baseline_rx_b
            add("fiber_drop", "high",
                f"{link_label}: Rx ujung {side} turun mendadak ke {fmt_dbm(rx)} (baseline {fmt_dbm(baseline)}, turun > {drop_db} dB)",
                rx=rx, device_side=side.lower())

    for side, rx, trend, trend_bad in (("A", rx_a, trend_rx_a, trend_bad_a), ("B", rx_b, trend_rx_b, trend_bad_b)):
        if trend_bad:
            add("fiber_trend", "average",
                f"{link_label}: tren Rx ujung {side} memburuk {abs(trend):.2f} dB/minggu (ambang {trend_limit} dB/minggu)",
                device_side=side.lower())

    # Status link = terburuk dari LOS, Rx, margin, deviasi, asimetri.
    static_levels = [
        "crit" if (los_a or los_b) else None,
        rx_level_a, rx_level_b, rx_high_level_a, rx_high_level_b,
        margin_level_a, margin_level_b,
        dev_level_ab, dev_level_ba,
        asym_level,
    ]
    status = optical.worst_status(static_levels)
    if status is None:
        status = "unknown"  # tidak ada nilai yang bisa dihitung — bukan "normal"

    # Langkah perhitungan untuk UI ("perhitungan langkah demi langkah").
    steps = []
    steps.append(
        {
            "label": f"Redaman A→B = Tx_A ({fmt_dbm(tx_a)}) − Rx_B ({fmt_dbm(rx_b)})",
            "value": fmt_db(att_ab),
            "ok": att_ab is not None,
            "reason": None if att_ab is not None else "Rx ujung B tidak terpantau / tidak ada data",
        }
    )
    steps.append(
        {
            "label": f"Redaman B→A = Tx_B ({fmt_dbm(tx_b)}) − Rx_A ({fmt_dbm(rx_a)})",
            "value": fmt_db(att_ba),
            "ok": att_ba is not None,
            "reason": None if att_ba is not None else "Ujung B bukan perangkat terpantau / tidak ada data",
        }
    )
    steps.append(
        {
            "label": f"Asimetri = |{fmt_db(att_ab)} − {fmt_db(att_ba)}|",
            "value": fmt_db(asymmetry),
            "ok": asymmetry is not None,
            "reason": None if asymmetry is not None else "Butuh redaman kedua arah",
        }
    )
    if budget_a:
        steps.append(
            {
                "label": f"Anggaran rancangan A→B ({wavelength_a} nm) = " + " + ".join(
                    f"{s['value_db']:.2f}" for s in budget_a["steps"]
                ),
                "value": f"{budget_a['total_db']:.2f} dB",
                "ok": True,
                "reason": None,
                "detail_steps": budget_a["steps"],
            }
        )
    if budget_b:
        steps.append(
            {
                "label": f"Anggaran rancangan B→A ({wavelength_b} nm) = " + " + ".join(
                    f"{s['value_db']:.2f}" for s in budget_b["steps"]
                ),
                "value": f"{budget_b['total_db']:.2f} dB",
                "ok": True,
                "reason": None,
                "detail_steps": budget_b["steps"],
            }
        )
    steps.append(
        {
            "label": f"Deviasi A→B = {fmt_db(att_ab)} − {fmt_db(budget_ab)}",
            "value": fmt_db(dev_ab),
            "ok": dev_ab is not None,
            "reason": None if dev_ab is not None else "Anggaran/redaman tidak lengkap",
        }
    )
    steps.append(
        {
            "label": f"Deviasi B→A = {fmt_db(att_ba)} − {fmt_db(budget_ba)}",
            "value": fmt_db(dev_ba),
            "ok": dev_ba is not None,
            "reason": None if dev_ba is not None else "Anggaran/redaman tidak lengkap",
        }
    )
    steps.append(
        {
            "label": f"Margin ujung A = Rx_A ({fmt_dbm(rx_a)}) − sensitivitas ({fmt_dbm(sensitivity)})",
            "value": fmt_db(margin_a),
            "ok": margin_a is not None,
            "reason": None if margin_a is not None else "Rx ujung A atau sensitivitas profil tidak ada",
        }
    )
    steps.append(
        {
            "label": f"Margin ujung B = Rx_B ({fmt_dbm(rx_b)}) − sensitivitas ({fmt_dbm(sensitivity)})",
            "value": fmt_db(margin_b),
            "ok": margin_b is not None,
            "reason": None if margin_b is not None else "Ujung B tidak terpantau / sensitivitas profil tidak ada",
        }
    )

    return {
        "status": status,
        "attenuation_ab_db": att_ab,
        "attenuation_ba_db": att_ba,
        "asymmetry_db": asymmetry,
        "budget_ab_db": budget_ab,
        "budget_ba_db": budget_ba,
        "deviation_ab_db": dev_ab,
        "deviation_ba_db": dev_ba,
        "margin_a_db": margin_a,
        "margin_b_db": margin_b,
        "los_a": bool(los_a),
        "los_b": bool(los_b),
        "issues": issues,
        "steps": steps,
        "wavelength_ab_nm": wavelength_a,
        "wavelength_ba_nm": wavelength_b,
        "signals": {
            "rx_a_dbm": rx_a, "tx_a_dbm": tx_a,
            "rx_b_dbm": rx_b, "tx_b_dbm": tx_b,
        },
    }
