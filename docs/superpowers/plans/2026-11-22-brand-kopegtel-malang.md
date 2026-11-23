# Brand Kopegtel Malang Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ganti identitas visual NMS Kopegtel dari mint/cobalt menjadi palet maroon, emas, krem, dan rose milik situs Kopegtel Malang, serta pasang logo resmi di sidebar, login, dan favicon.

**Architecture:** Seluruh warna sudah terpusat di `assets/css/tokens.css`, jadi perubahan utama ada di satu file. Logo dan warna hardcode hanya ada di `assets/js/nav.js`, `index.html`, dan `assets/img/favicon.svg`; ketiganya diganti dengan aset logo PNG transparan. Tidak ada perubahan layout, ID, nama field, atau logika JS.

**Tech Stack:** HTML5, CSS3 (`light-dark()`, `color-mix()`), JavaScript ES6+ vanilla. Tanpa dependency baru.

**Spec:** `docs/superpowers/specs/2026-11-22-brand-kopegtel-malang-design.md`

## Global Constraints

- Tanpa framework, CDN, atau font eksternal baru.
- Semua warna brand lewat token di `assets/css/tokens.css`; jangan menambah hex brand baru di file lain.
- Jangan mengubah `id`, `name`, `data-view`, `data-tab`, `data-range`, `value`, struktur `<tbody>`/`<select>`, atau logika bisnis di `assets/js/`.
- Warna status tetap: online `#10b981`, waspada `#f59e0b`, kritis `#f43f5e`, pemeliharaan `#8b5cf6`, idle `#94a3b8`.
- Tema gelap: kanvas `#0c0609`, aksen emas `#d9b36c`, fill maroon `#8b1a2e → #710014`, teks putih.
- Tema terang: kanvas `#f7f5f5`, aksen maroon `#8b1a2e`, sekunder khaki `#736953`.
- Logo: PNG transparan, bbox asli `32,48,535,498`, disimpan dengan sisi terpanjang 320 px.
- `design-preview/` dan `backups/` di luar cakupan.
- Pertahankan `focus-visible`, `.skip-link`, `aria-*`, target sentuh 44 px, dan `prefers-reduced-motion`.

## Review Focus

- **Teks muted/faint di tema terang:** pastikan `#6b625b` dan `#75695f` di atas `#f7f5f5` tetap ≥4.5:1; jangan mengikuti rasio tema gelap.
- **Checkbox "Ingat saya":** centang putih di atas gradien maroon harus terlihat; jangan biarkan stroke lama `#061019`.
- **Logo di sidebar gelap:** PNG transparan tidak boleh tampil sebagai kotak putih, tidak terpotong, dan tetap terbaca di 42 px.
- **Kedua tema:** `light-dark()` harus menghasilkan palet yang benar saat `prefers-color-scheme` gelap dan terang, termasuk override `data-theme`.
- **Badge status:** merah maroon aksen tidak boleh tertukar dengan merah kritis `#f43f5e`; badge status tetap memakai token status, bukan `--accent`/`--accent-2`.

---

### Task 1: Setujui dokumen desain dan siapkan aset logo

**Files:**
- Modify: `docs/superpowers/specs/2026-11-22-brand-kopegtel-malang-design.md`
- Create: `assets/img/kopegtel-malang.png`
- Create: `assets/img/favicon.png`
- Create: `assets/img/apple-touch-icon.png`

**Interfaces:**
- Produces: `assets/img/kopegtel-malang.png` (PNG transparan, sisi terpanjang 320 px) dipakai Task 3.
- Produces: `assets/img/favicon.png` (64×64) dan `assets/img/apple-touch-icon.png` (180×180) dipakai Task 4.

- [ ] **Step 1: Tandai dokumen desain disetujui**

Ubah baris status menjadi:

```markdown
- **Status:** disetujui · 2026-11-22
```

Tambahkan satu baris di bawah status:

```markdown
- **Catatan persetujuan:** pemilik meminta warna sama dengan situs Kopegtel Malang dan memakai alur skill superpowers.
```

- [ ] **Step 2: Unduh sumber logo dan pangkas margin transparan**

Jalankan dari root proyek:

