"""Utilitas CLI NMS Kopegtel.

Pemakaian:
    python backend/manage.py create-admin [--username NAMANA]
    python backend/manage.py reset-password <username>
    python backend/manage.py list-users
    python backend/manage.py check-config
    python backend/manage.py backup-db [--out DIR] [--keep N]
    python backend/manage.py restore-db <file.db>
    python backend/manage.py purge-scan-secrets [--include-backups] [file.db ...]
    python backend/manage.py check-keys
    python backend/manage.py rotate-encryption-key [--dry-run]
    python backend/manage.py rotate-secret-key
"""

import argparse
import getpass
import glob
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, "backend"))

load_dotenv(os.path.join(BASE_DIR, ".env"))

import config  # noqa: E402
import db  # noqa: E402
import dbbackup  # noqa: E402

from werkzeug.security import generate_password_hash  # noqa: E402

MIN_LEN = 10


def read_password(prompt):
    while True:
        first = getpass.getpass(prompt)
        if len(first) < MIN_LEN:
            print(f"Kata sandi minimal {MIN_LEN} karakter. Ulangi.")
            continue
        second = getpass.getpass("Ulangi kata sandi: ")
        if first != second:
            print("Kata sandi tidak sama. Ulangi.")
            continue
        return first


def cmd_create_admin(args):
    db.init_db()
    username = (args.username or "admin").strip().lower()
    if db.user_exists(username):
        print(f"Pengguna '{username}' sudah ada. Gunakan reset-password bila ingin mengganti kata sandinya.")
        return 1
    password = read_password(f"Kata sandi untuk admin '{username}' (min {MIN_LEN} karakter): ")
    db.create_user(username, generate_password_hash(password), role="admin", must_change_password=0)
    print(f"Akun admin '{username}' dibuat.")
    return 0


def cmd_reset_password(args):
    db.init_db()
    username = args.username.strip().lower()
    user = db.find_user(username)
    if user is None:
        print(f"Pengguna '{username}' tidak ditemukan.")
        return 1
    password = read_password(f"Kata sandi baru untuk '{username}' (min {MIN_LEN} karakter): ")
    db.update_user(user["id"], password_hash=generate_password_hash(password), must_change_password=1)
    print(f"Kata sandi '{username}' direset. Pengguna wajib menggantinya saat login berikutnya.")
    return 0


def cmd_list_users(_args):
    db.init_db()
    users = db.list_users()
    if not users:
        print("Belum ada pengguna.")
        return 0
    for user in users:
        flag = " [wajib ganti password]" if user["must_change_password"] else ""
        print(f"  {user['id']:>3}  {user['username']:<30} {user['role']}{flag}")
    return 0


def cmd_check_config(_args):
    errors = config.config_errors()
    if errors:
        for error in errors:
            print(f"ERROR konfigurasi: {error}")
        print("\nKonfigurasi TIDAK valid. Perbaiki .env lalu jalankan ulang.")
        return 1
    print("Konfigurasi valid. SECRET_KEY >= 32 karakter, ENCRYPTION_KEY kunci Fernet, ADMIN_PASSWORD memenuhi kebijakan.")
    return 0


# ---------------------------------------------------------
# T3 — backup / restore / rotasi kunci
# ---------------------------------------------------------

REQUIRED_TABLES = ("devices", "users", "problems", "settings", "audit_log")


def cmd_backup_db(args):
    db.init_db()
    try:
        target = dbbackup.run_backup(args.out, args.keep)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    size = os.path.getsize(target)
    print(f"Backup tersimpan: {target}")
    print(f"Ukuran: {size} byte (integrity_check: ok)")
    return 0


