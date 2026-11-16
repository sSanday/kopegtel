# Permintaan Purge ke GitHub Support (siap kirim)

Masalah: commit pra-rewrite **`6c8158a`** (sekitar 2026-11-04, pesan
"rapikan kode") di branch `cleanup/rapikan-kode` pernah memuat dua berkas:

- `nms.db` (~307 KB, SQLite: schema NMS, tidak ada kata sandi pengguna)
- `logs/sla_demo.db` (~336 KB, SQLite: memuat `settings.admin_pass_hash` —
  hash scrypt dari kata sandi admin lama)

Pada 2026-11-16 riwayat diganti (`git filter-branch` + `push --force-with-lease`)
dan kedua berkas sudah **tidak ada di commit mana pun** (URL branch → **404**).
Namun objek yang tidak terjangkau masih dilayani GitHub melalui raw cache:

- `https://raw.githubusercontent.com/sSanday/kopegtel/6c8158a/nms.db` → HTTP 200
- `https://raw.githubusercontent.com/sSanday/kopegtel/6c8158a/logs/sla_demo.db` → HTTP 200

Password admin sudah direset 2026-11-17 sehingga hash di berkas itu tidak
berlaku lagi, tetapi IP internal RFC1918 di dalamnya masih bersifat privat
dan hash lama tetap sebaiknya dihapus dari internet.

## Cara mengajukan

1. Buka: <https://support.github.com/contact/private-information>
2. Login sebagai pemilik akun/repo (Sanday).
3. Pilih kategori terkait **Removing cached views / sensitive data**.
4. Teks permintaan (sesuaikan akun/email):

---

Subject: Request to purge cached raw objects containing sensitive data

Hello GitHub Support,

I am the owner of the repository `sSanday/kopegtel`. On or around 2026-11-04,
commit `6c8158a` (short SHA; branch `cleanup/rapikan-kode`) accidentally
contained two SQLite database files with sensitive data:

- `nms.db`
- `logs/sla_demo.db` (contains a scrypt password hash of a former admin
  password, plus internal RFC1918 IP addresses)

I rewrote the branch history on 2026-11-16 (`git filter-branch` +
force-push), so the files no longer exist in any commit. However, GitHub's
raw cache still serves the unreachable objects:

- https://raw.githubusercontent.com/sSanday/kopegtel/6c8158a/nms.db (HTTP 200)
- https://raw.githubusercontent.com/sSanday/kopegtel/6c8158a/logs/sla_demo.db (HTTP 200)

I would like to request purging these cached objects / making the
unreachable SHA inaccessible. The full SHA is not available to me anymore
(the short SHA `6c8158a` is all that is recorded); please let me know if
you need more identifying information from my account.

Thank you.

---

## Alternatif cepat (bisa dilakukan pemilik kapan pun)

**Jadikan repo private**: Settings → General → Danger Zone →
"Change repository visibility" → Make private. Setelah itu raw URL di atas
merespons **404 untuk akses anonim** tanpa perlu menunggu Support. Bila repo
suatu saat dijadikan publik lagi, konsekuensi objek lama (jika belum
dipurge Support) terbuka kembali — ajukan permintaan di atas juga.

## Catatan

- Lokal: `git reflog expire --expire=now --all; git gc --prune=now` sudah
  dijalankan 2026-11-17; objek `6c8158a` tidak ada lagi di clone lokal.
- Dokumen temuan lengkap: `docs/TEMUAN.md` (M-13 & Pasca-eksposur).
