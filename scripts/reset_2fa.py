#!/usr/bin/env python
"""Reset 2FA (TOTP) seorang user lewat CLI — darurat kehilangan perangkat.

Pemakaian (dari direktori proyek, sehingga .env terbaca):

    python scripts/reset_2fa.py <username> [--yes]

Contoh:

    python scripts/reset_2fa.py admin --yes

Catatan:
- TOTP state pengguna utama ada di tabel users (totp_secret terenkripsi).
  Reset hanya MENGKOSONGKAN kolom 2FA — tidak perlu dekripsi dan tidak
  menyentuh password.
- Untuk akun sendiri dengan perangkat masuh ada, gunakan web:
  /api/2fa/disable (butuh password + kode). Skrip ini untuk kasus admin
  kehilangan seluruh perangkat / recovery code.
"""

import argparse
import sys

from nms.db import clear_user_2fa, get_user, get_user_2fa


def main():
    parser = argparse.ArgumentParser(description="Reset 2FA (TOTP) seorang user.")
    parser.add_argument("username", help="Username yang 2FA-nya direset")
    parser.add_argument("--yes", action="store_true", help="Lewati konfirmasi")
    args = parser.parse_args()

    user = get_user(args.username)
    if user is None:
        print(f"[X] User '{args.username}' tidak ditemukan di tabel users.")
        return 1

    state = get_user_2fa(args.username) or {}
    enabled = int(state.get("totp_enabled") or 0) == 1
    if not enabled:
        print(
            f"[i] 2FA user '{args.username}' tidak sedang aktif — tidak ada yang perlu direset."
        )
        return 0

    if not args.yes:
        jawab = (
            input(f"Reset 2FA (TOTP) untuk '{args.username}'? [y/N]: ").strip().lower()
        )
        if jawab not in ("y", "yes"):
            print("Dibatalkan.")
            return 0

    if not clear_user_2fa(args.username):
        print("[X] Gagal mereset 2FA (kesalahan database).")
        return 1
    print(f"[OK] 2FA '{args.username}' direset. User bisa login lagi dengan password,")
    print("     lalu lakukan setup 2FA baru dari UI (menu Autentikasi Dua Faktor).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