def _safety_copy(tag="safety"):
    """Salinan online database saat ini (backup API; aman saat WAL belum di-checkpoint)."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    _enabled, directory, _keep = dbbackup.backup_settings()
    os.makedirs(directory, exist_ok=True)
    target = os.path.join(directory, f"{tag}-nms-{stamp}.db")
    src = sqlite3.connect(db.DB_PATH)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    ok, message = _verify_db_file(target)
    if not ok:
        try:
            os.remove(target)
        except OSError:
            pass
        raise RuntimeError(f"Salinan pengaman gagal diverifikasi: {message}")
    return target


def _verify_db_file(path):
    """integrity_check + keberadaan tabel inti. Return (ok, pesan)."""
    try:
        conn = sqlite3.connect(path)
        try:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                return False, f"integrity_check gagal: {integrity}"
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
            missing = [table for table in REQUIRED_TABLES if table not in tables]
            if missing:
                return False, f"tabel inti tidak ada: {', '.join(missing)}"
            return True, "ok"
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return False, f"bukan database SQLite yang valid: {exc}"


def cmd_restore_db(args):
    candidate = os.path.abspath(args.file)
    if not os.path.isfile(candidate):
        print(f"ERROR: berkas tidak ditemukan: {candidate}", file=sys.stderr)
        return 1

    status = db.instance_lease_status()
    if status is not None and db._lease_fresh(status["heartbeat"]):
        print(
            f"ERROR: layanan NMS masih berjalan (PID {status['pid']}, heartbeat {status['heartbeat']}). "
            "Hentikan dulu proses backend (serve.py) sebelum restore.",
            file=sys.stderr,
        )
        return 1

    ok, message = _verify_db_file(candidate)
    if not ok:
        print(f"ERROR: berkas backup tidak valid — {message}", file=sys.stderr)
        return 1

    try:
        safety = _safety_copy("safety")
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"Salinan pengaman database saat ini: {safety}")

    tmp_path = db.DB_PATH + ".restore-tmp"
    shutil.copy2(candidate, tmp_path)
    with db._lock:
        try:
            db.get_conn().close()
        except Exception:
            pass
        db._conn = None
        os.replace(tmp_path, db.DB_PATH)
        for suffix in ("-wal", "-shm", "-journal"):
            side = db.DB_PATH + suffix
            if os.path.exists(side):
                os.remove(side)

    ok, message = _verify_db_file(db.DB_PATH)
    if not ok:
        print(f"ERROR: database hasil restore GAGAL diverifikasi ({message}). Salinan pengaman ada di {safety}", file=sys.stderr)
        return 1
    print("Restore selesai dan terverifikasi (integrity_check: ok).")
    print("Catatan: ENCRYPTION_KEY tidak ikut ter-backup — pastikan kunci tersimpan terpisah (mis. password manager).")
    print("Mulai lagi layanan: python backend/serve.py")
    return 0


def cmd_purge_scan_secrets(args):
    """Hapus kolom kredensial scan lama dari DB aktif dan (opsional) backup.

    Database aktif juga dimigrasikan otomatis oleh init_db(); perintah ini
    berguna untuk membersihkan berkas backup historis yang tidak dibuka app.
    """
    db.init_db()
    targets = [db.DB_PATH]
    if args.include_backups:
        _enabled, out_dir, _keep = dbbackup.backup_settings()
        targets.extend(sorted(glob.glob(os.path.join(out_dir, "*.db"))))
    targets.extend(args.files or [])

    seen = set()
    failed = False
    for raw_path in targets:
        path = os.path.abspath(raw_path)
        if path in seen:
            continue
        seen.add(path)
        if not os.path.isfile(path):
            print(f"LEWATI (tidak ada): {path}", file=sys.stderr)
            failed = True
            continue
        try:
            changed = db.scrub_scan_credentials_file(path)
        except Exception as exc:  # noqa: BLE001 - laporkan berkas yang gagal, lanjutkan
            print(f"ERROR {path}: {exc}", file=sys.stderr)
            failed = True
            continue
        if changed:
            print(f"DIBERSIHKAN: {path} (kolom kredensial scan dihapus, VACUUM + checkpoint selesai)")
        else:
            print(f"AMAN: {path} (tidak ada kolom kredensial scan)")

    if failed:
        return 1
    print("Selesai. Tidak ada nilai kredensial yang dicetak.")
    return 0


def cmd_check_keys(_args):
    db.init_db()
    stats = db.encrypted_value_stats()
    total = sum(item["total"] for item in stats)
    failed = sum(item["failed"] for item in stats)
    print(f"{'Kolom':<42} {'Total':>6} {'OK':>6} {'Gagal':>6}")
    for item in stats:
        print(f"{item['table'] + '.' + item['column']:<42} {item['total']:>6} {item['ok']:>6} {item['failed']:>6}")
    if failed:
        print(f"\n{failed} nilai GAGAL didekripsi dengan ENCRYPTION_KEY saat ini (dari {total}).")
        print("Jangan rotasi kunci; periksa ENCRYPTION_KEY di .env.")
        return 1
    print(f"\nSemua {total} nilai terenkripsi dapat didekripsi dengan ENCRYPTION_KEY saat ini.")
    return 0


def cmd_rotate_encryption_key(args):
    db.init_db()
    from cryptography.fernet import Fernet

    # Cadangan otomatis dulu (wajib sebelum operasi destruktif).
    backup_path = dbbackup.run_backup(None, None, actor="system", ip="127.0.0.1")
    print(f"Backup otomatis dibuat: {backup_path}")

    new_key = Fernet.generate_key()
    stats = db.encrypted_value_stats()
    total = sum(item["total"] for item in stats)
    already_broken = sum(item["failed"] for item in stats)
    if already_broken:
        print(f"ERROR: {already_broken} nilai sudah tidak dapat didekripsi dengan kunci saat ini. Perbaiki dulu dengan check-keys.", file=sys.stderr)
        return 1

    if args.dry_run:
        print(f"[DRY-RUN] Akan mengenkripsi ulang {total} nilai dalam satu transaksi. Tidak ada perubahan disimpan.")
        return 0

    try:
        written, failed = db.reencrypt_all(new_key)
    except Exception as exc:
        print(f"ERROR: rotasi gagal dan dibatalkan seluruhnya: {exc}", file=sys.stderr)
        print(f"Database tidak berubah. Cadangan: {backup_path}", file=sys.stderr)
        return 1
    if failed:
        print(f"ERROR: rotasi dibatalkan seluruhnya; {len(failed)} nilai gagal didekripsi:", file=sys.stderr)
        for item in failed:
            print(f"  - {item}", file=sys.stderr)
        print(f"Database tidak berubah. Cadangan: {backup_path}", file=sys.stderr)
        return 1

    # Verifikasi memakai kunci BARU secara lokal (env belum berubah).
    from cryptography.fernet import Fernet as _Fernet

    new_fernet = _Fernet(new_key)
    for table, cols in db.ENCRYPTED_FIELDS:
        for col in cols:
            rows = db.get_conn().execute(
                f"SELECT {col} FROM {table} WHERE {col} IS NOT NULL AND {col} != ''"
            ).fetchall()
            for row in rows:
                new_fernet.decrypt(row[col].encode("utf-8"))

    print(f"\nBERHASIL mengenkripsi ulang {written} nilai dengan kunci baru.")
    print("\nKUNCI ENCRYPTION_KEY BARU (tampil sekali — simpan sekarang, mis. di password manager):")
    print(f"\n    {new_key.decode('utf-8')}\n")
    print("Langkah selanjutnya:")
    print("  1. Buka .env, ganti nilai ENCRYPTION_KEY dengan kunci di atas.")
    print("  2. Simpan kunci LAMA dan kunci BARU di tempat aman (password manager).")
    print("  3. Mulai ulang layanan: python backend/serve.py")
    print("  4. Verifikasi: python backend/manage.py check-keys")
    print(f"Cadangan sebelum rotasi: {backup_path}")
    return 0


def cmd_rotate_secret_key(_args):
    import secrets

    new_key = secrets.token_hex(32)
    print("SECRET_KEY baru (tampil sekali — simpan sekarang, mis. di password manager):")
    print(f"\n    {new_key}\n")
    print("Konsekuensi: semua sesi login yang ada menjadi TIDAK BERLAKU (pengguna harus login ulang).")
    print("Langkah selanjutnya:")
    print("  1. Buka .env, ganti nilai SECRET_KEY dengan kunci di atas.")
    print("  2. Mulai ulang layanan: python backend/serve.py")
    print("  3. Semua pengguna login kembali dengan kata sandi masing-masing.")
    print("(Database tidak diubah; SECRET_KEY hanya menandatangani cookie sesi.)")
    return 0


def main():
    parser = argparse.ArgumentParser(description="Utilitas NMS Kopegtel")
    sub = parser.add_subparsers(dest="command", required=True)

    p_create = sub.add_parser("create-admin", help="Buat akun admin baru")
    p_create.add_argument("--username", default="admin")
    p_create.set_defaults(func=cmd_create_admin)

    p_reset = sub.add_parser("reset-password", help="Reset kata sandi pengguna")
    p_reset.add_argument("username")
    p_reset.set_defaults(func=cmd_reset_password)

    p_list = sub.add_parser("list-users", help="Daftar pengguna")
    p_list.set_defaults(func=cmd_list_users)

    p_check = sub.add_parser("check-config", help="Periksa konfigurasi (SECRET_KEY/ENCRYPTION_KEY/ADMIN_PASSWORD)")
    p_check.set_defaults(func=cmd_check_config)

    p_backup = sub.add_parser("backup-db", help="Backup online basis data (SQLite backup API)")
    p_backup.add_argument("--out", default=None, help="Folder tujuan (default: backups/ atau db_backup_dir)")
    p_backup.add_argument("--keep", type=int, default=None, help="Jumlah backup yang disimpan (default: 14 atau db_backup_keep)")
    p_backup.set_defaults(func=cmd_backup_db)

    p_restore = sub.add_parser("restore-db", help="Restore basis data dari berkas backup (layanan harus berhenti)")
    p_restore.add_argument("file", help="Berkas .db hasil backup-db")
    p_restore.set_defaults(func=cmd_restore_db)

    p_purge = sub.add_parser(
        "purge-scan-secrets",
        help="Hapus kolom kredensial scan lama dari DB aktif dan backup (VACUUM + checkpoint)",
    )
    p_purge.add_argument(
        "--include-backups",
        action="store_true",
        help="Ikut bersihkan semua berkas *.db di folder backup",
    )
    p_purge.add_argument("files", nargs="*", help="Berkas DB tambahan (opsional)")
    p_purge.set_defaults(func=cmd_purge_scan_secrets)

    p_keys = sub.add_parser("check-keys", help="Periksa semua nilai terenkripsi dapat didekripsi dengan ENCRYPTION_KEY saat ini")
    p_keys.set_defaults(func=cmd_check_keys)

    p_rotenc = sub.add_parser("rotate-encryption-key", help="Enkripsi ulang SEMUA nilai dengan ENCRYPTION_KEY baru")
    p_rotenc.add_argument("--dry-run", action="store_true", help="Hitung saja; tidak ada yang diubah")
    p_rotenc.set_defaults(func=cmd_rotate_encryption_key)

    p_rotsec = sub.add_parser("rotate-secret-key", help="Buat SECRET_KEY baru (semua sesi menjadi tidak berlaku)")
    p_rotsec.set_defaults(func=cmd_rotate_secret_key)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
