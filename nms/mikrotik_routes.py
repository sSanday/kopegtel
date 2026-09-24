"""nms.mikrotik_routes — halaman dan API MikroTik."""

import difflib
import re
import sqlite3
import threading
from datetime import datetime

from flask import Blueprint, Response, jsonify, render_template, request
from flask_login import current_user, login_required

from nms.auth import api_login_required
from nms.crypto import decrypt_secret
from nms.db import _commit_with_retry, db_lock, get_db
from nms.format import _fmt_uptime
from nms.mikrotik import _mt_evaluate, _mt_stale_info
from nms.mikrotik_poll import (
    _store_mt_backup,
    fetch_mikrotik_config,
    iface_state,
    mt_iface_flaps,
    mt_iface_oper,
    mt_iface_tg,
    mt_is_mikrotik,
    poll_mikrotik_ifaces,
)
from nms.notify import audit, send_telegram_alert
from nms.snmp import DISCOVER_MAX_IF, discover_interfaces

mikrotik_bp = Blueprint("mikrotik", __name__)


@mikrotik_bp.route("/mikrotik")
@login_required
def mikrotik_page():
    return render_template("mikrotik.html")


@mikrotik_bp.route("/api/mikrotik", methods=["GET"])
@api_login_required
def api_mikrotik_list():
    conn, c = get_db()
    try:
        c.execute(
            "SELECT ip, alias, category, snmp_community, snmp_profile FROM hosts ORDER BY id ASC"
        )
        hosts = [dict(r) for r in c.fetchall()]
        out = []
        for h in hosts:
            ip = h["ip"]
            if not (h.get("snmp_community") or "").strip():
                continue
            if (h.get("snmp_profile") or "auto") == "generic":
                continue
            c.execute(
                "SELECT cpu, mem_used, storage_used, temp_c, uptime_s, timestamp"
                " FROM device_health WHERE host=? ORDER BY id DESC LIMIT 1",
                (ip,),
            )
            r = c.fetchone()
            if r:
                try:
                    uptime_s = None if r["uptime_s"] is None else float(r["uptime_s"])
                except (ValueError, TypeError, KeyError, IndexError):
                    uptime_s = None
                status, severity, advice = _mt_evaluate(
                    ip, r["cpu"], r["mem_used"], r["storage_used"], r["temp_c"]
                )
                stale, stale_age = _mt_stale_info(r["timestamp"])
                if stale and status in ("normal", "warning"):
                    status, severity = "stale", "warning"
                    advice = (
                        f"Tanpa data SNMP baru sejak {stale_age} "
                        f"(terakhir {r['timestamp'] or '—'}). Kemungkinan host mati, "
                        "community diganti, atau UDP 161 diblokir."
                    )
                out.append(
                    {
                        "host": ip,
                        "alias": (h.get("alias") or "").strip() or ip,
                        "category": (h.get("category") or "").strip()
                        or "Uncategorized",
                        "cpu": r["cpu"],
                        "mem": r["mem_used"],
                        "storage": r["storage_used"],
                        "temp_c": r["temp_c"],
                        "uptime_s": uptime_s,
                        "uptime": _fmt_uptime(uptime_s),
                        "last_seen": r["timestamp"],
                        "is_mikrotik": bool(mt_is_mikrotik.get(ip)),
                        "stale": stale,
                        "stale_age": stale_age,
                        "status": status,
                        "severity": severity,
                        "advice": advice,
                    }
                )
            else:
                out.append(
                    {
                        "host": ip,
                        "alias": (h.get("alias") or "").strip() or ip,
                        "category": (h.get("category") or "").strip()
                        or "Uncategorized",
                        "cpu": None,
                        "mem": None,
                        "storage": None,
                        "temp_c": None,
                        "last_seen": None,
                        "is_mikrotik": bool(mt_is_mikrotik.get(ip)),
                        "status": "unknown",
                        "severity": None,
                        "advice": "Menunggu poll SNMP berikutnya.",
                    }
                )
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return jsonify(out)


