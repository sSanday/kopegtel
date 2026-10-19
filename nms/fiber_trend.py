"""nms.fiber_trend — prediksi tren Rx per ONT (Theil-Sen) dan ETA warn/crit.

Data masuk berupa rata-rata Rx per hari (dari fiber_history), berupa list
(tanggal 'YYYY-MM-DD', rata-rata, jumlah sampel). Fungsi murni
``compute_trend_from_points`` tidak menyentuh DB agar mudah dites; lapisan
``load_daily_points`` dan ``compute_all_trends`` memakai wrapper ``get_db``
sehingga berjalan di SQLite maupun PostgreSQL.

Catatan desain:
- ONT dengan ``source="simulator"`` nilainya random walk, jadi TIDAK NYATA.
  Hasil perhitungan tetap disimpan dengan flag ``simulated=True`` dan tidak
  boleh memicu peringatan.
- Lompatan mendadak (mis. konektor dibersihkan / ONT diganti) memotong
  history: hanya data SETELAH lompatan terakhir yang dipakai.
"""

from datetime import datetime, timedelta

from nms.db import get_db, get_setting
from nms.fiber import _th_for_ont
from nms.thread_safe import ThreadSafeDict

# Default & rentang (sumber kebenaran juga untuk validation API di app.py).
FIBER_TREND_ENABLED_DEFAULT = 1
FIBER_TREND_WINDOW_DAYS = 14
FIBER_TREND_WINDOW_MIN = 7
FIBER_TREND_WINDOW_MAX = 30
FIBER_TREND_MIN_SLOPE = 0.05
FIBER_TREND_MIN_SLOPE_MIN = 0.01
FIBER_TREND_MIN_SLOPE_MAX = 1.0
FIBER_TREND_HORIZON_DAYS = 14
FIBER_TREND_HORIZON_MIN = 3
FIBER_TREND_HORIZON_MAX = 60

# Konstanta metode (tidak bisa diubah via settings).
TREND_MIN_DAY_SAMPLES = 20  # minimum sampel per hari agar hari dianggap valid
TREND_MIN_DAYS = 5  # minimum hari terpakai agar analisis dijalankan
TREND_JUMP_DB = 2.0  # selisih rata-rata 2 hari berurutan = lompatan
TREND_ETA_CAP_DAYS = 90  # ETA di atas ini dirangkum menjadi "lebih dari 90 hari"
TREND_ETA_FLAG_OVER_CAP = "lebih dari 90 hari"

# Cache hasil komputasi: ont_id -> dict hasil.
fiber_trend_memory = ThreadSafeDict()
# Dedupe peringatan: (ont_id, "warn"|"crit") -> time.time() tekan terakhir.
fiber_trend_alert_mem = ThreadSafeDict()
FIBER_TREND_DEDUPE_S = 7 * 86400  # maksimal satu alert per ONT per level per 7 hari


def fiber_trend_enabled():
    """True bila fitur aktif (bisa dimatikan via settings)."""
    try:
        return int(get_setting("fiber_trend_enabled", FIBER_TREND_ENABLED_DEFAULT)) == 1
    except (ValueError, TypeError):
        return True


def _trend_settings():
    """(window_days, min_slope, horizon_days) di-clamp ke rentang valid."""
    try:
        window = int(
            float(get_setting("fiber_trend_window_days", FIBER_TREND_WINDOW_DAYS))
        )
    except (ValueError, TypeError):
        window = FIBER_TREND_WINDOW_DAYS
    try:
        min_slope = float(get_setting("fiber_trend_min_slope", FIBER_TREND_MIN_SLOPE))
    except (ValueError, TypeError):
        min_slope = FIBER_TREND_MIN_SLOPE
    try:
        horizon = int(
            float(get_setting("fiber_trend_horizon_days", FIBER_TREND_HORIZON_DAYS))
        )
    except (ValueError, TypeError):
        horizon = FIBER_TREND_HORIZON_DAYS
    return (
        min(FIBER_TREND_WINDOW_MAX, max(FIBER_TREND_WINDOW_MIN, window)),
        min(FIBER_TREND_MIN_SLOPE_MAX, max(FIBER_TREND_MIN_SLOPE_MIN, min_slope)),
        min(FIBER_TREND_HORIZON_MAX, max(FIBER_TREND_HORIZON_MIN, horizon)),
    )


def _quantile(sorted_vals, q):
    """Kuantil q (0..1) interpolasi linear; sorted_vals harus terurut."""
    n = len(sorted_vals)
    if n == 0:
        return None
    if n == 1:
        return float(sorted_vals[0])
    pos = q * (n - 1)
    lo = int(pos)
    hi = min(lo + 1, n - 1)
    frac = pos - lo
    return float(sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * frac)


