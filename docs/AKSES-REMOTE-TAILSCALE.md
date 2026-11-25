# Akses Remote SNMP MikroTik via Tailscale

Status: siap dijalankan (revisi 26 November 2026 setelah code review)
Tujuan: laptop di luar kantor bisa polling SNMP ke MikroTik asli `192.168.30.1`
tanpa port forwarding dan tanpa mengubah kode NMS.

## 1. Ringkasan

`192.168.30.1` adalah alamat privat di dalam kantor. Dari internet, alamat itu
tidak punya rute balik. Solusinya: satu PC kantor menjadi **subnet router**
Tailscale yang mengiklankan rute `192.168.30.0/24`. Laptop join tailnet yang
sama, lalu NMS polling seperti biasa.

```
Laptop (di luar kantor)
   │  Tailscale (WireGuard)
   ▼
PC kantor (subnet router, selalu menyala)
   │  LAN kantor
   ▼
MikroTik 192.168.30.1
```

Tidak ada UDP 161 atau TCP 8728 yang dibuka ke internet.

## 2. Untuk Tim IT (ringkas)

1. Siapkan satu PC Windows di kantor yang **selalu menyala** dan bisa
   `ping 192.168.30.1`.
2. Jalankan PowerShell **sebagai Administrator**, aktifkan IP forwarding:
   ```powershell
   Set-ItemProperty -Path 'HKLM:\SYSTEM\CurrentControlSet\Services\Tcpip\Parameters' -Name IPEnableRouter -Value 1
   Restart-Computer
   ```
3. Install Tailscale dari <https://tailscale.com/download/windows>, lalu login
   dengan akun yang sama seperti laptop pengguna. Jika login browser tidak
   memungkinkan, gunakan auth key lewat jalur pribadi (bagian 4).
4. Setelah PC menyala lagi dan Tailscale terpasang, jalankan:
   ```powershell
   tailscale set --hostname=kopegtel-office --accept-routes=false --unattended --advertise-routes=192.168.30.0/24
   ```
   `tailscale set` hanya mengubah preferensi yang disebut dan aman diulang.
   Jika PC **belum login**, jangan menjalankan `tailscale up` hanya dengan
   `--auth-key`: sertakan juga keempat preferensi di atas, atau gunakan script
   bagian 6. `tailscale up` menuntut **semua** preferensi non-default disebut
   setiap kali.
5. Buka <https://console.tailscale.com/admin/machines>, pilih
   `kopegtel-office` → **Edit route settings** → approve `192.168.30.0/24`
   → **Save**.
6. Kirim ke pengguna: nama PC, screenshot `tailscale status`, dan konfirmasi
   rute sudah approved. Jangan kirim password MikroTik atau auth key di grup.

PC kantor hanya **mengiklankan** rute (`--accept-routes=false`) agar tidak
membuat loop; laptop pengguna yang **menerima** rute (`--accept-routes`).

Estimasi: 10–15 menit setelah PC siap dan reboot.

## 3. Prasyarat

- Akun Tailscale. Paket Personal gratis sudah cukup: perangkat pengguna tak
  terbatas dan hingga 6 pengguna (cukup untuk PC kantor + laptop). Paket
  Personal ditujukan untuk penggunaan non-komersial; untuk pemakaian kantor
  jangka panjang, gunakan akun/plan yang disetujui Tim IT.
- PC kantor Windows 10/11 atau Windows Server 2016+, akses Administrator.
- PC kantor berada di jaringan kantor dan bisa `ping 192.168.30.1`.
- PC kantor tidak sleep/hibernate (lihat bagian 10).
- Laptop pengguna bisa install Tailscale.

## 4. Membuat auth key

Auth key dipakai agar PC kantor bisa login tanpa browser.

1. Buka <https://login.tailscale.com/admin/settings/keys>.
2. Pilih **Generate auth key**.
3. Isi:
   - Description: `kopegtel-office-router`
   - Reusable: **ya** (atau one-off jika hanya sekali pakai)
   - Ephemeral: **tidak**
   - Expiry: 90 hari
   - Tags: opsional `tag:office-router` (hanya jika TagOwners sudah diatur)
4. Simpan key di password manager. **Jangan** tempel di WhatsApp/Telegram/grup
   atau di command line.
5. Saat menjalankan script, tulis key ke file sementara lewat Notepad, lalu
   hapus file itu setelah setup (bagian 6).
6. Setelah setup selesai, key boleh dicabut di halaman yang sama.

## 5. Opsi A: GUI

1. Install Tailscale dari <https://tailscale.com/download/windows>.
2. Klik ikon Tailscale di system tray → **Log in** → pakai akun yang sama
   dengan laptop.
3. Buka PowerShell **sebagai Administrator**, aktifkan IP forwarding:
   ```powershell
   Set-ItemProperty -Path 'HKLM:\SYSTEM\CurrentControlSet\Services\Tcpip\Parameters' -Name IPEnableRouter -Value 1
   Restart-Computer
   ```
