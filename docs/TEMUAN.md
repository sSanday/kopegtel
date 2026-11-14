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
