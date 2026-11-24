"""Diagnostik konektivitas pilot NMS (ping + SNMP dari proses yang sama).

Alat ini menjawab satu pertanyaan: apakah kegagalan "Uji Koneksi" berasal dari
jaringan, SNMP, atau proses Python yang berjalan pada integritas rendah.

Contoh:
    .\\.venv\\Scripts\\python.exe tools\\pilot_connectivity.py --device 6 --device 7

Tidak pernah mencetak community/password. Exit code 0 bila semua perangkat
yang diminta lolos, 1 bila ada yang gagal.
"""

import argparse
import ipaddress
import os
import socket
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR / "backend"))

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - venv belum lengkap
    load_dotenv = None


def same_subnet(local_ip, target_ip, prefix_len=24):
    """True bila target berada di subnet lokal (heuristik /24 bila mask tak ada)."""
    try:
        net = ipaddress.ip_network(f"{local_ip}/{prefix_len}", strict=False)
        return ipaddress.ip_address(target_ip) in net
    except ValueError:
        return False


def verdict(ping_ok, snmp_ok, snmp_enabled, local_ip, target_ip, prefix_len=24):
    """Klasifikasi hasil: ok | snmp_fail | unreachable | wrong_network."""
    if ping_ok and (snmp_ok or not snmp_enabled):
        return "ok"
    if ping_ok:
        return "snmp_fail"
    if same_subnet(local_ip, target_ip, prefix_len):
        return "unreachable"
    return "wrong_network"


VERDICT_TEXT = {
    "ok": "OK - jalur jaringan dan SNMP berhasil.",
    "snmp_fail": "Ping OK, SNMP gagal - cek community/versi/ACL SNMP di perangkat.",
    "unreachable": "Ping gagal walau satu subnet - perangkat mati atau ICMP diblokir.",
    "wrong_network": "Beda jaringan - sambungkan laptop ke Wi-Fi kantor tempat perangkat berada.",
}


def local_ip_for(target_ip):
    """IP lokal yang dipakai untuk mencapai target (tanpa mengirim paket)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((target_ip, 161))
        return sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()


def prefix_len_for(local_ip):
    """Ambil prefix dari netmask antarmuka; fallback /24."""
    if not local_ip:
        return 24
    try:
        import psutil

        for _name, addrs in psutil.net_if_addrs().items():
            for addr in addrs:
                if addr.family == socket.AF_INET and addr.address == local_ip:
                    netmask = addr.netmask or "255.255.255.0"
                    return ipaddress.ip_network(f"0.0.0.0/{netmask}").prefixlen
    except Exception:
        pass
    return 24


def process_integrity():
    """Label integritas token proses saat ini (Windows); None di luar Windows."""
    if os.name != "nt":
        return None
    try:
        out = subprocess.run(
            ["whoami", "/groups"], capture_output=True, text=True, timeout=10
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    if "Low Mandatory Level" in out:
        return "Low"
    if "Medium Mandatory Level" in out:
        return "Medium"
    if "High Mandatory Level" in out:
        return "High"
    return "Tidak diketahui"


def snmp_config(device):
    import db

    return {
        "ip": device["ip"],
        "version": device["snmp_version"],
        "port": device["snmp_port"],
        "community": db.decrypt_secret(device["snmp_community"]),
        "v3_user": device["snmp_v3_user"],
        "v3_auth_proto": device["snmp_v3_auth_proto"],
        "v3_auth_password": db.decrypt_secret(device["snmp_v3_auth_password"]),
        "v3_priv_proto": device["snmp_v3_priv_proto"],
        "v3_priv_password": db.decrypt_secret(device["snmp_v3_priv_password"]),
    }


def check_device(device):
    from collectors import ping as ping_collector
    from collectors import snmp as snmp_collector

    local_ip = local_ip_for(device["ip"])
    prefix_len = prefix_len_for(local_ip)
    ping_result = ping_collector.check_ping(device["ip"])
    snmp_result = None
    if device["snmp_enabled"]:
        try:
            snmp_result = snmp_collector.check_snmp(snmp_config(device))
        except Exception as exc:  # hasil terstruktur, jangan hentikan alat
            snmp_result = {"ok": False, "reason": "error", "detail": str(exc)}

    snmp_ok = None if snmp_result is None else bool(snmp_result.get("ok"))
    status = verdict(
        bool(ping_result.get("ok")), snmp_ok, bool(device["snmp_enabled"]),
        local_ip or "", device["ip"], prefix_len,
    )
    return {
        "device": device,
        "local_ip": local_ip,
        "prefix_len": prefix_len,
        "ping": ping_result,
        "snmp": snmp_result,
        "status": status,
    }


def print_result(result):
    device = result["device"]
    print(f"\n== {device['name']} (id {device['id']}) {device['ip']} ==")
    local = result["local_ip"] or "tidak diketahui"
    print(f"IP lokal untuk target: {local}/{result['prefix_len']}")

    ping = result["ping"]
    if ping.get("ok"):
        print(f"Ping : OK ({ping.get('latency_ms')} ms)")
    else:
        print(f"Ping : GAGAL ({ping.get('reason')})")

    snmp = result["snmp"]
    if not device["snmp_enabled"]:
        print("SNMP : dilewati (nonaktif di NMS)")
    elif snmp and snmp.get("ok"):
        print(f"SNMP : OK ({snmp.get('sys_descr') or 'tanpa sysDescr'})")
    else:
        detail = (snmp or {}).get("detail") or (snmp or {}).get("reason")
        print(f"SNMP : GAGAL ({detail})")

    print(f"Verdict: {result['status']} — {VERDICT_TEXT[result['status']]}")


def print_environment():
    print("== Lingkungan proses ==")
    print(f"Executable : {sys.executable}")
    print(f"Base Python: {sys.base_prefix}")
    integrity = process_integrity()
    print(f"Integritas : {integrity or 'tidak diperiksa'}")
    if integrity == "Low":
        print(
            "PERINGATAN: proses berjalan pada integritas Low. Windows memblokir "
            "ICMP dan UDP SNMP. Jalankan lewat venv di luar folder proyek yang "
            "berlabel Low (lihat README bagian Troubleshooting)."
        )
    if "WindowsApps" in sys.base_prefix:
        print(
            "PERINGATAN: Python berasal dari Microsoft Store (AppContainer). "
            "Gunakan Python biasa untuk venv NMS."
        )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--device",
        type=int,
        action="append",
        dest="devices",
        help="ID perangkat (boleh diulang); default semua perangkat.",
    )
    args = parser.parse_args(argv)

    if load_dotenv is not None:
        load_dotenv(BASE_DIR / ".env")

    import db

    global_integrity = process_integrity()
    print_environment()

    if args.devices:
        devices = [db.get_device(device_id, include_secrets=True) for device_id in args.devices]
        devices = [device for device in devices if device is not None]
    else:
        devices = [
            db.get_device(device["id"], include_secrets=True)
            for device in db.query_devices(per_page=100)["devices"]
        ]

    if not devices:
        print("\nTidak ada perangkat yang diperiksa.")
        return 1

    failed = 0
    for device in devices:
        result = check_device(device)
        print_result(result)
        if result["status"] != "ok":
            failed += 1

    if global_integrity == "Low" and failed:
        print("\nCatatan: integritas Low membuat semua ping/SNMP gagal; perbaiki venv dulu.")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
