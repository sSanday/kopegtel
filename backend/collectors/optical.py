"""Kolektor optik (DDM/DOM) untuk modul fiber.

Dua jalur, keduanya READ-ONLY dan selalu menutup koneksi:

1. MikroTik RouterOS API (port 8728/8729):
   ``/interface/ethernet/print`` → port yang tidak disabled →
   ``/interface/ethernet/monitor`` dengan kata ``?name=<port>`` dan ``=once=``.
   Port yang mengembalikan field ``sfp-*`` dianggap port optik.
   Nama field dan format nilai BEDA antar versi RouterOS (v6/v7) — parsing
   di bawah dibuat defensif: nilai tak dikenal → None (tampil "—"),
   tidak pernah dikarang. WAJIB diverifikasi di perangkat nyata
   (bandingkan dengan WinBox/CLI: sfp-rx-power, sfp-tx-power, dll).

2. Fallback SNMP untuk switch/router lain:
   - ENTITY-SENSOR-MIB (RFC 3433): entSensorTable — entSensorType
     dBm = 14, celsius = 8, volts-dc = 4, amperes = 5; dikalikan
     entSensorScale dan dibagi 10^entSensorPrecision.
   - entPhysicalTable dipakai untuk korelasi nama sensor ↔ entitas (port).
   - Subtree mtxrOptical MIKROTIK-MIB (1.3.6.1.4.1.14988.1.1.19) hanya
     dipakai untuk DETEKSI keberadaan DDM (berapa leaf ditemukan) — struktur
     tabelnya belum terdokumentasi stabil, sehingga angkanya TIDAK dipakai
     untuk mengisi rx/tx (tidak boleh mengarang).

   Arah Rx/Tx ditentukan dari regex pada entPhysicalName/Descr
   (mis. "Rx Power", "tx-bias"). Bila tidak bisa ditentukan → rx/tx
   dibiarkan None dan dijelaskan di "detail". Verifikasi dengan
   snmpwalk ke perangkat nyata sebelum mengandalkan jalur ini.

Kegagalan satu port/sensor tidak menggagalkan port/sensor lain.
Nilai ekstrem (≤ −40 dBm / −inf) diperlakukan sebagai LOS di lapisan evaluasi.
"""

import re
import ssl

from librouteros import connect
from librouteros.exceptions import (
    LibRouterosError,
    MultiTrapError,
    TrapError,
)

import pysnmp.hlapi.asyncio as hlapi
import asyncio

from optical import (
    parse_bias_ma,
    parse_length_m,
    parse_power_dbm,
    parse_sfp_bool,
    parse_temperature_c,
    parse_voltage_v,
    parse_wavelength_nm,
)
from collectors.snmp import _auth_data, _walk  # reuse auth + walk yang sudah teruji
from collectors import tls as tls_util

TIMEOUT_SECONDS = 5
SNMP_TIMEOUT_SECONDS = 2  # konsisten dengan kolektor SNMP yang ada; budget 5 dtk berlaku untuk API RouterOS
SNMP_RETRIES = 0


# ---------------------------------------------------------
# MikroTik RouterOS API
# ---------------------------------------------------------

def _to_bool(value):
    return str(value).strip().lower() in ("true", "yes", "1")


def _classify_trap(message):
    lowered = (message or "").lower()
    if any(word in lowered for word in ("login", "password", "user", "auth", "invalid")):
        return "auth_failed"
    return "error"


def _monitor_port(api, name):
    """monitor satu port; kegagalan → None (port lain tetap diproses)."""
    try:
        rows = list(api.rawCmd("/interface/ethernet/monitor", f"?name={name}", "=once="))
    except Exception:
        return None
    if not rows:
        return None
    return {str(key): value for key, value in rows[0].items()}


