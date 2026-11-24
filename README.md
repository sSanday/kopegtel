# NMS Kopegtel

Network Management System untuk Koperasi Pegawai Telkom: pemantauan jaringan
(ping/SNMP/MikroTik API), topologi, pelaporan, fiber optik (DDM/OTDR), dan
Modul MikroTik (M1–M6) — baca-dulu, tulis-hanya-bila-diaktifkan secara sadar.

## Ringkasan & arsitektur singkat

- **Backend**: Python + Flask. `backend/app.py` hanya berisi pembuatan app,
  health check, penyajian HTML/statis, dan error handler; seluruh endpoint API
  berada di `backend/routes/` (satu blueprint per domain) dan helper bersama di
  `backend/core/`. REST API JSON di `/api/*`.
- **Basis data**: SQLite (`backend/nms.db`, WAL mode) melalui paket
  `backend/db/`. Migrasi skema otomatis saat start setelah lease instance
  diperoleh (`app.bootstrap()` → `db.init_db()`).
- **Pemantauan berkala**: APScheduler dalam proses yang sama (`backend/poller.py`).
- **Autentikasi**: sesi cookie HttpOnly + SameSite=Lax + token CSRF untuk semua
  metode tulis. Batas percobaan login (5 gagal / 10 menit per akun+IP dan
  50 gagal / 10 menit per IP), batas ukuran body request (default 1 MiB;
  login/MFA 8 KiB), dan sesi lama otomatis dicabut setelah password
  diganti/direset.
- **Ekspor CSV**: sel yang berpotensi menjadi formula (`=`, `+`, `-`, `@`)
  dinetralkan sebelum ditulis, termasuk nilai dari input anonim.
- **Peran**: admin (semua), operator (aksi termasuk tulis MikroTik bila
  diaktifkan), viewer (hanya baca; detail pelanggan disamarkan).
- **Produksi**: `backend/serve.py` memakai waitress (default 8 thread),
  satu instance per database (lease di tabel `instance_lease`).

## Persyaratan

- Python 3.11+ (diuji di 3.13), Windows dan Linux.
- Koneksi jaringan ke perangkat terpantau (ping/SNMP/MikroTik API).
- Untuk modul MikroTik: API RouterOS aktif pada perangkat + user baca.

## Instalasi

### Windows

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
copy .env.example .env
# isi .env (lihat tabel di bawah)
.\.venv\Scripts\python.exe backend\manage.py check-config
.\.venv\Scripts\python.exe backend\serve.py
```

### Linux

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements.lock
cp .env.example .env
nano .env
./.venv/bin/python backend/manage.py check-config
./.venv/bin/python backend/serve.py
```

Aplikasi berjalan di `http://127.0.0.1:5500` secara default (`HOST`/`PORT`
di `.env`). Mode dev (`app.run`, bukan untuk produksi):
`.\.venv\Scripts\python.exe backend\app.py`.

> **Windows:** selalu jalankan lewat `.venv\Scripts\python.exe`, bukan `python`
> polos. Jika `python` menunjuk ke Microsoft Store (`WindowsApps`), proses
> berjalan di AppContainer dan Windows memblokir ICMP/ping serta UDP SNMP —
> gejalanya `Ping: Gagal: unreachable` dan `SNMP: No response` walau perangkat
> hidup. Folder proyek berlabel integritas *Low* juga menyebabkan hal yang sama;
> simpan venv di folder biasa dan jalankan
> `tools\pilot_connectivity.py` untuk memeriksa.

## Pengembangan & pengujian

```bash
# seluruh tes (database sementara; tidak menyentuh backend/nms.db)
.\.venv\Scripts\python.exe -m pytest tests/ -q

# peta rute harus sama dengan docs/route-map.baseline.txt (117 rute)
python tools/route_map.py rute.txt
# bandingkan URL+metode dengan baseline; nama endpoint blueprint berprefiks
# (mis. auth.api_login) tetapi URL dan metode tidak berubah.

# uji beban perangkat tiruan (lihat docs/SKALA.md)
python tools/loadtest.py --scale 200
```

Jangan menjalankan `pytest` atau `loadtest` saat produksi memakai database
yang sama — keduanya memakai database sementara, tetapi beban CPU/IO tetap
berpengaruh. Skrip pengembangan lain ada di `tools/dev/`.


## Variabel .env

