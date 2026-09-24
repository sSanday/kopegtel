"""nms.fiber_routes — halaman dan API Fiber/OLT/ODP/Topo."""

import csv
import io
import ipaddress
import re
import sqlite3
from datetime import datetime, timedelta

from flask import Blueprint, Response, jsonify, render_template, request
from flask_login import current_user, login_required

from nms.auth import api_login_required
from nms.db import (
    _commit_with_retry,
    _fmt_maint_time,
    _parse_maint_time,
    db_lock,
    get_db,
)
from nms.fiber import (
    FIBER_SPLITTER_LOSS,
    _fiber_thresholds,
    _th_for_ont,
    fiber_budget_verdict,
    fiber_eval,
    fiber_link_budget,
)
from nms.fiber_poll import (
    _fiber_degrade_settings,
    _fiber_enrich_row,
    _fiber_flap_settings,
    _fiber_row_status,
    _fiber_single_check,
    _fiber_stale_info,
    fiber_alarm_memory,
    fiber_degrade_memory,
    fiber_degrade_tg,
    fiber_flap_memory,
)
from nms.format import _csv_safe, _fmt_duration, _parse_latlon
from nms.mikrotik import SYSUP_OID
from nms.notify import audit
from nms.olt import OLT_VENDOR_PRESETS, _olt_raw_to_dbm
from nms.snmp import _snmp_get, _snmp_getnext, _valid_oid, snmp_walk

fiber_bp = Blueprint("fiber", __name__)


@fiber_bp.route("/fiber")
@login_required
def fiber_page():
    return render_template("fiber.html")


@fiber_bp.route("/topo")
@login_required
def topo_page():
    return render_template("topo.html")


def _validate_fiber(d):
    ont_sn = (d.get("ont_sn") or "").strip()[:64]
    if not ont_sn or not re.match(r"^[A-Za-z0-9_.:\-]{3,64}$", ont_sn):
        return None, "ONT SN 3-64 karakter (huruf/angka/_-.:)"
    try:
        rx = d.get("rx_power", None)
        rx = None if rx in (None, "") else float(rx)
        if rx is not None and not -40 <= rx <= 10:
            return None, "Rx power harus -40..10 dBm"
    except (ValueError, TypeError):
        return None, "Rx power harus angka dBm"
    try:
        tx = d.get("tx_power", None)
        tx = None if tx in (None, "") else float(tx)
        if tx is not None and not -10 <= tx <= 10:
            return None, "Tx power harus -10..10 dBm"
    except (ValueError, TypeError):
        return None, "Tx power harus angka dBm"
    try:
        rw = d.get("rx_warn", None)
        rw = None if rw in (None, "") else float(rw)
        if rw is not None and not -40 <= rw <= 10:
            return None, "Override warn harus -40..10 dBm"
    except (ValueError, TypeError):
        return None, "Override warn harus angka dBm"
    try:
        rc = d.get("rx_crit", None)
        rc = None if rc in (None, "") else float(rc)
        if rc is not None and not -40 <= rc <= 10:
            return None, "Override crit harus -40..10 dBm"
    except (ValueError, TypeError):
        return None, "Override crit harus angka dBm"
    if rw is not None and rc is not None and not rc < rw:
        return None, "Override harus: crit < warn"
    mute = d.get("mute_alarm", 0)
    if isinstance(mute, str):
        mute = 1 if mute.strip().lower() in ("1", "true", "ya", "yes", "on") else 0
    try:
        mute = 1 if int(mute) else 0
    except (ValueError, TypeError):
        mute = 0
    mute_until_raw = d.get("mute_until", None)
    if mute_until_raw in (None, ""):
        mute_until = ""
    else:
        mute_until_dt = _parse_maint_time(str(mute_until_raw))
        if not mute_until_dt:
            return None, "mute_until harus format YYYY-MM-DD HH:MM (atau tanggal saja)"
        mute_until = _fmt_maint_time(mute_until_dt)
    mute_reason = str(d.get("mute_reason") or "").strip()[:200]
    ont_index = str(d.get("ont_index") or "").strip()[:64]
    if ont_index and not re.match(r"^[A-Za-z0-9_.\-:]{1,64}$", ont_index):
        return None, "ONT index 1-64 karakter (huruf/angka/_-.:)"
    source = str(d.get("source") or "manual").strip().lower()[:16]
    if source not in ("manual", "simulator", "snmp"):
        source = "manual"
    return {
        "ont_sn": ont_sn,
        "customer": str(d.get("customer") or "").strip()[:100],
        "olt_name": str(d.get("olt_name") or "").strip()[:100],
        "pon_port": str(d.get("pon_port") or "").strip()[:50],
        "odp_name": str(d.get("odp_name") or "").strip()[:100],
        "rx_power": rx,
        "tx_power": tx,
        "source": source,
        "mute_alarm": mute,
        "mute_until": mute_until,
        "mute_reason": mute_reason,
        "rx_warn": rw,
        "rx_crit": rc,
        "ont_index": ont_index,
    }, None


_FIBER_SORTS = (
    "olt",
    "rx_asc",
    "rx_desc",
    "sn_asc",
    "sn_desc",
    "checked_desc",
    "status",
)
_FIBER_STATUS_RANK = {
    "overload": 0,
    "critical": 1,
    "warning": 2,
    "stale": 3,
    "unknown": 4,
    "normal": 5,
}


@fiber_bp.route("/api/fiber", methods=["GET"])
@api_login_required
def api_fiber_list():
    conn, c = get_db()
    try:
        c.execute("SELECT * FROM fiber_onts ORDER BY olt_name ASC, id ASC")
        rows = [dict(r) for r in c.fetchall()]
    except sqlite3.OperationalError:
        rows = []
    finally:
        try:
            conn.close()
        except Exception:
            pass
    _dthresh, _ddays = _fiber_degrade_settings()
    _fthresh, _fhours = _fiber_flap_settings()
    down_map = _fiber_open_downtime_map()
    enriched = [_fiber_enrich_row(o, _ddays, _dthresh, down_map, _fhours) for o in rows]
    if not any(
        request.args.get(k) is not None
        for k in ("page", "per_page", "sort", "q", "status", "olt", "odp")
    ):
        return jsonify(enriched)
    status_f = (request.args.get("status") or "all").strip().lower()
    q = (request.args.get("q") or "").strip().lower()
    olt_f = (request.args.get("olt") or "").strip().lower()
    odp_f = (request.args.get("odp") or "").strip().lower()
    items = enriched
    if status_f != "all":
        items = [o for o in items if o["calc_status"] == status_f]
    if olt_f:
        items = [o for o in items if (o.get("olt_name") or "").strip().lower() == olt_f]
    if odp_f:
        items = [o for o in items if (o.get("odp_name") or "").strip().lower() == odp_f]
    if q:
        items = [
            o
            for o in items
            if q
            in " ".join(
                str(o.get(k) or "")
                for k in ("ont_sn", "customer", "olt_name", "odp_name", "pon_port")
            ).lower()
        ]
    sort = (request.args.get("sort") or "olt").strip().lower()
    if sort not in _FIBER_SORTS:
        return jsonify({"error": f"sort harus salah satu {list(_FIBER_SORTS)}"}), 400
    if sort == "rx_asc":
        items.sort(
            key=lambda o: (
                o.get("rx_power") is None,
                o.get("rx_power") if o.get("rx_power") is not None else 0,
                o.get("id"),
            )
        )
    elif sort == "rx_desc":
        items.sort(
            key=lambda o: (
                o.get("rx_power") is None,
                -(o.get("rx_power") if o.get("rx_power") is not None else 0),
                o.get("id"),
            )
        )
    elif sort == "sn_asc":
        items.sort(key=lambda o: ((o.get("ont_sn") or "").lower(), o.get("id")))
    elif sort == "sn_desc":
        items.sort(
            key=lambda o: ((o.get("ont_sn") or "").lower(), o.get("id")), reverse=True
        )
    elif sort == "checked_desc":
        items.sort(key=lambda o: (o.get("last_checked") or ""), reverse=True)
    elif sort == "status":
        items.sort(
            key=lambda o: (
                _FIBER_STATUS_RANK.get(o["calc_status"], 9),
                o.get("rx_power") if o.get("rx_power") is not None else 99,
                o.get("id"),
            )
        )
    else:
        items.sort(key=lambda o: ((o.get("olt_name") or "").lower(), o.get("id")))
    try:
        page = int(request.args.get("page", 1))
    except (ValueError, TypeError):
        return jsonify({"error": "page harus angka >= 1"}), 400
    try:
        per_page = int(request.args.get("per_page", 25))
    except (ValueError, TypeError):
        return jsonify({"error": "per_page harus angka 1-200"}), 400
    page = max(1, page)
    per_page = max(1, min(per_page, 200))
    total = len(items)
    pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, pages)
    start = (page - 1) * per_page
    return jsonify(
        {
            "items": items[start : start + per_page],
            "total": total,
            "page": page,
            "per_page": per_page,
            "pages": pages,
            "sort": sort,
        }
    )