def check_optical_mikrotik(config, skip_ports=None):
    """Kembalikan {method, ok, reason, detail, ports: [...], scanned: [...]}.

    skip_ports: nama port yang TIDAK dimonitor siklus ini (deteksi non-optik yang
    masih segar < 1 jam — deteksi ulang tiap 1 jam). Port optik selalu dimonitor.
    scanned: nama port yang benar-benar di-monitor siklus ini.
    """
    skip = set(skip_ports or ())
    ip = config["ip"]
    port = int(config.get("port") or 8728)
    user = (config.get("user") or "").strip()
    password = config.get("password") or ""
    use_ssl = bool(config.get("ssl"))

    if not user:
        return {"method": "mikrotik-optical", "ok": False, "reason": "auth_failed",
                "detail": "User RouterOS wajib diisi.", "ports": [], "scanned": []}

    kwargs = {"timeout": TIMEOUT_SECONDS, "port": port}
    if use_ssl:
        # M-11: verifikasi TLS wajib. Fingerprint per perangkat dipakai untuk
        # pinning sertifikat self-signed; tanpa itu, verifikasi CA standar.
        try:
            kwargs["ssl_wrapper"] = tls_util.ssl_wrapper(
                ip, config.get("ssl_fingerprint")
            )
        except ValueError as exc:
            return {"method": "mikrotik-optical", "ok": False, "reason": "tls_error",
                    "detail": str(exc), "ports": [], "scanned": []}

    try:
        api = connect(ip, username=user, password=password, **kwargs)
    except (TrapError, MultiTrapError) as exc:
        return {"method": "mikrotik-optical", "ok": False, "reason": _classify_trap(str(exc)),
                "detail": str(exc), "ports": [], "scanned": []}
    except LibRouterosError as exc:
        return {"method": "mikrotik-optical", "ok": False, "reason": "error",
                "detail": str(exc), "ports": [], "scanned": []}
    except tls_util.TlsVerificationError as exc:
        return {"method": "mikrotik-optical", "ok": False, "reason": exc.code,
                "detail": str(exc), "ports": [], "scanned": []}
    except ssl.SSLError as exc:
        return {"method": "mikrotik-optical", "ok": False, "reason": "tls_error",
                "detail": f"Verifikasi TLS gagal: {exc}", "ports": [], "scanned": []}
    except TimeoutError:
        return {"method": "mikrotik-optical", "ok": False, "reason": "timeout",
                "detail": "Koneksi ke RouterOS timeout.", "ports": [], "scanned": []}
    except (ConnectionRefusedError, ConnectionResetError, OSError) as exc:
        return {"method": "mikrotik-optical", "ok": False, "reason": "unreachable",
                "detail": str(exc), "ports": [], "scanned": []}
    except Exception as exc:  # jaring pengaman
        return {"method": "mikrotik-optical", "ok": False, "reason": "error",
                "detail": str(exc), "ports": [], "scanned": []}

    scanned = []
    try:
        try:
            ethernet_rows = list(api("/interface/ethernet/print"))
        except Exception:
            ethernet_rows = None
        if ethernet_rows is None:
            return {"method": "mikrotik-optical", "ok": False, "reason": "error",
                    "detail": "RouterOS tidak mengembalikan data /interface/ethernet/print.",
                    "ports": [], "scanned": []}

        ports = []
        for row in ethernet_rows:
            name = str(row.get("name") or "").strip()
            if not name:
                continue
            if _to_bool(row.get("disabled")):
                continue  # port mati → tidak dipantau
            if name in skip:
                continue  # bukan optik, deteksi masih segar (evaluasi ulang tiap 1 jam)
            mon = _monitor_port(api, name)
            scanned.append(name)
            if mon is None:
                continue  # kegagalan satu port tidak menggagalkan port lain
            sfp = {k: v for k, v in mon.items() if str(k).startswith("sfp-")}
            if not sfp:
                continue  # port biasa (tembaga/DAC) → bukan optik

            module_present = parse_sfp_bool(sfp["sfp-module-present"]) if "sfp-module-present" in sfp else True
            rx_raw = sfp.get("sfp-rx-power")
            tx_raw = sfp.get("sfp-tx-power")
            ports.append(
                {
                    "if_name": name,
                    "running": _to_bool(row.get("running")),
                    "module_present": module_present,
                    "rx_loss": parse_sfp_bool(sfp.get("sfp-rx-loss")),
                    "tx_fault": parse_sfp_bool(sfp.get("sfp-tx-fault")),
                    "rx_dbm": parse_power_dbm(rx_raw),
                    "tx_dbm": parse_power_dbm(tx_raw),
                    "temp_c": parse_temperature_c(sfp.get("sfp-temperature")),
                    "voltage_v": parse_voltage_v(sfp.get("sfp-supply-voltage")),
                    "bias_ma": parse_bias_ma(sfp.get("sfp-tx-bias-current")),
                    "ddm_supported": rx_raw is not None or tx_raw is not None,
                    "vendor": (str(sfp.get("sfp-vendor-name") or "").strip() or None),
                    "part_no": (str(sfp.get("sfp-vendor-part-number") or sfp.get("sfp-vendor-partnumber") or "").strip() or None),
                    "serial": (str(sfp.get("sfp-vendor-serial") or "").strip() or None),
                    "type": (str(sfp.get("sfp-type") or "").strip() or None),
                    "wavelength_nm": parse_wavelength_nm(sfp.get("sfp-wavelength")),
                    "link_length_m": parse_length_m(sfp.get("sfp-link-length-sm") or sfp.get("sfp-link-length")),
                }
            )
    finally:
        try:
            api.close()
        except Exception:
            pass

    return {"method": "mikrotik-optical", "ok": True, "reason": None,
            "detail": None, "ports": ports, "scanned": scanned}