4. Setelah reboot, jalankan:
   ```powershell
   tailscale set --hostname=kopegtel-office --accept-routes=false --unattended --advertise-routes=192.168.30.0/24
   ```
5. Lanjut ke bagian 7 (approve route).

Catatan: `tailscale set --unattended` membuat Tailscale tetap jalan setelah
user logout. Ini penting untuk PC kantor yang selalu menyala. `tailscale set`
aman diulang dan tidak menuntut flag lain disebut ulang.

## 6. Opsi B: Script

Script ada di `tools/office-tailscale-subnet.ps1`. Script akan:
- memeriksa IP forwarding Windows dan berhenti dengan instruksi bila belum aktif,
- mendeteksi Tailscale (install via winget/installer jika belum ada),
- menyalakan service,
- membaca status backend (`tailscale status --json`),
- jika node belum `Running` (misalnya `NeedsLogin` atau `Stopped`): login memakai
  auth key dari file atau prompt aman (bukan command line) dan menjalankan
  `tailscale up` dengan hostname, `--accept-routes=false`, `--unattended`, dan
  `--advertise-routes=192.168.30.0/24` sekaligus,
- jika node sudah `Running`: melewati `tailscale up`,
- selalu menjalankan `tailscale set` dengan empat preferensi yang sama agar
  hasil akhirnya idempoten,
- mencetak status dan langkah approve route.

Jalankan dari PowerShell **sebagai Administrator** di folder repo. Simpan auth
key di file sementara lewat Notepad (jangan lewat command line agar tidak
tersimpan di history):

```powershell
Set-ExecutionPolicy -Scope Process Bypass
notepad C:\Users\<nama-anda>\tailscale-auth.txt   # tempel key, simpan, tutup
.\tools\office-tailscale-subnet.ps1 -AuthKeyFile C:\Users\<nama-anda>\tailscale-auth.txt -DryRun
.\tools\office-tailscale-subnet.ps1 -AuthKeyFile C:\Users\<nama-anda>\tailscale-auth.txt
Remove-Item C:\Users\<nama-anda>\tailscale-auth.txt
```

Jika status backend sudah `Running`, auth key tidak wajib; script akan melewati
`tailscale up` dan langsung menjalankan `tailscale set`. Jika status
`NeedsLogin`/`Stopped`, script menjalankan `tailscale up` dengan keempat
preferensi sekaligus. Tanpa `-AuthKeyFile`, key diminta lewat prompt
`Read-Host -AsSecureString`; saat `NeedsLogin`, key wajib diisi.

Script tidak menerima auth key lewat argumen command line. Gunakan
`-AuthKeyFile` atau prompt.

Script tidak mencetak auth key. Perintah `tailscale up` memakai
`--auth-key=file:<path>` dan file sementara dihapus di blok `finally`.

**Perhatian:** setiap run menulis ulang empat preferensi di atas. Jika PC kantor
sudah mengiklankan subnet lain atau memakai hostname khusus, catat dulu
konfigurasinya. Script akan menggantinya; `-Reset` lebih luas lagi karena
mengembalikan preferensi lain yang tidak disebut ke default. Untuk PC yang
dipakai bersama, kabari Tim IT sebelum menjalankan.

Jika PC sudah pernah memakai Tailscale dengan preferensi lain dan ingin
mengulang dari awal:

```powershell
.\tools\office-tailscale-subnet.ps1 -Reset
```

`-Reset` hanya mereset preferensi Tailscale, **bukan** mengganti akun/tailnet.
Untuk akun berbeda, jalankan `tailscale logout` lalu login ulang dengan akun
yang benar.

`-DryRun` hanya mencetak rencana; tidak menginstall, tidak mengubah registry,
tidak menjalankan `tailscale`, dan auth key disamarkan sebagai
`--auth-key=file:<REDACTED>`.

## 7. Approve route

Route yang diiklankan belum aktif sampai disetujui:

1. Buka <https://console.tailscale.com/admin/machines>.
2. Cari mesin `kopegtel-office` (atau nama PC kantor).
3. Buka **Edit route settings** (sebagian tampilan menyebutnya **Subnets**).
4. Centang `192.168.30.0/24` → **Save**.

Jika memakai `autoApprovers` di ACL, langkah ini bisa otomatis. Untuk pilot,
approve manual lebih mudah.

## 8. Laptop pengguna

1. Install Tailscale di laptop dari <https://tailscale.com/download/windows>.
2. Login dengan akun yang **sama**.
3. Windows biasanya otomatis menerima subnet route. Jika tidak:
   ```powershell
   tailscale set --accept-routes
   ```
4. Cek:
   ```powershell
   tailscale status
   ping 192.168.30.1
   ```
   Expected: `kopegtel-office` terlihat `online`; ping balasan dari
   `192.168.30.1`.

## 9. Verifikasi SNMP

Dari folder repo:

```powershell
& "F:\Tugas Kuliah\Magang\kopegtel-venv\Scripts\python.exe" tools\pilot_connectivity.py --device 6
```

Expected (angka dan sysDescr bisa berbeda):

