"""SNMP v2c/v3 collector lewat pysnmp 7 (asyncio).

Mengambil sysName, sysDescr, sysUpTime. Semua kegagalan dikembalikan
sebagai hasil terstruktur — tidak pernah melempar exception ke atas.
"""

import asyncio

import pysnmp.hlapi.asyncio as hlapi

# OID numerik agar tidak perlu resolve MIB (menghindari unduhan pysmi).
OID_SYS_DESCR = "1.3.6.1.2.1.1.1.0"
OID_SYS_UPTIME = "1.3.6.1.2.1.1.3.0"
OID_SYS_NAME = "1.3.6.1.2.1.1.5.0"

TIMEOUT_SECONDS = 2
RETRIES = 0

_AUTH_PROTOCOLS = {
    "MD5": hlapi.usmHMACMD5AuthProtocol,
    "SHA": hlapi.usmHMACSHAAuthProtocol,
    "SHA-224": hlapi.usmHMAC128SHA224AuthProtocol,
    "SHA-256": hlapi.usmHMAC192SHA256AuthProtocol,
    "SHA-384": hlapi.usmHMAC256SHA384AuthProtocol,
    "SHA-512": hlapi.usmHMAC384SHA512AuthProtocol,
}

_PRIV_PROTOCOLS = {
    "DES": hlapi.usmDESPrivProtocol,
    "AES": hlapi.usmAesCfb128Protocol,
}


def _auth_data(config):
    version = (config.get("version") or "v2c").lower()

    if version == "v3":
        user = (config.get("v3_user") or "").strip()
        if not user:
            return None, {
                "method": "snmp",
                "ok": False,
                "reason": "auth_failed",
                "detail": "SNMP v3 memerlukan username.",
            }
        auth_password = config.get("v3_auth_password") or None
        priv_password = config.get("v3_priv_password") or None
        auth_proto = _AUTH_PROTOCOLS.get((config.get("v3_auth_proto") or "SHA").upper())
        priv_proto = _PRIV_PROTOCOLS.get((config.get("v3_priv_proto") or "AES").upper())

        if auth_password and auth_proto:
            return hlapi.UsmUserData(
                user,
                auth_password,
                priv_password,
                authProtocol=auth_proto,
                privProtocol=priv_proto if priv_password else hlapi.usmNoPrivProtocol,
            ), None
        return hlapi.UsmUserData(user), None

    community = (config.get("community") or "").strip()
    if not community:
        return None, {
            "method": "snmp",
            "ok": False,
            "reason": "auth_failed",
            "detail": "Community SNMP wajib diisi.",
        }
    return hlapi.CommunityData(community, mpModel=1), None  # mpModel=1 → SNMPv2c


async def _fetch(config):
    host = config["ip"]
    port = int(config.get("port") or 161)

    auth, error = _auth_data(config)
    if error is not None:
        return error

    engine = hlapi.SnmpEngine()
    target = await hlapi.UdpTransportTarget.create(
        (host, port), timeout=TIMEOUT_SECONDS, retries=RETRIES
    )
    iterator = hlapi.get_cmd(
        engine,
        auth,
        target,
        hlapi.ContextData(),
        hlapi.ObjectType(hlapi.ObjectIdentity(OID_SYS_DESCR)),
        hlapi.ObjectType(hlapi.ObjectIdentity(OID_SYS_UPTIME)),
        hlapi.ObjectType(hlapi.ObjectIdentity(OID_SYS_NAME)),
    )
    error_indication, error_status, _error_index, var_binds = await iterator

    if error_indication is not None:
        text = str(error_indication)
        lowered = text.lower()
        if "timed out" in lowered or "no snmp response" in lowered:
            reason = "snmp_no_response"
        elif "unknown user" in lowered or "authorization" in lowered or "wrong" in lowered:
            reason = "auth_failed"
        else:
            reason = "snmp_no_response"
        return {"method": "snmp", "ok": False, "reason": reason, "detail": text}

    if int(error_status) != 0:
        return {
            "method": "snmp",
            "ok": False,
            "reason": "auth_failed" if "authorization" in str(error_status).lower() else "error",
            "detail": str(error_status),
        }

    values = {str(oid): value for oid, value in var_binds}

    def lookup(oid, default=None):
        for key, value in values.items():
            if key.endswith(oid):
                return str(value)
        return default

    sys_uptime_raw = lookup("1.3.6.1.2.1.1.3.0")
    try:
        # TimeTicks dalam satuan 1/100 detik.
        sys_uptime_seconds = int(str(sys_uptime_raw).split()[0]) // 100 if sys_uptime_raw else None
    except (ValueError, TypeError, IndexError):
        sys_uptime_seconds = None

    return {
        "method": "snmp",
        "ok": True,
        "reason": None,
        "sys_descr": lookup("1.3.6.1.2.1.1.1.0"),
        "sys_name": lookup("1.3.6.1.2.1.1.5.0"),
        "sys_uptime": sys_uptime_seconds,
    }


