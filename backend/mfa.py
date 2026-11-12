"""E2 — MFA TOTP (RFC 6238: SHA1, 6 digit, 30 detik), kode pemulihan, kebijakan.

- Secret dibuat server; disimpan terenkripsi Fernet.
- Kode pemulihan (10) sekali-pakai; disimpan sebagai hash SHA-256.
- Pola URI: otpauth://totp/NMS%20Kopegtel:<username>?secret=...&issuer=NMS%20Kopegtel
- QR SVG dibuat server via segno (tanpa CDN).
- Anti-replay: kode TOTP pada step waktu yang sama (atau lebih lama) ditolak.
- Pembatasan: 5 gagal per 10 menit per akun+IP (login_attempts, username='mfa:<id>').
"""

import base64
import hashlib
import hmac
import io
import json
import logging
import secrets
import struct
import time
from datetime import datetime, timezone

import db

log = logging.getLogger("nms.mfa")

_STEP_SECONDS = 30
_DIGITS = 6
_ISSUER = "NMS Kopegtel"
RECOVERY_COUNT = 10
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # tanpa I,L,O,0,1
MFA_ATTEMPT_WINDOW_MINUTES = 10
MFA_MAX_FAILURES = 5


def new_secret():
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def _decode_secret(secret):
    text = str(secret or "").strip().upper().replace(" ", "")
    padding = "=" * (-len(text) % 8)
    return base64.b32decode(text + padding)


def _hotp(key, counter):
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return code % (10 ** _DIGITS)


def totp(secret, at=None):
    moment = int(at if at is not None else time.time())
    return _hotp(_decode_secret(secret), moment // _STEP_SECONDS)


def _hotp_str(secret, at=None):
    return f"{totp(secret, at):0{_DIGITS}d}"


def verify_totp(secret, code, *, window=1, last_step=0, at=None):
    """Return step yang cocok (untuk anti-replay) atau None."""
    digits = "".join(ch for ch in str(code or "") if ch.isdigit())
    if len(digits) != _DIGITS:
        return None
    key = _decode_secret(secret)
    now_step = int(at if at is not None else time.time()) // _STEP_SECONDS
    for step in range(now_step - window, now_step + window + 1):
        if step <= int(last_step or 0):
            continue
        if hmac.compare_digest(f"{_hotp(key, step):0{_DIGITS}d}", digits):
            return step
    return None


def new_recovery_codes():
    codes = []
    for _ in range(RECOVERY_COUNT):
        raw = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(10))
        codes.append(f"{raw[:5]}-{raw[5:]}")
    return codes


def hash_recovery_code(code):
    normalized = str(code or "").upper().replace("-", "").replace(" ", "")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def consume_recovery_code(hashes, code):
    """Verifikasi + hapus kode pemulihan. Return (ok, hashes_tersisa)."""
    target = hash_recovery_code(code)
    remaining = list(hashes or [])
    try:
        remaining.remove(target)
    except ValueError:
        return False, list(hashes or [])
    return True, remaining


def otpauth_uri(secret, username, org_name):
    from urllib.parse import quote

    label = quote(f"{_ISSUER}:{username}")
    issuer = quote(org_name or _ISSUER)
    return (
        f"otpauth://totp/{label}?secret={secret}&issuer={issuer}"
        f"&algorithm=SHA1&digits={_DIGITS}&period={_STEP_SECONDS}"
    )


def qr_png_svg(uri, scale=4):
    """Return SVG (str) QR dari URI otpauth (segno, tanpa CDN)."""
    import segno

    qr = segno.make(uri, error="m")
    buffer = io.BytesIO()
    qr.save(buffer, kind="svg", scale=scale, border=2)
    return buffer.getvalue().decode("utf-8")


# ---------------------------------------------------------
# Kebijakan MFA (administrasi)
# ---------------------------------------------------------

_VALID_ROLES = {"admin", "operator", "viewer"}


def policy_roles():
    raw = db.get_setting("mfa_required_roles", "[]")
    try:
        roles = json.loads(raw or "[]")
    except (TypeError, ValueError):
        roles = []
    return [role for role in roles if role in _VALID_ROLES]


def policy_grace_days():
    try:
        return max(0, int(db.get_setting("mfa_grace_days", "7")))
    except (TypeError, ValueError):
        return 7


def policy_started_at():
    return (db.get_setting("mfa_policy_started_at", "") or "").strip()


