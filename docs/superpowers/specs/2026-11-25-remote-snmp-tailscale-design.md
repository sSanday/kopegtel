# Desain: Akses Remote SNMP MikroTik via Tailscale Subnet Router

Tanggal: 25 November 2026
Status: Menunggu persetujuan

## Masalah

NMS berjalan di laptop yang saat ini berada di luar jaringan kantor. Perangkat
`192.168.30.1` (Mikrotik Utama) adalah alamat privat di dalam kantor, sehingga
tidak punya rute balik dari internet. SNMP ke perangkat asli sudah terbukti
bekerja 10/10 saat laptop berada di Wi-Fi kantor; yang hilang hanya jalur masuk
dari luar.

## Tujuan

- Laptop dapat melakukan polling SNMP ke `192.168.30.1` dari mana saja.
- Tidak ada perubahan kode NMS: perangkat 6 tetap memakai IP yang sama.
- Tidak membuka SNMP/API MikroTik langsung ke internet.
- Tidak memakai data sintetis; angka pilot tetap dari perangkat asli.

## Non-tujuan

- Mengganti topologi jaringan kantor.
- Membuka port 161/UDP atau 8728/TCP ke publik.
- Memindahkan NMS ke kantor.

## Pendekatan

Tailscale subnet router di satu PC kantor yang selalu menyala:

1. PC kantor menjalankan Tailscale dan mengiklankan rute
   `192.168.30.0/24` (`--advertise-routes`).
2. Rute disetujui di konsol admin Tailscale.
3. Laptop join tailnet yang sama dan menerima rute (`--accept-routes`).
4. NMS polling `192.168.30.1` seperti biasa; Tailscale yang meneruskan paket.

Tailscale memakai WireGuard, bekerja di belakang NAT/CGNAT, dan tidak
memerlukan port forwarding di MikroTik.

## Komponen

| Komponen | Perubahan |
|---|---|
| PC kantor | Install Tailscale, aktifkan subnet route, jangan sleep |
| Laptop | Install Tailscale, login, `--accept-routes` |
| NMS | Tidak ada perubahan kode; device 6 tidak diubah |
| MikroTik | Tidak ada perubahan konfigurasi |

## Keamanan

- Tailscale terenkripsi WireGuard; tidak ada port inbound di kantor.
- Rute `192.168.30.0/24` hanya diakses anggota tailnet.
- Auth key untuk PC kantor dibuat sekali, disimpan di password manager, dan
  sebaiknya diberi tag `office-router` dengan ACL yang hanya mengizinkan
  perangkat laptop pengguna.
- Setelah setup, cabut auth key yang tidak terpakai.
- Jangan mengaktifkan "exit node" atau "funnel".

## Verifikasi

1. `tailscale status` di laptop menunjukkan PC kantor online.
2. `ping 192.168.30.1` berhasil.
3. `python tools/pilot_connectivity.py --device 6` → verdict `ok`
   (Ping + SNMP berhasil).
4. NMS `/healthz` ok; device 6 berstatus `online`; SNMP `sys_descr`
   `RouterOS CCR1016-12G`.
5. Catat bukti (waktu, output, commit) di `ROADMAP.md` dan progress SDD.

## Risiko dan mitigasi

| Risiko | Mitigasi |
|---|---|
| PC kantor sleep/mati | Set power plan "Never sleep"; pilih PC yang selalu menyala |
| Rute belum di-approve | Approve manual di konsol Tailscale |
| Firewall kantor memblokir Tailscale | Tailscale memakai UDP 41641 outbound; jika diblokir, pakai DERP relay |
| PC kantor tidak berada di subnet 30 | Pastikan PC bisa route ke `192.168.30.0/24` |
| Auth key bocor | Rotate key; cabut akses; ACL ketat |
| Tailscale down | NMS kembali offline; tidak ada data palsu |

## Rollback

- Di PC kantor: `tailscale down` lalu hapus rute di konsol.
- Di laptop: `tailscale down`.
- NMS tidak perlu diubah; perangkat akan kembali `offline` sampai jalur pulih.

## Ketergantungan manusia

Tim IT (atau siapa pun di kantor) harus menjalankan langkah di PC kantor:
install Tailscale, login/auth key, dan aktifkan subnet route. Setelah itu
verifikasi end-to-end dilakukan dari laptop.
