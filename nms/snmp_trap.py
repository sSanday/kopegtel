"""nms.snmp_trap — receiver SNMP trap (UDP) + builder paket untuk tester.

Receiver mendengarkan UDP (default port 1162), memecah paket SNMP v2c/v1
secara manual (tanpa dependensi pysnmp), memvalidasi community, menyimpan
trap ke tabel ``snmp_traps``, menulis baris system_logs (agar muncul di halaman
Logs/CSV), dan mengirim peringatan (send_alert) untuk linkDown/coldStart
dengan dedupe singkat per sumber.

Builder ``build_v2c_trap``/``build_v1_trap`` dipakai oleh
``scripts/send_test_trap.py`` untuk menguji receiver end-to-end.

Perilaku community: setting ``snmp_trap_community`` kosong -> terima semua
community; diisi -> hanya community yang cocok yang diproses lengkap
(community lain tetap dicatat sebagai community_ok=0 tanpa alert).
"""

import socket
import threading
import time
from datetime import datetime

from nms.db import _commit_with_retry, _insert_system_log, get_db, get_setting
from nms.thread_safe import ThreadSafeDict

SNMP_TRAP_PORT_DEFAULT = 1162
SNMP_TRAP_PORT_MIN = 1024
SNMP_TRAP_PORT_MAX = 65535
SNMP_TRAP_COMMUNITY_MAX = 128
TRAP_ALERT_DEDUPE_S = 60
TRAP_RAW_MAX = 400

# OIDs baku yang dibaca receiver (teks dotted dengan titik awal, sesuai hasil decode).
OID_SYSUPTIME = ".1.3.6.1.2.1.1.3.0"
OID_SNMPTRAPOID = ".1.3.6.1.6.3.1.1.4.1.0"
OID_IFINDEX_PREFIX = ".1.3.6.1.2.1.2.2.1.1."
OID_IFOPER_PREFIX = ".1.3.6.1.2.1.2.2.1.10."
OID_SNMPTRAP_BASE = ".1.3.6.1.6.3.1.1.5."

# Pemetaan snmpTrapOID bawaan (suffix dari OID_SNMPTRAP_BASE).
_SNMP_GENERIC_V2 = {
    1: "coldstart",
    2: "warmstart",
    3: "linkdown",
    4: "linkup",
    5: "authfailure",
    6: "egpneigh",
}

# Pemetaan generic-trap v1.
_SNMP_GENERIC_V1 = {
    0: "coldstart",
    1: "warmstart",
    2: "linkdown",
    3: "linkup",
    4: "authfailure",
    5: "egpneigh",
}

_TRAP_ALERT_CATEGORIES = {"linkdown", "coldstart"}
_trap_alert_mem = ThreadSafeDict()  # (source_ip, category) -> time.time()

_trap_thread = None
_trap_stop = threading.Event()
_trap_lock = threading.Lock()


# ---------------------------------------------------------------------------
# PEMECAHAN BER
# ---------------------------------------------------------------------------


def _read_tlv(data, pos):
    """Baca satu TLV pada pos -> (tag, value_bytes, mulai_nilai, pos_akhir) | None."""
    if pos >= len(data):
        return None
    tag = data[pos]
    pos += 1
    if pos >= len(data):
        return None
    first = data[pos]
    pos += 1
    if first < 0x80:
        length = first
    else:
        octets = first & 0x7F
        if octets == 0 or octets > 3:
            return None
        if pos + octets > len(data):
            return None
        length = int.from_bytes(data[pos : pos + octets], "big")
        pos += octets
    if pos + length > len(data):
        return None
    return tag, data[pos : pos + length], pos, (pos + length)


def _ber_int(raw):
    """Integer BER (two's complement)."""
    if not raw:
        return 0
    return int.from_bytes(raw, "big", signed=True)


