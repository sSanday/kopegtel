# Alur Pengembangan NMS Kopegtel sampai 22 Desember 2026

> **Untuk pelaksana:** rencana ini dipakai bersama
> `docs/superpowers/specs/2026-11-24-pilot-fiber-prediction-design.md`.
> Setiap hari punya satu fokus, satu output, dan satu definisi selesai.
> Jangan menambah fitur baru setelah **15 Desember** (feature freeze).

**Goal:** NMS Kopegtel siap dinilai pada 22 Desember 2026: berjalan dengan data
nyata, punya satu fitur pembeda yang terbukti, semua tes hijau, dan bukti
dampak yang jujur.

**Architecture:** Pilot nyata di jaringan Kopegtel selama jam kerja + fitur
prediksi degradasi fiber di poller yang sudah ada + alat bukti
`tools/pilot_report.py`. Tidak ada data sintetis di runtime/demo.

**Tech Stack:** Python/Flask, SQLite, APScheduler, MikroTik API/SNMP, vanilla
JS, pytest.

**Spec:**
`docs/superpowers/specs/2026-11-24-pilot-fiber-prediction-design.md`

## Global Constraints

- Tidak ada data sintetis di runtime, pilot, atau demo; data uji hanya di `tests/`.
- TDD: tes gagal dulu, baru implementasi; suite penuh hijau sebelum merge.
- Tidak mengubah perilaku endpoint lama; fitur baru memakai endpoint/field baru.
- Tidak ada klaim 24/7; semua angka dihitung dari jendela teramati.
- Feature freeze 15 Desember 2026; setelah itu hanya perbaikan bug dan dokumen.
- Rahasia tidak boleh masuk log; RBAC + audit tetap berlaku.
- Setiap perubahan backend: restart server, cek `/healthz`, cek log.

## Review Focus

Lima hal yang paling mungkin merusak hasil di tangan pengguna, dan tes yang
menguncinya:

1. **Laptop hanya hidup di jam kerja** → rollup per jam + catch-up startup;
   tanpa ini tren/baseline/grafik kosong. Tes: rollup dua kali idempoten,
   grafik melihat jam terbaru.
2. **Skala severity proyek terbalik** (`average` > `warning`) → prediksi dekat
   harus lebih berat, bukan lebih ringan. Tes: ETA ≤7 hari = `average`, >7 hari
   = `warning`; target crit = `high`.
3. **ETA berayun di batas horizon** → problem buka/tutup berulang. Tes: margin
   tutup menahan flapping.
4. **Data parsial diklaim sebagai 24/7** → laporan harus memakai jendela
   observasi. Tes: `pilot_report.py` menghitung jam teramati, bukan 24 jam.
5. **Riwayat prediksi hilang** → tidak ada bukti lead time. Tes: snapshot per
   jam tercatat dan bisa dibaca ulang.

## 1. Definisi "Selesai" 22 Desember

"SEMpurna" yang bisa dipertanggungjawabkan bukan berarti tanpa batas, tetapi:

- **Tidak ada bug P0/P1** yang diketahui; semua tes lama + baru hijau.
- **Pilot nyata**: ≥10 perangkat, ≥10 hari kerja, alarm Telegram sampai ke Tim
  IT, ada minimal 1 insiden nyata yang terdeteksi.
- **Fitur pembeda**: prediksi ETA ambang Rx fiber berjalan dari data DDM nyata;
  ada riwayat prediksi; tidak ada problem storm.
- **Bukti**: `docs/LAPORAN-DAMPAK.md` berisi angka nyata + batasan; tidak ada
  klaim 24/7 atau data palsu.
- **Serah terima**: README, runbook start/stop, backup/restore teruji, akun
  viewer/operator, dan alur presentasi 10 menit.
- **Keamanan**: `pip-audit` bersih, tidak ada secret di log, RBAC/audit tetap
  bekerja.

Yang **tidak** dikejar: SNMPv3 penuh, jadwal on-call, HA/PostgreSQL, auto-
remediasi, atau fitur baru di luar prediksi fiber.

## 2. Kondisi Awal (24 Nov 2026)

- `main` bersih; 444 tes hijau; PR #6–#9 sudah merge.
- Desain pilot + prediksi sudah ditulis dan direview.
- **Temuan review**: `optical_hourly` tidak pernah terisi saat laptop hanya
  hidup di jam kerja (job rollup 02.35, data mentah >7 hari). Ini P0.
- Logbook kampus terisi sampai 15 Okt; tidak menjadi fokus pekerjaan.

## 3. Lima Fase

