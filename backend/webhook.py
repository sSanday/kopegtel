"""Webhook: validasi SSRF, tanda tangan HMAC, dan pengiriman aman.

- URL harus https (http hanya bila kanal mengizinkan eksplisit atau env
  ALLOW_PRIVATE_WEBHOOKS / allow_http di config).
- Target ditolak bila menunjuk loopback/link-local/privat kecuali
  ALLOW_PRIVATE_WEBHOOKS=1 (env) atau allow_private=true pada kanal.
- Tidak mengikuti redirect; timeout 5 detik; respons dibatasi.
- Tanda tangan: X-NMS-Timestamp (unix) dan X-NMS-Signature:
  sha256=<hex(HMAC-SHA256(secret, "<timestamp>.<body>"))>
- Body dibangun lewat json.dumps; secret tidak pernah di-log.
"""

import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import socket
import time
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import urlsplit

log = logging.getLogger("nms.webhook")

TIMEOUT_SECONDS = 5
MAX_RESPONSE_BYTES = 4096
MAX_HEADER_VALUE = 512
_HEADER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_BLOCKED_PREFIXES = (
    # IPv4
    ("0.0.0.0", "0.255.255.255"),
    ("10.0.0.0", "10.255.255.255"),
    ("100.64.0.0", "100.127.255.255"),
    ("127.0.0.0", "127.255.255.255"),
    ("169.254.0.0", "169.254.255.255"),
    ("172.16.0.0", "172.31.255.255"),
    ("192.168.0.0", "192.168.255.255"),
    ("224.0.0.0", "239.255.255.255"),
    ("240.0.0.0", "255.255.255.255"),
)
_V6_BLOCKED_NETS = tuple(
    ipaddress.ip_network(net)
    for net in ("::/128", "::1/128", "::ffff:0.0.0.0/96", "64:ff9b::/96",
                "2001:db8::/32", "fc00::/7", "fe80::/10", "ff00::/8")
)
_BLOCKED_HEADERS = {"host", "content-length", "content-type", "connection",
                    "x-nms-timestamp", "x-nms-signature", "transfer-encoding", "upgrade"}


def env_allows_private():
    return os.environ.get("ALLOW_PRIVATE_WEBHOOKS", "").strip().lower() in ("1", "true", "yes", "on")


