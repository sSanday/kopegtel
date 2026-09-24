"""nms.fiber_poll — evaluasi dan polling fiber (background jobs)."""

import re
import sqlite3
import time
from datetime import datetime, timedelta

from nms.db import (
    _commit_with_retry,
    _insert_system_log,
    _parse_maint_time,
    db_lock,
    get_active_maintenance_map,
    get_db,
    get_setting,
)
from nms.fiber import (
    FIBER_DEGRADE_DAYS,
    FIBER_DEGRADE_DB,
    FIBER_DEGRADE_MIN_SPAN_H,
    FIBER_FLAP_FLIPS,
    FIBER_FLAP_HOURS,
    FIBER_STALE_MIN,
    _fiber_thresholds,
    _th_for_ont,
    fiber_eval,
)
from nms.format import _fmt_age, _fmt_duration
from nms.monitor import (
    FIBER_PARENT_MIN,
    _fiber_oltkey,
    _fiber_parent_min,
    fiber_parent_down,
    refresh_fiber_parent_map,
)
from nms.notify import send_telegram_alert
from nms.olt import _olt_raw_to_dbm
from nms.snmp import _snmp_get, _valid_oid

_FIBER_SUMMARY_ICON = {
    "overload": "🔊",
    "critical": "🔴",
    "warning": "🟡",
    "stale": "🟣",
    "unknown": "⚪",
    "normal": "🟢",
}
_FIBER_SUMMARY_RANK = {"overload": 0, "critical": 1, "warning": 2, "stale": 3}


def _fiber_summary_clean(s):
    return re.sub(r"[*_`\[\]]", "", str(s or "")).strip()[:80]


def send_fiber_summary():
    """Laporan harian fiber via Telegram (cron 08:05, setelah heartbeat host).

    Snapshot hitungan status + daftar perlu perhatian (maks 8, diurut
    overload > critical > warning > stale) + degradasi dini (maks 5).
    Baris mute/maintenance dihitung tapi tak masuk daftar perhatian.
    Tanpa ONT terdaftar -> diam (tak ada yang dilaporkan).
    """
    try:
        conn, c = get_db()
        try:
            c.execute("SELECT * FROM fiber_onts ORDER BY id ASC")
            rows = [dict(r) for r in c.fetchall()]
        finally:
            conn.close()
    except Exception as e:
        print(f"[FIBER-SUMMARY] load gagal: {e}")
        return
    if not rows:
        return
    maint_map = get_active_maintenance_map()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    counts = {
        "total": len(rows),
        "normal": 0,
        "warning": 0,
        "critical": 0,
        "overload": 0,
        "stale": 0,
        "unknown": 0,
        "degrading": 0,
        "muted": 0,
        "maintenance": 0,
        "flapping": 0,
    }
    attention, degrading, flapping = [], [], []
    _pbuckets = {}
    for o in rows:
        status, severity, advice, _need, _stale, _age = _fiber_row_status(o)
        if status in counts:
            counts[status] += 1
        muted = _is_mute_active(o)
        if muted:
            counts["muted"] += 1
        in_maint = _fiber_maintenance_info(o, maint_map)[0]
        if in_maint:
            counts["maintenance"] += 1
        if muted or in_maint:
            continue
        _pkey = _fiber_oltkey(o)
        _parent = fiber_parent_down.get(_pkey) if _pkey else None
        if _parent:
            if severity:
                _b = _pbuckets.setdefault(_pkey, {"ent": _parent, "n": 0, "crit": 0})
                _b["n"] += 1
                if status in ("critical", "overload"):
                    _b["crit"] += 1
            continue
        if severity:
            try:
                rx_sort = (
                    float(o["rx_power"]) if o.get("rx_power") is not None else 99.0
                )
            except (ValueError, TypeError):
                rx_sort = 99.0
            attention.append((_FIBER_SUMMARY_RANK.get(status, 9), rx_sort, o, status))
            continue
        dg = fiber_degrade_memory.get(o["id"]) or {}
        if dg.get("degrading"):
            counts["degrading"] += 1
            degrading.append((o, dg.get("drop_db")))
        fl = fiber_flap_memory.get(o["id"]) or {}
        if fl.get("flapping"):
            counts["flapping"] += 1
            flapping.append((o, fl.get("flips")))
    attention.sort(key=lambda t: (t[0], t[1], t[2]["id"]))
    top = attention[:8]
    rest = len(attention) - len(top)
    lines = []
    for _pkey, _pb in _pbuckets.items():
        if not _pb["n"]:
            continue
        _nm = (_pb["ent"] or {}).get("name") or _pkey
        lines.append(
            f"🔌 `{_nm}` — induk bermasalah, {_pb['n']} ONT "
            f"({_pb['crit']} kritis/overload) disuppress"
        )
    for _rank, _rx, o, status in top:
        icon = _FIBER_SUMMARY_ICON.get(status, "•")
        cust = _fiber_summary_clean(o.get("customer"))
        loc = "/".join([x for x in (o.get("olt_name"), o.get("odp_name")) if x])
        rx_txt = f"{o['rx_power']} dBm" if o.get("rx_power") is not None else "—"
        extra = f" ({cust})" if cust else ""
        loc_txt = f" [{_fiber_summary_clean(loc)}]" if loc else ""
        lines.append(
            f"{icon} `{o['ont_sn']}`{extra} — {status.upper()} Rx {rx_txt}{loc_txt}"
        )
    if rest > 0:
        lines.append(f"  … +{rest} lainnya — lihat dashboard /triggers?cat=fiber")
    for o, drop in degrading[:5]:
        cust = _fiber_summary_clean(o.get("customer"))
        extra = f" ({cust})" if cust else ""
        lines.append(f"📉 `{o['ont_sn']}`{extra} — turun {drop} dB, cek sebelum kritis")
    _listed = {o["id"] for o, _ in degrading[:5]}
    try:
        _fth, _fh = _fiber_flap_settings()
    except Exception:
        _fth, _fh = FIBER_FLAP_FLIPS, FIBER_FLAP_HOURS
    for o, flips in flapping[:3]:
        if o["id"] in _listed:
            continue
        cust = _fiber_summary_clean(o.get("customer"))
        extra = f" ({cust})" if cust else ""
        lines.append(
            f"↔ `{o['ont_sn']}`{extra} — flapping {flips}x/{_fh} jam, cek konektor/ODP"
        )
    if not lines:
        lines.append("✅ Semua ONT terpantau normal.")
    try:
        _dth, _dd = _fiber_degrade_settings()
    except Exception:
        _dth, _dd = FIBER_DEGRADE_DB, FIBER_DEGRADE_DAYS
    send_telegram_alert(
        f"📊 *Laporan Harian Fiber (snapshot)*\n"
        f"Waktu : {now}\n"
        f"Total {counts['total']} ONT — "
        f"🟢{counts['normal']} 🟡{counts['warning']} 🔴{counts['critical']} "
        f"🔊{counts['overload']} 🟣{counts['stale']} "
        f"📉degradasi {counts['degrading']} ↔flap {counts['flapping']} 🔇mute {counts['muted']}\n\n"
        + "\n".join(lines)
    )


