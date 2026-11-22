# Integrasi Prometheus & Grafana

Status verifikasi (jujur): **kode /metrics sudah diuji otomatis** (format
parse oleh parser Prometheus, autentikasi token, cache, kardinalitas, escape
label) di ekosistem ini. **Dashboard Grafana belum diuji di instance Grafana
nyata** — JSON valid secara sintaksis, tetapi rupa dan perilaku panel belum
diverifikasi di Grafana. **Scrape produksi belum dicoba** — perintah di
halaman ini menunggu sesi dengan Prometheus/Grafana sungguhan.

## 1. Aktifkan /metrics di NMS

1. Login sebagai admin.
2. Buka **Administrasi → Token API & metrik** (`#tokens`).
3. Centang **Aktifkan endpoint /metrics**, pilih kardinalitas interface
   (`important` = hanya interface penting per perangkat; `all` = semua;
   `none` = tanpa metrik interface) dan batas seri (default 20.000; lebih
   dari itu keluaran dipotong dan `nms_metrics_series_truncated 1` terbit).
4. Klik **Buat token** (nama, kedaluwarsa opsional, daftar IP diizinkan
   opsional). Token **ditampilkan SEKALI** — salin dan simpan di tempat aman
   (password manager). Token tersimpan sebagai hash SHA-256; bila hilang,
   cabut dan buat baru.
5. Uji dari server Prometheus:

   ```bash
   curl -s -H "Authorization: Bearer <TOKEN>" http://127.0.0.1:5500/metrics | head -50
   # Harus: 200, Content-Type text/plain; version=0.0.4, label nms_*
   ```

Batas: `/metrics` di-cache 10 detik di proses aplikasi; tidak pernah
menanyai perangkat saat scrape; nilai perangkat yang basi (> 3 interval
tanpa poll) tidak diterbitkan supaya Prometheus menandainya stale; label
tidak memuat secret maupun IP klien. Rate limit 30 scrape/menit per IP.
Token hanya berlaku untuk `/metrics` — tidak bisa dipakai endpoint lain.

## 2. Konfigurasi Prometheus (`prometheus.yml`)

```yaml
global:
  scrape_interval: 30s
  evaluation_interval: 30s

scrape_configs:
  - job_name: nms-kopegtel
    metrics_path: /metrics
    authorization:
      credentials: <TOKEN>
      # Untuk yang lebih rapi: credentials_file (jangan commit file ini)
    static_configs:
      - targets: ["127.0.0.1:5500"]
```

Reload: `curl -X POST http://localhost:9090/-/reload` atau restart Prometheus.

## 3. Dashboard Grafana

**Belum diuji di Grafana nyata.** Bahan:

- `grafana/dashboards/nms-overview.json` — dashboard impor
  (`Grafana → Dashboards → Import → Upload JSON`). Variabel:
  - `$datasource` (uid datasource Prometheus)
  - `$device` (multi; `label_values(nms_device_up, device)`)
  - `$group` (multi; `label_values(nms_device_up, group)`)
  Panel: ringkasan armada (perangkat online, problem terbuka, poller,
  ukuran DB), CPU/memori/disk/latensi per perangkat, trafik interface
  (butuh metrik interface aktif), fiber Rx/redaman/margin, problem per
  severity, poller & ingest.
- `grafana/provisioning/dashboard.yml` — contoh provisioning (file
  provider; sesuaikan `path` ke folder dashboard Anda).
- `grafana/provisioning/datasources.yml` — contoh datasource Prometheus.

## 4. Contoh aturan alert Prometheus

Simpan sebagai `alerts.yml` (referenced dari `rule_files` di `prometheus.yml`):

```yaml
groups:
  - name: nms-kopegtel
    rules:
      - alert: NmsPerangkatDown
        expr: nms_device_up == 0
        for: 3m
        labels:
          severity: high
        annotations:
          summary: "Perangkat {{ $labels.device }} down"
          description: "nms_device_up=0 selama 3 menit pada {{ $labels.device }} (grup {{ $labels.group }})."

      - alert: NmsPollerMacet
        expr: time() - nms_poller_poll_last_success_timestamp_seconds > 300
        for: 5m
        labels:
          severity: high
        annotations:
          summary: "Poller NMS macet (> 5 menit tanpa poll berhasil)"
          description: "Periksa layanan NMS: poller berhenti atau semua perangkat tidak merespons."

      - alert: NmsFiberMarginRendah
        expr: nms_fiber_link_margin_db < 3
        for: 10m
        labels:
          severity: warning
        annotations:
          summary: "Margin fiber {{ $labels.link }} rendah ({{ $value }} dB)"
          description: "Margin redaman < 3 dB — SFP/sambungan/konektor perlu diperiksa."

      - alert: NmsTelemetriIngestDrop
        expr: increase(nms_ingest_messages_total{result=~"dropped_.*"}[15m]) > 100
        for: 10m
        labels:
          severity: warning
        annotations:
          summary: "Banyak paket {{ $labels.kind }} dibuang (> 100 dalam 15 menit)"
          description: "Periksa sumber asing atau laju melampaui batas pada penerima."
```

## 5. Catatan keamanan

- Token **hanya** disimpan sebagai hash di basis data; yang tampil sekali
  saat dibuat tidak bisa dipulihkan. Cabut dan buat ulang bila bocor.
- Sesuaikan `TRUSTED_PROXIES` bila NMS di belakang reverse proxy — IP klien
  untuk allowlist token & batas login mengikuti header proxy.
- `/metrics` tidak membutuhkan login (Bearer token sebagai autentikasi) —
  lindungi akses jaringan ke port aplikasi bila jaringan tidak tepercaya.
- Viewer di aplikasi web tidak bisa membuat token (hanya admin), dan token
  tidak bisa dipakai untuk endpoint `/api/*` apa pun.

## 6. Batasan yang diketahui

- Contoh dashboard **belum pernah dibuka di Grafana sungguhan**; impor dan
  verifikasi panel saat sesi Grafana tersedia.
- `nms_ingest_messages_total` dan `nms_poller_*` adalah penghitung sejak
  proses terakhir berjalan (reset saat restart) — bukan counter absolut
  yang tahan restart.
- Metrik interface hanya terbit sesuai pengaturan
  `metrics_interfaces` (`important` default) — pastikan interface penting
  ditandai di pengaturan MikroTik perangkat bila ingin metrik interface
  minimal.
- Perangkat yang tidak ter-poll > 3 interval tidak menerbitkan seri
  per-perangkatnya (Prometheus menandainya stale).