@fiber_bp.route("/api/fiber/summary", methods=["GET"])
@api_login_required
def api_fiber_summary():
    """Ringkasan fiber: hitungan per status + Rx terburuk/terbaik + opsi select."""
    conn, c = get_db()
    try:
        c.execute("SELECT * FROM fiber_onts ORDER BY id ASC")
        rows = [dict(r) for r in c.fetchall()]
    except sqlite3.OperationalError:
        rows = []
    finally:
        try:
            conn.close()
        except Exception:
            pass
    _dthresh, _ddays = _fiber_degrade_settings()
    _fthresh, _fhours = _fiber_flap_settings()
    down_map = _fiber_open_downtime_map()
    enriched = [_fiber_enrich_row(o, _ddays, _dthresh, down_map, _fhours) for o in rows]
    counts = {
        "total": len(enriched),
        "normal": 0,
        "warning": 0,
        "critical": 0,
        "overload": 0,
        "stale": 0,
        "unknown": 0,
        "degrading": 0,
        "muted": 0,
        "down_ongoing": 0,
        "flapping": 0,
    }
    for o in enriched:
        if o["calc_status"] in counts:
            counts[o["calc_status"]] += 1
        if o["degrading"]:
            counts["degrading"] += 1
        if o["mute_active"]:
            counts["muted"] += 1
        if o["down_ongoing"]:
            counts["down_ongoing"] += 1
        if o["flapping"]:
            counts["flapping"] += 1
    with_rx = [o for o in enriched if o.get("rx_power") is not None]
    worst = sorted(with_rx, key=lambda o: (o["rx_power"], o["id"]))[:10]
    best = sorted(with_rx, key=lambda o: (-o["rx_power"], o["id"]))[:5]

    def _mini(o):
        return {
            "id": o["id"],
            "ont_sn": o.get("ont_sn"),
            "customer": o.get("customer"),
            "olt_name": o.get("olt_name"),
            "odp_name": o.get("odp_name"),
            "rx_power": o.get("rx_power"),
            "calc_status": o.get("calc_status"),
        }

    opts = sorted(enriched, key=lambda o: (o.get("ont_sn") or "").lower())
    return jsonify(
        {
            "counts": counts,
            "worst_rx": [_mini(o) for o in worst],
            "best_rx": [_mini(o) for o in best],
            "ont_options": [
                {
                    "id": o["id"],
                    "ont_sn": o.get("ont_sn"),
                    "customer": o.get("customer"),
                    "rx_power": o.get("rx_power"),
                }
                for o in opts
            ],
            "olt_names": sorted(
                {(o.get("olt_name") or "").strip() for o in enriched} - {""}
            ),
            "odp_names": sorted(
                {(o.get("odp_name") or "").strip() for o in enriched} - {""}
            ),
            "degrade_days": _ddays,
            "degrade_thresh_db": _dthresh,
        }
    )


@fiber_bp.route("/api/fiber", methods=["POST"])
@api_login_required
def api_fiber_create():
    vals, err = _validate_fiber(request.get_json(silent=True) or {})
    if err:
        return jsonify({"error": err}), 400
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    status, _, _, _ = fiber_eval(vals["rx_power"], vals["tx_power"], _th_for_ont(vals))
    has_measurement = vals["rx_power"] is not None or vals["tx_power"] is not None
    conn, c = get_db()
    try:
        try:
            c.execute(
                """INSERT INTO fiber_onts(ont_sn,customer,olt_name,pon_port,odp_name,
                       rx_power,tx_power,status,last_checked,source,created_at,updated_at,
                       mute_alarm,mute_until,mute_reason,rx_warn,rx_crit,ont_index,last_seen)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?, ?,?,?,?,?,?,?,?)""",
                (
                    vals["ont_sn"],
                    vals["customer"],
                    vals["olt_name"],
                    vals["pon_port"],
                    vals["odp_name"],
                    vals["rx_power"],
                    vals["tx_power"],
                    status,
                    now if has_measurement else None,
                    vals["source"],
                    now,
                    now,
                    vals["mute_alarm"],
                    vals["mute_until"],
                    vals["mute_reason"],
                    vals["rx_warn"],
                    vals["rx_crit"],
                    vals["ont_index"],
                    now if has_measurement else None,
                ),
            )
            fid = c.lastrowid
            if has_measurement:
                c.execute(
                    "INSERT INTO fiber_history (ont_id, rx_power, tx_power, timestamp) VALUES (?,?,?,?)",
                    (fid, vals["rx_power"], vals["tx_power"], now),
                )
            conn.commit()
        except sqlite3.IntegrityError:
            try:
                conn.rollback()
            except Exception:
                pass
            return jsonify({"error": "ONT SN sudah terdaftar"}), 400
    finally:
        try:
            conn.close()
        except Exception:
            pass
    try:
        audit(
            current_user.username,
            "fiber.create",
            f"{vals['ont_sn']} rx={vals['rx_power']}",
        )
    except Exception:
        pass
    try:
        _fiber_single_check(fid)
    except Exception as e:
        print(f"[FIBER] single check create gagal: {e}")
    return jsonify({"status": "success", "id": fid}), 201


@fiber_bp.route("/api/fiber/<int:fid>", methods=["PUT"])
@api_login_required
def api_fiber_update(fid):
    vals, err = _validate_fiber(request.get_json(silent=True) or {})
    if err:
        return jsonify({"error": err}), 400
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    status, _, _, _ = fiber_eval(vals["rx_power"], vals["tx_power"], _th_for_ont(vals))
    has_measurement = vals["rx_power"] is not None or vals["tx_power"] is not None
    conn, c = get_db()
    try:
        c.execute("SELECT id, last_seen FROM fiber_onts WHERE id=?", (fid,))
        _old = c.fetchone()
        if not _old:
            return jsonify({"error": "ONT tidak ditemukan"}), 404
        last_seen_new = now if has_measurement else (_old["last_seen"] or None)
        try:
            c.execute(
                """UPDATE fiber_onts SET ont_sn=?, customer=?, olt_name=?, pon_port=?, odp_name=?,
                         rx_power=?, tx_power=?, status=?, last_checked=?, source=?, updated_at=?,
                         mute_alarm=?, mute_until=?, mute_reason=?, rx_warn=?, rx_crit=?, ont_index=?,
                         last_seen=?
                         WHERE id=?""",
                (
                    vals["ont_sn"],
                    vals["customer"],
                    vals["olt_name"],
                    vals["pon_port"],
                    vals["odp_name"],
                    vals["rx_power"],
                    vals["tx_power"],
                    status,
                    now if has_measurement else None,
                    vals["source"],
                    now,
                    vals["mute_alarm"],
                    vals["mute_until"],
                    vals["mute_reason"],
                    vals["rx_warn"],
                    vals["rx_crit"],
                    vals["ont_index"],
                    last_seen_new,
                    fid,
                ),
            )
            if has_measurement:
                c.execute(
                    "INSERT INTO fiber_history (ont_id, rx_power, tx_power, timestamp) VALUES (?,?,?,?)",
                    (fid, vals["rx_power"], vals["tx_power"], now),
                )
            conn.commit()
        except sqlite3.IntegrityError:
            try:
                conn.rollback()
            except Exception:
                pass
            return jsonify({"error": "ONT SN dipakai data lain"}), 400
    finally:
        try:
            conn.close()
        except Exception:
            pass
    try:
        audit(current_user.username, "fiber.update", f"id={fid} rx={vals['rx_power']}")
    except Exception:
        pass
    try:
        _fiber_single_check(fid)
    except Exception as e:
        print(f"[FIBER] single check update gagal: {e}")
    return jsonify({"status": "success"})


@fiber_bp.route("/api/fiber/<int:fid>", methods=["DELETE"])
@api_login_required
def api_fiber_delete(fid):
    conn, c = get_db()
    c.execute("DELETE FROM fiber_onts WHERE id=?", (fid,))
    deleted = c.rowcount
    try:
        c.execute("DELETE FROM fiber_history WHERE ont_id=?", (fid,))
        c.execute("DELETE FROM fiber_downtime WHERE ont_id=?", (fid,))
    except sqlite3.OperationalError:
        pass
    conn.commit()
    conn.close()
    for _mem in (
        fiber_alarm_memory,
        fiber_degrade_memory,
        fiber_flap_memory,
        fiber_degrade_tg,
    ):
        try:
            _mem.pop(fid, None)
        except Exception:
            pass
    if not deleted:
        return jsonify({"error": "ONT tidak ditemukan"}), 404
    try:
        audit(current_user.username, "fiber.delete", f"id={fid}")
    except Exception:
        pass
    return jsonify({"status": "success"})


