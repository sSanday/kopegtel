"""Blueprint Discovery: pemindaian CIDR & penambahan massal perangkat."""
import ipaddress
import logging
import threading
import concurrent.futures

from flask import Blueprint, g, jsonify, request

import db
import poller
from collectors import ping as ping_collector
from collectors import snmp as snmp_collector
from core.guard import audit, roles
from core.net import network_default_allowed
from core.validation import (
    ALLOWED_NETWORKS,
    DEVICE_TYPES,
    _ip_allowed,
    _parse_ip,
    _parse_optional_id,
)

bp = Blueprint("discovery", __name__)

log = logging.getLogger("nms.discovery")

_SCAN_SEMAPHORE = threading.Semaphore(32)
_scan_jobs_lock = threading.Lock()
# Maksimal 2 pemindaian serentak; sisanya ditolak dengan 429.
_SCAN_JOB_SLOTS = threading.Semaphore(2)


def _run_scan(job_id, cidr, community):
    try:
        _scan_addresses(job_id, cidr, community)
        db.finish_scan_job(job_id)
    except Exception:
        log.exception("Pemindaian %s gagal", job_id)
        try:
            db.fail_scan_job(job_id)
        except Exception:
            log.exception("Gagal menandai pemindaian %s sebagai gagal", job_id)
    finally:
        _SCAN_JOB_SLOTS.release()


def _scan_addresses(job_id, cidr, community):
    network = ipaddress.ip_network(cidr, strict=False)
    addresses = list(network.hosts()) or list(network)
    results = []
    completed = 0

    def probe(addr):
        with _SCAN_SEMAPHORE:
            ip = str(addr)
            ping = ping_collector.check_ping(ip)
            if not ping.get("ok"):
                return None
            entry = {
                "ip": ip,
                "latency_ms": ping.get("latency_ms"),
                "sysname": None,
                "identity": None,
                "version": None,
                "type_hint": None,
            }
            if community:
                try:
                    res = snmp_collector.check_snmp(
                        {"ip": ip, "version": "v2c", "port": 161, "community": community}
                    )
                    if res.get("ok"):
                        entry["sysname"] = res.get("sys_name")
                        entry["type_hint"] = "snmp"
                except Exception:
                    pass
            # Password RouterOS API TIDAK dikirim ke host hasil ping:
            # pemindaian tidak boleh menyemprotkan kredensial ke seluruh subnet.
            return entry

    with concurrent.futures.ThreadPoolExecutor(max_workers=32) as pool:
        futures = {pool.submit(probe, addr): addr for addr in addresses}
        for future in concurrent.futures.as_completed(futures):
            try:
                entry = future.result()
            except Exception:
                entry = None
            completed += 1
            if entry:
                results.append(entry)
            with _scan_jobs_lock:
                if completed % 10 == 0 or completed == len(addresses):
                    db.update_scan_job(job_id, completed, list(results))

    db.update_scan_job(job_id, completed, results)


@bp.get("/api/discovery/scans")
def api_list_scans():
    return jsonify({"scans": db.list_scan_jobs(20)})


@bp.get("/api/discovery/scans/<int:job_id>")
def api_get_scan(job_id):
    job = db.get_scan_job(job_id)
    if job is None:
        return jsonify({"error": "Pekerjaan pemindaian tidak ditemukan."}), 404
    return jsonify({"scan": job})


