# TEMUAN — hal yang ditemukan selama perapihan (T0–T6)

Aturan: jangan memperbaiki bug di luar lingkup tahap yang berjalan; catat di sini.

## T0
- Route map baseline: 116 route (`docs/route-map.baseline.txt`). Metode yang dihitung: GET/POST/PUT/PATCH/DELETE.
- `DATABASE_PATH` sudah didukung di `backend/db.py`; variabel baku `NMS_DB_PATH` ditambahkan sebagai preferensi (DATABASE_PATH tetap diterima).
- Semua tes pytest memakai database sementara (`tests/conftest.py`); tidak ada tes menyentuh `backend/nms.db`.

## T1
- Repo git baru — tidak ada riwayat yang memuat `.env`, `*.db`, atau `*.bak` (import awal bersih; `.gitignore` menutup sejak commit pertama).
- `backend/nms.db.bak-mikrotik-20261110-112510`: disalin ke `backups/` dengan verifikasi SHA256 + `integrity_check: ok`, lalu dihapus dari `backend/` atas konfirmasi pemilik.
- `pip-audit`: awalnya 2 advisory untuk `pip 26.1.2` (PYSEC-2026-3721); pip divenv diperbarui ke 26.3 → bersih.
- `requirements-test.txt` awal menyebut `pytest-xdist` namun tidak terpasang di venv; tetap dicantumkan (opsional) dan pytest dipatok `==9.2.0`.

## T4
- Perbaikan yang diizinkan dilakukan: (1) hilangkan N+1 di `query_mt_devices` (laporan armada 4757→83 ms @ 50 perangkat); (2) indeks baru `idx_if_stats_device_name_id (device_id, if_name, id)` (laporan armada 443→344 ms @ 200); (3) pengalibit dalam harness `tools/loadtest.py` (batch `executemany` + 1 commit/tick; pada mesin 3,8 GB RAM perulangan per perangkat terlalu lambat).
- Indeks ditambah lewat blok migrasi idempotent di `init_db()` (aman untuk DB yang sudah berjalan).
- Baseline route map tidak berubah (117 route). `docs/SKALA.md` berisi hasil terukur untuk 50/200/500/1000 (1000 memakai tick 15 menit — mesin uji 3,8 GB RAM tidak sanggup 5 menit; diperingatkan di dokumen).
- Pengukuran poll: estimasi siklus masih jauh di atas interval 60 dtk pada skala ≥200 (bottleneck = API MikroTik per perangkat, bukan SQLite). Target 80% TIDAK tercapai dan didokumentasikan apa adanya; tidak ada "perbaikan sekalian" yang dilakukan.
- `PRAGMA synchronous=OFF` hanya untuk DB uji di harness; produksi tetap WAL + NORMAL.

## T2
- `docs/route-map.baseline.txt` diperbarui setelah `/healthz` ditambahkan (117 route). Diff hanya satu baris.
- Fail-fast konfigurasi sekarang juga menolak start bila `ADMIN_PASSWORD` tidak memenuhi kebijakan (baru). Lingkungan dev/tes harus punya nilai yang sah (tes sudah menyediakan).
- `python backend/app.py` sekarang mendelegasikan ke `serve.py --dev` (perilaku sama); produksi: `python backend/serve.py` (waitress, `WAITRESS_THREADS`).
- Lease instance (`instance_lease`, heartbeat 10 detik, basi > 30 detik boleh diambil alih) ditambahkan; start kedua terhadap DB yang sama ditolak dengan pesan Indonesia dan exit code 1. Jika instance mati mendadak, lease kedaluwarsa sendiri; tidak perlu pembersihan manual.
- `print()` di backend diganti `logging` (kecuali `manage.py`, yang outputnya memang UX CLI di stdout; pesan error fatal konfigurasi tetap ke stderr).
- Logging: `logs/nms.log` berputar 5 × 10 MB, `LOG_LEVEL` (default INFO), redaksi nilai SECRET_KEY/ENCRYPTION_KEY/ADMIN_PASSWORD/token/password + blok private key, logger `apscheduler` diturunkan ke WARNING.
- `/healthz`: 200 hanya bila DB terbaca DAN heartbeat poller < 90 detik; tanpa poller → 503 (by design; diproduksi poller hidup dalam proses yang sama).
- `TRUSTED_PROXIES` (jumlah proxy, default 0) memasang `ProxyFix`; IP klien untuk batas login & audit_log dari `X-Forwarded-For`. `COOKIE_SECURE=1` → cookie sesi `Secure`. `HSTS_ENABLED=1` → tambah `Strict-Transport-Security` (default mati; aktifkan hanya di belakang HTTPS). CSP tidak diubah.
- Temuan keamanan yang TIDAK diperbaiki (di luar lingkup): CSP memakai `style-src 'unsafe-inline'` (diperlukan oleh markup saat ini).
- Catatan: lease tidak mencegah kecelakaan dua proses non-lease (mis. dua gunicorn) — serve.py adalah satu-satunya pintu masuk produksi, dan keduanya memakai tabel `instance_lease` yang sama.

## T3
- Bug ditemukan & diperbaiki: `SELECT rowid` pada tabel dengan `INTEGER PRIMARY KEY` mengembalikan kolom dengan nama alias (`id`, `device_id`), bukan `rowid` → akses `row["rowid"]` gagal. Diperbaiki dengan `rowid AS _rid`; rotasi enkripsi diuji ulang (6/6 nilai, transaksi tunggal, rollback bila ada kegagalan).
- `rotate-encryption-key`: backup otomatis dulu (selalu, termasuk mode `--dry-run` — sengaja, kecuali disebutkan dapat dimatikan), satu transaksi untuk SEMUA kolom terenkripsi (devices.snmp_community, snmp_v3_auth_password, snmp_v3_priv_password, mikrotik_password, notification_channels.config, mt_device_settings.write_password/ssh_password, mt_backups.content). Kunci baru ditampilkan SEKALI; verifikasi memakai kunci baru sebelum kunci dikembalikan ke pemakai. Tidak ada nilai yang dicetak.
- `check-keys` melaporkan jumlah nilai yang gagal dekripsi per kolom tanpa nilai; exit code 1 bila > 0.
- `restore-db` menolak jalan bila lease instance segar (layanan masih hidup); membuat salinan pengaman lewat online backup API (bukan salin file — aman untuk WAL); verifikasi `integrity_check` + keberadaan tabel inti sebelum DAN sesudah menimpa; tidak pernah mencetak nilai secret.
- Salinan pengaman restore & folder backup default: `backups/` (git-ignored), dapat diatur via env `DB_BACKUP_DIR`/`DB_BACKUP_KEEP`/`DB_BACKUP_ENABLED` atau pengaturan `db_backup_dir`/`db_backup_keep`/`db_backup_enabled` (default keep=14, harian 02:45, audit ke audit_log).
- `db` module memeriksa lease lewat tabel `instance_lease`; pada restore, koneksi lama ditutup dan diganti setelah file ditimpa.
- Catatan penting (dokumentasikan di README): backup database TIDAK berisi ENCRYPTION_KEY. Tanpa kunci, kredensial di dalam backup tidak dapat didekripsi setelah restore. Kunci harus disimpan terpisah (password manager). Kunci SECRET_KEY/ENCRYPTION_KEY yang tampil sekali saat rotasi tidak dicatat di logs/.
- `rotate-secret-key` hanya membuat nilai; tidak ada yang dirotasi otomatis di .env (dengan sengaja agar operator yang mengontrol).
- `rotate-encryption-key --dry-run` tetap membuat backup otomatis. Jika tidak diinginkan, jangan gunakan perintah ini; tidak ada opsi untuk mematikan backup.
- Baseline route map tidak berubah (tidak ada endpoint baru).

## T5
- `backend/db.py` (≈4.250 baris) dipecah menjadi paket `backend/db/` (connection, schema, devices, fiber, mikrotik, mtbackup, discovery, alerts, services, problems, users, settings, audit); `__init__.py` me-re-export 248 nama publik sehingga `import db` dan pemakaian lama tetap sama.
- `backend/app.py` (≈4.300 baris) dipecah menjadi `backend/routes/` (auth, devices, mikrotik, fiber, alerts, services, discovery, problems, admin) + `backend/core/` (guard, validation, metrics, constants, net, env); `app.py` kini 191 baris (setup app, `/healthz`, HTML/statis, error handler).
- Route map: **117 rute, URL+metode identik**. Nama endpoint blueprint berubah menjadi berprefiks (mis. `api_login` → `auth.api_login`) karena keterbatasan Flask; tidak ada pemakaian `url_for`/nama endpoint di HTML/JS, jadi perilaku HTTP tidak berubah. `docs/route-map.baseline.txt` diregenerasi sekali dan dicatat di sini.
- Bug ditemukan saat restart produksi: setelah `db.py` menjadi paket, `BASE_DIR` di `db/connection.py` menunjuk `backend/` (bukan root proyek), sehingga default DB menjadi `backend/backend/nms.db` dan start gagal `unable to open database file`. Diperbaiki (`dirname` tiga level) + tes regresi `tests/test_layout.py`. Tes sebelumnya tidak menangkap karena selalu mengeset `DATABASE_PATH`.
- Penyesuaian tes (semantik tidak berubah): `conftest.py` mereset `DB_PATH`/koneksi pada `db` dan `db.connection`; `test_data.py` memakai timestamp eksplisit agar tidak flaky saat melewati batas detik; `test_mikrotik_backups.py` menargetkan `routes.mikrotik.MT_BACKUP_COOLDOWN` dan `backups.run_due_backups`.
- Tes: 84 → **87** (3 tes regresi struktur/paket). Produksi direstart dengan `backend/serve.py`; `/healthz` 200, `/api/public-config` 200, halaman utama 200.

## E1 — Penerima syslog & SNMP trap (sedang berjalan)

