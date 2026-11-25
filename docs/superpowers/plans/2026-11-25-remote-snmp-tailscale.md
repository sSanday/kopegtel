# Remote SNMP via Tailscale Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** PC kantor menjadi Tailscale subnet router untuk `192.168.30.0/24`, sehingga laptop di luar kantor bisa melakukan polling SNMP ke MikroTik asli `192.168.30.1` tanpa mengubah kode NMS dan tanpa membuka port ke internet.

**Architecture:** Satu PC Windows di kantor menjalankan Tailscale dan mengiklankan rute `192.168.30.0/24`. Laptop join tailnet yang sama dan menerima rute. NMS tetap memakai device 6 dengan IP `192.168.30.1`; Tailscale yang meneruskan paket.

**Tech Stack:** Tailscale (WireGuard), PowerShell 5.1+, Windows 10/11, Python 3.13 venv NMS, `tools/pilot_connectivity.py`.

**Spec:** `docs/superpowers/specs/2026-11-25-remote-snmp-tailscale-design.md`

## Global Constraints

- Tidak ada perubahan kode NMS; device 6 dan IP `192.168.30.1` tidak diubah.
- Tidak membuka UDP 161, TCP 8728, atau port inbound lain ke internet.
- Script office harus mendukung `-DryRun`, aman dijalankan ulang, dan tidak pernah mencetak auth key.
- Auth key tidak boleh masuk git, log, atau screenshot.
- PC kantor harus selalu menyala dan bisa mencapai `192.168.30.0/24`.
- Dokumentasi dalam Bahasa Indonesia; perintah dan nama parameter dalam bahasa Inggris.
- Tidak ada data sintetis; angka pilot hanya dari perangkat asli.
- Semua tes yang sudah ada harus tetap hijau.

## Review Focus

- Auth key bocor lewat command line, history, atau output `-DryRun`.
- Rute `192.168.30.0/24` sudah diiklankan tetapi belum di-approve di konsol Tailscale.
- PC kantor sleep atau firewall memblokir Tailscale, sehingga tunnel putus tanpa terlihat.
- `tailscale up --reset` mengubah konfigurasi Tailscale yang sudah ada di PC kantor; jadikan opt-in lewat `-Reset`.
- Windows IP forwarding belum aktif, sehingga rute diiklankan tetapi paket tidak diteruskan.
- PC kantor berada di subnet lain dan tidak punya rute ke `192.168.30.1`.
- Laptop login ke tailnet berbeda, sehingga peer kantor tidak terlihat.
- Install Tailscale butuh admin dan mungkin butuh logout/login agar service aktif.

---

### Task 1: Script setup PC kantor

**Files:**
- Create: `tools/office-tailscale-subnet.ps1`
- Create: `tools/test_office-tailscale-subnet.ps1`

**Interfaces:**
- Consumes: tidak ada.
- Produces: `tools/office-tailscale-subnet.ps1` dengan parameter `-AuthKey`, `-Subnet` (default `192.168.30.0/24`), `-Hostname` (default `kopegtel-office`), `-Target` (default `192.168.30.1`), `-Reset`, `-DryRun`.
- Exit code `0` = sukses/dry-run valid; `1` = gagal.

- [ ] **Step 1: Tulis smoke test yang gagal**

Buat `tools/test_office-tailscale-subnet.ps1`:

```powershell
$ErrorActionPreference = 'Stop'
$script = Join-Path $PSScriptRoot 'office-tailscale-subnet.ps1'
if (-not (Test-Path $script)) { throw "Script belum ada: $script" }

$output = & pwsh -NoProfile -File $script -DryRun 2>&1 | Out-String
if ($LASTEXITCODE -ne 0) { throw "Dry-run gagal dengan exit code $LASTEXITCODE" }
if ($output -notmatch 'tailscale up') { throw "Dry-run tidak menampilkan perintah tailscale up" }
if ($output -notmatch '192\.168\.30\.0/24') { throw "Dry-run tidak menampilkan subnet" }
if ($output -notmatch 'kopegtel-office') { throw "Dry-run tidak menampilkan hostname" }
if ($output -match 'tskey-auth-[A-Za-z0-9]+') { throw "Dry-run membocorkan auth key" }
Write-Host 'OK: dry-run valid'
```

- [ ] **Step 2: Jalankan test untuk memastikan gagal**

Run:
```powershell
pwsh -NoProfile -File tools/test_office-tailscale-subnet.ps1
```
Expected: FAIL dengan pesan `Script belum ada`.

- [ ] **Step 3: Implementasikan `tools/office-tailscale-subnet.ps1`**

