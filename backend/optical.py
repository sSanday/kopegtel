"""Logika murni modul Fiber Optik.

Tidak mengakses database atau perangkat — seluruh fungsi deterministik
dan mudah diuji dengan pytest.

Prinsip: sistem tidak mengukur redaman fiber secara langsung.
Redaman diturunkan dari daya optik transceiver (DDM/DOM) di kedua ujung:
    Redaman A→B = Tx (ujung A) − Rx (ujung B)
    Redaman B→A = Tx (ujung B) − Rx (ujung A)
Nilai yang tidak punya sumber TIDAK PERNAH dikarang — fungsi mengembalikan
None (tampil "—") beserta alasannya di lapisan API.
"""

import math
import re
import statistics

# Batas bawah daya terima: di bawah ini dianggap LOS (Loss of Signal).
LOS_DBM = -40.0

# Panjang gelombang yang dikenal peta α (redaman serat, dB/km).
DEFAULT_WAVELENGTHS = (1310, 1550)

SPLITTER_RATIOS = ("1:2", "1:4", "1:8", "1:16", "1:32", "1:64")


# ---------------------------------------------------------
# Konversi satuan daya optik
# ---------------------------------------------------------

def mw_to_dbm(mw):
    """Miliwatt → dBm (10·log10(mW)). Input tidak valid → None."""
    try:
        value = float(mw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    return 10.0 * math.log10(value)


def uw_to_dbm(uw):
    """Mikrowatt → dBm. Input tidak valid → None."""
    try:
        value = float(uw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    return 10.0 * math.log10(value / 1000.0)


_NUM_RE = re.compile(r"-?(?:\d+(?:[.,]\d+)?|[.,]\d+)(?:e[+-]?\d+)?", re.IGNORECASE)


def parse_power_dbm(raw):
    """Parse daya optik dari string RouterOS / nilai sensor menjadi dBm.

    Format yang ditangani: "-5.21", "-5.21 dBm", "-5,21dBm", "0.31 mW",
    "310 uW", "0.0002 W". Tanpa satuan → dianggap sudah dBm.
    Kembalikan None untuk nilai tak terukur (mis. "-inf", kosong).
    """
    if raw is None:
        return None
    text = str(raw).strip().lower().replace("μ", "u").replace("µ", "u").replace("−", "-")
    if not text:
        return None
    if "inf" in text:  # -inf / inf / nan → tidak terukur
        return None
    match = _NUM_RE.search(text)
    if not match:
        return None
    number = float(match.group(0).replace(",", "."))
    if not math.isfinite(number):
        return None
    # Deteksi satuan tanpa bergantung pada spasi (mis. "0.31mW", "310µW").
    # Urutan penting: mw → uw → w (watt) — angka tanpa satuan dianggap dBm.
    if re.search(r"mw\b", text):
        return mw_to_dbm(number)
    if re.search(r"(?:^|\d)\s*u\s?w\b", text) or re.search(r"\buw\b", text):
        return uw_to_dbm(number)
    if re.search(r"w\b", text):  # Watt
        return mw_to_dbm(number * 1000.0)
    # Sisanya (termasuk "dbm") → angka sudah dBm.
    return number


def parse_temperature_c(raw):
    """"39.84C" / "39.84 °C" / 39.84 → °C, atau None."""
    if raw is None:
        return None
    text = str(raw).strip().lower().replace("−", "-")
    match = _NUM_RE.search(text)
    if not match:
        return None
    value = float(match.group(0).replace(",", "."))
    return value if math.isfinite(value) else None


def parse_voltage_v(raw):
    if raw is None:
        return None
    text = str(raw).strip().lower().replace("−", "-")
    match = _NUM_RE.search(text)
    if not match:
        return None
    value = float(match.group(0).replace(",", "."))
    return value if math.isfinite(value) else None


def parse_bias_ma(raw):
    if raw is None:
        return None
    text = str(raw).strip().lower().replace("−", "-")
    match = _NUM_RE.search(text)
    if not match:
        return None
    value = float(match.group(0).replace(",", "."))
    return value if math.isfinite(value) else None


def parse_length_m(raw):
    """"10km" → 10000, "500m" → 500, "10" → 10 (dianggap meter), atau None."""
    if raw is None:
        return None
    text = str(raw).strip().lower().replace("−", "-")
    match = _NUM_RE.search(text)
    if not match:
        return None
    value = float(match.group(0).replace(",", "."))
    if not math.isfinite(value):
        return None
    if "km" in text:
        return int(round(value * 1000))
    return int(round(value))


def parse_wavelength_nm(raw):
    if raw is None:
        return None
    text = str(raw).strip().lower()
    match = _NUM_RE.search(text)
    if not match:
        return None
    value = float(match.group(0).replace(",", "."))
    return int(round(value)) if math.isfinite(value) else None


def parse_sfp_bool(raw):
    """RouterOS boolean: true/false/yes/no/1/0 → bool."""
    return str(raw).strip().lower() in ("true", "yes", "1")


# ---------------------------------------------------------
# Statistik dasar
# ---------------------------------------------------------

def average(values, n=None):
    """Rata-rata nilai (terakhir n bila n diberikan). None bila kosong."""
    items = [v for v in values if v is not None]
    if n is not None:
        items = items[-n:]
    if not items:
        return None
    return sum(items) / len(items)


def median(values):
    items = [v for v in values if v is not None]
    if not items:
        return None
    return statistics.median(items)


def linear_trend_per_week(points):
    """Regresi linear nilai terhadap waktu → kemiringan dB per minggu.

    points: iterable of (epoch_days: float, value: float).
    Kembalikan None bila titik data < 3 atau rentang waktu nol.
    """
    xs, ys = [], []
    for x, y in points:
        if x is None or y is None:
            continue
        xs.append(float(x))
        ys.append(float(y))
    if len(xs) < 3:
        return None
    x_mean = sum(xs) / len(xs)
    y_mean = sum(ys) / len(ys)
    numerator = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys))
    denominator = sum((x - x_mean) ** 2 for x in xs)
    if denominator <= 0:
        return None
    slope_per_day = numerator / denominator
    return slope_per_day * 7.0


