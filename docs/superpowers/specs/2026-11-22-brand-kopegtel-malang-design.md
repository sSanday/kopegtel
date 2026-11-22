# Rancangan · Identitas visual Kopegtel Malang untuk NMS

- **Status:** disetujui · 2026-11-22
- **Catatan persetujuan:** pemilik meminta warna sama dengan situs Kopegtel Malang dan memakai alur skill superpowers.
- **Tanggal:** 2026-11-22
- **Jenis perubahan:** restyle warna + penggantian logo (perubahan kecil, terpusat di token)

## 1. Tujuan dan alasan

NMS Kopegtel selama ini memakai identitas mint (`#2fe0c4`) + cobalt (`#5b8dff`) di kanvas biru-hitam.
Pemilik ingin NMS memakai warna dan logo yang sama dengan situs resmi
<https://kopegtelmalang.co.id/>, supaya portal operasional terasa satu identitas dengan koperasi.

Warna situs yang menjadi acuan:

| Peran di situs | Hex |
|---|---|
| Maroon utama (section, tombol) | `#710014` |
| Maroon sekunder | `#8b1a2e` |
| Rose (menu aktif) | `#ff4d6d` |
| Emas / khaki (logo) | `#b8a47e`, `#736953` |
| Krem (latar terang) | `#f7f5f5`, `#fafafa` |
| Perak (logo) | `#bcb9ac`, `#d4cfc9` |

Logo resmi: PNG 599×599, latar transparan, area isi 535×498 px (margin transparan 32 px kiri, 48 px atas).

## 2. Keputusan yang sudah diambil

**Opsi A — identitas penuh.** Kanvas gelap dihangatkan menjadi maroon-hitam, panel diberi tint maroon,
emas menjadi aksen utama (teks, ikon, menu aktif), maroon menjadi bidang tombol/CTA, rose menjadi aksen
kedua. Tema terang menjadi krem dengan aksen maroon.

Opsi B (hanya mengganti aksen mint/cobalt, kanvas tetap biru-hitam) ditolak karena tidak akan terasa
seperti situs Kopegtel.

## 3. Goals

- Semua halaman NMS (16 halaman HTML + login) memakai palet baru secara konsisten, tema gelap dan terang.
- Logo Kopegtel Malang tampil di sidebar, halaman login, dan favicon.
- Kontras teks dan kontrol tetap memenuhi WCAG AA.

## 4. Non-goals

- Tidak mengubah layout, struktur HTML, `id`, `name`, `data-*`, `<tbody>`, atau `<select>` yang dipakai JS.
- Tidak mengubah logika bisnis, API, atau perilaku komponen.
- Tidak mengganti warna status semantik: online `#10b981`, waspada `#f59e0b`, kritis `#f43f5e`,
  pemeliharaan `#8b5cf6`, idle `#94a3b8`. Warna ini membawa arti operasional.
- Tidak menyentuh `design-preview/kopegtel-dashboard-2026.html` (konsep desain terpisah).
- Tidak menambah framework, font eksternal, atau CDN.

## 5. Anti-goals

- Memakai maroon `#710014` mentah sebagai latar seluruh dashboard (terlalu berat untuk tabel dan grafik).
- Mengganti warna status menjadi maroon/emas (arti status hilang).
- Menempelkan logo berlatar putih kotak di sidebar gelap.

## 6. Constraints

- Vanilla HTML/CSS/JS; token terpusat di `assets/css/tokens.css` dengan `light-dark()`.
- Hampir seluruh warna sudah memakai token; hanya logo SVG di `nav.js`, mesh login di `index.html`,
  dan `favicon.svg` yang masih hardcode warna lama.
- Logo raster transparan; tidak ada file vektor resmi.
- Target sentuh, focus ring, dan `prefers-reduced-motion` yang ada harus dipertahankan.

## 7. Desain palet

### 7.1 Tema gelap