Struktur wajib:

```powershell
[CmdletBinding()]
param(
    [string]$AuthKey = $env:TAILSCALE_AUTH_KEY,
    [string]$Subnet = '192.168.30.0/24',
    [string]$Hostname = 'kopegtel-office',
    [string]$Target = '192.168.30.1',
    [switch]$Reset,
    [switch]$DryRun
)
```

Fungsi dan perilaku:

- `Test-IsAdmin`: cek `WindowsPrincipal` / `IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)`.
- `Get-TailscalePath`: cari `tailscale.exe` di `$env:ProgramFiles\Tailscale`, `$env:ProgramFiles(x86)\Tailscale`, lalu `Get-Command tailscale`.
- `Install-Tailscale`: jika tidak ada, coba `winget install --id Tailscale.Tailscale -e --silent --accept-package-agreements --accept-source-agreements`; jika winget tidak ada/gagal, unduh `https://pkgs.tailscale.com/stable/tailscale-setup-latest.exe` ke `$env:TEMP` dan jalankan `Start-Process -FilePath $msi -ArgumentList '/quiet','/norestart' -Wait`.
- `Start-TailscaleService`: `Start-Service Tailscale`; tunggu maksimal 30 detik sampai `Get-Service Tailscale` berstatus `Running`.
- `Invoke-TailscaleUp`: susun argumen `up --advertise-routes=$Subnet --accept-routes --hostname=$Hostname`; tambahkan `--reset` hanya jika `-Reset` diberikan, dan `--authkey=$AuthKey` hanya jika `$AuthKey` tidak kosong. Di `-DryRun`, cetak perintah dengan auth key diganti `<REDACTED>`; jangan pernah mencetak nilai asli.
- `Test-OfficeTarget`: `Test-Connection -ComputerName $Target -Count 2 -Quiet`; jika gagal, cetak peringatan bahwa PC mungkin tidak berada di jaringan kantor, tetapi jangan gagal total sebelum Tailscale terpasang.
- Di awal, jika `-DryRun`: cetak ringkasan (`Subnet`, `Hostname`, `Target`, status Tailscale terdeteksi/TIDAK, status admin) dan perintah yang akan dijalankan; `exit 0`.
- Jika bukan admin: tulis pesan `Jalankan PowerShell sebagai Administrator.` lalu `exit 1`.
- Setelah `tailscale up`, jalankan `tailscale status` dan `tailscale ip -4`; cetak langkah berikutnya: approve route di konsol admin Tailscale.
- Jangan menyimpan auth key ke file atau history secara eksplisit; setelah dipakai, `Remove-Item Env:TAILSCALE_AUTH_KEY -ErrorAction SilentlyContinue`.
- Idempoten: jika Tailscale sudah terpasang dan service sudah jalan, lewati instalasi; `tailscale up` aman diulang. `--reset` hanya dipakai bila `-Reset` diminta eksplisit.

- [ ] **Step 4: Jalankan smoke test untuk memastikan lulus**

Run:
```powershell
pwsh -NoProfile -File tools/test_office-tailscale-subnet.ps1
```
Expected: `OK: dry-run valid`.

- [ ] **Step 5: Commit**

```powershell
git add tools/office-tailscale-subnet.ps1 tools/test_office-tailscale-subnet.ps1
git commit -m "feat(tools): script Tailscale subnet router untuk akses remote SNMP"
```

---

### Task 2: Panduan lengkap + pesan siap-kirim untuk Tim IT

**Files:**
- Create: `docs/AKSES-REMOTE-TAILSCALE.md`
- Create: `docs/PESAN-TIM-IT-TAILSCALE.md`

**Interfaces:**
- Consumes: parameter dan perilaku script dari Task 1.
- Produces: dokumen yang bisa langsung diikuti Tim IT dan pengguna.

- [ ] **Step 1: Tulis `docs/AKSES-REMOTE-TAILSCALE.md`**

Isi wajib:

1. **Ringkasan** — masalah IP privat, solusi subnet router, tanpa port forwarding.
2. **Prasyarat** — akun Tailscale gratis, auth key, PC kantor Windows yang selalu menyala, akses admin, PC bisa ping `192.168.30.1`.
3. **Membuat auth key** — langkah di `login.tailscale.com` → Settings → Keys → Generate auth key; pilih reusable, non-ephemeral, tag `office-router`; simpan di password manager.
4. **Opsi A: GUI** — install Tailscale, login, `tailscale up --advertise-routes=192.168.30.0/24 --accept-routes --hostname=kopegtel-office`, atau via tray icon.
5. **Opsi B: Script** — contoh:
   ```powershell
   Set-ExecutionPolicy -Scope Process Bypass
   .\tools\office-tailscale-subnet.ps1 -AuthKey "tskey-auth-..." -DryRun
   .\tools\office-tailscale-subnet.ps1 -AuthKey "tskey-auth-..."
   ```
   Jelaskan `-DryRun` dan bahwa auth key tidak boleh ditempel ke chat.
