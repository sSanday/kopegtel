# SKALA.md — uji skala & kinerja (perangkat TIRUAN)

> **Catatan penting**: seluruh pengukuran di halaman ini memakai perangkat
> sintetis (`tests/fake_routeros.py` + pengisian riwayat 24 jam melalui SQL
> batch). Perangkat keras, jaringan, dan RouterOS asli dapat berbeda hasilnya.
> Angka berarti: batas AMAN yang kami rekomendasikan, bukan jaminan.

## Metodologi

`tools/loadtest.py --scale N`:

1. Membuat DB sementara (`NMS_DB_PATH`), mengisi N perangkat sintetis
   (±70% MikroTik dengan 10 interface — 20% di antaranya port optik — sisanya
   ping/SNMP).
2. Mensimulasikan 24 jam polling yang dipercepat ke tabel yang sama dengan
   produksi (`device_checks`, `device_metrics`, `interface_stats`,
   `mt_metric_history`), dengan granularitas:
   - skala 50/200/500 → **tick 5 menit** (288 tick),
   - skala 1000 → **tick 15 menit** (96 tick) — mesin uji 3,8 GB RAM tidak
     sanggup mengisi 5 menit (tertukar / terlalu lambat).
   ⚠️ Baris yang terisi hanya 1/5 (tick 5) dan 1/15 (tick 15) dari polling
   60 detik nyata per hari; perkiraan pertumbuhan DB per hari dinyatakan
   sebagai ukuran terukur ×5 (tick 5) / ×15 (tick 15).
3. Mengukur: ukuran DB, RSS memori, error `database is locked`, durasi poll
   nyata (sampel 10 perangkat: fake MikroTik API + ping 127.0.0.1, berurutan),
   p95 endpoint utama (10–15 repetisi).
4. Skenario: 50 / 200 / 500 / 1000 perangkat.
5. Pengisian memakai batch `executemany` + satu commit per tick; `synchronous=OFF`
   hanya untuk DB uji.

## Skenario ingest (E1 — syslog/trap)

`tools/loadtest.py --ingest N --ingest-devices M`:

1. DB sementara; M perangkat MikroTik sintetis; N pesan syslog tiruan
   (3 format RFC 3164 / RFC 5424 / polos RouterOS) dikirim lewat
   `IngestManager` (bukan socket nyata) dengan thread penulis batch seperti
   di produksi, lalu `p95` endpoint `/api/events/syslog` diukur.
2. Hasil pengukuran di mesin uji (3,8 GB RAM):

| Pengukuran | Nilai |
| --- | --- |
| 2.000 pesan × 20 perangkat (enqueue+flush) | total ~2.065 msg/s · antrean mulus · 0 dibuang |
| 20.000 pesan × 20 perangkat | ~763 msg/s total · 20.000 baris tersimpan · 0 dibuang · RSS 33→39 MB |
| Ukuran DB setelah 20.000 pesan | ~13,5 MB |
| p95 `/api/events/syslog` setelah 20.000 pesan | **3,5 ms** (mean 3,5 ms) |

   Angka sebelum batcher dijalankan (tanpa thread penulis) terlihat lebih
   tinggi (~2.000-4.000 msg/s) tetapi menahan 10.000 pesan di antrean — bukan
   perilaku produksi. Dengan thread penulis aktif, throughput realistis dan
   tidak ada paket dibuang.
3. Batas aman yang direkomendasikan: penerima mampu melaju > 700 pesan/s
   per instans dengan overhead SQLite WAL; jauh di atas "200 pesan/detik
   selama 10 detik" per perangkat yang menjadi batas laju banjir. Retensi
   bawaan syslog 14 hari / trap 30 hari; jam pembersihan `purge-old-data`
   juga memangkas tabel E1.

## Skenario flow (E3 — NetFlow v5/v9/IPFIX)

`tools/loadtest.py --flow N --flow-devices M`:

1. DB sementara; M perangkat MikroTik sintetis; N paket NetFlow v5 tiruan
   dikirim lewat `FlowManager` (bukan soket) — mengukur throughput penerima,
   flush bucket 5 menit, dan ukuran DB.
2. Hasil pengukuran di mesin uji (3,8 GB RAM):

| Pengukuran | Nilai |
| --- | --- |
| 20.000 paket x 20 perangkat (v5) | ~5.476 paket/s total · 20.000 flow · 0 dibuang |
| Bucket 5 menit terisi | 140 baris (top-N memotong ke 100/kunci) |
| Ukuran DB setelah 20.000 flow | ~2,5 MB |
| RSS sebelum/sesudah | 33,6 MB → 38,8 MB |

