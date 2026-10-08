"""nms.power — Power Consumption Analytics (manual + SNMP + smart-meter).

Opsi 4: gabungan input manual dan polling SNMP otomatis.
Energi dihitung dengan integrasi trapezoid antar sampel agar kWh realistis.
"""

from datetime import datetime

from nms.db import db_lock, get_db, get_setting

POWER_DEFAULT_TARIFF = 1500.0  # Rp per kWh (PLN rumah tangga acuan)
POWER_DEFAULT_THRESHOLD = 1000.0  # Watt total
POWER_RETENTION_DAYS = 90


def _to_float_or_none(value):
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def get_power_settings():
    tariff = get_setting("power_tariff_per_kwh", POWER_DEFAULT_TARIFF)
    threshold = get_setting("power_threshold_watts", POWER_DEFAULT_THRESHOLD)
    try:
        tariff = float(tariff)
    except (TypeError, ValueError):
        tariff = POWER_DEFAULT_TARIFF
    try:
        threshold = float(threshold)
    except (TypeError, ValueError):
        threshold = POWER_DEFAULT_THRESHOLD
    return {"tariff_per_kwh": tariff, "threshold_watts": threshold}


def record_power_metric(host, watts=None, voltage=None, current=None, source="manual"):
    host = (host or "").strip()[:255]
    if not host:
        raise ValueError("host wajib diisi")
    watts = _to_float_or_none(watts)
    voltage = _to_float_or_none(voltage)
    current = _to_float_or_none(current)
    if watts is None and voltage is None and current is None:
        raise ValueError("minimal isi watts, voltage, atau current")
    if watts is not None and not -1000000 <= watts <= 1000000:
        raise ValueError("watts di luar batas wajar")
    source = (source or "manual").strip()[:30] or "manual"
    now = _now_str()
    with db_lock:
        conn, c = get_db()
        try:
            c.execute(
                "SELECT watts, timestamp FROM power_metrics WHERE host=? ORDER BY id DESC LIMIT 1",
                (host,),
            )
            prev = c.fetchone()
            energy = 0.0
            if prev and prev["watts"] is not None and watts is not None:
                try:
                    prev_ts = datetime.strptime(prev["timestamp"], "%Y-%m-%d %H:%M:%S")
                    cur_ts = datetime.strptime(now, "%Y-%m-%d %H:%M:%S")
                    elapsed = max(0.0, (cur_ts - prev_ts).total_seconds())
                    # trapezoid, batasi gap 6 jam agar kWh tidak meledak saat data lama
                    elapsed = min(elapsed, 6 * 3600)
                    energy = ((float(prev["watts"]) + float(watts)) / 2.0) * elapsed / 3600000.0
                    energy = max(0.0, energy)
                except (ValueError, TypeError):
                    energy = 0.0
            c.execute(
                "INSERT INTO power_metrics(host, watts, voltage, current, energy_kwh, source, timestamp)"
                " VALUES(?,?,?,?,?,?,?)",
                (host, watts, voltage, current, energy, source, now),
            )
            conn.commit()
        finally:
            conn.close()
    return {"host": host, "watts": watts, "energy_kwh": energy, "timestamp": now, "source": source}


