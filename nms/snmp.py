"""nms.snmp — SNMP data-plane: codec BER, GET/NEXT/WALK, discovery interface."""

import ipaddress
import os
import random
import re
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed


def _encode_ber_len(n):
    if n < 0:
        raise ValueError("length negatif")
    if n < 128:
        return bytes([n])
    lb = n.to_bytes((n.bit_length() + 7) // 8, "big")
    if len(lb) > 4:
        raise ValueError("length terlalu besar")
    return bytes([0x80 | len(lb)]) + lb


def _encode_ber_uint(value):
    """BER INTEGER kanonikal untuk bilangan non-negatif (0..2^31-1).

    Panjang minimal; bila bit tertinggi menyala, byte 0x00 ditambah di
    depan supaya tidak terbaca negatif (POSINT per BER).
    """
    v = int(value)
    if v < 0 or v > 2**31 - 1:
        raise ValueError(f"nilai di luar rentang request-id/param: {value!r}")
    raw = v.to_bytes(max(1, (v.bit_length() + 7) // 8), "big")
    if raw[0] & 0x80:
        raw = b"\x00" + raw
    return b"\x02" + _encode_ber_len(len(raw)) + raw


def _random_request_id():
    """request-id acak 31-bit (0 .. 2^31-1)."""
    return random.getrandbits(31)


def _encode_snmp_v1_get(
    community,
    oid_list,
    _pdu_tag=0xA0,
    request_id=None,
    non_repeaters=0,
    max_repetitions=None,
):
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
        return b"\x06" + _encode_ber_len(len(encoded)) + encoded

    req_id = _encode_ber_uint(
        _random_request_id() if request_id is None else request_id
    )
    if _pdu_tag == 0xA5:
        # GETBULK: dua field setelah request-id adalah non-repeaters dan
        # max-repetitions (bukan error-status/error-index).
        if max_repetitions is None:
            raise ValueError("GETBULK wajib max_repetitions")
        rest = _encode_ber_uint(non_repeaters) + _encode_ber_uint(max_repetitions)
    else:
        rest = b"\x02\x01\x00\x02\x01\x00"
    varbinds = b""
    for oid in oid_list:
        oid_enc = encode_oid(oid)
        varbinds += (
            b"\x30"
            + _encode_ber_len(len(oid_enc + b"\x05\x00"))
            + oid_enc
            + b"\x05\x00"
        )
    varbind_list = b"\x30" + _encode_ber_len(len(varbinds)) + varbinds
    pdu = (
        bytes([_pdu_tag])
        + _encode_ber_len(len(req_id + rest + varbind_list))
        + req_id
        + rest
        + varbind_list
    )
    comm_bytes = str(community or "")[:128].encode()

    # SNMP Auto-Discovery + monitoring berjalan di SNMPv2c.
    _ver = 1
    version = bytes([0x02, 0x01, _ver])
    comm_tlv = bytes([0x04]) + _encode_ber_len(len(comm_bytes)) + comm_bytes
    return (
        b"\x30"
        + _encode_ber_len(len(version + comm_tlv + pdu))
        + version
        + comm_tlv
        + pdu
    )


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


_SNMP_VALUE_TAGS = (0x02, 0x04, 0x41, 0x42, 0x43, 0x46)


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
            if last_was_oid and tag in _SNMP_VALUE_TAGS:
                if tag == 0x04:
                    try:
                        found.append(val.decode("utf-8"))
                    except UnicodeDecodeError:
                        found.append(val.decode("latin-1"))
                elif 1 <= len(val) <= 9:
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


def _parse_pdu_header(resp):
    """Header PDU respons: (request_id, error_status, error_index).

    Tiga field INTEGER pertama pada PDU (pdu[0..2]); kembalikan None
    bila struktur tidak dapat diurai. Untuk GETBULK klien (0xA5), field
    [1] dan [2] pada PAKET REQUEST berisi non-repeaters/max-repetitions.
    """
    try:
        top = _tlv_children(bytes(resp))
        if len(top) != 1 or top[0][0] != 0x30:
            return None
        msg = _tlv_children(top[0][1])
        if len(msg) < 3:
            return None
        pdu = _tlv_children(msg[2][1])
        if len(pdu) < 3:
            return None
        vals = []
        for tlv in pdu[:3]:
            if tlv[0] != 0x02:
                return None
            vals.append(int.from_bytes(tlv[1], "big", signed=True))
        return vals[0], vals[1], vals[2]
    except Exception:
        return None


class _SnmpLink:
    """Satu socket UDP untuk satu walk: kirim + terima dengan cocok-id.

    Datagram dengan request-id berbeda / tak dapat diurai diabaikan
    TANPA mengulang timeout penuh — sisa deadline dihitung dari waktu
    tunggu awal. Alamat pengirim TIDAK divalidasi (agen multi-IP boleh
    menjawab dari alamat lain). Recv buffer 65535 ( respons GETBULK
    bisa membesar).
    """

    def __init__(self, ip, timeout=2.5, recv_buffer=65535):
        self.ip = str(ip)
        self.timeout = max(0.05, float(timeout))
        self._buffer = int(recv_buffer)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(self.timeout)

    def receive(self, request_id):
        """Terima datagram ber-request-id cocok, atau None saat deadline habis."""
        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                self.sock.settimeout(remaining)
                resp, _addr = self.sock.recvfrom(self._buffer)
            except socket.timeout:
                return None
            except OSError:
                return None
            header = _parse_pdu_header(resp)
            if header is None:
                continue  # paket rusak — tunggu datagram lain sampai deadline
            if header[0] != request_id:
                continue  # respons bawaan/sisa request lain — abaikan
            return bytes(resp)

    def request(self, pkt, request_id=None):
        """Kirim satu request; kembalikan (resp, request_id)."""
        rid = _random_request_id() if request_id is None else int(request_id)
        self.sock.sendto(bytes(pkt), (self.ip, 161))
        return self.receive(rid), rid

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()
        return False


def _snmp_wire_request(ip, pkt, request_id, timeout):
    """Satu request pada socket sendiri; kembalikan bytes atau None."""
    with _SnmpLink(ip, timeout) as link:
        resp, _rid = link.request(pkt, request_id)
        return resp


def _oid_under(oid, base):
    base = base.strip().strip(".")
    oid = (oid or "").strip().strip(".")
    return oid == base or oid.startswith(base + ".")


def _oid_sort_key(oid):
    """Kunci pembanding kenaikan numerik OID: (int, int, ...)."""
    try:
        return tuple(int(p) for p in str(oid).strip().strip(".").split("."))
    except ValueError:
        return ()


def _snmp_getnext(ip, community, oid, timeout=2.5):
    """Satu GETNEXT. Kembalikan (oid_str, tag, ival, sval) atau (None, None, None, None).

    Request-id acak per request; datagram ber-request-id salah diabaikan
    sampai deadline habis (tanpa mengulang timeout penuh).
    """
    link = None
    try:
        rid = _random_request_id()
        pkt = _encode_snmp_v1_get(community, [oid], _pdu_tag=0xA1, request_id=rid)
        resp = _snmp_wire_request(ip, pkt, rid, timeout)
        if resp is None:
            print(f"[SNMP ERROR] {ip} getnext: timeout")
            return None, None, None, None
        vbs = _extract_snmp_varbinds(resp)
        if vbs:
            return vbs[0]
        return None, None, None, None
    except Exception as e:
        print(f"[SNMP ERROR] {ip} getnext: {e}")
        return None, None, None, None


_SNMP_BULK_TIMEOUT_S = 2.5  # timeout per request GETBULK
_BULK_REPS_MAX = 20  # max-repetitions awal
_BULK_UNSUPPORTED_TTL_S = 3600


_BULK_UNSUPPORTED = {}  # ip -> monotonic ts saat diketahui tanpa GETBULK
_BULK_LOCK = threading.Lock()


def _bulk_supported(ip, now_ts):
    with _BULK_LOCK:
        ts = _BULK_UNSUPPORTED.get(ip)
    return ts is None or (now_ts - ts) >= _BULK_UNSUPPORTED_TTL_S


def _mark_bulk_unsupported(ip):
    with _BULK_LOCK:
        _BULK_UNSUPPORTED[ip] = time.monotonic()


def _snmp_getbulk(ip, community, oid, max_repetitions, timeout=None):
    """Satu GETBULK (non_repeaters=0). Kembalikan (varbinds, error).

    varbinds: list (oid_str, tag, ival, sval) berurutan. error: None bila
    sukses; "tooBig" bila error_status=1 (pemanggil mengecilkan batch),
    "timeout" bila tidak ada respons ber-request-id cocok, atau teks
    singkat lain (agen rusak/status anomali) -> jatuh ke jalur GETNEXT.
    """
    rep = max(1, int(max_repetitions))
    try:
        rid = _random_request_id()
        pkt = _encode_snmp_v1_get(
            community,
            [oid],
            _pdu_tag=0xA5,
            request_id=rid,
            non_repeaters=0,
            max_repetitions=rep,
        )
        resp = _snmp_wire_request(
            ip, pkt, rid, timeout if timeout is not None else _SNMP_BULK_TIMEOUT_S
        )
        if resp is None:
            return [], "timeout"
        header = _parse_pdu_header(resp)
        if header is None:
            return [], "malformed"
        _rid, error_status, _eidx = header
        if error_status == 1:
            return [], "tooBig"
        if error_status != 0:
            return [], f"error_status={error_status}"
        return _extract_snmp_varbinds(resp), None
    except Exception as e:
        print(f"[SNMP ERROR] {ip} bulk: {e}")
        return [], str(e)


def _walk_bulk(ip, community, base, limit):
    """Walk via GETBULK. Kembalikan list baris, atau None bila butuh fallback."""
    out = []
    seen = set()
    cur = base
    reps = _BULK_REPS_MAX
    prev_key = _oid_sort_key(base)
    with _SnmpLink(ip, _SNMP_BULK_TIMEOUT_S) as link:
        while True:
            if len(out) >= limit:
                return out
            remaining = limit - len(out)
            batch = max(1, min(reps, remaining))
            rid = _random_request_id()
            pkt = _encode_snmp_v1_get(
                community,
                [cur],
                _pdu_tag=0xA5,
                request_id=rid,
                non_repeaters=0,
                max_repetitions=batch,
            )
            try:
                resp = link.request(pkt, rid)[0]
            except OSError as e:
                print(f"[SNMP ERROR] {ip} bulk: {e}")
                return None
            if resp is None:
                return None  # tanpa respons -> fallback GETNEXT
            header = _parse_pdu_header(resp)
            if header is None:
                return None  # respons rusak -> fallback
            _rid, error_status, _eidx = header
            if error_status == 1:
                # tooBig: kecilkan max-repetitions jadi setengah (min 1), ulangi.
                if batch <= 1:
                    return None
                reps = max(1, batch // 2)
                continue
            if error_status != 0:
                return None  # status anomali -> fallback jalur lama
            vbs = _extract_snmp_varbinds(resp)
            if not vbs:
                return None  # tidak ada varbind -> fallback
            for oid, tag, ival, sval in vbs:
                if len(out) >= limit:
                    return out
                if not _oid_under(oid, base):
                    return out  # keluar dari kolom yang dipetakan -> potong
                if tag in (0x80, 0x81, 0x82):
                    return out  # noSuchObject/Instance/endOfMibView
                key = _oid_sort_key(oid)
                if not key or oid in seen or (prev_key is not None and key <= prev_key):
                    return out  # agen rusak (OID tidak naik) -> hentikan
                seen.add(oid)
                out.append((oid, tag, ival, sval))
                prev_key = key
                cur = oid
        # (while True) — keluar hanya lewat return di atas


def snmp_walk(ip, community, base, max_rows=64, use_bulk=True):
    """Walk satu kolom tabel. Kembalikan list (oid, tag, ival, sval).

    Bawaan lewat GETBULK (satu socket per walk, max-repetitions awal 20
    dibatasi sisa max_rows). Bila bulk tidak dijawab / gagal bermakna,
    otomatis jatuh ke jalur GETNEXT lama; host yang terbukti tanpa
    GETBULK dicatat di _BULK_UNSUPPORTED dan dilewati selama 1 jam.
    """
    base = _valid_oid(base)
    if not base:
        return []
    limit = max(1, min(int(max_rows or 64), 256))
    ip = str(ip)
    now_ts = time.monotonic()
    if use_bulk and _bulk_supported(ip, now_ts):
        out = _walk_bulk(ip, community, base, limit)
        if out is not None:
            return out
        out = _walk_getnext(ip, community, base, limit)
        if out:
            # Bulk gagal namun jalur GETNEXT hidup -> tandai host 1 jam.
            _mark_bulk_unsupported(ip)
        return out
    return _walk_getnext(ip, community, base, limit)


def _walk_getnext(ip, community, base, limit):
    """Jalur GETNEXT klasik (satu request per baris)."""
    out, cur = [], base
    seen = set()
    for _ in range(limit):
        oid, tag, ival, sval = _snmp_getnext(ip, community, cur)
        if not oid or not _oid_under(oid, base) or oid in seen:
            break
        seen.add(oid)
        if tag in (0x80, 0x81, 0x82):
            break
        out.append((oid, tag, ival, sval))
        cur = oid
    return out


IF_DESCR_BASE = "1.3.6.1.2.1.2.2.1.2"
IF_OPER_BASE = "1.3.6.1.2.1.2.2.1.8"
DISCOVER_MAX_IF = 128


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


SYS_DESCR_OID = "1.3.6.1.2.1.1.1.0"
SYS_NAME_OID = "1.3.6.1.2.1.1.5.0"


def detect_vendor(sys_descr, sys_name):
    text = f"{sys_descr or ''} {sys_name or ''}".lower()
    signatures = (
        ("MikroTik", ("mikrotik", "routeros")),
        ("Cisco", ("cisco",)),
        ("Huawei", ("huawei",)),
        ("ZTE", ("zte",)),
        ("FiberHome", ("fiberhome",)),
        ("Ubiquiti", ("ubiquiti", "edgeos")),
    )
    for vendor, needles in signatures:
        if any(needle in text for needle in needles):
            return vendor
    return "Generic"


def discover_subnet(
    network, community, timeout=0.8, max_hosts=256, workers=32, return_diagnostics=False
):
    net = ipaddress.ip_network(str(network).strip(), strict=False)
    hosts = list(net.hosts())
    if len(hosts) > max_hosts:
        raise ValueError(f"Subnet terlalu besar; maksimum {max_hosts} host")
    community = str(community or "").strip()
    if not community or len(community) > 128:
        raise ValueError("community wajib diisi dan maksimal 128 karakter")
    timeout = max(0.2, min(float(timeout), 3.0))

    diagnostics = {
        "hosts": len(hosts),
        "responded": 0,
        "timeout": 0,
        "no_response": 0,
        "partial": 0,
        "community_or_error": 0,
    }

    def probe(addr):
        vals, error = _snmp_get(
            str(addr),
            community,
            [SYS_DESCR_OID, SYS_NAME_OID],
            timeout=timeout,
            return_status=True,
        )
        if error:
            if "time" in str(error).lower() or "timeout" in str(error).lower():
                diagnostics["timeout"] += 1
            elif "partial" in str(error).lower():
                diagnostics["partial"] += 1
            else:
                diagnostics["community_or_error"] += 1
            return None
        if vals and any(v is not None for v in vals):
            diagnostics["responded"] += 1
            return {
                "ip": str(addr),
                "sys_descr": vals[0],
                "sys_name": vals[1],
                "vendor": detect_vendor(vals[0], vals[1]),
                "reachable": True,
            }
        diagnostics["no_response"] += 1
        return None

    results = []
    with ThreadPoolExecutor(max_workers=max(1, min(int(workers), 64))) as pool:
        futures = [pool.submit(probe, addr) for addr in hosts]
        for future in as_completed(futures):
            item = future.result()
            if item:
                results.append(item)
    results = sorted(results, key=lambda x: ipaddress.ip_address(x["ip"]))
    if return_diagnostics:
        return results, diagnostics
    return results


def _snmp_get(ip, community, oids, timeout=2.0, *, return_status=False):
    """Satu request SNMP GET untuk banyak OID. Kembalikan list int/None.

    Posisi dipertahankan:(vals[i] untuk oids[i], None bila tak terjawab).
    Heuristik: bila jumlah nilai == jumlah OID, mapping posisional;
    bila kurang (error varbind), semua dianggap tak terjawab parsial -> None.
    Request-id acak per request; datagram ber-request-id salah diabaikan
    sampai deadline habis.
    """
    oids = [o for o in (oids or [])]
    if not oids:
        return []
    try:
        rid = _random_request_id()
        pkt = _encode_snmp_v1_get(community, oids, request_id=rid)
        resp = _snmp_wire_request(ip, pkt, rid, timeout)
        if resp is None:
            print(f"[SNMP ERROR] {ip}: timeout")
            if return_status:
                return [None] * len(oids), "timeout"
            return [None] * len(oids)
        vals = _extract_snmp_values(resp)
        if len(vals) == len(oids):
            if return_status:
                return list(vals), None
            return list(vals)
        if return_status:
            return [None] * len(oids), "partial_response"
        return [None] * len(oids)
    except Exception as e:
        print(f"[SNMP ERROR] {ip}: {e}")
        if return_status:
            return [None] * len(oids), str(e)
        return [None] * len(oids)
