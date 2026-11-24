# Design: Pilot Nyata + Prediksi Degradasi Fiber + Paket Bukti Penilaian

**Tanggal:** 2026-11-24
**Status:** menunggu persetujuan
**Branch dasar:** `main`
**Tenggat penilaian:** 2026-12-22

## 1. Intent & Why

NMS sudah kuat secara teknis (444 tes, RBAC + audit, MFA, eskalasi, syslog/trap,
NetFlow/IPFIX, Prometheus, fiber DDM), tetapi belum dipakai Tim IT Kopegtel —
masih berjalan di laptop pengembang. Penilaian magang 22 Desember 2026 menuntut
dua hal sekaligus: kebaruan teknis di mata dosen dan bukti manfaat nyata di mata
Tim IT.

Strategi: **pilot nyata selama jam kerja + satu fitur pembeda berbasis data
nyata + paket bukti terukur**. Tidak ada demo dengan data sintetis; semua yang
ditunjukkan berasal dari pilot.

## 2. Goals

- **G1 Pilot**: 10–30 perangkat Kopegtel terpantau minimal 10 hari kerja, alarm
  Telegram aktif ke Tim IT.
- **G2 Fitur**: prediksi waktu-ke-ambang Rx fiber per link/ujung (ETA hari),
  peringkat link berisiko, problem proaktif `fiber_predicted`, proyeksi di
  grafik.
- **G3 Bukti**: laporan dampak berisi angka nyata (insiden terdeteksi, waktu
  deteksi, alarm berguna, ETA), logbook Tim IT, dan batasan yang jujur.
- **G4 Paket penilaian**: alur presentasi 10 menit + README/handover, memakai
  data pilot.
- **G5** Semua tes lama (444) + tes baru hijau; tidak ada regresi.

## 3. Non-goals

- Tidak ada data sintetis di presentasi/demo.
- Tidak ada ML/black box; hanya regresi linier + guardrail.
- Tidak mengubah arsitektur (PostgreSQL, poller terpisah, HA).
- Tidak ada auto-remediasi/tulis perangkat.
- Tidak mengganti mekanisme alert/eskalasi.
- Tidak menambah halaman besar baru; panel di halaman fiber yang ada.

## 4. Anti-goals

- Fitur yang tidak dipakai/divalidasi Tim IT.
- Angka dampak yang tidak bisa dipertanggungjawabkan (mis. klaim 24/7 padahal
  jam kerja).
- Problem storm dari prediksi.
- Demo yang mengandalkan data palsu.

## 5. Constraints

- 28 hari (24 Nov–22 Des); fitur freeze 15 Des.
- Laptop hanya menyala saat di kantor (08.00–17.00); malam/weekend tidak ada
  data. Semua klaim dihitung dari jendela teramati.
- Akses baca API MikroTik/SNMP ke 1–2 perangkat ber-SFP sudah dikonfirmasi
  bisa; perangkat lain ping/SNMP.
- Satu pengembang + agent; data uji hanya di `tests/`.
- Keamanan: user MikroTik read-only dibatasi IP NMS; `ALLOWED_NETWORKS`;
  kredensial terenkripsi.

## 6. Kondisi Saat Ini (Recon)

- `optical_readings` + `optical_hourly` menyimpan DDM; baseline median 7 hari,
  tren regresi 14 hari (`backend/fiber.py`).
- Problem fiber: `fiber_los`, `fiber_rx_low`, `fiber_rx_high`,
  `fiber_margin_low`, `fiber_asymmetry`, `fiber_drop`, `fiber_trend`,
  `fiber_deviation`; evaluasi tiap 60 detik dengan 2 evaluasi berturut +
  histeresis (`backend/poller.py`).
- `fiber_trend` sudah memicu problem saat slope ≤ −0,5 dB/minggu.
- Cache status di `optical_link_state`; ringkasan di `/api/optical/summary`.
- UI fiber: Ringkasan, Port optik, Link & redaman, Pengukuran, Kalkulator,
  Pengaturan.