| Token | Nilai |
|---|---|
| `--bg` | `#0c0609` |
| `--panel` | `rgba(28, 10, 16, 0.78)` |
| `--panel-2` | `rgba(40, 14, 22, 0.88)` |
| `--panel-deep-start` / `-mid` / `-end` | `#170a10` / `#10060a` / `#0b0407` |
| `--login-panel` | `#0f060a` |
| `--ink` / `--ink-soft` / `--muted` / `--ink-faint` | `#f7f1ec` / `#d9cbc2` / `#ab9a91` / `#8d7c74` |
| `--line` / `--line-strong` / `--line-emphasis` | `rgba(255,241,236,.08)` / `.15` / `.24` |
| `--field-line` | `#75675f` |
| `--surface-1..5` | `rgba(255,241,236,.02/.03/.05/.08/.12)` |
| `--track` / `--scrollbar` | `rgba(255,241,236,.08)` / `.14` |
| `--highlight` / `--highlight-strong` | `rgba(255,255,255,.06)` / `.34` |
| `--muted-divider` | `rgba(255,241,236,.22)` |
| `--accent` (emas) | `#d9b36c` |
| `--accent-2` (rose) | `#ff4d6d` |
| `--accent-fill-a` / `-b` / `-c` | `#8b1a2e` / `#710014` / `#8a6d3b` |
| `--accent-fill-a-hover` / `-b-hover` | `#a02138` / `#8b1a2e` |
| `--on-accent` / `--on-accent-soft` | `#ffffff` / `rgba(255,255,255,.28)` |
| `--map-node` / `-hover` / `--map-grid-dot` | `#14080d` / `#1c0c12` / `rgba(255,241,236,.08)` |
| `--field-bg` / `-focus` / `--field-autofill` | `rgba(18,8,13,.85)` / `rgba(18,8,13,.95)` / `#150a0f` |
| `--backdrop` / `--toast-bg` / `--code-bg` | `rgba(5,2,4,.76)` / `rgba(22,9,14,.96)` / `rgba(0,0,0,.28)` |
| `--card-bg-from` / `-to` | `rgba(255,255,255,.07)` / `.02` |
| `--glow-blue` (emas) | `rgba(217,179,108,.10)` |
| `--glow-teal` / `-strong` (maroon) | `rgba(139,26,46,.18)` / `.24` |
| `--glow-input` | `rgba(217,179,108,.10)` |
| `--grid-line` | `rgba(255,241,236,.05)` |
| `--chevron` | stroke `#8a7d74` |
| `--check` | stroke `#ffffff` |

Nama token `--glow-blue` / `--glow-teal` dipertahankan agar CSS yang memakainya tidak perlu diubah;
komentarnya diperbarui menjadi "glow emas/maroon".

### 7.2 Tema terang

| Token | Nilai |
|---|---|
| `--bg` | `#f7f5f5` |
| `--panel` / `--panel-2` | `#ffffff` / `#faf8f7` |
| `--panel-deep-start` / `-mid` / `-end` | `#ffffff` / `#f7f2ef` / `#f0e9e5` |
| `--login-panel` | `#fbf9f8` |
| `--ink` / `--ink-soft` / `--muted` / `--ink-faint` | `#1f1f1f` / `#3f3a36` / `#6b625b` / `#75695f` |
| `--line` / `--line-strong` / `--line-emphasis` | `rgba(31,31,31,.10)` / `.18` / `.28` |
| `--field-line` | `#8a7d74` |
| `--surface-1..5` | `rgba(31,31,31,.025/.04/.06/.08/.12)` |
| `--track` / `--scrollbar` | `rgba(31,31,31,.08)` / `.18` |
| `--highlight` / `--highlight-strong` | `rgba(255,255,255,.85)` / `.95` |
| `--muted-divider` | `rgba(31,31,31,.22)` |
| `--accent` (maroon) | `#8b1a2e` |
| `--accent-2` (khaki) | `#736953` |
| `--map-node` / `-hover` / `--map-grid-dot` | `#ffffff` / `#f5f0ec` / `rgba(31,31,31,.12)` |
| `--field-bg` / `-focus` / `--field-autofill` | `rgba(255,255,255,.90)` / `#ffffff` / `#f3eeea` |
| `--backdrop` / `--toast-bg` / `--code-bg` | `rgba(31,15,20,.45)` / `rgba(255,255,255,.98)` / `rgba(31,31,31,.06)` |
| `--card-bg-from` / `-to` | `rgba(255,255,255,.94)` / `.72` |
| `--glow-blue` (khaki) | `rgba(184,164,126,.12)` |
| `--glow-teal` / `-strong` (maroon) | `rgba(139,26,46,.08)` / `.12` |
| `--glow-input` | `rgba(139,26,46,.12)` |
| `--grid-line` | `rgba(31,31,31,.05)` |

