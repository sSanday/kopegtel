# DESIGN.md · NMS Kopegtel

Sistem desain "Calm Obsidian Telemetry" untuk ruang kendali jaringan (NOC) dan teknisi lapangan.
Taruh file ini di root repo `kopegtel/`.

## 1. Prinsip

- **Tenang dan presisi.** Permukaan gelap berlapis, border hairline 1px, sudut membulat lembut. Tanpa glow neon dan tanpa gradasi warna-warni per kartu.
- **Data dulu.** Angka metrik, status link, dan alarm adalah fokus. Dekorasi tidak boleh mengalahkan keterbacaan tabel dan grafik.
- **Satu identitas.** Selaras dengan `index.html`: latar deep obsidian maroon `#0c0609`, aksen emas `#d9b36c` dan rose `#ff4d6d`; logo resmi `assets/img/kopegtel-malang.png` (sidebar 42 px, login 72 px, favicon 64 px).
- **Vanilla saja.** HTML5, CSS3, ES6+. Tanpa framework eksternal.

## 2. Sumber token

`assets/css/tokens.css` adalah satu-satunya sumber token (sudah memakai `@layer` dan `light-dark()`).
Jangan membuat token ganda di `dashboard.css`. Sesuaikan nilai di `tokens.css` dengan tabel di bawah,
dan pertahankan dukungan tema terang yang sudah ada.

### Permukaan

| Token (konsep) | Nilai gelap |
|---|---|
| Kanvas halaman | `#0c0609` |
| Panel, kartu KPI, topbar | `rgba(28, 10, 16, 0.78)` |
| Sub-panel, hover baris, dropdown, modal | `rgba(40, 14, 22, 0.88)` |
| Input | `rgba(18, 8, 13, 0.85)` |
| Backdrop dialog/drawer | `rgba(5, 2, 4, 0.76)` + `backdrop-filter: blur(10px)` |

### Garis dan cahaya

- Border standar: `rgba(255,255,255,0.07)`
- Border kuat (input aktif, tombol ghost, modal): `rgba(255,255,255,0.14)`
- Highlight tepi atas kartu: `inset 0 1px 0 rgba(255,255,255,0.06)`

### Teks

- Utama `#f7f1ec`, sekunder `#ab9a91`, redup `#8d7c74`

### Aksen dan status

- Primer (tombol utama, tab aktif, kurva utama): emas `#d9b36c` (tema gelap) / maroon `#8b1a2e` (tema terang)
- Sekunder (TX/pembanding, tautan): rose `#ff4d6d` (tema gelap) / khaki `#736953` (tema terang)
- Tombol terisi: maroon `#8b1a2e → #710014`, teks putih
- Tema terang: kanvas `#f7f5f5`, aksen maroon `#8b1a2e`, sekunder khaki `#736953`
- Warna status tidak berubah (online `#10b981`, waspada `#f59e0b`, kritis `#f43f5e`, pemeliharaan `#8b5cf6`, idle `#94a3b8`).
- Badge status = pill lembut (latar 12%, border 24%, teks cerah):
  - Online / Normal `#10b981`
  - Degradasi / Waspada `#f59e0b`
  - Offline / Kritis / LOS `#f43f5e`
  - Pemeliharaan `#8b5cf6`
  - Idle / Unknown / No DDM `#94a3b8`

## 3. Tipografi

- Antarmuka: `'Plus Jakarta Sans', 'Inter', system-ui, -apple-system, sans-serif`
- Telemetri: `'JetBrains Mono', 'Fira Code', ui-monospace, monospace`
- Wajib `font-variant-numeric: tabular-nums` pada: nilai KPI, angka metrik, jam `#clock`, IP, port, redaman dB/dBm, latency ms, OID SNMP.
- Judul halaman `.topbar__title`: 1.45rem / 700 / letter-spacing -0.02em
- Judul panel `.panel__title`: 1.05rem / 600 / letter-spacing -0.01em
- Eyebrow `.topbar__eyebrow`: 0.72rem / 600 / uppercase / 0.08em / warna aksen primer

Catatan: tidak ada font web yang dimuat saat ini. Pakai font sistem sebagai fallback dan jangan menambah request eksternal.

## 4. Layout dan spasi

- Shell: sidebar 252px di desktop (>= 1024px), konten fleksibel `max-width: 1440px`, padding `24px 28px`.
- Radius: panel/stat/dialog 16px · panel bersarang/metric-card/hint-bar/note-info 12px · input/select/textarea/tombol 10px · status/chip/tab 999px.
- Transisi: `0.18s cubic-bezier(0.16, 1, 0.3, 1)`. Hormati `prefers-reduced-motion`.

## 5. Komponen

1. **Topbar `.topbar`**: sticky, `z-index: 20`, `backdrop-filter: blur(14px)`. Semua halaman operasional memuat `#refresh` dan `#clock`. Di < 1024px ada tombol hamburger.
2. **Tab halaman `.page-tabs`**: pill horizontal di bawah topbar untuk halaman multi-view, `role="tablist"`, `aria-selected`, sinkron dengan `location.hash`.
3. **Tabel `.mon-table`** dalam `.table-wrap` (`overflow-x: auto`): tanpa garis vertikal, pemisah horizontal hairline, padding sel `12px 16px`, header 0.72rem kapital redup, hover halus. Sel bertumpuk memakai `.cell-stack` (baris 1 tebal, baris 2 redup).
4. **Modal / drawer `<dialog>`**: semua form tambah/edit dan panel detail. Tutup lewat tombol `×`, `Esc`, dan klik backdrop. Di <= 640px menjadi bottom sheet atau layar penuh.
5. **Grafik SVG `.chart`**: `stroke-width: 2`, `stroke-linecap: round`, area gradasi transparan, grid putus-putus `rgba(255,255,255,0.06)`.
6. **Target sentuh**: `button`, `input`, `select` minimal 44px tinggi di layar sentuh.
7. **Aksesibilitas**: pertahankan `.skip-link`, `aria-live`, `aria-expanded`, dan ring `:focus-visible` yang jelas.
8. **Logo resmi**: gunakan `assets/img/kopegtel-malang.png` (PNG transparan) di sidebar 42 px, login 72 px, dan favicon 64 px (`favicon.png`, `apple-touch-icon.png`).

## 6. Bahasa dan judul

- Semua teks UI berbahasa Indonesia, kalimat aktif dan ringkas, terbaca dalam 3 detik.
- `<title>` dan subjudul memakai pemisah `·`: `Kejadian · NMS Kopegtel`. Jangan pakai `—` di judul.
- Istilah baku: Inventaris, Ringkasan, Daftar Host, Pengumpulan Data, Grup Host, Pemeliharaan, Pemindaian, Notifikasi, Aturan Aksi, Template Pesan, Kanal Notifikasi, Administrasi.
- Empty state menyebut kondisi saat ini dan satu langkah berikutnya.

## 7. Larangan

- Jangan ubah `id`, `name`, `data-view`, `data-tab`, `data-range`, `value`, atau struktur `<tbody>`/`<select>` yang dipakai file JS di `assets/js/`.
- Jangan hapus fitur atau ubah logika bisnis.
- Jangan tambah framework, CDN, atau font eksternal.
- Jangan pakai atribut `style="..."`; pindahkan ke class CSS.
- Jangan pakai palet lama (mint/cobalt); identitas sekarang maroon, emas, krem, rose (lihat `assets/css/tokens.css`).
