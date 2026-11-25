# Permintaan Akses SFP/DDM untuk Pilot NMS Kopegtel

**Tanggal:** 25 November 2026
**Dari:** Sandi (magang NMS Kopegtel)
**Untuk:** Tim IT Kopegtel
**Perihal:** Akses read-only 1–2 perangkat ber-SFP untuk pilot prediksi degradasi fiber

## 1. Latar belakang

NMS Kopegtel sedang menjalankan pilot pemantauan perangkat dan fiber. Fitur
yang diuji: **prediksi kapan daya Rx fiber akan melewati ambang** (warning/
critical) memakai data DDM/DOM dari port SFP. Fitur sudah terpasang dan lulus
tes, tetapi belum bisa menghasilkan prediksi pada data nyata karena perangkat
pilot saat ini tidak memiliki port SFP.

## 2. Temuan teknis (25 November 2026)

- MikroTik Utama (CCR1016-12G, `192.168.30.1`): SNMP jalan, tetapi hanya
  memiliki `ether1`–`ether12` + `bridge1`; tidak ada port SFP dan
  `mtxrOptical` kosong.
- API RouterOS 8728/8729 tertutup dari laptop NMS.
- Tabel `optical_readings`, `optical_hourly`, dan `fiber_links` kosong.
- ONT ZTE F670L (`192.168.1.1`) memiliki web UI, tetapi belum ada integrasi
  pembacaan Rx ke NMS.

## 3. Yang dibutuhkan

### A. Perangkat

- 1–2 perangkat dengan port **SFP/SFP+ dan DDM/DOM aktif** (MikroTik, switch,
  atau OLT/ONT yang mendukung SNMP).
- Sebutkan: merek/model, nama port SFP, modul SFP (vendor/part number), dan
  apakah link fiber terpasang.
- Jika memungkinkan, satu link dengan dua ujung yang bisa dipantau.

### B. Akses read-only

- **SNMP v2c**: community read-only, atau **SNMP v3** (user/auth/priv
  read-only), dari IP NMS `192.168.110.158`.
- Alternatif MikroTik: user API read-only + aktifkan `/ip service api` (8728)
  atau `api-ssl` (8729), dibatasi ke IP NMS.
- Tidak perlu akses tulis; NMS tidak mengubah konfigurasi perangkat.

### C. Data link (untuk profil ambang)

- Perangkat/port ujung A dan B.
- Panjang fiber (km), jumlah splice/konektor, splitter (mis. 1:8) bila GPON.
- Datasheet SFP/ONT: Rx sensitivity, Rx overload, Tx power range, wavelength.

### D. Keamanan

- Kredensial dikirim lewat kanal aman (bukan email/chat biasa), disimpan
  terenkripsi di NMS.
- Akses dibatasi IP NMS dan bisa dicabut kapan saja.
- NMS hanya membaca; tidak ada perubahan konfigurasi perangkat.

## 4. Target waktu

- Akses aktif: **Jumat, 28 November 2026** (atau lebih cepat).
- Data DDM minimal 24 titik hourly (satu per jam) — sekitar 3 hari kerja
  dengan 9 jam/hari.
- ETA pertama: **~1 Desember 2026**.
- Jika akses belum tersedia sampai **1 Desember**, pilot beralih ke rencana
  cadangan (wallboard NOC) sesuai spec; tidak ada demo dengan data palsu.

## 5. Verifikasi setelah akses

- `tools\pilot_connectivity.py --device <id>` (ping + SNMP/API).
- Cek `optical_readings` dan `optical_hourly` terisi.
- Daftarkan link di menu Fiber → Link & Redaman, lalu jalankan evaluasi.
- Bukti: `tools\pilot_report.py` menampilkan prediksi/lead time; jika ambang
  belum terlewati, ditulis **Belum terverifikasi**.

## 6. Status permintaan

- [ ] Dikirim ke Tim IT (tanggal: ____)
- [ ] Akses diterima
- [ ] Verifikasi DDM berhasil
- [ ] Link fiber terdaftar + profil ambang
- [ ] Data 3 hari kerja terkumpul
- [ ] ETA pertama muncul

## 7. Pesan singkat untuk Tim IT (siap kirim)

> Selamat siang Pak/Bu, saya Sandi (magang NMS Kopegtel). Untuk pilot prediksi
> degradasi fiber, saya butuh akses **read-only** ke 1–2 perangkat yang punya
> port **SFP/SFP+ dengan DDM/DOM aktif** (SNMP v2c/v3 atau API MikroTik), dari
> laptop NMS `192.168.110.158`. Perangkat pilot saat ini (CCR1016-12G) tidak
> punya port SFP, jadi prediksi belum bisa diuji dengan data nyata. Mohon info
> merek/model, port SFP, dan modul SFP-nya; data link (panjang, splice,
> splitter) juga membantu. Target akses 28 Nov agar ETA pertama muncul ~1 Des.
> NMS hanya membaca, tidak mengubah konfigurasi. Terima kasih.

## 8. Pesan konsolidasi untuk Tim IT (DDM + Host .211 + perangkat tambahan)

> Selamat siang Pak/Bu, saya Sandi (magang NMS Kopegtel). Ada tiga hal yang
> kami butuhkan untuk pilot NMS:
>
> 1. **Akses DDM (prioritas).** Mohon akses read-only ke 1–2 perangkat yang
>    punya port SFP/SFP+ dengan DDM/DOM aktif (SNMP v2c/v3 atau API MikroTik),
>    dari laptop NMS `192.168.110.158`. CCR1016-12G saat ini tidak punya port
>    SFP. Mohon info merek/model, port SFP, modul SFP, dan data link (panjang,
>    splice, splitter). Target akses 28 Nov agar ETA pertama ~1 Des.
> 2. **Status Host `192.168.30.211`.** NMS mencatat ICMP timeout dan problem
>    terbuka sejak 24 Nov 00:24 WIB. Apakah host memang down, atau ICMP
>    diblokir? Jika diblokir, mohon akses SNMP/agent read-only agar statusnya
>    bisa dipantau.
> 3. **Perangkat pilot tambahan.** Saat ini baru 4 perangkat (MikroTik, ONT,
>    web, host). Mohon 10–30 perangkat lagi (IP + SNMP community read-only)
>    agar cakupan pilot memadai.
>
> NMS hanya membaca, tidak mengubah konfigurasi. Terima kasih.
