"""nms.twofa — TOTP 2FA opsional untuk login web (Google Authenticator dsb.).

Data disimpan di tabel users (totp_secret terenkripsi via nms.crypto,
totp_recovery = JSON daftar HASH recovery code — plaintext tidak pernah
disimpan). Alur:

    login (password benar + totp_enabled) -> session["2fa_pending"] (5 menit)
    -> POST /login/2fa (kode 6 digit ATAU satu recovery code)
    -> login_user() + audit, persis alur login biasa.

Pembatas gagal per USERNAME (5 gagal -> blokir 5 menit): penghitung
in-memory per proses. Gunicorn di repo ini berjalan 1 worker (Dockerfile
dan systemd --workers 1), jadi aman; bila nanti >1 worker, penghitung
menjadi terpisah per worker (lebih lemah, bukan berlebih) — pertimbangkan
backing Redis saat itu.
"""

import hashlib
import hmac
import json
import secrets
import threading
import time

import pyotp

from nms.crypto import decrypt_secret, encrypt_secret
from nms.db import (
    clear_user_2fa,
    ensure_2fa_row,
    get_user_2fa,
    set_user_2fa,
    touch_last_login,
)
from nms.thread_safe import ThreadSafeDict

PENDING_TTL_S = 300  # masa tahan sesi "belum verifikasi 2FA"
_TOTP_VALID_WINDOW = 1  # toleransi 1 langkah (per spesifikasi)
_RECOVERY_CODES = 8
_FAILED_LIMIT = 5
_FAILED_BLOCK_S = 300


# ---- Gagal per username ------------------------------------------------------

_2fa_fails = ThreadSafeDict()
_2fa_lock = threading.Lock()


def _prune_2fa_fails(now_ts):
    with _2fa_lock:
        for key in list(_2fa_fails.keys()):
            fails, blocked_until, ts = _2fa_fails[key]
            if now_ts - ts > _FAILED_BLOCK_S * 2 and blocked_until <= now_ts:
                _2fa_fails.pop(key, None)


def fail_block_remaining(username, now_ts):
    """Sisa detik blokir per username; 0 bila tidak diblokir."""
    _prune_2fa_fails(now_ts)
    _, blocked_until, _ = _2fa_fails.get(username, (0, 0, 0))
    return max(0, int(blocked_until - now_ts))


def record_2fa_fail(username, now_ts):
    """Catat gagal verifikasi; blokir saat mencapai ambang. Kembalikan (fails, blokir_s)."""
    _prune_2fa_fails(now_ts)
    now_ts = now_ts or time.time()
    with _2fa_lock:
        fails, blocked_until, ts = _2fa_fails.get(username, (0, 0, 0))
        fails += 1
        if fails >= _FAILED_LIMIT:
            blocked_until = now_ts + _FAILED_BLOCK_S
            _2fa_fails[username] = (0, blocked_until, now_ts)
            return _FAILED_LIMIT, _FAILED_BLOCK_S
        _2fa_fails[username] = (fails, 0, now_ts)
        return fails, 0


def clear_2fa_failures(username):
    _2fa_fails.pop(username, None)


# ---- State -------------------------------------------------------------------


def state(username):
    """Baca state 2FA: {enabled, secret, last_step, recovery_hashes}."""
    row = get_user_2fa(username) or {}
    return {
        "enabled": int(row.get("totp_enabled") or 0) == 1,
        "secret": decrypt_secret(row.get("totp_secret") or ""),
        "last_step": int(row.get("totp_last_step") or 0),
        "recovery_hashes": _load_recovery_hash(row.get("totp_recovery") or ""),
    }


def enabled_for(username):
    try:
        return state(username)["enabled"]
    except Exception:
        return False


def _load_recovery_hash(raw):
    try:
        data = json.loads(raw or "[]")
        return data if isinstance(data, list) else []
    except (ValueError, TypeError):
        return []


def _store_secret(username, plaintext_secret):
    ok = set_user_2fa(username, totp_secret=encrypt_secret(plaintext_secret))
    if not ok:
        raise RuntimeError("gagal menulis state 2FA ke database")


def store_pending_secret(username, plaintext_secret):
    """Simpan secret hasil setup (belum aktif)."""
    _store_secret(username, plaintext_secret)


def enable(username, recovery_hashes):
    """Aktifkan 2FA + simpan daftar HASH recovery code."""
    ok = set_user_2fa(
        username,
        totp_enabled=1,
        totp_last_step=0,
        totp_recovery=json.dumps(list(recovery_hashes)),
    )
    if not ok:
        raise RuntimeError("gagal aktivasi 2FA di database")


def replace_secret_and_recover(
    username, plaintext_secret, recovery_hashes, last_step=0
):
    """Tulis secret baru + recovery baru + aktif (dipakai setelah setup)."""
    ok = set_user_2fa(
        username,
        totp_secret=encrypt_secret(plaintext_secret),
        totp_enabled=1,
        totp_last_step=int(last_step),
        totp_recovery=json.dumps(list(recovery_hashes)),
    )
    if not ok:
        raise RuntimeError("gagal menulis state 2FA ke database")


