# PROGRES.md — E1–E4

Diperbarui: 2026-11-15

## Ringkasan tahap

| Tahap | Status | Keterangan |
|---|---|---|
| E1 — Syslog & SNMP trap | selesai | tes 120 lolos; commit `3feafb1` |
| E2 — Webhook, eskalasi, MFA | selesai | tes 162 lolos; commit `34e42fa` |
| E3 — Traffic Flow | selesai | tes 179 lolos; commit `94304c3`; produksi hidup |
| E4 — Metrik Prometheus | selesai | tes 190 lolos; commit E4; produksi hidup |

## Aturan kerja

- Kerjakan berurutan E1 → E4; jangan lompat tahap.
- Setiap tahap: semua tes lama + baru lolos, lalu commit.
- Jangan mengubah perilaku endpoint lama. Fitur baru memakai endpoint baru.
- Tidak ada data contoh di UI/runtime. Empty state menjelaskan cara menyiapkan.
- Data dari UDP tidak dipercaya: hanya IP perangkat terdaftar, batas laju,
  batas ukuran, antrean berbatas, batch insert, hitung yang dibuang.
- Port bawaan ≥ 1024 dan default MATI.
- Semua teks luar di-escape saat ditampilkan; viewer hanya ringkasan.
- Retensi mengikuti mekanisme `purge_old_data`.

## Status E2 — selesai (2026-11-11)

### A. Webhook (kanal jenis `webhook`)

- URL https (http hanya dengan izin eksplisit `allow_http`), POST, JSON.
- Header kustom (disimpan dalam config terenkripsi; Host/X-NMS-*/Content-Type diabaikan).
- Tanda tangan `X-NMS-Timestamp` + `X-NMS-Signature` = `sha256=<hex(HMAC-SHA256(secret, "<ts>.<body>"))>`.
- Anti-SSRF: resolusi DNS ditolak untuk loopback/link-local/privat/reserved kecuali
  `ALLOW_PRIVATE_WEBHOOKS=1` atau `allow_private` pada kanal (khusus admin; env
  dipromosikan menjadi izin kanal saat disimpan); proxy dinonaktifkan;
  redirect tidak diikuti; timeout 5 dtk; respons dibatasi (default 1 KB, maks 4 KB).
- Retry 3x dengan backoff 1s/2s; hasil (status/durasi/galat tanpa secret/header) ke `action_log`.
- Preset `generic`, `slack`, `discord`; body lewat `json.dumps`.
- "Uji kirim" benar-benar mengirim (sama jalurnya dengan kirim masalah).
- API: secret tidak pernah ditampilkan kembali (`has_secret`); edit tanpa secret
  mempertahankan yang tersimpan; audit `channel.create/update/test`.

### B. Eskalasi

- Tabel `escalation_policies`, `escalation_steps` (urutan, jeda menit dari langkah
  sebelumnya/awal, target kanal/pengguna, jam kerja per langkah opsional, zona Asia/Jakarta),
  `escalation_state` (tahan restart, idempoten; problem/ack/recovery tercatat).
- `action_rules.escalation_policy_id`; aturan aksi dengan kebijakan tidak kirim
  langsung — mesin tiap 30 detik (`escalation-engine` di poller) mengirim langkah
  berikutnya selagi problem terbuka & belum diakui; ack menghentikan; problem pulih
  mengirim notifikasi pemulihan; pemeliharaan menahan pengiriman; parent offline
  menahan (evaluasi ulang saat parent pulih).
- Jam kerja: per kebijakan (`business_hours_enabled`, start/end, hari ISO) atau
  per langkah (`business_hours_only`).
- Badai notifikasi: >N problem dalam 60 detik (`notification_storm_threshold`,
  default 5) digabung menjadi satu pesan ringkasan; sisanya ditahan 60 detik.
- Kontak pengguna: `users.email`, `users.telegram_chat_id` (hanya admin; eskalasi
  target pengguna memakai kanal Telegram/SMTP pertama yang aktif; "Uji kirim" tetap
  per kanal).
- **Jadwal jaga (siapa jaga pada rentang waktu) BELUM diimplementasikan — sengaja
  dilewati** (opsional, paling berat menurut urutan prioritas). Dicatat sebagai
  "belum" di TEMUAN.

### C. MFA (TOTP RFC 6238: SHA1, 6 digit, 30 detik)

- Aktivasi di menu **Profil** (halaman baru): secret server-side (Fernet),
  QR SVG buatan server (segno 1.6.6, dipin), 10 kode pemulihan sekali-pakai
  (hash SHA-256), aktif setelah kode pertama terverifikasi.