- Route map: 117 → **129** rute (+12 endpoint E1 di blueprint `events`).
  `docs/route-map.baseline.txt` diregenerasi.
- Decode trap memakai skema pysnmp 7.1.32 + pyasn1 BER (bukan engine
  asyncio) — engine asyncio tidak cocok dengan thread penerima. SNMPv2c
  (inform dibalas Response PDU), v1 opsional, **v3 USM belum diimplementasi**
  (dicatat; tidak ditunda diam-diam — status "belum" di PROGRES).
- Perintah RouterOS untuk syslog/trap **belum diuji pada perangkat nyata**
  (MikroTik Utama API 8728 nonaktif; perangkat asli tidak dapat diakses saat
  pengerjaan). Empty state di halaman Kejadian/Administrasi menandai
  "Verifikasi di versi RouterOS-mu."
- Collector `/interface/print` kini menarik `if-index` (field SNMP). Pemetaan
  ifIndex → nama interface bekerja lewat snapshot `mt_interfaces.data`
  (JSON berisi `if_index`). **Belum diverifikasi pada RouterOS nyata** —
  pada RouterOS < 7, `if-index` mungkin tidak tersedia; jatuh ke `None` dan
  pesan trap memakai ifIndex angka.
- RateLimiter default 200 pesan/detik/jendela 10 detik per sumber (diatur lewat
  `ingest_rate_limit_per_sec` / `ingest_rate_window_seconds`). Mekanisme parser
  3 format syslog (RFC 3164 / 5424 / polos RouterOS) sudah diuji otomatis
  (`tests/test_events.py`), bukan di perangkat asli.
- Penerima default MATI, port 5514 (syslog UDP) / 5514 (syslog TCP) / 1162
  (trap); bind address dapat diatur; hanya IP perangkat terdaftar yang
  diterima; pesan dibuang dihitung (`unknown` / `rate` / `auth` / `queue` /
  `invalid`) dan tampil di tab Status penerima.
- Loadtest ingest (`--ingest 20000 --ingest-devices 20`): ~763 msg/s dengan
  thread penulis batch aktif, 20.000/20.000 baris tertulis, 0 dibuang,
  p95 `/api/events/syslog` = **3,5 ms** (baris terakhir diukur; lihat
  docs/SKALA.md).
- Perbaikan test: `test_data.py::test_service_sla_any_mode` tidak lagi bergantung
  pada batas detik waktu (timestamp eksplisit) — flaky, hanya perbaikan tes,
  bukan perubahan semantik.
- Perbaikan bug saat pengembangan E1: (1) hitungan batch/flush ganda pada
  batcher (flush manual vs thread penulis) — tidak ada baris tertulis ganda
  karena antrean hanya dikonsumsi sekali; (2) dedup `mt_logs` vs syslog
  memecah query `IN (...)` menjadi 500 parameter per query (batas SQLite
  999 variabel).

## E2 — Webhook, eskalasi, MFA (sedang berjalan)

- Route map: 129 → **140** rute (+5 escalation, +6 mfa).
  `docs/route-map.baseline.txt` diregenerasi.
- Webhook dikirim dengan proxy dinonaktifkan (`ProxyHandler({})`) supaya keputusan
  anti-SSRF tidak bisa dialihkan lewat proxy kampus ke alamat internal. Batasan
  TOCTOU DNS: validasi & koneksi memakai resolusi berbeda — diterima sebagai risiko
  (didokumentasikan di kode), sama seperti implementasi webhook umum.
- Inform (SNMP v2c inform) membalas Response PDU — diimplementasikan & diuji;
  trap v1 opsional; **v3 USM belum**.
- Eskalasi target pengguna memakai kanal Telegram/SMTP pertama yang aktif sebagai
  transport (bukan kanal terpisah per pengguna) — didokumentasikan di UI/PROGRES.
- **Jadwal jaga (oncall) belum diimplementasikan** (opsional, langkah terakhir).
- Penggabungan badai notifikasi: amplifikasi ringkasan satu pesan per 60 detik per
  burst; state disimpan di memori (tidak tahan restart untuk badai itu saja —
  langkah individual tidak terpengaruh, tetap idempoten).
- QR MFA memakai **segno 1.6.6** (dependency baru, dipin di `requirements.lock`;
  pure-Python, tanpa dependensi). SVG disisipkan sebagai data URI `<img>` di UI.
- Anti-replay MFA: kode TOTP pada langkah waktu yang sama/lebih lama ditolak;
  verifikasi pendaftaran TIDAK mengonsumsi langkah login pertama (spesifikasi
  memilih kenyamanan pemakaian pertama + keamanan pasca-login).
- Kebijakan MFA per peran dengan masa tenggang (`mfa_required_roles`,
  `mfa_grace_days`, `mfa_policy_started_at`); di luar tenggang login ditolak 403.
- Kode pemulihan MFA disimpan sebagai hash SHA-256 (sekali tampil).
- SNMP v3 & jadwal jaga = item yang BELUM ada, sengaja.

## E3 — Traffic Flow (selesai)

- Route map: 140 → **149** rute (+9: `flow/summary`, `flow/talkers`,
  `flow/apps`, `flow/conversations`, `flow/interfaces`, `flow/status`,
  +CSV per endpoint).
- Library **netflow 0.12.2** (MIT, tanpa dependensi) dipakai untuk v1/v5/v9/IPFIX.
  Dipilih dibanding parser sendiri: template v9/IPFIX bersifat stateful per
  (eksporter, observation domain) dengan TTL dan LRU-500 — parser tangan akan
  mudah salah pada template withdrawal dan IPFIX enterprise fields.
- **Tidak diuji di perangkat nyata**: perintah RouterOS untuk mengaktifkan
  Traffic Flow belum diuji (MikroTik Utama tidak dapat diakses); ditandai
  "Verifikasi di versi RouterOS-mu" di UI. Field sampling v5 (`sampling_interval`)
  ditangani dengan mask `& 0x3FFF` — perilaku pasti pada MikroTik belum terverifikasi.
- Flow mentah TIDAK disimpan; agregasi di memori (bucket 5 menit), flush tiap
  60 detik dalam satu transaksi dengan top-100 per kunci (lainnya digabung ke
  "lainnya"); rollup ke jam (idempoten lewat `flow_hourly_rolled_until`);
  retensi: 7 hari bucket 5 menit / 90 hari per jam (dapat diatur).
- ifIndex → nama interface memakai snapshot `mt_interfaces.data` (if-index,
  ditambahkan di E1); DHCP lease → nama host dipakai untuk IP internal
  (tanpa reverse DNS).
- v9/IPFIX memakai cache template per (eksporter, jenis, observation domain)
  dengan TTL 2 jam dan batas 500 (LRU); data sebelum template dihitung lalu dibuang.
- **Reverse DNS OFF** (spec default); pemetaan host via lease DHCP MikroTik saja.
- Privasi: halaman Traffic dibatasi peran (viewer hanya total), export CSV login
  + peran, pengaturan lewat audit_log.
- **flow_top_talker problem (opsional, default mati) TIDAK diimplementasikan** —
  butuh kapasitas link per eksporter yang belum tersedia dari snapshot; dicatat.

## E4 — /metrics Prometheus + Token API (sedang berjalan)

- Route map: 149 → **153** rute (+4: `/metrics`, `/api/metrics/tokens` GET/POST/DELETE).
- Dependensi baru: **prometheus_client==0.26.0** (Apache-2.0, resmi Prometheus,
  tanpa dependensi). Render format teks lewat custom collector — escaping label
  ditangani pustaka resmi.
- Scrape TIDAK menanyai perangkat (semua dari snapshot DB/memori); cache 10 detik;
  gagal 404 bila `metrics_enabled` tidak diaktifkan (default MATI).
- Token 32-byte acak (`nms_` + base64url), hash SHA-256, constant-time compare,
  `last_used_at` di-update, allowlist IP opsional, kedaluwarsa opsional, cabut
  kapan pun. Token hanya berlaku untuk /metrics — tidak bisa dipakai endpoint `/api/*`.
- Perangkat basi (>3 interval tanpa poll, gabungan `metrics_ts`/`last_seen`)
  TIDAK diterbitkan sebagai seri per-perangkat (Prometheus menandainya stale).
- Batas kardinalitas `metrics_series_limit` (default 20.000); keluaran terpotong
  menerbitkan `nms_metrics_series_truncated 1`.
- Label tidak berisi secret/IP klien; escaping diuji dengan nama perangkat
  berisi `evil"><script>` — output tetap valid & terparse Prometheus.
- Dashboard `grafana/dashboards/nms-overview.json` (19 panel, variabel
  `datasource`, `device`, `group`), provisioning + datasource contoh di
  `grafana/provisioning/`, dokumen `docs/INTEGRASI.md` dengan scrape_config,
  4 aturan alert contoh (perangkat down, poller macet, margin fiber rendah,
  ingest drop). **Dashboard belum diuji di Grafana nyata.**
- Ingest/poller metrics (`nms_ingest_messages_total`, `nms_poller_*`) adalah
  counter sejak proses berjalan — reset saat restart; bukan ukuran absolut
  yang tahan restart.
- Benchmark scrape: payload ~19 KB; repeat ~0 ms (cache 10 dtk); fresh build
  p50 ~12 ms · p95 ~51 ms · max ~77 ms @ 20 perangkat (docs/SKALA.md).