3. Catatan: throughput terukur adalah batas atas internal (tanpa soket UDP,
   tanpa jitter jaringan). Penerima nyata dibatasi laju 200 paket/detik/sumber
   (pengaturan `ingest_rate_limit_per_sec`) — di atas itu paket dibuang dan
   dihitung.

## Skenario metrik /metrics (E4 — Prometheus)

Pengukuran di mesin uji (3,8 GB RAM), 20 perangkat MikroTik sintetis,
satu adaptor metrik DB (tanpa polling saat scrape):

| Pengukuran | Nilai |
| --- | --- |
| Ukuran payload `/metrics` | ~19 KB |
| Scrape ter-cache (repeat calls) | p50 0,002 ms · p95 0,003 ms |
| Fresh build (cache di-reset 30x) | p50 11,9 ms · p95 51,3 ms · max 77,0 ms |

Batas aman: /metrics di-cache 10 detik; scrape berulang dalam rentang itu
biaya ~0. Scrape berkala setiap 15–30 detik (default umum Prometheus)
nyaris gratis; build penuh ~12–50 ms cukup untuk 100+ perangkat pada
periode scrape wajar.

## Hasil (sebelum → sesudah perbaikan yang diizinkan)

| Pengukuran | Sebelum | Sesudah | Keterangan |
| --- | --- | --- | --- |
| laporan armada 50 perangkat | 4757 ms | **83 ms** | N+1 di `query_mt_devices` dihilangkan (enrich bulk) |
| laporan armada 200 perangkat | — | ~344 ms | + indeks `idx_if_stats_device_name_id` (443→344) |
| `/api/summary` 50 perangkat | 7 ms | 7 ms | — |
| `/api/devices` 50 perangkat | ~20 ms | ~20 ms | — |
| `/api/topology` 200 perangkat | 5,6 ms | 3,5 ms | — |

## Tabel hasil per skala (setelah perbaikan)

### Skala 50 perangkat (tick 5 menit)

| Metric | Nilai |
| --- | --- |
| Baris device_checks / metrics / interface_stats / mt_metric_history | 14.400 / 14.400 / 109.440 / 40.320 |
| Perkiraan pertumbuhan DB per hari @ poll 60 dtk | ~120 MB |
| RSS setelah fill / bench (MB) | ~26 / ~35 |
| Error `database is locked` | 0 |
| Estimasi siklus poll (8 worker) | 18,8 s dari 60 s (31%) |
| p95 `/api/summary` / `/api/devices` / `topology` / `problems` / `mikrotik/summary` / laporan armada | 10 / 21 / 3 / 2 / 2 / **84 ms** |

### Skala 200 perangkat (tick 5 menit)

| Metric | Nilai |
| --- | --- |
| Baris device_checks / metrics / interface_stats / mt_metric_history | 57.600 / 57.600 / 437.760 / 161.280 |
| Perkiraan pertumbuhan DB per hari @ poll 60 dtk | ~540 MB (~16 GB/bulan) |
| RSS setelah fill / bench (MB) | ~26 / ~58 |
| Error `database is locked` | 0 |
| Estimasi siklus poll (8 worker) | 76,7 s dari 60 s (**128% interval**) |
| p95 `/api/summary` | 50 ms |
| p95 `/api/devices` (p1 / halaman terakhir) | ~117 / ~145 ms |
| p95 `/api/topology` / `/api/problems` / `/api/mikrotik/summary` | 4 / 2 / 2 ms |
| p95 laporan armada | **~344 ms** (sedikit di atas target 300 ms; 443 → 344 ms setelah indeks) |

### Skala 500 perangkat (tick 5 menit)

| Metric | Nilai |
| --- | --- |
| Baris device_checks / metrics / interface_stats / mt_metric_history | 144.000 / 144.000 / 1.094.400 / 403.200 |
| Perkiraan pertumbuhan DB per hari @ poll 60 dtk | ~1,36 GB (~41 GB/bulan) |
| RSS setelah fill / bench (MB) | ~53 / ~67 |
| Error `database is locked` | 0 |
| Estimasi siklus poll (8 worker) | 183 s dari 60 s (**305% interval**) |
| p95 `/api/summary` | 103 ms |
| p95 `/api/devices` (p1 / halaman terakhir) | 274 / 277 ms |
| p95 `/api/topology` / `/api/problems` / `/api/mikrotik/summary` | 8 / 2 / 2 ms |
| p95 laporan armada | **~1121 ms** (target 300 ms tidak tercapai) |

### Skala 1000 perangkat (tick 15 menit; RAM 3,8 GB — batas praktik)

