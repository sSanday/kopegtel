"""Entry produksi NMS Kopegtel.

Produksi (waitress, poller dalam proses yang sama):
    python backend/serve.py

Pengembangan (Flask dev server di bawah flag --dev):
    python backend/serve.py --dev

Opsi: --host/--port/--threads (default dari env HOST/PORT/WAITRESS_THREADS).
Satu instance saja per database: lease di tabel instance_lease (heartbeat
10 detik; lease basi > 30 detik diambil alih). Migrasi skema dan seed admin
baru dijalankan setelah lease didapat, jadi instance kedua tidak memigrasikan
database saat instance pertama masih hidup.

Environment:
    WAITRESS_THREADS  jumlah thread waitress (default 8)
    NMS_DB_PATH       lokasi database (opsional; default backend/nms.db)
"""
import argparse
import os
import sys
import threading
import time

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BACKEND_DIR)

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(BASE_DIR, ".env"))

import config  # noqa: E402

config.setup_logging()
config.validate_or_exit()

import logging  # noqa: E402

import db  # noqa: E402
import poller  # noqa: E402
import app as app_module  # noqa: E402

log = logging.getLogger("nms.serve")

_LEASE_HEARTBEAT_SECONDS = 10


def _lease_watchdog(token):
    while True:
        time.sleep(_LEASE_HEARTBEAT_SECONDS)
        try:
            ok = db.heartbeat_instance_lease(token)
        except Exception:
            ok = False
        if not ok:
            log.critical("Lease instance hilang (instance lain mengambil alih). Menghentikan proses.")
            os._exit(1)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Server NMS Kopegtel")
    parser.add_argument("--dev", action="store_true", help="mode pengembangan (Flask dev server), bukan untuk produksi")
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "5500")))
    parser.add_argument("--threads", type=int, default=int(os.environ.get("WAITRESS_THREADS", "8")))
    args = parser.parse_args(argv)

    token = db.acquire_instance_lease()
    if token is None:
        status = db.instance_lease_status() or {}
        log.critical(
            "Instance NMS lain sedang berjalan (PID %s, heartbeat %s). "
            "Hentikan instance tersebut atau tunggu lease kedaluwarsa (>30 detik tanpa heartbeat).",
            status.get("pid"), status.get("heartbeat"),
        )
        print(
            "Instance NMS lain sedang berjalan (PID "
            f"{status.get('pid')}, heartbeat {status.get('heartbeat')}). Keluar.",
            file=sys.stderr,
        )
        return 1

    ingest = None
    flow = None
    try:
        # Heartbeat dulu, baru inisialisasi berat: lease harus terus segar
        # selama migrasi skema berjalan.
        watchdog = threading.Thread(target=_lease_watchdog, args=(token,), daemon=True, name="lease-watchdog")
        watchdog.start()

        # Impor app tidak menyentuh database. Migrasi skema + seed admin baru
        # dijalankan di sini, setelah lease dipegang, sehingga instance kedua
        # tidak bisa memigrasikan DB saat instance pertama masih hidup.
        app_module.bootstrap()

        poller.start_poller()
        from ingest import INGEST
        from flow import FLOW

        ingest, flow = INGEST, FLOW
        try:
            ingest.start()
        except Exception:
            log.exception("Penerima syslog/trap gagal dimulai; aplikasi tetap berjalan.")
        try:
            flow.start()
        except Exception:
            log.exception("Penerima flow gagal dimulai; aplikasi tetap berjalan.")
        if args.dev:
            log.warning("Mode --dev: menggunakan server pengembangan Flask, jangan dipakai di produksi.")
            app_module.app.run(host=args.host, port=args.port, debug=False, use_reloader=False)
        else:
            from waitress import serve

            log.info("Waitress melayani di http://%s:%d (threads=%d)", args.host, args.port, args.threads)
            serve(app_module.app, host=args.host, port=args.port, threads=args.threads)
    finally:
        if ingest is not None:
            try:
                ingest.stop()
            except Exception:
                log.warning("Gagal menghentikan penerima ingest.", exc_info=True)
        if flow is not None:
            try:
                flow.stop()
            except Exception:
                log.warning("Gagal menghentikan penerima flow.", exc_info=True)
        try:
            poller.shutdown_poller()
        except Exception:
            log.warning("Gagal menghentikan poller.", exc_info=True)
        try:
            db.release_instance_lease(token)
        except Exception:
            log.warning("Gagal melepas lease instance.", exc_info=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
