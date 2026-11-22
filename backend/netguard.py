"""Validasi target koneksi keluar (anti-SSRF) dan resolusi DNS.

Webhook dan SMTP memakai modul ini agar hanya alamat publik yang boleh
dihubungi, kecuali kanal (khusus admin) atau environment secara eksplisit
mengizinkan target privat. Resolusi mengembalikan daftar IP dan pemanggil
harus terhubung ke salah satu IP tersebut, bukan me-resolve host lagi, agar
serangan DNS rebinding tidak dapat mengarahkan koneksi ke jaringan internal.
"""

import ipaddress
import re
import socket

from core.env import env_flag  # satu sumber; di-re-export untuk pemanggil lama

_V4_BLOCKED_NETS = tuple(
    ipaddress.ip_network(net)
    for net in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "224.0.0.0/4",
        "240.0.0.0/4",
    )
)
_V6_BLOCKED_NETS = tuple(
    ipaddress.ip_network(net)
    for net in (
        "::/128",
        "::1/128",
        "::ffff:0:0/96",
        "64:ff9b::/96",
        "2001::/32",
        "2001:db8::/32",
        "2002::/16",
        "fc00::/7",
        "fe80::/10",
        "fec0::/10",
        "ff00::/8",
    )
)
_HOST_RE = re.compile(r"^(?=.{1,253}\.?$)[A-Za-z0-9_](?:[A-Za-z0-9._-]*[A-Za-z0-9_])?\.?$")


def as_bool(value):
    """Konversi nilai JSON/string menjadi boolean tanpa jebakan bool("false")."""
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def env_allows_private_smtp():
    return env_flag("ALLOW_PRIVATE_SMTP")


def ip_blocked(ip):
    """True bila IP bukan alamat publik yang aman untuk koneksi keluar."""
    if ip.version == 6 and ip.ipv4_mapped is not None:
        return ip_blocked(ip.ipv4_mapped)
    nets = _V6_BLOCKED_NETS if ip.version == 6 else _V4_BLOCKED_NETS
    if any(ip in network for network in nets):
        return True
    return (
        not ip.is_global
        or ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or (ip.version == 6 and ip.is_site_local)
    )


def any_private(addresses):
    """True bila daftar alamat memuat setidaknya satu IP non-publik."""
    for address in addresses or ():
        try:
            if ip_blocked(ipaddress.ip_address(address)):
                return True
        except ValueError:
            return True
    return False


def normalize_host(host):
    """Normalisasi host (IP literal atau nama DNS) tanpa skema/path/port.

    Raise ValueError untuk input yang bisa menyamarkan target, mis. host yang
    memuat kredensial, path, spasi, atau port.
    """
    raw = str(host or "").strip()
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1]
    if not raw:
        raise ValueError("Host wajib diisi.")
    if len(raw) > 253:
        raise ValueError("Host terlalu panjang.")
    if any(ch.isspace() for ch in raw) or any(ch in raw for ch in "/@?#"):
        raise ValueError("Host tidak valid.")
    try:
        return str(ipaddress.ip_address(raw))
    except ValueError:
        pass
    if ":" in raw or ".." in raw:
        raise ValueError("Host tidak valid.")
    try:
        ascii_host = raw.encode("idna").decode("ascii")
    except UnicodeError:
        raise ValueError("Host tidak valid.") from None
    if not _HOST_RE.fullmatch(ascii_host):
        raise ValueError("Host tidak valid.")
    return ascii_host.rstrip(".") or ascii_host


def resolve_addresses(host, port, *, allow_private=False, resolver=None):
    """Resolve host ke daftar IP yang sudah divalidasi.

    ``resolver`` dapat diganti untuk pengujian; secara default memakai
    ``socket.getaddrinfo``. Bila ``allow_private`` false, seluruh hasil
    resolusi harus publik. Ini mencegah campuran IP publik dan privat pada
    satu nama DNS.
    """
    host = normalize_host(host)
    try:
        port = int(port)
    except (TypeError, ValueError):
        raise ValueError("Port tidak valid.") from None
    if not 1 <= port <= 65535:
        raise ValueError("Port tidak valid.")

    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None

    if literal is not None:
        addresses = [str(literal)]
    else:
        getaddrinfo = resolver or socket.getaddrinfo
        try:
            infos = getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
        except OSError:
            raise ValueError("DNS gagal diselesaikan.") from None
        except Exception:  # noqa: BLE001 - jangan bocorkan detail resolver
            raise ValueError("DNS gagal diselesaikan.") from None
        addresses = []
        for info in infos or ():
            try:
                addresses.append(str(ipaddress.ip_address(info[4][0])))
            except (IndexError, TypeError, ValueError):
                raise ValueError("Resolusi DNS tidak valid.") from None
        if not addresses:
            raise ValueError("DNS tidak menghasilkan alamat.")

    unique = list(dict.fromkeys(addresses))
    for address in unique:
        ip = ipaddress.ip_address(address)
        if not allow_private and ip_blocked(ip):
            # Jangan bocorkan hasil resolusi internal ke klien/operator.
            raise ValueError("Target bukan IP publik.")
    return unique