```powershell
$src = "$env:TEMP\kopegtel-recon\logo.png"
if (-not (Test-Path $src)) {
  New-Item -ItemType Directory -Force "$env:TEMP\kopegtel-recon" | Out-Null
  Invoke-WebRequest -Uri "https://kopegtelmalang.co.id/KopegtelMlg_Logo.png" -OutFile $src
}
```

Jalankan skrip crop dan resize berikut dari root proyek:

```powershell
Add-Type -AssemblyName System.Drawing
$src = "$env:TEMP\kopegtel-recon\logo.png"
$img = [System.Drawing.Bitmap]::FromFile($src)
$crop = New-Object System.Drawing.Rectangle(32, 48, 535, 498)
$cut = $img.Clone($crop, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
$w = 320
$h = [int][Math]::Round($cut.Height * ($w / $cut.Width))
$out = New-Object System.Drawing.Bitmap($w, $h, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
$g = [System.Drawing.Graphics]::FromImage($out)
$g.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
$g.PixelOffsetMode = [System.Drawing.Drawing2D.PixelOffsetMode]::HighQuality
$g.Clear([System.Drawing.Color]::Transparent)
$g.DrawImage($cut, 0, 0, $w, $h)
$g.Dispose()
$out.Save((Join-Path (Get-Location) "assets\img\kopegtel-malang.png"), [System.Drawing.Imaging.ImageFormat]::Png)
$out.Dispose(); $cut.Dispose(); $img.Dispose()
```

- [ ] **Step 3: Buat favicon dan apple-touch-icon**

Jalankan skrip berikut dari root proyek:

```powershell
Add-Type -AssemblyName System.Drawing
$logo = [System.Drawing.Bitmap]::FromFile((Join-Path (Get-Location) "assets\img\kopegtel-malang.png"))
function New-Icon($size, $pad, $bg, $path) {
  $bmp = New-Object System.Drawing.Bitmap($size, $size, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
  $g = [System.Drawing.Graphics]::FromImage($bmp)
  $g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::HighQuality
  $g.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
  $g.PixelOffsetMode = [System.Drawing.Drawing2D.PixelOffsetMode]::HighQuality
  if ($bg) { $g.Clear([System.Drawing.ColorTranslator]::FromHtml($bg)) } else { $g.Clear([System.Drawing.Color]::Transparent) }
  $inner = $size - 2 * $pad
  $scale = [Math]::Min($inner / $logo.Width, $inner / $logo.Height)
  $w = [int][Math]::Round($logo.Width * $scale)
  $h = [int][Math]::Round($logo.Height * $scale)
  $x = [int](($size - $w) / 2)
  $y = [int](($size - $h) / 2)
  $g.DrawImage($logo, $x, $y, $w, $h)
  $g.Dispose()
  $bmp.Save($path, [System.Drawing.Imaging.ImageFormat]::Png)
  $bmp.Dispose()
}
New-Icon 64 2 $null (Join-Path (Get-Location) "assets\img\favicon.png")
New-Icon 180 18 '#f7f5f5' (Join-Path (Get-Location) "assets\img\apple-touch-icon.png")
$logo.Dispose()
```

- [ ] **Step 4: Verifikasi aset**

Jalankan:

```powershell
Add-Type -AssemblyName System.Drawing
foreach ($f in "assets/img/kopegtel-malang.png","assets/img/favicon.png","assets/img/apple-touch-icon.png") {
  $b = [System.Drawing.Bitmap]::FromFile((Resolve-Path $f))
  Write-Output ("{0}: {1}x{2} {3}" -f $f, $b.Width, $b.Height, $b.PixelFormat)
  $b.Dispose()
}
```

Expected: logo 320×298 `Format32bppArgb`; favicon 64×64; apple-touch 180×180.

- [ ] **Step 5: Commit**

```bash
git add docs/superpowers/specs/2026-11-22-brand-kopegtel-malang-design.md docs/superpowers/plans/2026-11-22-brand-kopegtel-malang.md assets/img/kopegtel-malang.png assets/img/favicon.png assets/img/apple-touch-icon.png
git commit -m "docs(brand): approve Kopegtel Malang identity and add logo assets"
```

---

### Task 2: Ganti palet di `tokens.css`

**Files:**
- Modify: `assets/css/tokens.css`

**Interfaces:**
- Produces: semua token warna baru yang dipakai `dashboard.css`, `login.css`, dan halaman HTML.
- Consumes: tabel nilai di spec §7.1 (gelap) dan §7.2 (terang).