| Variabel | Wajib | Keterangan |
| --- | --- | --- |
| `SECRET_KEY` | ya | Kunci sesi Flask, minimal 32 karakter acak |
| `ENCRYPTION_KEY` | ya | Kunci Fernet (kredensial terenkripsi di DB) |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | ya* | Akun admin awal; min 10 karakter; wajib ganti saat login pertama |
| `ALLOWED_NETWORKS` | tidak | CIDR yang boleh dipantau (kosong = hanya RFC1918 privat) |
| `HOST` / `PORT` | tidak | Default `127.0.0.1:5500` |
| `DEGRADED_LATENCY_MS` | tidak | Ambang latensi perangkat "degraded" (default 250) |
| `APP_VERSION` | tidak | Versi ditampilkan di halaman Laporan |
| `WAITRESS_THREADS` | tidak | Jumlah thread waitress (default 8) |
| `LOG_LEVEL` | tidak | `DEBUG`/`INFO`/`WARNING`/`ERROR` (default `INFO`) |
| `COOKIE_SECURE` | tidak | `1` bila diakses lewat HTTPS (cookie sesi menjadi Secure) |
| `HSTS_ENABLED` | tidak | `1` hanya bila aplikasi berada di belakang HTTPS |
| `TRUSTED_PROXIES` | tidak | Jumlah proxy tepercaya (IP klien dari `X-Forwarded-For`) |
| `MAX_REQUEST_BYTES` | tidak | Batas ukuran body request API/form (default 1048576 = 1 MiB) |
| `DB_BACKUP_ENABLED` | tidak | Backup online harian (default aktif) |
| `DB_BACKUP_DIR` | tidak | Folder backup (default `backups/`) |
| `DB_BACKUP_KEEP` | tidak | Jumlah backup yang disimpan (default 14) |
| `NMS_DB_PATH` | tidak | Lokasi database (default `backend/nms.db`) |

*`ADMIN_PASSWORD` hanya dipakai sekali untuk membuat akun awal saat tabel
`users` masih kosong; setelah itu diabaikan.

## Menjalankan sebagai service

### Windows (NSSM)

```powershell
nssm install NMS-Kopegtel "C:\apps\nms\.venv\Scripts\python.exe" "C:\apps\nms\backend\serve.py"
nssm set NMS-Kopegtel AppDirectory C:\apps\nms
nssm set NMS-Kopegtel Start SERVICE_AUTO_START
nssm start NMS-Kopegtel
```

### Linux (systemd)

```ini
[Unit]
Description=NMS Kopegtel
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/nms
ExecStart=/opt/nms/.venv/bin/python /opt/nms/backend/serve.py
Restart=on-failure
Environment=PATH=/opt/nms/.venv/bin

[Install]
WantedBy=multi-user.target
```

## Reverse proxy (contoh)

Atur `COOKIE_SECURE=1`, `HSTS_ENABLED=1`, `TRUSTED_PROXIES=1`; biarkan
`HOST=127.0.0.1` agar aplikasi hanya menerima koneksi dari proxy.

**Caddy** (`Caddyfile`):

```
nms.internal.example.com
reverse_proxy 127.0.0.1:5500
```

**nginx**:

```nginx
server {
    listen 443 ssl;
    server_name nms.internal.example.com;
    ssl_certificate     /etc/letsencrypt/live/nms.internal.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/nms.internal.example.com/privkey.pem;
    add_header Strict-Transport-Security "max-age=31536000" always;
    location / {
        proxy_pass http://127.0.0.1:5500;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
    }
}
```

CSP tidak diubah oleh modul ini. Jangan jalankan di internet publik tanpa
VPN/ACL internal — aplikasi tidak dirancang untuk paparan langsung.

## Firewall

Prinsip: hanya reverse proxy/admin yang boleh membuka port aplikasi; server NMS
boleh keluar ke perangkat yang dipantau, tetapi perangkat tidak perlu membuka
akses masuk ke NMS.

**Windows Firewall** (PowerShell, jalankan sebagai admin):

```powershell
# Port aplikasi hanya dari reverse proxy lokal
New-NetFirewallRule -DisplayName "NMS 5500 dari proxy" -Direction Inbound `
  -Protocol TCP -LocalPort 5500 -RemoteAddress 127.0.0.1 -Action Allow
# Akses admin ke UI hanya lewat HTTPS proxy (443) atau VPN; jangan buka 5500 ke LAN
```

**Linux (ufw)**:

```bash
sudo ufw allow from 127.0.0.1 to any port 5500 proto tcp
sudo ufw allow 443/tcp          # reverse proxy
sudo ufw enable
```

**Linux (nftables/iptables)**: setara — izinkan `tcp/5500` hanya dari alamat
proxy, izinkan `tcp/443` dari LAN/VPN, dan tolak sisanya.

Koneksi keluar yang perlu diizinkan dari server NMS:

- ICMP + SNMP `udp/161` ke perangkat yang dipantau;
- MikroTik API `tcp/8728` (atau API-SSL `tcp/8729`) dan SSH `tcp/22` (M6) ke
  perangkat MikroTik;
- `https://api.telegram.org` (kanal Telegram) atau server SMTP internal;
- pembaruan paket OS/Python bila diperlukan.

