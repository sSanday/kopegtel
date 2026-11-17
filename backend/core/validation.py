"""Validator bersama (perangkat, jaringan, protokol)."""
import ipaddress
import math
import os

from core.net import parse_allowed_networks, ip_default_allowed

ALLOWED_NETWORKS = parse_allowed_networks(os.environ.get("ALLOWED_NETWORKS", ""))

# SQLite INTEGER adalah signed 64-bit. Melampaui rentang ini, binding
# parameter query memicu OverflowError di sqlite3 -> HTTP 500. Semua
# nilai int dari klien wajib berada di [SQLITE_INT64_MIN, SQLITE_INT64_MAX].
SQLITE_INT64_MAX = 2**63 - 1
SQLITE_INT64_MIN = -(2**63)

# Nomor halaman query-string dikalikan per_page sebelum menjadi OFFSET.
# Batasi halaman agar hasil kali tetap jauh di bawah int64 meskipun per_page
# maksimum (500): 1.000.000 * 500 = 500 juta << 9,22e18.
QUERY_PAGE_MAX = 1_000_000

DEVICE_TYPES = {
    "mikrotik", "router", "switch", "access-point", "server",
    "olt", "ont", "firewall", "lainnya",
}

_SNMP_VERSIONS = {"v2c", "v3"}
_SNMP_AUTH_PROTOS = {"MD5", "SHA", "SHA-224", "SHA-256", "SHA-384", "SHA-512"}
_SNMP_PRIV_PROTOS = {"DES", "AES"}


def parse_finite_float(value):
    """float() yang menolak bool dan NaN/Infinity (bisa lolos dari JSON mentah).

    Return float terhingga, atau None bila ``value`` bukan angka terhingga.
    Dipakai untuk semua input angka dari klien: ``float('nan')``/``inf``
    pada JSON Python lolos ``float()`` dan jebol sampai ke validasi/DB.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        # True/False secara teknis int(1/0); input JSON "true" pada field
        # angka harus DITOLAK (400), bukan diam-diam menjadi 1.0/1.
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number):
        return None
    return number


def parse_finite_int(value):
    """int() yang menolak bool/float non-terhingga/NaN, pecahan, dan int64-overflow.

    Return int terhingga (integral) di [SQLITE_INT64_MIN, SQLITE_INT64_MAX],
    atau None. ``int()`` pada JSON ``1e999`` memicu OverflowError; pada
    ``nan`` memicu ValueError; pada pecahan seperti 2.9 diam-diam memotong -
    ketiganya diperlakukan sama: ditolak dengan None agar pemanggil
    mengembalikan 400. Bilangan bulat raksasa (mis. 10**30) juga ditolak
    di sini karena di luar signed 64-bit SQLite dan melempar OverflowError
    saat query di-bind. Input int/string digit diperiksa EKSAK (jalur
    float dilewati) agar 9223372036854775807 tidak ikut tertolak.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        # Input int (mis. dari JSON) - pemeriksaan eksak.
        if value > SQLITE_INT64_MAX or value < SQLITE_INT64_MIN:
            return None
        return value
    if isinstance(value, str):
        text = value.strip()
        try:
            exact = int(text, 10)  # eksak, arbitrary precision
        except ValueError:
            pass  # bukan digit murni (mis. "1e999", "2.5") -> jalur float
        else:
            if exact > SQLITE_INT64_MAX or exact < SQLITE_INT64_MIN:
                return None
            return exact
    number = parse_finite_float(value)
    if number is None:
        return None
    if not number.is_integer():
        return None
    result = int(number)  # eksak untuk double integral
    if result > SQLITE_INT64_MAX or result < SQLITE_INT64_MIN:
        return None
    return result


def _parse_ip(value):
    try:
        return ipaddress.ip_address(str(value).strip())
    except ValueError:
        return None


def _ip_allowed(addr):
    if not ALLOWED_NETWORKS:
        # Tanpa ALLOWED_NETWORKS, hanya rentang privat yang boleh dipakai
        # (dokumentasi .env.example). Jangan mengizinkan semua IP.
        return ip_default_allowed(addr)
    return any(addr in network for network in ALLOWED_NETWORKS)


def _parse_port(value, field):
    try:
        port = int(str(value).strip())
    except (TypeError, ValueError):
        return None, f"{field} harus berupa angka."
    if port < 1 or port > 65535:
        return None, f"{field} harus antara 1 dan 65535."
    return port, None


def _parse_interval(value):
    try:
        interval = int(str(value).strip())
    except (TypeError, ValueError):
        return None, "Interval harus berupa angka (detik)."
    if interval < 10:
        return None, "Interval minimal 10 detik."
    if interval > 86400:
        return None, "Interval maksimal 86400 detik (1 hari)."
    return interval, None


def _parse_optional_id(value, field):
    if value in (None, "", "null"):
        return None, None
    number = parse_finite_int(value)
    if number is None:
        return None, f"{field} harus berupa angka id atau kosong."
    if number < 1:
        return None, f"{field} tidak valid."
    # Batas atas (signed 64-bit) sudah dijamin parse_finite_int.
    return number, None


def parse_query_int(raw, default, *, min_value, max_value):
    """int() untuk query-string yang TIDAK PERNAH melempar.

    Nilai bukan angka / kosong -> ``default``; hasil di-clamp ke
    [``min_value``, ``max_value``]. Dipakai untuk ``?limit=abc`` dsb.
    yang sebelumnya melempar ValueError dan berakhir HTTP 500.
    """
    if raw is None:
        return default
    try:
        number = int(str(raw).strip(), 10)
    except (TypeError, ValueError, OverflowError):
        return default
    if number < min_value:
        return min_value
    if number > max_value:
        return max_value
    return number