## Temuan bug (perbaikan, ditemukan lewat tes)
- `backups.py run_due_backups`: `int(settings.get("backup_hour") or 2)` — jam backup `0` (00:00) diperlakukan falsy dan menjadi `2`. Diperbaiki dengan pengecekan `is None` eksplisit. Akar sebenarnya ada di `get_mt_device_settings` (`int(row["backup_hour"] or 2)` dan jalur save) dengan pola yang sama — juga diperbaiki. Tes `test_scheduled_backup_runs_once_a_day` hanya gagal pada rentang 00:00–00:59 WIB.
- `alerts.py send_telegram`: middlebox jaringan kampus ini me-reset sebagian koneksi TLS 1.3 ke api.telegram.org (uji 10 koneksi: TLS 1.2 = 10/10, default = 10/10, TLS 1.3 eksplisit = 8/10). Pengirim Telegram dipersempit ke TLS 1.2 (URL/payload/retry tidak berubah). Ini BUKAN bypass filter; tetap melintasi gateway dengan CA yang asli (Go Daddy); hanya versi TLS yang dipilih agar cocok dengan middlebox yang ada.
- Yang TIDAK saya lakukan: mem-bypass/menonaktifkan verifikasi sertifikat atau menambah root CA kampus ke trust store; perilaku itu tidak diperlukan dan tidak aman.

## H-3 - Kredensial scan tidak boleh disimpan (selesai)

- `scan_jobs` tidak lagi memiliki kolom `community`, `mikrotik_user`, atau
  `mikrotik_password`. Kredensial scan hanya hidup di memori thread
  pemindaian dan tidak pernah ditulis ke database.
- Database lama dimigrasikan otomatis saat `init_db()`: tabel dibuat ulang
  tanpa kolom kredensial, data aman dipertahankan, lalu `PRAGMA secure_delete=ON`,
  `VACUUM`, dan `wal_checkpoint(TRUNCATE)` agar sisa plaintext tidak tertinggal
  di `nms.db` maupun `nms.db-wal`.
- Backup historis dibersihkan dengan
  `python backend/manage.py purge-scan-secrets --include-backups`. Perintah ini
  juga memverifikasi `integrity_check` dan tidak pernah mencetak nilai secret.
- UI Discovery menegaskan kredensial hanya dipakai di memori dan tidak disimpan.
- Tes regresi `tests/test_discovery_security.py` (4 tes): skema tanpa kolom
  kredensial, kredensial tetap sampai ke thread tetapi tidak masuk DB/WAL,
  migrasi DB lama menghapus byte plaintext, dan pembersihan backup.
- Verifikasi live: `scan_jobs` tinggal 10 kolom aman; dua backup pra-migrasi
  sudah dibersihkan (`integrity_check: ok`); API daftar/detail scan tidak
  mengembalikan field kredensial. Suite: **203 passed**.

## M-1 - Pengaturan retensi UI tidak berpengaruh (selesai)

- Job `purge-old-checks` dan `purge-old-data` tidak lagi menerima `days`,
  `hourly_days`, `events_days`, atau `audit_days` hardcoded. Keduanya membaca
  pengaturan setiap kali berjalan, jadi perubahan di UI langsung berlaku pada
  pembersihan berikutnya tanpa restart.
- `db.retention_days()` menjadi satu sumber nilai retensi untuk job dan
  pratinjau. Nilai hilang/tidak valid kembali ke default; nilai di atas batas
  divalidasi ke 1..3650 hari.
- `purge_old_data` memakai `None` (bukan default truthy) untuk events/audit,
  sehingga `retention_events_days` dan `retention_audit_days` benar-benar
  dipakai.
- Retensi riwayat pemeriksaan perangkat kini dapat diatur melalui
  `retention_checks_days` (default 30 hari) dan tampil di form Retensi data
  beserta pratinjaunya. Ini menggantikan hardcode 30 hari.
- Pratinjau `_retention_preview` memakai resolver yang sama dan menampilkan
  jumlah baris `device_checks` yang akan dihapus.
- Tes regresi `tests/test_retention.py` (5 tes): resolver pengaturan, fallback
  nilai tidak valid, kecocokan pratinjau dengan hasil purge, override eksplisit
  untuk pemanggil lama, dan job scheduler tanpa kwargs hardcoded. Suite:
  **208 passed**.

## M-2 - SSRF lewat kanal webhook dan SMTP (selesai)

- `backend/netguard.py` menjadi validator tunggal target keluar: normalisasi
  host (menolak kredensial, path, port, spasi), resolusi DNS, dan blokir
  loopback/privat/link-local/CGNAT/multicast/reserved untuk IPv4 dan IPv6
  (termasuk IPv4-mapped dan NAT64). Bila satu nama menghasilkan campuran IP
  publik dan privat, seluruh target ditolak.
- Webhook: host di-resolve sekali per pengiriman, lalu setiap percobaan koneksi
  memakai IP hasil resolusi tersebut (`_PinnedHTTP(S)Connection`). SNI dan
  verifikasi sertifikat tetap memakai hostname asli. Redirect tidak diikuti dan
  proxy dimatikan, sehingga DNS rebinding maupun redirect ke jaringan internal
  tidak bisa dipakai.
- SMTP: `_PinnedSMTP` terhubung ke IP hasil validasi, tetapi EHLO/STARTTLS tetap
  memakai hostname asli agar verifikasi sertifikat benar. Port dibatasi 1..65535.
- `allow_private`/`allow_http` webhook dan `allow_private` SMTP hanya dapat
  diaktifkan admin. Operator tidak boleh mengaktifkan maupun mewarisinya dari
  kanal lama saat mengedit; target baru operator selalu divalidasi ketat.
- `POST /api/channels/<id>/test` mengembalikan 403 untuk operator bila kanal
  memakai target privat/http. Penolakan dicatat ke audit log.
- Galat SMTP dipetakan ke pesan tetap (autentikasi, koneksi, TLS, dan
  sebagainya). Banner/detail mentah hanya masuk log server, tidak pernah ke
  respons API.
- Escape hatch `ALLOW_PRIVATE_WEBHOOKS`/`ALLOW_PRIVATE_SMTP` hanya dipromosikan
  menjadi izin tersimpan saat admin menyimpan kanal yang targetnya benar-benar
  privat dan admin tidak menyatakan penolakan eksplisit. Operator tidak bisa
  memakainya untuk membuat target internal.
- Tes regresi `tests/test_channel_ssrf.py` (18 tes): izin operator ditolak,
  kanal lama permisif tidak bisa diarahkan ulang, `/test` operator 403, admin
  dapat memilih target privat (termasuk `localhost`), pengiriman privat
  ditolak, pemindaian `127.0.0.1:5500` lewat webhook/SMTP ditolak, DNS
  rebinding pada `/test` tidak melakukan koneksi, campuran DNS publik/privat
  ditolak, `fec0::/10` diblokir, semua alamat SMTP hasil validasi dicoba, dan
  galat SMTP disanitasi. Pesan penolakan tidak memuat IP hasil resolusi
  internal. Suite: **226 passed**.

## M-3 - Flooding login tanpa autentikasi & batas ukuran request (selesai)

- `auth.login_blocked` memeriksa dua ambang: **5 gagal / 10 menit per
  akun+IP** dan **50 gagal / 10 menit per IP tanpa melihat username**.
  Mengganti username tidak lagi menghindari blokir; respons `429` menyertakan
  `Retry-After: 600`.
- `login_attempts` menjadi sumber hitungan semua percobaan (bukan hanya
  audit). Username dipotong 64 karakter, catatan lebih tua dari 60 menit
  dibersihkan, dan indeks `(ip, failed_at)` serta `(username, ip, failed_at)`
  ditambahkan agar pemeriksaan tetap murah saat dibanjiri.
- `audit_log` tidak lagi menerima satu baris per kegagalan login. Hanya
  kegagalan pertama per IP dalam jendela dan saat ambang akun+IP/IP tercapai
  yang dicatat (jumlahnya terbatas, bukan satu baris per percobaan), ditambah
  cap global 500 baris `auth.login_failed` per 10 menit.
- Semua field audit dipotong di `db.add_audit_log`: username 64, IP 64, aksi
  64, objek 200, ringkasan 400 karakter. Jadi pemanggil mana pun tidak bisa
  menulis string raksasa meskipun melewati validasi route.
- Input login >64 karakter dan password >1024 karakter selalu gagal tanpa
  memicu pemeriksaan hash; kebijakan password baru juga menolak >1024
  karakter.
- `MAX_CONTENT_LENGTH` default **1 MiB** (dapat diubah lewat
  `MAX_REQUEST_BYTES`), `MAX_FORM_MEMORY_SIZE` mengikuti, dan
  `MAX_FORM_PARTS=1000`. `/api/login` dan `/api/mfa/verify-login` memakai
  batas **8 KiB**. Body berlebih menghasilkan `413` JSON
  `request_too_large`, bukan error HTML.
- Tes regresi `tests/test_login_flood.py` (8 tes): rotasi username tetap
  diblokir per IP, IP lain tidak ikut terblokir, audit terbatas dan memiliki
  cap global, username/field audit dipotong sebelum masuk DB, catatan login
  lama dibersihkan, serta body login dan API besar ditolak `413`. Suite:
  **234 passed**.

## M-4 - Sesi tidak dicabut setelah ganti/reset password (selesai)

- `users.session_epoch` (migrasi otomatis, default 0) disalin ke cookie saat
  login. `core.guard.current_user` menolak cookie yang epoch-nya berbeda,
  menghapus sesi, dan mengembalikan `401 session_revoked`.
- `db.update_user` menaikkan epoch secara otomatis setiap `password_hash`
  berubah, sehingga ganti password sendiri, reset oleh admin, dan
  `manage.py reset-password` semuanya mencabut sesi lama. Perubahan peran
  atau kontak tidak mencabut sesi.
- Sesi yang dipakai mengganti password (sendiri, atau admin mereset dirinya)
  diperbarui ke epoch baru dan tetap hidup; sesi lain milik akun itu gugur,
  termasuk cookie "remember" yang berlaku hingga 14 hari.
- Login MFA yang belum selesai juga dibatalkan bila password direset di
  tengah langkah kedua.
