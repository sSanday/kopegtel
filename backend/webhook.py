"""Webhook: validasi SSRF, tanda tangan HMAC, dan pengiriman aman.

- URL harus https (http hanya bila kanal mengizinkan eksplisit atau env
  ALLOW_PRIVATE_WEBHOOKS / allow_http di config).
- Target ditolak bila menunjuk loopback/link-local/privat kecuali
  allow_private=true pada kanal. ALLOW_PRIVATE_WEBHOOKS=1 adalah escape hatch
  tingkat deploy; saat admin menyimpan kanal, izin itu dipromosikan menjadi
  allow_private agar kebijakan tersimpan eksplisit dan tidak bisa dipakai
  operator.
- Host di-resolve sekali, lalu koneksi memakai IP hasil resolusi tersebut
  (DNS pinning) sehingga tidak rentan DNS rebinding.
- Tidak mengikuti redirect; timeout 5 detik; respons dibatasi.
- Tanda tangan: X-NMS-Timestamp (unix) dan X-NMS-Signature:
  sha256=<hex(HMAC-SHA256(secret, "<timestamp>.<body>"))>
- Body dibangun lewat json.dumps; secret tidak pernah di-log.
"""

import hashlib
import hmac
import http.client
import json
import logging
import re
import socket
import time
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import urlsplit

import netguard

log = logging.getLogger("nms.webhook")

TIMEOUT_SECONDS = 5
MAX_RESPONSE_BYTES = 4096
MAX_HEADER_VALUE = 512
_HEADER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_BLOCKED_HEADERS = {"host", "content-length", "content-type", "connection",
                    "x-nms-timestamp", "x-nms-signature", "transfer-encoding", "upgrade"}


def env_allows_private():
    return netguard.env_flag("ALLOW_PRIVATE_WEBHOOKS")


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
    raw_host = (parts.hostname or "").strip()
    if not raw_host:
        raise ValueError("Host webhook wajib diisi.")
    try:
        host = netguard.normalize_host(raw_host)
    except ValueError as exc:
        raise ValueError(str(exc)) from None
    try:
        port = parts.port
    except ValueError:
        raise ValueError("Port URL tidak valid.") from None
    if port is not None and not (1 <= port <= 65535):
        raise ValueError("Port URL tidak valid.")
    return parts, scheme, host, (port or (443 if scheme == "https" else 80))


def _ip_blocked(ip):
    """True bila IP bukan publik (loopback/privat/link-local/reserved)."""
    return netguard.ip_blocked(ip)


def _resolve_webhook_target(url, *, allow_private=False, allow_http=None, allow_env=True):
    """Validasi target dan kembalikan (parts, daftar IP hasil resolusi).

    ``allow_env`` hanya dipakai pemanggil tepercaya (mis. alat admin); alur
    kanal selalu meneruskan kebijakan target yang tersimpan eksplisit.
    """
    parts, scheme, host, port = _parse_url(url)
    if allow_http is None:
        allow_http = scheme == "http"
    if scheme == "http" and not allow_http:
        raise ValueError("http hanya diizinkan bila kanal ini mengizinkan eksplisit.")
    effective_private = allow_private or (allow_env and env_allows_private())
    # localhost hanya boleh untuk kanal yang secara eksplisit diizinkan admin.
    if (host.lower() == "localhost" or host.lower().endswith(".localhost")) and not effective_private:
        raise ValueError("Target loopback tidak diizinkan.")
    addresses = netguard.resolve_addresses(
        host,
        port,
        allow_private=effective_private,
        resolver=socket.getaddrinfo,
    )
    return parts, addresses


def validate_webhook_target(url, *, allow_private=False, allow_http=None, allow_env=True):
    """Validasi target + resolusi DNS. Raise ValueError bila ditolak."""
    parts, _addresses = _resolve_webhook_target(
        url, allow_private=allow_private, allow_http=allow_http, allow_env=allow_env
    )
    return parts


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """HTTPConnection yang selalu terhubung ke IP hasil validasi."""

    _pinned_address = None

    def connect(self):
        self.sock = socket.create_connection(
            (self._pinned_address, self.port), self.timeout, self.source_address
        )
        if self._tunnel_host:
            self._tunnel()


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPSConnection dengan IP terpin dan SNI/verifikasi nama asli."""

    _pinned_address = None

    def connect(self):
        self.sock = socket.create_connection(
            (self._pinned_address, self.port), self.timeout, self.source_address
        )
        if self._tunnel_host:
            self._tunnel()
            server_hostname = self._tunnel_host
        else:
            server_hostname = self.host
        self.sock = self._context.wrap_socket(self.sock, server_hostname=server_hostname)


class _PinnedHTTPHandler(urlrequest.HTTPHandler):
    def __init__(self, address):
        super().__init__()
        self._address = address

    def http_open(self, req):
        connection = type(
            "_PinnedHTTP", (_PinnedHTTPConnection,), {"_pinned_address": self._address}
        )
        return self.do_open(connection, req)


class _PinnedHTTPSHandler(urlrequest.HTTPSHandler):
    def __init__(self, address):
        super().__init__()
        self._address = address

    def https_open(self, req):
        connection = type(
            "_PinnedHTTPS", (_PinnedHTTPSConnection,), {"_pinned_address": self._address}
        )
        return self.do_open(connection, req, context=self._context)


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
    allow_http = netguard.as_bool(config.get("allow_http"))
    allow_private = netguard.as_bool(config.get("allow_private"))
    scheme = (urlsplit(url).scheme or "").lower()
    if scheme == "http" and not allow_http:
        return False, "URL ditolak: http memerlukan izin eksplisit pada kanal.", 0
    try:
        _parts, addresses = _resolve_webhook_target(
            url, allow_private=allow_private, allow_http=allow_http, allow_env=False
        )
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
    # Setiap percobaan memakai IP hasil resolusi awal (bukan resolve ulang).
    max_response = int(config.get("max_response_bytes") or MAX_RESPONSE_BYTES)
    max_response = min(MAX_RESPONSE_BYTES, max(256, max_response))

    delays = (1.0, 2.0)
    last_detail = "Belum dicoba."
    for attempt in range(1, 4):
        address = addresses[(attempt - 1) % len(addresses)]
        opener = urlrequest.build_opener(
            _NoRedirect(),
            urlrequest.ProxyHandler({}),
            _PinnedHTTPHandler(address),
            _PinnedHTTPSHandler(address),
        )
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
