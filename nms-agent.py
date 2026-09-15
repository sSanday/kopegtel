import os
import time
import argparse
import requests
import psutil
import socket

# ==========================================
# KONFIGURASI AGENT (bisa via argumen/ENV)
# ==========================================
# URL dari Dashboard NMS
DEFAULT_URL = os.environ.get("NMS_URL", "http://127.0.0.1:5000/api/agent/report")
# Kunci auth agent — WAJIB sama dengan AGENT_API_KEY di .env server (jika diaktifkan)
API_KEY = os.environ.get("AGENT_API_KEY", "")
# IP identitas agent. Kosongkan ("") = deteksi otomatis.
AGENT_IP = os.environ.get("AGENT_IP", "")
# Interval pengiriman data (detik). Default 60 (dulu 5 — terlalu sering, membanjiri DB)
def _get_interval():
    try:
        return max(5, int(os.environ.get("AGENT_INTERVAL", "60")))
    except (ValueError, TypeError):
        return 60
INTERVAL = _get_interval()
# ==========================================


def get_ip_address():
    """Deteksi IP lokal secara otomatis, atau pakai AGENT_IP jika diisi manual."""
    if AGENT_IP:
        return AGENT_IP
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return socket.gethostname()

def main():
    ap = argparse.ArgumentParser(description="NMS Agent")
    ap.add_argument("--server", default=DEFAULT_URL)
    ap.add_argument("--interval", type=int, default=INTERVAL)
    ap.add_argument("--api-key", default=API_KEY)
    a = ap.parse_args()

    my_ip = get_ip_address()
    headers = {"X-API-Key": a.api_key} if a.api_key else {}
    print(f"NMS Agent mulai berjalan...")
    print(f"Melapor sebagai Host: {my_ip}")
    print(f"Target URL: {a.server} tiap {a.interval}s")
    print(f"Auth API key: {'aktif' if a.api_key else 'NONAKTIF (aktifkan AGENT_API_KEY di server!)'}")
    print("-" * 40)

    last_net = psutil.net_io_counters()
    last_time = time.time()

    while True:
        try:
            # Ambil data sistem
            cpu = psutil.cpu_percent(interval=1)  # memberi jeda ~1 detik

            now_net = psutil.net_io_counters()
            now_time = time.time()
            time_diff = now_time - last_time

            # Hitung Mbps (Megabit per detik) = bytes * 8 / 1.048.576 / detik
            if time_diff > 0:
                net_in = ((now_net.bytes_recv - last_net.bytes_recv) * 8) / (1024 * 1024 * time_diff)
                net_out = ((now_net.bytes_sent - last_net.bytes_sent) * 8) / (1024 * 1024 * time_diff)
            else:
                net_in = 0.0
                net_out = 0.0

            last_net = now_net
            last_time = now_time

            ram = psutil.virtual_memory().percent
            disk = psutil.disk_usage('/').percent

            payload = {
                "host": my_ip,
                "cpu": cpu,
                "ram": ram,
                "disk": disk,
                "net_in": round(net_in, 2),
                "net_out": round(net_out, 2)
            }

            # Kirim ke NMS Dashboard
            res = requests.post(a.server, json=payload, headers=headers, timeout=8)

            if res.status_code == 200:
                print(f"[OK] {my_ip} | CPU:{cpu:05.1f}% RAM:{ram:05.1f}% | IN:{net_in:05.2f}Mbps OUT:{net_out:05.2f}Mbps")
            elif res.status_code == 403:
                print("[ERR] API key ditolak server. Samakan AGENT_API_KEY dengan server!")
                time.sleep(min(a.interval, 60))
            else:
                print(f"[ERR] Server merespon dengan kode: {res.status_code}")

        except requests.exceptions.ConnectionError:
            print(f"[ERR] Gagal terhubung ke {a.server}. Pastikan NMS Dashboard menyala.")
        except Exception as e:
            print(f"[ERR] Terjadi kesalahan: {e}")

        time.sleep(a.interval)

if __name__ == "__main__":
    main()
