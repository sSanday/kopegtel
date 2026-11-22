"""Tahap M6 — backup konfigurasi RouterOS via SSH (opsional, default mati).

Aturan:
- Kredensial SSH terpisah dari kredensial baca; didekripsi sekali pakai.
- Verifikasi host key SSH yang ketat: host key yang tidak dikenal/cocok DITOLAK.
  Tidak ada auto-accept.
- Tidak ada restore dari aplikasi. Hanya `/export hide-sensitive` (baca).
"""
import base64
import hashlib

DEFAULT_TIMEOUT = 12
EXPORT_TIMEOUT = 60


class BackupError(Exception):
    """Kesalahan backup; ``code`` dipakai untuk memetakan status HTTP."""

    def __init__(self, message, code="backup_failed"):
        super().__init__(message)
        self.code = code


def _fingerprint_blob(blob):
    digest = hashlib.sha256(blob).digest()
    return "SHA256:" + base64.b64encode(digest).rstrip(b"=").decode("ascii")


def parse_expected_fingerprint(host_key):
    """Normalisasi host key dari pengaturan menjadi fingerprint SHA256.

    Dua bentuk yang diterima (keduanya bukan mode auto-accept):
      1. ``SHA256:<base64>`` — fingerprint seperti keluaran ``ssh-keyscan -l``.
      2. Baris public key lengkap, mis. ``ssh-ed25519 AAAA... comment``.

    Bentuk lain ditolak. Raise BackupError(code='ssh_host_key_invalid').
    """
    text = (host_key or "").strip()
    if not text:
        raise BackupError(
            "Host key SSH belum disimpan. Rekam fingerprint perangkat (ssh-keyscan -l) atau tempel baris public key-nya.",
            code="ssh_host_key_missing",
        )
    if text.startswith("SHA256:"):
        body = text[len("SHA256:"):].strip()
        if len(body) < 20:
            raise BackupError("Fingerprint SHA256 tidak valid.", code="ssh_host_key_invalid")
        return f"SHA256:{body}"
    parts = text.split()
    if len(parts) >= 2 and parts[0].startswith(("ssh-", "ecdsa-")):
        try:
            blob = base64.b64decode(parts[1], validate=True)
        except Exception as exc:
            raise BackupError(f"Public key tidak bisa didekode: {exc}", code="ssh_host_key_invalid") from exc
        if len(blob) < 32:
            raise BackupError("Blob public key terlalu kecil.", code="ssh_host_key_invalid")
        return _fingerprint_blob(blob)
    raise BackupError(
        "Format host key tidak dikenal. Gunakan \"SHA256:<fingerprint>\" atau baris public key lengkap.",
        code="ssh_host_key_invalid",
    )


def run_export(*, host, port, user, password, host_key,
               connect_timeout=DEFAULT_TIMEOUT, export_timeout=EXPORT_TIMEOUT):
    """Jalankan `/export hide-sensitive` lewat SSH. Return teks ekspor.

    Host key diverifikasi SEBELUM kredensial dikirim; ketidakcocokan = BackupError.
    """
    try:
        import paramiko
    except ImportError as exc:
        raise BackupError(
            "Paket paramiko belum terpasang di server.", code="ssh_unavailable"
        ) from exc

    expected = parse_expected_fingerprint(host_key)
    server = (str(host or "").strip(), int(port or 22))
    if not server[0]:
        raise BackupError("IP/host perangkat kosong.", code="ssh_host_missing")
    username = (user or "").strip()
    if not username or not password:
        raise BackupError("Kredensial SSH belum lengkap.", code="ssh_no_credentials")

    transport = None
    try:
        transport = paramiko.Transport(server)
        transport.banner_timeout = connect_timeout
        transport.auth_timeout = connect_timeout
        try:
            transport.start_client(timeout=connect_timeout)
        except Exception as exc:
            raise BackupError(f"SSH tidak terhubung: {exc}", code="unreachable") from exc
        server_key = transport.get_remote_server_key()
        presented = _fingerprint_blob(server_key.asbytes()) if server_key is not None else None
        if presented != expected:
            raise BackupError(
                "Host key SSH tidak cocok dengan yang tersimpan. Periksa kembali perangkat atau rekam host key-nya ulang.",
                code="host_key_untrusted",
            )
        try:
            transport.auth_password(username, password)
        except paramiko.AuthenticationException as exc:
            raise BackupError("Autentikasi SSH gagal.", code="auth_failed") from exc
        except Exception as exc:
            raise BackupError(f"SSH gagal membuka sesi: {exc}", code="auth_failed") from exc

        channel = transport.open_session(timeout=export_timeout)
        channel.settimeout(export_timeout)
        channel.exec_command("/export hide-sensitive")
        out_chunks = []
        while True:
            chunk = channel.recv(65536)
            if not chunk:
                break
            out_chunks.append(chunk)
        try:
            err_bytes = channel.recv_stderr(65536)
            while channel.recv_stderr_ready():
                err_bytes += channel.recv_stderr(65536)
        except Exception:
            err_bytes = b""
        channel.close()
        out_bytes = b"".join(out_chunks)
        text = out_bytes.decode("utf-8", "replace")
        if not text.strip():
            detail = err_bytes.decode("utf-8", "replace").strip()
            raise BackupError(
                "Ekspor kosong" + (f": {detail[:200]}" if detail else "."),
                code="empty_export",
            )
        return text
    except BackupError:
        raise
    except Exception as exc:
        raise BackupError(str(exc), code="backup_failed") from exc
    finally:
        if transport is not None:
            try:
                transport.close()
            except Exception:
                pass