def _ts_fit(pts):
    """Theil-Sen pada [(x_int, y_float)] -> (slope, intercept, q25, q75)."""
    slopes = []
    n = len(pts)
    for i in range(n):
        xi, yi = pts[i]
        for j in range(i + 1, n):
            xj, yj = pts[j]
            dx = xj - xi
            if dx > 0:
                slopes.append((yj - yi) / dx)
    if not slopes:
        # Semua titik di hari yang sama (tidak mungkin setelah filter normalisasi).
        slopes = [0.0]
    slopes.sort()
    slope = _quantile(slopes, 0.5)
    intercept = _quantile(sorted(y - slope * x for x, y in pts), 0.5)
    q25 = _quantile(slopes, 0.25)
    q75 = _quantile(slopes, 0.75)
    return slope, intercept, q25, q75


def _eta_days(rx_now, level, slope):
    """Estimasi (hari, over_cap) sampai Rx=slope*day menyentuh level.

    Kembalikan (None, False) bila tidak relevan: slope >= 0 (tidak turun)
    atau rx_now sudah <= level (sudah tersentuh/lewati). Nilai di atas
    FIBER_TREND_ETA_CAP_DAYS dikemas (None, True).
    """
    if rx_now is None or level is None or slope is None:
        return None, False
    if slope >= 0:
        return None, False
    if rx_now <= level:
        return None, False
    days = (rx_now - level) / (-slope)
    if days > TREND_ETA_CAP_DAYS:
        return None, True
    return days, False


def _blank_result(n_days=0, window_days=0, reason=None, simulated=False):
    """Kerangka hasil untuk status tanpa analisis."""
    return {
        "status": "insufficient_data",
        "reason": reason,
        "slope_db_per_day": None,
        "rx_now": None,
        "n_days": n_days,
        "window_days": window_days,
        "eta_warn_days": None,
        "eta_warn_min_days": None,
        "eta_warn_max_days": None,
        "eta_warn_range": None,
        "eta_warn_at": None,
        "eta_warn_flag": None,
        "eta_crit_days": None,
        "eta_crit_min_days": None,
        "eta_crit_max_days": None,
        "eta_crit_range": None,
        "eta_crit_at": None,
        "eta_crit_flag": None,
        "confidence": "rendah",
        "simulated": bool(simulated),
    }


def compute_trend_from_points(points, th, now=None, settings=None):
    """Prediksi tren Rx dari rata-rata per hari (fungsi murni, tanpa DB).

    points : list (tanggal 'YYYY-MM-DD', rata-rata Rx dBm, jumlah sampel)
    th     : dict threshold (kunci "warn" dan "crit" wajib)
    now    : datetime acuan untuk ETA (default: sekarang)
    settings : {"min_slope": float, "simulated": bool} (opsional)

    Hari dengan sampel < 20 dibuang. Kurang dari 5 hari ->
    status "insufficient_data". Lompatan >= 2.0 dB antar hari berurutan
    memotong analisis: hanya data setelah lompatan terakhir dipakai.
    Slope pakai Theil-Sen (median kemiringan semua pasangan); intersep =
    median(y - slope*x); Rx terkini = nilai garis pada hari terakhir.
    Kuartil Q25/Q75 dari kemiringan dipakai sebagai rentang ETA.
    """
    settings = dict(settings or {})
    min_slope = settings.get("min_slope", FIBER_TREND_MIN_SLOPE)
    try:
        min_slope = float(min_slope)
    except (ValueError, TypeError):
        min_slope = FIBER_TREND_MIN_SLOPE
    simulated = bool(settings.get("simulated", False))
    now = now or datetime.now()

    # Normalisasi: buang hari tanpa sampel cukup, gabung duplikat tanggal
    # (nilai terakhir menang), urutkan naik.
    by_date = {}
    for point in points or []:
        try:
            day, rx, nsamp = point[0], point[1], point[2]
            rx = float(rx)
        except (ValueError, TypeError, IndexError):
            continue
        try:
            d = datetime.strptime(str(day).strip(), "%Y-%m-%d").date()
        except ValueError:
            continue
        try:
            n = int(nsamp)
        except (ValueError, TypeError):
            n = 0
        if n < TREND_MIN_DAY_SAMPLES:
            continue
        by_date[d] = rx  # kemunculan belakangan menggantikan
    clean = sorted(by_date.items())
    n_days = len(clean)
    if n_days < TREND_MIN_DAYS:
        return _blank_result(n_days=n_days, simulated=simulated)

    # Deteksi lompatan mendadak antara dua hari berurutan (>= 2.0 dB).
    jump_idx = None
    for i in range(1, n_days):
        if clean[i][1] - clean[i - 1][1] >= TREND_JUMP_DB:
            jump_idx = i - 1
    if jump_idx is not None:
        # Pakai hanya data SETELAH lompatan terakhir.
        clean = clean[jump_idx + 1 :]
        n_days = len(clean)
        if n_days < TREND_MIN_DAYS:
            return _blank_result(
                n_days=n_days, reason="perubahan mendadak", simulated=simulated
            )

    first_day = clean[0][0]
    pts = [((d - first_day).days, rx) for d, rx in clean]
    slope, intercept, q25, q75 = _ts_fit(pts)
    x_last = pts[-1][0]
    rx_now = slope * x_last + intercept
    window_days = x_last + 1

    if slope <= -min_slope:
        status = "degrading"
    elif slope >= min_slope:
        status = "improving"
    else:
        status = "stable"

    conf = "rendah"
    if n_days >= 10 and (q75 is not None and q75 < 0):
        conf = "tinggi"
    elif n_days >= 7 and (slope is not None and slope < 0):
        conf = "sedang"

    result = _blank_result(n_days=n_days, window_days=window_days, simulated=simulated)

    def _pack_eta(prefix, level):
        days, over_cap = _eta_days(
            rx_now if status == "degrading" else None, level, slope
        )
        if days is None:
            at = None
        else:
            at = (now + timedelta(days=days)).strftime("%Y-%m-%d")
        # Rentang dari kuartil kemiringan (Q25 lebih cepat, Q75 lebih lambat).
        dmin, dmax, flag = None, None, over_cap or None
        if days is not None:
            dmin, _over_min = _eta_days(rx_now, level, q25)
            dmax, _over_max = _eta_days(rx_now, level, q75)
            dmin = round(dmin, 1) if dmin is not None else None
            dmax = round(dmax, 1) if dmax is not None else None
        result[f"eta_{prefix}_days"] = round(days, 1) if days is not None else None
        result[f"eta_{prefix}_min_days"] = dmin
        result[f"eta_{prefix}_max_days"] = dmax
        result[f"eta_{prefix}_range"] = (
            {"min": dmin, "max": dmax} if days is not None else None
        )
        result[f"eta_{prefix}_at"] = at
        result[f"eta_{prefix}_flag"] = (
            TREND_ETA_FLAG_OVER_CAP if (days is None and over_cap) else None
        )

    result["status"] = status
    result["slope_db_per_day"] = round(slope, 4)
    result["rx_now"] = round(rx_now, 2)
    result["confidence"] = conf
    _pack_eta("warn", th.get("warn"))
    _pack_eta("crit", th.get("crit"))
    return result


