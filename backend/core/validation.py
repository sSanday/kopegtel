"""Validator bersama (perangkat, jaringan, protokol)."""
import ipaddress
import math
import os

from core.net import parse_allowed_networks, ip_default_allowed

ALLOWED_NETWORKS = parse_allowed_networks(os.environ.get("ALLOWED_NETWORKS", ""))

DEVICE_TYPES = {
    "mikrotik", "router", "switch", "access-point", "server",
    "olt", "ont", "firewall", "lainnya",
}

_SNMP_VERSIONS = {"v2c", "v3"}
_SNMP_AUTH_PROTOS = {"MD5", "SHA", "SHA-224", "SHA-256", "SHA-384", "SHA-512"}
_SNMP_PRIV_PROTOS = {"DES", "AES"}


def parse_finite_float(value):
    """float() yang menolak NaN/Infinity (bisa lolos dari JSON mentah).

    Return float terhingga, atau None bila ``value`` bukan angka terhingga.
    Dipakai untuk semua input angka dari klien: ``float('nan')``/``inf``
    pada JSON Python lolos ``float()`` dan jebol sampai ke validasi/DB.
    """
    if value is None:
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
    """int() yang menolak bool/float non-terhingga/NaN dan pecahan.

    Return int terhingga (integral), atau None. ``int()`` pada JSON
    ``1e999`` memicu OverflowError; pada ``nan`` memicu ValueError; pada
    pecahan seperti 2.9 diam-diam memotong - ketiganya diperlakukan sama:
    ditolak dengan None agar pemanggil mengembalikan 400.
    """
    number = parse_finite_float(value)
    if number is None:
        return None
    if not number.is_integer():
        return None
    return int(number)


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
    return number, None
