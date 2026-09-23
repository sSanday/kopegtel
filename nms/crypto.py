"""nms.crypto — enkripsi kredensial SSH (ssh_pass) saat rest.

Skema: Fernet (AES-128-CBC + HMAC) dari paket `cryptography` — sudah
terpasang sebagai dependensi paramiko, jadi tanpa dep baru. Nilai
tersimpan berprefix ``enc$``; baris lama tanpa prefix dianggap plaintext
legacy dan tetap bisa dipakai (fallback migrasi — password baru selalu
tersimpan terenkripsi).

Kunci: env NMS_CRED_KEY (Fernet key b64) bila diset, jika tidak diturunkan
dari SECRET_KEY. Rotasi SECRET_KEY/NMS_CRED_KEY membuat simpanan lama tak
terbaca (fail-closed -> decrypt mengembalikan string kosong + WARN di log,
tanpa membocorkan materi rahasia apa pun).
"""

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from nms.config import SECRET_KEY

PREFIX = "enc$"


def _fernet():
    import os

    raw = (os.environ.get("NMS_CRED_KEY") or "").strip()
    if raw:
        return Fernet(raw.encode())
    digest = hashlib.sha256(f"nms-ssh-v1|{SECRET_KEY}".encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_secret(plaintext):
    """Enkripsi password -> 'enc$...' . Nilai kosong tetap kosong."""
    plaintext = plaintext or ""
    if not plaintext:
        return ""
    return PREFIX + _fernet().encrypt(plaintext.encode()).decode()


def decrypt_secret(stored):
    """Kebalikan encrypt_secret; legacy tanpa prefix lolos apa adanya.

    Token rusak/kunci salah -> "" (fail-closed) + WARN tanpa isi rahasia.
    """
    if not stored:
        return ""
    if not stored.startswith(PREFIX):
        return stored
    try:
        return _fernet().decrypt(stored[len(PREFIX) :].encode()).decode()
    except (InvalidToken, ValueError, Exception) as e:
        print(f"[CRYPTO] ssh_pass tak bisa didekripsi (kunci berubah?): {e}")
        return ""