@mikrotik_bp.route("/api/mikrotik/<path:host>/history")
@api_login_required
def api_mikrotik_history(host):
    try:
        hours = int(request.args.get("hours", 24))
    except (ValueError, TypeError):
        return jsonify({"error": "hours harus angka 1-336"}), 400
    hours = max(1, min(hours, 336))
    metric = request.args.get("metric", "cpu")
    cols = {
        "cpu": "cpu",
        "mem": "mem_used",
        "storage": "storage_used",
        "temp": "temp_c",
    }
    col = cols.get(metric, "cpu")
    conn, c = get_db()
    try:
        c.execute(
            f"SELECT timestamp, {col} AS val FROM device_health"
            " WHERE host=? AND timestamp > datetime('now','localtime',?) ORDER BY id ASC",
            (host, f"-{hours} hours"),
        )
        rows = c.fetchall()
    finally:
        conn.close()
    labels = [
        (
            r["timestamp"].split(" ")[1]
            if r["timestamp"] and " " in r["timestamp"]
            else r["timestamp"]
        )
        for r in rows
    ]
    return jsonify(
        {
            "host": host,
            "metric": metric,
            "hours": hours,
            "labels": labels,
            "values": [r["val"] for r in rows],
            "count": len(rows),
        }
    )


@mikrotik_bp.route("/api/mikrotik/<path:host>/interfaces", methods=["GET"])
@api_login_required
def api_mt_iface_list(host):
    conn, c = get_db()
    try:
        c.execute("SELECT ip FROM hosts WHERE ip=?", (host,))
        if not c.fetchone():
            return jsonify({"error": "Host tidak ditemukan"}), 404
        c.execute(
            "SELECT if_index, name, oper, monitor, last_changed FROM snmp_interfaces"
            " WHERE host=? ORDER BY if_index ASC",
            (host,),
        )
        out = []
        for r in c.fetchall():
            d = dict(r)
            c.execute(
                "SELECT net_in, net_out, timestamp FROM iface_traffic"
                " WHERE host=? AND if_index=? ORDER BY id DESC LIMIT 1",
                (host, d["if_index"]),
            )
            last = c.fetchone()
            d["last_in"] = last["net_in"] if last else None
            d["last_out"] = last["net_out"] if last else None
            d["last_seen"] = last["timestamp"] if last else None
            out.append(d)
    finally:
        conn.close()
    return jsonify(out)


@mikrotik_bp.route("/api/mikrotik/<path:host>/interfaces/discover", methods=["POST"])
@api_login_required
def api_mt_iface_discover(host):
    conn, c = get_db()
    try:
        c.execute("SELECT snmp_community, snmp_profile FROM hosts WHERE ip=?", (host,))
        h = c.fetchone()
    finally:
        conn.close()
    if not h:
        return jsonify({"error": "Host tidak ditemukan"}), 404
    community = (h["snmp_community"] or "").strip()
    if not community:
        return jsonify({"error": "Host belum punya SNMP community"}), 400
    if (h["snmp_profile"] or "auto") == "generic":
        return jsonify({"error": "Profil host generic (SNMP health nonaktif)"}), 400
    found = discover_interfaces(host, community)
    if not found:
        return (
            jsonify(
                {
                    "error": "Tidak ada interface terjawab (cek community/firewall/UDP 161)"
                }
            ),
            502,
        )
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with db_lock:
        conn, c = get_db()
        try:
            for f in found:
                c.execute(
                    "INSERT INTO snmp_interfaces (host, if_index, name, oper, monitor, last_changed)"
                    " VALUES (?,?,?,?,1,?)"
                    " ON CONFLICT(host, if_index) DO UPDATE SET name=excluded.name,"
                    " oper=excluded.oper, last_changed=excluded.last_changed",
                    (host, f["if_index"], f["name"][:64], f["oper"], now),
                )
            conn.commit()
        finally:
            conn.close()
    try:
        audit(current_user.username, "mt.discover", f"{host} {len(found)} iface")
    except Exception:
        pass
    try:
        threading.Thread(target=poll_mikrotik_ifaces, daemon=True).start()
    except Exception:
        pass
    return jsonify(
        {
            "status": "success",
            "count": len(found),
            "truncated": len(found) >= DISCOVER_MAX_IF,
            "interfaces": found,
        }
    )