@fiber_bp.route("/api/fiber/<int:fid>/history")
@api_login_required
def api_fiber_history(fid):
    try:
        hours = int(request.args.get("hours", 24))
    except (ValueError, TypeError):
        return jsonify({"error": "hours harus angka 1-720"}), 400
    hours = max(1, min(hours, 720))
    conn, c = get_db()
    try:
        c.execute("SELECT id, ont_sn, customer FROM fiber_onts WHERE id=?", (fid,))
        ont = c.fetchone()
        if not ont:
            return jsonify({"error": "ONT tidak ditemukan"}), 404
        c.execute(
            "SELECT timestamp, rx_power, tx_power FROM fiber_history "
            "WHERE ont_id=? AND timestamp > datetime('now','localtime',?) ORDER BY id ASC LIMIT 25000",
            (fid, f"-{hours} hours"),
        )
        rows = c.fetchall()
    finally:
        conn.close()
    if len(rows) > 500:
        step = (len(rows) + 499) // 500
        rows = rows[::step]
    if hours <= 24:
        labels = [
            (
                r["timestamp"].split(" ")[1]
                if r["timestamp"] and " " in r["timestamp"]
                else r["timestamp"]
            )
            for r in rows
        ]
    else:
        labels = [
            (
                r["timestamp"][5:16]
                if r["timestamp"] and len(r["timestamp"]) >= 16
                else r["timestamp"]
            )
            for r in rows
        ]
    return jsonify(
        {
            "ont": dict(ont),
            "hours": hours,
            "labels": labels,
            "rx": [r["rx_power"] for r in rows],
            "tx": [r["tx_power"] for r in rows],
            "count": len(rows),
        }
    )


@fiber_bp.route("/api/fiber/<int:fid>/history/export")
@api_login_required
def api_fiber_history_export(fid):
    """Export history Rx/Tx satu ONT ke CSV (maks 720 jam / 30 hari)."""
    try:
        hours = int(request.args.get("hours", 168))
    except (ValueError, TypeError):
        return jsonify({"error": "hours harus angka 1-720"}), 400
    hours = max(1, min(hours, 720))
    conn, c = get_db()
    try:
        c.execute("SELECT id, ont_sn, customer FROM fiber_onts WHERE id=?", (fid,))
        ont = c.fetchone()
        if not ont:
            return jsonify({"error": "ONT tidak ditemukan"}), 404
        c.execute(
            "SELECT timestamp, rx_power, tx_power FROM fiber_history "
            "WHERE ont_id=? AND timestamp > datetime('now','localtime',?) ORDER BY id ASC LIMIT 25000",
            (fid, f"-{hours} hours"),
        )
        rows = [dict(r) for r in c.fetchall()]
    finally:
        try:
            conn.close()
        except Exception:
            pass
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["timestamp", "rx_dbm", "tx_dbm"])
    for r in rows:
        w.writerow([r["timestamp"], r["rx_power"], r["tx_power"]])
    try:
        audit(
            current_user.username,
            "fiber.history_export",
            f"{ont['ont_sn']} rows={len(rows)} hours={hours}",
        )
    except Exception:
        pass
    safe_sn = re.sub(r"[^A-Za-z0-9_.\-]", "_", ont["ont_sn"] or "ont")[:48]
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={
            "Content-Disposition": f"attachment; filename=history_{safe_sn}_{hours}h.csv"
        },
    )


def _fiber_open_downtime_map():
    """{ont_id: started_at} catatan downtime yang masih terbuka (1 query)."""
    try:
        conn, c = get_db()
        try:
            c.execute(
                "SELECT ont_id, started_at FROM fiber_downtime WHERE resolved_at IS NULL"
            )
            return {r["ont_id"]: r["started_at"] for r in c.fetchall()}
        finally:
            conn.close()
    except Exception:
        return {}


@fiber_bp.route("/api/fiber/<int:fid>/downtime", methods=["GET"])
@api_login_required
def api_fiber_downtime(fid):
    conn, c = get_db()
    try:
        c.execute("SELECT id FROM fiber_onts WHERE id=?", (fid,))
        if not c.fetchone():
            return jsonify({"error": "ONT tidak ditemukan"}), 404
        c.execute(
            "SELECT id, ont_sn, status, rx_dbm, started_at, resolved_at, duration_s"
            " FROM fiber_downtime WHERE ont_id=? ORDER BY id DESC LIMIT 100",
            (fid,),
        )
        out = []
        for r in c.fetchall():
            out.append(
                {
                    "id": r["id"],
                    "ont_sn": r["ont_sn"],
                    "status": r["status"],
                    "rx_dbm": r["rx_dbm"],
                    "started_at": r["started_at"],
                    "resolved_at": r["resolved_at"] or "Ongoing",
                    "duration_s": r["duration_s"],
                    "duration": _fmt_duration(r["duration_s"]),
                    "state": "resolved" if r["resolved_at"] else "ongoing",
                }
            )
    finally:
        conn.close()
    return jsonify(out)


@fiber_bp.route("/api/fiber/<int:fid>/sla", methods=["GET"])
@api_login_required
def api_fiber_sla(fid):
    """SLA ONT dari catatan downtime. ?days=7|14|30|90 (default 30)."""
    try:
        days = int(request.args.get("days", 30))
    except (ValueError, TypeError):
        return jsonify({"error": "days harus angka"}), 400
    if days not in (7, 14, 30, 90):
        return jsonify({"error": "days harus salah satu 7/14/30/90"}), 400
    now = datetime.now()
    win_start = now - timedelta(days=days)
    win_s = days * 86400
    conn, c = get_db()
    try:
        c.execute("SELECT id, ont_sn FROM fiber_onts WHERE id=?", (fid,))
        ont = c.fetchone()
        if not ont:
            return jsonify({"error": "ONT tidak ditemukan"}), 404
        c.execute(
            "SELECT started_at, resolved_at, duration_s FROM fiber_downtime"
            " WHERE ont_id=? AND (resolved_at IS NULL OR resolved_at >= ?)"
            " ORDER BY id ASC",
            (fid, win_start.strftime("%Y-%m-%d %H:%M:%S")),
        )
        rows = [dict(r) for r in c.fetchall()]
    finally:
        conn.close()
    down_s, incidents, longest = 0, 0, 0
    for r in rows:
        try:
            s = datetime.strptime(r["started_at"], "%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            continue
        try:
            e = (
                datetime.strptime(r["resolved_at"], "%Y-%m-%d %H:%M:%S")
                if r["resolved_at"]
                else now
            )
        except (ValueError, TypeError):
            e = now
        s = max(s, win_start)
        overlap = max(0, int((min(e, now) - s).total_seconds()))
        if overlap <= 0:
            continue
        down_s += overlap
        incidents += 1
        longest = max(longest, overlap)
    uptime_pct = round(max(0.0, (win_s - down_s) / win_s * 100), 2)
    return jsonify(
        {
            "ont": {"id": ont["id"], "ont_sn": ont["ont_sn"]},
            "days": days,
            "window_s": win_s,
            "uptime_pct": uptime_pct,
            "incidents": incidents,
            "total_downtime_s": down_s,
            "total_downtime_str": _fmt_duration(down_s),
            "longest_s": longest,
            "longest_str": _fmt_duration(longest),
        }
    )


@fiber_bp.route("/api/fiber/export")
@api_login_required
def api_fiber_export():
    conn, c = get_db()
    try:
        c.execute("SELECT * FROM fiber_onts ORDER BY olt_name ASC, id ASC")
        rows = [dict(r) for r in c.fetchall()]
    except sqlite3.OperationalError:
        rows = []
    finally:
        try:
            conn.close()
        except Exception:
            pass
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(
        [
            "ont_sn",
            "customer",
            "olt",
            "pon_port",
            "odp",
            "rx_dbm",
            "tx_dbm",
            "status",
            "saran",
            "last_checked",
            "source",
            "mute_alarm",
            "mute_until",
            "mute_reason",
        ]
    )
    for o in rows:
        status, _, advice, _need = fiber_eval(
            o.get("rx_power"), o.get("tx_power"), _th_for_ont(o)
        )
        w.writerow(
            [
                _csv_safe(o.get("ont_sn")),
                _csv_safe(o.get("customer")),
                _csv_safe(o.get("olt_name")),
                _csv_safe(o.get("pon_port")),
                _csv_safe(o.get("odp_name")),
                o.get("rx_power"),
                o.get("tx_power"),
                status,
                _csv_safe(advice),
                _csv_safe(o.get("last_checked")),
                _csv_safe(o.get("source")),
                o.get("mute_alarm") or 0,
                _csv_safe(o.get("mute_until")),
                _csv_safe(o.get("mute_reason")),
            ]
        )
    try:
        audit(current_user.username, "fiber.export", f"rows={len(rows)}")
    except Exception:
        pass
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=fiber_redaman.csv"},
    )