| Fase | Tanggal | Fokus | Hasil |
|---|---|---|---|
| **0. Data benar** | 25–27 Nov | Rollup optik per jam + maintenance startup | Tren/baseline/grafik hidup |
| **1. Mesin prediksi** | 30 Nov–4 Des | Fungsi prediksi, riwayat, poller, API | `fiber_predicted` dari data nyata |
| **2. UI + bukti** | 7–11 Des | Panel risiko, proyeksi grafik, `pilot_report.py` | Fitur terlihat + angka pilot |
| **3. Hardening** | 14–18 Des | Regresi, keamanan, backup, laporan, presentasi | Release candidate |
| **4. Freeze + nilai** | 21–22 Des | Latihan, serah terima, penilaian | Siap dinilai |

## 4. Alur Harian

### Fase 0 — Data Benar (25–27 Nov)

| Tanggal | Pekerjaan | Output | Selesai bila |
|---|---|---|---|
| **25 Nov (Rabu)** | TDD `db.rollup_optical_hourly()`: agregasi jam selesai, upsert **replace** (bukan tambah `samples`); tes dua kali jalan | Fungsi + tes unit | Dua kali rollup tidak menggandakan samples |
| **26 Nov (Kamis)** | Job `optical-hourly-rollup` tiap jam menit ke-5 + catch-up startup; `purge_old_data` memanggil rollup sebelum hapus; job `maintenance-startup` untuk purge cek/data + backup | Job + tes integrasi | Grafik 7d, `trend_rx`, baseline melihat jam terbaru; maintenance startup idempoten |
| **27 Nov (Jumat)** | PR + merge P0; mulai pilot: daftar 10–30 perangkat, uji Telegram, daftarkan link fiber nyata, isi profil ambang | P0 merge + pilot aktif | `/healthz` 200; Telegram uji terkirim; minimal 1 link fiber terdaftar |

**Catatan:** mulai pilot hari ini supaya data DDM terkumpul. Prediksi butuh
minimal 24 titik per jam (≈3 hari kerja) sebelum ETA pertama bisa dihitung.

### Fase 1 — Mesin Prediksi (30 Nov–4 Des)

| Tanggal | Pekerjaan | Output | Selesai bila |
|---|---|---|---|
| **30 Nov (Senin)** | TDD `link_eval.predict_crossing()`: ambang berikutnya (`warn`/`crit`), guard slope, horizon, tanpa data | Fungsi murni + tes | Semua kasus batas lulus |
| **1 Des (Selasa)** | Tabel `fiber_prediction_log` + fungsi DB + snapshot per jam; tes | Skema + query + tes | Prediksi tercatat, tidak menimpa cache |
| **2 Des (Rabu)** | Integrasi `fiber_predicted` ke `evaluate_link`/poller: severity sesuai skala proyek, dedupe, close margin | Issue + problem + tes | Buka/tutup problem stabil, tidak flapping |
| **3 Des (Kamis)** | Cache `LINK_STATE_METRICS_KEYS` + `/api/optical/summary` + `/api/fiber-links/<id>` | API + tes | Ringkasan mengembalikan top-5 link berisiko |
| **4 Des (Jumat)** | Panel Ringkasan "Link berisiko" + 3 input pengaturan + label estimasi | UI + verifikasi data nyata | Panel tampil dari data pilot, bukan data uji |

### Fase 2 — UI + Bukti (7–11 Des)

| Tanggal | Pekerjaan | Output | Selesai bila |
|---|---|---|---|
| **7 Des (Senin)** | Detail link: chip prediksi + garis proyeksi putus-putus di grafik; empty state | UI detail | Proyeksi terlihat dan labelnya jelas |
| **8 Des (Selasa)** | `tools/pilot_report.py`: jendela observasi, insiden, latensi alert, MTTR, riwayat prediksi | CLI + tes | Laporan Markdown/CSV keluar dari DB pilot |
| **9 Des (Rabu)** | Jalankan laporan pada data pilot; verifikasi angka; umpan balik Tim IT #1 | Angka pilot + catatan | Tim IT mengonfirmasi ≥1 alarm berguna |
| **10 Des (Kamis)** | Polish: mobile, a11y, error/loading state, konsistensi istilah | Perbaikan UI | Tidak ada halaman rusak di 360px/1280px |
| **11 Des (Jumat)** | Suite penuh + `pip-audit` + backup/restore drill; perbaiki regresi | Hasil verifikasi | Semua tes hijau; audit bersih; restore terbukti |

### Fase 3 — Hardening + Paket Penilaian (14–18 Des)

