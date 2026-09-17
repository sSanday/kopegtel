import os
import time
import argparse
import requests
import psutil
import socket


DEFAULT_URL = os.environ.get("NMS_URL", "http://127.0.0.1:5000/api/agent/report")

API_KEY = os.environ.get("AGENT_API_KEY", "")

AGENT_IP = os.environ.get("AGENT_IP", "")

def _get_interval():
    try:
        return max(5, int(os.environ.get("AGENT_INTERVAL", "60")))
    except (ValueError, TypeError):
        return 60
INTERVAL = _get_interval()


def get_ip_address():
    if AGENT_IP:
        return AGENT_IP
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
        finally:
            s.close()

        parts = ip.split(".")
        if len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts) \
                and not ip.startswith("127."):
            return ip
    except Exception:
        pass
    try:
        resolved = socket.gethostbyname(socket.gethostname())
        if not resolved.startswith("127."):
            return resolved
    except Exception:
        pass
    return socket.gethostname()

def main():
    ap = argparse.ArgumentParser(description="NMS Agent")
    ap.add_argument("--server", default=DEFAULT_URL)
    ap.add_argument("--interval", type=int, default=INTERVAL)
    ap.add_argument("--api-key", default=API_KEY)
    ap.add_argument("--host-ip", default=AGENT_IP,
                    help="Identitas IP pelapor (lebih diutamakan dari AGENT_IP). "
                         "Wajib sama dengan IP yang terdaftar di dashboard.")
    a = ap.parse_args()


    if a.interval is None or a.interval < 5:
        print(f"[WARN] --interval {a.interval} tidak valid, dipakai 5 detik (minimal).")
        a.interval = 5

    my_ip = a.host_ip or get_ip_address()
    headers = {"X-API-Key": a.api_key} if a.api_key else {}
    print("NMS Agent mulai berjalan...")
    print(f"Melapor sebagai Host: {my_ip}")
    print(f"Target URL: {a.server} tiap {a.interval}s")
    print(f"Auth API key: {'aktif' if a.api_key else 'NONAKTIF (aktifkan AGENT_API_KEY di server!)'}")
    print("-" * 40)

    last_net = psutil.net_io_counters()
    last_time = time.time()

    while True:
        try:

            cpu = psutil.cpu_percent(interval=1)

            now_net = psutil.net_io_counters()
            now_time = time.time()
            time_diff = now_time - last_time


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


            res = requests.post(a.server, json=payload, headers=headers, timeout=8)

            if res.status_code == 200:
                print(f"[OK] {my_ip} | CPU:{cpu:05.1f}% RAM:{ram:05.1f}% | IN:{net_in:05.2f}Mbps OUT:{net_out:05.2f}Mbps")
            elif res.status_code == 403:
                print("[ERR] API key ditolak server. Samakan AGENT_API_KEY dengan server!")
                time.sleep(min(a.interval, 60))
            else:


                try:
                    detail = (res.text or "").strip()[:200]
                except Exception:
                    detail = ""
                print(f"[ERR] Server merespon dengan kode: {res.status_code} {detail}")

        except requests.exceptions.ConnectionError:
            print(f"[ERR] Gagal terhubung ke {a.server}. Pastikan NMS Dashboard menyala.")
        except Exception as e:
            print(f"[ERR] Terjadi kesalahan: {e}")

        time.sleep(a.interval)

if __name__ == "__main__":
    main()
