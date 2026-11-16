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
# Baris "balasan" Windows/Linux (EN/ID): "Reply from", "Balasan dari",
# "bytes from" (Linux). Tanpa token ini, output dianggap BUKAN balasan sukses.
_REPLY_RE = re.compile(r"(reply from|balasan dari|bytes from)", re.IGNORECASE)
# Baris kegagalan Windows/Linux (EN/ID). Windows dapat mengembalikan exit
# code 0 walau ICMP gagal (mis. "Destination host unreachable" dari stack
# lokal), jadi returncode == 0 SAJA tidak cukup (L-6).
_FAILURE_RE = re.compile(
    r"(timed out|habisan waktu|host unreachable|tidak dapat dijangkau|"
    r"destination host|general failure|kesalahan umum|ttl expired|ttl habis|"
    r"expired in transit|undeliverable)",
    re.IGNORECASE,
)


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
    latency_ms = None
    match = _LATENCY_RE.search(output)
    if match:
        latency_ms = float(match.group(1).replace(",", "."))

    # Returncode 0 tidak cukup: balasannya harus terdeteksi DAN bukan baris
    # kegagalan ("Destination host unreachable" bisa bernilai 0 di Windows).
    reachable = (
        result.returncode == 0
        and _FAILURE_RE.search(output) is None
        and bool(_REPLY_RE.search(output) or latency_ms is not None)
    )

    if reachable:
        return {"method": "ping", "ok": True, "latency_ms": latency_ms, "reason": None}

    lowered = output.lower()
    if "timed out" in lowered or "request timed out" in lowered or "habis waktu" in lowered:
        reason = "timeout"
    else:
        reason = "unreachable"
    return {"method": "ping", "ok": False, "latency_ms": None, "reason": reason}