- Cookie lama tanpa `session_epoch` (dari versi sebelum perbaikan) ditolak
  sekali dan pengguna diminta login ulang.
- Tes regresi `tests/test_session_revocation.py` (7 tes). Suite:
  **241 passed**.

## M-5 - Formula injection pada ekspor CSV audit dan ketersediaan (selesai)

- `backend/core/csvsafe.py` menjadi satu-satunya netralisator sel CSV. Sel teks
  yang diawali `=`, `+`, `-`, `@`, atau karakter kontrol (`\t`, `\r`, `\n`)
  diberi awalan apostrof. Spasi/tab di depan formula juga diperiksa agar tidak
  menjadi bypass; angka asli dibiarkan agar tetap terbaca sebagai angka.
- `api_audit_log_csv` dan `api_report_availability_csv` memakai `csv_row`,
  sehingga username login gagal yang diisi anonim, objek/ringkasan audit, serta
  nama perangkat/grup tidak lagi dieksekusi sebagai formula saat CSV dibuka.
- Ekspor CSV lain (SLA layanan, armada MikroTik, redaman fiber, syslog/trap,
  flow) memakai helper yang sama; dua salinan `_csv_safe` lokal dihapus agar
  tidak ada jalur ekspor yang tertinggal.
- Tes regresi `tests/test_csv_injection.py` (11 tes): semua prefiks formula,
  karakter kontrol/spasi di depan, angka tetap angka, username gagal login
  anonim, dan nama perangkat/grup pada laporan ketersediaan. Suite:
  **252 passed**.

## M-6 - Kode pemulihan MFA bertabrakan dengan TOTP & input non-ASCII (selesai)

- `verify_for_login` tidak lagi menentukan jenis kode hanya dari jumlah digit.
  TOTP dicoba lebih dulu; bila tidak cocok, kode pemulihan selalu dicoba.
  Kode pemulihan 10 karakter yang kebetulan memuat tepat 6 digit (sekitar
  1,88% kode) tidak lagi dianggap TOTP, tetap dapat dipakai, dan tetap
  sekali pakai.
- Hanya **6 digit ASCII** yang masuk ke `hmac.compare_digest`. Digit Unicode
  (mis. Arab-Indik) ditolak sebagai kode salah (`401 mfa_invalid`), bukan
  `TypeError`/HTTP 500. Pemisah spasi dan tanda hubung pada TOTP tetap
  diterima.
- `mfa_secret` NULL/kosong/rusak tidak lagi menjadi kunci HMAC kosong:
  `_decode_secret` menolak secret kosong dan `verify_totp` mengembalikan
  `None` untuk secret tidak valid. Bila secret tidak dapat didekripsi, kode
  pemulihan tetap dapat dipakai sehingga pengguna tidak terkunci.
- Tes regresi `tests/test_mfa.py` (4 tes M-6): digit Unicode dan secret
  NULL/rusak, kode pemulihan 6 digit lewat alur login penuh beserta
  sekali-pakai, dan respons API non-ASCII tanpa 500. Suite: **256 passed**.

## M-7 - `/api/settings` & `/api/reports/system-info` bocor dan berat (selesai)

- `GET /api/settings` untuk non-admin sekarang hanya mengembalikan nilai aman
  yang dipakai UI monitoring: `org_name`, `timezone`, dan
  `default_poll_interval`. Path backup (`db_backup_dir`), kebijakan MFA,
  batas ingest, pengaturan token/metrik, dan `retention_preview` tidak lagi
  dikirim ke viewer/operator. Admin tetap menerima pengaturan lengkap.
- `GET /api/reports/system-info` untuk non-admin hanya memuat versi aplikasi,
  organisasi, zona waktu, uptime, status poller, dan waktu server. Versi
  Python/Flask, nama/ukuran berkas database, dan `table_counts` hanya untuk
  admin. Panel "Ukuran basis data" di Reports otomatis disembunyikan bila
  data itu tidak ada.
- Delapan `COUNT(*)` pratinjau retensi dan empat belas `COUNT(*)` informasi
  tabel tidak lagi berjalan di dalam `db._lock` global. Keduanya memakai
  `db.readonly_connection()` (koneksi baca terpisah, `query_only=ON`) dan
  cache 60 detik; perubahan pengaturan retensi langsung membatalkan cache
  pratinjau. Request lain dan poller tetap berjalan saat hitungan berlangsung.
- Indeks waktu ditambahkan lewat migrasi idempoten: `device_metrics(ts)`,
  `interface_stats(ts)`, `events(created_at)`,
  `device_metrics_hourly(hour_ts)`, dan `optical_hourly(hour_ts)`, sehingga
  pratinjau dan job purge tidak lagi memindai seluruh tabel.
- Fixture `tests/conftest.py::fresh_db` sekarang menutup koneksi di akhir tes
  agar tes berikutnya tidak mewarisi database sementara. Ini juga menghapus
  kegagalan urutan `test_login_flood.py` + `test_api.py` yang lama.
- Tes regresi `tests/test_admin_exposure.py` (7 tes): whitelist settings,
  data admin tetap lengkap, tidak ada hitungan retensi untuk non-admin,
  field internal system-info tersembunyi, admin tetap mendapat field
  internal, `table_counts` tidak menunggu lock global, dan cache pratinjau
  dipakai ulang. Suite: **263 passed**.

## M-8 - waitress tidak tercantum di requirements.txt (selesai)

- `waitress>=3.0,<4` ditambahkan ke `requirements.txt`. Sebelumnya paket ini
  hanya ada di `requirements.lock`, sehingga instalasi dari daftar runtime
  berakhir `ImportError` saat `backend/serve.py` masuk mode produksi.
  `requirements.lock` tetap memakai versi pasti `waitress==3.0.2`.
- Audit seluruh impor pihak ketiga di `backend/` menemukan masalah sejenis:
  `pyasn1` diimpor langsung oleh `backend/ingest/traps.py` (decoder/encoder
  BER) tetapi hanya tersedia sebagai dependensi transitif pysnmp. Paket itu
  kini dideklarasikan eksplisit (`pyasn1>=0.6,<1`, lock `0.6.5`) agar tidak
  hilang bila dependensi pysnmp berubah.
- Tes regresi `tests/test_requirements.py` (4 tes): setiap impor pihak ketiga
  runtime wajib ada di `requirements.txt`, waitress wajib ada di daftar
  runtime dan lock, seluruh persyaratan runtime wajib terkunci dengan versi
  pasti, dan `serve.py` harus benar-benar mengimpor waitress.
- Verifikasi: `pip install --dry-run --ignore-installed -r requirements.txt`
  berhasil dan memilih `waitress-3.0.2`; `pip check` bersih. Suite:
  **267 passed**.

## M-9 - Satu koneksi SQLite bersama, tanpa rollback (selesai)

- Akar masalah: seluruh thread memakai satu koneksi SQLite, dan setiap fungsi
  menulis beberapa statement lalu memanggil `conn.commit()` sendiri. Bila
  fungsi gagal di tengah (mis. `purge_old_data`) sebelum commit, transaksi
  tetap terbuka di koneksi bersama; commit milik thread lain berikutnya ikut
  menyimpan perubahan setengah jadi itu. `purge_old_data` juga menelan
  kegagalan purge flow, sehingga DELETE lain tetap di-commit tanpa rollup.
  `/healthz` membaca koneksi bersama tanpa `_lock`.
- `db/connection.py` sekarang menyediakan `transaction()`: blok normal
  di-commit tepat sekali setelah selesai, exception apa pun me-rollback
  seluruh blok, dan transaksi bersarang ikut transaksi terluar (kegagalan
  inner menandai transaksi luar lewat `_tx_failed` sehingga commit berikutnya
  dibatalkan). `_lock` diganti `_DatabaseLock` yang tetap reentrant tetapi
  menjadi jaring pengaman: transaksi yang masih terbuka saat lock terluar
  dilepas di-rollback, dan sisa transaksi "yatim" dari jalur luar lock
  di-rollback saat lock berikutnya diambil. Koneksi memakai
  `_TransactionConnection` yang menolak commit setelah transaksi tercemar.
- Semua blok tulis di `backend/db/*.py` (alerts, audit, devices, discovery,
  escalation, fiber, flow, ingest, mikrotik, mtbackup, problems, services,
  settings, tokens, users) memakai `with transaction() as conn:`; commit
  per-fungsi dihapus. `backend/fiber.py::resolve_link_problems`,
  `backend/mfa.py::_update_user`, dan helper lease instance ikut memakai
  `transaction()`. Hanya `schema.init_db()` dan rebuild tabel scan-job lama
  yang masih memanggil `commit()` eksplisit karena menjalankan DDL/VACUUM.
- `purge_old_data` tidak lagi menelan error purge flow: kegagalan membatalkan
  seluruh transaksi, jadi tidak ada DELETE/rollup yang tersimpan sebagian.
  `/healthz` memakai `db.readonly_connection()` (koneksi baca terpisah) dan
  tidak menyentuh koneksi bersama. `get_service()` memindahkan query perangkat
  ke dalam `_lock`; verifikasi kunci di `manage.py` berjalan di dalam lock dan
  mereset kedua alias koneksi setelah restore.
- Tes regresi `tests/test_transactions.py` (6 tes): tulisan gagal tidak bocor
  ke commit berikutnya, blok lock yang lupa commit di-rollback, transaksi
  yatim dibersihkan sebelum lock berikutnya, kegagalan transaksi dalam
  membatalkan transaksi luar, kegagalan `purge_old_data` mengembalikan DELETE
  yang sudah berjalan, dan `/healthz` tidak memakai koneksi bersama.
- Suite penuh: **273 passed** (1 peringatan deprecation Flask `__version__`
  yang sudah ada sebelumnya); `pip check` bersih; server hidup di
  `http://127.0.0.1:5000` (healthz 200, satu listener).