def _parse_url(url):
    raw = str(url or "").strip()
    if not raw:
        raise ValueError("URL webhook wajib diisi.")
    try:
        parts = urlsplit(raw)
    except ValueError:
        raise ValueError("URL webhook tidak valid.") from None
    scheme = (parts.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise ValueError("URL webhook hanya boleh http(s).")
    if parts.username or parts.password:
        raise ValueError("URL webhook tidak boleh memuat kredensial.")
    host = (parts.hostname or "").strip().lower()
    if not host:
        raise ValueError("Host webhook wajib diisi.")
    if host.endswith(".localhost") or host == "localhost":
        raise ValueError("Target loopback tidak diizinkan.")
    try:
        port = parts.port
    except ValueError:
        raise ValueError("Port URL tidak valid.") from None
    if port is not None and not (1 <= port <= 65535):
        raise ValueError("Port URL tidak valid.")
    return parts, scheme, host, (port or (443 if scheme == "https" else 80))


def _ip_blocked(ip):
    """True bila IP bukan publik (loopback/privat/link-local/reserved)."""
    if ip.version == 6:
        if ip.ipv4_mapped is not None:
            return _ip_blocked(ip.ipv4_mapped)
        if any(ip in net for net in _V6_BLOCKED_NETS):
            return True
        return ip.is_loopback or ip.is_multicast or ip.is_unspecified or ip.is_private or ip.is_reserved
    first, second, third, fourth = (int(part) for part in str(ip).split("."))
    for a, b in _BLOCKED_PREFIXES:
        start = [int(p) for p in a.split(".")]
        end = [int(p) for p in b.split(".")]
        if start <= [first, second, third, fourth] <= end:
            return True
    return False


def validate_webhook_target(url, *, allow_private=False, allow_http=None):
    """Validasi target + resolusi DNS. Raise ValueError bila ditolak."""
    parts, scheme, host, port = _parse_url(url)
    if allow_http is None:
        allow_http = scheme == "http"
    if scheme == "http" and not allow_http:
        raise ValueError("http hanya diizinkan bila kanal ini mengizinkan eksplisit.")
    if allow_private or env_allows_private():
        return parts
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        raise ValueError(f"DNS gagal diselesaikan: {type(exc).__name__}") from None
    resolved = {info[4][0] for info in infos}
    for address in resolved:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            raise ValueError("Resolusi DNS tidak valid.") from None
        if _ip_blocked(ip):
            raise ValueError(f"Target bukan IP publik ({address}).")
    return parts


def _payload(preset, subject, text, problem):
    problem = problem or {}
    if preset == "slack":
        return {"text": f"{subject}\n{text}"}
    if preset == "discord":
        return {"content": f"{subject}\n{text}"}
    payload = {
        "subject": subject,
        "text": text,
        "source": "nms-kopegtel",
    }
    if problem.get("severity"):
        payload["severity"] = problem["severity"]
    if problem.get("kind"):
        payload["problem_kind"] = problem["kind"]
    problem_id = problem.get("problem_id") or problem.get("id")
    if problem_id:
        payload["problem_id"] = problem_id
    if problem.get("device_id"):
        payload["device_id"] = problem["device_id"]
    if problem.get("device_name"):
        payload["device"] = problem["device_name"]
    return payload


def _sanitize_headers(headers):
    clean = {}
    if not isinstance(headers, dict):
        return clean
    for raw_name, raw_value in list(headers.items())[:20]:
        name = str(raw_name or "").strip()[:64]
        value = str(raw_value or "")[:MAX_HEADER_VALUE]
        if not _HEADER_NAME_RE.match(name):
            continue
        if name.lower() in _BLOCKED_HEADERS:
            continue
        clean[name] = value
    return clean


def send_webhook(config, subject, text, problem=None):
    """POST ke webhook. Return (ok, detail, attempts). Secret tidak di-log."""
    url = config.get("url") or ""
    allow_http = bool(config.get("allow_http"))
    allow_private = bool(config.get("allow_private"))
    scheme = (urlsplit(url).scheme or "").lower()
    if scheme == "http" and not allow_http:
        return False, "URL ditolak: http memerlukan izin eksplisit pada kanal.", 0
    try:
        validate_webhook_target(url, allow_private=allow_private, allow_http=allow_http)
    except ValueError as exc:
        return False, f"URL ditolak: {exc}", 0

    secret = str(config.get("secret") or "")
    if not secret:
        return False, "Secret webhook belum diatur.", 0

    preset = (config.get("preset") or "generic").lower()
    if preset not in ("slack", "discord", "generic"):
        preset = "generic"
    body = json.dumps(_payload(preset, subject, text, problem), ensure_ascii=False).encode("utf-8")
    timestamp = str(int(time.time()))
    signature = hmac.new(secret.encode("utf-8"), f"{timestamp}.".encode("utf-8") + body,
                         hashlib.sha256).hexdigest()
    headers = _sanitize_headers(config.get("headers") or {})
    headers.update({
        "Content-Type": "application/json",
        "X-NMS-Timestamp": timestamp,
        "X-NMS-Signature": f"sha256={signature}",
        "User-Agent": "NMS-Kopegtel-Webhook/1.0",
    })

    class _NoRedirect(urlrequest.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    # Proxy dimatikan: keputusan SSRF dilakukan lokal; proxy kampus bisa
    # mengarahkan ulang ke alamat internal yang tidak diperiksa DNS kita.
    opener = urlrequest.build_opener(_NoRedirect(), urlrequest.ProxyHandler({}))
    max_response = int(config.get("max_response_bytes") or MAX_RESPONSE_BYTES)
    max_response = min(MAX_RESPONSE_BYTES, max(256, max_response))

    delays = (1.0, 2.0)
    last_detail = "Belum dicoba."
    for attempt in range(1, 4):
        started = time.monotonic()
        request = urlrequest.Request(url, data=body, headers=headers, method="POST")
        try:
            with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
                response.read(max_response + 1)
                status = response.status
            duration = int((time.monotonic() - started) * 1000)
            if 200 <= status < 300:
                return True, f"HTTP {status} ({duration} ms)", attempt
            last_detail = f"HTTP {status} ({duration} ms)"
        except urlerror.HTTPError as exc:
            duration = int((time.monotonic() - started) * 1000)
            last_detail = f"HTTP {exc.code} ({duration} ms)"
        except urlerror.URLError as exc:
            last_detail = f"Koneksi gagal ({type(exc.reason).__name__})"
        except TimeoutError:
            last_detail = "Koneksi timeout (5 detik)."
        except OSError as exc:
            last_detail = f"Koneksi gagal ({type(exc).__name__})"
        except Exception as exc:  # noqa: BLE001 - detail aman di-log (tanpa URL/secret)
            last_detail = f"Gagal mengirim ({type(exc).__name__})"
        if attempt < 3:
            time.sleep(delays[attempt - 1])
    return False, last_detail, 3