@fiber_bp.route("/api/fiber/import", methods=["POST"])
@api_login_required
def api_fiber_import():
    """Import massal ONT dari CSV (hindari input satu-satu).

    Kolom: ont_sn*,customer,olt_name,pon_port,odp_name,rx_power,tx_power,
    source,mute_alarm,mute_until,mute_reason,rx_warn,rx_crit,ont_index (*wajib).
    Mode via form/query 'mode': 'skip' (default, duplikat SN dilewati) atau
    'upsert'/'update' (duplikat SN diperbarui + snapshot history baru).
    Semantik upsert = merge: hanya kolom yang ADA dan TERISI di CSV yang
    ditimpa; sel kosong / kolom tak ada mempertahankan nilai lama (aman untuk
    update massal Rx hasil OPM tanpa menghapus customer/ODP).
    Memory alarm di-seed diam-diam agar import massal tak membanjiri Telegram;
    alarm tetap tampil di triggers dan telegram dikirim saat ada perubahan
    berikutnya.
    """
    mode = (
        (
            (request.form.get("mode") if request.form else None)
            or request.args.get("mode")
            or "skip"
        )
        .strip()
        .lower()
    )
    if mode not in ("skip", "upsert", "update"):
        return jsonify({"error": "mode harus 'skip' atau 'upsert'"}), 400
    do_upsert = mode in ("upsert", "update")
    if "file" not in request.files:
        return jsonify({"error": "File CSV wajib diunggah (field 'file')"}), 400
    try:
        raw = request.files["file"].read(1000000 + 1)
    except Exception:
        return jsonify({"error": "Gagal membaca file"}), 400
    if not raw or len(raw) > 1000000:
        return jsonify({"error": "File kosong atau melebihi 1 MB"}), 400
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return jsonify({"error": "File harus CSV UTF-8"}), 400
    try:
        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames or "ont_sn" not in [
            h.strip() for h in reader.fieldnames
        ]:
            return jsonify({"error": "Header CSV harus memuat kolom 'ont_sn'"}), 400
        rows = [r for _, r in zip(range(501), reader)]
    except Exception:
        return jsonify({"error": "Format CSV tidak valid"}), 400
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    created, updated, skipped, errors = 0, 0, 0, []
    with db_lock:
        conn, c = get_db()
        try:
            for i, r in enumerate(rows, start=2):
                vals, err = _validate_fiber(
                    {
                        k.strip(): (v.strip() if isinstance(v, str) else v)
                        for k, v in (r or {}).items()
                        if k
                    }
                )
                if err:
                    errors.append(f"baris {i}: {err}")
                    continue
                try:
                    c.execute(
                        "SELECT id FROM fiber_onts WHERE ont_sn=?", (vals["ont_sn"],)
                    )
                    existing = c.fetchone()
                    if existing:
                        if not do_upsert:
                            skipped += 1
                            continue
                        fid = existing["id"]
                        raw = {
                            (k.strip() if isinstance(k, str) else k): v
                            for k, v in (r or {}).items()
                            if k
                        }
                        c.execute("SELECT * FROM fiber_onts WHERE id=?", (fid,))
                        cur = dict(c.fetchone())
                        merged = dict(vals)
                        for _f in (
                            "customer",
                            "olt_name",
                            "pon_port",
                            "odp_name",
                            "rx_power",
                            "tx_power",
                            "source",
                            "mute_alarm",
                            "mute_until",
                            "mute_reason",
                            "rx_warn",
                            "rx_crit",
                            "ont_index",
                        ):
                            _rv = raw.get(_f)
                            if _rv is None or (
                                isinstance(_rv, str) and not _rv.strip()
                            ):
                                merged[_f] = cur.get(_f)
                        if merged.get("mute_until"):
                            try:
                                _dt = _parse_maint_time(str(merged["mute_until"]))
                                merged["mute_until"] = (
                                    _fmt_maint_time(_dt) if _dt else ""
                                )
                            except Exception:
                                merged["mute_until"] = cur.get("mute_until") or ""
                        _raw_rx = raw.get("rx_power")
                        _raw_tx = raw.get("tx_power")
                        _has_new_meas = (
                            _raw_rx is not None
                            and not (isinstance(_raw_rx, str) and not _raw_rx.strip())
                        ) or (
                            _raw_tx is not None
                            and not (isinstance(_raw_tx, str) and not _raw_tx.strip())
                        )
                        merged["last_seen"] = (
                            now if _has_new_meas else (cur.get("last_seen") or None)
                        )
                        has_meas = (
                            merged["rx_power"] is not None
                            or merged["tx_power"] is not None
                        )
                        status, _, _, _ = fiber_eval(
                            merged["rx_power"], merged["tx_power"], _th_for_ont(merged)
                        )
                        c.execute(
                            """UPDATE fiber_onts SET customer=?, olt_name=?, pon_port=?, odp_name=?,
                                     rx_power=?, tx_power=?, status=?, last_checked=?, source=?,
                                     updated_at=?, mute_alarm=?, mute_until=?, mute_reason=?,
                                     rx_warn=?, rx_crit=?, ont_index=?, last_seen=? WHERE id=?""",
                            (
                                merged["customer"],
                                merged["olt_name"],
                                merged["pon_port"],
                                merged["odp_name"],
                                merged["rx_power"],
                                merged["tx_power"],
                                status,
                                now if has_meas else None,
                                merged["source"],
                                now,
                                merged["mute_alarm"],
                                merged["mute_until"],
                                merged["mute_reason"],
                                merged["rx_warn"],
                                merged["rx_crit"],
                                merged["ont_index"],
                                merged["last_seen"],
                                fid,
                            ),
                        )
                        if has_meas:
                            c.execute(
                                "INSERT INTO fiber_history (ont_id, rx_power, tx_power, timestamp) VALUES (?,?,?,?)",
                                (fid, merged["rx_power"], merged["tx_power"], now),
                            )
                        fiber_alarm_memory[fid] = status
                        updated += 1
                        continue
                    status, _, _, _ = fiber_eval(
                        vals["rx_power"], vals["tx_power"], _th_for_ont(vals)
                    )
                    has_meas_imp = (
                        vals["rx_power"] is not None or vals["tx_power"] is not None
                    )
                    c.execute(
                        """INSERT INTO fiber_onts(ont_sn,customer,olt_name,pon_port,odp_name,
                               rx_power,tx_power,status,last_checked,source,created_at,updated_at,
                               mute_alarm,mute_until,mute_reason,rx_warn,rx_crit,ont_index,last_seen)
                               VALUES(?,?,?,?,?,?,?,?,?,?,?, ?,?,?,?,?,?,?,?)""",
                        (
                            vals["ont_sn"],
                            vals["customer"],
                            vals["olt_name"],
                            vals["pon_port"],
                            vals["odp_name"],
                            vals["rx_power"],
                            vals["tx_power"],
                            status,
                            now if has_meas_imp else None,
                            vals["source"],
                            now,
                            now,
                            vals["mute_alarm"],
                            vals["mute_until"],
                            vals["mute_reason"],
                            vals["rx_warn"],
                            vals["rx_crit"],
                            vals["ont_index"],
                            now if has_meas_imp else None,
                        ),
                    )
                    fid = c.lastrowid
                    if vals["rx_power"] is not None or vals["tx_power"] is not None:
                        c.execute(
                            "INSERT INTO fiber_history (ont_id, rx_power, tx_power, timestamp) VALUES (?,?,?,?)",
                            (fid, vals["rx_power"], vals["tx_power"], now),
                        )
                    fiber_alarm_memory[fid] = status
                    created += 1
                except sqlite3.IntegrityError:
                    skipped += 1
                except Exception as e:
                    errors.append(f"baris {i}: {e}")
            _commit_with_retry(conn)
        finally:
            conn.close()
    try:
        audit(
            current_user.username,
            "fiber.import",
            f"created={created} updated={updated} skipped={skipped}",
        )
    except Exception:
        pass
    return jsonify(
        {
            "status": "success",
            "created": created,
            "updated": updated,
            "skipped": skipped,
            "errors": errors[:20],
        }
    )


def _validate_odp(d):
    name = (d.get("name") or "").strip()[:100]
    if len(name) < 2 or not re.match(r"^[A-Za-z0-9 _.\-/]{2,100}$", name):
        return None, "Nama ODP 2-100 karakter (huruf/angka/spasi/_-./)"
    try:
        capacity = int(d.get("capacity", 8))
    except (ValueError, TypeError):
        return None, "Kapasitas harus angka 1-128"
    if not 1 <= capacity <= 128:
        return None, "Kapasitas harus 1-128 port"
    lat, lon, err = _parse_latlon(d)
    if err:
        return None, err
    return {
        "name": name,
        "olt_name": str(d.get("olt_name") or "").strip()[:100],
        "capacity": capacity,
        "location": str(d.get("location") or "").strip()[:100],
        "lat": lat,
        "lon": lon,
    }, None