- Login: password benar → bila MFA aktif, wajib kode di langkah kedua
  (`/api/mfa/verify-login`, CSRF wajib, TTL 5 menit, anti-replay step waktu);
  kode pemulihan dapat menggantikan kode TOTP.
- Pembatasan: 5 gagal / 10 menit per akun+IP (`login_attempts` dengan username `mfa:<id>`).
- Kebijakan admin: `mfa_required_roles` (JSON list) + `mfa_grace_days`; setelah
  tenggang habis, peran wajib tidak bisa login; UI banner di Profil; pengaturan di
  Administrasi → Pengaturan umum.
- Admin bisa reset MFA pengguna lain (via menu Users, konfirmasi password admin,
  audit `auth.mfa_reset`); menonaktifkan MFA sendiri butuh password + kode.
- Kolom terenkripsi baru masuk `check-keys` & `rotate-encryption-key`
  (`users.mfa_secret`, `users.mfa_pending_secret`).
- Halaman login menampilkan langkah MFA; Profile menyediakan
  aktivasi/nonaktif + kode pemulihan sekali tampil.
- Route map: 129 → **140** rute.
- Route map E3: 140 → **149** rute (+9: `flow/summary|talkers|apps|conversations|interfaces|status` + 3 CSV).
- Dependensi baru: **segno==1.6.6** (QR SVG untuk MFA; pure-Python, tanpa dependensi;
  alasan tertulis di `requirements.txt`).
- Tes: `tests/test_webhook.py` (17), `tests/test_escalation.py` (12),
  `tests/test_mfa.py` (13). Total suite 120 → **162 passed**.
- Produksi direstart; `/healthz` 200; semua halaman 200; `/api/me` &
  `/api/mfa/status` → 401 (wajib login); `check-keys` mencakup
  `users.mfa_secret` & `users.mfa_pending_secret` (0 gagal).

Belum terverifikasi (butuh perangkat/sesi nyata):
- Pengiriman webhook ke endpoint nyata; perintah RouterOS; MFA di ponsel nyata;
  dashboard/alert belum diuji di Grafana (E4).
- Jadwal jaga (oncall) belum ada (opsional, dilewati).

## Status E3 — tertunda (menunggu giliran)

## Status E3 — Traffic Flow — selesai (2026-11-11)

| Aspek | Status |
|---|---|
| Penerima UDP 2055 (NetFlow v5/v9) & 4739 (IPFIX) | Selesai, default MATI, hanya IP perangkat terdaftar |
| v5 | Selesai & diuji (paket tiruan) |
| v9 (template per eksporter/domain) | Selesai & diuji |
| IPFIX | Selesai & diuji |
| Agregasi bucket 5 menit + top-N ("lainnya") | Selesai & diuji |
| Rollup per jam (idempoten) | Selesai & diuji |
| Retensi 7 hari / 90 hari | Selesai & diuji |
| ifIndex -> nama interface | Selesai (snapshot mt_interfaces) |
| Nama host dari lease DHCP | Selesai & diuji |
| UI: Ringkasan / Top Talkers / Aplikasi / Percakapan / Status | Selesai |
| Viewer hanya total | Selesai & diuji |
| flow_top_talker (opsional) | TIDAK diimplementasikan |

Loadtest flow: `--flow 20000 --flow-devices 20` → ~5.476 paket/s, 20.000 flow,
0 dibuang; 140 baris bucket (top-N aktif). `requirements.lock`: `netflow==0.12.2`
(pin; MIT, tanpa dependensi). Produksi direstart; `/healthz` 200; semua halaman 200;
`/api/flow/summary` 401 (wajib login). Total tes 162 → **179 passed**.

## Status E4 — tertunda (menunggu giliran)

## Status E4 — metrik Prometheus — selesai (2026-11-11)

- Token API baca-saja (Administrasi → Token API & metrik): SHA-256 hash,
  constant-time, allowlist IP, kedaluwarsa, cabut, `last_used_at`, audit.
- `GET /metrics`: format teks Prometheus via custom collector
  (`prometheus_client==0.26.0`, dipin); default MATI; Bearer token;
  rate limit 30/menit/IP; cache 10 detik; hanya dari snapshot DB;
  stale device dihilangkan; `nms_metrics_series_truncated`; label aman.
- Metrik: build_info, perangkat per status/tipe, per perangkat up/latency/
  cpu/memory/disk/temp/uptime/last_poll, interface (penting|semua|tidak),
  fiber (rx/tx dBm, suhu, redaman, margin, status), problem per severity,
  ketersediaan layanan, poller (running, durasi, sukses/galat), ingest
  (syslog/trap/flow diterima/dibuang), ukuran DB.
