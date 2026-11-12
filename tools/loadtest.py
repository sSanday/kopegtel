"""Uji skala & kinerja NMS Kopegtel (perangkat TIRUAN — bukan perangkat asli).

Mensimulasikan 24 jam polling yang dipercepat ke database SEMENTARA, lalu
mengukur: ukuran DB/hari, durasi siklus poll (sampel nyata terhadap
fake_routeros + ping 127.0.0.1), p95 endpoint utama, penggunaan memori/CPU,
dan error "database is locked".

Penggunaan:
    python tools/loadtest.py --scale 50
    python tools/loadtest.py --scale 1000 --tick-minutes 5 --out hasil.json
    python tools/loadtest.py --ingest 10000 --ingest-devices 20

Tidak pernah menyentuh backend/nms.db (NMS_DB_PATH selalu file sementara).
"""
import argparse
import json
import os
import random
import shutil
import statistics
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from contextlib import redirect_stdout

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(BASE_DIR, "backend")
TESTS = os.path.join(BASE_DIR, "tests")
sys.path.insert(0, BACKEND)
sys.path.insert(0, TESTS)

MT_PROPS = [
    ("RB1100AHx4", "7.15.3"),
    ("CCR2004-1G-12S+2XS", "7.12.1"),
    ("RB750Gr3", "6.49.15"),
    ("CRS326-24S+2Q+RM", "7.16"),
]
PING_TYPES = ["server", "olt", "switch"]


def log(msg):
    print(f"[loadtest] {msg}", flush=True)


def seed_devices(db, n, seed=42):
    random.seed(seed)
    mt_pw = db.encrypt_secret("loadtest-mikrotik")
    community = db.encrypt_secret("loadtest-community")
    n_mt = int(n * 0.7)
    devices = []
    for i in range(n):
        is_mt = i < n_mt
        if is_mt:
            model, version = MT_PROPS[i % len(MT_PROPS)]
            device_id = db.create_device({
                "name": f"LT-MT-{i:04d}", "ip": f"10.{(i // 256) % 256}.{i % 256}.{i % 254 + 1}",
                "type": "mikrotik", "location": "LoadTest",
                "poll_interval": 60, "group_id": None,
                "parent_id": devices[-1] if (i % 3 == 2 and devices) else None,
                "maintenance": False,
                "snmp_enabled": False, "snmp_version": "v2c", "snmp_port": 161,
                "snmp_v3_auth_proto": "SHA", "snmp_v3_priv_proto": "AES",
                "mikrotik_enabled": True, "mikrotik_port": 1,
                "mikrotik_user": "loadtest", "mikrotik_password": mt_pw,
                "mikrotik_ssl": False,
            })
            with db._lock:
                db.get_conn().execute(
                    "UPDATE devices SET routeros_version=?, mikrotik_model=? WHERE id=?",
                    (version, model, device_id),
                )
                db.get_conn().commit()
        else:
            device_id = db.create_device({
                "name": f"LT-PING-{i:04d}", "ip": f"10.{(i // 256) % 256}.{i % 256}.{(i + 100) % 254 + 1}",
                "type": random.choice(PING_TYPES), "location": "LoadTest",
                "poll_interval": 60, "group_id": None,
                "parent_id": devices[-1] if (i % 5 == 4 and devices) else None,
                "maintenance": False,
                "snmp_enabled": True, "snmp_version": "v2c", "snmp_community": community,
                "snmp_port": 161, "snmp_v3_auth_proto": "SHA", "snmp_v3_priv_proto": "AES",
                "mikrotik_enabled": False, "mikrotik_port": 8728,
                "mikrotik_user": "", "mikrotik_password": "", "mikrotik_ssl": False,
            })
        devices.append(device_id)
    # Masalah terbuka sintetis (untuk /api/problems)
    for i in range(0, min(20, n), 2):
        db.open_problem(devices[i], "device_down" if i % 4 == 0 else "high_latency", "high" if i % 4 == 0 else "average", f"LoadTest problem {i}")
    return devices


