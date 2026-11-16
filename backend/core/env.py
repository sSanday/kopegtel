"""Versi aplikasi & waktu mulai (dipakai bersama)."""
import os
import time

APP_VERSION = os.environ.get("APP_VERSION", "1.0.0")
SERVER_STARTED = time.monotonic()


def env_flag(name):
    """True bila variabel lingkungan di-set ke 1/true/yes/on (case-insensitive)."""
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def env_int(name, default, minimum=None, maximum=None):
    """int dari variabel lingkungan dengan validasi.

    Return ``default`` bila variabel kosong; raise ValueError dengan pesan
    jelas bila bukan angka/di luar rentang (bukan traceback OverflowError).
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} harus berupa angka, bukan {raw!r}.") from None
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} minimal {minimum}, bukan {value}.")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} maksimal {maximum}, bukan {value}.")
    return value


def env_float(name, default, minimum=None, maximum=None):
    """float dari variabel lingkungan dengan validasi (NaN/Infinity ditolak)."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"{name} harus berupa angka, bukan {raw!r}.") from None
    if value != value or value in (float("inf"), float("-inf")):  # NaN/Inf
        raise ValueError(f"{name} harus berupa angka terhingga, bukan {raw!r}.")
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} minimal {minimum}, bukan {value}.")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} maksimal {maximum}, bukan {value}.")
    return value