def _odp_aggregation():
    """Kembalikan list ODP + hitungan ONT per status (link by odp_name)."""
    conn, c = get_db()
    try:
        try:
            c.execute("SELECT * FROM odps ORDER BY name ASC")
            odps = [dict(r) for r in c.fetchall()]
        except sqlite3.OperationalError:
            return []
        try:
            c.execute(
                "SELECT odp_name, rx_power, tx_power, rx_warn, rx_crit, last_seen, source FROM fiber_onts"
            )
            onts = [dict(r) for r in c.fetchall()]
        except sqlite3.OperationalError:
            onts = []
    finally:
        try:
            conn.close()
        except Exception:
            pass
    known = {o["name"].lower(): o["name"] for o in odps}
    buckets = {
        o["name"]: {
            "total": 0,
            "normal": 0,
            "warning": 0,
            "critical": 0,
            "overload": 0,
            "stale": 0,
            "unknown": 0,
        }
        for o in odps
    }
    buckets[""] = {
        "total": 0,
        "normal": 0,
        "warning": 0,
        "critical": 0,
        "overload": 0,
        "stale": 0,
        "unknown": 0,
    }
    for t in onts:
        key = known.get((t.get("odp_name") or "").strip().lower(), "")
        status, _, _, _ = fiber_eval(
            t.get("rx_power"), t.get("tx_power"), _th_for_ont(t)
        )
        if status in ("normal", "warning", "unknown") and _fiber_stale_info(t)[0]:
            status = "stale"
        b = buckets[key]
        b["total"] += 1
        b[status if status in b else "unknown"] += 1
    rank = {
        "critical": 4,
        "overload": 3,
        "warning": 2,
        "stale": 2,
        "unknown": 1,
        "normal": 0,
    }
    out = []
    for o in odps:
        b = buckets[o["name"]]
        worst = max(
            (k for k in b if k != "total" and b[k] > 0),
            key=lambda k: rank.get(k, 0),
            default="normal",
        )
        fill = round(b["total"] / o["capacity"] * 100, 1) if o["capacity"] else 0
        out.append({**o, **b, "worst": worst, "fill_pct": fill})
    unb = buckets[""]
    if unb["total"]:
        worst = max(
            (k for k in unb if k != "total" and unb[k] > 0),
            key=lambda k: rank.get(k, 0),
            default="normal",
        )
        out.append(
            {
                "id": 0,
                "name": "(tanpa ODP)",
                "olt_name": "",
                "capacity": 0,
                "location": "",
                **unb,
                "worst": worst,
                "fill_pct": 0,
            }
        )
    return out


@fiber_bp.route("/api/odps", methods=["GET"])
@api_login_required
def api_odp_list():
    return jsonify(_odp_aggregation())


@fiber_bp.route("/api/odps", methods=["POST"])
@api_login_required
def api_odp_create():
    vals, err = _validate_odp(request.get_json(silent=True) or {})
    if err:
        return jsonify({"error": err}), 400
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn, c = get_db()
    c.execute("SELECT id FROM odps WHERE name COLLATE NOCASE = ?", (vals["name"],))
    if c.fetchone():
        conn.close()
        return jsonify({"error": "Nama ODP sudah terdaftar"}), 400
    try:
        c.execute(
            "INSERT INTO odps (name, olt_name, capacity, location, lat, lon, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (
                vals["name"],
                vals["olt_name"],
                vals["capacity"],
                vals["location"],
                vals["lat"],
                vals["lon"],
                now,
                now,
            ),
        )
        nid = c.lastrowid
        conn.commit()
    except sqlite3.IntegrityError:
        try:
            conn.rollback()
        except Exception:
            pass
        conn.close()
        return jsonify({"error": "Nama ODP sudah terdaftar"}), 400
    conn.close()
    try:
        audit(current_user.username, "odp.create", vals["name"])
    except Exception:
        pass
    return jsonify({"status": "success", "id": nid}), 201


@fiber_bp.route("/api/odps/<int:oid>", methods=["PUT"])
@api_login_required
def api_odp_update(oid):
    vals, err = _validate_odp(request.get_json(silent=True) or {})
    if err:
        return jsonify({"error": err}), 400
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn, c = get_db()
    c.execute("SELECT name FROM odps WHERE id=?", (oid,))
    old = c.fetchone()
    if not old:
        conn.close()
        return jsonify({"error": "ODP tidak ditemukan"}), 404
    c.execute(
        "SELECT id FROM odps WHERE name COLLATE NOCASE = ? AND id != ?",
        (vals["name"], oid),
    )
    if c.fetchone():
        conn.close()
        return jsonify({"error": "Nama ODP dipakai data lain"}), 400
    try:
        c.execute(
            "UPDATE odps SET name=?, olt_name=?, capacity=?, location=?, lat=?, lon=?, updated_at=? WHERE id=?",
            (
                vals["name"],
                vals["olt_name"],
                vals["capacity"],
                vals["location"],
                vals["lat"],
                vals["lon"],
                now,
                oid,
            ),
        )
        if old["name"].lower() != vals["name"].lower():
            c.execute(
                "UPDATE fiber_onts SET odp_name=? WHERE odp_name COLLATE NOCASE = ?",
                (vals["name"], old["name"]),
            )
        conn.commit()
    except sqlite3.IntegrityError:
        try:
            conn.rollback()
        except Exception:
            pass
        conn.close()
        return jsonify({"error": "Nama ODP dipakai data lain"}), 400
    conn.close()
    try:
        audit(current_user.username, "odp.update", f"id={oid} {vals['name']}")
    except Exception:
        pass
    return jsonify({"status": "success"})


@fiber_bp.route("/api/odps/<int:oid>", methods=["DELETE"])
@api_login_required
def api_odp_delete(oid):
    conn, c = get_db()
    c.execute("SELECT name FROM odps WHERE id=?", (oid,))
    row = c.fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "ODP tidak ditemukan"}), 404
    c.execute("DELETE FROM odps WHERE id=?", (oid,))
    c.execute(
        "UPDATE fiber_onts SET odp_name='' WHERE odp_name COLLATE NOCASE = ?",
        (row["name"],),
    )
    conn.commit()
    conn.close()
    try:
        audit(current_user.username, "odp.delete", f"id={oid} {row['name']}")
    except Exception:
        pass
    return jsonify({"status": "success"})


def _validate_olt(d):
    name = (d.get("name") or "").strip()[:100]
    if len(name) < 2 or not re.match(r"^[A-Za-z0-9 _.\-/]{2,100}$", name):
        return None, "Nama OLT 2-100 karakter (huruf/angka/spasi/_-./)"
    ip = str(d.get("ip") or "").strip()[:64]
    if ip:
        try:
            ipaddress.ip_address(ip)
        except ValueError:
            return None, "IP OLT tidak valid"
    community = str(d.get("community") or "").strip()[:128]
    vendor = str(d.get("vendor") or "generic").strip().lower()[:16]
    if vendor not in ("zte", "huawei", "generic"):
        vendor = "generic"
    rx_base = str(d.get("rx_base") or "").strip().strip(".")[:128]
    tx_base = str(d.get("tx_base") or "").strip().strip(".")[:128]
    if rx_base and not _valid_oid(rx_base):
        return None, "rx_base bukan OID valid"
    if tx_base and not _valid_oid(tx_base):
        return None, "tx_base bukan OID valid"
    try:
        div = float(d.get("div", 100))
    except (ValueError, TypeError):
        return None, "div harus angka 1-10000"
    if not 1 <= div <= 10000:
        return None, "div harus 1-10000"
    try:
        scale = float(d.get("scale", 1.0))
    except (ValueError, TypeError):
        return None, "scale harus angka > 0"
    if not 1e-9 <= scale <= 1e6:
        return None, "scale harus 1e-9..1e6 (cth ZTE 0.002, Huawei 0.01)"
    try:
        offset = float(d.get("offset", 0.0))
    except (ValueError, TypeError):
        return None, "offset harus angka -10000..10000"
    if not -10000 <= offset <= 10000:
        return None, "offset harus -10000..10000 dB"
    lat, lon, err = _parse_latlon(d)
    if err:
        return None, err
    return {
        "name": name,
        "ip": ip,
        "community": community,
        "vendor": vendor,
        "rx_base": rx_base,
        "tx_base": tx_base,
        "div": div,
        "scale": scale,
        "offset": offset,
        "lat": lat,
        "lon": lon,
    }, None


def _apply_olt_preset(vals, raw_data):
    """Isi field kosong dari preset vendor (dipakai saat create OLT).

    rx/tx yang kosong -> preset. div/scale/offset -> preset hanya bila user
    tak mengirim ketiganya sama sekali (transform utuh default vendor);
    bila user menyentuh salah satunya, seluruhnya dihormati apa adanya
    agar kustomisasi tak tertimpa diam-diam.
    """
    p = OLT_VENDOR_PRESETS.get(vals.get("vendor") or "generic")
    if not p or vals.get("vendor") == "generic":
        return vals
    vals = dict(vals)
    if not vals.get("rx_base") and p.get("rx_base"):
        vals["rx_base"] = p["rx_base"]
    if not vals.get("tx_base") and p.get("tx_base"):
        vals["tx_base"] = p["tx_base"]
    raw_data = raw_data or {}
    if all(raw_data.get(k) is None for k in ("div", "scale", "offset")):
        for k in ("div", "scale", "offset"):
            if p.get(k) is not None:
                vals[k] = p[k]
    return vals


