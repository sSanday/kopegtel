"""nms.monitor — ping host, heartbeat agent, dan korelasi induk fiber."""

import re
import sqlite3
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from nms.config import DOWN_COOLDOWN_S
from nms.db import (
    _commit_with_retry,
    _insert_system_log,
    db_lock,
    get_active_maintenance_map,
    get_db,
    get_setting,
    get_target_hosts,
    log_system_event,
)
from nms.notify import send_telegram_alert

agent_offline_memory = {}


_PING_TARGET_RE = re.compile(r"^[a-zA-Z0-9]([a-zA-Z0-9.\-]{0,253}[a-zA-Z0-9])?$")
_PING_RTT_RES = (
    re.compile(r"rtt min/avg/max/mdev = [\d.]+/([\d.]+)/"),
    re.compile(r"round-trip min/avg/max(?:/stddev)? = [\d.]+/([\d.]+)/"),
)


def ping_host(host):
    host = (host or "").strip()

    if not host or host.startswith("-") or not _PING_TARGET_RE.match(host):
        return -1, 100.0
    try:
        result = subprocess.run(
            ["ping", "-c", "3", "-W", "2", "-i", "0.5", host],
            capture_output=True,
            text=True,
            timeout=12,
        )
        out = result.stdout or ""
        loss_match = re.search(
            r"(\d+(?:\.\d+)?)%\s*(?:packet\s+)?loss", out, re.IGNORECASE
        )
        loss_pct = float(loss_match.group(1)) if loss_match else 100.0

        avg = None
        for rx in _PING_RTT_RES:
            m = rx.search(out)
            if m:
                avg = float(m.group(1))
                break
        if avg is not None and loss_pct < 100:
            return avg, loss_pct
        return -1, 100.0
    except Exception:
        return -1, 100.0