Token fill, `--on-accent`, dan hover tidak memakai `light-dark()`: nilainya sama di kedua tema karena
teks putih di atas maroon tetap terbaca.

### 7.3 Kontras yang sudah diverifikasi

| Pasangan | Rasio |
|---|---|
| Emas `#d9b36c` di kanvas `#0c0609` | 10.20:1 |
| Emas `#d9b36c` di panel `#1c0a10` | 9.65:1 |
| Rose `#ff4d6d` di panel `#1c0a10` | 5.94:1 |
| Putih di maroon `#8b1a2e` | 9.19:1 |
| Putih di maroon `#710014` | 12.26:1 |
| Maroon `#8b1a2e` di krem `#f7f5f5` | 8.46:1 |
| Khaki `#736953` di krem `#f7f5f5` | 4.99:1 |
| Garis field gelap `#75675f` di `#14080d` | 3.61:1 |
| Garis field terang `#8a7d74` di putih | 3.99:1 |

## 8. Logo

- Pangkas margin transparan ke bbox `32,48,535,498`.
- Simpan `assets/img/kopegtel-malang.png` (transparan, sisi terpanjang 320 px) untuk sidebar dan login.
- Sidebar: `42×42` (object-fit contain), teks "NMS KOPEGTEL" tetap di kanan logo.
- Login: `72×72`, drop-shadow emas tipis.
- `assets/img/favicon.png` 64×64 transparan dan `assets/img/apple-touch-icon.png` 180×180 berlatar krem.
- Semua halaman: ganti `<link rel="icon">` ke `favicon.png`, tambah `apple-touch-icon`.
- `alt=""` karena link logo sudah punya `aria-label`; teks NMS tetap terbaca pembaca layar.

## 9. File yang disentuh

1. `assets/css/tokens.css` — seluruh nilai palet, chevron, check.
2. `assets/js/nav.js` — lockup sidebar: SVG lama diganti `<img>` logo.
3. `index.html` — lockup login, mesh SVG (emas/maroon), meta `theme-color`.
4. `assets/img/` — logo PNG, favicon PNG, apple-touch-icon PNG.
5. Semua 16 halaman HTML root — favicon dan meta `theme-color`.
6. `DESIGN.md` — dokumentasi palet diperbarui agar tidak bertentangan.
7. `docs/superpowers/specs/2026-11-22-brand-kopegtel-malang-design.md` — dokumen ini.

## 10. Verifikasi

- `grep` memastikan tidak ada `#2fe0c4`, `#5b8dff`, atau varian lama yang tersisa di file yang dipakai.
- Buka `index.html` (login) dan `dashboard.html` di tema terang dan gelap; cek tabel, tombol, badge status,
  grafik, sidebar, dan logo.
- Cek keyboard: skip link, focus ring, menu, checkbox "Ingat saya".
- Cek 390 px: sidebar drawer, logo tidak terpotong, tombol tetap 44 px.
- `git diff --stat` untuk memastikan tidak ada perubahan di luar daftar file.

## 11. Yang diserahkan ke builder

- Nilai hover dan varian status yang tidak disebut di atas: tetap seperti sekarang.
- Ukuran logo boleh disesuaikan ±4 px bila lockup terlihat tidak seimbang.
- Bila kontras suatu komponen gagal saat dicek, builder boleh menggelapkan/mencerahkan satu langkah
  dalam keluarga warna yang sama dan mencatatnya di laporan.
