"""Verifikasi TLS untuk RouterOS API-SSL (M-11).

RouterOS umumnya memakai sertifikat self-signed, sehingga verifikasi CA biasa
gagal. Jalan yang aman adalah *certificate pinning*: admin menyimpan fingerprint
SHA-256 sertifikat per perangkat, lalu setiap koneksi TLS membandingkan
sertifikat yang diterima dengan fingerprint tersebut sebelum kredensial dikirim.

Dua mode:
- Fingerprint diisi  -> handshake TLS tanpa validasi CA, tetapi sertifikat peer
  WAJIB cocok dengan fingerprint (dibandingkan constant-time). Mode ini tetap
  aman terhadap MITM karena penyerang tidak bisa memalsukan sertifikat dengan
  fingerprint yang sama.
- Fingerprint kosong -> verifikasi CA standar (CERT_REQUIRED + hostname).
  Sertifikat self-signed akan ditolak; admin harus mengisi fingerprint.
"""

import hashlib
import hmac
import re
import ssl

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_SEPARATORS = re.compile(r"[\s:\-]")
_SHA256_PREFIX = re.compile(r"^\s*sha-?256\s*:?\s*", re.IGNORECASE)


class TlsVerificationError(ssl.SSLError):
    """Kegagalan verifikasi TLS yang aman untuk ditampilkan ke pengguna."""

    def __init__(self, message, code="tls_error"):
        super().__init__(message)
        self.code = code


def normalize_fingerprint(value):
    """Normalisasi fingerprint SHA-256 ke 64 digit hex huruf kecil.

    Menerima format ``AA:BB:...``, spasi, tanda hubung, atau awalan
    ``sha256:``. Mengembalikan ``None`` untuk nilai kosong. Melempar
    ``ValueError`` bila bentuknya tidak valid.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    text = _SHA256_PREFIX.sub("", text)
    text = _SEPARATORS.sub("", text).lower()
    if not _HEX64.fullmatch(text):
        raise ValueError(
            "Fingerprint sertifikat harus 64 digit heksadesimal SHA-256 "
            "(boleh dipisah titik dua)."
        )
    return text


def format_fingerprint(value):
    """Tampilkan fingerprint sebagai pasangan hex huruf besar dipisah titik dua."""
    normalized = normalize_fingerprint(value)
    if normalized is None:
        return None
    return ":".join(normalized[i:i + 2] for i in range(0, 64, 2)).upper()


def _fingerprint_of_socket(tls_socket):
    certificate = tls_socket.getpeercert(binary_form=True)
    if not certificate:
        raise TlsVerificationError(
            "RouterOS tidak mengirimkan sertifikat TLS.", "tls_error"
        )
    return hashlib.sha256(certificate).hexdigest()


def ssl_wrapper(ip, fingerprint=None):
    """Buat callable ``ssl_wrapper`` untuk librouteros.

    ``fingerprint`` kosong -> verifikasi CA standar. Bila diisi, sertifikat peer
    wajib cocok dengan fingerprint; koneksi ditutup dan exception dilempar
    sebelum kredensial RouterOS dikirim.
    """
    expected = normalize_fingerprint(fingerprint)

    if expected is None:
        context = ssl.create_default_context()
        # Hostname diverifikasi terhadap IP perangkat; sertifikat dengan CN/SAN
        # yang tidak cocok akan ditolak (gunakan pinning bila perlu).
        return lambda sock: context.wrap_socket(sock, server_hostname=ip)

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    context.minimum_version = ssl.TLSVersion.TLSv1_2

    def wrap(sock):
        tls_socket = context.wrap_socket(sock, server_hostname=ip)
        try:
            actual = _fingerprint_of_socket(tls_socket)
            if not hmac.compare_digest(actual, expected):
                raise TlsVerificationError(
                    "Fingerprint sertifikat RouterOS tidak cocok "
                    f"(diharapkan {format_fingerprint(expected)}, "
                    f"diterima {format_fingerprint(actual)}). "
                    "Periksa apakah sertifikat perangkat berubah; bila ya, "
                    "perbarui fingerprint di pengaturan perangkat.",
                    "tls_fingerprint_mismatch",
                )
        except Exception:
            try:
                tls_socket.close()
            except OSError:
                pass
            raise
        return tls_socket

    return wrap