def policy_state(user):
    required = user["role"] in policy_roles()
    started = policy_started_at()
    grace_open = True
    if required and started:
        try:
            begin = datetime.strptime(started, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            deadline = begin.timestamp() + policy_grace_days() * 86400
            grace_open = time.time() < deadline
        except (TypeError, ValueError):
            grace_open = True
    return {"required": required, "grace_open": grace_open, "grace_days": policy_grace_days()}


def mfa_status_for(user):
    hashes = _hashes_from_user(user)
    policy = policy_state(user)
    return {
        "enabled": bool(user["mfa_enabled"]),
        "recovery_codes_left": len(hashes),
        "required_by_policy": policy["required"],
        "grace_open": policy["grace_open"],
        "pending_setup": bool(user["mfa_pending_secret"]) and not user["mfa_enabled"],
    }


def _hashes_from_user(user):
    try:
        hashes = json.loads(user["mfa_recovery_hashes"] or "[]")
        return [str(item) for item in hashes]
    except (TypeError, ValueError):
        return []


def _update_user(user_id, **fields):
    assignments, values = [], []
    for key, value in fields.items():
        assignments.append(f"{key}=?")
        values.append(value)
    if not assignments:
        return
    assignments.append("updated_at=?")
    values.append(datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    values.append(user_id)
    with db._lock:
        conn = db.get_conn()
        conn.execute(f"UPDATE users SET {', '.join(assignments)} WHERE id = ?", values)
        conn.commit()


def start_setup(user_id):
    user = db.find_user_by_id(user_id)
    if user is None:
        return None
    if user["mfa_enabled"]:
        return {"enabled": True}
    secret = new_secret()
    _update_user(user_id, mfa_pending_secret=db.encrypt_secret(secret))
    return {"enabled": False, "secret": secret}


def enable_after_verify(user_id, code):
    user = db.find_user_by_id(user_id)
    if user is None:
        return None
    if not user["mfa_pending_secret"]:
        return {"error": "Tidak ada pendaftaran MFA yang sedang menunggu."}
    try:
        secret = db.decrypt_secret(user["mfa_pending_secret"])
    except Exception:
        return {"error": "Secret MFA tidak dapat dibaca. Ulangi pendaftaran."}
    if verify_totp(secret, code, window=1, last_step=0) is None:
        return {"error": "Kode salah atau kedaluwarsa. Coba lagi."}
    codes = new_recovery_codes()
    hashes = [hash_recovery_code(code_item) for code_item in codes]
    _update_user(
        user_id,
        mfa_pending_secret=None,
        mfa_secret=db.encrypt_secret(secret),
        mfa_enabled=1,
        mfa_recovery_hashes=json.dumps(hashes),
        # Jangan konsumsi langkah verifikasi pendaftaran: kode itu boleh
        # dipakai sekali lagi pada jendela langkahnya untuk login pertama.
        mfa_last_step=0,
    )
    return {"enabled": True, "recovery_codes": codes}


def verify_for_login(user, code):
    """Verifikasi kode TOTP (anti-replay) atau kode pemulihan. Return (ok, kind)."""
    hashes = _hashes_from_user(user)
    code_digits = "".join(ch for ch in str(code or "") if ch.isdigit())
    if len(code_digits) == _DIGITS:
        try:
            secret = db.decrypt_secret(user["mfa_secret"])
        except Exception:
            return False, None
        step = verify_totp(secret, code_digits, window=1, last_step=user["mfa_last_step"])
        if step is not None:
            _update_user(user["id"], mfa_last_step=step)
            return True, "totp"
        return False, None
    ok, remaining = consume_recovery_code(hashes, code)
    if ok:
        _update_user(user["id"], mfa_recovery_hashes=json.dumps(remaining))
        return True, "recovery"
    return False, None


def disable(user, password, code, check_password):
    """Nonaktifkan MFA (butuh password + kode). Return (ok, error)."""
    if not user["mfa_enabled"]:
        return False, "MFA belum aktif."
    if not check_password(password):
        return False, "Kata sandi salah."
    ok, kind = verify_for_login(user, code)
    if not ok:
        return False, "Kode MFA salah."
    _update_user(
        user["id"],
        mfa_secret=None,
        mfa_pending_secret=None,
        mfa_enabled=0,
        mfa_recovery_hashes=json.dumps([]),
        mfa_last_step=0,
    )
    return True, None


def admin_reset(target_user_id):
    _update_user(
        target_user_id,
        mfa_secret=None,
        mfa_pending_secret=None,
        mfa_enabled=0,
        mfa_recovery_hashes=json.dumps([]),
        mfa_last_step=0,
    )
