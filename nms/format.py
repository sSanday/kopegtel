"""nms.format — helper format teks dan validasi koordinat."""


def _fmt_age(age_min):
    try:
        m = float(age_min)
    except (ValueError, TypeError):
        return "?"
    if m < 1:
        return "baru saja"
    if m < 60:
        return f"{int(m)} mnt"
    if m < 60 * 48:
        return f"{int(m // 60)} jam"
    d = m / (60 * 24)
    return f"{int(d)} hari" if d >= 10 else f"{round(d, 1)} hari"


def _fmt_duration(s):
    if s is None:
        return "Ongoing"
    try:
        s = int(s)
    except (ValueError, TypeError):
        return "—"
    m, sec = divmod(s, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {sec}s"
    return f"{sec}s"


def _fmt_uptime(s):
    if s is None:
        return None
    try:
        s = int(s)
    except (ValueError, TypeError):
        return None
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, _s = divmod(s, 60)
    if d:
        return f"{d}h {h}j"
    if h:
        return f"{h}j {m}m"
    return f"{m}m"


def _csv_safe(v):
    if v is None or isinstance(v, (int, float)):
        return v
    s = str(v).replace("\r", " ").replace("\n", " ")
    if s[:1] in ("=", "+", "-", "@", "|", "%") or s[:1] in ("\t",):
        return "'" + s
    return s


def _parse_latlon(d):
    """Koordinat opsional: (lat, lon) atau (None, None). Kembalikan
    (lat, lon, err); pasangan tak lengkap / di luar rentang = err."""
    lat_raw, lon_raw = d.get("lat", None), d.get("lon", None)
    if lat_raw in (None, "") and lon_raw in (None, ""):
        return None, None, None
    try:
        lat = float(lat_raw)
        lon = float(lon_raw)
    except (ValueError, TypeError):
        return None, None, "lat/lon harus angka desimal"
    if not -90 <= lat <= 90:
        return None, None, "lat harus -90..90"
    if not -180 <= lon <= 180:
        return None, None, "lon harus -180..180"
    return round(lat, 6), round(lon, 6), None