## M-10 - Urutan startup melanggar mekanisme lease (selesai)

- Akar masalah: `app.py` menjalankan `db.init_db()` dan `_seed_admin()` saat
  modul diimpor, sedangkan `serve.py` mengimpor `app` sebelum mengambil lease
  instance. Instance kedua karena itu bisa menjalankan migrasi skema (dan
  membuat akun admin) saat instance pertama masih hidup. Selain itu
  `poller.start_poller()` berada di luar blok `try/finally`, dan impor
  `ingest`/`flow` terjadi setelah poller dinyalakan tetapi sebelum blok
  proteksi, sehingga kegagalan di antaranya meninggalkan poller berjalan dan
  lease tidak dilepas.
- `app.py` sekarang bebas efek samping database saat diimpor. Inisialisasi
  dipindahkan ke fungsi eksplisit `bootstrap()` (skema + akun admin awal);
  `serve.py` memanggilnya hanya setelah `db.acquire_instance_lease()`
  berhasil. Mode dev `python backend/app.py` juga memanggil `bootstrap()`.
- `acquire_instance_lease()` membuat tabel `instance_lease` minimal sendiri
  (`CREATE TABLE IF NOT EXISTS`) sebelum memeriksa lease. Tabel ini prasyarat
  untuk mengambil lease, sehingga database baru tetap bisa memperoleh lease
  tanpa menjalankan migrasi skema penuh lebih dulu; tabel aplikasi lain baru
  dibuat oleh `bootstrap()` setelah lease dipegang.
- Seluruh startup setelah lease berada dalam satu blok `try/finally`:
  watchdog heartbeat dinyalakan lebih dulu, lalu `bootstrap()`, poller, dan
  penerima ingest/flow. Kegagalan `bootstrap()` atau `start_poller()` tetap
  menjalankan pembersihan (poller, ingest/flow) dan melepas lease. Impor
  ingest/flow juga dipindahkan ke dalam blok proteksi.
- Tes regresi `tests/test_startup_order.py` (6 tes): `import app, serve` tidak
  membuat tabel/migrasi/seed admin; lease bisa diambil pada database kosong dan
  hanya `instance_lease` yang dibuat; `bootstrap()` membuat skema + admin;
  instance kedua berhenti tanpa bootstrap/poller/pelepasan lease; urutan
  lease → bootstrap → poller → server dengan cleanup lengkap; kegagalan
  `start_poller()` tetap menutup poller dan melepas lease. Fixture M-9 juga
  diperbaiki agar tidak mewariskan koneksi database sementara ke tes berikutnya.
- Suite penuh: **279 passed** (1 peringatan deprecation Flask `__version__`
  yang sudah ada sebelumnya); `pip check` bersih; server hidup di
  `http://127.0.0.1:5000` (healthz 200, satu listener) dan instance kedua
  ditolak dengan exit 1 tanpa mengganggu instance pertama.

## M-11 - Verifikasi TLS dimatikan untuk RouterOS API-SSL (selesai)

- Akar masalah: ketiga jalur koneksi RouterOS (collector baca, kolektor optik,
  aksi tulis) memakai konteks TLS dengan `verify_mode = CERT_NONE` dengan alasan
  sertifikat self-signed. Kredensial API dan data konfigurasi karena itu rentan
  MITM: siapa pun di jalur bisa memalsukan sertifikat dan membaca/menyisipkan
  perintah.
- Perbaikan memakai **pinning fingerprint SHA-256 per perangkat** lewat helper
  bersama `collectors/tls.py`:
  - Fingerprint diisi -> handshake TLS (>= 1.2) tanpa validasi CA, tetapi
    sertifikat peer WAJIB cocok dengan fingerprint tersimpan; perbandingan
    constant-time (`hmac.compare_digest`) di dalam `ssl_wrapper`, sebelum
    kredensial dikirim. Tidak cocok -> koneksi ditutup, `TlsVerificationError`
    (kode `tls_fingerprint_mismatch`) dilempar.
  - Fingerprint kosong -> verifikasi CA standar (`create_default_context`,
    CERT_REQUIRED + hostname). Sertifikat self-signed ditolak; admin harus
    mengisi fingerprint.
  - Fingerprint dinormalisasi ke 64 hex huruf kecil; masukan `AA:BB:..`, spasi,
    tanda hubung, atau awalan `sha256:` diterima. Fingerprint adalah data
    sertifikat publik, bukan rahasia.
  - Fingerprint invalid tersimpan tidak pernah membuat exception mentah bocor:
    ketiga jalur gagal dengan jelas (`tls_error` / `ActionError` kode TLS).
- Perangkat menyimpan `devices.mikrotik_ssl_fingerprint` (DDL baru + migrasi
  ALTER idempoten; terverifikasi pada salinan database produksi: kolom
  `mikrotik_ssl_fingerprint` id 45, `integrity_check=ok`, 4 perangkat utuh).
  `row_to_device` mengeksposnya; create/update device menyimpannya; field yang
  tidak dikirim mempertahankan nilai tersimpan; string kosong menghapus pinning.
  Uji koneksi (`/api/devices/test`) dan aksi tulis MikroTik memakai fingerprint
  perangkat yang sama; fingerprint divalidasi juga di backend (400 bila bukan
  64 hex).
- UI: field "Fingerprint SHA-256 sertifikat" pada form perangkat, tampil hanya
  saat SSL diaktifkan, divalidasi klien (64 hex) dan server; labels error TLS
  (`tls_error`, `tls_fingerprint_mismatch`) ditambahkan di inventory.js,
  mikrotik.js, `_MT_ERROR_LABELS`, prioritas probe kapabilitas, pemetaan
  `metrics_error`, dan status HTTP aksi tulis (502).
- Ketiga lokasi temuan tidak lagi memakai `CERT_NONE` (dijaga tes regresi).
  Sertifikat CA-signed tetap bisa dipakai tanpa pinning.
- Tes regresi `tests/test_mikrotik_tls.py` (17 tes): normalisasi fingerprint;
  `ssl_wrapper` terhadap TLS server lokal self-signed (pin cocok diterima, pin
  tidak cocok ditolak sebelum data, tanpa pin ditolak CA, fingerprint tersimpan
  invalid -> `tls_error` bukan exception); `check_mikrotik` end-to-end lewat
  RouterOS tiruan di atas TLS; `perform_action` memetakan kegagalan TLS ke
  `ActionError.code`; route perangkat (normalisasi saat create, preserve saat
  field absen, clear saat kosong, 400 saat invalid); uji koneksi meneruskan dan
  memvalidasi fingerprint; tiga lokasi temuan bebas `CERT_NONE`.
- Suite penuh: **296 passed** (1 peringatan deprecation Flask `__version__`
  yang sudah ada sebelumnya); `pip check` bersih; server hidup di
  `http://127.0.0.1:5000` (healthz 200, satu listener) setelah restart.

## M-12 - ALLOWED_NETWORKS kosong berarti semua IP diizinkan untuk perangkat (selesai)

