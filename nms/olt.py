"""nms.olt — preset vendor dan konversi optik OLT."""

OLT_VENDOR_PRESETS = {
    "zte": {
        "rx_base": "1.3.6.1.4.1.3902.1012.3.50.12.1.1.10",
        "tx_base": "1.3.6.1.4.1.3902.1012.3.50.12.1.1.14",
        "div": 1.0,
        "scale": 0.002,
        "offset": -30.0,
        "note": "ZTE C300/C320. ont_index = sufiks hasil walk "
        "(<ponIfIndex>.<onuIdx>[.1]), mis. 268501248.5.1. "
        "Tx kolom .14 mengikuti encoding yang sama — verifikasi via tombol Test.",
    },
    "huawei": {
        "rx_base": "1.3.6.1.4.1.2011.6.128.1.1.2.51.1.4",
        "tx_base": "",
        "div": 1.0,
        "scale": 0.01,
        "offset": -100.0,
        "note": "Huawei MA5600T/MA5800 (rumus (raw-10000)/100). "
        "Tx ONT tidak tersedia di tabel ini — kosongkan (pantau Rx saja).",
    },
    "generic": {
        "rx_base": "",
        "tx_base": "",
        "div": 100.0,
        "scale": 1.0,
        "offset": 0.0,
        "note": "",
    },
}


def _olt_transform(olt):
    """(div, scale, offset) dengan default aman untuk baris lama."""
    try:
        div = float(olt.get("div") or 100.0) or 100.0
    except (ValueError, TypeError):
        div = 100.0
    try:
        scale = float(olt.get("scale", 1.0))
    except (ValueError, TypeError):
        scale = 1.0
    if scale == 0:
        scale = 1.0
    try:
        offset = float(olt.get("offset", 0.0))
    except (ValueError, TypeError):
        offset = 0.0
    return div, scale, offset


def _olt_raw_to_dbm(raw, olt):
    """Mentah SNMP -> dBm. None bila tak bisa dikonversi."""
    try:
        div, scale, offset = _olt_transform(olt)
        return round(float(raw) / div * scale + offset, 2)
    except (ValueError, TypeError, ZeroDivisionError):
        return None