fiber_alarm_memory = {}
fiber_degrade_memory = {}
fiber_flap_memory = {}
fiber_degrade_tg = {}
FIBER_DEGRADE_TG_COOLDOWN_S = 86400


def get_ont_optical_power_snmp(olt_ip, community, ont_index, vendor="zte"):
    """Hook integrasi OLT via SNMP (belum aktif, return None).

    vendor 'zte'    : ONT Rx/Tx umumnya di enterprise MIB 1.3.6.1.4.1.3902
                      (beda tipe OLT beda tabel, sesuaikan lewat uji snmpwalk).
    vendor 'huawei' : hwGpon MIB 1.3.6.1.4.1.2011.6.128.1.x (rx/tx optical power).
    Cara pakai: isi OLT IP + community di env / form, lalu ganti fungsi ini
    dengan snmpwalk asli memakai pola get_snmp_bandwidth() di atas.
    """
    _ = (olt_ip, community, ont_index, vendor)
    return None, None


def _is_mute_active(o, now=None):
    """True bila alarm ONT sedang di-mute (hormati mute_until).

    mute_alarm=0 -> tidak mute. mute_until kosong -> mute permanen.
    mute_until terlewati -> mute dianggap kedaluwarsa (alarm aktif lagi).
    Format mute_until fleksibel mengikuti _parse_maint_time.
    """
    try:
        if not o.get("mute_alarm"):
            return False
    except (AttributeError, TypeError):
        return False
    until_raw = (
        (o.get("mute_until") or "").strip()
        if isinstance(o.get("mute_until"), str)
        else o.get("mute_until")
    )
    if not until_raw:
        return True
    try:
        until = _parse_maint_time(str(until_raw))
    except Exception:
        return True
    if not until:
        return True
    return (now or datetime.now()) <= until


def _fiber_maintenance_info(o, maint_map):
    """Cek maintenance hierarki untuk satu ONT.

    Urutan: ONT SN persis -> ODP:<nama> -> OLT:<nama> (case-insensitive).
    Kembalikan (in_maint, reason, scope) dengan scope salah satu
    'ont'/'odp'/'olt'/None.
    """
    if not maint_map:
        return False, None, None
    try:
        norm = {str(k).strip().lower(): v for k, v in (maint_map or {}).items()}
    except Exception:
        return False, None, None
    sn = (o.get("ont_sn") or "").strip()
    if sn and sn.lower() in norm:
        e = norm[sn.lower()] or {}
        return True, (e.get("reason") or "").strip() or None, "ont"
    odp = (o.get("odp_name") or "").strip().lower()
    if odp and ("odp:" + odp) in norm:
        e = norm["odp:" + odp] or {}
        return True, (e.get("reason") or "").strip() or None, "odp"
    olt = (o.get("olt_name") or "").strip().lower()
    if olt and ("olt:" + olt) in norm:
        e = norm["olt:" + olt] or {}
        return True, (e.get("reason") or "").strip() or None, "olt"
    return False, None, None


