# Pesan untuk Tim IT — Akses Remote SNMP via Tailscale

Copy-paste pesan di bawah ke WhatsApp/Telegram pribadi Tim IT. Jangan sertakan
auth key di grup.

---

Halo Tim IT, saya butuh bantuan untuk monitoring SNMP MikroTik
(`192.168.30.1`) dari luar kantor. Tidak perlu buka port atau ubah MikroTik;
cukup satu PC kantor yang selalu menyala sebagai jalur Tailscale.

**Yang dibutuhkan**
- 1 PC Windows di kantor yang selalu menyala (tidak sleep).
- Akses Administrator di PC tersebut.
- PC bisa `ping 192.168.30.1` dari jaringan kantor.

**Langkah (10–15 menit)**
1. Install Tailscale dari <https://tailscale.com/download/windows>, lalu login
   dengan akun yang saya kirim terpisah. Kalau tidak bisa login browser, saya
   kirim auth key lewat jalur pribadi.
2. Buka PowerShell **sebagai Administrator**, lalu jalankan:
   ```powershell
   Set-ItemProperty -Path 'HKLM:\SYSTEM\CurrentControlSet\Services\Tcpip\Parameters' -Name IPEnableRouter -Value 1
   Restart-Computer
   ```
3. Setelah reboot, buka PowerShell **sebagai Administrator** lagi:
   ```powershell
   tailscale set --advertise-routes=192.168.30.0/24 --accept-routes=false
   tailscale up --hostname=kopegtel-office --unattended
   ```
   Jika memakai auth key, tambahkan `--auth-key=...` pada perintah
   `tailscale up` (key saya kirim pribadi, jangan di grup).
4. Buka <https://console.tailscale.com/admin/machines>, pilih
   `kopegtel-office` → **Subnets** → centang `192.168.30.0/24` → **Save**.
5. Kirim balik:
   - nama PC,
   - screenshot `tailscale status`,
   - konfirmasi route `192.168.30.0/24` sudah approved.

**Mohon jangan kirim**
- password MikroTik,
- auth key Tailscale,
- screenshot yang memuat key tersebut.

Kalau PC kantor sudah punya Tailscale dengan konfigurasi lain, kabari saya
dulu supaya tidak menimpa konfigurasi yang ada. Terima kasih.

---

## Catatan untuk pengguna

- Auth key dibuat di <https://login.tailscale.com/admin/settings/keys> dan
  dikirim hanya ke Tim IT lewat jalur pribadi.
- Setelah setup selesai, cabut auth key di halaman yang sama.
- Panduan lengkap ada di `docs/AKSES-REMOTE-TAILSCALE.md`.