Batasi juga sisi perangkat: user MikroTik memakai `address=<IP server NMS>`,
SNMP `allowed-address`, dan firewall RouterOS hanya mengizinkan API/SSH dari
IP NMS.


## Backup & restore

Backup menyalin database lewat SQLite backup API (aman saat berjalan):

```bash
python backend/manage.py backup-db                # → backups/nms-YYYYMMDD-HHMMSS.db
python backend/manage.py backup-db --keep 30
```

Backup online juga dijadwalkan otomatis setiap hari pukul **02:45** (waktu
aplikasi) selama `db_backup_enabled` aktif; jumlah salinan mengikuti
`DB_BACKUP_KEEP`/pengaturan (default 14).

Backup **tidak berisi** `ENCRYPTION_KEY`. Simpan kunci itu terpisah (mis.
password manager) — tanpa kunci, kredensial di dalam backup tidak dapat
didekripsi setelah restore.

Restore (layanan harus berhenti; salinan pengaman dibuat otomatis):

```bash
python backend/manage.py restore-db backups/nms-YYYYMMDD-HHMMSS.db
```

Rotasi kunci (menampilkan kunci baru **sekali**; backup otomatis dulu; semua
nilai terenkripsi ditulis ulang dalam satu transaksi):

```bash
python backend/manage.py check-keys               # periksa dulu
python backend/manage.py rotate-encryption-key --dry-run
python backend/manage.py rotate-encryption-key
python backend/manage.py rotate-secret-key        # sesi login jadi tidak berlaku
```

## Menyiapkan MikroTik

User baca untuk NMS (bukan hak tulis), contoh RouterOS v7:

```
/user group add name=nms-ro policy=read,api
/user add name=nms-ro group=nms-ro password=GANTI-SAYA address=192.168.30.0/24
```

- **API SSL** (opsional, bila `address=` dibatasi, pertimbangkan `policy=read,api,ssh` dan port 8729).
  Sertifikat yang dipakai RouterOS umumnya **self-signed**, sehingga NMS tidak menerima
  sertifikat itu begitu saja: isi **Fingerprint SHA-256** sertifikat pada form perangkat
  (muncul saat checkbox SSL dicentang). Ambil fingerprint dari WinBox/Terminal
  (`/certificate print detail` → kolom `fingerprint`) atau dari mesin NMS:
  `openssl s_client -connect <ip>:8729 </dev/null 2>/dev/null | openssl x509 -noout -fingerprint -sha256`.
  Format `AA:BB:...` langsung bisa ditempel. Kosongkan untuk menghapus pinning; tanpa
  fingerprint hanya sertifikat tepercaya CA yang diterima. Fingerprint adalah data
  sertifikat publik (bukan kata sandi) dan berubah bila sertifikat diperbarui.
- **SNMP**: aktifkan dan batasi `allowed-address`; isi komunitas hanya di form perangkat.
- **SSH untuk backup konfigurasi (M6, opsional)**: user terpisah, contoh `policy=sensitive,read`;
  halaman MikroTik → perangkat → *Backup konfigurasi (SSH)*; `host key` dicatat ketat.
- **User tulis (M5, opsional, default mati)**: user berbeda dengan policy `write`;
  peringatkan bahwa policy write itu luas — aktifkan hanya bila memang diperlukan.

Lalu: halaman **MikroTik** → perangkat → aktifkan *API MikroTik* (port 8728/8729),
atau centang *SNMP* pada pengaturan perangkat. Tamu (viewer) hanya melihat jumlah.

## Upgrade

```bash
# 1. Backup database (wajib)
.\.venv\Scripts\python.exe backend/manage.py backup-db
# 2. Perbarui kode & dependensi
git pull
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
# 3. Mulai layanan; migrasi skema otomatis bila perlu
.\.venv\Scripts\python.exe backend/serve.py
# 4. Verifikasi
curl -s http://127.0.0.1:5500/healthz   # {"status":"ok"}
```

Jika skema berubah, `init_db` menambahkan kolom/indeks/tabel secara idempotent.
Kembalikan dari backup jika gagal: `python backend/manage.py restore-db`.

## Troubleshooting