| Tanggal | Pekerjaan | Output | Selesai bila |
|---|---|---|---|
| **14 Des (Senin)** | Buffer: perbaiki temuan pilot; ulang tes; cek performa 30 perangkat | Perbaikan + angka performa | Tidak ada P0/P1 tersisa |
| **15 Des (Selasa)** | **Feature freeze**; snapshot data pilot; tag release candidate | Tag + arsip data | Tidak ada commit fitur setelah ini |
| **16 Des (Rabu)** | Draf `docs/LAPORAN-DAMPAK.md`: konteks, metode, cakupan jam kerja, insiden, deteksi, alarm berguna, prediksi | Draf laporan | Semua angka bisa dilacak ke DB/logbook |
| **17 Des (Kamis)** | Finalisasi laporan + batasan + README/handover | Laporan final | Tidak ada klaim tanpa bukti |
| **18 Des (Jumat)** | `docs/ALUR-PRESENTASI.md` 10 menit + latihan dengan data pilot | Alur + slide | Latihan selesai, timing ≤10 menit |

### Fase 4 — Freeze + Penilaian (21–22 Des)

| Tanggal | Pekerjaan | Output | Selesai bila |
|---|---|---|---|
| **21 Des (Senin)** | Latihan final, serah terima akun/runbook, backup, checklist penilaian | Checklist + backup | Semua item checklist tercentang |
| **22 Des (Selasa)** | Penilaian: presentasi + demo data pilot | — | Selesai |

## 5. Rutinitas Pilot Harian (25 Nov–21 Des)

- **08.00** — nyalakan server; cek `/healthz` 200 dan `/api/poller/status`.
- **08.05** — cek masalah terbuka, link fiber, panel prediksi; uji Telegram
  sekali pada hari pertama.
- **Sepanjang hari** — kerja pengembangan; setiap alarm yang masuk dicatat di
  logbook Tim IT (valid/tidak, tindakan).
- **16.45** — snapshot/backup; matikan server dengan rapi.
- **Jumat** — rekap mingguan: insiden, alarm berguna, kualitas prediksi,
  penyesuaian ambang.

## 6. Prioritas Backlog

**P0 — wajib, tidak boleh dipotong**
- Rollup optik per jam + maintenance startup.
- Prediksi `fiber_predicted` + riwayat + API.
- Pilot nyata + alarm Telegram.
- Tes hijau + laporan dampak + presentasi.

**P1 — penting, boleh disederhanakan**
- Panel ringkasan link berisiko.
- Proyeksi di grafik detail.
- `pilot_report.py` (Markdown dulu, CSV menyusul).
- Handover/runbook.

**P2 — hanya jika waktu tersisa**
- Wallboard NOC.
- Ekspor PDF laporan.
- Chart prediksi multi-link.

**Tidak dikerjakan**
- ML, SNMPv3 penuh, on-call, HA, auto-remediasi.
- Data sintetis di runtime/demo.
- Klaim 24/7.

## 7. Gerbang Kualitas (setiap merge)

1. Tes baru ditulis lebih dulu dan gagal sebelum implementasi.
2. `pytest` penuh hijau (bukan hanya tes baru).
3. Tidak ada data uji masuk DB produksi.
4. `pip-audit` bersih.
5. `/healthz` 200; halaman utama + fiber 200.
6. PR deskriptif; merge setelah hijau.
7. Setiap perubahan backend: restart server + cek log.

## 8. Rencana Pemotongan jika Tertinggal

Urutan potong dari yang paling aman:

1. Garis proyeksi di grafik (chip + panel tetap ada).
2. Input pengaturan UI (pakai default + dokumentasi).
3. Ekspor CSV (Markdown tetap ada).
4. Panel top-5 (satu daftar sederhana).
5. Wallboard/P2.

Yang **tidak boleh** dipotong: P0, prediksi inti, pilot, bukti, tes, laporan.

## 9. Risiko

| Risiko | Mitigasi |
|---|---|
| Akses DDM berubah | Pilot tetap jalan untuk ping/SNMP; prediksi ditandai "belum terverifikasi" |
| Data malam kosong | Laporan memakai jendela teramati; tidak klaim 24/7 |
| ETA tidak akurat | Label estimasi; laporan menyebut hasil apa adanya |
| Waktu habis | Cut list di atas; freeze 15 Des |
| Problem storm | Severity sesuai skala, horizon, close margin, dedupe, tes |
| Server mati saat ditinggal | Runbook start/stop; backup harian saat startup |

## 10. Setelah 22 Des

- Serahkan akun, runbook, dan backup ke Tim IT.
- Simpan data pilot + laporan sebagai arsip.
- Catat batasan dan pekerjaan lanjutan (SNMPv3, on-call, HA) di `docs/TEMUAN.md`.