def _bucket_rows(rows, bucket):
    """Agregasi rows (dict timestamp,watts,energy_kwh) ke bucket jam/hari."""
    buckets = {}
    for r in rows:
        try:
            ts = datetime.strptime(r["timestamp"], "%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError, KeyError):
            continue
        if bucket == "day":
            key = ts.strftime("%Y-%m-%d")
        else:
            key = ts.strftime("%Y-%m-%d %H:00")
        b = buckets.setdefault(key, {"label": key, "watts_sum": 0.0, "watts_n": 0, "watts_max": 0.0, "energy_kwh": 0.0})
        w = float(r.get("watts") or 0.0)
        b["watts_sum"] += w
        b["watts_n"] += 1
        b["watts_max"] = max(b["watts_max"], w)
        b["energy_kwh"] += float(r.get("energy_kwh") or 0.0)
    out = []
    for key in sorted(buckets):
        b = buckets[key]
        out.append(
            {
                "label": b["label"],
                "avg_watts": round(b["watts_sum"] / b["watts_n"], 2) if b["watts_n"] else 0.0,
                "peak_watts": round(b["watts_max"], 2),
                "energy_kwh": round(b["energy_kwh"], 4),
            }
        )
    return out


def get_power_summary(hours=24):
    hours = min(max(int(hours), 1), 2160)
    settings = get_power_settings()
    conn, c = get_db()
    try:
        c.execute(
            "SELECT host, watts, voltage, current, energy_kwh, source, timestamp"
            " FROM power_metrics WHERE timestamp >= datetime('now','localtime',?)"
            " ORDER BY timestamp ASC",
            (f"-{hours} hours",),
        )
        rows = [dict(r) for r in c.fetchall()]
        # latest per host
        c.execute(
            "SELECT host, MAX(timestamp) AS ts FROM power_metrics GROUP BY host"
        )
        # ambil latest global per host (tidak dibatasi hours agar perangkat tetap muncul)
        c.execute(
            "SELECT p.host, p.watts, p.voltage, p.current, p.energy_kwh, p.source, p.timestamp"
            " FROM power_metrics p INNER JOIN (SELECT host, MAX(id) AS mid FROM power_metrics GROUP BY host) m"
            " ON p.id = m.mid ORDER BY p.host ASC"
        )
        latest_all = [dict(r) for r in c.fetchall()]
        # rated / threshold per host dari tabel hosts dan power_devices
        try:
            c.execute("SELECT ip, alias, power_rated_watts FROM hosts")
            host_meta = {r["ip"]: dict(r) for r in c.fetchall()}
        except Exception:
            host_meta = {}
        try:
            c.execute("SELECT name, rated_watts, threshold_watts FROM power_devices")
            dev_meta = {r["name"]: dict(r) for r in c.fetchall()}
        except Exception:
            dev_meta = {}
    finally:
        conn.close()

    latest_in_range = {}
    for r in rows:
        latest_in_range[r["host"]] = r

    total_watts = round(sum(float(r.get("watts") or 0.0) for r in latest_all), 2)
    period_energy = round(sum(float(r.get("energy_kwh") or 0.0) for r in rows), 4)
    peak_watts = round(max([float(r.get("watts") or 0.0) for r in rows], default=0.0), 2)
    cost = round(period_energy * float(settings["tariff_per_kwh"]), 2)

    # per-device dengan estimasi biaya & status threshold
    devices = []
    for r in latest_all:
        name = r["host"]
        rated = None
        thr = None
        if name in host_meta and host_meta[name].get("power_rated_watts") is not None:
            try:
                rated = float(host_meta[name]["power_rated_watts"])
            except (TypeError, ValueError):
                rated = None
        if name in dev_meta:
            try:
                if dev_meta[name].get("rated_watts") is not None:
                    rated = float(dev_meta[name]["rated_watts"])
                if dev_meta[name].get("threshold_watts") is not None:
                    thr = float(dev_meta[name]["threshold_watts"])
            except (TypeError, ValueError):
                pass
        w = float(r.get("watts") or 0.0)
        limit = thr if thr is not None else None
        devices.append(
            {
                "host": name,
                "alias": (host_meta.get(name, {}).get("alias") or ""),
                "watts": r.get("watts"),
                "voltage": r.get("voltage"),
                "current": r.get("current"),
                "source": r.get("source"),
                "timestamp": r.get("timestamp"),
                "rated_watts": rated,
                "threshold_watts": limit,
                "over_threshold": bool(limit is not None and w > limit),
            }
        )
    devices.sort(key=lambda d: float(d.get("watts") or 0.0), reverse=True)

    bucket = "day" if hours > 72 else "hour"
    trend = _bucket_rows(rows, bucket)
    over_total = total_watts > float(settings["threshold_watts"])

    return {
        "hours": hours,
        "bucket": bucket,
        "tariff_per_kwh": settings["tariff_per_kwh"],
        "threshold_watts": settings["threshold_watts"],
        "total_watts": total_watts,
        "period_energy_kwh": period_energy,
        "period_cost": cost,
        "peak_watts": peak_watts,
        "device_count": len(latest_all),
        "over_threshold": over_total,
        "devices": devices,
        "latest": list(latest_in_range.values()),
        "trend": trend,
        "history": rows[-2000:],
    }


def get_power_history(host=None, hours=24, bucket="raw"):
    hours = min(max(int(hours), 1), 2160)
    conn, c = get_db()
    try:
        if host:
            c.execute(
                "SELECT host, watts, voltage, current, energy_kwh, source, timestamp"
                " FROM power_metrics WHERE host=? AND timestamp >= datetime('now','localtime',?)"
                " ORDER BY timestamp ASC LIMIT 5000",
                (host, f"-{hours} hours"),
            )
        else:
            c.execute(
                "SELECT host, watts, voltage, current, energy_kwh, source, timestamp"
                " FROM power_metrics WHERE timestamp >= datetime('now','localtime',?)"
                " ORDER BY timestamp ASC LIMIT 5000",
                (f"-{hours} hours",),
            )
        rows = [dict(r) for r in c.fetchall()]
    finally:
        conn.close()
    if bucket in ("hour", "day"):
        return _bucket_rows(rows, bucket)
    return rows


def poll_power_snmp():
    """Polling SNMP otomatis untuk semua host yang punya power_oid."""
    try:
        from nms.snmp import _snmp_get, _valid_oid
    except Exception as e:
        print(f"[POWER-SNMP] import gagal: {e}")
        return {"polled": 0, "ok": 0, "error": str(e)}
    try:
        conn, c = get_db()
        try:
            c.execute(
                "SELECT ip, snmp_community, power_oid, power_scale FROM hosts"
                " WHERE power_oid IS NOT NULL AND power_oid != ''"
            )
            targets = [dict(r) for r in c.fetchall()]
        finally:
            conn.close()
    except Exception as e:
        print(f"[POWER-SNMP] load targets gagal: {e}")
        return {"polled": 0, "ok": 0, "error": str(e)}
    polled = ok = 0
    for t in targets:
        oid = (t.get("power_oid") or "").strip()
        if not _valid_oid(oid):
            continue
        polled += 1
        try:
            vals = _snmp_get(t["ip"], t.get("snmp_community") or "", [oid], timeout=3.0)
        except Exception as e:
            print(f"[POWER-SNMP] {t['ip']}: {e}")
            continue
        raw = vals[0] if vals else None
        if raw is None:
            continue
        try:
            scale = float(t.get("power_scale") or 1.0)
            watts = float(raw) * scale
        except (TypeError, ValueError):
            continue
        try:
            record_power_metric(t["ip"], watts=watts, source="snmp")
            ok += 1
        except Exception as e:
            print(f"[POWER-SNMP] simpan {t['ip']} gagal: {e}")
    if polled:
        print(f"[POWER-SNMP] {ok}/{polled} host terpolling")
    return {"polled": polled, "ok": ok}