def _ber_oid(raw):
    """Contents OID -> teks dotted (base-128; sub-nilai pertama = a1*40+a2)."""
    if not raw:
        return ""
    # Cari akhir sub-nilai pertama (byte tanpa bit kontinuasi).
    i = 0
    val = 0
    while i < len(raw) and raw[i] & 0x80:
        val = (val << 7) | (raw[i] & 0x7F)
        i += 1
    if i >= len(raw):
        return ""  # OID terpotong
    val = (val << 7) | (raw[i] & 0x7F)
    i += 1
    if val < 40:
        arcs = [0, val]
    elif val < 80:
        arcs = [1, val - 40]
    else:
        arcs = [2, val - 80]
    val = 0
    for b in raw[i:]:
        val = (val << 7) | (b & 0x7F)
        if not b & 0x80:
            arcs.append(val)
            val = 0
    if raw[-1] & 0x80:
        return ""  # OID terpotong
    return "." + ".".join(str(a) for a in arcs)


def _encode_oid(oid_text):
    """Teks dotted -> contents OID (sub-nilai pertama base-128: a1*40+a2)."""
    if not isinstance(oid_text, str):
        return b""
    digits = oid_text.strip().strip(".").split(".")
    try:
        arcs = [int(d) for d in digits if d != ""]
    except ValueError:
        return b""
    if len(arcs) < 2 or arcs[0] > 2 or any(a < 0 for a in arcs):
        return b""
    if arcs[0] < 2 and arcs[1] > 39:
        return b""

    def _base128(v):
        out = [v & 0x7F]
        v >>= 7
        while v:
            out.append((v & 0x7F) | 0x80)
            v >>= 7
        return bytes(reversed(out))

    out = bytearray()
    out += _base128(arcs[0] * 40 + arcs[1])
    for arc in arcs[2:]:
        out += _base128(arc)
    return bytes(out)


def _encode_len(n):
    if n < 0x80:
        return bytes([n])
    if n < 0x100:
        return bytes([0x81, n])
    return bytes([0x82, (n >> 8) & 0xFF, n & 0xFF])


def _encode_tlv(tag, payload):
    return bytes([tag]) + _encode_len(len(payload)) + payload


def _decode_varbind_value(tag, raw):
    """Value varbind -> (repr, int_value | None)."""
    if tag == 0x02:
        v = _ber_int(raw)
        return str(v), v
    if tag == 0x04:
        try:
            s = raw.decode("utf-8", "replace")
        except Exception:
            s = raw.hex()
        return s, None
    if tag == 0x05:
        return "null", None
    if tag == 0x06:
        return _ber_oid(raw), None
    if tag == 0x40:
        if len(raw) == 4:
            return ".".join(str(b) for b in raw), None
        return raw.hex(), None
    if tag in (0x41, 0x42, 0x43):
        v = int.from_bytes(raw, "big") if raw else 0
        return str(v), v
    if not raw:
        return "", None
    return raw.hex(), None


def decode_trap(data):
    """Pecah datagram SNMP trap v2c/v1 -> dict | None.

    Kembalikan dict berisi version, community, pdu_tag, trap_oid, generic,
    specific, sys_uptime, if_index, if_oper, varbinds (list (oid, repr)).
    """
    if not isinstance(data, (bytes, bytearray)) or len(data) < 20:
        return None
    head = _read_tlv(bytes(data), 0)
    if not head or head[0] != 0x30:
        return None
    pos = head[2]
    version = None
    community = ""
    # INTEGER version
    t = _read_tlv(data, pos)
    if not t or t[0] != 0x02 or len(t[1]) == 0 or len(t[1]) > 8:
        return None
    try:
        version = _ber_int(t[1])
    except Exception:
        return None
    pos = t[3]
    # OCTET STRING community
    t = _read_tlv(data, pos)
    if not t or t[0] != 0x04:
        return None
    community = t[1].decode("utf-8", "replace")
    pos = t[3]
    # PDU trap
    t = _read_tlv(data, pos)
    if not t:
        return None
    pdu_tag = t[0]
    if pdu_tag not in (0xA7, 0xA4):
        return None
    pdu = t[1]

    result = {
        "version": "2c" if version == 1 else "v1",
        "community": community,
        "pdu_tag": pdu_tag,
        "trap_oid": "",
        "generic": None,
        "specific": None,
        "sys_uptime": None,
        "if_index": None,
        "if_oper": None,
        "varbinds": [],
    }

    if pdu_tag == 0xA7:
        # SNMPv2-Trap: request-id, error-status, error-index, varbind-list
        p = 0
        for _ in range(3):
            t2 = _read_tlv(pdu, p)
            if not t2 or t2[0] != 0x02:
                return None
            p = t2[3]
        t2 = _read_tlv(pdu, p)
        if not t2 or t2[0] != 0x30:
            return None
        _parse_varbind_list(result, pdu, t2[2])
    else:
        # v1: enterprise, agent-addr, generic, specific, timeticks, varbinds
        p = 0
        t2 = _read_tlv(pdu, p)
        if not t2 or t2[0] != 0x06:
            return None
        result["trap_oid"] = _ber_oid(t2[1])
        p = t2[3]
        t2 = _read_tlv(pdu, p)
        if not t2:
            return None
        p = t2[3]
        t2 = _read_tlv(pdu, p)
        if not t2 or t2[0] != 0x02:
            return None
        try:
            result["generic"] = _ber_int(t2[1])
        except Exception:
            return None
        p = t2[3]
        t2 = _read_tlv(pdu, p)
        if not t2 or t2[0] != 0x02:
            return None
        try:
            result["specific"] = _ber_int(t2[1])
        except Exception:
            return None
        p = t2[3]
        t2 = _read_tlv(pdu, p)
        if not t2 or t2[0] != 0x43:
            return None
        try:
            result["sys_uptime"] = int.from_bytes(t2[1], "big") if t2[1] else 0
        except Exception:
            return None
        p = t2[3]
        t2 = _read_tlv(pdu, p)
        if not t2 or t2[0] != 0x30:
            return None
        _parse_varbind_list(result, pdu, t2[2])
    _finalize(result)
    return result