# ---------------------------------------------------------
# Redaman & anggaran link
# ---------------------------------------------------------

def attenuation_db(tx_dbm, rx_dbm):
    """Redaman = Tx − Rx (dB). Butuh kedua nilai → selain itu None."""
    if tx_dbm is None or rx_dbm is None:
        return None
    return round(float(tx_dbm) - float(rx_dbm), 2)


def alpha_for_wavelength(wavelength_nm, alpha_1310, alpha_1550):
    wl = wavelength_nm
    if wl is None:
        return float(alpha_1550)
    try:
        wl = float(wl)
    except (TypeError, ValueError):
        return float(alpha_1550)
    # Serat SM: redaman minimum sekitar 1550 nm; di bawah 1380 dipakai α(1310).
    return float(alpha_1310) if wl < 1380 else float(alpha_1550)


def splitter_loss_db(ratio, table):
    """Redaman splitter dari tabel umum (perbandingan mis. '1:8'). Tidak dikenal → None."""
    if not ratio:
        return 0.0
    key = str(ratio).strip().lower().replace(" ", "")
    if key in ("", "-", "none", "tanpa"):
        return 0.0
    table = table or {}
    if key in table:
        try:
            return float(table[key])
        except (TypeError, ValueError):
            return None
    return None


def link_budget_db(length_km, wavelength_nm, *, splices, connectors, splitter_ratio,
                   alpha_1310, alpha_1550, splice_loss, connector_loss, splitter_table):
    """Anggaran rancangan = panjang × α(λ) + splice × ls + konektor × lc + splitter.

    Kembalikan dict siap tampil (langkah demi langkah) atau None bila input tidak valid.
    """
    try:
        length = float(length_km)
    except (TypeError, ValueError):
        return None
    if length < 0:
        return None
    alpha = alpha_for_wavelength(wavelength_nm, alpha_1310, alpha_1550)
    fiber_loss = length * alpha
    splice_total = max(0, int(splices or 0)) * float(splice_loss)
    connector_total = max(0, int(connectors or 0)) * float(connector_loss)
    splitter = splitter_loss_db(splitter_ratio, splitter_table)
    if splitter is None:
        return None
    total = fiber_loss + splice_total + connector_total + splitter
    steps = [
        {"label": f"Panjang serat ({length:g} km × {alpha:g} dB/km)", "value_db": round(fiber_loss, 2)},
        {"label": f"Splice ({int(splices or 0)} × {float(splice_loss):g} dB)", "value_db": round(splice_total, 2)},
        {"label": f"Konektor ({int(connectors or 0)} × {float(connector_loss):g} dB)", "value_db": round(connector_total, 2)},
        {"label": f"Splitter {splitter_ratio or '—'}", "value_db": round(splitter, 2)},
    ]
    return {
        "total_db": round(total, 2),
        "alpha_db_per_km": alpha,
        "steps": steps,
    }


