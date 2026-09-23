"""nms.snmp — SNMP data-plane: codec BER, GET/NEXT/WALK, discovery iface.

Dipindah dari app.py (modul 4, P3) tanpa perubahan perilaku. Murni tanpa
dependensi Flask/DB (hanya socket) agar mudah dites.
get_snmp_bandwidth & polling tetap di app.py (terikat fake test & state).
"""

import os
import re
import socket


def _encode_snmp_v1_get(community, oid_list, _pdu_tag=0xA0):
    def encode_oid(oid):
        try:
            parts = [int(x) for x in str(oid).split(".") if x != ""]
        except (ValueError, TypeError):
            raise ValueError(f"OID tidak valid: {oid!r}")
        if (
            len(parts) < 2
            or parts[0] < 0
            or parts[0] > 2
            or parts[1] < 0
            or any(p < 0 for p in parts)
        ):
            raise ValueError(f"OID tidak valid: {oid!r}")
        first = parts[0] * 40 + parts[1]
        encoded = bytes([first])
        for p in parts[2:]:
            if p == 0:
                encoded += bytes([0])
            else:
                segs = []
                while p > 0:
                    segs.append(p & 0x7F)
                    p >>= 7
                segs.reverse()
                for i, s in enumerate(segs):
                    encoded += bytes([s | (0x80 if i < len(segs) - 1 else 0)])
        return b"\x06" + _encode_len(len(encoded)) + encoded

    def _encode_len(n):
        if n < 0:
            raise ValueError("length negatif")
        if n < 128:
            return bytes([n])
        lb = n.to_bytes((n.bit_length() + 7) // 8, "big")
        if len(lb) > 4:
            raise ValueError("length terlalu besar")
        return bytes([0x80 | len(lb)]) + lb

    def encode_tlv(tag, value):
        return bytes([tag]) + _encode_len(len(value)) + value

    req_id = b"\x02\x01\x01"
    varbinds = b""
    for oid in oid_list:
        oid_enc = encode_oid(oid)
        varbinds += encode_tlv(0x30, oid_enc + b"\x05\x00")
    varbind_list = encode_tlv(0x30, varbinds)
    pdu = encode_tlv(_pdu_tag, req_id + b"\x02\x01\x00\x02\x01\x00" + varbind_list)
    comm_bytes = str(community or "")[:128].encode()

    _ver = (
        1 if os.environ.get("SNMP_VERSION", "1").strip().lower() in ("2", "2c") else 0
    )
    version = bytes([0x02, 0x01, _ver])
    comm_tlv = encode_tlv(0x04, comm_bytes)
    return encode_tlv(0x30, version + comm_tlv + pdu)


def _ber_read_tlv(data, pos):
    try:
        if pos + 2 > len(data):
            return None
        tag = data[pos]
        first = data[pos + 1]
        if first < 128:
            ln, hdr = first, 2
        else:
            n = first & 0x7F
            if n == 0 or n > 4 or pos + 2 + n > len(data):
                return None
            ln = int.from_bytes(data[pos + 2 : pos + 2 + n], "big")
            hdr = 2 + n
        end = pos + hdr + ln
        if ln < 0 or end > len(data):
            return None
        return tag, bytes(data[pos + hdr : end]), end
    except Exception:
        return None


_SNMP_VALUE_TAGS = (0x02, 0x41, 0x42, 0x43, 0x46)


def _extract_snmp_values(resp):
    found = []

    def walk(buf):
        pos, last_was_oid = 0, False
        while pos < len(buf):
            t = _ber_read_tlv(buf, pos)
            if t is None:
                break
            tag, val, pos = t
            if tag == 0x06:
                last_was_oid = True
                continue
            if tag & 0x20:

                last_was_oid = False
                walk(val)
                continue
            if last_was_oid and tag in _SNMP_VALUE_TAGS and 1 <= len(val) <= 9:
                found.append(int.from_bytes(val, "big", signed=(tag == 0x02)))
            last_was_oid = False

    try:
        walk(bytes(resp))
    except Exception:
        pass
    return found


def _decode_oid(raw):
    """Bytes OID -> '1.3.6...'. None bila truncated/invalid."""
    try:
        raw = bytes(raw)
        if not raw:
            return None
        arcs = [raw[0] // 40, raw[0] % 40]
        val, complete = 0, True
        for byte in raw[1:]:
            val = (val << 7) | (byte & 0x7F)
            if byte & 0x80:
                complete = False
            else:
                arcs.append(val)
                val, complete = 0, True
        if not complete:
            return None
        return ".".join(str(a) for a in arcs)
    except Exception:
        return None


def _tlv_children(buf):
    pos, out = 0, []
    buf = bytes(buf)
    while pos < len(buf):
        t = _ber_read_tlv(buf, pos)
        if t is None:
            break
        out.append(t)
        pos = t[2]
    return out


def _extract_snmp_varbinds(resp):
    """Urai respons -> list (oid_str, tag, ival, sval) berurutan.

    ival untuk INTEGER/Counter/Gauge/TimeTicks (<=8 byte), sval untuk
    OCTET STRING. Tag error (noSuchObject/Instance/endOfMibView) -> nilai None.
    """
    out = []
    try:
        top = _tlv_children(resp)
        if len(top) != 1 or top[0][0] != 0x30:
            return out
        msg = _tlv_children(top[0][1])
        if len(msg) < 3:
            return out
        pdu = _tlv_children(msg[2][1])
        if len(pdu) < 4:
            return out
        for vb in _tlv_children(pdu[3][1]):
            if vb[0] != 0x30:
                continue
            parts = _tlv_children(vb[1])
            if len(parts) < 2 or parts[0][0] != 0x06:
                continue
            oid = _decode_oid(parts[0][1])
            if not oid:
                continue
            tag, raw = parts[1][0], bytes(parts[1][1])
            ival, sval = None, None
            if tag in (0x02, 0x41, 0x42, 0x43, 0x46) and 1 <= len(raw) <= 8:
                try:
                    ival = int.from_bytes(raw, "big", signed=(tag == 0x02))
                except Exception:
                    ival = None
            elif tag == 0x04 and len(raw) <= 256:
                try:
                    sval = raw.decode("utf-8", errors="replace").strip()
                except Exception:
                    sval = None
            out.append((oid, tag, ival, sval))
    except Exception:
        pass
    return out


def _oid_under(oid, base):
    base = base.strip().strip(".")
    oid = (oid or "").strip().strip(".")
    return oid == base or oid.startswith(base + ".")


def _snmp_getnext(ip, community, oid, timeout=2.5):
    """Satu GETNEXT. Kembalikan (oid_str, tag, ival, sval) atau (None, None, None, None)."""
    sock = None
    try:
        pkt = _encode_snmp_v1_get(community, [oid], _pdu_tag=0xA1)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(timeout)
        sock.sendto(pkt, (ip, 161))
        resp, _ = sock.recvfrom(8192)
        vbs = _extract_snmp_varbinds(resp)
        if vbs:
            return vbs[0]
        return None, None, None, None
    except Exception as e:
        print(f"[SNMP ERROR] {ip} getnext: {e}")
        return None, None, None, None
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass


def snmp_walk(ip, community, base, max_rows=64):
    """Walk satu kolom tabel. Kembalikan list (oid, tag, ival, sval)."""
    base = _valid_oid(base)
    if not base:
        return []
    out, cur = [], base
    seen = set()
    for _ in range(max(1, min(max_rows, 256))):
        oid, tag, ival, sval = _snmp_getnext(ip, community, cur)
        if not oid or not _oid_under(oid, base) or oid in seen:
            break
        seen.add(oid)
        if tag in (0x80, 0x81, 0x82):  # noSuch / endOfMibView
            break
        out.append((oid, tag, ival, sval))
        cur = oid
    return out


IF_DESCR_BASE = "1.3.6.1.2.1.2.2.1.2"
IF_OPER_BASE = "1.3.6.1.2.1.2.2.1.8"
DISCOVER_MAX_IF = (
    128  # batas interface per discover (walk berhenti sendiri di ujung tabel)
)


def discover_interfaces(ip, community, max_if=DISCOVER_MAX_IF):
    """Walk ifDescr + ifOperStatus. Kembalikan list {if_index, name, oper}."""
    try:
        descrs = snmp_walk(ip, community, IF_DESCR_BASE, max_rows=max_if + 8)
        opers = snmp_walk(ip, community, IF_OPER_BASE, max_rows=max_if + 8)
    except Exception as e:
        print(f"[SNMP] discover {ip} gagal: {e}")
        return []

    def _idx(oid, base):
        try:
            suffix = oid.strip().strip(".")[len(base.strip().strip(".")) + 1 :]
            i = int(suffix)
            return i if i >= 1 else None
        except (ValueError, TypeError, IndexError):
            return None

    names, opermap = {}, {}
    for oid, _tag, _iv, sv in descrs:
        i = _idx(oid, IF_DESCR_BASE)
        if i is None:
            continue
        name = re.sub(r"[^\x20-\x7e]", "", sv or "")[:64] or f"if{i}"
        names[i] = name
    for oid, _tag, iv, _sv in opers:
        i = _idx(oid, IF_OPER_BASE)
        if i is None:
            continue
        opermap[i] = iv
    out = []
    for i in sorted(set(names) | set(opermap))[:max_if]:
        out.append(
            {"if_index": i, "name": names.get(i, f"if{i}"), "oper": opermap.get(i)}
        )
    return out


_OID_RE = re.compile(r"^\.?([0-9]+\.)+[0-9]+$")


def _valid_oid(s):
    s = str(s or "").strip()
    if not s or len(s) > 128 or not _OID_RE.match(s):
        return None
    try:
        if any(int(p) > 2**32 - 1 for p in s.strip(".").split(".")):
            return None
    except ValueError:
        return None
    return s


def _snmp_get(ip, community, oids, timeout=2.0):
    """Satu request SNMP GET untuk banyak OID. Kembalikan list int/None.

    Posisi dipertahankan:(vals[i] untuk oids[i], None bila tak terjawab).
    Heuristik: bila jumlah nilai == jumlah OID, mapping posisional;
    bila kurang (error varbind), semua dianggap tak terjawab parsial -> None.
    """
    oids = [o for o in (oids or [])]
    if not oids:
        return []
    sock = None
    try:
        pkt = _encode_snmp_v1_get(community, oids)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(timeout)
        sock.sendto(pkt, (ip, 161))
        resp, _ = sock.recvfrom(8192)
        vals = _extract_snmp_values(resp)
        # hanya mapping posisional bila jumlah pas (respons error/varbind
        # parsial jumlahnya tak cocok -> anggap tak terjawab semua)
        if len(vals) == len(oids):
            return list(vals)
        return [None] * len(oids)
    except Exception as e:
        print(f"[SNMP ERROR] {ip}: {e}")
        return [None] * len(oids)
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass
