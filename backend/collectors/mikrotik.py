"""MikroTik RouterOS API collector lewat librouteros (port 8728 / 8729 SSL).

Satu koneksi API per siklus poll, selalu ditutup (try/finally), timeout 5 detik.
Mengambil:
- identity       → /system/identity/print
- resource       → /system/resource/print (cpu, memori, storage, uptime, versi, papan)
- health         → /system/health/print (suhu, cpu-suhu, voltase; opsional)
- routerboard    → /system/routerboard/print (model, serial, firmware; opsional)
- interfaces     → /interface/print (name, type, running, disabled, rx/tx-byte)

Kegagalan per bagian ditoleransi (misal health gagal tapi resource berhasil) —
bagian yang gagal dibiarkan NULL, tidak menggagalkan keseluruhan.
Semua perintah bersifat READ-ONLY (print); tidak ada perintah konfigurasi.
"""

import re
import ssl

from librouteros import connect
from librouteros.exceptions import (
    LibRouterosError,
    MultiTrapError,
    TrapError,
)

from . import mikrotik_modules

TIMEOUT_SECONDS = 5
MAX_INTERFACE_ROWS = 5000


def _classify_trap(message):
    lowered = (message or "").lower()
    if any(word in lowered for word in ("login", "password", "user", "auth", "invalid")):
        return "auth_failed"
    return "error"


def _to_number(value):
    try:
        return float(str(value).strip()) if value is not None and str(value).strip() != "" else None
    except (TypeError, ValueError):
        return None


def _to_int(value):
    number = _to_number(value)
    return int(number) if number is not None else None


def _to_bool(value):
    return str(value).strip().lower() in ("true", "yes", "1")


def _parse_uptime_seconds(text):
    """RouterOS uptime, mis. "1w2d03:04:05", "2d03:04:05", "03:04:05", "04:05"."""
    s = str(text or "").strip()
    if not s:
        return None
    total = 0
    matched = False
    weeks = re.search(r"(\d+)w", s)
    if weeks:
        total += int(weeks.group(1)) * 7 * 86400
        matched = True
    days = re.search(r"(\d+)d", s)
    if days:
        total += int(days.group(1)) * 86400
        matched = True
    clock = re.search(r"(\d+):(\d{1,2}):(\d{1,2})", s)
    if clock:
        total += int(clock.group(1)) * 3600 + int(clock.group(2)) * 60 + int(clock.group(3))
        matched = True
    else:
        short = re.fullmatch(r"(\d{1,2}):(\d{1,2})", s)
        if short:
            total += int(short.group(1)) * 60 + int(short.group(2))
            matched = True
    return total if matched else None


def _parse_health(rows):
    """RouterOS v6 → satu dict datar; v7 → daftar baris name/value."""
    data = {}
    if rows:
        first = rows[0]
        if isinstance(first, dict) and "name" in first and "value" in first:
            for row in rows:
                key = str(row.get("name") or "").strip()
                if key:
                    data[key] = row.get("value")
        elif isinstance(first, dict):
            data = dict(first)

    def num(key):
        return _to_number(data.get(key))

    return {
        "temperature": num("temperature"),
        "cpu_temperature": num("cpu-temperature"),
        "voltage": num("voltage"),
    }


def _safe_print(api, path, proplist=None):
    """Jalankan perintah print; kegagalan mengembalikan None, tidak melempar."""
    try:
        if proplist:
            return list(api.rawCmd(path, f"=.proplist={proplist}"))
        return list(api(path))
    except Exception:
        if not proplist:
            return None
        # Sebagian versi menolak nama field tertentu; coba tanpa proplist.
        try:
            return list(api(path))
        except Exception:
            return None


def check_mikrotik(config, plan=None):
    ip = config["ip"]
    port = int(config.get("port") or 8728)
    user = (config.get("user") or "").strip()
    password = config.get("password") or ""
    use_ssl = bool(config.get("ssl"))

    if not user:
        return {
            "method": "mikrotik",
            "ok": False,
            "reason": "auth_failed",
            "detail": "User RouterOS wajib diisi.",
        }

    kwargs = {"timeout": TIMEOUT_SECONDS, "port": port}

    if use_ssl:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE  # RouterOS biasanya memakai sertifikat self-signed
        kwargs["ssl_wrapper"] = lambda sock: context.wrap_socket(sock, server_hostname=ip)

    try:
        api = connect(ip, username=user, password=password, **kwargs)
    except (TrapError, MultiTrapError) as exc:
        # RouterOS melaporkan kredensial salah lewat !trap, bukan exception khusus.
        return {
            "method": "mikrotik",
            "ok": False,
            "reason": _classify_trap(str(exc)),
            "detail": str(exc),
        }
    except LibRouterosError as exc:
        return {"method": "mikrotik", "ok": False, "reason": "error", "detail": str(exc)}
    except TimeoutError:
        return {"method": "mikrotik", "ok": False, "reason": "timeout", "detail": "Koneksi ke RouterOS timeout."}
    except (ConnectionRefusedError, ConnectionResetError, OSError) as exc:
        return {"method": "mikrotik", "ok": False, "reason": "unreachable", "detail": str(exc)}
    except Exception as exc:  # jaring pengaman
        return {"method": "mikrotik", "ok": False, "reason": "error", "detail": str(exc)}

    try:
        identity_rows = _safe_print(api, "/system/identity/print")
        resource_rows = _safe_print(api, "/system/resource/print")
        health_rows = _safe_print(api, "/system/health/print")
        routerboard_rows = _safe_print(api, "/system/routerboard/print")
        interface_rows = _safe_print(
            api,
            "/interface/print",
            ".id,name,type,mtu,mac-address,running,disabled,rx-byte,tx-byte,"
            "rx-error,tx-error,rx-drop,tx-drop,comment,if-index",
        )

        if resource_rows is None and identity_rows is None:
            # Koneksi terbuka tapi semua perintah gagal — laporkan sebagai galat, bukan sukses kosong.
            return {
                "method": "mikrotik",
                "ok": False,
                "reason": "error",
                "detail": "RouterOS tidak mengembalikan data resource/identity.",
            }

        identity = identity_rows[0].get("name") if identity_rows else None
        resource = resource_rows[0] if resource_rows else {}
        health = _parse_health(health_rows)
        routerboard = routerboard_rows[0] if routerboard_rows else {}

        free_memory = _to_int(resource.get("free-memory"))
        total_memory = _to_int(resource.get("total-memory"))
        free_hdd = _to_int(resource.get("free-hdd-space"))
        total_hdd = _to_int(resource.get("total-hdd-space"))

        interfaces = []
        for row in interface_rows or []:
            name = str(row.get("name") or "").strip()
            if not name:
                continue
            interfaces.append(
                {
                    "name": name,
                    "type": str(row.get("type") or ""),
                    "mtu": _to_int(row.get("mtu")),
                    "mac": str(row.get("mac-address") or "").strip() or None,
                    "running": _to_bool(row.get("running")),
                    "disabled": _to_bool(row.get("disabled")),
                    "rx_byte": _to_int(row.get("rx-byte")),
                    "tx_byte": _to_int(row.get("tx-byte")),
                    "rx_error": _to_int(row.get("rx-error")),
                    "tx_error": _to_int(row.get("tx-error")),
                    "rx_drop": _to_int(row.get("rx-drop")),
                    "tx_drop": _to_int(row.get("tx-drop")),
                    "comment": str(row.get("comment") or "").strip() or None,
                    "router_id": str(row.get(".id") or "").strip() or None,
                    "if_index": _to_int(row.get("if-index")),
                }
            )

        base = {
            "identity": identity,
            "board_name": resource.get("board-name"),
            "version": resource.get("version"),
            "architecture_name": resource.get("architecture-name"),
            "uptime": resource.get("uptime"),
            "uptime_s": _parse_uptime_seconds(resource.get("uptime")),
            "cpu_load": _to_int(resource.get("cpu-load")),
            "cpu_count": _to_int(resource.get("cpu-count")),
            "cpu_frequency": _to_int(resource.get("cpu-frequency")),
            "free_memory": free_memory,
            "total_memory": total_memory,
            "free_hdd_space": free_hdd,
            "total_hdd_space": total_hdd,
            "temperature": health["temperature"],
            "cpu_temperature": health["cpu_temperature"],
            "voltage": health["voltage"],
            "model": routerboard.get("model"),
            "serial_number": routerboard.get("serial-number"),
            "current_firmware": routerboard.get("current-firmware"),
            "interfaces": interfaces[:MAX_INTERFACE_ROWS],
            "interfaces_truncated": len(interfaces) > MAX_INTERFACE_ROWS,
        }

        plan = plan or {}
        module_result = None
        try:
            module_result = mikrotik_modules.collect(
                api,
                base={**base, "stale_user_days": plan.get("stale_user_days")},
                layers=plan.get("layers") or ["fast"],
                modules=plan.get("modules") or {},
                capabilities=plan.get("capabilities") or {},
                log_cursor=plan.get("log_cursor") or {},
                max_rows=int(plan.get("max_rows") or MAX_INTERFACE_ROWS),
            )
        except Exception as exc:  # kegagalan modul tidak menggagalkan metrik dasar
            module_result = {
                "ok": False,
                "reason": "error",
                "detail": str(exc),
                "modules": {},
                "counts": {},
                "system": {},
                "capabilities": plan.get("capabilities") or {},
                "log_cursor": plan.get("log_cursor") or {},
                "layer_status": {},
                "truncated": {},
                "probe_errors": [],
            }

        base.update(
            {
                "method": "mikrotik",
                "ok": True,
                "reason": None,
                "modules": module_result.get("modules") or {},
                "module_counts": module_result.get("counts") or {},
                "system": module_result.get("system") or {},
                "capabilities": module_result.get("capabilities") or {},
                "log_cursor": module_result.get("log_cursor") or {},
                "layer_status": module_result.get("layer_status") or {},
                "module_truncated": module_result.get("truncated") or {},
                "probe_errors": module_result.get("probe_errors") or [],
                "module_ok": bool(module_result.get("ok")),
                "module_error": None if module_result.get("ok") else module_result.get("detail"),
            }
        )
        return base
    finally:
        try:
            api.close()
        except Exception:
            pass
