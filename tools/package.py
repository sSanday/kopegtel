"""Membuat dist/nms-kopegtel-<versi>.zip untuk dibagikan/instalasi.

Mengecualikan: .env, .env.* (kecuali .env.example), .venv, *.bak*, *.db*,
*.log, .pytest_cache, __pycache__, .freebuff, .git, dist, backups/, logs/,
tools/dev/. Setelah arsip dibuat, isi diverifikasi: ada berkas terlarang
atau pola secret (nilai SECRET_KEY/ENCRYPTION_KEY, blok private key) → gagal.

Penggunaan:
    python tools/package.py [--version 1.0.0]
"""
import argparse
import fnmatch
import os
import re
import sys
import zipfile

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIST_DIR = os.path.join(BASE_DIR, "dist")

# Kecocokan terhadap path relatif (dengan garis miring /).
FORBIDDEN_PATTERNS = [
    ".env",
    ".env.*",          # .env.local, .env.production, ...
    ".venv/*",
    "*/.venv/*",
    "*.bak*",
    "*.db",
    "*.db-wal",
    "*.db-shm",
    "*.db-journal",
    "*.log",
    ".pytest_cache/*",
    "*/.pytest_cache/*",
    "__pycache__/*",
    "*/__pycache__/*",
    "*.pyc",
    ".freebuff/*",
    "*/.freebuff/*",
    "backups/*",
    "logs/*",
    "tools/dev/*",
    ".git/*",
    ".git",
    "dist/*",
]
ALLOWED_PATTERNS = [
    ".env.example",  # contoh konfigurasi tetap disertakan
]
PRIVATE_KEY_RE = re.compile(rb"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----")


def rel_posix(path):
    return os.path.relpath(path, BASE_DIR).replace(os.sep, "/")


def is_forbidden(rel):
    for allowed in ALLOWED_PATTERNS:
        if fnmatch.fnmatch(rel, allowed):
            return False
    for pattern in FORBIDDEN_PATTERNS:
        if fnmatch.fnmatch(rel, pattern):
            return True
        # Pola tanpa garis miring juga diuji terhadap nama berkas.
        if "/" not in pattern and fnmatch.fnmatch(os.path.basename(rel), pattern):
            return True
    return False


def iter_files():
    for root, dirs, files in os.walk(BASE_DIR):
        dirs[:] = [d for d in dirs if d not in (".git", ".venv", "dist", "__pycache__", ".pytest_cache", ".freebuff")]
        for name in files:
            full = os.path.join(root, name)
            rel = rel_posix(full)
            if is_forbidden(rel):
                continue
            yield full, rel


def load_secret_values():
    """Ambil nilai SECRET_KEY/ENCRYPTION_KEY dari lingkungan atau .env (tanpa dicetak)."""
    values = []
    for key in ("SECRET_KEY", "ENCRYPTION_KEY"):
        value = os.environ.get(key, "").strip()
        if value:
            values.append(value)
    env_file = os.path.join(BASE_DIR, ".env")
    if os.path.isfile(env_file):
        try:
            from dotenv import dotenv_values

            data = dotenv_values(env_file)
        except Exception:
            data = {}
        for key in ("SECRET_KEY", "ENCRYPTION_KEY"):
            value = str(data.get(key) or "").strip()
            if value:
                values.append(value)
    return list(dict.fromkeys(values))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", default=os.environ.get("APP_VERSION", "1.0.0"))
    args = parser.parse_args()

    version = args.version.strip()
    if not re.fullmatch(r"[0-9]+(\.[0-9]+)*([a-zA-Z0-9._-]*)", version):
        print(f"Versi tidak valid: {version!r}", file=sys.stderr)
        return 2

    os.makedirs(DIST_DIR, exist_ok=True)
    archive_path = os.path.join(DIST_DIR, f"nms-kopegtel-{version}.zip")
    files = list(iter_files())
    if not files:
        print("Tidak ada berkas yang layak dikemas.", file=sys.stderr)
        return 2

    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for full, rel in files:
            zf.write(full, rel)

    # ---------- Verifikasi isi arsip ----------
    errors = []
    with zipfile.ZipFile(archive_path, "r") as zf:
        names = zf.namelist()
        for name in names:
            if is_forbidden(name):
                errors.append(f"berkas terlarang di arsip: {name}")
        secrets = load_secret_values()
        for name in names:
            data = zf.read(name)
            for secret in secrets:
                if secret.encode("utf-8") in data:
                    errors.append(f"nilai secret ditemukan di arsip (nama berkas tidak dicetak)")
                    break
            if PRIVATE_KEY_RE.search(data):
                errors.append(f"blok private key ditemukan di arsip: {name}")
    if errors:
        os.remove(archive_path)
        print("Arsip GAGAL dibuat karena temuan berikut:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1

    total = sum(os.path.getsize(full) for full, _ in files)
    print(f"Arsip dibuat: {archive_path}")
    print(f"Berkas: {len(files)}  Ukuran asli: {total} byte")
    print("Verifikasi secret/private key: bersih")
    print("Isi arsip:")
    for _, rel in sorted(files, key=lambda item: item[1]):
        print(f"  {rel}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