INTERFACE_TEMPLATES = [
    ("ether1", "ether"), ("ether2", "ether"), ("ether3", "ether"), ("ether4", "ether"),
    ("ether5", "ether"), ("ether6", "ether"), ("ether7", "ether"), ("ether8", "ether"),
    ("sfp-sfpplus1", "sfp"), ("sfp-sfpplus2", "sfp"),
]


def seed_mt_snapshots(db, devices, n_mt):
    now = db._now()
    conn = db.get_conn()
    with db._lock:
        for i, device_id in enumerate(devices[:n_mt]):
            iface_rows = []
            rx = 10_000_000
            for pos, (name, itype) in enumerate(INTERFACE_TEMPLATES):
                optical = itype == "sfp" and (i % 5 == 0)
                if itype == "sfp" and not optical:
                    continue  # port sfp hanya pada 20% perangkat mikrotik
                iface_rows.append({
                    "row_key": f"if-{i}-{pos}", "name": name, "type": itype, "mtu": 1500,
                    "mac": f"02:00:00:{i % 256:02x}:{pos:02x}:01",
                    "running": 1, "disabled": 0,
                    "rx_byte": rx, "tx_byte": rx // 2, "rx_error": pos % 3, "tx_error": pos % 2,
                    "rx_drop": 0, "tx_drop": 0, "comment": "loadtest",
                })
            db.replace_mt_snapshot("mt_interfaces", device_id, iface_rows, now)
        conn.commit()