def check_snmp(config):
    try:
        return asyncio.run(_fetch(config))
    except Exception as exc:  # jaring pengaman: hasil terstruktur, bukan 500
        return {"method": "snmp", "ok": False, "reason": "error", "detail": str(exc)}


# ---------------------------------------------------------
# Fallback metrik lewat SNMP (dipakai hanya bila RouterOS API gagal)
# OID standar: HOST-RESOURCES-MIB & IF-MIB. Sub-tree MIKROTIK-MIB (1.3.6.1.4.1.14988)
# SENGAJA tidak dipakai — OID suhu/voltase RouterOS belum terverifikasi di perangkat nyata.
# ---------------------------------------------------------

_OID_HR_STORAGE_TABLE = "1.3.6.1.2.1.25.2.3.1"  # .3 descr, .4 alloc units, .5 size, .6 used
_OID_HR_PROCESSOR_LOAD = "1.3.6.1.2.1.25.3.3.1.2"
_OID_IF_NAME = "1.3.6.1.2.1.31.1.1.1.1"          # ifXTable ifName
_OID_IF_HC_IN_OCTETS = "1.3.6.1.2.1.31.1.1.1.6"  # ifHCInOctets
_OID_IF_HC_OUT_OCTETS = "1.3.6.1.2.1.31.1.1.1.10"  # ifHCOutOctets
_OID_IF_OPER_STATUS = "1.3.6.1.2.1.2.2.1.8"      # ifTable ifOperStatus (1 = up)
_OID_IF_ADMIN_STATUS = "1.3.6.1.2.1.2.2.1.7"     # ifTable ifAdminStatus (2 = down/disabled)


async def _walk(engine, auth, target, oid):
    """Walk satu OID; mengembalikan list (oid_str, value_str). Kegagalan → list kosong."""
    rows = []
    try:
        async for error_indication, error_status, _idx, var_binds in hlapi.walk_cmd(
            engine,
            auth,
            target,
            hlapi.ContextData(),
            hlapi.ObjectType(hlapi.ObjectIdentity(oid)),
            lexicographicMode=False,
        ):
            if error_indication is not None or int(error_status) != 0:
                break
            for name, value in var_binds:
                rows.append((str(name), str(value)))
    except Exception:
        pass
    return rows