def _fiber_degrade_settings():
    """(thresh_db, days) degradasi dari settings, di-clamp ke rentang valid."""
    try:
        thresh = float(get_setting("fiber_degrade_db", FIBER_DEGRADE_DB))
    except (ValueError, TypeError):
        thresh = FIBER_DEGRADE_DB
    try:
        days = int(float(get_setting("fiber_degrade_days", FIBER_DEGRADE_DAYS)))
    except (ValueError, TypeError):
        days = FIBER_DEGRADE_DAYS
    return min(10.0, max(0.5, thresh)), min(30, max(1, days))


def _fiber_stale_threshold_min():
    try:
        m = int(float(get_setting("fiber_stale_min", FIBER_STALE_MIN)))
    except (ValueError, TypeError):
        m = FIBER_STALE_MIN
    return min(10080, max(10, m))


def _fiber_stale_info(o, now=None):
    """(is_stale, age_txt). Hanya sumber otomatis (snmp/simulator) yang punya
    pengukuran tapi tak ada data baru melewati ambang fiber_stale_min.
    Manual tak pernah stale (datanya input teknisi, bukan hasil polling)."""
    try:
        src = (o.get("source") or "manual").strip().lower()
    except (AttributeError, TypeError):
        src = "manual"
    if src not in ("snmp", "simulator"):
        return False, None
    if o.get("rx_power") is None and o.get("tx_power") is None:
        return False, None
    seen_raw = (o.get("last_seen") or "").strip()
    if not seen_raw:
        return False, None
    try:
        seen = datetime.strptime(seen_raw, "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return False, None
    now = now or datetime.now()
    age_min = (now - seen).total_seconds() / 60.0
    if age_min < 0:
        return False, None
    if age_min >= _fiber_stale_threshold_min():
        return True, _fmt_age(age_min)
    return False, None


def _fiber_stale_advice(o, age_txt):
    rx = o.get("rx_power")
    rx_txt = f"{rx} dBm" if rx is not None else "—"
    seen = o.get("last_seen") or "—"
    return (
        f"Data tidak segar sejak {age_txt} (terakhir {seen}). "
        f"Rx terakhir {rx_txt}. Kemungkinan ONT LOS/mati atau SNMP OLT gagal — "
        "cek OLT, ONT, dan jalur fiber."
    )


def _fiber_degradation(fid, current_rx, now=None):
    """(degrading, drop_db). Bandingkan Rx terawal dalam window
    (fiber_degrade_days) vs Rx kini. drop positif = memburuk.
    Butuh rentang history >= 24 jam agar noise sesaat tak false-positive."""
    try:
        cur = float(current_rx)
    except (ValueError, TypeError):
        return False, None
    if fid is None:
        return False, None
    thresh, days = _fiber_degrade_settings()
    now = now or datetime.now()
    start = (now - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        conn, c = get_db()
        try:
            c.execute(
                "SELECT rx_power, timestamp FROM fiber_history WHERE ont_id=? "
                "AND rx_power IS NOT NULL AND timestamp >= ? "
                "ORDER BY timestamp ASC, id ASC LIMIT 1",
                (fid, start),
            )
            first = c.fetchone()
        finally:
            conn.close()
    except Exception:
        return False, None
    if not first:
        return False, None
    try:
        first_rx = float(first["rx_power"])
        first_ts = datetime.strptime(first["timestamp"], "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError, KeyError):
        return False, None
    if (now - first_ts).total_seconds() < FIBER_DEGRADE_MIN_SPAN_H * 3600:
        return False, None
    drop = round(first_rx - cur, 2)
    return drop >= thresh, drop


def _fiber_flap_settings():
    """(min_flips, hours) flap dari settings, di-clamp ke rentang valid."""
    try:
        flips = int(float(get_setting("fiber_flap_flips", FIBER_FLAP_FLIPS)))
    except (ValueError, TypeError):
        flips = FIBER_FLAP_FLIPS
    try:
        hours = int(float(get_setting("fiber_flap_hours", FIBER_FLAP_HOURS)))
    except (ValueError, TypeError):
        hours = FIBER_FLAP_HOURS
    return min(20, max(2, flips)), min(72, max(1, hours))


def _fiber_flap(fid, now=None):
    """(flapping, flips). Hitung bolak-balik normal<->terganggu dari history.

    Tiap titik dinilai via fiber_eval (threshold global); pindah kubu
    normal<->(warning/critical/overload) dihitung 1 flip. Titik tanpa data
    dilewati (tak memutus rangkaian).
    """
    if fid is None:
        return False, None
    thresh, hours = _fiber_flap_settings()
    now = now or datetime.now()
    start = (now - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        conn, c = get_db()
        try:
            c.execute(
                "SELECT rx_power, tx_power FROM fiber_history WHERE ont_id=? "
                "AND timestamp >= ? ORDER BY timestamp ASC, id ASC LIMIT 20000",
                (fid, start),
            )
            pts = c.fetchall()
        finally:
            conn.close()
    except Exception:
        return False, None
    if not pts:
        return False, 0
    th = _fiber_thresholds()
    flips, prev_bad = 0, None
    for r in pts:
        try:
            rx = None if r["rx_power"] is None else float(r["rx_power"])
        except (ValueError, TypeError):
            rx = None
        try:
            tx = None if r["tx_power"] is None else float(r["tx_power"])
        except (ValueError, TypeError):
            tx = None
        if rx is None and tx is None:
            continue
        status, _, _, _ = fiber_eval(rx, tx, th)
        bad = status in ("warning", "critical", "overload")
        if prev_bad is not None and bad != prev_bad:
            flips += 1
        prev_bad = bad
    return flips >= thresh, flips


_FIBER_DOWN_SEV = ("critical", "overload")


def _fiber_downtime_transition(c, fid, ont_sn, status, rx, timestamp):
    """Buka/tutup catatan downtime dalam transaksi milik caller.

    Buka saat critical/overload; tutup saat normal/warning/unknown.
    stale (tak terpantau) membiarkan catatan terbuka (gangguan dianggap
    berlanjut sampai ada data segar). Kembalikan durasi detik bila baru
    saja tertutup, else None. c = cursor aktif.
    """
    try:
        c.execute(
            "SELECT id, status, started_at FROM fiber_downtime "
            "WHERE ont_id=? AND resolved_at IS NULL ORDER BY id DESC LIMIT 1",
            (fid,),
        )
        open_row = c.fetchone()
    except sqlite3.OperationalError:
        return None
    if status in _FIBER_DOWN_SEV:
        if open_row:
            if open_row["status"] != status:
                try:
                    c.execute(
                        "UPDATE fiber_downtime SET status=? WHERE id=?",
                        (status, open_row["id"]),
                    )
                except sqlite3.OperationalError:
                    pass
            return None
        try:
            c.execute(
                "INSERT INTO fiber_downtime (ont_id, ont_sn, status, rx_dbm, started_at)"
                " VALUES (?,?,?,?,?)",
                (fid, ont_sn, status, rx, timestamp),
            )
        except sqlite3.OperationalError:
            pass
        return None
    if not open_row or status == "stale":
        return None
    try:
        started = datetime.strptime(open_row["started_at"], "%Y-%m-%d %H:%M:%S")
        ended = datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S")
        dur = max(0, int((ended - started).total_seconds()))
    except (ValueError, TypeError):
        dur = 0
    try:
        c.execute(
            "UPDATE fiber_downtime SET resolved_at=?, duration_s=? WHERE id=?",
            (timestamp, dur, open_row["id"]),
        )
    except sqlite3.OperationalError:
        return None
    return dur


_FIBER_PARENT_LOGS = {
    "FIBER_CRITICAL",
    "FIBER_WARNING",
    "FIBER_OVERLOAD",
    "FIBER_NORMAL",
    "FIBER_STALE",
    "FIBER_DEGRADE",
}


def _fiber_parent_of(o):
    """Entri induk aktif untuk satu ONT (None bila tak ada)."""
    try:
        return fiber_parent_down.get(_fiber_oltkey(o))
    except Exception:
        return None


def _fiber_emit(c, o, rx, dec, log, tg, timestamp, tg_queue, closed_dur=None):
    """Tulis log + antre telegram dengan aturan supresi induk.

    Bila ONT di bawah parent aktif dan event bertipe alarm/pulih/stale/
    degradasi (bukan mute/maintenance eksplisit): telegram dibuang,
    log ditulis ulang sebagai FIBER_PARENT. Dipakai poll & single check.
    """
    parent = _fiber_parent_of(o)
    if log:
        etype, ehost, emsg = log
        if closed_dur is not None and etype == "FIBER_NORMAL":
            emsg = f"{emsg} Durasi gangguan: {_fmt_duration(closed_dur)}."
        if parent and etype in _FIBER_PARENT_LOGS:
            pname = parent.get("name") or _fiber_oltkey(o) or "?"
            etype = "FIBER_PARENT"
            emsg = (
                f"{dec['status'].upper()} Rx {rx} Tx {o.get('tx_power')} — "
                f"telegram disuppress (induk OLT '{pname}' bermasalah)"
            )
        try:
            _insert_system_log(c, etype, ehost, emsg, timestamp)
        except Exception:
            pass
    if tg and not parent:
        if closed_dur is not None and dec["status"] == "normal":
            tg = f"{tg}\nDurasi gangguan: {_fmt_duration(closed_dur)}"
        tg_queue.append(tg)


def _fiber_decide(o, th, prev, maint_map, timestamp):
    """Satu keputusan evaluasi ONT. Kembalikan dict:
    {status, severity, advice, need, muted, in_maint, stale, tg_msg, log, mem}.
    tg_msg None bila tak perlu kirim (belum berubah / mute / maintenance).
    log = (event_type, host, message) atau None. mem = nilai memory baru.
    """
    status, severity, advice, need = fiber_eval(
        o.get("rx_power"), o.get("tx_power"), th
    )
    muted = _is_mute_active(o)
    in_maint, maint_reason, maint_scope = _fiber_maintenance_info(o, maint_map)
    stale, stale_age = _fiber_stale_info(o)
    if stale and status in ("normal", "warning", "unknown"):
        status, severity = "stale", "warning"
        advice = _fiber_stale_advice(o, stale_age or "?")
    rx, tx = o.get("rx_power"), o.get("tx_power")
    label = o.get("customer") or o.get("ont_sn")
    tx_txt = f"Tx: {tx} dBm" if tx is not None else "Tx: —"
    tg_msg, log, mem = None, None, prev
    if status in ("critical", "overload", "warning") and prev != status:
        mem = status
        if status == "overload":
            tg_msg = (
                f"🔊 *FIBER OVERLOAD — PERLU PEREDAM*\nONT: `{o['ont_sn']}` ({label})\n"
                f"Rx: *{rx} dBm* (> {th['overload']} dBm) · {tx_txt}\n{advice}\nWaktu: {timestamp}"
            )
        elif status == "critical":
            tg_msg = (
                f"🚨 *FIBER REDAMAN TINGGI*\nONT: `{o['ont_sn']}` ({label})\n"
                f"Rx: *{rx} dBm* · {tx_txt}\n{advice}\nWaktu: {timestamp}"
            )
        else:
            tg_msg = (
                f"⚠️ *FIBER WARNING*\nONT: `{o['ont_sn']}` ({label})\n"
                f"Rx: *{rx} dBm* · {tx_txt}\n{advice}\nWaktu: {timestamp}"
            )
        if in_maint:
            scope_txt = (
                f" ({maint_scope.upper()} {(o.get('odp_name') or o.get('olt_name') or o['ont_sn'])})"
                if maint_scope and maint_scope != "ont"
                else ""
            )
            log = (
                "FIBER_MAINT",
                o["ont_sn"],
                f"Rx {rx} dBm Tx {tx} dalam maintenance{scope_txt} — telegram disuppress"
                f"{(' - ' + maint_reason) if maint_reason else ''}",
            )
            tg_msg = None
        elif muted:
            mute_note = ""
            try:
                if (o.get("mute_until") or "").strip():
                    mute_note = f" s/d {o.get('mute_until')}"
                if (o.get("mute_reason") or "").strip():
                    mute_note += f" ({(o.get('mute_reason') or '').strip()[:100]})"
            except Exception:
                pass
            log = (
                "FIBER_MUTED",
                o["ont_sn"],
                f"Rx {rx} dBm Tx {tx} — alarm dimute{mute_note}",
            )
            tg_msg = None
        else:
            log = (
                "FIBER_" + status.upper(),
                o["ont_sn"],
                f"Rx {rx} dBm Tx {tx} — {advice}",
            )
    elif status == "normal":
        if prev not in (None, "normal"):
            if prev == "stale":
                log = ("FIBER_NORMAL", o["ont_sn"], f"Terpantau kembali (Rx {rx} dBm)")
            else:
                log = ("FIBER_NORMAL", o["ont_sn"], f"Rx kembali normal ({rx} dBm)")
            if not muted and not in_maint:
                tg_msg = (
                    f"✅ *FIBER PULIH*\nONT: `{o['ont_sn']}`\n"
                    f"Rx: {rx} dBm\nWaktu: {timestamp}"
                )
        mem = "normal"
    elif status == "stale":
        mem = "stale"
        if prev != "stale":
            if in_maint:
                log = (
                    "FIBER_MAINT",
                    o["ont_sn"],
                    "Stale dalam maintenance — telegram disuppress"
                    f"{(' - ' + maint_reason) if maint_reason else ''}",
                )
            elif muted:
                log = ("FIBER_MUTED", o["ont_sn"], "Stale — alarm dimute")
            else:
                log = (
                    "FIBER_STALE",
                    o["ont_sn"],
                    f"Tak terpantau sejak {stale_age} — {advice}",
                )
                tg_msg = (
                    f"⚠️ *FIBER TAK TERPANTAU (STALE)*\nONT: `{o['ont_sn']}` ({label})\n"
                    f"{advice}\nWaktu: {timestamp}"
                )
    elif status == "unknown":
        mem = "unknown"
    return {
        "status": status,
        "severity": severity,
        "advice": advice,
        "need": need,
        "muted": muted,
        "in_maint": in_maint,
        "stale": stale,
        "tg_msg": tg_msg,
        "log": log,
        "mem": mem,
    }


def _fiber_single_check(fid):
    """Evaluasi 1 ONT secara sinkron (dipakai create/update agar alert cepat).

    Update status/memory/log + kirim telegram langsung tanpa menunggu
    scheduler, tanpa thread seluruh tabel (anti duplikat: memory di-set di sini
    sehingga poll 120s berikutnya tidak mengirim ulang).
    """
    try:
        conn, c = get_db()
        try:
            c.execute("SELECT * FROM fiber_onts WHERE id=?", (fid,))
            row = c.fetchone()
            if not row:
                return
            o = dict(row)
        finally:
            conn.close()
    except Exception as e:
        print(f"[FIBER] single check load gagal: {e}")
        return
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        refresh_fiber_parent_map()
    except Exception as e:
        print(f"[FIBER] parent refresh gagal: {e}")
    dec = _fiber_decide(
        o,
        _th_for_ont(o),
        fiber_alarm_memory.get(fid),
        get_active_maintenance_map(),
        timestamp,
    )
    closed_dur = None
    tg_out = []
    with db_lock:
        conn, c = get_db()
        try:
            c.execute(
                "UPDATE fiber_onts SET status=?, last_checked=? WHERE id=?",
                (dec["status"], timestamp, fid),
            )
            try:
                closed_dur = _fiber_downtime_transition(
                    c, fid, o["ont_sn"], dec["status"], o.get("rx_power"), timestamp
                )
            except Exception as e:
                print(f"[FIBER] downtime single check gagal: {e}")
                closed_dur = None
            _fiber_emit(
                c,
                o,
                o.get("rx_power"),
                dec,
                dec["log"],
                dec["tg_msg"],
                timestamp,
                tg_out,
                closed_dur,
            )
            _commit_with_retry(conn)
        except sqlite3.OperationalError as e:
            print(f"[DB LOCK] fiber single check gagal: {e}")
            try:
                conn.rollback()
            except Exception:
                pass
        finally:
            conn.close()
    fiber_alarm_memory[fid] = dec["mem"]
    for tmsg in tg_out:
        try:
            send_telegram_alert(tmsg)
        except Exception as e:
            print(f"[WARN] telegram fiber gagal: {e}")


def poll_fiber_monitor():
    """Job scheduler: snapshot history semua ONT + cek threshold.

    ONT source=simulator nilainya digerakkan (random walk) agar grafik demo hidup.
    ONT source=manual/snmp nilainya tidak diubah, tapi tetap dicatat ke history
    agar grafik tidak kosong. ONT tanpa Rx & Tx dilewati (tak ada yang dicatat).
    Telegram hanya saat status berubah (anti spam); disuppress saat mute /
    maintenance (tetap dicatat di system_logs, memory di-set agar tak ada
    ledakan notifikasi setelah maintenance selesai).
    Korelasi induk: 2-pass. Pass 1 evaluasi murni semua ONT; pass 1.5
    mendeteksi insiden massal per OLT (>=fiber_parent_min kritis/overload)
    menjadi 1 alarm induk; pass 2 menerapkan log/telegram/status dengan
    supresi induk (alarm individual dibuang, log FIBER_PARENT).
    """
    import random

    try:
        conn, c = get_db()
        try:
            try:
                c.execute("SELECT * FROM fiber_onts ORDER BY id ASC")
            except sqlite3.OperationalError:
                return
            rows = [dict(r) for r in c.fetchall()]
        finally:
            conn.close()
    except Exception as e:
        print(f"[FIBER] load gagal: {e}")
        return
    if not rows:
        return
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    maint_map = get_active_maintenance_map()
    try:
        refresh_fiber_parent_map()
    except Exception as e:
        print(f"[FIBER] parent refresh gagal: {e}")
    try:
        parent_min = _fiber_parent_min()
    except Exception:
        parent_min = FIBER_PARENT_MIN
    tg_queue = []
    computed = []
    _clearing = []
    with db_lock:
        conn, c = get_db()
        try:
            for o in rows:
                oid = o["id"]
                rx = o["rx_power"]
                tx = o.get("tx_power")
                if (o.get("source") or "manual") == "simulator":
                    base = rx if rx is not None else -19.0
                    try:
                        base = float(base)
                    except (ValueError, TypeError):
                        base = -19.0
                    rx = max(
                        -22.0, min(-16.0, round(base + random.uniform(-0.6, 0.6), 2))
                    )
                    c.execute(
                        "UPDATE fiber_onts SET rx_power=?, last_checked=?, updated_at=?, last_seen=? WHERE id=?",
                        (rx, timestamp, timestamp, timestamp, oid),
                    )
                    o["rx_power"] = rx
                if rx is None and tx is None:
                    continue
                try:
                    c.execute(
                        "INSERT INTO fiber_history (ont_id, rx_power, tx_power, timestamp) VALUES (?, ?, ?, ?)",
                        (oid, rx, tx, timestamp),
                    )
                except sqlite3.OperationalError:
                    pass
                o["rx_power"], o["tx_power"] = rx, tx
                dec = _fiber_decide(
                    o, _th_for_ont(o), fiber_alarm_memory.get(oid), maint_map, timestamp
                )
                fiber_alarm_memory[oid] = dec["mem"]
                try:
                    _was_degr = bool(
                        (fiber_degrade_memory.get(oid) or {}).get("degrading")
                    )
                    _degr, _drop = _fiber_degradation(oid, rx)
                    fiber_degrade_memory[oid] = {"degrading": _degr, "drop_db": _drop}
                except Exception as e:
                    print(f"[FIBER] degradasi {oid} gagal: {e}")
                    _was_degr, _degr, _drop = False, False, None
                try:
                    _flap, _flips = _fiber_flap(oid)
                    fiber_flap_memory[oid] = {"flapping": _flap, "flips": _flips}
                except Exception as e:
                    print(f"[FIBER] flap {oid} gagal: {e}")
                computed.append((o, oid, rx, tx, dec, _was_degr, _degr, _drop))
            try:
                bad_by_olt = {}
                for _o, _oid, _rx, _tx, _dec, _w, _d, _dr in computed:
                    if (
                        _dec["status"] in ("critical", "overload")
                        and not _dec.get("muted")
                        and not _dec.get("in_maint")
                    ):
                        bad_by_olt.setdefault(_fiber_oltkey(_o), []).append(_o)
                for _oltkey, _members in bad_by_olt.items():
                    if (
                        not _oltkey
                        or _oltkey in fiber_parent_down
                        or len(_members) < parent_min
                    ):
                        continue
                    _name = next(
                        (
                            (m.get("olt_name") or "").strip()
                            for m in _members
                            if (m.get("olt_name") or "").strip()
                        ),
                        _oltkey,
                    )
                    fiber_parent_down[_oltkey] = {
                        "ip": None,
                        "since": timestamp,
                        "name": _name,
                        "synthetic": True,
                        "count": len(_members),
                    }
                    tg_queue.append(
                        f"🔌 *INSIDEN MASSAL OLT*\nOLT: `{_name}`\n"
                        f"{len(_members)} ONT kritis/overload (ambang {parent_min}). "
                        f"Alarm individual disuppress mulai kini.\n"
                        f"Cek OLT/power/PON uplink!\nWaktu: {timestamp}"
                    )
                    try:
                        _insert_system_log(
                            c,
                            "FIBER_PARENT",
                            _name,
                            f"insiden massal: {len(_members)} ONT kritis/overload",
                            timestamp,
                        )
                    except Exception:
                        pass
                for _key, _ent in list(fiber_parent_down.items()):
                    if not (_ent or {}).get("synthetic"):
                        continue
                    _still = sum(
                        1
                        for (_o, _oid, _rx, _tx, _dec, _w, _d, _dr) in computed
                        if _fiber_oltkey(_o) == _key
                        and _dec["status"] in ("critical", "overload")
                    )
                    (_ent or {}).update({"count": _still})
                    if not _still:
                        _clearing.append(_key)
                        _pname = (_ent or {}).get("name") or _key
                        tg_queue.append(
                            f"✅ *INSIDEN MASSAL PULIH*\nOLT: `{_pname}`\n"
                            f"Seluruh ONT kembali normal.\nWaktu: {timestamp}"
                        )
                        try:
                            _insert_system_log(
                                c,
                                "FIBER_PARENT",
                                _pname,
                                "insiden massal pulih",
                                timestamp,
                            )
                        except Exception:
                            pass
            except Exception as e:
                print(f"[FIBER] korelasi induk gagal: {e}")
            for o, oid, rx, tx, dec, _was_degr, _degr, _drop in computed:
                _dlog, _dtg = None, None
                if _degr and not _was_degr and dec["status"] == "normal":
                    try:
                        _dth, _dd = _fiber_degrade_settings()
                    except Exception:
                        _dth, _dd = FIBER_DEGRADE_DB, FIBER_DEGRADE_DAYS
                    if (
                        time.time() - fiber_degrade_tg.get(oid, 0)
                        >= FIBER_DEGRADE_TG_COOLDOWN_S
                    ):
                        fiber_degrade_tg[oid] = time.time()
                        _label = o.get("customer") or o.get("ont_sn")
                        if dec.get("in_maint"):
                            _dlog = (
                                "FIBER_MAINT",
                                o["ont_sn"],
                                f"Degradasi {_drop} dB dalam maintenance — telegram disuppress",
                            )
                        elif dec.get("muted"):
                            _dlog = (
                                "FIBER_MUTED",
                                o["ont_sn"],
                                f"Degradasi {_drop} dB — alarm dimute",
                            )
                        else:
                            _dlog = (
                                "FIBER_DEGRADE",
                                o["ont_sn"],
                                f"Rx turun {_drop} dB dalam {_dd} hari (kini {rx} dBm)",
                            )
                            _dtg = (
                                f"📉 *FIBER DEGRADASI TERDETEKSI*\nONT: `{o['ont_sn']}` ({_label})\n"
                                f"Rx turun *{_drop} dB* dalam {_dd} hari "
                                f"(kini {rx} dBm, ambang {_dth} dB).\n"
                                f"Cek bending/konektor/splicing sebelum kritis.\nWaktu: {timestamp}"
                            )
                try:
                    closed_dur = _fiber_downtime_transition(
                        c, oid, o["ont_sn"], dec["status"], rx, timestamp
                    )
                except Exception as e:
                    print(f"[FIBER] downtime {oid} gagal: {e}")
                    closed_dur = None
                _fiber_emit(
                    c,
                    o,
                    rx,
                    dec,
                    dec["log"],
                    dec["tg_msg"],
                    timestamp,
                    tg_queue,
                    closed_dur,
                )
                _fiber_emit(c, o, rx, dec, _dlog, _dtg, timestamp, tg_queue)
                c.execute(
                    "UPDATE fiber_onts SET status=?, last_checked=? WHERE id=?",
                    (dec["status"], timestamp, oid),
                )
            for _ckey in _clearing:
                fiber_parent_down.pop(_ckey, None)
            _commit_with_retry(conn)
        except sqlite3.OperationalError as e:
            print(f"[DB LOCK] poll_fiber gagal: {e}")
            try:
                conn.rollback()
            except Exception:
                pass
        finally:
            conn.close()
    for msg in tg_queue:
        try:
            send_telegram_alert(msg)
        except Exception as e:
            print(f"[WARN] telegram fiber gagal: {e}")


def poll_fiber_snmp():
    """Tarik Rx/Tx ONT dari OLT via SNMP (source=snmp).

    Tiap ONT butuh olt_name (cocok ke tabel olts) + ont_index (sufiks OID).
    Nilai mentah dibagi `div` OLT (umumnya 100 = satuan 0.01 dBm).
    Status/alarm/history ditangani poll_fiber_monitor berikutnya.

    Catatan: didefinisikan di sini (sebelum blok scheduler) agar referensi
    scheduler.add_job saat import tak NameError.
    """
    try:
        conn, c = get_db()
        try:
            try:
                c.execute("SELECT * FROM olts ORDER BY id ASC")
                olts = [dict(r) for r in c.fetchall()]
            except sqlite3.OperationalError:
                return
            try:
                c.execute(
                    "SELECT id, ont_sn, olt_name, ont_index, rx_power, tx_power"
                    " FROM fiber_onts WHERE source='snmp'"
                )
                onts = [dict(r) for r in c.fetchall()]
            except sqlite3.OperationalError:
                return
        finally:
            conn.close()
    except Exception as e:
        print(f"[FIBER-SNMP] load gagal: {e}")
        return
    if not olts or not onts:
        return
    by_name = {(o.get("name") or ""): o for o in olts}
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    updates = []
    for t in onts:
        idx = (t.get("ont_index") or "").strip()
        olt = by_name.get((t.get("olt_name") or "").strip())
        if (
            not idx
            or not olt
            or not (olt.get("community") or "").strip()
            or not olt.get("ip")
        ):
            continue
        oids, kinds = [], []
        if _valid_oid(olt.get("rx_base") or ""):
            oids.append(olt["rx_base"].strip().strip(".") + "." + idx)
            kinds.append("rx")
        if _valid_oid(olt.get("tx_base") or ""):
            oids.append(olt["tx_base"].strip().strip(".") + "." + idx)
            kinds.append("tx")
        if not oids:
            continue
        try:
            vals = _snmp_get(olt["ip"], olt["community"], oids, timeout=3.0)
        except Exception as e:
            print(f"[FIBER-SNMP] {t.get('ont_sn')}: {e}")
            continue
        new_rx, new_tx = t.get("rx_power"), t.get("tx_power")
        got_fresh = False
        for kind, v in zip(kinds, vals or []):
            if v is None:
                continue
            f = _olt_raw_to_dbm(v, olt)
            if f is None:
                continue
            if kind == "rx" and -40 <= f <= 10:
                new_rx = f
                got_fresh = True
            elif kind == "tx" and -10 <= f <= 10:
                new_tx = f
                got_fresh = True
        if got_fresh:
            updates.append((new_rx, new_tx, timestamp, timestamp, t["id"]))
    if not updates:
        return
    with db_lock:
        conn, c = get_db()
        try:
            c.executemany(
                "UPDATE fiber_onts SET rx_power=?, tx_power=?,"
                " last_seen=?, last_checked=? WHERE id=?",
                updates,
            )
            _commit_with_retry(conn)
        except sqlite3.OperationalError as e:
            print(f"[DB LOCK] poll_fiber_snmp gagal: {e}")
            try:
                conn.rollback()
            except Exception:
                pass
        finally:
            conn.close()
    print(f"[FIBER-SNMP] {len(updates)} ONT diperbarui dari OLT")


def _fiber_row_status(o):
    """Status satu baris ONT: eval + overlay stale.

    Kembalikan (status, severity, advice, need, stale, stale_age).
    """
    status, severity, advice, need = fiber_eval(
        o.get("rx_power"), o.get("tx_power"), _th_for_ont(o)
    )
    stale, stale_age = _fiber_stale_info(o)
    if stale and status in ("normal", "warning", "unknown"):
        status, severity = "stale", "warning"
        advice = _fiber_stale_advice(o, stale_age or "?")
    return status, severity, advice, need, stale, stale_age


def _fiber_enrich_row(
    o, degrade_days=7, degrade_thresh=3.0, down_map=None, flap_hours=24
):
    """Baris ONT + field terhitung untuk API (status, mute, stale, degradasi, flap, downtime)."""
    status, severity, advice, need, stale, stale_age = _fiber_row_status(o)
    dg = fiber_degrade_memory.get(o["id"]) or {}
    fl = fiber_flap_memory.get(o["id"]) or {}
    down_since = (down_map or {}).get(o["id"])
    return {
        **o,
        "calc_status": status,
        "severity": severity,
        "advice": advice,
        "need_attenuator_db": need,
        "mute_active": _is_mute_active(o),
        "stale": stale,
        "stale_age": stale_age,
        "degrading": bool(dg.get("degrading")),
        "degrade_drop_db": dg.get("drop_db"),
        "degrade_days": degrade_days,
        "degrade_thresh_db": degrade_thresh,
        "flapping": bool(fl.get("flapping")),
        "flap_count": fl.get("flips"),
        "flap_hours": flap_hours,
        "down_since": down_since,
        "down_ongoing": down_since is not None,
    }
