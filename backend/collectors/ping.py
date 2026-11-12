"""Ping ICMP dengan timeout 2 detik.

Memakai utilitas ``ping`` bawaan sistem operasi karena raw-socket ICMP
di Windows membutuhkan hak admin. Output ping Indonesia/Inggris sama-sama
ditangkap untuk mengambil nilai latency.
"""

import re
import subprocess
import sys

TIMEOUT_SECONDS = 2
_LATENCY_RE = re.compile(r"(?:time|waktu)[=<]\s*(\d+(?:[.,]\d+)?)\s*ms", re.IGNORECASE)


def check_ping(ip):
    if sys.platform.startswith("win"):
        args = ["ping", "-n", "1", "-w", str(TIMEOUT_SECONDS * 1000), ip]
    else:
        args = ["ping", "-c", "1", "-W", str(TIMEOUT_SECONDS), ip]

    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS + 2,
        )
    except subprocess.TimeoutExpired:
        return {"method": "ping", "ok": False, "latency_ms": None, "reason": "timeout"}
    except OSError:
        return {"method": "ping", "ok": False, "latency_ms": None, "reason": "unreachable"}

    output = (result.stdout or "") + (result.stderr or "")
    reachable = result.returncode == 0

    latency_ms = None
    match = _LATENCY_RE.search(output)
    if match:
        latency_ms = float(match.group(1).replace(",", "."))

    if reachable:
        return {"method": "ping", "ok": True, "latency_ms": latency_ms, "reason": None}

    lowered = output.lower()
    if "timed out" in lowered or "request timed out" in lowered or "habis waktu" in lowered:
        reason = "timeout"
    else:
        reason = "unreachable"
    return {"method": "ping", "ok": False, "latency_ms": None, "reason": reason}