@mikrotik_bp.route("/api/mikrotik/<path:host>/interfaces", methods=["PATCH"])
@api_login_required
def api_mt_iface_update(host):
    data = request.get_json(silent=True) or {}
    try:
        idx = int(data.get("if_index", 0))
    except (ValueError, TypeError):
        return jsonify({"error": "if_index harus angka"}), 400
    if idx < 1:
        return jsonify({"error": "if_index harus >= 1"}), 400
    sets, params = [], []
    if "monitor" in data:
        sets.append("monitor=?")
        params.append(1 if data.get("monitor") else 0)
    if "name" in data:
        name = str(data.get("name") or "").strip()[:64]
        if not name:
            return jsonify({"error": "Nama tidak boleh kosong"}), 400
        sets.append("name=?")
        params.append(name)
    if not sets:
        return jsonify({"error": "Tidak ada field yang dikirim"}), 400
    conn, c = get_db()
    c.execute("SELECT 1 FROM snmp_interfaces WHERE host=? AND if_index=?", (host, idx))
    if not c.fetchone():
        conn.close()
        return jsonify({"error": "Interface tidak ditemukan (discover dulu)"}), 404
    c.execute(
        f"UPDATE snmp_interfaces SET {', '.join(sets)} WHERE host=? AND if_index=?",
        (*params, host, idx),
    )
    conn.commit()
    conn.close()
    try:
        audit(current_user.username, "mt.iface", f"{host} if{idx} {sets}")
    except Exception:
        pass
    return jsonify({"status": "success"})


@mikrotik_bp.route("/api/mikrotik/<path:host>/interfaces/<int:idx>", methods=["DELETE"])
@api_login_required
def api_mt_iface_delete(host, idx):
    conn, c = get_db()
    c.execute("DELETE FROM snmp_interfaces WHERE host=? AND if_index=?", (host, idx))
    deleted = c.rowcount
    try:
        c.execute("DELETE FROM iface_traffic WHERE host=? AND if_index=?", (host, idx))
    except sqlite3.OperationalError:
        pass
    conn.commit()
    conn.close()
    try:
        mt_iface_oper.pop((host, idx), None)
        iface_state.pop((host, idx), None)
        mt_iface_tg.pop((host, idx), None)
        mt_iface_flaps.pop((host, idx), None)
    except Exception:
        pass
    if not deleted:
        return jsonify({"error": "Interface tidak ditemukan"}), 404
    try:
        audit(current_user.username, "mt.iface_delete", f"{host} if{idx}")
    except Exception:
        pass
    return jsonify({"status": "success"})


def _mt_backup_host_row(host):
    conn, c = get_db()
    try:
        c.execute(
            "SELECT ip, ssh_user, ssh_pass, ssh_port, backup_enable,"
            " backup_last, backup_ok FROM hosts WHERE ip=?",
            (host,),
        )
        row = c.fetchone()
        if not row:
            return None
        out = dict(row)
        out["ssh_pass"] = decrypt_secret(out.get("ssh_pass") or "")
        return out
    finally:
        conn.close()


@mikrotik_bp.route("/api/mikrotik/<path:host>/backups", methods=["GET"])
@api_login_required
def api_mt_backup_list(host):
    if not _mt_backup_host_row(host):
        return jsonify({"error": "Host tidak ditemukan"}), 404
    conn, c = get_db()
    try:
        c.execute(
            "SELECT id, taken_at, size, sha256, changed FROM mt_backups"
            " WHERE host=? ORDER BY id DESC LIMIT 50",
            (host,),
        )
        out = [dict(r) for r in c.fetchall()]
    finally:
        conn.close()
    return jsonify(out)


@mikrotik_bp.route("/api/mikrotik/<path:host>/backups/<int:bid>", methods=["GET"])
@api_login_required
def api_mt_backup_get(host, bid):
    conn, c = get_db()
    try:
        c.execute(
            "SELECT id, host, taken_at, size, sha256, content FROM mt_backups"
            " WHERE host=? AND id=?",
            (host, bid),
        )
        row = c.fetchone()
    finally:
        conn.close()
    if not row:
        return jsonify({"error": "Backup tidak ditemukan"}), 404
    d = dict(row)
    if (request.args.get("download") or "").strip() == "1":
        safe = re.sub(r"[^A-Za-z0-9_.\-]", "_", host)[:48]
        return Response(
            d.get("content") or "",
            mimetype="text/plain",
            headers={
                "Content-Disposition": f"attachment; filename={safe}_{d.get('taken_at','')[:10]}.rsc"
            },
        )
    return jsonify(
        {k: d.get(k) for k in ("id", "host", "taken_at", "size", "sha256", "content")}
    )