def _parse_varbind_list(result, data, pos):
    while pos < len(data):
        t = _read_tlv(data, pos)
        if not t:
            break
        pos = t[3]
        if t[0] != 0x30:
            continue
        vb = t[1]
        t2 = _read_tlv(vb, 0)
        if not t2 or t2[0] != 0x06:
            continue
        oid = _ber_oid(t2[1])
        t3 = _read_tlv(vb, t2[3])
        if not t3:
            value, _iv = "", None
        else:
            value, _iv = _decode_varbind_value(t3[0], t3[1])
            if oid == OID_SYSUPTIME and _iv is not None:
                result["sys_uptime"] = _iv
            if oid == OID_SNMPTRAPOID:
                result["trap_oid"] = value
            if oid.startswith(OID_IFINDEX_PREFIX) and _iv is not None:
                tail = oid[len(OID_IFINDEX_PREFIX) :]
                if tail.isdigit() and result["if_index"] is None:
                    result["if_index"] = int(tail)
            if oid.startswith(OID_IFOPER_PREFIX) and _iv is not None:
                result["if_oper"] = _iv
        result["varbinds"].append((oid, value))


def _finalize(result):
    """Tentukan category dari snmpTrapOID (v2c) atau generic-trap (v1)."""
    trap_oid = (result.get("trap_oid") or "").strip()
    if result.get("pdu_tag") == 0xA7:
        suffix = None
        if trap_oid.startswith(OID_SNMPTRAP_BASE):
            tail = trap_oid[len(OID_SNMPTRAP_BASE) :].split(".")[0]
            try:
                suffix = int(tail)
            except ValueError:
                suffix = None
        if suffix in _SNMP_GENERIC_V2:
            result["category"] = _SNMP_GENERIC_V2[suffix]
        else:
            result["category"] = "other"
    else:
        result["category"] = _SNMP_GENERIC_V1.get(result.get("generic"), "other")
    if not result.get("category"):
        result["category"] = "other"


# ---------------------------------------------------------------------------
# PEMBANGUN PAKET (untuk tester)
# ---------------------------------------------------------------------------


TRAP_TYPES = {
    # type -> (v2c suffix di OID_SNMPTRAP_BASE, v1 generic)
    "linkdown": (3, 2),
    "linkup": (4, 3),
    "coldstart": (1, 0),
    "warmstart": (2, 1),
}
TRAP_TYPE_DESCRIPTIONS = {
    "linkdown": "ifIndex %d interface turun",
    "linkup": "ifIndex %d interface naik",
    "coldstart": "agent reboot dingin",
    "warmstart": "agent reboot hangat",
}


def _vb(oid, tag, payload):
    return _encode_tlv(
        0x30, _encode_tlv(0x06, _encode_oid(oid)) + _encode_tlv(tag, payload)
    )