6. **Approve route** — Machines → PC kantor → Edit route settings → approve `192.168.30.0/24`.
7. **Laptop** — install Tailscale, login akun yang sama, `tailscale up --accept-routes`, cek `tailscale status`, `ping 192.168.30.1`.
8. **Verifikasi SNMP** — `python tools/pilot_connectivity.py --device 6`; expected `Verdict: ok`; lalu nyalakan NMS dan cek `/healthz`.
9. **Troubleshooting** — tabel: peer tidak terlihat (akun beda), route tidak tampil (belum approve), ping timeout (PC sleep/firewall/subnet), IP forwarding Windows belum aktif (`Set-ItemProperty -Path 'HKLM:\SYSTEM\CurrentControlSet\Services\Tcpip\Parameters' -Name IPEnableRouter -Value 1` lalu reboot), SNMP timeout (community/ACL), Tailscale blocked (DERP).
10. **Keamanan** — jangan Funnel/exit node, cabut auth key lama, ACL minimal, jangan expose port.
11. **Rollback** — `tailscale down` di kedua sisi, hapus route, NMS kembali offline.

- [ ] **Step 2: Tulis `docs/PESAN-TIM-IT-TAILSCALE.md`**

Pesan pendek siap copy-paste ke WhatsApp/Telegram, maksimal ~300 kata:

- Tujuan: akses monitoring SNMP dari luar kantor.
- Yang dibutuhkan: satu PC kantor yang selalu menyala, Windows, akses admin.
- Langkah: install Tailscale, jalankan script/2 perintah, approve route (atau kirim screenshot).
- Yang dikirim balik: screenshot `tailscale status`, konfirmasi rute `192.168.30.0/24` approved, dan nama PC.
- Larangan: jangan kirim password MikroTik atau auth key di grup; auth key hanya ke pengguna.
- Estimasi: 10–15 menit.

- [ ] **Step 3: Verifikasi dokumen**

Run:
```powershell
Select-String -Path docs/AKSES-REMOTE-TAILSCALE.md,docs/PESAN-TIM-IT-TAILSCALE.md -Pattern 'tskey-auth-[A-Za-z0-9]{10,}'
```
Expected: tidak ada hasil (tidak ada auth key nyata).

Cek juga bahwa nama parameter di dokumen sama dengan script:
```powershell
Select-String -Path tools/office-tailscale-subnet.ps1 -Pattern 'AuthKey|Subnet|Hostname|Target|DryRun'
```
Expected: semua parameter muncul.

- [ ] **Step 4: Commit**

```powershell
git add docs/AKSES-REMOTE-TAILSCALE.md docs/PESAN-TIM-IT-TAILSCALE.md
git commit -m "docs: panduan akses remote SNMP via Tailscale dan pesan Tim IT"
```

---

### Task 3: Preflight laptop dan prosedur verifikasi

**Files:**
- Modify: `docs/AKSES-REMOTE-TAILSCALE.md` (bagian verifikasi laptop)
- Create: `docs/VERIFIKASI-REMOTE-SNMP.md`

**Interfaces:**
- Consumes: `tools/pilot_connectivity.py --device 6` dan endpoint `/healthz`.
- Produces: checklist bukti yang bisa ditempel ke laporan.

- [ ] **Step 1: Tulis `docs/VERIFIKASI-REMOTE-SNMP.md`**

Isi wajib:

1. **Sebelum konek** — pastikan Tailscale laptop `Running`, akun sama, `tailscale status` menunjukkan PC kantor `online`.
2. **Cek rute** — `tailscale status --json` atau `tailscale debug prefs`; pastikan `192.168.30.0/24` diterima.
3. **Cek jaringan** — `ping 192.168.30.1` (2–4 paket), `tracert -d 192.168.30.1`; expected hop terakhir MikroTik.
4. **Cek SNMP** — `python tools/pilot_connectivity.py --device 6`; expected:
   ```
   Ping : BERHASIL
   SNMP : BERHASIL
   Verdict: ok
   ```
