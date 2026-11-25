# Akses Remote SNMP MikroTik via Tailscale

Status: siap dijalankan (25 November 2026)
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
2. Install Tailscale dari <https://tailscale.com/download/windows>, lalu login
   dengan akun yang sama seperti laptop pengguna.
3. Jalankan PowerShell **sebagai Administrator**:
   ```powershell
   Set-ItemProperty -Path 'HKLM:\SYSTEM\CurrentControlSet\Services\Tcpip\Parameters' -Name IPEnableRouter -Value 1
   Restart-Computer
   ```
4. Setelah reboot, jalankan:
   ```powershell
   tailscale set --advertise-routes=192.168.30.0/24 --accept-routes=false
   tailscale up --hostname=kopegtel-office --unattended
   ```
5. Buka <https://console.tailscale.com/admin/machines>, pilih
   `kopegtel-office` → **Subnets** → approve `192.168.30.0/24` → **Save**.
6. Kirim ke pengguna: nama PC, screenshot `tailscale status`, dan konfirmasi
   rute sudah approved. Jangan kirim password MikroTik atau auth key di grup.

PC kantor hanya **mengiklankan** rute (`--accept-routes=false`) agar tidak
membuat loop; laptop pengguna yang **menerima** rute (`--accept-routes`).

Estimasi: 10–15 menit setelah PC siap.

## 3. Prasyarat

- Akun Tailscale (paket gratis cukup untuk 2 perangkat).
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
4. Simpan key di password manager. **Jangan** tempel di WhatsApp/Telegram/grup.
5. Setelah setup selesai, key boleh dicabut di halaman yang sama.

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
   tailscale set --advertise-routes=192.168.30.0/24 --accept-routes=false
   tailscale up --hostname=kopegtel-office --unattended
   ```
5. Lanjut ke bagian 7 (approve route).

Catatan: `tailscale up --unattended` membuat Tailscale tetap jalan setelah
user logout. Ini penting untuk PC kantor yang selalu menyala.

## 6. Opsi B: Script

Script ada di `tools/office-tailscale-subnet.ps1`. Script akan:
- mendeteksi Tailscale (install via winget/installer jika belum ada),
- menyalakan service,
- login memakai auth key dari environment variable,
- mengiklankan `192.168.30.0/24`,
- mencetak status dan langkah approve route.

Jalankan dari PowerShell **sebagai Administrator** di folder repo:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
$env:TAILSCALE_AUTH_KEY = 'tskey-auth-...'   # jangan di grup/chat
.\tools\office-tailscale-subnet.ps1 -DryRun
.\tools\office-tailscale-subnet.ps1
```

Jika PC sudah pernah memakai Tailscale dengan konfigurasi lain dan ingin
mengulang dari awal:

```powershell
.\tools\office-tailscale-subnet.ps1 -Reset
```

`-DryRun` hanya mencetak rencana; tidak menginstall, tidak mengubah apa pun,
dan auth key disamarkan sebagai `<REDACTED>`.

## 7. Approve route

Route yang diiklankan belum aktif sampai disetujui:

1. Buka <https://console.tailscale.com/admin/machines>.
2. Cari mesin `kopegtel-office` (atau nama PC kantor).
3. Buka bagian **Subnets**.
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

Expected:

```
Ping : BERHASIL
SNMP : BERHASIL
Verdict: ok
```

Lalu nyalakan NMS:

```powershell
& "F:\Tugas Kuliah\Magang\kopegtel-venv\Scripts\python.exe" backend\serve.py
```

Cek:

```powershell
Invoke-RestMethod http://127.0.0.1:5000/healthz -NoProxy
```

Expected: `{"status":"ok"}`. Di UI, **Monitoring → Hosts → Mikrotik Utama**
harus `online` dan SNMP `sys_descr` `RouterOS CCR1016-12G`.

Checklist verifikasi lengkap (termasuk cek rute, tracert, dan bukti):
`docs/VERIFIKASI-REMOTE-SNMP.md`.

## 10. Troubleshooting

| Gejala | Penyebab umum | Tindakan |
|---|---|---|
| `tailscale status` tidak menampilkan PC kantor | Akun/tailnet berbeda | Login ulang dengan akun yang sama |
| PC kantor terlihat, tapi `ping 192.168.30.1` timeout | Route belum approved | Approve `192.168.30.0/24` di admin console |
| Route sudah approved, ping tetap timeout | IP forwarding Windows belum aktif | Jalankan `IPEnableRouter=1`, reboot |
| Ping ke IP Tailscale PC kantor gagal | PC sleep/mati atau firewall | Cek power plan dan Windows Firewall |
| `tailscale set` tidak dikenal | Tailscale terlalu lama | Update Tailscale ke versi terbaru |
| `tailscale up` access denied | PowerShell bukan Administrator | Buka PowerShell sebagai Administrator |
| SNMP timeout padahal ping OK | Community berubah / ACL / perangkat down | Uji lewat **Edit** perangkat, jangan form Tambah |
| Tailscale lambat/tidak connect | UDP 41641 diblokir, memakai DERP | `tailscale netcheck`; lihat status relay |
| PC kantor logout lalu Tailscale mati | Belum unattended | `tailscale up --unattended` |

Jika ping ke `192.168.30.1` gagal dari PC kantor sendiri, masalahnya bukan
Tailscale: PC tersebut tidak berada di jaringan kantor yang benar.

## 11. Keamanan

- Jangan aktifkan **Funnel** atau **exit node** di PC kantor.
- Jangan buka UDP 161/TCP 8728 ke internet.
- Auth key hanya untuk PC kantor; simpan di password manager, cabut setelah
  tidak dipakai.
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