def load_daily_points(ont_ids, days):
    """Rata-rata Rx per hari untuk banyak ONT (Satu query agregat).

    Kembalikan {ont_id: [(tanggal 'YYYY-MM-DD', rata_rata, jumlah), ...]}
    terurut naik per tanggal. Kompatibel SQLite dan PostgreSQL (substr,
    AVG, COUNT, GROUP BY — semua SQL standar).
    """
    ids = sorted({int(i) for i in (ont_ids or []) if i is not None})
    if not ids:
        return {}
    try:
        days = min(60, max(3, int(days)))
    except (ValueError, TypeError):
        days = FIBER_TREND_WINDOW_DAYS
    start = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    placeholders = ", ".join("?" for _ in ids)
    conn, c = get_db()
    try:
        c.execute(
            "SELECT ont_id, substr(timestamp, 1, 10) AS d, "
            "AVG(rx_power) AS avg_rx, COUNT(*) AS n "
            f"FROM fiber_history WHERE ont_id IN ({placeholders}) "
            "AND timestamp >= ? AND rx_power IS NOT NULL "
            "GROUP BY ont_id, d ORDER BY ont_id, d",
            (*ids, start),
        )
        rows = [dict(r) for r in c.fetchall()]
    except Exception:
        rows = []
    finally:
        try:
            conn.close()
        except Exception:
            pass
    out = {i: [] for i in ids}
    for r in rows:
        try:
            fid = int(r["ont_id"])
            avg = float(r["avg_rx"])
            n = int(r["n"])
        except (ValueError, TypeError, KeyError):
            continue
        if r["d"]:
            out[fid].append((str(r["d"]), avg, n))
    return out


def compute_all_trends():
    """Hitung trend semua ONT + simpan di fiber_trend_memory.

    Identitas ONT (sn, pelanggan, OLT/ODP, source, threshold warn/crit)
    ikut disimpan agar endpoint summary tidak perlu query ulang.
    """
    conn, c = get_db()
    try:
        c.execute("SELECT * FROM fiber_onts ORDER BY id ASC")
        rows = [dict(r) for r in c.fetchall()]
    except Exception:
        rows = []
    finally:
        try:
            conn.close()
        except Exception:
            pass
    window, min_slope, _horizon = _trend_settings()
    points_map = load_daily_points([o["id"] for o in rows], window)
    for o in rows:
        fid = o["id"]
        src = (o.get("source") or "manual").strip().lower()
        simulated = src == "simulator"
        try:
            th = _th_for_ont(o)
        except Exception:
            th = {"warn": None, "crit": None}
        try:
            res = compute_trend_from_points(
                points_map.get(fid, []),
                th,
                settings={"min_slope": min_slope, "simulated": simulated},
            )
        except Exception as e:
            print(f"[FIBER-TREND] hitung {fid} gagal: {e}")
            res = _blank_result(simulated=simulated)
        res.update(
            {
                "ont_id": fid,
                "ont_sn": o.get("ont_sn"),
                "customer": o.get("customer"),
                "olt_name": o.get("olt_name"),
                "odp_name": o.get("odp_name"),
                "source": src,
                "warn": th.get("warn"),
                "crit": th.get("crit"),
                "computed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
        )
        fiber_trend_memory[fid] = res
    return dict(fiber_trend_memory.items())