- Akar masalah: `.env.example` dan README menyatakan bahwa ALLOWED_NETWORKS
  kosong berarti hanya rentang privat (RFC1918) yang boleh dipantau, tetapi
  `core/validation.py `_ip_allowed` justru `return True` saat daftar kosong.
  Pengecualian hanya di jalur scan (`network.is_private`), dan `is_private`
  sendiri meloloskan loopback (127.0.0.0/8, ::1) dan link-local
  (169.254.0.0/16, fe80::/10). Dampaknya: perangkat bisa didaftarkan atau
  diuji ke tujuan publik, loopback, atau link-local.
- Perbaikan:
  - `core/net.py` mendefinisikan `DEFAULT_ALLOWED_NETWORKS` (RFC1918 IPv4 dan
    ULA fc00::/7 IPv6 - prinsip yang sama dengan RFC1918; CGNAT 100.64.0.0/10
    dan ruang dokumentasi TIDAK termasuk) plus helper `ip_default_allowed`
    dan `network_default_allowed` (subnet_of, menolak rentang menjulur).
  - `_ip_allowed` tanpa ALLOWED_NETWORKS kini memakai kebijakan privat itu;
    dengan ALLOWED_NETWORKS terisi, perilaku lama (hanya daftar) tidak berubah.
  - Scan (`routes/discovery.py`) memakai `network_default_allowed` alih-alih
    `is_private`: loopback/link-local/CGNAT/publik ditolak, rentang privat
    diterima; `bulk-add` dan `/api/devices*` otomatis ikut lewat `_ip_allowed`.
  - `.env.example` diperjelas: kosong berarti hanya rentang privat (RFC1918 /
    fc00::/7) untuk perangkat & scan; loopback, link-local, CGNAT, dan IP
    publik ditolak.
  - Perubahan perilaku yang disengaja: perangkat terdaftar dengan IP publik/
    loopback tidak bisa dibuat/diubah/diuji lagi sampai ALLOWED_NETWORKS
    terisi eksplisit; jaringan menjulur (mis. /15) juga ditolak scan.
- Tes regresi `tests/test_allowed_networks.py` (49 tes): unit kebijakan default
  dan `network_default_allowed` (termasuk fc00::/7, "menjulur", TEST-NET,
  loopback, link-local, CGNAT, multicast/unspecified), dan uji route: buat/
  ubah perangkat (400 untuk IP publik/loopback/link-local, 201 untuk RFC1918),
  `/api/devices/test` 400, scan 403 untuk CIDR non-privat / 202 untuk privat,
  allowlist eksplisit berlaku juga untuk IP publik, `bulk-add` melewati IP
  non-privat. Suite penuh: lihat docs/PROGRES.md.
- Catatan migrasi: deployment yang mengisi ALLOWED_NETWORKS tidak terpengaruh.

## M-13 - File sensitif di riwayat git dan zip (asesmen selesai; remediasi history menunggu keputusan)

- Berkas terdampak di riwayat: `nms.db` (307.200 byte) dan `logs/sla_demo.db`
  (335.872 byte) ditambahkan di commit `6c8158a` (2026-11-04, "rapikan kode")
  dan dihapus dari tree di `cb9ebfd` (adopsi kopegtel2). Keduanya masih
  tersedia di GitHub **sekarang ini**: `raw.githubusercontent.com/
  sSanday/kopegtel/6c8158a/nms.db` dan `.../logs/sla_demo.db` merespons
  **HTTP 200** tanpa autentikasi (branch cleanup/rapikan-kode). `origin/main`
  tidak mengandung berkas ini. Commit lokal sesudah itu (H-1..M-12) tetap
  memiliki riwayat yang sama; push berikutnya membawa riwayat itu.
- Isi snapshot (diverifikasi pada salinan blob, tanpa nilai rahasia dicetak):
  - `users`: 0 baris di keduanya. `snmp_community`, `ssh_pass`,
    `ipmi_password`, `olts.community`, `snmp_trap_community`: kosong semua.
  - `host_encryption` = `'{}'` di semua baris (bukan ciphertext).
  - IP perangkat di dalamnya: `192.168.110.167`, `192.168.110.25`,
    `192.168.110.251` (hosts, kedua DB) + `192.168.110.1` (services, hanya
    sla_demo.db) - semuanya RFC1918, fingerprint internal jaringan.
  - `sla_demo.db` berisi `settings.admin_user` (len=5, kemungkinan "admin")
    dan `settings.admin_pass_hash` - hash **scrypt** (len=162). Hash ini bisa
    di-crack offline; paparan beratnya tergantung kekuatan password lama dan
    apakah password itu dipakai ulang di tempat lain.
  - `sla_demo.db` juga berisi 12 audit_logs (hanya IP `127.0.0.1`), 1 services,
    360 service_history, 1 SLA - tidak ada rahasia.
  - `nms.db`: tidak ada kunci admin; settings hanya ambang/metrik.
- Zip yang pernah dibagikan: zip yang disebut audit berisi `.env` dengan
  `TELEGRAM_BOT_TOKEN` dan `TELEGRAM_CHAT_ID`; keduanya **tidak dibaca kode
  apa pun** (integrasi Telegram aplikasi memakai kanal di database:
  `alerts.send_telegram`, `escalation.py`, `telegram_chat_id` pengguna - bukan
  env). `.env` sendiri tidak pernah dikomit (`.gitignore`: `.env`, `*.db`,
  `logs/`), dan tidak ditemukan di mesin ini (`kopegtel-main.zip` 18/10/2026
  bersih: hanya `.env.example`; `server.log` di dalamnya nol match untuk pola
  token/password/community).
- Remediasi yang dibutuhkan (menunggu persetujuan pemilik; aksi eksternal/
  destruktif tidak dilakukan tanpa konfirmasi):
  1. Rotasi `TELEGRAM_BOT_TOKEN` (BotFather -> /revoke) - karena zip pernah
     dibagikan; bot bisa diambil alih siapa pun yang punya zip.
  2. Ganti password admin lama (hash scrypt di atas) bila dipakai ulang
     di tempat lain; bila hanya di aplikasi lama, perlakukan sebagai bocor.
  3. Bersihkan riwayat git `cleanup/rapikan-kode` (purge `nms.db` dan
     `logs/sla_demo.db` di semua commit, force-push dengan lease). Efek:
     hashes semua commit sesudah 6c8158a berubah, termasuk referensi hash di
     dokumen ini; clone lama tetap menyimpan blob (purge penuh perlu dukungan
     GitHub). Alternatif: terima risiko (paparan: IP internal + hash scrypt)
     dan cukup rotasi.
  4. Hapus baris `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` dari `.env` lokal
     (dead config).
- Sudah dilakukan: verifikasi menyeluruh di atas; tree berjalan bersih
  (`git ls-tree HEAD` tanpa .db/.env); backup hash blob sudah diambil ke
  Temp untuk asesmen.

### M-13 - Status akhir remediasi (2026-11-16, setelah persetujuan pemilik)

- Riwayat `cleanup/rapikan-kode` **ditulis ulang** dengan `git filter-branch`
  (`git rm --cached --ignore-unmatch -- nms.db logs/sla_demo.db`): berkas
  sensitif tidak ada lagi di commit mana pun; tree branch identik sebelum/
  sesudah rewrite (`git diff` kosong); ref `refs/original` dihapus; referensi
  hash pra-rewrite (mis. `6c8158a`) sudah tidak valid untuk checkout lokal
  (semua commit sesudah `6c8158a` mendapat hash baru).
- `git push --force-with-lease` berhasil: remote `cleanup/rapikan-kode` kini
  menunjuk tip hasil rewrite (lihat `git ls-remote`). URL branch untuk
  `nms.db` dan `logs/sla_demo.db` merespons **404**.
- **Akses langsung via SHA lama masih terbuka**: `raw.githubusercontent.com/
  sSanday/kopegtel/6c8158a/nms.db` dan `.../logs/sla_demo.db` tetap **HTTP
  200** - GitHub menyimpan objek tak terjangkau (cache/raw). Menghentikan ini
  sepenuhnya memerlukan permintaan purge ke GitHub Support
  (https://support.github.com/contact/private-information) dengan referensi
  hash lama; itu di luar jangkauan saya. Sementara itu, anggap isi kedua DB
  tetap dapat dibaca siapa pun yang tahu SHA.
- Isi yang terekspos untuk diketahui publik sampai purge: hash scrypt admin
  lama (`sla_demo.db`), nama pengguna admin (len=5), dan IP internal RFC1918
  (`192.168.110.167`, `192.168.110.25`, `192.168.110.251`, `192.168.110.1`).
  Tidak ada community SNMP/password SSH/IPMI/kontak Telegram/akun pengguna.
- `TELEGRAM_BOT_TOKEN` dan `TELEGRAM_CHAT_ID` dihapus dari `.env` lokal
  (tidak dikomit; tidak dibaca kode apa pun). Backup `.env` di Temp yang
  dibuat saat scrub telah dihapus 2026-11-17. **Rotasi token dilakukan
  pemilik** (BotFather /revoke) karena zip pernah dibagikan.
- Branch cadangan `backup/pre-m13-rewrite` dan salinan ekstraksi sementara
  sudah dihapus; ref lokal yang tersisa tidak mereferensikan berkas sensitif.
- 2026-11-17: `git reflog expire --expire=now --all; git gc --prune=now`
  dijalankan; objek pra-rewrite (termasuk `6c8158a`) **tidak ada lagi** di
  clone lokal (`git cat-file -t 6c8158a` -> "Not a valid object name");
  `git fsck --unreachable` bersih. Purge sisi GitHub tetap perlu Support
  (lihat di atas); teks permintaan siap pakai: `docs/GITHUB-SUPPORT-PURGE.md`.
- Sisa di tangan pemilik:
  1. Rotasi token Telegram (sudah dinyatakan akan dilakukan).
  2. ~~Ganti password admin lama~~ **SELESAI 2026-11-17** (lihat
     "Pasca-eksposur" di bawah).
  3. Permintaan purge ke GitHub Support (opsional; teks siap pakai di
     `docs/GITHUB-SUPPORT-PURGE.md`).

## L-1 - Login membocorkan keberadaan akun & oracle "password benar" (selesai)

- `check_password_hash` dilewati ketika username tidak ada, sehingga waktu
  respons berbeda antara akun tidak dikenal vs password salah (enumerasi
  akun lewat selisih waktu). Sekarang `routes/auth.py` selalu menjalankan
  satu verifikasi hash (acuan `_DUMMY_PASSWORD_HASH` dibuat sekali saat
  modul diimpor, hanya untuk menyamakan waktu).
- Respons `403 mfa_required` pada `auth.login_mfa_policy_blocked` mengungkap
  bahwa kata sandi BENAR (oracle untuk memvalidasi daftar password bocor).
  Sekarang respons disamakan dengan kata sandi salah
  (`401 Nama pengguna atau kata sandi salah.`);
  kegagalannya tetap dihitung pada batas login (`login_attempts`) dan
  dicatat di audit sebagai `auth.login_mfa_policy_blocked` untuk
  investigasi. Tes regresi `tests/test_mfa.py` diperbarui; `tests/test_low_findings.py`
  menegaskan respons identik untuk akun tidak ada/salah sandi/kebijakan
  diblokir.

## L-2 (lanjutan) - int64 overflow -> HTTP 500 pada ID dari JSON (selesai 2026-11-17)

Perbaikan L-2 menolak NaN/Infinity, tetapi bilangan bulat raksasa lolos
tanpa batas atas; dipakai sebagai ID query, SQLite melempar OverflowError
("Python int too large to convert to SQLite INTEGER") -> HTTP 500, padahal
tujuan L-2 justru menghilangkan 500. Terverifikasi: `db.get_device`,
`get_channel`, `get_template`, `find_user_by_id` semuanya OverflowError
untuk 10**30; repro: POST ke pembuat aturan alert dengan
`{"channel_id": 10**30, "template_id": 1}` menghasilkan 500.

Perbaikan (`backend/core/validation.py`):
- `SQLITE_INT64_MAX`/`SQLITE_INT64_MIN` (signed 64-bit) sebagai batas wajib.
- `parse_finite_int` menolak di luar rentang int64: input int dan string
  digit diperiksa **eksak** (jalur float dilewati, agar 9223372036854775807
  tidak ikut tertolak karena pembulatan double); input float diperiksa
  sesudah `int()` (konversi double integral selalu eksak). `10**30`,
  `2**63`, `"9223372036854775808"` -> None (400).
- `parse_finite_int`/`parse_finite_float` menolak bool (`True`/`False`) -
  field angka yang menerima `true` dari JSON kini 400, bukan diam-diam 1/1.0
  (docstring sebelumnya menyatakan menolak bool, tapi kodenya tidak).
- `_parse_optional_id` batas atas dijamin `parse_finite_int` (1..int64 max).
- Helper baru `parse_query_int(raw, default, min_value=, max_value=)` -
  int() untuk query-string yang tidak pernah melempar (dipakai di dokumentasi;
  seluruh `int(request.args.get(...))` yang tidak di-wrap sudah diverifikasi
  terbungkus try/except di seluruh routes, mis. audit-log/devices/mikrotik/
  problems/services - perilaku tidak berubah).
- Hardening tambahan: `routes/alerts.py` (port SMTP, risiko `1e999` -> inf
  -> `int()` OverflowError) dan `_int_arg` di `routes/events.py` kini
  menangkap OverflowError juga.

Titik yang terdampak (semua kini 400, bukan 500):
`routes/devices.py` (api/devices/test, existing_id),
`routes/escalation.py` (target_id langkah kebijakan),
`routes/mikrotik.py` (topology/apply child_id & parent_id),
`routes/fiber.py` (device_a_id),
`routes/alerts.py` (api/rules channel_id & template_id) dan
`_parse_optional_id` dipakai di devices/alerts/fiber/discovery/mikrotik
(group_id, device_id, parent_id, profile_id, escalation_policy_id, dst).
`db/tokens.py` (expires_in_days) sudah ter-cover lewat MAX_TOKEN_TTL_DAYS.

Tes: `tests/test_low_findings.py` +9 tes (unit parse_finite_int int64/bool,
_parse_optional_id, parse_query_int; integrasi: /api/rules channel & template
raksasa, escalation policy target_id, devices/test existing_id, fiber-links
device_a_id, topology/apply -> semuanya 400, bukan 500). Suite: 65 passed di
file itu; suite penuh lihat docs/PROGRES.md.

## L-2 - Input angka non-terhingga menghasilkan HTTP 500 (selesai)

Python `json` (dipakai Flask `request.get_json`) menerima `Infinity`/`NaN`,
sehingga `int()` pada float JSON bisa memicu OverflowError dan `float()`
lolos ke validasi/DB. Ditemukan empat kelas jalur dan semua diperbaiki
lewat helper bersama `parse_finite_float`/`parse_finite_int`
(`backend/core/validation.py`; menolak NaN/Inf/bool-non-integral dan
string kosong, menghasilkan None -> 400):

- `db/tokens.py`: `expires_in_days` tanpa cap -> `timedelta(days=10**30)`
  OverflowError. Sekarang dibatasi `MAX_TOKEN_TTL_DAYS=3650` dan ditolak
  bila non-terhingga/bukan angka.
- `routes/admin.py`: `int(value)` di blok int_keys dan `int(...)` untuk
  `retention_optical_hourly_days` - Infinity lolos -> 500. Sekarang lewat
  `parse_finite_int` (400 bila bukan angka terhingga).
- `routes/devices.py`: `int(existing_id)` (JSON float inf -> OverflowError).
- `routes/services.py`: `float(sla_target)` NaN lolos validasi
  (`nan < 0`/`nan > 100` selalu False); sekarang ditolak.
- Jalur JSON lain yang diperbaiki sejalan: `routes/mikrotik.py` (topology,
  interval, write/ssh port, backup_hour), `routes/fiber.py` (device_a_id,
  length_km, wavelength, _small_int, settings float/int, splitter loss,
  OTDR _num), `routes/events.py` (cooldown/auto_resolve/device_id/group_id/
  resolves_rule_id/port), `routes/escalation.py` (repeat_minutes,
  target_id, delay_minutes), `routes/alerts.py` (channel_id, template_id),
  `core/validation.py._parse_optional_id`.
- Tes regresi di `tests/test_low_findings.py` (settings Infinity/NaN/10**30,
  sla_target NaN, devices/test id 1e999, device_ids, api_token expires).

## L-3 - verify_api_token bisa TypeError bila expires_at rusak (selesai)

`_parse_iso` mengembalikan None untuk timestamp rusak; kode membandingkan
`None < datetime` -> TypeError -> 500 pada setiap scrape Prometheus dengan
token ber-expires_at rusak di DB. Sekarang expires_at yang terisi tetapi
tak terurai diperlakukan sebagai kedaluwarsa (token ditolak, tercatat),
dan penulisan kode diuji lewat `tests/test_low_findings.py`
(`test_verify_api_token_with_broken_expires_at_is_rejected`).

## L-4 - RateLimiter /metrics sebenarnya 1800 req/menit & dict sumber bocor (selesai)

- `RateLimiter(per_second=30, window_seconds=60)` menghasilkan limit 1800
  per 60 detik (rate tersirat 30/detik, bukan 30/menit). Kelas ditulis
  ulang: konstruktor kini `RateLimiter(limit, window_seconds)` dengan limit
  eksplisit (default aman), dan /metrics memakai `RateLimiter(limit=30,
  window_seconds=60)` = 30 scrape/menit/IP.
- Panggilan ingest (syslog/trap) dan flow mempertahankan perilaku lama
  (sustained 200/detik selama 10 detik = 2000 per window) dengan
  `limit=per_sec*window, window_seconds=window` + parsing pengaturan
  toleran string kotor.
- Dict sumber tidak pernah dibersihkan (memori tumbuh selamanya). Sekarang:
  sweep berkala (setiap 512 panggilan) + saat penuh (default 4096 sumber),
  sumber dengan seluruh kejadian kedaluwarsa dibuang, sumber baru ditolak
  (fail-safe) bila semua penuh.
- Tes: `test_metrics_rate_limited_to_30_per_minute_per_ip` (30 sukses,
  ke-31 = 429, IP lain bebas), `test_rate_limiter_sweeps_stale_sources`,
  `test_rate_limiter_rejects_new_sources_when_full` (+2 tes lama diupdate).

## L-5 - Job scan bisa "running" selamanya; scan paralel tak dibatasi; spray kredensial (selesai)

- Thread scan tanpa try/finally: exception membuat job tetap `running`.
  `_run_scan` kini membungkus pekerjaan dalam try/except/finally; pada
  kegagalan, job ditandai `error` lewat `db.fail_scan_job` (kolom status
  scan_jobs), di-log traceback, dan slot selalu dilepas.
- Jumlah scan paralel tidak dibatasi: `_SCAN_JOB_SLOTS = Semaphore(2)`;
  endpoint `POST /api/discovery/scan` menolak dengan `429 scan_busy` bila
  penuh; slot dilepas di finally thread (atau manual bila gagal sebelum
  thread dibuat).
- Probe MikroTik mengirim user+password RouterOS ke port 8728 pada SETIAP
  host yang hidup di CIDR (kredensial bocor ke host non-MikroTik/penyerang).
  **Dihapus**: endpoint tidak lagi menerima `mikrotik_user`/
  `mikrotik_password` (diabaikan bila dikirim; komunitas SNMP opsional
  tetap ada karena memang inti fungsi scan dan biasanya "public"). UI
  pemindaian disederhanakan (field & kolom RouterOS hilang); uji koneksi
  RouterOS hanya lewat formulir perangkat (button "Uji koneksi").
- UI: kolom hasil scan menjadi sysName + tipe (bukan identitas RouterOS);
  riwayat menampilkan status "gagal"; progres polling menampilkan pesan
  gagal; `data-collection.js` tidak lagi membaca `mikrotik_password`.
- Tes: `test_low_findings.py` (job gagal ditandai error, slot 429, slot
  dilepas, kredensial di-body diabaikan) + `tests/test_discovery_security.py`
  diupdate.

## L-6 - Ping: "Destination host unreachable" bisa rc 0 di Windows (selesai)

`check_ping` menganggap rc==0 selalu reachable, tetapi di Windows balasan
ICMP gagal (mis. "Destination host unreachable" dari stack lokal, TTL
expired) bisa rc 0 - perangkat "offline" tampil online di monitoring.
Verifikasi di mesin ini: timeout sesungguhnya menghasilkan rc 1, tetapi
balasan DHR tidak dapat diproduksi di jaringan Wi-Fi ini; fix defensif
dilakukan agar aman tanpa bergantung pada perilaku OS:
- `reachable` kini mensyaratkan rc==0 DAN tidak ada token kegagalan
  (multi-bahasa, lihat di bawah) DAN ada token balasan sukses
  (EN/ID/de/fr/es/pt/it/nl/pl/ru/ja/zh). Bila output tidak dikenali,
  perangkat dilaporkan tidak reachable (fail-safe).
- Update 2026-11-17 (L-6 lanjutan): token balasan/kegagalan/timeout & regex
  latency diperluas ke de, fr, es, pt, it, nl, pl, ru, ja, zh (mis. DE
  `Antwort von` / `Zeitüberschreitung`, FR `Réponse de` / `délai d'attente`,
  ES `Respuesta desde` / `tiempo de espera`, PT `Resposta de` /
  `esgotado o tempo limite`, IT `Risposta da` / `non raggiungibile`,
  NL `Antwoord van` / `Doelhost onbereikbaar`, PL `Odpowiedź z` /
  `przekroczono limit czasu`, RU `Ответ от` / `интервал ожидания`,
  JA `からの応答` / `タイムアウト`, ZH `的回复` / `请求超时`).
  Latency saja (tanpa token balasan) TIDAK lagi dianggap bukti hidup —
  latency multi-bahasa bisa cocok walau bahasanya tidak dikenal; hanya
  token balasan yang menentukan. Token kegagalan TTL masih hanya EN/ID.
  Latensi RU (`время<1мс`, satuan `мс`) ikut diparse.