- Grafana: `grafana/dashboards/nms-overview.json` (belum diuji di
  Grafana nyata), provisioning + datasource contoh, `docs/INTEGRASI.md`.
- Benchmark: payload ~19 KB; repeat ~0 ms (cache 10 dtk); fresh build
  p50 ~12 ms · p95 ~51 ms · max ~77 ms @ 20 perangkat (docs/SKALA.md).
- Pasca-commit `94304c3` (E3), E4 live di produksi: screenshot admin
  membuktikan halaman Token API + buat/cabut token bekerja; `/metrics`
  aktif setelah dinyalakan di pengaturan (default MATI).
- **190 passed**; route map 149 → **153**.

## Hasil akhir E1–E4

- E1 syslog/SNMP trap — commit `3feafb1` (120 tes)
- E2 webhook/eskalasi/MFA — commit `34e42fa` (162 tes)
- E3 Traffic Flow — commit `94304c3` (179 tes)
- E4 metrik Prometheus — commit E4 (190 tes)
- Produksi `http://127.0.0.1:5500` hidup dengan keempat tahap terpasang.
- Belum terverifikasi di perangkat nyata (MikroTik utama tidak bisa diakses):
  perintah RouterOS, jalur syslog/trap flow dari perangkat asli, purge optik
  DDM, pengiriman webhook/eskalasi ke endpoint produksi, scrape Prometheus
  ke instance nyata, dan dashboard Grafana di instance nyata.

## Catatan sesi

- E1: commit `3feafb1`; produksi hidup (healthz 200); 120 tes lolos.
- E2: commit E2; produksi hidup (healthz 200); 162 tes lolos.
- H-3: kredensial scan tidak lagi disimpan; DB lama + backup dibersihkan dengan
  `VACUUM`/checkpoint; 203 tes lolos; produksi hidup (healthz 200).
- M-1: job retensi membaca pengaturan UI (termasuk retensi pemeriksaan baru);
  pratinjau memakai resolver yang sama; 208 tes lolos; produksi hidup (healthz 200).
- M-2: SSRF webhook/SMTP ditutup (izin privat/http hanya admin, DNS dipin sekali,
  galat SMTP disanitasi, port internal 5500 & DNS rebinding diuji); 226 tes
  lolos; produksi hidup (healthz 200).
- M-3: login dibatasi per IP (bukan hanya akun+IP), audit gagal login dibatasi
  dan dipotong, body request dibatasi 1 MiB (login/MFA 8 KiB); 234 tes lolos;
  produksi hidup (healthz 200).
- M-4: sesi dicabut lewat epoch saat password diganti/direset (termasuk cookie
  "remember" dan login MFA yang tertunda); 241 tes lolos; produksi hidup
  (healthz 200).
- M-5: sel CSV dinetralkan dari formula (`=`, `+`, `-`, `@`, karakter kontrol,
  termasuk yang didahului spasi) di audit, ketersediaan, dan semua ekspor CSV
  lain; 252 tes lolos; produksi hidup (healthz 200).
- M-6: kode pemulihan 6 digit tidak lagi dianggap TOTP, digit non-ASCII
  ditolak tanpa 500, dan secret MFA kosong/rusak tidak menjadi kunci HMAC
  kosong; 256 tes lolos; produksi hidup (healthz 200) dan verifikasi live
  memakai akun sementara berhasil (akun dibersihkan).
- M-7: non-admin hanya menerima pengaturan/system-info yang aman; COUNT(*)
  retensi dan tabel pindah ke koneksi baca terpisah + cache 60 detik, dengan
  indeks waktu baru; 263 tes lolos; produksi hidup (healthz 200) dan
  verifikasi live per peran berhasil (akun sementara dibersihkan).
- M-8: `waitress` dan `pyasn1` (impor langsung) ditambahkan ke
  `requirements.txt`; tes penjaga memastikan semua impor runtime pihak ketiga
  tercantum dan terkunci versi pasti di `requirements.lock`; 267 tes lolos;
  produksi hidup (healthz 200).
- M-9: semua penulisan DB memakai `db.transaction()` (commit sekali, rollback
  penuh saat gagal, transaksi bersarang ikut terluar); `_lock` me-rollback
  transaksi yang menggantung di batas blok; `purge_old_data` tidak lagi
  menelan kegagalan flow; `/healthz` memakai koneksi baca terpisah. Server
  hidup di `http://127.0.0.1:5000` (healthz 200, satu listener); 273 tes
  lolos.