# ---------------------------------------------------------
# Fallback SNMP: ENTITY-SENSOR-MIB + mtxrOptical (deteksi saja)
# ---------------------------------------------------------

_OID_SENSOR_TYPE = "1.3.6.1.2.1.99.1.1.1.1"
_OID_SENSOR_SCALE = "1.3.6.1.2.1.99.1.1.1.2"
_OID_SENSOR_PRECISION = "1.3.6.1.2.1.99.1.1.1.3"
_OID_SENSOR_VALUE = "1.3.6.1.2.1.99.1.1.1.4"
_OID_SENSOR_STATUS = "1.3.6.1.2.1.99.1.1.1.5"
_OID_PHYSICAL_DESCR = "1.3.6.1.2.1.47.1.1.1.1.2"
_OID_PHYSICAL_NAME = "1.3.6.1.2.1.47.1.1.1.1.7"
_OID_PHYSICAL_SERIAL = "1.3.6.1.2.1.47.1.1.1.1.11"
_OID_PHYSICAL_MODEL = "1.3.6.1.2.1.47.1.1.1.1.13"
_OID_MTXR_OPTICAL = "1.3.6.1.4.1.14988.1.1.19"

_SCALE_FACTOR = {
    1: 1e24, 2: 1e21, 3: 1e18, 4: 1e15, 5: 1e12, 6: 1e9, 7: 1e6, 8: 1e3,
    9: 1.0, 10: 1e-3, 11: 1e-6, 12: 1e-9, 13: 1e-12, 14: 1e-15, 15: 1e-18,
}
_SENSOR_TYPE_VOLTS_DC = 4
_SENSOR_TYPE_AMPERES = 5
_SENSOR_TYPE_CELSIUS = 8
_SENSOR_TYPE_DBM = 14

def _index_of(oid_str):
    return oid_str.split(".")[-1]


def _apply_scale(value, scale, precision):
    factor = _SCALE_FACTOR.get(int(scale or 9), 1.0)
    divisor = 10 ** int(precision or 0)
    try:
        return float(value) * factor / divisor
    except (TypeError, ValueError):
        return None


_DIRECTION_RE = re.compile(r"\b(rx|tx|receive|receiv|transmit|input|output)\b", re.IGNORECASE)
_PORT_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_/.\-]*")
_PORT_SUFFIX_RE = re.compile(
    r"[-_.]?(rx|tx|receive|receiv|transmit|input|output|power|dbm|temp|temperature|voltage|bias|current)([-_.][A-Za-z0-9]+)*$",
    re.IGNORECASE,
)


