# BACKEND-TODO — permintaan perubahan dari pekerjaan UI

File ini mencatat hal yang **tidak bisa diselesaikan dengan benar hanya di frontend**
dan membutuhkan perubahan kontrak/skema backend. Jangan dikerjakan tanpa persetujuan
pemilik; setiap perubahan wajib menambah tes regresi di `tests/`.

Status: usulan, belum diimplementasikan.

---

## 1. `GET /api/activity` — tambahkan `kind` dan/atau `severity`

- **Konteks UI:** feed "Aktivitas Terbaru" di dasbor perlu membedakan kejadian
  baik/buruk. Saat ini UI memetakan `old_status`/`new_status` sebagai fallback
  (offline → bahaya, degradasi → waspada, lainnya → normal) karena pesan bebas
  tidak boleh di-regex.
- **Usulan:** tambahkan field `kind` (`info|warning|critical`) dan/atau
  `severity` pada setiap entri `db.recent_activity()`.
- **Dampak:** aditif; klien lama tetap jalan. Butuh tes kecil di
  `tests/test_*` yang memeriksa bentuk respons.
- **Sampai selesai:** fallback status di `assets/js/dashboard.js` dipertahankan.

## 2. `GET /api/flow/talkers|apps|conversations` (+ `.csv`) — parameter `interface`

- **Konteks UI:** filter interface pada halaman Lalu lintas kini hanya berlaku
  untuk tabel "Trafik per interface" (filter dipindah ke panel tersebut) karena
  backend tidak menyimpan dimensi interface pada agregat `src/dst/pair/app/proto`.
  Baris `flow_buckets` untuk kind tersebut hanya berisi
  `(exporter_device_id, bucket_ts, kind, key)`.
- **Usulan:** saat menulis bucket di `backend/flow/service.py`, sertakan label
  interface masuk/keluar (mis. kolom `if_name` / key `src@ifname`), atau tabel
  agregat terpisah `flow_buckets_iface`. Lalu terima `interface=<nama>` di
  endpoint top-N dan ekspor CSV. Butuh migrasi skema + backfill/retensi.
- **Dampak:** perubahan skema dan ingestion — perlu persetujuan eksplisit,
  uji `tests/test_flow.py`, dan rencana retensi.
- **Sampai selesai:** UI tidak menawarkan filter global interface agar tidak
  menyesatkan; opsi interface tetap dimuat ulang saat eksporter/rentang berganti.

## 3. Dasbor — endpoint tren/sparkline (opsional)

- **Konteks UI:** kartu statistik idealnya menampilkan sparkline, tetapi
  `/api/summary` tidak menyediakan deret waktu. UI tidak boleh mengarang data.
- **Usulan:** `GET /api/dashboard/trend?range=24h` →
  `{ devices: [...], problems: [...], availability: [...], grain: "hourly" }`.
  Bisa diturunkan dari `device_checks`/`events`.
- **Dampak:** endpoint baru, read-only, role sama dengan `/api/summary`.

## 4. `GET /api/devices` — urutkan berdasarkan prioritas status (opsional)

- **Konteks UI:** dasbor mengambil per status (`status=offline|degraded|unknown|
  maintenance`, `per_page=100`) lalu mengurutkan di klien agar perangkat offline
  tidak terlewat pada armada besar. Cara ini benar tetapi berbiaya beberapa
  request dan tetap dibatasi 100 per status.
- **Usulan:** parameter `sort=status_rank` (offline → unknown → maintenance →
  degraded → online, lalu nama) di `db.query_devices`.
- **Dampak:** aditif; tanpa perubahan skema.

## 5. `.env` / konfigurasi UI terang-gelap (bila diperlukan)

- **Konteks UI:** mode gelap/terang mengikuti `prefers-color-scheme` dan override
  `data-theme` di sisi klien. Tidak ada backend yang dibutuhkan saat ini.
- **Catatan:** bila nanti preferensi tema ingin disimpan per pengguna, tambahkan
  kolom preferensi di tabel `users` — belum diminta.