- [ ] **Step 1: Ganti nilai token `:root`**

Terapkan nilai dari spec §7.1 dan §7.2 pada blok `:root` di `assets/css/tokens.css`. Pertahankan nama token, urutan, dan struktur `light-dark()`. Token berikut tidak memakai `light-dark()` karena nilainya sama di kedua tema: `--accent-fill-a`, `--accent-fill-b`, `--accent-fill-c`, `--accent-fill-a-hover`, `--accent-fill-b-hover`, `--on-accent`, `--on-accent-soft`.

- [ ] **Step 2: Perbarui komentar**

Ganti komentar lama yang menyebut mint/cobalt/teal/blue dengan istilah brand baru. Contoh:

```css
/* Aksen: --accent emas/maroon untuk teks/ikon, --accent-fill-* untuk bidang terisi. */
/* Gradien & glow: nama token lama dipertahankan agar kompatibel; nilainya emas/maroon. */
```

- [ ] **Step 3: Perbarui ikon data URI**

- `--chevron`: ganti stroke `%2364748b` menjadi `%238a7d74`.
- `--check`: ganti stroke `%23061019` menjadi `%23ffffff` karena latar centang sekarang maroon.

- [ ] **Step 4: Verifikasi tidak ada warna lama**

```powershell
Select-String -Path assets/css/tokens.css -Pattern '2fe0c4|5b8dff|0b7a6c|2563eb|46e6cd|6f9bff|5ce0a5|64748b|061019'
```

Expected: tidak ada hasil.

Pastikan warna status masih utuh:

```powershell
Select-String -Path assets/css/tokens.css -Pattern '10b981|f59e0b|f43f5e|8b5cf6|94a3b8'
```

Expected: kelima nilai ditemukan.

- [ ] **Step 5: Verifikasi kontras**

Jalankan skrip Node dari spec §7.3 dan pastikan hasilnya sama dengan tabel. Fokus pada:

```text
#d9b36c on #0c0609 = 10.20
#ffffff on #8b1a2e = 9.19
#8b1a2e on #f7f5f5 = 8.46
#736953 on #f7f5f5 = 4.99
```

- [ ] **Step 6: Commit**

```bash
git add assets/css/tokens.css
git commit -m "style(brand): replace mint palette with Kopegtel Malang maroon and gold"
```

---

### Task 3: Pasang logo di sidebar dan login

**Files:**
- Modify: `assets/js/nav.js:40-50`
- Modify: `assets/css/dashboard.css:168-172`
- Modify: `assets/css/login.css:151-156`
- Modify: `index.html:18-44`
- Modify: `index.html:82-135`

**Interfaces:**
- Consumes: `assets/img/kopegtel-malang.png` dari Task 1.
- Produces: lockup sidebar dan login memakai `<img class="lockup__mark">`.

- [ ] **Step 1: Ganti lockup sidebar di `nav.js`**

Ganti blok `<svg class="lockup__mark">…</svg>` (baris 42-48) dengan:

```js
'<img class="lockup__mark" src="assets/img/kopegtel-malang.png" alt="" width="42" height="42" />' +
```

Pertahankan `<a class="lockup" href="dashboard.html" aria-label="NMS Kopegtel">` dan teks `NMS KOPEGTEL`.

- [ ] **Step 2: Sesuaikan ukuran mark sidebar**

Di `assets/css/dashboard.css`, ubah `.lockup__mark` menjadi:

```css
.lockup__mark {
  width: 42px;
  height: 42px;
  flex: none;
  object-fit: contain;
}
```

- [ ] **Step 3: Sesuaikan mark login**

Di `assets/css/login.css`, ubah `.lockup__mark` menjadi:

```css
.lockup__mark {
  width: 72px;
  height: 72px;
  flex: none;
  object-fit: contain;
  filter: drop-shadow(0 10px 20px color-mix(in srgb, var(--accent) 24%, transparent));
}
```

- [ ] **Step 4: Ganti lockup login di `index.html`**

Ganti seluruh `<svg class="lockup__mark" …>…</svg>` (baris 21-38) dengan:

```html
<img class="lockup__mark" src="assets/img/kopegtel-malang.png" alt="" width="72" height="72" />
```