@fiber_bp.route("/api/olts", methods=["GET"])
@api_login_required
def api_olt_list():
    conn, c = get_db()
    try:
        try:
            c.execute(
                "SELECT id, name, ip, vendor, rx_base, tx_base, div, scale, offset, lat, lon,"
                " last_tested, last_test_ok, last_test_msg FROM olts ORDER BY name ASC"
            )
            rows = [dict(r) for r in c.fetchall()]
        except sqlite3.OperationalError:
            rows = []
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return jsonify(rows)


@fiber_bp.route("/api/olts", methods=["POST"])
@api_login_required
def api_olt_create():
    raw = request.get_json(silent=True) or {}
    vals, err = _validate_olt(raw)
    if err:
        return jsonify({"error": err}), 400
    vals = _apply_olt_preset(vals, raw)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn, c = get_db()
    try:
        c.execute("SELECT id FROM olts WHERE name COLLATE NOCASE = ?", (vals["name"],))
        if c.fetchone():
            return jsonify({"error": "Nama OLT sudah terdaftar"}), 400
        try:
            c.execute(
                "INSERT INTO olts (name, ip, community, vendor, rx_base, tx_base, div,"
                " scale, offset, lat, lon, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    vals["name"],
                    vals["ip"],
                    vals["community"],
                    vals["vendor"],
                    vals["rx_base"],
                    vals["tx_base"],
                    vals["div"],
                    vals["scale"],
                    vals["offset"],
                    vals["lat"],
                    vals["lon"],
                    now,
                    now,
                ),
            )
            nid = c.lastrowid
            conn.commit()
        except sqlite3.IntegrityError:
            try:
                conn.rollback()
            except Exception:
                pass
            return jsonify({"error": "Nama OLT sudah terdaftar"}), 400
    finally:
        try:
            conn.close()
        except Exception:
            pass
    try:
        audit(current_user.username, "olt.create", vals["name"])
    except Exception:
        pass
    return jsonify({"status": "success", "id": nid}), 201


@fiber_bp.route("/api/olts/<int:oid>", methods=["PUT"])
@api_login_required
def api_olt_update(oid):
    vals, err = _validate_olt(request.get_json(silent=True) or {})
    if err:
        return jsonify({"error": err}), 400
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn, c = get_db()
    try:
        c.execute("SELECT name, community FROM olts WHERE id=?", (oid,))
        old = c.fetchone()
        if not old:
            return jsonify({"error": "OLT tidak ditemukan"}), 404
        if not vals["community"] and old["community"]:
            vals["community"] = old["community"]
        c.execute(
            "SELECT id FROM olts WHERE name COLLATE NOCASE = ? AND id != ?",
            (vals["name"], oid),
        )
        if c.fetchone():
            return jsonify({"error": "Nama OLT dipakai data lain"}), 400
        try:
            c.execute(
                "UPDATE olts SET name=?, ip=?, community=?, vendor=?, rx_base=?, tx_base=?,"
                " div=?, scale=?, offset=?, lat=?, lon=?, updated_at=? WHERE id=?",
                (
                    vals["name"],
                    vals["ip"],
                    vals["community"],
                    vals["vendor"],
                    vals["rx_base"],
                    vals["tx_base"],
                    vals["div"],
                    vals["scale"],
                    vals["offset"],
                    vals["lat"],
                    vals["lon"],
                    now,
                    oid,
                ),
            )
            if old["name"].lower() != vals["name"].lower():
                c.execute(
                    "UPDATE fiber_onts SET olt_name=? WHERE olt_name COLLATE NOCASE = ?",
                    (vals["name"], old["name"]),
                )
            conn.commit()
        except sqlite3.IntegrityError:
            try:
                conn.rollback()
            except Exception:
                pass
            return jsonify({"error": "Nama OLT dipakai data lain"}), 400
    finally:
        try:
            conn.close()
        except Exception:
            pass
    try:
        audit(current_user.username, "olt.update", f"id={oid} {vals['name']}")
    except Exception:
        pass
    return jsonify({"status": "success"})


@fiber_bp.route("/api/olts/<int:oid>", methods=["DELETE"])
@api_login_required
def api_olt_delete(oid):
    conn, c = get_db()
    try:
        c.execute("SELECT name FROM olts WHERE id=?", (oid,))
        row = c.fetchone()
        if not row:
            return jsonify({"error": "OLT tidak ditemukan"}), 404
        c.execute("DELETE FROM olts WHERE id=?", (oid,))
        conn.commit()
    finally:
        try:
            conn.close()
        except Exception:
            pass
    try:
        audit(current_user.username, "olt.delete", f"id={oid} {row['name']}")
    except Exception:
        pass
    return jsonify({"status": "success"})


@fiber_bp.route("/api/olts/presets", methods=["GET"])
@api_login_required
def api_olt_presets():
    return jsonify(
        {k: {kk: vv for kk, vv in v.items()} for k, v in OLT_VENDOR_PRESETS.items()}
    )


def _olt_test_connection(olt, timeout=3.0):
    """Uji SNMP ke OLT: reachability + sampel satu entri Rx/Tx.

    Kembalikan dict {reachable, sysup_s, rx, tx, ok, message, elapsed_ms}.
    rx/tx berisi {ok, oid, index, raw, dbm} atau None bila base tak diisi.
    """
    import time as _t

    t0 = _t.time()
    ip = (olt.get("ip") or "").strip()
    comm = (olt.get("community") or "").strip()
    res: dict = {
        "reachable": False,
        "sysup_s": None,
        "rx": None,
        "tx": None,
        "ok": False,
        "message": "",
        "elapsed_ms": 0,
    }
    if not ip or not comm:
        res["message"] = "IP/community OLT belum diisi"
        return res
    try:
        vals = _snmp_get(ip, comm, [SYSUP_OID], timeout=timeout)
    except Exception as e:
        res["message"] = f"SNMP error: {e}"
        res["elapsed_ms"] = int((_t.time() - t0) * 1000)
        return res
    if not vals or vals[0] is None or vals[0] < 0:
        res["message"] = "OLT tak menjawab (cek IP/community/SNMP aktif)"
        res["elapsed_ms"] = int((_t.time() - t0) * 1000)
        return res
    res["reachable"] = True
    try:
        res["sysup_s"] = round(vals[0] / 100.0, 1)
    except (ValueError, TypeError):
        pass
    for key, base in (
        ("rx", (olt.get("rx_base") or "").strip().strip(".")),
        ("tx", (olt.get("tx_base") or "").strip().strip(".")),
    ):
        if not _valid_oid(base):
            continue
        try:
            oid, _tag, ival, sval = _snmp_getnext(ip, comm, base, timeout=timeout)
        except Exception:
            oid, ival, sval = None, None, None
        if not oid:
            res[key] = {"ok": False, "error": "OID tak menjawab — cek rx/tx_base"}
            continue
        suffix = oid[len(base) :].lstrip(".")
        raw = ival
        if raw is None and sval not in (None, ""):
            try:
                raw = float(sval)
            except (ValueError, TypeError):
                raw = None
        res[key] = {
            "ok": raw is not None,
            "oid": oid,
            "index": suffix,
            "raw": raw,
            "dbm": _olt_raw_to_dbm(raw, olt) if raw is not None else None,
        }
    res["elapsed_ms"] = int((_t.time() - t0) * 1000)
    bases = [k for k in ("rx", "tx") if res.get(k) is not None]
    if not bases:
        res["ok"] = True
        res["message"] = f"terjangkau (up {res['sysup_s']}s), OID belum dikonfigurasi"
    elif any(res[k].get("ok") for k in bases):
        res["ok"] = True
        parts = []
        for k in bases:
            e: dict = res[k]
            parts.append(
                f"{k}={e.get('dbm')}dBm(idx {e.get('index')})"
                if e.get("ok")
                else f"{k} gagal"
            )
        res["message"] = f"terjangkau (up {res['sysup_s']}s); " + ", ".join(parts)
    else:
        res["message"] = "terjangkau, tapi OID Rx/Tx tak menjawab — cek rx/tx_base"
    return res


