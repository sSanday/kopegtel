# Verifikasi Remote SNMP MikroTik

Status: checklist siap pakai (26 November 2026)
Prasyarat: Tailscale di PC kantor dan laptop sudah aktif, route
`192.168.30.0/24` sudah approved.

> **Penting:** jangan menyalakan NMS sebelum ping dan SNMP lolos. Jika tunnel
> belum siap, NMS akan mencatat perangkat `offline` palsu dan mengotori data
> pilot.

## 0. Urutan cepat

```powershell
tailscale status
tailscale ping kopegtel-office
ping 192.168.30.1
& "F:\Tugas Kuliah\Magang\kopegtel-venv\Scripts\python.exe" tools\pilot_connectivity.py --device 6
```

Jika empat langkah itu lolos, baru nyalakan NMS.

## 1. Sebelum konek

Pastikan Tailscale laptop berjalan dan memakai akun yang sama:

```powershell
Get-Service Tailscale | Select-Object Status, StartType
tailscale status
tailscale ip -4
```

Expected:
- service `Running`,
- daftar peer memuat `kopegtel-office` dengan status `online`,
- laptop punya IP `100.x.y.z`.

Jika `kopegtel-office` tidak muncul, akun/tailnet berbeda. Login ulang dengan
akun yang sama seperti PC kantor.

## 2. Cek rute

```powershell
tailscale status
Get-NetRoute -DestinationPrefix '192.168.30.0/24' -ErrorAction SilentlyContinue
tailscale ping kopegtel-office
```

Expected:
- `tailscale status` menampilkan `kopegtel-office` dan route
  `192.168.30.0/24` (atau label `subnet`),
- `Get-NetRoute` menampilkan rute ke `192.168.30.0/24` lewat interface
  Tailscale,
- `tailscale ping kopegtel-office` mendapat balasan `pong`.

Jika `tailscale ping` gagal, masalahnya di Tailscale (peer offline, firewall,
atau relay). Jika `tailscale ping` berhasil tetapi `ping 192.168.30.1` gagal,
masalahnya di subnet route/IP forwarding.

## 3. Cek jaringan

```powershell
ping 192.168.30.1 -n 4
tracert -d 192.168.30.1
```

Expected:
- 4 balasan dari `192.168.30.1`,
- hop terakhir menuju MikroTik.

Jika ping timeout, cek:
1. route `192.168.30.0/24` sudah approved,
2. IP forwarding Windows di PC kantor sudah aktif,
3. PC kantor tidak sleep,
4. PC kantor memang bisa `ping 192.168.30.1` dari jaringan kantor.

## 4. Cek SNMP

```powershell
& "F:\Tugas Kuliah\Magang\kopegtel-venv\Scripts\python.exe" tools\pilot_connectivity.py --device 6
```

Expected:

```
Ping : BERHASIL
SNMP : BERHASIL
Verdict: ok
```

Jika ping berhasil tetapi SNMP gagal, periksa community tersimpan di device 6.
Jangan menguji lewat form **Tambah perangkat** untuk IP yang sudah terdaftar;
gunakan **Edit → Mikrotik Utama → Uji koneksi** dan biarkan community kosong
agar memakai kredensial tersimpan.

## 5. Cek NMS

Setelah langkah 4 lolos:

```powershell
& "F:\Tugas Kuliah\Magang\kopegtel-venv\Scripts\python.exe" backend\serve.py
```

Di terminal lain:

```powershell
Invoke-RestMethod http://127.0.0.1:5000/healthz -NoProxy
```

Expected: `{"status":"ok"}`.

Di UI:
1. Buka **Monitoring → Hosts**.
2. Pilih **Mikrotik Utama**.
3. Status harus `online`.
4. Detail SNMP harus memuat `sys_descr` `RouterOS CCR1016-12G`.

## 6. Bukti yang disimpan

Catat di `ROADMAP.md` dan
`.superpowers/sdd/2026-11-25-pilot-report/progress.md`:

| Item | Isi |
|---|---|
| Waktu verifikasi | `YYYY-MM-DD HH:MM WIB` |
| `tailscale status` | peer `kopegtel-office` online |
| `ping 192.168.30.1` | 4/4 balasan |
| `pilot_connectivity.py --device 6` | `Verdict: ok` |
| `/healthz` | `{"status":"ok"}` |
| Status device 6 | `online`, SNMP `RouterOS CCR1016-12G` |
| Commit hash | hash dokumentasi/bukti |

Jangan menulis angka dampak dari sesi ini sebagai angka final pilot; bukti ini
hanya membuktikan jalur remote bekerja. Angka final tetap memakai jendela data
bersih 26 Nov–12 Des.

## 7. Jika gagal

| Gejala | Penyebab | Tindakan |
|---|---|---|
| `kopegtel-office` tidak muncul | Akun berbeda | Login ulang dengan akun yang sama |
| Peer muncul, `tailscale ping` gagal | PC kantor sleep/mati/firewall | Cek PC, power plan, firewall |
| `tailscale ping` OK, `ping 192.168.30.1` gagal | Route belum approved | Approve di admin console |
| Route approved, ping tetap gagal | IP forwarding Windows | `IPEnableRouter=1`, reboot |
| Ping OK, SNMP timeout | Community/ACL/perangkat | Uji lewat Edit, cek device 6 |
| Semua gagal setelah perubahan | Perlu rollback | Lihat bagian 12 `docs/AKSES-REMOTE-TAILSCALE.md` |

## 8. Setelah verifikasi

- Biarkan NMS berjalan selama jam kantor.
- Jangan mengubah IP device 6.
- Jika tunnel dimatikan, hentikan NMS atau biarkan device 6 `offline`; jangan
  menganggapnya insiden nyata.
- Jalankan `tools/pilot_report.py` setelah data bersih terkumpul.