def _port_key(name, descr):
    """Kunci port dari entPhysicalName/Descr (heuristik deterministik, tanpa mengarang)."""
    text = (name or descr or "").strip()
    if not text:
        return None
    match = _PORT_TOKEN_RE.search(text)
    token = (match.group(0) if match else text).strip("-_. ")
    # Buang akhiran parameter (mis. "sfp-sfpplus1-rx-power" → "sfp-sfpplus1").
    while True:
        stripped = _PORT_SUFFIX_RE.sub("", token).strip("-_. ")
        if stripped == token:
            break
        token = stripped
    return token or None


def _group_sensors(sensors, physicals):
    """Gabungkan kolom entSensor + entPhysicalTable per PORT.

    sensors: {index: {"type","scale","precision","value","status"}}
    physicals: {index: {"name","descr","serial","model"}}
    Kembalikan (ports: [...], notes: [str]).

    Arah Rx/Tx: dari label sensor bila menyebut rx/tx; bila sebuah port memiliki
    tepat dua sensor dBm tanpa label arah, dipakai konvensi fisika transceiver
    (Rx ≤ Tx) — dicatat di notes agar diverifikasi dengan snmpwalk.
    """
    # 1) Kumpulkan nilai per kunci port.
    groups = {}
    unassigned = {}
    for index, sensor in sensors.items():
        try:
            sensor_type = int(sensor.get("type") or 0)
        except (TypeError, ValueError):
            continue
        if sensor_type not in (_SENSOR_TYPE_DBM, _SENSOR_TYPE_CELSIUS,
                               _SENSOR_TYPE_VOLTS_DC, _SENSOR_TYPE_AMPERES):
            continue
        actual = _apply_scale(sensor.get("value"), sensor.get("scale"), sensor.get("precision"))
        if actual is None:
            continue
        phy = physicals.get(index, {})
        name = (phy.get("name") or phy.get("descr") or f"sensor-{index}").strip()
        label = f"{phy.get('name') or ''} {phy.get('descr') or ''}".lower()
        port = _port_key(phy.get("name"), phy.get("descr")) or f"sensor-{index}"
        group = groups.setdefault(
            port,
            {
                "if_name": port, "running": True, "module_present": True,
                "rx_loss": False, "tx_fault": False, "ddm_supported": True,
                "rx_dbm": None, "tx_dbm": None,
                "temp_c": None, "voltage_v": None, "bias_ma": None,
                "vendor": None, "part_no": (phy.get("model") or None),
                "serial": (phy.get("serial") or None),
                "type": None, "wavelength_nm": None, "link_length_m": None,
                "_dbm": [],
            },
        )
        # Serial/model diambil dari sensor pertama yang memilikinya.
        if not group["serial"]:
            group["serial"] = phy.get("serial") or None
        if not group["part_no"]:
            group["part_no"] = phy.get("model") or None

        direction = None
        match = _DIRECTION_RE.search(label)
        if match:
            word = match.group(1)
            if word in ("rx", "receive", "receiv", "input"):
                direction = "rx"
            else:
                direction = "tx"

        if sensor_type == _SENSOR_TYPE_DBM:
            if direction == "rx":
                group["rx_dbm"] = actual
            elif direction == "tx":
                group["tx_dbm"] = actual
            else:
                group["_dbm"].append(actual)
        elif sensor_type == _SENSOR_TYPE_CELSIUS:
            group["temp_c"] = actual
        elif sensor_type == _SENSOR_TYPE_VOLTS_DC:
            group["voltage_v"] = actual
        elif sensor_type == _SENSOR_TYPE_AMPERES:
            group["bias_ma"] = actual

    # 2) Pasangkan dBm tanpa arah dengan konvensi fisika bila tepat dua per port.
    notes = []
    for port, group in groups.items():
        leftover = [v for v in group.pop("_dbm")]
        missing_rx = group["rx_dbm"] is None
        missing_tx = group["tx_dbm"] is None
        if missing_rx and missing_tx and len(leftover) == 2:
            group["rx_dbm"] = min(leftover)
            group["tx_dbm"] = max(leftover)
            notes.append(
                f"{port}: arah Rx/Tx ditentukan dari konvensi daya transceiver "
                f"(Rx ≤ Tx); verifikasi dengan snmpwalk ke perangkat nyata."
            )
        elif leftover:
            notes.append(
                f"{port}: {len(leftover)} sensor dBm tanpa arah yang diketahui; rx/tx dibiarkan kosong."
            )
    return list(groups.values()), notes