- Tes: fake subprocess untuk rc=0 + DHR/timeout (not ok), balasan
  Windows/Linux (ok), 10 locale ok + 10 locale gagal-rc0 + 10 locale
  timeout + 1 locale tak dikenal (fail-safe down).

## L-7 - Galat mentah dikirim ke klien (selesai)

- `alerts.send_telegram` meneruskan `exc` (bisa memuat URL yang mengandung
  bot_token) ke API dan action_log. Sekarang: HTTPError ->
  `"Telegram menolak permintaan (HTTP <kode>)..."`; Exception lain ->
  pesan generik + tipe exception di log server; deskripsi Telegram yang
  ditampilkan dibersihkan token-nya (`_redact_token`).
- `routes/mikrotik.py` (aksi RouterOS) mengembalikan `Aksi gagal: {exc}`
  dan `str(exc)` ke klien. Sekarang respons memakai pesan kurasi per kode
  (`_ACTION_ERROR_CLIENT_MESSAGES`, mis. unreachable->502,
  not_found->404, management_interface->409); detail mentah hanya ke log;
  `mt_actions.result` dan audit menyimpan tipe exception + kode, bukan
  teks mentah (dapat berisi respons router).
- `routes/mikrotik.py` (backup 502) dan `routes/events.py` (2 warning
  penerima gagal dimuat) kini generik + `log.warning(...)` dengan tipe
  exception; detail mentah tidak lagi dikirim ke klien.