def fill_history(db, devices, n_mt, tick_minutes):
    ticks = 24 * 60 // tick_minutes
    start = datetime.now(timezone.utc) - timedelta(hours=24)
    log(f"mengisi 24 jam riwayat: {ticks} tick x {len(devices)} perangkat (tick {tick_minutes} menit)")
    lock_errors = 0
    conn = db.get_conn()
    with db._lock:
        conn.execute("PRAGMA synchronous=OFF")   # DB uji sementara; tidak perlu fsync
    for tick in range(ticks):
        ts = (start + timedelta(minutes=tick * tick_minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
        checks, metrics, if_rows, mt_rows = [], [], [], []
        try:
            for i, device_id in enumerate(devices):
                is_mt = i < n_mt
                checks.append((device_id, ts, "online", 1.5 + (i % 7) * 0.3, "{}"))
                metrics.append((
                    device_id, ts, 20 + (i % 40), 512 + (i % 200), 4096, 8000 + (i % 100), 32000,
                    45.0, 42.0, 12.1, 86400 + tick * 60, 1.5,
                ))
                iface_count = 10 if is_mt else 2
                for pos in range(iface_count):
                    if_rows.append((
                        device_id, ts, f"ether{(pos % 10) + 1}" if pos < 8 else f"sfp-sfpplus{pos - 7}",
                        1, 0, 1_000_000 * (pos + 10) + tick, 500_000 * (pos + 10) + tick,
                        50_000_000 + pos * 1_000_000, 25_000_000 + pos * 500_000,
                    ))
                if is_mt:
                    counts = {"ppp_active": 10 + i % 30, "hotspot_active": i % 7,
                              "connections": 800 + i % 500, "dhcp_leases": 30 + i % 80}
                    mt_rows.extend([(device_id, ts, key, value) for key, value in counts.items()])
            with db._lock:
                conn.executemany(
                    "INSERT INTO device_checks (device_id, checked_at, status, latency_ms, detail_json, maintenance)"
                    " VALUES (?,?,?,?,?,0)",
                    checks,
                )
                conn.executemany(
                    "INSERT INTO device_metrics (device_id, ts, cpu_pct, mem_used, mem_total, disk_used, disk_total,"
                    " temp_c, cpu_temp_c, voltage, uptime_s, ping_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    metrics,
                )
                conn.executemany(
                    "INSERT INTO interface_stats (device_id, ts, if_name, running, disabled, rx_bytes, tx_bytes, rx_bps, tx_bps)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    if_rows,
                )
                conn.executemany(
                    "INSERT INTO mt_metric_history (device_id, ts, metric, value) VALUES (?,?,?,?)",
                    mt_rows,
                )
                conn.commit()
        except Exception as exc:
            if "locked" in str(exc).lower():
                lock_errors += 1
            else:
                raise
    with db._lock:
        conn.execute("PRAGMA synchronous=NORMAL")
    return lock_errors


def table_counts(conn):
    tables = ["device_checks", "device_metrics", "interface_stats", "mt_metric_history", "problems", "devices"]
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}


def db_bytes(path):
    total = 0
    for suffix in ("", "-wal", "-shm"):
        candidate = path + suffix
        if os.path.exists(candidate):
            total += os.path.getsize(candidate)
    return total


def bench_endpoints(client, reps):
    endpoints = [
        ("GET /api/summary", "/api/summary"),
        ("GET /api/devices p1", "/api/devices?per_page=25&page=1"),
        ("GET /api/devices p_akhir", None),  # diisi belakangan
        ("GET /api/topology", "/api/topology"),
        ("GET /api/problems", "/api/problems?status=open&limit=100"),
        ("GET /api/mikrotik/summary", "/api/mikrotik/summary"),
        ("GET laporan armada", "/api/mikrotik/reports/fleet"),
    ]
    devices_page = client.get("/api/devices?per_page=25&page=1").get_json()
    last_page = max(1, (devices_page.get("total") or 1) // 25)
    results = {}
    for label, path in endpoints:
        if path is None:
            path = f"/api/devices?per_page=25&page={last_page}"
        times = []
        for _ in range(reps):
            import time as _time

            t0 = _time.perf_counter()
            response = client.get(path)
            elapsed = (_time.perf_counter() - t0) * 1000
            assert response.status_code in (200, 304), f"{label}: HTTP {response.status_code}"
            times.append(elapsed)
        times.sort()
        p95 = times[max(0, int(len(times) * 0.95) - 1)]
        results[label] = {
            "n": len(times),
            "mean_ms": round(statistics.fmean(times), 1),
            "p50_ms": round(statistics.median(times), 1),
            "p95_ms": round(p95, 1),
            "max_ms": round(max(times), 1),
        }
    return results


def ingest_benchmark(messages, devices_count, args):
    """Skenario ingest: N pesan syslog dari M perangkat terdaftar.

    Mengukur throughput flush batch, jumlah dibuang, dan p95 endpoint daftar.
    Tidak mengirim ke jaringan; semua lewat antrean internal IngestManager.
    """
    tmp = tempfile.mkdtemp(prefix="nms-loadtest-ingest-")
    db_path = os.path.join(tmp, "loadtest-ingest.db")
    os.environ.update({
        "NMS_DB_PATH": db_path,
        "SECRET_KEY": "loadtest-secret-key-0123456789abcdef",
        "ENCRYPTION_KEY": "9fG9f2iVfTq7Zuk5rxlb3bMaOQrVvZ3kM7yD5KBmUE8=",
        "ADMIN_PASSWORD": "loadtest-admin-password",
        "ADMIN_USERNAME": "admin",
        "LOG_LEVEL": "ERROR",
    })
    import time as _time

    import psutil  # noqa: E402
    import db  # noqa: E402

    db.init_db()
    with db._lock:
        db.get_conn().execute("UPDATE users SET must_change_password=0")
        db.get_conn().commit()
    rss_before = psutil.Process().memory_info().rss

    log(f"ingest: {messages} pesan x {devices_count} perangkat (db={db_path})")
    devices = []
    for index in range(devices_count):
        devices.append(_create_ingest_device(db, index))

    from ingest.service import IngestManager

    manager = IngestManager()
    manager.ensure_batchers()
    # Jalankan thread penulis batch seperti produksi agar antrean terkuras.
    manager._syslog_batcher.start()
    manager._trap_batcher.start()

    formats = [
        "<134>Nov 11 07:00:00 lt-{i} loadtest-event-0007",
        "loadtest-event-0007 tag-detail",
        "<29>1 2026-11-11T00:00:00+00:00 lt host-{i} 1 - loadtest-event-0007",
    ]
    t_enqueue_start = _time.perf_counter()
    for n in range(messages):
        index = n % devices_count
        text = formats[n % len(formats)].format(i=index)
        ip = f"192.0.2.{10 + index}"
        manager.process_syslog_datagram(text.encode("utf-8"), ip)
    enqueue_seconds = _time.perf_counter() - t_enqueue_start
    # Hentikan thread penulis; penghentian menulis sisa antrean.
    t_flush_start = _time.perf_counter()
    manager.stop()
    flush_seconds = _time.perf_counter() - t_flush_start

    stats = manager.status()
    counts = db.ingest_table_counts()
    rss_after = psutil.Process().memory_info().rss

    # Benchmark endpoint daftar syslog (dengan auth admin).
    with redirect_stdout(open(os.devnull, "w", encoding="utf-8")):
        import app as app_module  # noqa: E402

    # Impor app membuat akun admin (must_change_password=1); matikan untuk loadtest.
    with db._lock:
        db.get_conn().execute("UPDATE users SET must_change_password=0")
        db.get_conn().commit()

    client = app_module.app.test_client()
    login = client.post("/api/login", json={"username": "admin", "password": os.environ["ADMIN_PASSWORD"]})
    assert login.status_code == 200, "login admin gagal pada DB ingest loadtest"
    times = []
    for _ in range(args.bench_reps):
        t0 = _time.perf_counter()
        response = client.get("/api/events/syslog?per_page=50&page=1")
        times.append((_time.perf_counter() - t0) * 1000)
        if response.status_code != 200:
            raise AssertionError(f"HTTP {response.status_code}: {response.get_data(as_text=True)[:500]}")
    times.sort()
    p95 = times[max(0, int(len(times) * 0.95) - 1)]

    result = {
        "scenario": "ingest",
        "messages": messages,
        "devices": devices_count,
        "enqueue_seconds": round(enqueue_seconds, 3),
        "flush_seconds": round(flush_seconds, 3),
        "msgs_per_sec_enqueue": round(messages / max(enqueue_seconds, 1e-9), 1),
        "msgs_per_sec_total": round(messages / max(enqueue_seconds + flush_seconds, 1e-9), 1),
        "syslog_rows": counts["syslog_events"],
        "dropped_unknown": stats["syslog_udp"]["dropped_unknown"],
        "dropped_rate": stats["syslog_udp"]["dropped_rate"],
        "dropped_queue": stats["syslog_udp"]["dropped_queue"],
        "rss_mb": {"before": round(rss_before / 1e6, 1), "after": round(rss_after / 1e6, 1)},
        "db_bytes": db_bytes(db_path),
        "endpoint_events_syslog_p95_ms": round(p95, 1),
        "endpoint_events_syslog_mean_ms": round(statistics.fmean(times), 1),
    }
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(result, handle, indent=2, ensure_ascii=False)
        log(f"hasil ditulis ke {args.out}")
    if not args.keep_temp:
        shutil.rmtree(tmp, ignore_errors=True)
        log("folder sementara DB uji dihapus.")
    else:
        log(f"folder sementara DB uji dipertahankan (--keep-temp): {tmp}")


def _ip_us(text):
    import struct
    import socket

    return struct.unpack("!I", socket.inet_aton(text))[0]


def _flow_packet_v5():
    """Paket NetFlow v5 minimal yang valid (untuk uji skala, bukan perangkat nyata)."""
    import struct
    import socket

    header = struct.pack("!HHIIIIBBH", 5, 1, 1000, 2000000000, 0, 1, 0, 0, 0)
    flow = struct.pack(
        "!IIIHHIIIIHHxBBBHHBBxx",
        _ip_us("192.0.2.1"), _ip_us("203.0.113.5"), 0, 3, 4,
        10, 5000, 100, 200, 12345, 443, 0x18, 6, 0, 0, 0, 0, 0,
    )
    return header + flow


def flow_benchmark(messages, devices_count, args):
    """Skenario flow: N paket NetFlow v5 tiruan lewat FlowManager (tanpa jaringan)."""
    tmp = tempfile.mkdtemp(prefix="nms-loadtest-flow-")
    db_path = os.path.join(tmp, "loadtest-flow.db")
    os.environ.update({
        "NMS_DB_PATH": db_path,
        "SECRET_KEY": "loadtest-secret-key-0123456789abcdef",
        "ENCRYPTION_KEY": "9fG9f2iVfTq7Zuk5rxlb3bMaOQrVvZ3kM7yD5KBmUE8=",
        "ADMIN_PASSWORD": "loadtest-admin-password",
        "ADMIN_USERNAME": "admin",
        "LOG_LEVEL": "ERROR",
    })
    import time as _time

    import psutil  # noqa: E402
    import db  # noqa: E402

    db.init_db()
    log(f"flow: {messages} paket x {devices_count} perangkat (db={db_path})")
    rss_before = psutil.Process().memory_info().rss
    devices = []
    for index in range(devices_count):
        devices.append(_create_ingest_device(db, index))

    from flow.service import FlowManager

    manager = FlowManager()
    manager.ensure_pipeline()
    packet = _flow_packet_v5()

    t_enqueue_start = _time.perf_counter()
    for n in range(messages):
        index = n % devices_count
        ip = f"192.0.2.{10 + index}"
        manager.process_flow_datagram(packet, ip, "netflow_udp")
    enqueue_seconds = _time.perf_counter() - t_enqueue_start
    t_flush_start = _time.perf_counter()
    flushed = manager.flush_buckets()
    flush_seconds = _time.perf_counter() - t_flush_start

    stats = manager.status()["netflow_udp"]
    counts = db.ingest_table_counts() if hasattr(db, "ingest_table_counts") else {}
    rss_after = psutil.Process().memory_info().rss
    buckets = db.flow_table_counts()
    result = {
        "scenario": "flow",
        "messages": messages,
        "devices": devices_count,
        "enqueue_seconds": round(enqueue_seconds, 3),
        "flush_seconds": round(flush_seconds, 3),
        "msgs_per_sec_enqueue": round(messages / max(enqueue_seconds, 1e-9), 1),
        "msgs_per_sec_total": round(messages / max(enqueue_seconds + flush_seconds, 1e-9), 1),
        "flows": stats["flows"],
        "bytes": stats["bytes"],
        "dropped_unknown": stats["dropped_unknown"],
        "dropped_rate": stats["dropped_rate"],
        "dropped_invalid": stats["dropped_invalid"],
        "buckets": buckets,
        "rows_written": flushed.get("rows", 0),
        "rss_mb": {"before": round(rss_before / 1e6, 1), "after": round(rss_after / 1e6, 1)},
        "db_bytes": db_bytes(db_path),
    }
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(result, handle, indent=2, ensure_ascii=False)
        log(f"hasil ditulis ke {args.out}")
    if not args.keep_temp:
        shutil.rmtree(tmp, ignore_errors=True)
        log("folder sementara DB uji dihapus.")
    else:
        log(f"folder sementara DB uji dipertahankan (--keep-temp): {tmp}")


def _create_ingest_device(db, index):
    return db.create_device({
        "name": f"LT-INGEST-{index:04d}", "ip": f"192.0.2.{10 + index}",
        "type": "mikrotik", "location": "LoadTest",
        "poll_interval": 60, "group_id": None, "parent_id": None,
        "maintenance": False,
        "snmp_enabled": False, "snmp_version": "v2c", "snmp_port": 161,
        "snmp_v3_auth_proto": "SHA", "snmp_v3_priv_proto": "AES",
        "mikrotik_enabled": False, "mikrotik_port": 8728,
        "mikrotik_user": "", "mikrotik_password": "", "mikrotik_ssl": False,
    })


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scale", type=int, choices=[50, 200, 500, 1000], default=None, help="jumlah perangkat untuk skenario utama")
    parser.add_argument("--ingest", type=int, default=None, help="jumlah pesan syslog tiruan untuk skenario ingest (mis. 10000)")
    parser.add_argument("--ingest-devices", type=int, default=20)
    parser.add_argument("--flow", type=int, default=None, help="jumlah paket flow tiruan (v5) (mis. 10000)")
    parser.add_argument("--flow-devices", type=int, default=20)
    parser.add_argument("--tick-minutes", type=int, default=None, help="granularitas simulasi (default: 1 menit utk <=200, 5 menit utk >200)")
    parser.add_argument("--poll-sample", type=int, default=10, help="jumlah perangkat sampel untuk ukur durasi poll nyata")
    parser.add_argument("--bench-reps", type=int, default=25)
    parser.add_argument("--out", default=None, help="tulis ringkasan JSON ke berkas")
    parser.add_argument("--keep-temp", action="store_true", help="pertahankan folder sementara DB uji (default: dihapus otomatis)")
    args = parser.parse_args()

    if args.ingest:
        return ingest_benchmark(args.ingest, args.ingest_devices, args)
    if args.flow:
        return flow_benchmark(args.flow, args.flow_devices, args)
    if not args.scale:
        parser.error("--scale wajib diisi (atau gunakan --ingest/--flow N).")

    tick_minutes = args.tick_minutes or (1 if args.scale <= 200 else 5)

    tmp = tempfile.mkdtemp(prefix="nms-loadtest-")
    db_path = os.path.join(tmp, "loadtest.db")
    os.environ.update({
        "NMS_DB_PATH": db_path,
        "SECRET_KEY": "loadtest-secret-key-0123456789abcdef",
        "ENCRYPTION_KEY": "9fG9f2iVfTq7Zuk5rxlb3bMaOQrVvZ3kM7yD5KBmUE8=",
        "ADMIN_PASSWORD": "loadtest-admin-password",
        "ADMIN_USERNAME": "admin",
        "LOG_LEVEL": "ERROR",
    })

    import psutil  # noqa: E402
    import db  # noqa: E402
    import poller  # noqa: E402

    db.init_db()
    # Admin loadtest tidak wajib ganti password agar endpoint bisa diuji langsung.
    with db._lock:
        db.get_conn().execute("UPDATE users SET must_change_password=0")
        db.get_conn().commit()
    process = psutil.Process()
    rss_before = process.memory_info().rss
    log(f"scale={args.scale} tick={tick_minutes}m db={db_path}")

    devices = seed_devices(db, args.scale)
    n_mt = int(args.scale * 0.7)
    seed_mt_snapshots(db, devices, n_mt)
    size_after_seed = db_bytes(db_path)

    lock_errors = fill_history(db, devices, n_mt, tick_minutes)
    with db._lock:
        conn = db.get_conn()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("ANALYZE")
        conn.commit()
    counts = table_counts(conn)
    size_after_fill = db_bytes(db_path)
    rss_after_fill = process.memory_info().rss
    log(f"ukuran DB: seed={size_after_seed/1e6:.1f}MB fill={size_after_fill/1e6:.1f}MB rows={counts}")

    # ---------- Sampel poll nyata ----------
    from fake_routeros import FakeRouterOS, module_rows  # noqa: E402

    fakes = []
    sample_mt = min(args.poll_sample // 2, n_mt)
    sample_ping = args.poll_sample - sample_mt
    sample_devices = []
    for mt_i in range(sample_mt):
        server = FakeRouterOS(rows=module_rows(), counts={"/ip/firewall/connection/print": 100})
        port = server.start()
        fakes.append(server)
        dev_id = db.create_device({
            "name": f"LT-SAMPLE-MT-{port}", "ip": f"127.0.0.{10 + mt_i}", "type": "mikrotik",
            "location": "LoadTest", "poll_interval": 60, "group_id": None, "parent_id": None,
            "maintenance": False, "snmp_enabled": False, "snmp_version": "v2c",
            "snmp_port": 161, "snmp_v3_auth_proto": "SHA", "snmp_v3_priv_proto": "AES",
            "mikrotik_enabled": True, "mikrotik_port": port,
            "mikrotik_user": "api", "mikrotik_password": db.encrypt_secret("loadtest"),
            "mikrotik_ssl": False,
        })
        sample_devices.append(("mikrotik", dev_id))
    for ping_i in range(sample_ping):
        dev_id = db.create_device({
            "name": f"LT-SAMPLE-PING-{ping_i}", "ip": f"127.0.0.{30 + ping_i}", "type": "server",
            "location": "LoadTest", "poll_interval": 60, "group_id": None, "parent_id": None,
            "maintenance": False, "snmp_enabled": False, "snmp_version": "v2c",
            "snmp_port": 161, "snmp_v3_auth_proto": "SHA", "snmp_v3_priv_proto": "AES",
            "mikrotik_enabled": False, "mikrotik_port": 8728,
            "mikrotik_user": "", "mikrotik_password": "", "mikrotik_ssl": False,
        })
        sample_devices.append(("ping", dev_id))
    import time as _time

    poll_times = []
    poll_errors = 0
    log(f"mengukur {len(sample_devices)} poll nyata (berurutan)")
    for kind, dev_id in sample_devices:
        t0 = _time.perf_counter()
        try:
            poller.run_check(dev_id)
            poll_times.append((kind, (_time.perf_counter() - t0) * 1000))
        except Exception as exc:
            poll_errors += 1
            log(f"poll {kind} device {dev_id} gagal: {exc}")
    for server in fakes:
        server.stop()
    mt_times = [t for kind, t in poll_times if kind == "mikrotik"]
    ping_times = [t for kind, t in poll_times if kind == "ping"]
    mean_mt = statistics.fmean(mt_times) if mt_times else 0.0
    mean_ping = statistics.fmean(ping_times) if ping_times else 0.0
    est_sequential = mean_mt * n_mt + mean_ping * (args.scale - n_mt)
    est_8_workers = est_sequential / 8

    # ---------- Benchmark endpoint ----------
    with redirect_stdout(open(os.devnull, "w", encoding="utf-8")):
        import app as app_module  # noqa: E402

    # Seed admin di app.py membuat must_change_password=1; matikan untuk loadtest.
    with db._lock:
        db.get_conn().execute("UPDATE users SET must_change_password=0")
        db.get_conn().commit()

    client = app_module.app.test_client()
    login = client.post("/api/login", json={"username": "admin", "password": os.environ["ADMIN_PASSWORD"]})
    assert login.status_code == 200, "login admin gagal pada DB loadtest"
    bench = bench_endpoints(client, args.bench_reps)
    rss_after_bench = process.memory_info().rss
    cpu_percent = process.cpu_percent(interval=0.2)

    total_checks = counts["device_checks"] + sample_devices.__len__()
    result = {
        "scale": args.scale,
        "tick_minutes": tick_minutes,
        "devices": {"total": args.scale, "mikrotik": n_mt, "ping_snmp": args.scale - n_mt},
        "rows": counts,
        "db_bytes": {
            "after_seed": size_after_seed,
            "after_fill": size_after_fill,
            "mb_per_day_estimate": round(size_after_fill / 1e6, 1),
        },
        "rss_mb": {
            "before_seed": round(rss_before / 1e6, 1),
            "after_fill": round(rss_after_fill / 1e6, 1),
            "after_bench": round(rss_after_bench / 1e6, 1),
        },
        "cpu_percent": cpu_percent,
        "lock_errors_during_fill": lock_errors,
        "poll": {
            "sample": len(poll_times),
            "errors": poll_errors,
            "mean_mikrotik_ms": round(mean_mt, 1),
            "mean_ping_ms": round(mean_ping, 1),
            "estimated_cycle_sequential_s": round(est_sequential / 1000, 1),
            "estimated_cycle_8_workers_s": round(est_8_workers / 1000, 1),
            "interval_s": 60,
            "target_pct_interval": round(100 * est_8_workers / 1000 / 60, 1) if 60 else None,
        },
        "endpoints": bench,
    }
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(result, handle, indent=2, ensure_ascii=False)
        log(f"hasil ditulis ke {args.out}")

    if not args.keep_temp:
        try:
            shutil.rmtree(tmp, ignore_errors=True)
            log("folder sementara DB uji dihapus.")
        except Exception as exc:
            log(f"folder sementara gagal dihapus ({tmp}): {exc}")
    else:
        log(f"folder sementara DB uji dipertahankan (--keep-temp): {tmp}")


if __name__ == "__main__":
    main()