| Gejala | Penyebab umum | Penanganan |
| --- | --- | --- |
| Perangkat `api_disabled` | API MikroTik belum diaktifkan | Aktifkan di pengaturan perangkat |
| Tab MikroTik `auth_failed` | User/kata sandi salah | Periksa user & kredensial |
| `snmp_no_response` | SNMP mati/diblokir | Cek `snmp` di router, ACL, UDP 161 |
| Ping `unreachable` + SNMP timeout, padahal perangkat hidup | Proses Python berjalan Low/AppContainer (Microsoft Store) sehingga ICMP & UDP diblokir | Jalankan `.venv\Scripts\python.exe` dari Python biasa; pastikan folder venv tidak berlabel *Low*; uji dengan `tools\pilot_connectivity.py` |
| `database is locked` | Dua proses menulis DB | Ada satu instance saja; cek `instance_lease` |
| `/healthz` 503 | Poller mati/DB rusak | Lihat `logs/nms.log`; restart service; restore bila perlu |
| Login gagal berulang | Akun atau IP terblokir sementara | Tunggu 10 menit atau hubungi admin |
| Fleet report lambat | Tabel metrik besar | Lihat docs/SKALA.md; `ANALYZE`/`VACUUM` |
| `Uji kirim` Telegram gagal berulang | Jaringan memfilter api.telegram.org (reset acak) | Aplikasi sudah memakai TLS 1.2; minta IT mengizinkan server ini ke api.telegram.org; atau pakai SMTP internal |

## Checklist keamanan

- [ ] `.env` tidak di-commit; `ENCRYPTION_KEY` tersimpan terpisah dari backup DB
- [ ] Akun admin awal diganti kata sandinya; `viewer` dipakai untuk akses baca
- [ ] `HOST=127.0.0.1` di belakang proxy HTTPS + `COOKIE_SECURE=1`, `HSTS_ENABLED=1`, `TRUSTED_PROXIES` sesuai
- [ ] User MikroTik baca-saja; API-SSL bila memungkinkan; user tulis hanya bila dibutuhkan
- [ ] `ALLOWED_NETWORKS` dibatasi; SSH/MikroTik hanya dari IP server NMS
- [ ] Backup DB terjadwal aktif; pernah diuji `restore-db` di lingkungan uji
- [ ] `logs/nms.log` dipantau; rotasi otomatis 5 × 10 MB

## Struktur folder

```
backend/
  app.py            entry Flask: setup app, healthz, HTML/statis, error handler
  serve.py          entry produksi (waitress + lease instance)
  manage.py         CLI: admin, backup/restore, check/rotate key
  config.py         validasi env fail-fast + logging berputar/redaksi
  routes/           blueprint per domain:
    auth.py         login/logout/sesi/password
    devices.py      perangkat, metrik, topologi, grup, profil, pemeliharaan
    mikrotik.py     M1–M6: ringkasan, tab, temuan, metrik, aksi, backup
    fiber.py        F1–F5: optik, link, pengukuran, profil, laporan
    alerts.py       kanal, template, aturan aksi, action_log
    services.py     layanan & SLA + CSV
    discovery.py    pemindaian CIDR & tambah massal
    problems.py     masalah & aktivitas
    admin.py        audit log, laporan, pengaturan, pengguna
  core/             guard (CSRF/auth), validasi, metrik, konstanta, env
  db/               paket database per domain (connection, schema, devices,
                    mikrotik, mtbackup, fiber, alerts, services, ...);
                    __init__.py me-re-export nama publik lama
  collectors/       kolektor MikroTik/SNMP/optik/ping + backup SSH (M6)
  poller.py         APScheduler: poll berkala, alert, retensi, backup harian
  alerts.py         pengiriman Telegram/SMTP
  backups.py        orkestrasi backup konfigurasi MikroTik (M6)
  dbbackup.py       backup online database terjadwal
assets/             front-end statis (JS per halaman, CSS, ikon)
tests/              pytest; fake RouterOS HANYA di sini
tools/              util dev/QA (package.py, route_map.py, loadtest.py, pilot_connectivity.py)
tools/dev/          skrip pengembangan satu-kali (tidak dipakai produksi)
docs/               SKALA.md, TEMUAN.md, route-map.baseline.txt
logs/               nms.log (rotasi 5 × 10 MB, git-ignored)
backups/            backup DB (git-ignored)
```

Lihat [docs/SKALA.md](docs/SKALA.md) untuk batas kapasitas terukur dan
kriteria kapan beralih ke PostgreSQL/TimescaleDB atau poller terpisah.
Temuan/batasan yang sengaja tidak diubah ada di [docs/TEMUAN.md](docs/TEMUAN.md).