def check_agent_heartbeat():
    targets = get_target_hosts()

    conn, c = get_db()
    try:
        last_map = {}
        for host in targets:

            c.execute(
                """
                SELECT timestamp FROM agent_metrics 
                WHERE host=? AND cpu_percent IS NOT NULL ORDER BY id DESC LIMIT 1
            """,
                (host,),
            )
            row = c.fetchone()
            last_map[host] = row["timestamp"] if row else None
    finally:
        conn.close()

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    newly_offline = []
    with db_lock:
        for host in targets:
            last_ts = last_map.get(host)
            if not last_ts:
                continue
            try:
                last_time = datetime.strptime(last_ts, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
            diff = (datetime.now() - last_time).total_seconds()

            if diff > 120 and not agent_offline_memory.get(host, False):

                agent_offline_memory[host] = True
                newly_offline.append(host)
    for host in newly_offline:
        msg = f"⚠️ *AGENT OFFLINE*\nHost: `{host}`\nTidak ada laporan dari Agent selama lebih dari 2 menit.\nWaktu: {now}"
        try:
            send_telegram_alert(msg)
        except Exception as e:
            print(f"[WARN] telegram offline-alert gagal: {e}")
        try:
            log_system_event(
                "AGENT_OFFLINE", host, "Agent berhenti merespon (Heartbeat hilang)"
            )
        except Exception as e:
            print(f"[WARN] log AGENT_OFFLINE gagal: {e}")


status_memory = {}
down_since = {}

last_down_telegram = {}


def check_host(host):
    latency, packet_loss = ping_host(host)
    return host, latency, packet_loss


def check_network():
    targets = get_target_hosts()
    if not targets:
        return

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    for t in targets:
        if t not in status_memory:
            status_memory[t] = False

    max_workers = min(len(targets), 20)
    results = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {executor.submit(check_host, h): h for h in targets}
        for future in as_completed(future_map):
            try:
                host, latency, packet_loss = future.result()
                results[host] = (latency, packet_loss)
            except Exception as e:
                h = future_map.get(future, "?")
                print(f"[WARN] ping {h} gagal: {e}")

    telegram_queue = []
    maint_map = get_active_maintenance_map()
    with db_lock:
        conn, c = get_db()
        try:
            for host in targets:
                if host not in results:
                    continue
                try:
                    latency, packet_loss = results[host]
                    host_is_down = latency == -1
                    was_down = status_memory.get(host, False)
                    in_maint = host in maint_map

                    if host_is_down and not was_down:

                        status_memory[host] = True
                        down_since[host] = datetime.now()
                        c.execute(
                            "SELECT 1 FROM down_events WHERE host=? AND resolved_at IS NULL LIMIT 1",
                            (host,),
                        )
                        if c.fetchone() is None:
                            c.execute(
                                "INSERT INTO down_events (host, started_at, is_maintenance) VALUES (?, ?, ?)",
                                (host, timestamp, 1 if in_maint else 0),
                            )
                        if in_maint:
                            reason = (maint_map[host].get("reason") or "").strip()[:200]
                            _insert_system_log(
                                c,
                                "MAINTENANCE_DOWN",
                                host,
                                f"DOWN dalam maintenance{(' - ' + reason) if reason else ''}",
                                timestamp,
                            )
                            print(
                                f"[MAINT] Telegram DOWN {host} disuppress (maintenance)"
                            )
                        else:
                            _insert_system_log(
                                c, "NETWORK_DOWN", host, "Ping timeout/RTO", timestamp
                            )

                            now_dt = datetime.now()
                            last_tg = last_down_telegram.get(host)
                            if (
                                last_tg is None
                                or (now_dt - last_tg).total_seconds() >= DOWN_COOLDOWN_S
                            ):
                                last_down_telegram[host] = now_dt
                                _aff, _onames = _fiber_parent_register(
                                    host, timestamp, c
                                )
                                _impact = (
                                    (
                                        f"\nOLT: `{', '.join(_onames)}` "
                                        f"(~{_aff} ONT, alarm ONT disuppress)"
                                    )
                                    if _aff
                                    else ""
                                )
                                telegram_queue.append(
                                    f"🚨 *ALARM!*\nHost   : `{host}`\nStatus : *DOWN*\nWaktu  : {timestamp}{_impact}"
                                )
                            else:
                                print(
                                    f"[COOLDOWN] Telegram DOWN {host} ditahan (flapping?)"
                                )

                    elif not host_is_down and was_down:

                        status_memory[host] = False
                        duration_str = ""
                        duration_s = None
                        was_maint_event = False
                        if host in down_since:
                            delta = datetime.now() - down_since.pop(host)
                            duration_s = int(delta.total_seconds())
                        else:

                            try:
                                c.execute(
                                    "SELECT started_at FROM down_events WHERE host=? AND resolved_at IS NULL ORDER BY id DESC LIMIT 1",
                                    (host,),
                                )
                                orow = c.fetchone()
                                if orow and orow["started_at"]:
                                    started = datetime.strptime(
                                        orow["started_at"], "%Y-%m-%d %H:%M:%S"
                                    )
                                    duration_s = max(
                                        0,
                                        int((datetime.now() - started).total_seconds()),
                                    )
                            except Exception:
                                pass
                        try:
                            c.execute(
                                "SELECT is_maintenance FROM down_events WHERE host=? AND resolved_at IS NULL ORDER BY id DESC LIMIT 1",
                                (host,),
                            )
                            mrow = c.fetchone()
                            was_maint_event = bool(mrow and mrow["is_maintenance"])
                        except Exception:
                            was_maint_event = False
                        if duration_s is not None:
                            m, s = divmod(duration_s, 60)
                            duration_str = f"\nDurasi DOWN : {m} menit {s} detik"
                        c.execute(
                            "UPDATE down_events SET resolved_at=?, duration_s=? WHERE host=? AND resolved_at IS NULL",
                            (timestamp, duration_s, host),
                        )
                        if in_maint or was_maint_event:
                            _insert_system_log(
                                c,
                                "MAINTENANCE_UP",
                                host,
                                f"Pulih dalam maintenance{ duration_str.replace(chr(10), '')}",
                                timestamp,
                            )
                            print(
                                f"[MAINT] Telegram PULIH {host} disuppress (maintenance)"
                            )
                        else:
                            _insert_system_log(
                                c,
                                "NETWORK_UP",
                                host,
                                f"Pulih setelah {duration_str.replace(chr(10), '')}",
                                timestamp,
                            )
                            telegram_queue.append(
                                f"✅ *PULIH!*\nHost    : `{host}`\nLatency : {latency:.2f} ms\nLoss    : {packet_loss:.0f}%{duration_str}"
                            )
                            try:
                                for _k in [
                                    k
                                    for k, v in list(fiber_parent_down.items())
                                    if not (v or {}).get("synthetic")
                                    and (v or {}).get("ip") == host
                                ]:
                                    fiber_parent_down.pop(_k, None)
                            except Exception:
                                pass

                    c.execute(
                        "INSERT INTO ping_logs (host, latency, packet_loss, timestamp) VALUES (?, ?, ?, ?)",
                        (host, latency, packet_loss, timestamp),
                    )
                    label = (
                        f"{latency:.2f} ms | loss {packet_loss:.0f}%"
                        if not host_is_down
                        else "DOWN"
                    )
                    print(f"[LOG] {timestamp} | {host:<16} | {label}")
                except Exception as e:
                    print(f"[WARN] proses hasil {host} gagal (tetap lanjut): {e}")
                    continue

            _commit_with_retry(conn)
        except sqlite3.OperationalError as e:
            print(f"[DB LOCK] check_network commit gagal: {e}")
            try:
                conn.rollback()
            except Exception:
                pass
        finally:
            conn.close()

    for msg in telegram_queue:
        try:
            send_telegram_alert(msg)
        except Exception as e:
            print(f"[WARN] telegram gagal: {e}")


fiber_parent_down = {}
FIBER_PARENT_MIN = 5


def _fiber_parent_min():
    try:
        m = int(float(get_setting("fiber_parent_min", FIBER_PARENT_MIN)))
    except (ValueError, TypeError):
        m = FIBER_PARENT_MIN
    return min(50, max(2, m))


def _fiber_oltkey(o):
    try:
        return (o.get("olt_name") or "").strip().lower()
    except (AttributeError, TypeError):
        return ""


def refresh_fiber_parent_map():
    """Sinkronkan entri ping dari status_memory (sintetik dipertahankan).

    Kembalikan map (boleh dipakai langsung). Dipanggil tiap awal poll dan
    single check; check_network juga register/unregister langsung agar
    tak ada jeda ras.
    """
    try:
        down_ips = {h for h, d in list(status_memory.items()) if d}
    except Exception:
        down_ips = set()
    if not down_ips:
        for k in [
            k
            for k, v in list(fiber_parent_down.items())
            if not (v or {}).get("synthetic")
        ]:
            fiber_parent_down.pop(k, None)
        return fiber_parent_down
    try:
        conn, c = get_db()
        try:
            c.execute("SELECT name, ip FROM olts")
            olts = [dict(r) for r in c.fetchall()]
        finally:
            conn.close()
    except Exception:
        return fiber_parent_down
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    by_ip = {}
    for o in olts:
        ip = (o.get("ip") or "").strip()
        if ip:
            by_ip.setdefault(ip, []).append((o.get("name") or "").strip().lower())
    live = set()
    for ip in down_ips:
        for key in by_ip.get(ip, []):
            if not key:
                continue
            live.add(key)
            if key not in fiber_parent_down:
                reg = next(
                    (
                        o.get("name")
                        for o in olts
                        if (o.get("name") or "").strip().lower() == key
                    ),
                    "",
                )
                fiber_parent_down[key] = {
                    "ip": ip,
                    "since": now,
                    "name": reg or key,
                    "synthetic": False,
                    "count": 0,
                }
    for k in [
        k
        for k, v in list(fiber_parent_down.items())
        if not (v or {}).get("synthetic") and k not in live
    ]:
        fiber_parent_down.pop(k, None)
    return fiber_parent_down


def _fiber_parent_register(host_ip, timestamp, c):
    """Daftarkan OLT ber-IP ini sebagai induk + hitung ONT terdampak.

    Kembalikan (affected_count, [olt_names]). c = cursor aktif (baca saja).
    Dipakai transisi DOWN host agar supresi berlaku seketika.
    """
    try:
        c.execute("SELECT name FROM olts WHERE ip=?", (host_ip,))
        names = [
            (r["name"] or "").strip() for r in c.fetchall() if (r["name"] or "").strip()
        ]
    except sqlite3.OperationalError:
        return 0, []
    affected = 0
    for name in names:
        key = name.lower()
        if key and key not in fiber_parent_down:
            fiber_parent_down[key] = {
                "ip": host_ip,
                "since": timestamp,
                "name": name,
                "synthetic": False,
                "count": 0,
            }
        try:
            affected += c.execute(
                "SELECT COUNT(*) FROM fiber_onts WHERE olt_name COLLATE NOCASE = ?",
                (name,),
            ).fetchone()[0]
        except sqlite3.OperationalError:
            pass
    return affected, names