async def _fetch_metrics(config):
    host = config["ip"]
    port = int(config.get("port") or 161)

    auth, error = _auth_data(config)
    if error is not None:
        return error

    engine = hlapi.SnmpEngine()
    target = await hlapi.UdpTransportTarget.create(
        (host, port), timeout=TIMEOUT_SECONDS, retries=RETRIES
    )

    storage_rows = await _walk(engine, auth, target, _OID_HR_STORAGE_TABLE)
    cpu_rows = await _walk(engine, auth, target, _OID_HR_PROCESSOR_LOAD)
    if_name_rows = await _walk(engine, auth, target, _OID_IF_NAME)
    if_in_rows = await _walk(engine, auth, target, _OID_IF_HC_IN_OCTETS)
    if_out_rows = await _walk(engine, auth, target, _OID_IF_HC_OUT_OCTETS)
    if_oper_rows = await _walk(engine, auth, target, _OID_IF_OPER_STATUS)
    if_admin_rows = await _walk(engine, auth, target, _OID_IF_ADMIN_STATUS)

    # --- hrProcessorLoad: rata-rata semua core ---
    cpu_loads = []
    for _oid, value in cpu_rows:
        try:
            cpu_loads.append(float(value))
        except ValueError:
            continue
    cpu_pct = round(sum(cpu_loads) / len(cpu_loads), 1) if cpu_loads else None

    # --- hrStorageTable: kelompokkan per index, kolom .3/.4/.5/.6 ---
    table = {}
    for oid_str, value in storage_rows:
        parts = oid_str.split(".")
        try:
            column = int(parts[-2])
            index = parts[-1]
        except (ValueError, IndexError):
            continue
        table.setdefault(index, {})[column] = value

    mem_used = mem_total = disk_used = disk_total = None
    for entry in table.values():
        try:
            descr = str(entry.get(3) or "")
            units = int(entry.get(4))
            size = int(entry.get(5))
            used = int(entry.get(6))
        except (TypeError, ValueError):
            continue
        total_bytes = units * size
        used_bytes = units * used
        lowered = descr.lower()
        if any(word in lowered for word in ("memory", "ram")):
            if mem_total is None:
                mem_total, mem_used = total_bytes, used_bytes
        elif any(word in lowered for word in ("disk", "hdd", "flash", "nand", "storage", "system")):
            if disk_total is None:
                disk_total, disk_used = total_bytes, used_bytes

    # --- ifXTable/ifTable: counter trafik per interface ---
    def index_map(rows):
        mapping = {}
        for oid_str, value in rows:
            index = oid_str.split(".")[-1]
            mapping[index] = value
        return mapping

    names = index_map(if_name_rows)
    ins = index_map(if_in_rows)
    outs = index_map(if_out_rows)
    oper = index_map(if_oper_rows)
    admin = index_map(if_admin_rows)

    interfaces = []
    for index, name in names.items():
        try:
            rx = int(ins.get(index)) if ins.get(index) is not None else None
            tx = int(outs.get(index)) if outs.get(index) is not None else None
        except ValueError:
            rx = tx = None
        oper_status = oper.get(index)
        admin_status = admin.get(index)
        interfaces.append(
            {
                "name": name,
                "type": "",
                "running": oper_status == "1",
                "disabled": admin_status == "2",
                "rx_byte": rx,
                "tx_byte": tx,
            }
        )

    got_any = any(v is not None for v in (cpu_pct, mem_total, disk_total)) or interfaces

    return {
        "method": "snmp",
        "ok": bool(got_any),
        "reason": None if got_any else "snmp_no_response",
        "detail": None if got_any else "Tidak ada data hrProcessorLoad/hrStorageTable/ifXTable.",
        "identity": None,
        "board_name": None,
        "version": None,
        "uptime": None,
        "uptime_s": None,
        "cpu_load": cpu_pct,
        "cpu_count": None,
        "cpu_frequency": None,
        "free_memory": (mem_total - mem_used) if (mem_total is not None and mem_used is not None) else None,
        "total_memory": mem_total,
        "free_hdd_space": (disk_total - disk_used) if (disk_total is not None and disk_used is not None) else None,
        "total_hdd_space": disk_total,
        "temperature": None,
        "cpu_temperature": None,
        "voltage": None,
        "model": None,
        "serial_number": None,
        "current_firmware": None,
        "interfaces": interfaces,
        "source": "snmp",
    }


def collect_snmp_metrics(config):
    """Fallback: ambil metrik lewat SNMP (satu putaran walk). Hasil terstruktur."""
    try:
        return asyncio.run(_fetch_metrics(config))
    except Exception as exc:
        return {"method": "snmp", "ok": False, "reason": "error", "detail": str(exc)}