- `backups.run_backup` audit tetap memuat detail exception untuk
  investigasi (log internal, bukan respons API); `valueerror` terkurasi
  (mis. "SSH belum diaktifkan") tetap ditampilkan apa adanya.
- Tes: `test_send_telegram_*_hides_bot_token`, `test_mikrotik_action_error_is_generic`.

## L-8 - int(PORT) crash & variabel env tidak terdokumentasi (selesai)

- `core/env.py` baru: `env_int`/`env_float` (validasi terhingga dan
  rentang; error jelas, bukan traceback) dipakai app.py (PORT,
  TRUSTED_PROXIES, MAX_REQUEST_BYTES), serve.py (PORT, WAITRESS_THREADS;
  error bersih + exit 2), poller.py (POLL_MAX_WORKERS, DEGRADED_LATENCY_MS),
  dan core.env.env_flag sebagai satu sumber (menggantikan duplikasi
  `_env_flag` di app.py/guard.py/netguard.py).
- `config.config_errors()` kini memvalidasi PORT, WAITRESS_THREADS,
  POLL_MAX_WORKERS, TRUSTED_PROXIES, MAX_REQUEST_BYTES (int, rentang) dan
  DEGRADED_LATENCY_MS (float terhingga) - startup gagal dengan pesan yang
  jelas bila salah.
- `.env.example` kini mendokumentasikan semua variabel yang dipakai kode:
  ALLOW_PRIVATE_WEBHOOKS, ALLOW_PRIVATE_SMTP, NMS_DB_PATH/DATABASE_PATH,
  DB_BACKUP_ENABLED/DIR/KEEP, POLL_MAX_WORKERS (+ WAITRESS_THREADS sudah ada).
- Tes: `test_config_errors_flag_invalid_numeric_env`, `test_env_int_*`,
  `test_env_example_documents_used_vars`. Verifikasi manual: PORT=bukan-angka
  -> "ERROR konfigurasi: PORT harus berupa angka..." lalu exit 1.

## L-9 - Boolean string di API pengaturan (bool("0") == True) (selesai)

- `routes/admin.py.flag_keys` memakai `bool(value)` sehingga "0" dianggap
  True (disimpan sebagai "1"). Dipakai `netguard.as_bool` (string
  1/true/yes/on -> True; selain itu False).
- Seluruh konversi JSON->bool di jalur klien dialihkan ke `netguard.as_bool`:
  routes/devices (snmp_enabled, mikrotik_enabled, mikrotik_ssl,
  maintenance), routes/mikrotik (modules, write_enabled/write_ssl/
  ssh_enabled/backup_enabled, ignored), routes/events (allow_v1, enabled),
  routes/escalation (business_hours_enabled/enabled/business_hours_only),
  routes/alerts (enabled, allow_private/allow_http pada kanal),
  routes/problems (acked), routes/auth (remember).
  (Nilai DB/internal tetap `bool()` - itu kolom boolean, bukan input klien.)
- Tes: `test_settings_string_flags_parse_correctly`.

## L-10 - Kualitas kode: import tak terpakai & duplikasi (selesai)

- Import tak terpakai dihapus: `dbbackup.py` (glob), `flow/service.py`
  (collections), `ingest/service.py` (json), `db/ingest.py` (_days_ago),
  `db/mikrotik.py` (Fernet/InvalidToken, _get_fernet, decrypt_secret,
  get_setting), `routes/metrics.py` (json).
- `routes/metrics.py`: variabel `truncated` tidak terpakai -> `_truncated`.
- `routes/auth.py`: `session["must_change_password"]` (tidak pernah dibaca;
  guard membaca `user["must_change_password"]` dari DB) dihapus.
- `db/connection.py`: `import uuid` (satu-satunya; bukan ganda lagi) kini
  di posisi grup stdlib yang semestinya.
- `_env_flag` duplikat di app.py/core/guard.py: satu sumber baru
  `core.env.env_flag`; `netguard.env_flag` mengimpornya (dipakai webhook).
- Fungsi escape duplikat JS: `assets/js/nav.js` dan `device-form.js`
  menghapus implementasi sendiri; keduanya mendelegasikan ke
  `window.NMSFormat.escapeHtml` (format.js) dengan fallback string yang
  sama. Aset JS di-bump (nav.js?v=10, device-form.js?v=2,
  data-collection.js?v=2 di semua halaman HTML).
- Pesan usang "Tipe kanal harus telegram atau smtp." pada routes/alerts.py
  diperbaiki menjadi "telegram, smtp, atau webhook." (webhook memang kanal
  yang valid).
- `uuid` ganda: hasil audit menunjukkan hanya satu impor di
  `db/connection.py`; normalisasi penempatan dilakukan dan kerja pada
  temuan tertulis di sini.

## Pasca-eksposur - penyelesaian lanjutan (2026-11-17)

- **Password admin (hash yang beredar publik) direset paksa** lewat jalur
  DB aplikasi (`db.update_user`): hash scrypt baru; `must_change_password=1`
  → login berikutnya **wajib** mengganti password; `session_epoch` 0→1 →
  semua cookie/sesi lama gugur (M-4). Tercatat di audit log:
  `auth.admin_password_reset_cli` (user_id 0, username `system`).
  Password sementara disampaikan sekali ke pemilik (tidak ditulis ke
  berkas/log mana pun). Bila sementara hilang: `py backend\manage.py
  reset-password admin`.
- **Backup `.env` di Temp dihapus**
  (`Temp\opencode\env-backup-20261116-213719.env`, berisi nilai TELEGRAM
  token/chat id lama). `.env` saat ini hanya memuat ADMIN_USERNAME,
  ADMIN_PASSWORD, ALLOWED_NETWORKS, DEGRADED_LATENCY_MS, ENCRYPTION_KEY,
  PORT, SECRET_KEY — tanpa TELEGRAM_*.
- **Reflog lokal di-expire** (`git reflog expire --expire=now --all`) dan
  `git gc --prune=now`: objek pra-rewrite (termasuk `6c8158a`) **tidak ada
  lagi** di clone lokal (`git cat-file -t 6c8158a` → "Not a valid object
  name"; `git fsck --unreachable` bersih; 2.522 objek di 1 pack).
- **`raw.githubusercontent.com/sSanday/kopegtel/6c8158a/nms.db` masih
  HTTP 200** (2026-11-17) — objek tak terjangkau di sisi GitHub. Bukan sesuatu
  yang bisa ditutup dari mesin ini; perlu GitHub Support. Teks permintaan
  siap pakai: `docs/GITHUB-SUPPORT-PURGE.md`. Alternatif instan yang bisa
  dipilih pemilik: **jadikan repo private** (raw URL langsung 404 untuk
  akses anonim).
- **Rotasi token Telegram (BotFather /revoke) tetap di pemilik.** Kanal
  Telegram di aplikasi terenkripsi Fernet di DB (`notification_channels`),
  tidak bergantung pada `.env`; simpan token baru lewat UI pengaturan
  notifikasi — bukan ke `.env`, bukan ke git.
- **L-6 lanjutan**: ping kini mengenali output 12 bahasa (EN, ID, de, fr,
  es, pt, it, nl, pl, ru, ja, zh) — token balasan, token kegagalan, token
  timeout, dan regex latency (satuan `ms`/`мс`); latency tanpa token balasan
  bukan lagi bukti hidup; locale tak dikenal = fail-safe down. 10 locale
  diuji untuk reply/failure/timeout + 1 fail-safe. Catatan tersisa: token
  kegagalan "TTL expired" hanya EN/ID; locale selain 12 di atas tetap
  fail-safe down sampai tokennya ditambahkan.
- **Jaringan**: mesin ini di Wi-Fi **10.20.26.143** (2026-11-17);
  `192.168.30.1` tidak terjangkau (ping & TCP gagal) — perangkat 6/7/8 akan
  tetap tampil offline sampai mesin kembali ke jaringan yang meng-routing
  192.168.30.0/24. Bukan regresi (poller tidak memakai ALLOWED_NETWORKS).
- **MikroTik RouterOS API (perangkat 6)**: belum bisa diverifikasi dari
  sini karena router tidak terjangkau. Setelah jaringan kembali: di router
  jalankan `/ip service enable api` (port 8728) dan/atau `/ip service enable
  api-ssl` (port 8729), lalu simpan kredensial (+ fingerprint SHA-256 bila
  SSL, M-11) di perangkat 6 lewat UI; uji dengan tombol "Uji koneksi".
- Suite regresi L-6 lanjutan: `tests/test_low_findings.py` **56 passed**;
  suite penuh dijalankan pada sesi ini (lihat docs/PROGRES.md).
