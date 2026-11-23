# Design: Onboarding & Quick Wins (Approach 1 — Patchwork)

**Tanggal:** 2026-11-23
**Status:** disetujui · 2026-11-23
**Branch dasar:** `main` (HEAD `5d8cf95`)

## 1. Intent & Why

Pengguna baru (admin) membuka NMS Kopegtel dan menemukan tabel kosong di hampir semua halaman (inventaris, layanan, fiber, traffic, events, mikrotik). Mereka tidak tahu harus mulai dari mana, dan sesi login kadaluarsa (idle 30 menit) saat mengisi form panjang, sehingga redirect ke login tanpa penjelasan. Tujuan: **membuat pengguna baru bisa mencoba fitur dalam 2 menit pertama** tanpa kebingungan.

## 2. Goals

- **G1**: Saat pertama kali (tabel `devices` kosong), sistem otomatis membuat 1 perangkat contoh ("contoh-router", tipe router, IP 192.168.30.99) agar tabel tidak kosong dan pengguna bisa melihat contoh.
- **G2**: Idle timeout default dinaikkan dari 30 ke 120 menit (tetap configurable via admin).
- **G3**: Selama tab terbuka, sesi diperpanjang otomatis tiap 10 menit (panggil `/api/me`).
- **G4**: Setiap tabel kosong menampilkan empty state dengan tombol "Tambah" yang jelas dan contoh data.
- **G5**: Field wajib (nama, IP, interval, community, user/password RouterOS) punya tooltip/help.
- **G6**: Hasil "Uji koneksi" ditampilkan dengan warna hijau (berhasil) / merah (gagal) per metode.

## 3. Non-goals

- Tidak membuat wizard multi-langkah (Approach 2) — terlalu besar untuk quick win.
- Tidak mengubah skema database (kecuali seed data).
- Tidak menambah fitur monitoring baru.
- Tidak mengubah layout atau navigasi.

## 4. Anti-goals

- Jangan auto-seed data contoh jika tabel sudah berisi data (hanya seed sekali).
- Jangan auto-refresh token jika tab tidak terlihat (hemat resource).
- Jangan menambah tooltip yang mengganggu (hanya `title` + `aria-describedby`).

## 5. Constraints

- Vanilla JS (ES6+), tanpa framework.
- Backend Python + Flask, SQLite.
- Idle timeout diatur via `db.get_setting("idle_timeout_minutes")`.
- Auto-refresh token: panggil `/api/me` yang memperbarui `last_activity_at`.
- Seed data: jalankan di `app.bootstrap()` atau saat pertama kali `init_db()` selesai, hanya jika `devices` kosong.

## 6. Approaches (dipilih)

**Approach 1 — Patchwork cepat** (dipilih pemilik):
- Seed dummy di backend (1 tempat).
- Timeout di config (1 baris).
- Auto-refresh di `shell.js` atau `api.js` (1 tempat).
- Empty state di 6 halaman HTML (inventory, services, fiber, traffic, events, mikrotik) + dashboard (sudah ada).
- Tooltip via `title` + `aria-describedby` di field wajib (device-form.js, service form, fiber form).
- Warna hasil uji koneksi di device-form.js.

**Mengapa bukan Approach 2/3**: Approach 2 (modul terpusat) overkill untuk quick win; Approach 3 (bertahap) memperlambat delivery. Patchwork cepat memberi nilai langsung.

## 7. Bagian yang sudah diputuskan

- Seed device: nama "contoh-router", tipe "router", IP "192.168.30.99", interval 60s, tanpa SNMP/MikroTik. Hanya dibuat jika `COUNT(*) FROM devices = 0`.
- Idle timeout default: 30 → 120 menit (tetap bisa diubah admin).
- Auto-refresh: tiap 10 menit, hanya saat `document.visibilityState === 'visible'`.
- Empty state: pola yang sama dengan dashboard (judul + deskripsi + tombol "Tambah").
- Tooltip: atribut `title` + `aria-describedby` ke elemen bantuan kecil.
- Hasil uji koneksi: hijau untuk `ok: true`, merah untuk `ok: false`.

## 8. Yang diserahkan ke builder

- Implementasi seed data di backend.
- Perubahan config default idle timeout.
- Auto-refresh di frontend.
- Empty state HTML di 6 halaman.
- Tooltip di form perangkat (dan layanan/fiber jika relevan).
- Warna hasil uji koneksi.
- Test: seed data (1 test), auto-refresh (manual), empty state (visual).

## 9. Non-goals tambahan

- Tidak membuat wizard multi-langkah.
- Tidak mengubah skema DB (kecuali seed).
- Tidak menambah fitur monitoring baru.
- Tidak mengubah layout/navigasi.