def margin_db(rx_dbm, sensitivity_dbm):
    """Margin operasi = Rx − sensitivitas penerima. Butuh keduanya → selain itu None."""
    if rx_dbm is None or sensitivity_dbm is None:
        return None
    return round(float(rx_dbm) - float(sensitivity_dbm), 2)


def deviation_db(measured_db, budget_db):
    """Deviasi = redaman terukur − anggaran rancangan."""
    if measured_db is None or budget_db is None:
        return None
    return round(float(measured_db) - float(budget_db), 2)


def asymmetry_db(att_ab, att_ba):
    if att_ab is None or att_ba is None:
        return None
    return round(abs(float(att_ab) - float(att_ba)), 2)


# ---------------------------------------------------------
# Level ambang dengan histeresis
# ---------------------------------------------------------

def hysteresis_level_low(value, warn_low, crit_low, hysteresis, was_bad):
    """Level ambang untuk metrik yang makin kecil makin buruk (Rx, margin).

    Pulih hanya bila membaik melewati warn_low + hysteresis (histeresis 1 dB default).
    """
    if value is None or warn_low is None:
        return None
    value = float(value)
    if was_bad:
        if value >= float(warn_low) + float(hysteresis):
            return "ok"
        if crit_low is not None and value < float(crit_low):
            return "crit"
        return "warn"
    if crit_low is not None and value < float(crit_low):
        return "crit"
    if value < float(warn_low):
        return "warn"
    return "ok"


def hysteresis_level_high(value, warn_high, crit_high, hysteresis, was_bad):
    """Level ambang untuk metrik yang makin besar makin buruk (asimetri)."""
    if value is None or warn_high is None:
        return None
    value = float(value)
    if was_bad:
        if value <= float(warn_high) - float(hysteresis):
            return "ok"
        if crit_high is not None and value > float(crit_high):
            return "crit"
        return "warn"
    if crit_high is not None and value > float(crit_high):
        return "crit"
    if value > float(warn_high):
        return "warn"
    return "ok"


def worst_status(levels):
    """Gabungkan beberapa level ('ok'|'warn'|'crit'|None) → status terburuk."""
    rank = {"crit": 3, "warn": 2, "ok": 1}
    best = "ok"
    seen_ok = False
    for level in levels:
        if level is None:
            continue
        seen_ok = True
        if rank.get(level, 0) > rank.get(best, 0):
            best = level
    return best if seen_ok else None


def is_los(reading):
    """LOS dari pembacaan: flag rx_loss, atau Rx ≤ -40 dBm, atau nilai -inf (None + module present)."""
    if not reading:
        return False
    if reading.get("rx_loss"):
        return True
    rx = reading.get("rx_dbm")
    if rx is not None and rx <= LOS_DBM:
        return True
    return False