```
Ping : OK (12.3 ms)
SNMP : OK (RouterOS CCR1016-12G)
Verdict: ok — OK - jalur jaringan dan SNMP berhasil.
```

Lalu nyalakan NMS:

```powershell
& "F:\Tugas Kuliah\Magang\kopegtel-venv\Scripts\python.exe" backend\serve.py --port 5000
```

Cek:

```powershell
Invoke-RestMethod http://127.0.0.1:5000/healthz
```

Expected: `{"status":"ok"}`. Di UI, **Pemantauan → Daftar Host → Mikrotik
Utama** harus `online` dan SNMP `sys_descr` `RouterOS CCR1016-12G`.

Checklist verifikasi lengkap (termasuk cek rute, tracert, dan bukti):
`docs/VERIFIKASI-REMOTE-SNMP.md`.

## 10. Troubleshooting

| Gejala | Penyebab umum | Tindakan |
|---|---|---|
| `tailscale status` tidak menampilkan PC kantor | Akun/tailnet berbeda | `tailscale logout`, login ulang dengan akun yang sama |
| PC kantor terlihat, tapi `ping 192.168.30.1` timeout | Route belum approved | Approve `192.168.30.0/24` di **Edit route settings** |
| Route sudah approved, ping tetap timeout | IP forwarding Windows belum aktif | Jalankan `IPEnableRouter=1`, reboot |
| Ping ke IP Tailscale PC kantor gagal | PC sleep/mati atau firewall | `powercfg /change standby-timeout-ac 0`; cek Windows Firewall |
| `tailscale up` error “requires mentioning all non-default flags” | Preferensi non-default tidak disebut semua | Sebutkan `--hostname`, `--accept-routes=false`, `--unattended`, `--advertise-routes` saat `up`, atau jalankan script bagian 6 |
| `tailscale set` tidak dikenal | Tailscale terlalu lama | Update Tailscale ke versi terbaru |
| `tailscale up` access denied | PowerShell bukan Administrator | Buka PowerShell sebagai Administrator |
| SNMP timeout padahal ping OK | Community berubah / ACL / perangkat down | Uji lewat **Edit** perangkat, jangan form Tambah |
| Tailscale lambat/tidak connect | UDP 41641 diblokir, memakai DERP | `tailscale netcheck`; lihat status relay |
| PC kantor logout lalu Tailscale mati | Belum unattended | `tailscale set --unattended` |

Jika ping ke `192.168.30.1` gagal dari PC kantor sendiri, masalahnya bukan
Tailscale: PC tersebut tidak berada di jaringan kantor yang benar.

Power plan PC kantor (PowerShell sebagai Administrator):

```powershell
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
powercfg /change monitor-timeout-ac 30
```

## 11. Keamanan

- Jangan aktifkan **Funnel** atau **exit node** di PC kantor.
- Jangan buka UDP 161/TCP 8728 ke internet.
- Auth key hanya untuk PC kantor; simpan di password manager, kirim lewat
  file/prompt pribadi, dan cabut setelah tidak dipakai. Jangan menaruh key di
  `$env:TAILSCALE_AUTH_KEY = 'tskey-...'` atau argumen command line karena
  tersimpan di history PowerShell.
- Gunakan tag `tag:office-router` dan ACL yang hanya mengizinkan laptop
  pengguna mengakses `192.168.30.0/24`.
- Tagged device tidak terkena key expiry; jika tidak memakai tag, matikan
  key expiry untuk PC kantor agar tidak tiba-tiba logout.
- Review perangkat di admin console secara berkala; hapus perangkat yang tidak
  dikenal.

Contoh ACL minimal (ganti email dengan akun Anda):

```json
{
  "grants": [
    {
      "src": ["email-anda@example.com"],
      "dst": ["192.168.30.0/24"],
      "ip": ["*:*"]
    }
  ]
}
```

## 12. Rollback

Di PC kantor:

```powershell
tailscale set --advertise-routes=
tailscale down
```

Di laptop:

```powershell
tailscale down
```

Lalu hapus perangkat `kopegtel-office` dari admin console jika sudah tidak
dipakai. NMS akan melaporkan device 6 `offline`; tidak ada data palsu yang
dibuat.

## 13. Bukti yang disimpan

Setelah berhasil, simpan:

- waktu verifikasi (WIB),
- output `tailscale status`,
- output `tailscale set` (empat preferensi) dan `tailscale debug prefs`,
- output `ping 192.168.30.1`,
- output `tools/pilot_connectivity.py --device 6`,
- status `/healthz`,
- commit hash perubahan dokumentasi.

Masukkan ke `ROADMAP.md` dan
`.superpowers/sdd/2026-11-25-pilot-report/progress.md`.

## Referensi

- Subnet routers: <https://tailscale.com/kb/1019/subnets>
- Auth keys: <https://tailscale.com/kb/1085/auth-keys>
- Tailscale CLI `up`: <https://tailscale.com/docs/reference/tailscale-cli/up>
- Install Windows: <https://tailscale.com/docs/install/windows>