def disable(username):
    return clear_user_2fa(username)


def set_last_step(username, step):
    set_user_2fa(username, totp_last_step=int(step))


def store_recovery_hashes(username, recovery_hashes):
    return set_user_2fa(username, totp_recovery=json.dumps(list(recovery_hashes)))


def recovery_left(username):
    return len(state(username)["recovery_hashes"])


# ---- Kode --------------------------------------------------------------------


def normalize_recovery_code(value):
    """Normalkan recovery code: buang spasi, uppercase (format XXXX-YYYY)."""
    return "".join(str(value or "").split()).upper()


def recovery_sha256(normalized_code):
    """Hash recovery code — bandingkan TANPA tanda hubung (XXXXYYYY)."""
    return hashlib.sha256(
        normalize_recovery_code(normalized_code).replace("-", "").encode("utf-8")
    ).hexdigest()


def generate_recovery_codes(count=_RECOVERY_CODES):
    """(plaintext_codes, hashes) — plaintext hanya untuk respons sekali."""
    codes, hashes = [], []
    for _ in range(count):
        raw = secrets.token_hex(4).upper()  # 8 heksa = 32 bit entropi
        code = f"{raw[:4]}-{raw[4:]}"
        codes.append(code)
        hashes.append(recovery_sha256(code))
    return codes, hashes


def matched_totp_step(secret, code, now_ts=None):
    """Langkah TOTP yang cocok untuk kode, atau None.

    Comparing via hmac.compare_digest untuk tiap kandidat sel dalam
    valid_window=1 (now-1, now, now+1).
    """
    code = "".join(str(code or "").split())
    if not code.isdigit() or len(code) != 6:
        return None
    totp = pyotp.TOTP(secret)
    tc = (
        int(now_ts // totp.interval)
        if now_ts is not None
        else int(time.time() // totp.interval)
    )
    for delta in (0, -1, 1):
        step = tc + delta
        if step < 0:
            continue
        candidate = totp.generate_otp(step)
        if hmac.compare_digest(code, candidate):
            return step
    return None


def verify_login_code(username, code):
    """Verifikasi kode 6 digit ATAU recovery code untuk user 2FA aktif.

    Kembalikan (ok, alasan); alasan: "totp" | "recovery" | "bad" |
    "replay" | "not_enabled" | "no_secret".
    """
    st = state(username)
    if not st["enabled"]:
        return False, "not_enabled"
    if not st["secret"]:
        return False, "no_secret"

    normalized = normalize_recovery_code(code)
    digits = "".join(normalized.replace("-", "").split())
    is_totp = digits.isdigit() and len(digits) == 6 and "-" not in normalized

    if is_totp:
        step = matched_totp_step(st["secret"], digits)
        if step is None:
            return False, "bad"
        if step <= st["last_step"]:
            return False, "replay"
        set_last_step(username, step)
        return True, "totp"

    # Recovery code: mencocokkan sekali saja; hash yang cocok dihapus.
    cand_hash = recovery_sha256(normalized)
    for idx, known in enumerate(st["recovery_hashes"]):
        if hmac.compare_digest(str(known), cand_hash):
            remaining = [h for i, h in enumerate(st["recovery_hashes"]) if i != idx]
            if not store_recovery_hashes(username, remaining):
                return False, "bad"
            return True, "recovery"
    return False, "bad"


def verify_totp_only(username, code):
    """Verifikasi TOTP (tanpa recovery) — dipakai endpoint enable/disable.

    Kembalikan (ok, alasan) dengan alasan "totp" | "bad" | "replay" |
    "no_secret".
    """
    st = state(username)
    if not st["secret"]:
        return False, "no_secret"
    digits = "".join(str(code or "").split())
    if not (digits.isdigit() and len(digits) == 6):
        return False, "bad"
    step = matched_totp_step(st["secret"], digits)
    if step is None:
        return False, "bad"
    # Anti-replay (step <= last_step) hanya berlaku di gerbang LOGIN.
    # Endpoint disable/enable semacam ini diproteksi password + limiter,
    # jadi kode pada window yang sama boleh dipakai (last_step tetap
    # dirapikan ke langkah terbaru bila maju).
    if st["enabled"] and step > st["last_step"]:
        set_last_step(username, step)
    return True, "totp"


def consume_recovery_or_totp(username, code):
    """Untuk disable: terima kode TOTP rakus ATAU recovery code sekali pakai."""
    ok, reason = verify_totp_only(username, code)
    if ok:
        return True, "totp"
    normalized = normalize_recovery_code(code)
    st = state(username)
    cand_hash = recovery_sha256(normalized)
    for idx, known in enumerate(st["recovery_hashes"]):
        if hmac.compare_digest(str(known), cand_hash):
            remaining = [h for i, h in enumerate(st["recovery_hashes"]) if i != idx]
            if store_recovery_hashes(username, remaining):
                return True, "recovery"
    return False, "bad"