@fiber_bp.route("/api/olts/<int:oid>/test", methods=["POST"])
@api_login_required
def api_olt_test(oid):
    conn, c = get_db()
    try:
        c.execute("SELECT * FROM olts WHERE id=?", (oid,))
        row = c.fetchone()
        olt = dict(row) if row else None
    finally:
        try:
            conn.close()
        except Exception:
            pass
    if not olt:
        return jsonify({"error": "OLT tidak ditemukan"}), 404
    res = _olt_test_connection(olt)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with db_lock:
        conn, c = get_db()
        try:
            c.execute(
                "UPDATE olts SET last_tested=?, last_test_ok=?, last_test_msg=? WHERE id=?",
                (now, 1 if res["ok"] else 0, res["message"][:200], oid),
            )
            _commit_with_retry(conn)
        finally:
            try:
                conn.close()
            except Exception:
                pass
    try:
        audit(current_user.username, "olt.test", f"{olt['name']} ok={res['ok']}")
    except Exception:
        pass
    return jsonify(
        {**res, "olt": {"id": olt["id"], "name": olt["name"]}, "tested_at": now}
    )


def _discover_sn(olt_name, suffix):
    """SN otomatis hasil discover: sanitasi + batasi 64 karakter.

    Ekor (sufiks index, bagian yang unik) dipertahankan; bila masih
    kepanjangan, pangkas dari kiri.
    """
    base = re.sub(r"[^A-Za-z0-9_.:\-]", "-", (olt_name or "OLT").strip()) or "OLT"
    suffix = re.sub(r"[^A-Za-z0-9_.:\-]", "-", (suffix or "").strip()) or "0"
    room = 64 - len(suffix) - 1
    sn = f"{base[:max(1, room)]}-{suffix}"
    return sn if len(sn) <= 64 else sn[-64:]


@fiber_bp.route("/api/olts/<int:oid>/discover", methods=["POST"])
@api_login_required
def api_olt_discover(oid):
    """Enumerasi ONT dari OLT via walk rx_base, lalu bulk upsert.

    Body opsional: {limit (1-256, default 256), update (bool, default true)}.
    Baris baru -> source=snmp; baris existing source!=snmp dilewati aman
    (tak menimpa data manual). Memory alarm di-seed agar tak banjir telegram.
    """
    data = request.get_json(silent=True) or {}
    try:
        limit = int(data.get("limit", 256))
    except (ValueError, TypeError):
        return jsonify({"error": "limit harus angka 1-256"}), 400
    limit = max(1, min(limit, 256))
    do_update = str(data.get("update", "1")).strip().lower() not in (
        "0",
        "false",
        "tidak",
        "no",
    )
    conn, c = get_db()
    try:
        c.execute("SELECT * FROM olts WHERE id=?", (oid,))
        row = c.fetchone()
        olt = dict(row) if row else None
    finally:
        try:
            conn.close()
        except Exception:
            pass
    if not olt:
        return jsonify({"error": "OLT tidak ditemukan"}), 404
    ip = (olt.get("ip") or "").strip()
    comm = (olt.get("community") or "").strip()
    base_rx = (olt.get("rx_base") or "").strip().strip(".")
    base_tx = (olt.get("tx_base") or "").strip().strip(".")
    if not ip or not comm:
        return jsonify({"error": "IP/community OLT belum diisi"}), 400
    if not _valid_oid(base_rx):
        return (
            jsonify(
                {
                    "error": "rx_base belum diisi — pilih preset vendor dulu atau isi manual"
                }
            ),
            400,
        )
    try:
        up = _snmp_get(ip, comm, [SYSUP_OID], timeout=3.0)
    except Exception as e:
        return jsonify({"error": f"OLT tak terjangkau: {e}"}), 502
    if not up or up[0] is None:
        return jsonify({"error": "OLT tak menjawab SNMP (cek IP/community)"}), 502
    try:
        walked = snmp_walk(ip, comm, base_rx, max_rows=limit)
    except Exception as e:
        return jsonify({"error": f"walk gagal: {e}"}), 502
    suffixes, rx_map = [], {}
    for woid, _tag, ival, sval in walked or []:
        suffix = (woid or "")[len(base_rx) :].lstrip(".")
        if not suffix:
            continue
        raw = ival
        if raw is None and sval not in (None, ""):
            try:
                raw = float(sval)
            except (ValueError, TypeError):
                continue
        dbm = _olt_raw_to_dbm(raw, olt)
        if dbm is None or not -40 <= dbm <= 10:
            continue
        suffixes.append(suffix)
        rx_map[suffix] = dbm
    tx_map = {}
    if _valid_oid(base_tx) and suffixes:
        for i in range(0, len(suffixes), 25):
            chunk = suffixes[i : i + 25]
            try:
                tvals = _snmp_get(
                    ip, comm, [base_tx + "." + s for s in chunk], timeout=4.0
                )
            except Exception:
                tvals = [None] * len(chunk)
            for sfx, v in zip(chunk, tvals or []):
                if v is None:
                    continue
                dbm = _olt_raw_to_dbm(v, olt)
                if dbm is not None and -10 <= dbm <= 10:
                    tx_map[sfx] = dbm
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    created = updated = skipped_manual = skipped = 0
    with db_lock:
        conn, c = get_db()
        try:
            c.execute(
                "SELECT id, source, ont_index FROM fiber_onts WHERE olt_name COLLATE NOCASE = ?",
                (olt["name"],),
            )
            existing = {}
            for r in c.fetchall():
                idx = (r["ont_index"] or "").strip()
                if idx:
                    existing[idx] = dict(r)
            for sfx in suffixes:
                rx_dbm = rx_map[sfx]
                tx_dbm = tx_map.get(sfx)
                ex = existing.get(sfx)
                if ex and (ex.get("source") or "manual") != "snmp":
                    skipped_manual += 1
                    continue
                status, _, _, _ = fiber_eval(rx_dbm, tx_dbm, _fiber_thresholds())
                if ex:
                    if not do_update:
                        skipped += 1
                        continue
                    c.execute(
                        "UPDATE fiber_onts SET rx_power=?, tx_power=?, status=?,"
                        " last_checked=?, last_seen=? WHERE id=?",
                        (rx_dbm, tx_dbm, status, now, now, ex["id"]),
                    )
                    c.execute(
                        "INSERT INTO fiber_history (ont_id, rx_power, tx_power, timestamp)"
                        " VALUES (?,?,?,?)",
                        (ex["id"], rx_dbm, tx_dbm, now),
                    )
                    fiber_alarm_memory[ex["id"]] = status
                    updated += 1
                    continue
                sn = _discover_sn(olt["name"], sfx)
                try:
                    c.execute(
                        """INSERT INTO fiber_onts(ont_sn,customer,olt_name,pon_port,odp_name,
                               rx_power,tx_power,status,last_checked,source,created_at,updated_at,
                               mute_alarm,rx_warn,rx_crit,ont_index,last_seen)
                               VALUES(?,?,?,?,?,?,?,?,?,?,?, ?,?,?,?,?,?)""",
                        (
                            sn,
                            "",
                            olt["name"],
                            "",
                            "",
                            rx_dbm,
                            tx_dbm,
                            status,
                            now,
                            "snmp",
                            now,
                            now,
                            0,
                            None,
                            None,
                            sfx,
                            now,
                        ),
                    )
                    nid = c.lastrowid
                except sqlite3.IntegrityError:
                    skipped += 1
                    continue
                c.execute(
                    "INSERT INTO fiber_history (ont_id, rx_power, tx_power, timestamp)"
                    " VALUES (?,?,?,?)",
                    (nid, rx_dbm, tx_dbm, now),
                )
                fiber_alarm_memory[nid] = status
                created += 1
            _commit_with_retry(conn)
        finally:
            conn.close()
    try:
        audit(
            current_user.username,
            "olt.discover",
            f"{olt['name']} created={created} updated={updated}",
        )
    except Exception:
        pass
    return jsonify(
        {
            "status": "success",
            "olt": olt["name"],
            "walked": len(suffixes),
            "created": created,
            "updated": updated,
            "skipped_manual": skipped_manual,
            "skipped": skipped,
        }
    )


_TOPO_RANK = {
    "critical": 5,
    "overload": 4,
    "warning": 3,
    "stale": 2,
    "unknown": 1,
    "normal": 0,
}


def _topo_counts(members):
    """(counts, worst) untuk sekelompok ONT yang sudah punya _status."""
    counts = {
        "total": len(members),
        "normal": 0,
        "warning": 0,
        "critical": 0,
        "overload": 0,
        "stale": 0,
        "unknown": 0,
    }
    worst, rank = "normal", -1
    for m in members:
        st = m.get("_status", "unknown")
        if st in counts:
            counts[st] += 1
        if _TOPO_RANK.get(st, 0) > rank:
            worst, rank = st, _TOPO_RANK.get(st, 0)
    return counts, (worst if members else "normal")


