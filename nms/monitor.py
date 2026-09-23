"""nms.monitor — ping host dan heartbeat agent.

Dipindah dari app.py (modul 3, P3) tanpa perubahan perilaku.
check_host/check_network/SNMP menyusul di modul berikutnya (terikat fake
test dan dependensi fiber).
"""

import re
import subprocess
from datetime import datetime

from nms.db import get_db, get_target_hosts, log_system_event
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