Pertahankan `<a class="lockup" … aria-label="NMS Kopegtel, sistem manajemen jaringan">` dan teks lockup.

- [ ] **Step 5: Recolor mesh login**

Di `index.html` bagian `.mesh`:

- stop `#2fe0c4` → `#d9b36c`
- stop `#5b8dff` → `#8b1a2e`
- stop radial glow `#2fe0c4` → `#d9b36c`

- [ ] **Step 6: Verifikasi warna lama hilang**

```powershell
Select-String -Path assets/js/nav.js,index.html -Pattern '2fe0c4|5b8dff'
```

Expected: tidak ada hasil.

- [ ] **Step 7: Verifikasi logo termuat**

Buka `index.html` lewat server lokal dan jalankan di browser:

```js
const img = document.querySelector('.lockup__mark');
({ complete: img.complete, w: img.naturalWidth, h: img.naturalHeight, src: img.currentSrc })
```

Expected: `complete: true`, `w > 0`, `h > 0`, `src` berakhir `kopegtel-malang.png`.

- [ ] **Step 8: Commit**

```bash
git add assets/js/nav.js assets/css/dashboard.css assets/css/login.css index.html
git commit -m "feat(brand): show Kopegtel Malang logo in sidebar and login"
```

---

### Task 4: Favicon dan theme-color semua halaman

**Files:**
- Modify: `administration.html`, `alerts.html`, `dashboard.html`, `data-collection.html`, `events.html`, `fiber.html`, `ganti-password.html`, `index.html`, `inventory.html`, `mikrotik.html`, `monitoring.html`, `profile.html`, `reports.html`, `services.html`, `traffic.html`, `users.html`

**Interfaces:**
- Consumes: `assets/img/favicon.png` dan `assets/img/apple-touch-icon.png` dari Task 1.

- [ ] **Step 1: Ganti link ikon**

Di setiap halaman, ganti:

```html
<link rel="icon" href="assets/img/favicon.svg" type="image/svg+xml" />
```

dengan:

```html
<link rel="icon" href="assets/img/favicon.png" type="image/png" />
<link rel="apple-touch-icon" href="assets/img/apple-touch-icon.png" />
```

- [ ] **Step 2: Ganti `theme-color`**

Ganti nilai:

```html
<meta name="theme-color" content="#f3f6fb" media="(prefers-color-scheme: light)" />
<meta name="theme-color" content="#060a14" media="(prefers-color-scheme: dark)" />
```

dengan:

```html
<meta name="theme-color" content="#f7f5f5" media="(prefers-color-scheme: light)" />
<meta name="theme-color" content="#0c0609" media="(prefers-color-scheme: dark)" />
```

- [ ] **Step 3: Verifikasi 16 halaman**

```powershell
$files = Get-ChildItem *.html
foreach ($f in $files) {
  $t = Get-Content -Raw $f.FullName
  $icon = ([regex]::Matches($t, 'favicon\.png')).Count
  $apple = ([regex]::Matches($t, 'apple-touch-icon\.png')).Count
  $old = ([regex]::Matches($t, 'favicon\.svg|#f3f6fb|#060a14')).Count
  Write-Output ("{0}: icon={1} apple={2} old={3}" -f $f.Name, $icon, $apple, $old)
}
```

Expected: 16 baris, `icon=1`, `apple=1`, `old=0`.

- [ ] **Step 4: Hapus favicon lama**

Pastikan tidak ada halaman yang masih merujuk `favicon.svg`:

```powershell
Select-String -Path *.html -Pattern 'favicon\.svg'
```

Expected: tidak ada hasil. Lalu jalankan:

```bash
git rm assets/img/favicon.svg
```

- [ ] **Step 5: Commit**

```bash
git add administration.html alerts.html dashboard.html data-collection.html events.html fiber.html ganti-password.html index.html inventory.html mikrotik.html monitoring.html profile.html reports.html services.html traffic.html users.html
git commit -m "chore(brand): update favicon and theme-color on all pages"
```

---

### Task 5: Perbarui dokumentasi desain

**Files:**
- Modify: `DESIGN.md`

**Interfaces:**
- Consumes: palet final dari spec §7.

- [ ] **Step 1: Perbarui tabel permukaan dan teks**

