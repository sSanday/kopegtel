# Laporan Dampak Pilot NMS Kopegtel

**Status:** template — angka final diisi dari data pilot nyata sebelum 22 Desember 2026.
**Jangan menambahkan angka, grafik, atau insiden yang tidak tercatat di `backend/nms.db`.**

## Cara mengisi

1. Jalankan alat bukti dari akar repo:
   ```powershell
   .\.venv\Scripts\python.exe tools\pilot_report.py --days 7
   ```
   atau untuk periode tertentu:
   ```powershell
   .\.venv\Scripts\python.exe tools\pilot_report.py --since 2026-12-01 --until 2026-12-12 --out laporan
   ```
2. Salin keluaran Markdown ke bagian **Hasil** di bawah ini.
3. Tempel empat CSV pendukung (`cakupan-perangkat.csv`, `insiden.csv`,
   `prediksi.csv`, `alarm.csv`) sebagai lampiran.
4. Isi kolom **Berguna?** berdasarkan umpan balik Tim IT. Kosongkan bila belum
   divalidasi — jangan menebak.
5. Perbarui bagian **Batasan** bila ada kondisi pilot yang berubah.

> Alat ini membaca database secara read-only. Data sintetis hanya ada di
> `tests/` dan tidak boleh dipakai di laporan.

## 1. Konteks

- **Instansi:** Kopegtel Malang.
- **Tujuan pilot:** membuktikan NMS mendeteksi masalah jaringan nyata dan
  membantu Tim IT merespons lebih cepat.
- **Lingkungan:** perangkat MikroTik/SFP yang disetujui Tim IT.
- **Jam operasi:** laptop NMS hidup pada jam kerja kantor; jam aktual dan jeda
  tercatat di tabel jendela observasi, bukan asumsi 24/7.
- **Pembeda yang diuji:** prediksi degradasi fiber (ETA ke ambang Rx
  waspada/kritis) dari tren data nyata.

## 2. Metode

- Sumber angka: `backend/nms.db` (pemeriksaan perangkat, masalah, prediksi
  fiber, dan log alarm).
- Jendela observasi dihitung dari timestamp pemeriksaan yang benar-benar
  tersimpan, bukan asumsi 24/7.
- MTTD dihitung dari cek gagal pertama pada outage yang sama ke problem
  tercatat untuk `device_down`/`high_latency`. Nilai hanya dipakai bila cek
  `online` terakhir berada <= 3x interval polling (minimal 3 menit) sebelum
  cek gagal pertama; bila NMS sempat mati atau pindah jaringan, hasilnya
  `n/a` — bukan nol. Masalah fiber memakai `n/a` karena tidak didahului cek
  perangkat.
- MTTR dihitung dari `resolved_at - started_at` untuk problem yang selesai.
- Cakupan dihitung terhadap jam kerja Senin-Jumat (08:00-17:00). Hari kerja
  tanpa data tetap dihitung dalam penyebut; hari ini dihitung sampai waktu
  laporan dibuat.
- Prediksi fiber dibandingkan dengan jam pertama ketika `rx_avg` benar-benar
  melewati ambang. Prediksi yang belum terlewati dilaporkan sebagai
  **Belum terverifikasi**.
- Uji kanal (`Uji kirim:`) dilaporkan terpisah dan tidak dihitung sebagai
  alarm produksi.
- Tidak ada data sintetis, estimasi, atau angka yang dikarang.

## 3. Hasil

> Tempel keluaran `tools/pilot_report.py` di sini. Pertahankan bagian
> "Belum terverifikasi" apa adanya.

<!-- MULAI SALINAN OTOMATIS -->

_(belum diisi — jalankan alat bukti pada data pilot)_

<!-- SELESAI SALINAN OTOMATIS -->

## 4. Alarm yang berguna

| Waktu | Problem | Channel | Status | Berguna? (isi Tim IT) | Catatan |
|---|---|---|---|---|---|
| _(dari alarm.csv)_ | | | | | |

Isi kolom **Berguna?** dengan `ya`, `tidak`, atau `sebagian`, disertai alasan
singkat dari Tim IT. Alarm yang tidak berguna tetap dilaporkan agar false
positive terlihat, bukan disembunyikan. Baris dengan kategori `uji kanal`
adalah tes pengiriman manual, bukan alarm produksi; jangan mencampurnya dengan
hitungan alarm nyata.

## 5. Prediksi fiber: hasil versus kenyataan

| Link | Ujung | ETA saat terbit (hari) | Ambang terlewati | Lead time (hari) | Status |
|---|---|---|---|---|---|
| _(dari prediksi.csv)_ | | | | | |

Interpretasi wajib jujur:

- **Terkonfirmasi** = ambang benar-benar terlewati pada data.
- **Belum terverifikasi** = belum ada bukti ambang terlewati; bukan klaim
  prediksi benar.
- Selisih lead time versus ETA dicatat apa adanya, termasuk bila prediksi
  terlalu cepat atau terlalu lambat.

## 6. Batasan

- Data hanya ada saat laptop NMS hidup; jam aktual dan jeda terlihat di tabel
  jendela observasi. Malam dan akhir pekan tidak diklaim sebagai waktu
  terpantau.
- Cakupan dihitung terhadap jam kerja Senin-Jumat (08:00-17:00); hari libur
  tidak dikecualikan dan hari ini dihitung sampai waktu laporan dibuat.
- MTTD `n/a` berarti transisi online->gagal tidak teramati rapat (mis. NMS
  mati atau pindah jaringan), bukan nol detik.
- Perangkat hanya bisa dijangkau dari jaringan kantor atau VPN; dari WiFi lain
  NMS memang tidak dapat menjangkau perangkat, dan itu bukan bukti perangkat
  mati.
- Prediksi memakai regresi linier sederhana; bukan jaminan dan tidak
  menggantikan pemeriksaan fisik fiber.
- Jumlah insiden nyata bisa nol. Bila demikian, tulis "belum ada insiden nyata
  pada periode ini" — jangan mengarang insiden.
- Angka `n/a` dan sel kosong di CSV berarti bukti belum tersedia, bukan nol.

## 7. Kesimpulan

Isi setelah data final:

- Apa yang terbukti dari pilot (deteksi, waktu respons, alarm berguna).
- Apa yang belum terbukti (mis. prediksi belum terkonfirmasi).
- Rekomendasi untuk Tim IT (lanjut pilot, tambah perangkat, atau perbaiki
  proses).

## 8. Lampiran

- `cakupan-perangkat.csv` — cakupan dan status per perangkat.
- `insiden.csv` — daftar masalah dan waktu deteksi/penyelesaian.
- `prediksi.csv` — riwayat prediksi dan hasilnya.
- `alarm.csv` — alarm produksi/uji kanal dan kolom validasi Tim IT.