def _vb_int(oid, value):
    payload = value.to_bytes((value.bit_length() + 8) // 8 or 1, "big", signed=True)
    return _vb(oid, 0x02, payload)


def _vb_str(oid, text):
    return _vb(oid, 0x04, str(text or "").encode("utf-8", "replace"))


def _tik_payload(ticks):
    ticks = max(0, int(ticks))
    return ticks.to_bytes((ticks.bit_length() + 8) // 8 or 1, "big")


def _vb_tik(oid, ticks):
    return _vb(oid, 0x43, _tik_payload(ticks))


def build_v2c_trap(
    community,
    trap_oid=None,
    if_index=None,
    if_oper=None,
    sysuptime=None,
    description=None,
    extra_varbinds=None,
):
    """Bangun paket SNMPv2c-Trap (PDU 0xA7) lengkap dengan varbind baku."""
    community = community if community is not None else "public"
    if trap_oid is None:
        trap_oid = OID_SNMPTRAP_BASE + "3"  # linkDown
    uptime = sysuptime if sysuptime is not None else int(time.monotonic() * 100)
    varbinds = bytearray()
    varbinds += _vb_tik(OID_SYSUPTIME, uptime)
    varbinds += _vb(OID_SNMPTRAPOID, 0x06, _encode_oid(trap_oid))
    if if_index is not None:
        varbinds += _vb_int(f"{OID_IFINDEX_PREFIX}{if_index}", int(if_index))
        if if_oper is not None:
            varbinds += _vb_int(f"{OID_IFOPER_PREFIX}{if_index}", int(if_oper))
        varbinds += _vb_str(f"1.3.6.1.2.1.2.2.1.2.{if_index}", f"eth{if_index}")
    if description:
        varbinds += _vb_str("1.3.6.1.4.1.9999.1", str(description))
    for vb_oid, vb_tag, vb_payload in extra_varbinds or []:
        varbinds += _vb(vb_oid, vb_tag, vb_payload)
    pdu = (
        _encode_tlv(0x02, b"\x00")  # request-id 0
        + _encode_tlv(0x02, b"\x00")  # error-status
        + _encode_tlv(0x02, b"\x00")  # error-index
        + _encode_tlv(0x30, bytes(varbinds))
    )
    msg = (
        _encode_tlv(0x02, b"\x01")  # versi v2c
        + _encode_tlv(0x04, str(community).encode("utf-8", "replace"))
        + _encode_tlv(0xA7, pdu)
    )
    return _encode_tlv(0x30, msg)


def build_v1_trap(
    community,
    generic=2,
    specific=0,
    enterprise_oid="1.3.6.1.4.1.8072.9999",
    agent_ip="127.0.0.1",
    sysuptime=None,
    if_index=None,
    if_oper=None,
    description=None,
):
    """Bangun paket trap SNMPv1 (PDU 0xA4)."""
    community = community if community is not None else "public"
    if isinstance(agent_ip, str):
        try:
            raw_ip = bytes(int(x) for x in agent_ip.split("."))
        except ValueError:
            raw_ip = b"\x7f\x00\x00\x01"
        if len(raw_ip) != 4:
            raw_ip = b"\x7f\x00\x00\x01"
    else:
        raw_ip = agent_ip if isinstance(agent_ip, bytes) else b"\x7f\x00\x00\x01"
    uptime = sysuptime if sysuptime is not None else int(time.monotonic() * 100)
    varbinds = bytearray()
    if if_index is not None:
        varbinds += _vb_int(f"{OID_IFINDEX_PREFIX}{if_index}", int(if_index))
        if if_oper is not None:
            varbinds += _vb_int(f"{OID_IFOPER_PREFIX}{if_index}", int(if_oper))
    if description:
        varbinds += _vb_str("1.3.6.1.4.1.9999.1", str(description))
    pdu = (
        _encode_tlv(0x06, _encode_oid(enterprise_oid))
        + _encode_tlv(0x40, raw_ip)
        + _encode_tlv(0x02, int(generic).to_bytes(1, "big"))
        + _encode_tlv(0x02, int(specific).to_bytes(1, "big"))
        + _encode_tlv(0x43, _tik_payload(uptime))  # timeticks
        + _encode_tlv(0x30, bytes(varbinds))
    )
    msg = (
        _encode_tlv(0x02, b"\x00")  # versi v1
        + _encode_tlv(0x04, str(community).encode("utf-8", "replace"))
        + _encode_tlv(0xA4, pdu)
    )
    return _encode_tlv(0x30, msg)


# ---------------------------------------------------------------------------
# RECEIVER
# ---------------------------------------------------------------------------


def trap_receiver_settings():
    """(enabled, port, community) dari settings, tervalidasi."""
    try:
        enabled = int(get_setting("snmp_trap_enabled", 1, type_cast=int)) == 1
    except (ValueError, TypeError):
        enabled = True
    try:
        port = int(float(get_setting("snmp_trap_port", SNMP_TRAP_PORT_DEFAULT)))
    except (ValueError, TypeError):
        port = SNMP_TRAP_PORT_DEFAULT
    port = min(SNMP_TRAP_PORT_MAX, max(SNMP_TRAP_PORT_MIN, port))
    community = str(get_setting("snmp_trap_community", "", type_cast=str) or "").strip()
    return enabled, port, community[:SNMP_TRAP_COMMUNITY_MAX]


def _trap_alert_text(row, source_ip):
    cat = row.get("category") or "other"
    if cat == "linkdown":
        ifx = row.get("if_index")
        ifx_txt = f" (ifIndex {ifx})" if ifx is not None else ""
        return (
            f"🚨 *SNMP TRAP LINKDOWN*\n"
            f"Sumber: {source_ip}{ifx_txt}\n"
            f"Interface turun (trap bawaan perangkat).\n"
            f"Ping/cek manual ke perangkat bila itu klien penting."
        )
    if cat == "coldstart":
        return (
            f"❄ *SNMP TRAP COLDSTART*\n"
            f"Sumber: {source_ip} — perangkat baru selesai reboot."
        )
    return f"📡 *SNMP TRAP* {cat} dari {source_ip}"


def _mark_system_log(c, row, source_ip):
    event_type = "TRAP_" + (row["category"] or "other").upper()
    message_bits = []
    if row["if_index"] is not None:
        message_bits.append(f"ifIndex {row['if_index']}")
    if row["if_oper"] is not None:
        message_bits.append(f"ifOperStatus {row['if_oper']}")
    if row["trap_oid"]:
        message_bits.append(f"trapOID {row['trap_oid']}")
    if row["generic"] is not None:
        message_bits.append(f"generic {row['generic']}")
    if row.get("community_ok") == 0:
        message_bits.append("community TIDAK valid")
    _insert_system_log(
        c,
        event_type,
        source_ip,
        "SNMP trap " + ("; ".join(message_bits) or ""),
    )


def handle_trap_datagram(data, addr):
    """Decode -> simpan DB -> system_logs -> alert. Kembalikan row dict | None."""
    src_ip = addr[0] if addr else "0.0.0.0"
    src_port = addr[1] if addr and len(addr) > 1 else 0
    parsed = decode_trap(data)
    if not parsed:
        return None
    _enabled, _port, expected_community = trap_receiver_settings()
    community_ok = True
    if expected_community and parsed["community"] != expected_community:
        community_ok = False
    category = parsed.get("category") or "other"
    raw_bits = []
    for oid, value in parsed.get("varbinds", [])[:8]:
        raw_bits.append(f"{oid}={value}")
    row = {
        "received_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source_ip": src_ip,
        "source_port": int(src_port or 0),
        "community_ok": 1 if community_ok else 0,
        "snmp_version": parsed.get("version") or "2c",
        "trap_oid": (parsed.get("trap_oid") or "")[:120],
        "generic": parsed.get("generic"),
        "specific": parsed.get("specific"),
        "if_index": parsed.get("if_index"),
        "if_oper": parsed.get("if_oper"),
        "sys_uptime": parsed.get("sys_uptime"),
        "category": category,
        "message": _trap_alert_text_gray(parsed, src_ip),
        "raw_vars": "; ".join(raw_bits)[:TRAP_RAW_MAX],
    }
    try:
        conn, c = get_db()
        try:
            c.execute(
                "INSERT INTO snmp_traps (received_at, source_ip, source_port, "
                "community_ok, snmp_version, trap_oid, generic, specific, "
                "if_index, if_oper, sys_uptime, category, message, raw_vars) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    row["received_at"],
                    row["source_ip"],
                    row["source_port"],
                    row["community_ok"],
                    row["snmp_version"],
                    row["trap_oid"],
                    row["generic"],
                    row["specific"],
                    row["if_index"],
                    row["if_oper"],
                    row["sys_uptime"],
                    row["category"],
                    row["message"],
                    row["raw_vars"],
                ),
            )
            _mark_system_log(c, row, src_ip)
            _commit_with_retry(conn)
        finally:
            conn.close()
    except Exception as e:
        print(f"[TRAP] simpan gagal: {e}")
        return None
    if community_ok and category in _TRAP_ALERT_CATEGORIES:
        _maybe_send_alert(row, src_ip)
    return row


def _trap_alert_text_gray(parsed, src_ip):
    """Ringkasan satu baris utk kolom message (tanpa emoji)."""
    cat = parsed.get("category") or "other"
    bits = [f"trap {cat}"]
    if parsed.get("if_index") is not None:
        bits.append(f"ifIndex {parsed['if_index']}")
    if parsed.get("generic") is not None:
        bits.append(f"generic {parsed['generic']}")
    return f"{src_ip}: " + "; ".join(bits)


def _maybe_send_alert(row, src_ip):
    """Kirim send_alert dengan dedupe per (ip, category) TRAP_ALERT_DEDUPE_S."""
    key = (src_ip, row["category"])
    now = time.time()
    if now - float(_trap_alert_mem.get(key, 0)) < TRAP_ALERT_DEDUPE_S:
        return
    _trap_alert_mem.set(key, now)
    severity = "warning" if row["category"] == "linkdown" else "info"
    try:
        from nms.notify import send_alert

        send_alert(_trap_alert_text(row, src_ip), severity=severity, event="snmp_trap")
    except Exception as e:
        print(f"[TRAP] alert gagal: {e}")


def _trap_run(sock):
    tname = threading.current_thread().name
    print(f"[TRAP] receiver aktif di {sock.getsockname()[0]}:{sock.getsockname()[1]}")
    while not _trap_stop.is_set():
        try:
            data, addr = sock.recvfrom(65535)
        except socket.timeout:
            continue
        except OSError:
            break
        except Exception as e:  # recv gangguan aneh: tetap lanjut
            print(f"[TRAP] recv gagal: {e}")
            continue
        try:
            handle_trap_datagram(data, addr)
        except Exception as e:
            print(f"[TRAP] proses trap gagal ({tname}): {e}")
    try:
        sock.close()
    except Exception:
        pass
    print("[TRAP] receiver berhenti")


def start_trap_receiver(port=None):
    """Mulai thread receiver (idempoten). False bila mati/binding gagal."""
    global _trap_thread
    with _trap_lock:
        if _trap_thread is not None and _trap_thread.is_alive():
            return True
        if os_env_disabled():
            return False
        enabled, default_port, _comm = trap_receiver_settings()
        if not enabled:
            return False
        port = int(port) if port else default_port
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("0.0.0.0", port))
        except OSError as e:
            print(f"[TRAP] bind {port} gagal ({e}) — receiver tidak jalan")
            try:
                sock.close()
            except Exception:
                pass
            return False
        sock.settimeout(1.0)
        _trap_stop.clear()
        _trap_thread = threading.Thread(
            target=_trap_run, args=(sock,), name="snmp-trap-receiver", daemon=True
        )
        _trap_thread.start()
        return True


def stop_trap_receiver():
    """Hentikan thread receiver (untuk test/kenyamanan ops)."""
    global _trap_thread
    _trap_stop.set()
    thr = _trap_thread
    if thr is not None and thr is not threading.current_thread():
        thr.join(timeout=3.0)
    _trap_thread = None
    _trap_alert_mem.clear()


def os_env_disabled():
    return _env_flag("NMS_DISABLE_TRAP_RECEIVER") or _env_flag(
        "NMS_DISABLE_TRAP"
    )  # alias pendek


def _env_flag(name):
    import os

    return str(os.environ.get(name, "")).strip().lower() in ("1", "true", "yes")