| Metric | Nilai |
| --- | --- |
| Baris device_checks / metrics / interface_stats / mt_metric_history | 96.000 / 96.000 / 729.600 / 268.800 |
| Perkiraan pertumbuhan DB per hari @ poll 60 dtk | ~2,7 GB (~81 GB/bulan) |
| RSS setelah fill / bench (MB) | ~55 / ~67 |
| Error `database is locked` | 0 |
| Estimasi siklus poll (8 worker) | 370 s dari 60 s (**616% interval**) |
| p95 `/api/summary` | 70 ms |
| p95 `/api/devices` (p1 / halaman terakhir) | 189 / 191 ms |
| p95 `/api/topology` / `/api/problems` / `/api/mikrotik/summary` | 10 / 2 / 3 ms |
| p95 laporan armada | **~1229 ms** (target 300 ms tidak tercapai) |

Angka untuk skala 1000 diukur dengan repetisi 10 (bukan 15); granularitas lebih
rendah dari skenario lain (lihat Metodologi), jadi absolut denahnya lebih ringan
daripada skenario 5 menit.

## Batas aman yang direkomendasikan

- **Sampai ~200 perangkat @ interval 60 detik**: p95 endpoint < 300 ms;
  hanya laporan armada (~344 ms, dibuka on-demand admin) sedikit di atas target.
- **Sampai ~500 perangkat**: `/api/devices` nyaris batas (274–277 ms); laporan
  armada > 1,1 s. Perpanjang `poll_interval` perangkat MikroTik (mis. 300 detik)
  dan pembatasan modul API pada perangkat tidak kritis.
- **1000 perangkat**: di luar kemampuan mesin uji 3,8 GB RAM (dan di atas target
  interval secara desain). Butuh perangkat keras lebih besar atau pindah ke
  PostgreSQL/TimescaleDB + poller terpisah.

## Bottleneck yang tersisa

1. **Durasi poll MikroTik** — didominasi panggilan API RouterOS per modul
   (≈4,2–4,3 s per perangkat tiruan, satu sesi, BUKAN masalah SQLite).
   Perilaku ini tidak boleh diubah tanpa persetujuan (perilaku aplikasi).
2. **Laporan armada** — 84 ms @ 50, ~344 ms @ 200, ~1121 ms @ 500,
   ~1229 ms @ 1000 — biaya `MAX(id)` + GROUP BY pada `interface_stats` yang
   dibiarkan identik demi semantik. Kandidat: cache ringkasan 5 detik (perlu
   izin; mengaburkan pengukuran) atau mengganti sumber ke `mt_interfaces`.
3. **Pertumbuhan DB** — lihat tabel; retensi bawaan 7 hari (metrik mentah)
   menekan ukuran; pastikan job `purge-old-data` (02:35) dan
   `db-backup-daily` (02:45) aktif.

## Kapan perlu pindah ke PostgreSQL/TimescaleDB / poller terpisah

- Jumlah perangkat > **±500** dengan interval 60 detik (poll tidak mengejar
  interval walau `poll_interval` diperpanjang).
- Pertumbuhan DB > **±1,5 GB/hari** setelah retensi (skenario 500–1000).
- `database is locked` berulang walau WAL + `busy_timeout` 5 s aktif.
- Kebutuhan HA/aktif-pasif atau analitik historis > 90 hari (TimescaleDB jauh
  lebih cocok daripada SQLite untuk roll-up LTTB dll.).
- Poller tidak sanggup: pindah ke instance terpisah antara penulis (poller)
  dan pembaca (web), atau PostgreSQL dengan partitioning.

## Target yang tidak tercapai dan alasannya

- **Siklus poll < 80% interval pada skala besar**: tidak tercapai sejak skala
  200. Bottleneck = biaya API MikroTik per perangkat (4,2–4,3 s tiruan), bukan
  basis data; hanya bisa dipenuhi dengan interval lebih panjang atau lebih
  sedikit perangkat MikroTik per instance.
- **Laporan armada p95 < 300 ms**: hanya tercapai di skala ≤ 50 perangkat
  (84 ms). Skala ≥ 200: 344 ms – 1229 ms; lihat Bottleneck.
- **`/api/devices` p95 < 300 ms @ 500**: 274–277 ms — nyaris batas.
- **Skala 1000 di mesin uji 3,8 GB RAM**: fill 5 menit terlalu berat;
  diselesaikan dengan tick 15 menit; angka absolut lebih ringan daripada
  skenario 5 menit. Batas 1000 perangkat memerlukan perangkat keras lebih
  besar.