@fiber_bp.route("/api/fiber/topology", methods=["GET"])
@api_login_required
def api_fiber_topology():
    """Pohon OLT -> ODP -> ONT + koordinat untuk peta.

    Pengelompokan case-insensitive mengikuti agregasi ODP; nama liar
    (tak terdaftar) dan bucket tanpa ODP/OLT tetap ditampilkan.
    Field sensitif OLT (community) tak pernah dikirim.
    """
    conn, c = get_db()
    try:
        try:
            c.execute("SELECT * FROM olts ORDER BY name ASC")
            olts = [dict(r) for r in c.fetchall()]
        except sqlite3.OperationalError:
            olts = []
        try:
            c.execute("SELECT * FROM odps ORDER BY name ASC")
            odps = [dict(r) for r in c.fetchall()]
        except sqlite3.OperationalError:
            odps = []
        try:
            c.execute("SELECT * FROM fiber_onts ORDER BY id ASC")
            onts = [dict(r) for r in c.fetchall()]
        except sqlite3.OperationalError:
            onts = []
    finally:
        try:
            conn.close()
        except Exception:
            pass
    for o in onts:
        try:
            o["_status"] = _fiber_row_status(o)[0]
        except Exception:
            o["_status"] = "unknown"
    olt_by_key = {(o.get("name") or "").strip().lower(): o for o in olts}
    odp_groups = {}
    for d in odps:
        raw_olt = (d.get("olt_name") or "").strip()
        canon = olt_by_key.get(raw_olt.lower())
        name = (canon.get("name") if canon else raw_olt) or ""
        odp_groups.setdefault(name.strip().lower(), {"display": name, "items": []})[
            "items"
        ].append(d)
    ont_groups = {}
    for o in onts:
        raw_olt = (o.get("olt_name") or "").strip()
        canon = olt_by_key.get(raw_olt.lower())
        dkey = ((canon.get("name") if canon else raw_olt) or "").strip().lower()
        pkey = (o.get("odp_name") or "").strip().lower()
        ont_groups.setdefault((dkey, pkey), []).append(o)

    def _ont_node(o):
        return {
            "id": o["id"],
            "ont_sn": o.get("ont_sn"),
            "customer": o.get("customer"),
            "pon_port": o.get("pon_port"),
            "rx_power": o.get("rx_power"),
            "tx_power": o.get("tx_power"),
            "status": o.get("_status", "unknown"),
        }

    def _sorted_onts(items):
        return sorted(
            (_ont_node(o) for o in items),
            key=lambda x: (
                -_TOPO_RANK.get(x["status"], 0),
                x["rx_power"] if x["rx_power"] is not None else 99,
                x["id"],
            ),
        )

    out = []
    reg_keys = [(o.get("name") or "").strip().lower() for o in olts]
    wild_keys = sorted(
        {
            k
            for k in list(odp_groups) + [dk for dk, _ in ont_groups]
            if k not in reg_keys
        }
    )
    for dkey in reg_keys + wild_keys:
        canon = olt_by_key.get(dkey)
        display = (
            canon.get("name")
            if canon
            else odp_groups.get(dkey, {}).get("display") or ""
        )
        if not display:
            display = "(tanpa OLT)"
        olt_info = (
            {
                "id": canon["id"],
                "name": canon.get("name"),
                "ip": canon.get("ip"),
                "vendor": canon.get("vendor"),
                "lat": canon.get("lat"),
                "lon": canon.get("lon"),
            }
            if canon
            else None
        )
        node_odps, all_onts = [], []
        for d in sorted(
            odp_groups.get(dkey, {}).get("items", []),
            key=lambda x: (x.get("name") or "").lower(),
        ):
            members = ont_groups.pop((dkey, (d.get("name") or "").strip().lower()), [])
            all_onts.extend(members)
            counts, worst = _topo_counts(members)
            cap = d.get("capacity") or 0
            node_odps.append(
                {
                    "name": d.get("name"),
                    "registered": True,
                    "odp": {
                        "id": d["id"],
                        "name": d.get("name"),
                        "olt_name": d.get("olt_name"),
                        "capacity": cap,
                        "location": d.get("location"),
                        "lat": d.get("lat"),
                        "lon": d.get("lon"),
                    },
                    "counts": counts,
                    "worst": worst,
                    "fill_pct": round(len(members) / cap * 100, 1) if cap else 0,
                    "onts": _sorted_onts(members),
                }
            )
        leftovers = sorted(
            [
                (pk, ont_groups.pop((dkey, pk)))
                for (dk, pk) in [k for k in list(ont_groups) if k[0] == dkey]
            ],
            key=lambda t: (t[0] != "", t[0]),
        )
        for pkey, members in leftovers:
            all_onts.extend(members)
            label = next(
                (
                    o.get("odp_name") or ""
                    for o in members
                    if (o.get("odp_name") or "").strip()
                ),
                "",
            )
            counts, worst = _topo_counts(members)
            node_odps.append(
                {
                    "name": label.strip() or "(tanpa ODP)",
                    "registered": False,
                    "odp": {
                        "id": 0,
                        "name": label.strip(),
                        "olt_name": display if display != "(tanpa OLT)" else "",
                        "capacity": 0,
                        "location": "",
                        "lat": None,
                        "lon": None,
                    },
                    "counts": counts,
                    "worst": worst,
                    "fill_pct": 0,
                    "onts": _sorted_onts(members),
                }
            )
        if not node_odps and not canon:
            continue
        counts, worst = _topo_counts(all_onts)
        out.append(
            {
                "name": display,
                "registered": bool(canon),
                "olt": olt_info,
                "counts": counts,
                "worst": worst,
                "odps": node_odps,
            }
        )
    return jsonify({"olts": out})


@fiber_bp.route("/api/fiber/link-budget", methods=["POST"])
@api_login_required
def api_fiber_link_budget():
    data = request.get_json(silent=True) or {}

    def _num(v, name, lo, hi):
        try:
            f = float(v)
        except (ValueError, TypeError):
            raise ValueError(f"{name} harus angka")
        if not lo <= f <= hi:
            raise ValueError(f"{name} harus {lo}..{hi}")
        return f

    try:
        tx = _num(data.get("tx_dbm"), "tx_dbm", -10, 10)
        fiber_km = _num(data.get("fiber_km", 0), "fiber_km", 0, 100)
        connectors = int(_num(data.get("connectors", 4), "connectors", 0, 50))
        splices = int(_num(data.get("splices", 0), "splices", 0, 100))
        margin = _num(data.get("margin_db", 0), "margin_db", 0, 10)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    splitters = data.get("splitters") or []
    if not isinstance(splitters, list) or not 1 <= len(splitters) <= 3:
        return (
            jsonify({"error": 'splitters harus list 1-3 tingkat (mis. ["1:4","1:8"])'}),
            400,
        )
    if any(s not in FIBER_SPLITTER_LOSS for s in splitters):
        return (
            jsonify(
                {"error": f"splitter harus salah satu {sorted(FIBER_SPLITTER_LOSS)}"}
            ),
            400,
        )

    result = fiber_link_budget(tx, splitters, fiber_km, connectors, splices, margin)
    actual_rx = None
    ont = None
    ont_id = data.get("ont_id")
    if ont_id is not None:
        try:
            ont_id = int(ont_id)
        except (ValueError, TypeError):
            return jsonify({"error": "ont_id harus angka"}), 400
        conn, c = get_db()
        try:
            c.execute(
                "SELECT id, ont_sn, customer, rx_power FROM fiber_onts WHERE id=?",
                (ont_id,),
            )
            row = c.fetchone()
        finally:
            conn.close()
        if not row:
            return jsonify({"error": "ONT tidak ditemukan"}), 404
        ont = {"id": row["id"], "ont_sn": row["ont_sn"], "customer": row["customer"]}
        actual_rx = row["rx_power"]
    verdict, severity, advice = fiber_budget_verdict(result["expected_rx"], actual_rx)
    return jsonify(
        {
            **result,
            "splitters": splitters,
            "fiber_km": fiber_km,
            "connectors": connectors,
            "splices": splices,
            "ont": ont,
            "actual_rx": actual_rx,
            "delta_db": (
                round(actual_rx - result["expected_rx"], 2)
                if actual_rx is not None
                else None
            ),
            "verdict": verdict,
            "severity": severity,
            "advice": advice,
        }
    )


__all__ = [
    "fiber_bp",
    "fiber_page",
    "topo_page",
    "_validate_fiber",
    "_FIBER_SORTS",
    "_FIBER_STATUS_RANK",
    "api_fiber_list",
    "api_fiber_summary",
    "api_fiber_create",
    "api_fiber_update",
    "api_fiber_delete",
    "api_fiber_history",
    "api_fiber_history_export",
    "_fiber_open_downtime_map",
    "api_fiber_downtime",
    "api_fiber_sla",
    "api_fiber_export",
    "api_fiber_import",
    "_validate_odp",
    "_odp_aggregation",
    "api_odp_list",
    "api_odp_create",
    "api_odp_update",
    "api_odp_delete",
    "_validate_olt",
    "_apply_olt_preset",
    "api_olt_list",
    "api_olt_create",
    "api_olt_update",
    "api_olt_delete",
    "api_olt_presets",
    "_olt_test_connection",
    "api_olt_test",
    "_discover_sn",
    "api_olt_discover",
    "_TOPO_RANK",
    "_topo_counts",
    "api_fiber_topology",
    "api_fiber_link_budget",
]