@bp.post("/api/discovery/scan")
@roles("admin", "operator")
def api_start_scan():
    data = request.get_json(silent=True) or {}
    cidr_raw = str(data.get("cidr") or "").strip()
    try:
        network = ipaddress.ip_network(cidr_raw, strict=False)
    except ValueError:
        return jsonify({"error": "CIDR tidak valid (contoh: 192.168.30.0/24)."}), 400
    size = network.num_addresses
    if size > 1024:
        return jsonify({"error": "Maksimal 1024 alamat per pemindaian."}), 400
    if size < 2:
        return jsonify({"error": "Rentang terlalu kecil."}), 400

    if ALLOWED_NETWORKS:
        for addr in network:
            if not _ip_allowed(addr):
                return jsonify({"error": "CIDR berada di luar ALLOWED_NETWORKS yang diizinkan."}), 403
    elif not network_default_allowed(network):
        return jsonify({"error": "Tanpa ALLOWED_NETWORKS, hanya rentang privat (RFC1918) yang boleh dipindai."}), 403

    # Slot dipindai hanya maksimal 2 sekaligus; slot dilepas oleh _run_scan
    # (finally) setelah pekerjaan selesai atau gagal.
    if not _SCAN_JOB_SLOTS.acquire(blocking=False):
        return jsonify(
            {"error": "Masih ada pemindaian lain yang berjalan. Tunggu sampai selesai.", "code": "scan_busy"}
        ), 429
    try:
        community = str(data.get("community") or "").strip() or None

        total = len(list(network.hosts())) or network.num_addresses
        # Kredensial hanya diteruskan ke thread di memori; jangan pernah menulisnya
        # ke scan_jobs (H-3).
        job_id = db.create_scan_job(str(network), total, g.user["username"])
    except Exception:
        _SCAN_JOB_SLOTS.release()
        raise
    audit("discovery.scan", f"scan:{job_id}", f"Memindai {network}")
    # Password RouterOS API tidak diterima di endpoint ini (penyemprotan
    # kredensial ke subnet) - hanya komunitas SNMP opsional.
    try:
        thread = threading.Thread(
            target=_run_scan,
            args=(job_id, str(network), community),
            daemon=True,
        )
        thread.start()
    except Exception:
        _SCAN_JOB_SLOTS.release()
        try:
            db.fail_scan_job(job_id)
        except Exception:
            log.exception("Gagal menandai pemindaian %s sebagai gagal", job_id)
        raise
    return jsonify({"scan": {"id": job_id, "cidr": str(network), "total": total, "status": "running"}}), 202


@bp.post("/api/discovery/bulk-add")
@roles("admin", "operator")
def api_bulk_add():
    data = request.get_json(silent=True) or {}
    ips = data.get("ips") or []
    if not isinstance(ips, list) or not ips:
        return jsonify({"error": "Tidak ada perangkat yang dipilih."}), 400
    if len(ips) > 200:
        return jsonify({"error": "Maksimal 200 perangkat per penambahan."}), 400
    device_type = str(data.get("type") or "lainnya").strip().lower()
    if device_type not in DEVICE_TYPES:
        device_type = "lainnya"
    group_id, error = _parse_optional_id(data.get("group_id"), "Grup")
    if error:
        return jsonify({"error": error}), 400
    if group_id and db.get_group(group_id) is None:
        return jsonify({"error": "Grup tidak ditemukan."}), 404

    created, skipped = [], []
    names_hint = data.get("names") if isinstance(data.get("names"), dict) else {}
    for raw_ip in ips:
        addr = _parse_ip(raw_ip)
        if addr is None:
            skipped.append({"ip": str(raw_ip), "reason": "IP tidak valid"})
            continue
        ip = str(addr)
        if db.ip_exists(ip):
            skipped.append({"ip": ip, "reason": "Sudah terdaftar"})
            continue
        if not _ip_allowed(addr):
            skipped.append({"ip": ip, "reason": "Di luar ALLOWED_NETWORKS"})
            continue
        name = str(names_hint.get(ip) or "").strip()[:120] or f"Host {ip}"
        base = name
        suffix = 2
        while db.name_exists(name):
            name = f"{base} ({suffix})"
            suffix += 1
        device_id = db.create_device(
            {
                "name": name,
                "ip": ip,
                "type": device_type,
                "location": "",
                "group_id": group_id,
                "parent_id": None,
                "maintenance": False,
                "poll_interval": int(db.get_setting("default_poll_interval", "60")),
                "snmp_enabled": False,
                "snmp_version": "v2c",
                "snmp_port": 161,
                "snmp_v3_auth_proto": "SHA",
                "snmp_v3_priv_proto": "AES",
                "mikrotik_enabled": False,
                "mikrotik_port": 8728,
                "mikrotik_ssl": False,
            }
        )
        poller.schedule_device({"id": device_id, "ip": ip, "poll_interval": 60})
        poller.schedule_immediate_check(device_id, delay_seconds=5)
        created.append({"id": device_id, "ip": ip, "name": name})
    if created:
        audit("discovery.bulk_add", "discovery", f"Menambah {len(created)} perangkat dari hasil pindai")
    return jsonify({"created": created, "skipped": skipped}), 201
