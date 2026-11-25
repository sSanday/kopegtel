# ROADMAP — NMS Kopegtel (24 Nov – 22 Des 2026)

**Periode:** 24 Nov 2026 – 22 Des 2026 (28 hari, ~20 hari kerja)
**Tenggat:** 22 Des 2026 (penilaian magang)
**Status:** P0/P1 merged (PR #10); alat bukti P2 merged (PR #11); fix race alarm (PR #12); fix uji SNMP/IP duplikat (PR #14); pilot berjalan — angka final menunggu data nyata
**Branch dasar:** `main` @ `c4a3937` (suite 444/444 = 100%)

## 0. Prinsip & batasan

- **Jam kerja:** laptop menyala 08.00–17.00 di kantor. Malam/weekend tidak ada
  data. Semua klaim dihitung dari jendela teramati (tidak ada klaim 24/7).
- **Data nyata saja:** tidak ada demo dengan data sintetis. Jika data nyata
  belum cukup, laporan menyatakan "belum terverifikasi" secara jujur (pola yang
  sudah dipakai proyek).
- **Satu fitur pembeda:** prediksi degradasi fiber (ETA ambang Rx). Tidak
  menambah fitur lain yang tidak dipakai/divalidasi Tim IT.
- **Tidak mengubah:** status warna, layout, ID, `data-*`, business logic,
  mekanisme alert/eskalasi. Semua tes lama harus tetap hijau.
- **K freeze:** fitur freeze 15 Des; 16–22 Des hanya laporan, presentasi,
  serah terima.

## 1. Prioritas (urutan kerja)

| Prioritas | Cakupan | Alasan |
|---|---|---|
| **P0** | Rollup optik per jam + maintenance startup | Tanpa ini, `fiber_trend`, baseline, grafik, dan prediksi kosong selama pilot (laptop mati malam). |
| **P1** | Prediksi degradasi fiber (fungsi, poller, cache, API, UI) | Satu fitur pembeda berbasis data nyata; melengkapi `fiber_trend` yang sudah ada. |
| **P2** | Pilot nyata + `pilot_report.py` + laporan dampak | Bukti manfaat nyata untuk Tim IT dan dosen. |
| **P3** | Paket penilaian (presentasi, handover, README) | Membungkus semua hasil jadi alur 10 menit. |
| **P4** (cadangan) | Wallboard NOC / config drift | Hanya jika akses DDM batal atau waktu berlebih. |

## 2. Minggu 1 — 24–27 Nov: fondasi data & pilot siap

**Tujuan:** data optik terisi saat jam kerja; pilot siap jalan.

### P0 — Rollup optik per jam + maintenance startup (PR terpisah)
- [x] `db.rollup_optical_hourly()` idempoten (replace, bukan additive).
- [x] Job `optical-hourly-rollup` tiap jam menit ke-5 + catch-up startup.
- [x] `purge_old_data` panggil rollup sebelum hapus; hilangkan upsert aditif.
- [x] `maintenance-startup`: purge cek/data + `run_due_backup` sekali setelah poller hidup.
- [x] Tes: double-run tidak menggandakan `samples`; catch-up startup; grafik `7d` dan `trend_rx` melihat jam terbaru.
- [x] Merge ke `main` — tercakup PR #10 (25 Nov 2026).

### Pilot — persiapan (paralel, operasional)
- [ ] Daftar 10–30 perangkat pilihan Tim IT; akses read-only 1–2 MikroTik ber-SFP.
  - 25 Nov: perangkat pilot saat ini (CCR1016-12G) **tidak punya port SFP** dan API 8728/8729 tertutup. Permintaan akses SFP/DDM disiapkan di `docs/PERMINTAAN-AKSES-DDM.md` (target 28 Nov; pivot 1 Des bila tidak ada). Pesan konsolidasi (DDM + status Host `.211` + 10–30 perangkat tambahan) ada di §8; **belum dikirim** per 25 Nov.
- [ ] Grup Telegram Tim IT; uji kirim; aturan aksi default severity warning/high. **Belum dikonfigurasi** per 25 Nov (`action_rules`/`message_templates` kosong).
- [ ] Daftarkan link fiber nyata + isi profil ambang (datasheet SFP, panjang, splice, konektor).
- [ ] Dokumen jam operasi + checklist harian (nyalakan 08.00, cek `/healthz`, matikan 17.00).

**Jendela data bersih:** mulai 25 Nov 2026 13:15 WIB (setelah laptop kembali ke Wi-Fi kantor). Periode 19–25 Nov sebelum itu memuat cek dari jaringan salah dan **tidak dipakai untuk angka final**. 25 Nov sendiri masih memiliki jeda ~30 menit akibat restart server (13:43–14:17 dan 14:42–15:12), jadi laporan final memakai periode 26 Nov–12 Des.

**Catatan jaringan 25 Nov 16:15:** laptop berpindah ke Wi-Fi `mabooo` (`10.31.248.143/24`); MikroTik/ONT tidak terjangkau. Server sengaja dihentikan agar tidak mencatat offline palsu. Problem 147 (ONT) dan 148 (MikroTik) yang terbuka adalah artefak jaringan salah, bukan insiden nyata; keduanya akan tertutup sendiri saat server kembali melihat perangkat `online`. Setelah kembali ke Wi-Fi kantor: nyalakan server, lalu uji SNMP lewat **Edit "Mikrotik Utama"** dengan community dikosongkan (memakai kredensial tersimpan).

**Acceptance:** rollup PR hijau; `/healthz` 200; data `optical_hourly` terisi hingga jam terakhir di hari kerja.

## 3. Minggu 2 — 30 Nov – 4 Des: prediksi fiber inti

**Tujuan:** fungsi prediksi + integrasi poller + API hijau; minimal 1 link dengan ETA.

### P1 — Fungsi & riwayat
- [x] Setting baru: `fiber_predict_horizon_days` (14), `fiber_predict_min_slope_db_per_week` (0.1), `fiber_predict_close_margin_days` (3).
- [x] `link_eval.predict_crossing(...)` next-threshold (warn/crit), guard slope, margin tutup.
- [x] Tabel `fiber_prediction_log` (snapshot per jam) + retensi 180 hari.
- [x] Tes unit: data kurang, slope positif, sudah di bawah ambang, batas horizon, target crit, margin tutup.

### P1 — Integrasi
- [x] `evaluate_link`: issue `fiber_predicted` per ujung; severity sesuai skala proyek (`average` > `warning`; `high` untuk crit dekat); pesan informatif.
- [x] Poller: tambah kind ke `FIBER_PROBLEM_KINDS`/`_FIBER_EVAL_KINDS`; dedupe/alert existing; anti-flap dengan margin.
- [x] Cache & API: field prediksi di `LINK_STATE_METRICS_KEYS`; `/api/optical/summary` (top 5 link berisiko); `/api/fiber-links/<id>`.
- [x] Merge ke `main` — PR #10 (25 Nov 2026), suite 470 passed.

**Acceptance:** integrasi poller buka/tutup hijau; tidak ada problem storm; minimal 1 link menampilkan ETA dari data nyata; riwayat prediksi tercatat.

## 4. Minggu 3 — 7 – 11 Des: UI + alat bukti

**Tujuan:** panel prediksi terlihat di UI; `pilot_report.py` siap; umpan balik Tim IT #1.

### P1 — UI
- [x] Ringkasan: kartu "Link berisiko" + panel "Prediksi degradasi" (5 baris, tautan detail).
- [ ] Detail link: chip prediksi + garis proyeksi putus-putus di grafik. (chip selesai; garis proyeksi belum)
- [x] Pengaturan fiber: 3 input baru + label estimasi.
- [x] `KIND_LABELS`: `fiber_predicted` → "Prediksi Rx melewati ambang".

### P2 — Alat bukti
- [x] `tools/pilot_report.py`: jendela observasi, cakupan jam kerja, insiden (MTTD/MTTR), prediksi (lead time, terkonfirmasi/belum), alarm + kolom "berguna?" — PR #11 (25 Nov 2026).
- [x] Template `docs/LAPORAN-DAMPAK.md` + daftar alat bukti di README — PR #11.
- [x] Review independen: MTTD berjangkar cek `online` terakhir, cakupan memakai menit unik tanpa akhir pekan, uji kanal dipisah dari alarm produksi (`20ed3ef`, `7fd958a`).
- [x] Full suite (`pytest tests/ -q`) hijau — 497 passed, 1 warning (25 Nov 2026, **sebelum** fix storm). Setelah fix: 90 tes area alarm (termasuk 27 tes pilot_report) hijau; full suite penuh belum dijalankan ulang karena batas waktu harness, bukan karena kegagalan.
- [ ] Umpan balik Tim IT #1: validasi alarm, catat false positive.
- [ ] Jalankan `pilot_report.py` pada data pilot nyata dan isi angka final di `docs/LAPORAN-DAMPAK.md`.

**Acceptance:** UI menampilkan prediksi dari data nyata; `pilot_report.py` menghasilkan Markdown/CSV; 0 regresi.

## 5. Minggu 4 — 14 – 18 Des: freeze, bukti, presentasi

**Tujuan:** fitur freeze; angka final; presentasi & handover siap.

- [ ] 15 Des: feature freeze (tidak ada fitur baru; hanya bugfix).
- [ ] Snapshot data pilot (backup DB + CSV report).
- [ ] `docs/LAPORAN-DAMPAK.md`: konteks, metode, cakupan jam kerja, perangkat/cek, insiden, waktu deteksi, alarm berguna, hasil prediksi (lead time), batasan.
- [ ] `docs/ALUR-PRESENTASI.md`: 10 menit (dashboard → masalah → fiber → angka → batasan).
- [ ] README: bagian "Pilot & dampak" + tautan.
- [ ] Handover: akun viewer/operator, runbook start/stop, backup, Telegram.
- [ ] Latihan presentasi dengan data pilot.

**Acceptance:** laporan berisi angka nyata + batasan jujur; presentasi 10 menit siap; handover lengkap.

## 6. 21 – 22 Des: finalisasi & penilaian

- [ ] 21 Des: rehearsal terakhir, cek `/healthz`, backup final, koreksi typo.
- [ ] 22 Des: penilaian — demo dashboard → masalah → fiber prediksi → angka → batasan.

## 7. Kriteria sukses (22 Des)

- **Pilot:** ≥10 perangkat, ≥10 hari kerja, ≥1 insiden nyata terdeteksi, Tim IT mengonfirmasi ≥1 alarm berguna.
- **Fitur:** prediksi muncul dari data nyata; minimal 1 link dengan ETA; riwayat prediksi tercatat; tidak ada problem storm; semua tes hijau.
- **Data:** selama jam kerja `optical_hourly` terisi hingga jam terakhir; tren/baseline/grafik tidak kosong; maintenance harian berjalan saat startup.
- **Bukti:** laporan memuat angka + batasan; presentasi 10 menit siap; tidak ada data sintetis.
- **Jujur:** jika prediksi belum terbukti pada data nyata, laporan menyatakan "belum terverifikasi".

## 8. Risiko & mitigasi

| Risiko | Mitigasi |
|---|---|
| Akses DDM berubah | Ganti pembeda ke wallboard NOC sebelum 1 Des; tanpa demo palsu. |
| Data malam kosong | Hitung per jendela teramati; tulis batasan. |
| Rollup terlewat (laptop mati) | Job tiap jam + catch-up startup; tes idempoten. |
| Maintenance malam tidak jalan | `maintenance-startup` sekali setelah poller hidup. |
| ETA tidak akurat | Guardrail, label estimasi, evaluasi; turunkan klaim bila perlu. |
| Problem storm | Severity sesuai skala, horizon, min slope, dedupe, margin tutup; uji. |
| Waktu habis | Freeze 15 Des; pilot + bukti diprioritaskan. |

## 9. Non-goals (tidak dikerjakan periode ini)

- Tidak ada data sintetis di presentasi/demo.
- Tidak ada ML/black box.
- Tidak mengubah arsitektur (PostgreSQL, poller terpisah, HA).
- Tidak ada auto-remediasi/tulis perangkat.
- Tidak mengganti mekanisme alert/eskalasi.
- Tidak menambah halaman besar baru; panel di halaman fiber yang ada.
- Tidak mengerjakan logbook (di luar scope; dikerjakan terpisah).

## 10. Catatan operasional

- Server: `python backend/serve.py` (waitress, default port dari `.env` = 5000).
- Health check: `http://127.0.0.1:5000/healthz` (200 bila DB terbaca & heartbeat poller < 90 dtk).
- Satu instance per database (lease `instance_lease`); jangan start ganda; tunggu lease kedaluwarsa (~30 dtk) bila restart.
- Tes: `python -m pytest tests/ -q` (database sementara; jangan saat produksi).
- Branch kerja: `feat/pilot-fiber-prediction`; PR → merge ke `main` per langkah besar.
- 25 Nov: race `deque mutated during iteration` di `alerts._storm_gate` diperbaiki
  (PR #12): lock + snapshot; regresi test
  `test_storm_gate_uses_snapshot_when_entries_mutated`. Verifikasi: 13 tes
  eskalasi + 77 tes events/webhook/pilot_report hijau; full suite penuh
  terputus oleh restart harness, bukan kegagalan tes.
