"""nms.service_checks — logika pemeriksaan service per tipe (tcp/http/icmp/dns).

Satu fungsi kecil per tipe, murni mudah dites (mock requests / dnspython /
ping_host), tanpa akses DB. Orkestrasi timing & status dilakukan run_check().
"""

import ipaddress
import socket
import time

import dns.resolver
import requests

from nms.monitor import ping_host

_TCP_TIMEOUT = 5
_HTTP_TIMEOUT = 5
# Spesifikasi timeout DNS 5 detik total: dibagi dua karena tiap probe
# menanyakan Tipe A dan AAAA.
_DNS_LIFETIME = 2.5
# Batas pembacaan body HTTP untuk pencarian keyword (hemat memori).
_HTTP_BODY_LIMIT = 64 * 1024


# ---- Tipe: TCP ---------------------------------------------------------------


def check_tcp_service(ip, port):
    """Benar bila koneksi TCP dapat dibuat. Exception apa pun = gagal."""
    try:
        port_int = int(port)
        if not 1 <= port_int <= 65535:
            raise ValueError("port out of range")
        with socket.create_connection((ip, port_int), timeout=_TCP_TIMEOUT):
            return True
    except Exception:
        return False


# ---- Tipe: HTTP --------------------------------------------------------------


def check_http_service(url, expected="", is_url_allowed=None):
    """Benar bila status < 400 dan (bila keyword diisi) ada di body.

    Body dibaca streaming dan dibatasi _HTTP_BODY_LIMIT byte.
    """
    try:
        url = (url or "").strip()
        if not url:
            return False
        if is_url_allowed is None:
            from urllib.parse import urlparse

            if (urlparse(url).scheme or "").lower() not in ("http", "https"):
                return False
        elif not is_url_allowed(url):
            return False
        import os

        import urllib3

        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        needle = (expected or "").strip().lower()
        with requests.get(
            url,
            timeout=_HTTP_TIMEOUT,
            verify=os.environ.get("NMS_SSL_VERIFY", "1") != "0",
            headers={"User-Agent": "NMS-KOPEGTEL/1.0"},
            stream=True,
        ) as r:
            if r.status_code >= 400:
                return False
            if not needle:
                return True
            buf = b""
            for chunk in r.iter_content(chunk_size=8192):
                if not chunk:
                    continue
                buf += chunk
                if len(buf) >= _HTTP_BODY_LIMIT:
                    buf = buf[:_HTTP_BODY_LIMIT]
                    break
            return needle in buf.decode("utf-8", "ignore").lower()
    except Exception:
        return False


# ---- Tipe: ICMP --------------------------------------------------------------


def check_icmp_service(ip):
    """Benar bila packet loss < 100%. Kembalikan (ok, latency_avg_ms)."""
    try:
        latency, loss = ping_host(ip)
    except Exception:
        return False, 0.0
    try:
        loss = float(loss)
    except (TypeError, ValueError):
        return False, 0.0
    try:
        latency = float(latency)
    except (TypeError, ValueError):
        latency = 0.0
    if loss < 100.0:
        return True, max(0.0, latency)
    return False, 0.0


# ---- Tipe: DNS ---------------------------------------------------------------


def _norm_addr(value):
    """Normalisasi alamat IP untuk perbandingan (kanonik + lowercase)."""
    text = str(value or "").strip()
    try:
        return str(ipaddress.ip_address(text)).lower()
    except ValueError:
        return text.lower()


def _dns_server_ips(server):
    """Daftar IP nameserver dari input (IP literal atau hostname)."""
    server = (server or "").strip()
    if not server:
        return []
    try:
        ipaddress.ip_address(server)
        return [server]
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(server, 53, type=socket.SOCK_DGRAM)
    except OSError:
        return []
    out = []
    for info in infos:
        ip = info[4][0]
        if ip not in out:
            out.append(ip)
        if len(out) >= 3:
            break
    return out


def dns_lookup_answers(server, query):
    """Kumpulan jawaban A + AAAA dari server DNS. Kosong = tidak jawab."""
    resolver = dns.resolver.Resolver(configure=False)
    resolver.nameservers = _dns_server_ips(server)
    resolver.lifetime = _DNS_LIFETIME
    resolver.timeout = _DNS_LIFETIME
    answers = set()
    for rtype in ("A", "AAAA"):
        try:
            resp = resolver.resolve(query, rtype)
        except Exception:
            continue
        for rdata in resp:
            answers.add(_norm_addr(rdata))
    return answers


def check_dns_service(server, query, expected=""):
    """Benar bila server menjawab; bila expected diisi, harus cocok."""
    try:
        if not (query or "").strip():
            return False
        answers = dns_lookup_answers(server, query)
    except Exception:
        return False
    if not answers:
        return False
    want = (expected or "").strip()
    if not want:
        return True
    return _norm_addr(want) in answers


# ---- Orkestrator -------------------------------------------------------------


def run_check(svc_type, ip, port, url, query="", expected="", is_url_allowed=None):
    """Jalankan satu pemeriksaan. Kembalikan (status, latency_ms).

    Status ONLINE/OFFLINE/PENDING; latency 0 untuk OFFLINE. Tipe tidak
    dikenal menghasilkan PENDING dengan latency terukur (perilaku lama).
    """
    start = time.time()
    svc_type = (svc_type or "").strip().lower()
    latency = None
    if svc_type == "tcp":
        ok = check_tcp_service(ip, port)
    elif svc_type == "http":
        ok = check_http_service(url, expected, is_url_allowed)
    elif svc_type == "icmp":
        ok, latency = check_icmp_service(ip)
    elif svc_type == "dns":
        ok = check_dns_service(ip, query, expected)
    else:
        return "PENDING", (time.time() - start) * 1000

    if not ok:
        return "OFFLINE", 0
    if latency is None:
        latency = (time.time() - start) * 1000
    return "ONLINE", latency