5. **Cek NMS** — start server, `GET /healthz` → `{"status":"ok"}`, buka Monitoring → Hosts → Mikrotik Utama → status `online`, SNMP `sys_descr` `RouterOS CCR1016-12G`.
6. **Bukti** — simpan timestamp WIB, output perintah, commit hash; isi ke `ROADMAP.md` dan `.superpowers/sdd/2026-11-25-pilot-report/progress.md`.
7. **Jika gagal** — tabel gejala → penyebab → tindakan, merujuk `docs/AKSES-REMOTE-TAILSCALE.md`.

- [ ] **Step 2: Verifikasi perintah dokumen**

Run:
```powershell
Select-String -Path docs/VERIFIKASI-REMOTE-SNMP.md -Pattern 'pilot_connectivity|healthz|tailscale status|ping 192.168.30.1'
```
Expected: semua perintah kunci muncul.

- [ ] **Step 3: Commit**

```powershell
git add docs/AKSES-REMOTE-TAILSCALE.md docs/VERIFIKASI-REMOTE-SNMP.md
git commit -m "docs: preflight dan checklist verifikasi remote SNMP"
```

---

### Task 4: Eksekusi di kantor dan verifikasi end-to-end (menunggu Tim IT)

**Files:**
- Modify: `ROADMAP.md`
- Modify: `.superpowers/sdd/2026-11-25-pilot-report/progress.md`
- Modify: `docs/LAPORAN-DAMPAK.md` (bagian keterbatasan, bila perlu)

**Interfaces:**
- Consumes: semua artefak Task 1–3 dan akses Tailscale yang sudah aktif.
- Produces: bukti SNMP remote nyata dan status pilot yang diperbarui.

- [ ] **Step 1: Minta Tim IT menjalankan Task 1**

Kirim `docs/PESAN-TIM-IT-TAILSCALE.md`. Setelah mereka konfirmasi, minta:
- nama PC kantor,
- screenshot `tailscale status`,
- konfirmasi route `192.168.30.0/24` sudah approved.

- [ ] **Step 2: Pasang Tailscale di laptop dan join tailnet**

Run:
```powershell
winget install --id Tailscale.Tailscale -e --silent --accept-package-agreements --accept-source-agreements
tailscale up --accept-routes
tailscale status
```
Expected: PC kantor terlihat `online`; route `192.168.30.0/24` ada.

- [ ] **Step 3: Preflight jaringan dan SNMP**

Run:
```powershell
ping 192.168.30.1
& "F:\Tugas Kuliah\Magang\kopegtel-venv\Scripts\python.exe" tools\pilot_connectivity.py --device 6
```
Expected: ping reply; `Verdict: ok`; SNMP BERHASIL.

- [ ] **Step 4: Nyalakan NMS dan verifikasi live**

Run:
```powershell
& "F:\Tugas Kuliah\Magang\kopegtel-venv\Scripts\python.exe" backend\serve.py
```
Lalu:
```powershell
Invoke-RestMethod http://127.0.0.1:5000/healthz -NoProxy
```
Expected: `{"status":"ok"}`; device 6 `online`; `sys_descr` `RouterOS CCR1016-12G`.

- [ ] **Step 5: Catat bukti dan commit**

Isi `ROADMAP.md` dan progress SDD dengan:
- waktu verifikasi (WIB),
- output `pilot_connectivity.py`,
- status `/healthz`,
- commit hash,
- catatan bahwa ini akses remote nyata, bukan simulasi.

```powershell
git add ROADMAP.md .superpowers/sdd/2026-11-25-pilot-report/progress.md docs/LAPORAN-DAMPAK.md
git commit -m "docs: bukti verifikasi SNMP remote via Tailscale"
```

- [ ] **Step 6: Hentikan tunnel bila tidak dipakai**

Jika ingin menghemat kuota atau menghentikan akses:
```powershell
tailscale down
```
NMS akan melaporkan device 6 offline; tidak ada data palsu.

---

## Self-Review

- **Spec coverage:** masalah, tujuan, non-tujuan, pendekatan, keamanan, verifikasi, risiko, rollback, dan ketergantungan manusia semuanya punya task.
- **Step scan:** setiap langkah punya perintah/checkable result; tidak ada "handle edge cases" tanpa detail.
- **Type consistency:** parameter script (`AuthKey`, `Subnet`, `Hostname`, `Target`, `DryRun`) dipakai konsisten di dokumen dan test.
- **Review focus:** auth key, route approval, sleep/firewall, `--reset`, subnet, akun beda, dan admin semuanya punya langkah verifikasi/troubleshooting.
- **Proportion:** plan lebih pendek dari implementasi; script dan dokumen ditulis saat eksekusi, bukan ditranskrip di sini.