async def _fetch_optical_snmp(config):
    host = config["ip"]
    port = int(config.get("port") or 161)

    auth, error = _auth_data(config)
    if error is not None:
        return {**error, "method": "snmp-optical", "ports": []}

    engine = hlapi.SnmpEngine()
    target = await hlapi.UdpTransportTarget.create(
        (host, port), timeout=SNMP_TIMEOUT_SECONDS, retries=SNMP_RETRIES
    )

    # 1) Walk tabel sensor dulu — fisik & mtxrOptical hanya diperlukan bila ada kandidat DDM.
    type_rows = await _walk(engine, auth, target, _OID_SENSOR_TYPE)
    scale_rows = await _walk(engine, auth, target, _OID_SENSOR_SCALE)
    precision_rows = await _walk(engine, auth, target, _OID_SENSOR_PRECISION)
    value_rows = await _walk(engine, auth, target, _OID_SENSOR_VALUE)
    status_rows = await _walk(engine, auth, target, _OID_SENSOR_STATUS)

    sensors = {}
    for oid_str, value in type_rows:
        sensors.setdefault(_index_of(oid_str), {})["type"] = value
    for oid_str, value in scale_rows:
        sensors.setdefault(_index_of(oid_str), {})["scale"] = value
    for oid_str, value in precision_rows:
        sensors.setdefault(_index_of(oid_str), {})["precision"] = value
    for oid_str, value in value_rows:
        sensors.setdefault(_index_of(oid_str), {})["value"] = value
    for oid_str, value in status_rows:
        sensors.setdefault(_index_of(oid_str), {})["status"] = value

    sensor_types = set()
    for sensor in sensors.values():
        try:
            sensor_types.add(int(sensor.get("type") or 0))
        except (TypeError, ValueError):
            continue
    ddm_candidates = bool(sensor_types & {_SENSOR_TYPE_DBM, _SENSOR_TYPE_CELSIUS,
                                          _SENSOR_TYPE_VOLTS_DC, _SENSOR_TYPE_AMPERES})

    physicals = {}
    mtxr_rows = []
    if ddm_candidates:
        descr_rows = await _walk(engine, auth, target, _OID_PHYSICAL_DESCR)
        name_rows = await _walk(engine, auth, target, _OID_PHYSICAL_NAME)
        serial_rows = await _walk(engine, auth, target, _OID_PHYSICAL_SERIAL)
        model_rows = await _walk(engine, auth, target, _OID_PHYSICAL_MODEL)
        mtxr_rows = await _walk(engine, auth, target, _OID_MTXR_OPTICAL)
        for oid_str, value in descr_rows:
            physicals.setdefault(_index_of(oid_str), {})["descr"] = value
        for oid_str, value in name_rows:
            physicals.setdefault(_index_of(oid_str), {})["name"] = value
        for oid_str, value in serial_rows:
            physicals.setdefault(_index_of(oid_str), {})["serial"] = value
        for oid_str, value in model_rows:
            physicals.setdefault(_index_of(oid_str), {})["model"] = value

    ports, notes = _group_sensors(sensors, physicals)
    detail_parts = []
    if notes:
        detail_parts.extend(notes)
    if mtxr_rows:
        detail_parts.append(
            f"mtxrOptical: {len(mtxr_rows)} leaf ditemukan (struktur belum diverifikasi; angka tidak dipakai)."
        )

    return {
        "method": "snmp-optical",
        "ok": bool(ports),
        "reason": None if ports else "snmp_no_response",
        "detail": " ".join(detail_parts) or ("Tidak ada entSensor dBm/celsius yang ditemukan." if not ports else None),
        "ports": ports,
        "sensors_seen": len(sensors),
    }


def check_optical_snmp(config):
    """Fallback SNMP optik. Hasil terstruktur, tidak pernah melempar."""
    try:
        return asyncio.run(_fetch_optical_snmp(config))
    except Exception as exc:
        return {"method": "snmp-optical", "ok": False, "reason": "error",
                "detail": str(exc), "ports": []}