Ganti nilai gelap di `DESIGN.md` §2 dengan palet baru:

- Kanvas `#0c0609`
- Panel `rgba(28, 10, 16, 0.78)`
- Sub-panel `rgba(40, 14, 22, 0.88)`
- Input `rgba(18, 8, 13, 0.85)`
- Backdrop `rgba(5, 2, 4, 0.76)`
- Teks utama `#f7f1ec`, sekunder `#ab9a91`, redup `#8d7c74`

- [ ] **Step 2: Perbarui aksen dan status**

- Primer: emas `#d9b36c`
- Sekunder: rose `#ff4d6d`
- Tombol terisi: maroon `#8b1a2e → #710014`
- Tema terang: kanvas `#f7f5f5`, aksen `#8b1a2e`, sekunder `#736953`
- Status tidak berubah; tulis bahwa status sengaja tetap.

- [ ] **Step 3: Tambahkan catatan logo**

Tambahkan satu paragraf di §1 atau §5: logo resmi `assets/img/kopegtel-malang.png` dipakai di sidebar 42 px, login 72 px, dan favicon.

- [ ] **Step 4: Verifikasi tidak ada warna lama**

```powershell
Select-String -Path DESIGN.md -Pattern '2fe0c4|5b8dff|0b7a6c|2563eb|Cyber Mint|Electric Cobalt'
```

Expected: tidak ada hasil.

- [ ] **Step 5: Commit**

```bash
git add DESIGN.md
git commit -m "docs(brand): document Kopegtel Malang palette and logo"
```

---

### Task 6: Verifikasi akhir

**Files:**
- No new files.

- [ ] **Step 1: Verifikasi seluruh repo**

```powershell
git grep -n -E '2fe0c4|5b8dff|0b7a6c|2563eb|46e6cd|6f9bff|5ce0a5' -- '*.html' '*.css' '*.js'
```

Expected: tidak ada hasil di file produksi. File `backups/` dan `design-preview/` diabaikan. Dokumen spec boleh menyebut warna lama sebagai konteks sejarah.

- [ ] **Step 2: Jalankan server lokal dan cek login**

```powershell
Start-Process -FilePath ".venv\Scripts\python.exe" -ArgumentList "-m","http.server","8765" -WorkingDirectory (Get-Location) -WindowStyle Hidden
```

Buka `http://127.0.0.1:8765/index.html` di browser, lalu cek:

```js
const root = getComputedStyle(document.documentElement);
({
  bg: root.getPropertyValue('--bg').trim(),
  accent: root.getPropertyValue('--accent').trim(),
  fill: root.getPropertyValue('--accent-fill-a').trim(),
  logo: (() => { const i = document.querySelector('.lockup__mark'); return { w: i.naturalWidth, h: i.naturalHeight, complete: i.complete }; })()
})
```

Expected: `--bg` berisi `light-dark(#f7f5f5, #0c0609)`, `--accent` berisi `light-dark(#8b1a2e, #d9b36c)`, logo termuat. Baca juga console browser; expected tidak ada error.

- [ ] **Step 3: Cek dashboard di kedua tema**

Buka `dashboard.html`, paksa tema:

```js
document.documentElement.dataset.theme = 'light';
```

lalu:

```js
document.documentElement.dataset.theme = 'dark';
```

Periksa: sidebar, logo, tombol primer, badge status, tabel, dan grafik. Tidak ada elemen yang memakai mint/cobalt.

- [ ] **Step 4: Cek keyboard dan 390 px**

- Tab pertama mencapai skip link, focus ring terlihat.
- Sidebar drawer terbuka/tertutup di lebar sempit.
- Logo tidak terpotong dan tidak menutupi teks.
- Tombol tetap ≥44 px.

- [ ] **Step 5: Cek diff**

```powershell
git status --short
git log --oneline -6
git show --stat --oneline HEAD~5..HEAD
```

Expected: hanya file di daftar Task 1-5 yang berubah; tidak ada perubahan pada `design-preview/`, `backups/`, `backend/`, atau `assets/js/` selain `nav.js`.

- [ ] **Step 6: Laporan akhir**

Tulis laporan: palet yang dipakai, kontras, file yang berubah, hasil cek browser, dan hal yang belum bisa dicek (mis. screenshot bila jendela browser tidak terlihat).