- Visibilitas poller & alasan masalah sudah ada (PR #9).
- **Belum ada**: prediksi kapan ambang akan terlewati, peringkat risiko,
  proyeksi grafik, problem proaktif sebelum ambang.

## 7. Approaches

**A. Pilot saja** — dampak kuat, kebaruan kurang.
**B. Fitur saja** — demo kuat, "dipakai siapa?" rapuh.
**C. Paket penilaian saja** — aman, nilai unggul rendah.
**D. Hybrid pilot + fitur + bukti (dipilih)** — satu fitur dibangun sambil pilot
berjalan; bukti dikumpulkan otomatis; laporan memakai data nyata.

Fitur pembeda yang dipertimbangkan:

- **Prediksi degradasi fiber (dipilih)**: relevan dengan bisnis Kopegtel, data
  DDM tersedia, bisa dijelaskan jujur, dan melengkapi `fiber_trend` yang sudah
  ada.
- **Wallboard NOC**: cepat & terlihat, tetapi kurang dalam secara teknis;
  cadangan jika akses DDM batal.
- **Config drift dari backup MikroTik**: berguna, tetapi kurang visual dan
  butuh backup rutin.
- **Anomali traffic**: butuh baseline panjang; risiko klaim berlebihan.

Sub-pendekatan prediksi:

- **On-demand di API**: murah dikembangkan, tetapi alert tetap butuh jalur
  poller dan beban evaluasi berulang.
- **Di poller + cache (dipilih)**: memakai jadwal 60 detik yang ada, satu jalur
  untuk problem & API, hasil disimpan di `optical_link_state`.
- **ML**: ditolak; data 2–3 minggu tidak cukup untuk klaim model.

## 8. Desain Rinci

### 8.1 Pilot

- **Perangkat**: 1–2 MikroTik dengan SFP (API/SNMP read-only) + 10–30
  perangkat pilihan Tim IT (ping/SNMP).
- **Kanal**: grup Telegram Tim IT; uji kirim; aturan aksi default untuk
  severity warning/high.
- **Fiber**: daftarkan link nyata; isi profil ambang sesuai datasheet SFP,
  panjang, splice, konektor.
- **Operasi**: nyalakan 08.00, matikan 17.00; cek `/healthz`; catat jam
  operasi.
- **Logbook Tim IT**: tanggal, alarm, valid/tidak, tindakan, catatan (manual,
  dilampirkan di laporan).
- **Alat bukti**: `tools/pilot_report.py` (baru) → Markdown/CSV: jendela
  observasi, perangkat, total cek, insiden per kind, waktu deteksi (pemeriksaan
  buruk pertama → `action_log` alert pertama), prediksi fiber & hasil, uptime
  dalam jendela, daftar alarm.

### 8.2 Prediksi Fiber

**Setting baru** (default, rentang):

- `fiber_predict_horizon_days` = 14 (1–90)
- `fiber_predict_min_slope_db_per_week` = 0.1 (0.01–5)

**Fungsi murni** `link_eval.predict_crossing(rx_now, slope_per_week, warn_low,
crit_low, horizon_days, min_slope)`:

- Guard: `warn_low` ada; `rx_now` ada dan > `warn_low` (belum melanggar);
  `slope_per_week <= -min_slope`.
- `days = (rx_now - warn_low) / (-slope_per_week / 7)`; valid bila
  `0 < days <= horizon`.
- Hitung juga `days_to_crit` bila `crit_low` ada dan `rx_now > crit_low`.
- Kembalikan `{days_to_warn, days_to_crit, target: "warn"|"crit",
  threshold_dbm}` atau `None`.

**Integrasi `evaluate_link`**: per ujung A/B, tambah issue `fiber_predicted`:

- severity `warning` bila `days_to_warn <= 7`, selain itu `average`.
- pesan: "Link X: Rx ujung A diprediksi menyentuh ambang waspada (−15,00 dBm)
  dalam ~9 hari (Rx kini −11,30 dBm, tren −0,42 dB/minggu)".
- `device_side` diisi agar problem tercatat di perangkat yang tepat.

**Poller**: tambah `fiber_predicted` ke `FIBER_PROBLEM_KINDS` dan
`_FIBER_EVAL_KINDS`; dedupe/histeresis/alerts yang ada dipakai apa adanya.
`open_problem` hanya memperbarui pesan/severity tanpa alert ulang saat ETA
berubah.

**Cache & API**:

- Tambah field prediksi ke `LINK_STATE_METRICS_KEYS`: `predicted_days_a/b`,
  `predicted_target_a/b`, `predicted_threshold_a/b`, `predicted_slope_a/b`.
- `/api/optical/summary`: `links_at_risk`, `at_risk_links` (top 5: nama, side,
  rx, threshold, slope, days).
- `/api/fiber-links/<id>`: `predictions` per ujung.

**UI**:

- Ringkasan: kartu "Link berisiko" + panel "Prediksi degradasi" (5 baris,
  tautan ke detail).
- Detail link: chip "Prediksi: ~9 hari ke ambang waspada" + garis proyeksi
  putus-putus di grafik.
- Pengaturan fiber: 2 input baru.
- Label "estimasi linier dari tren 14 hari; bukan jaminan".
- `KIND_LABELS` di `reports.js`: `fiber_predicted` → "Prediksi Rx melewati
  ambang".

**Tes**: unit fungsi murni (data kurang, slope positif, sudah di bawah ambang,
batas horizon, crit), integrasi poller buka/tutup, API summary/detail, validasi
setting. Data sintetis hanya di `tests/`, bukan demo.

### 8.3 Paket Bukti & Penilaian

- `docs/LAPORAN-DAMPAK.md`: template + angka final (diisi 15–21 Des): konteks,
  metode, cakupan jam kerja, jumlah perangkat/cek, insiden, waktu deteksi,
  alarm berguna, hasil prediksi fiber, batasan.
- `docs/ALUR-PRESENTASI.md`: 10 menit memakai data pilot (dashboard → masalah →
  fiber → angka → batasan).
- README: bagian "Pilot & dampak" + tautan.
- Handover: akun viewer/operator, runbook start/stop, backup, Telegram.

## 9. Linimasa

- **24–30 Nov**: TDD prediksi + API; mulai pilot (perangkat, Telegram, fiber
  link).
- **1–7 Des**: UI ringkasan/detail/pengaturan; `pilot_report.py`; data
  terkumpul; umpan balik Tim IT #1.
- **8–14 Des**: perbaikan dari pilot; suite penuh; draf laporan dampak.
- **15–21 Des**: freeze fitur; angka final; latihan presentasi; serah terima.
- **22 Des**: penilaian.

## 10. Kriteria Sukses

- **Pilot**: ≥10 perangkat, ≥10 hari kerja, ≥1 insiden nyata terdeteksi, Tim IT
  mengonfirmasi ≥1 alarm berguna.
- **Fitur**: prediksi muncul dari data nyata; minimal 1 link dengan ETA; tidak
  ada problem storm; semua tes hijau.
- **Bukti**: laporan memuat angka + batasan; presentasi 10 menit siap; tidak ada
  data sintetis.
- Jika prediksi belum terbukti pada data nyata, laporan menyatakan "belum
  terverifikasi" secara jujur (pola yang sudah dipakai proyek ini).

## 11. Risiko & Mitigasi

- **Akses DDM berubah** → ganti pembeda ke wallboard NOC sebelum 1 Des; tanpa
  demo palsu.
- **Data malam kosong** → hitung per jendela teramati; tulis batasan.
- **Sedikit insiden** → ukur degradasi/early warning; logbook Tim IT.
- **ETA tidak akurat** → guardrail, label estimasi, evaluasi; turunkan klaim
  bila perlu.
- **Problem storm** → severity average/warning, horizon, min slope, dedupe; uji.
- **Waktu habis** → fitur freeze 15 Des; pilot + bukti diprioritaskan.

## 12. Keputusan yang Diambil (Builder Calls)

- Kind baru `fiber_predicted`, bukan memperluas `fiber_trend`.
- Prediksi di poller + cache, bukan on-demand.
- Regresi linier + guardrail; tanpa ML.
- CLI `tools/pilot_report.py`, bukan halaman UI baru.
- Label estimasi eksplisit; tidak ada klaim akurasi.

## 13. Yang Diserahkan ke Builder

- Implementasi fungsi & integrasi (nama final, struktur response, markup).
- Ambang default dan format pesan (boleh disesuaikan saat implementasi).
- Tata letak panel/grafik.
- Daftar perangkat & kontak Tim IT (operasional, dikonfirmasi saat pilot).

## 14. Dependensi

- Tim IT: akses read-only 1–2 perangkat SFP, daftar 10–30 perangkat, grup
  Telegram.
- Jadwal di kantor untuk menyalakan laptop.
- Data DDM minimal 24 jam per jam sebelum ETA bisa dihitung (target: aktif
  26 Nov).