@mikrotik_bp.route("/api/mikrotik/<path:host>/backups/<int:bid>/diff", methods=["GET"])
@api_login_required
def api_mt_backup_diff(host, bid):
    conn, c = get_db()
    try:
        c.execute(
            "SELECT id, taken_at, content FROM mt_backups"
            " WHERE host=? AND id<=? ORDER BY id DESC LIMIT 2",
            (host, bid),
        )
        rows = [dict(r) for r in c.fetchall()]
    finally:
        conn.close()
    if not rows or rows[0]["id"] != bid:
        return jsonify({"error": "Backup tidak ditemukan"}), 404
    cur = rows[0]
    prev = rows[1] if len(rows) > 1 else None
    if not prev:
        return jsonify(
            {
                "id": bid,
                "vs": None,
                "diff": [],
                "note": "versi pertama (baseline, tanpa pembanding)",
            }
        )
    diff = list(
        difflib.unified_diff(
            (prev.get("content") or "").splitlines(),
            (cur.get("content") or "").splitlines(),
            fromfile=f"#{prev['id']} {prev['taken_at']}",
            tofile=f"#{cur['id']} {cur['taken_at']}",
            n=3,
        )
    )
    if len(diff) > 500:
        diff = diff[:500] + [f"... dipotong, total {len(diff)} baris"]
    return jsonify({"id": bid, "vs": prev["id"], "diff": diff, "lines": len(diff)})


@mikrotik_bp.route("/api/mikrotik/<path:host>/backup", methods=["POST"])
@api_login_required
def api_mt_backup_now(host):
    row = _mt_backup_host_row(host)
    if not row:
        return jsonify({"error": "Host tidak ditemukan"}), 404
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        ok, payload = fetch_mikrotik_config(
            host,
            row.get("ssh_user") or "",
            row.get("ssh_pass") or "",
            row.get("ssh_port") or 22,
        )
    except Exception as e:
        return jsonify({"error": f"fetch gagal: {e}"}), 502
    tg_msg = None
    with db_lock:
        conn, c = get_db()
        try:
            tg_msg = _store_mt_backup(c, host, ok, payload, timestamp)
            _commit_with_retry(conn)
        except sqlite3.OperationalError as e:
            try:
                conn.rollback()
            except Exception:
                pass
            return jsonify({"error": f"DB sibuk: {e}"}), 503
        finally:
            conn.close()
    if tg_msg:
        try:
            send_telegram_alert(tg_msg)
        except Exception as e:
            print(f"[WARN] telegram backup gagal: {e}")
    try:
        audit(current_user.username, "mt.backup_now", f"{host} ok={ok}")
    except Exception:
        pass
    if not ok:
        return jsonify({"error": payload}), 502
    return jsonify(
        {"status": "success", "taken_at": timestamp, "notified": bool(tg_msg)}
    )


@mikrotik_bp.route("/api/mikrotik/<path:host>/iface/<int:idx>/history")
@api_login_required
def api_mt_iface_history(host, idx):
    try:
        hours = int(request.args.get("hours", 24))
    except (ValueError, TypeError):
        return jsonify({"error": "hours harus angka 1-336"}), 400
    hours = max(1, min(hours, 336))
    conn, c = get_db()
    try:
        c.execute(
            "SELECT name FROM snmp_interfaces WHERE host=? AND if_index=?", (host, idx)
        )
        iface = c.fetchone()
        if not iface:
            return jsonify({"error": "Interface tidak ditemukan"}), 404
        c.execute(
            "SELECT timestamp, net_in, net_out FROM iface_traffic"
            " WHERE host=? AND if_index=? AND timestamp > datetime('now','localtime',?)"
            " ORDER BY id ASC",
            (host, idx, f"-{hours} hours"),
        )
        rows = c.fetchall()
    finally:
        conn.close()
    if len(rows) > 500:
        step = (len(rows) + 499) // 500
        rows = rows[::step]
    labels = [
        (
            r["timestamp"].split(" ")[1]
            if r["timestamp"] and " " in r["timestamp"]
            else r["timestamp"]
        )
        for r in rows
    ]
    return jsonify(
        {
            "host": host,
            "if_index": idx,
            "name": iface["name"],
            "hours": hours,
            "labels": labels,
            "rx": [r["net_in"] for r in rows],
            "tx": [r["net_out"] for r in rows],
            "count": len(rows),
        }
    )


__all__ = [
    "mikrotik_bp",
    "mikrotik_page",
    "api_mikrotik_list",
    "api_mikrotik_history",
    "api_mt_iface_list",
    "api_mt_iface_discover",
    "api_mt_iface_update",
    "api_mt_iface_delete",
    "_mt_backup_host_row",
    "api_mt_backup_list",
    "api_mt_backup_get",
    "api_mt_backup_diff",
    "api_mt_backup_now",
    "api_mt_iface_history",
]
