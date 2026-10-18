"""Uji GETBULK snmp_walk + fallback GETNEXT + codec request-id (fake socket).

Tidak menyentuh jaringan: `socket.socket` di-patch dengan fake yang
menjawab dari mini-MIB in-process, membangun respons dari request-id
paket yang diterima.
"""

import socket
import time  # noqa: F401 (dipakai pada kasus _BULK_UNSUPPORTED kedaluwarsa)
import unittest
from unittest import mock

from nms import snmp as S

AGENT_IP = "10.66.66.66"
BASE = "1.3.6.1.2.1.2.2.1.2"  # ifDescr
OPER_BASE = "1.3.6.1.2.1.2.2.1.8"  # ifOperStatus


# ---- helper encode sisi test -------------------------------------------------


def _tlv(tag, val):
    ln = len(val)
    if ln < 128:
        return bytes([tag, ln]) + val
    lb = ln.to_bytes((ln.bit_length() + 7) // 8, "big")
    return bytes([tag, 0x80 | len(lb)]) + lb + val


def _enc_oid(oid):
    parts = [int(x) for x in str(oid).split(".") if x != ""]
    out = bytes([parts[0] * 40 + parts[1]])
    for p in parts[2:]:
        if p == 0:
            out += b"\x00"
            continue
        segs, q = [], p
        while q:
            segs.append(q & 0x7F)
            q >>= 7
        segs.reverse()
        for i, s in enumerate(segs):
            out += bytes([s | (0x80 if i < len(segs) - 1 else 0)])
    return out


def _resp(pdutag, reqid, vbs, error_status=0, error_index=0, community=b"pub"):
    """Paket respons SNMP v2c. vbs: list (oid_str, tag, value_bytes)."""
    body = b"".join(
        _tlv(0x30, _tlv(0x06, _enc_oid(oid)) + _tlv(tag, val)) for oid, tag, val in vbs
    )
    pdu = _tlv(
        pdutag,
        S._encode_ber_uint(reqid)
        + S._encode_ber_uint(error_status)
        + S._encode_ber_uint(error_index)
        + _tlv(0x30, body),
    )
    return _tlv(0x30, b"\x02\x01\x01" + _tlv(0x04, community) + pdu)


def _parse_request(pkt):
    """Ambil (pdu_tag, reqid, field1, field2, oid_pertama) dari paket request."""
    top = S._tlv_children(pkt)
    msg = S._tlv_children(top[0][1])
    pdu = S._tlv_children(msg[2][1])
    vbs = S._tlv_children(pdu[3][1])
    oid_parts = S._tlv_children(vbs[0][1])
    oid = S._decode_oid(oid_parts[0][1])
    ints = tuple(int.from_bytes(t[1], "big", signed=True) for t in pdu[:3])
    return msg[2][0], ints[0], ints[1], ints[2], oid


# ---- fake agent + fake socket ------------------------------------------------


class _Agent:
    """Mini-MIB: satu tabel terurut dari baris (oid, tag, value_bytes).

    GETBULK (0xA5) mengembalikan max-repetitions baris setelah OID
    request; GETNEXT (0xA1) satu baris; habis dijawab endOfMibView.
    """

    def __init__(self, rows):
        self.rows = sorted(rows, key=lambda r: S._oid_sort_key(r[0]))
        self.pending = []
        self.bulk_reqs = []  # (cur_oid, max_rep)
        self.next_reqs = []  # (cur_oid,)
        self.answer_bulk = True  # False: GETBULK didiamkan (tanpa datagram)
        self.too_big_above = (
            None  # max_rep > nilai ini dijawab tooBig (None: tidak pernah)
        )
        self.bad_rid_once = False
        self._bad_done = False

    # -- penjawab ---------------- -
    def _rows_after(self, cur, count):
        cur_key = S._oid_sort_key(cur)
        out = [r for r in self.rows if S._oid_sort_key(r[0]) > cur_key]
        return out[:count]

    def handle(self, pkt):
        pdu_tag, reqid, f1, f2, oid = _parse_request(pkt)
        if pdu_tag == 0xA5:
            self.bulk_reqs.append((oid, f2))
            if not self.answer_bulk:
                return []  # diam sampai timeout penuh
            if self.too_big_above is not None and f2 > self.too_big_above:
                # tooBig: error_status=1, varbind list kosong
                return [_resp(0xA2, reqid, [], error_status=1, error_index=0)]
            rows = self._rows_after(oid, f2)
            if not rows:
                return [
                    _resp(
                        0xA2,
                        self._rid_for(reqid),
                        [(oid, 0x82, b"")],
                    )
                ]
            vbs = [(r[0], r[1], r[2]) for r in rows]
            return [_resp(0xA2, self._rid_for(reqid), vbs)]
        if pdu_tag == 0xA1:
            self.next_reqs.append((oid,))
            rows = self._rows_after(oid, 1)
            if not rows:
                return [
                    _resp(
                        0xA2,
                        self._rid_for(reqid),
                        [(oid, 0x82, b"")],
                    )
                ]
            r = rows[0]
            return [_resp(0xA2, self._rid_for(reqid), [(r[0], r[1], r[2])])]
        return []

    def _rid_for(self, reqid):
        """Kadang balas dengan request-id SALAH dulu lalu yang benar."""
        if self.bad_rid_once and not self._bad_done:
            self._bad_done = True
            # antrikan respons rid salah ditangani pemanggil (di bawah)
            self._spoof_wrong = reqid - 1
        return reqid


class _FakeSocket:
    def __init__(self, agents, *args, **kwargs):
        self.agents = agents
        self.closed = False

    def settimeout(self, t):
        pass

    def sendto(self, data, addr):
        for agent in self.agents:
            for datagram in agent.handle(data):
                agent.pending.append(datagram)

    def recvfrom(self, bufsize):
        for agent in self.agents:
            if getattr(agent, "_spoof_wrong", None) is not None:
                rid_salah = agent._spoof_wrong
                agent._spoof_wrong = None
                # Balas rid salah (diabaikan), lalu rid asli berikutnya.
                datagram = _resp(0xA2, rid_salah, [(BASE + ".1", 0x04, b"x")])
                return datagram, ("9.9.9.9", 161)
            if agent.pending:
                return agent.pending.pop(0), ("9.9.9.9", 161)
        raise socket.timeout("no response (fake)")

    def close(self):
        self.closed = True


def _patch_socket(agent):
    return mock.patch(
        "nms.snmp.socket.socket",
        lambda *args, **kwargs: _FakeSocket([agent], *args, **kwargs),
    )


class CodecRequestIDTest(unittest.TestCase):
    def test_getbulk_encoder(self):
        pkt = S._encode_snmp_v1_get(
            "pub",
            [BASE],
            _pdu_tag=0xA5,
            request_id=123456,
            non_repeaters=0,
            max_repetitions=20,
        )
        pdu_tag, reqid, f1, f2, _oid = _parse_request(pkt)
        self.assertEqual(pdu_tag, 0xA5)
        self.assertEqual(reqid, 123456)
        self.assertEqual(f1, 0)  # non-repeaters
        self.assertEqual(f2, 20)  # max-repetitions

    def test_tag_a0_a1_tidak_berubah(self):
        for tag in (0xA0, 0xA1):
            pkt = S._encode_snmp_v1_get("pub", ["1.3.6.1.2.1.1.5.0"], _pdu_tag=tag)
            pdu_tag, _rid, f1, f2, _ = _parse_request(pkt)
            self.assertEqual(pdu_tag, tag)
            self.assertEqual(f1, 0)  # error_status (v1)
            self.assertEqual(f2, 0)  # error_index (v1)

    def test_request_id_acak_31bit(self):
        ids = set()
        for _ in range(30):
            pkt = S._encode_snmp_v1_get("pub", [BASE])
            ids.add(S._parse_pdu_header(pkt)[0])
        self.assertTrue(len(ids) > 5)  # tidak lagi selalu 1
        for rid in ids:
            self.assertGreaterEqual(rid, 0)
            self.assertLessEqual(rid, 2**31 - 1)

    def test_request_id_kanonikal(self):
        # 128 = 0x80 -> Harus ditambahkan 0x00 di depan (tidak negatif)
        pkt = S._encode_snmp_v1_get("pub", [BASE], request_id=128)
        top = S._tlv_children(pkt)
        msg = S._tlv_children(top[0][1])
        pdu = S._tlv_children(msg[2][1])
        self.assertEqual(pdu[0][1], b"\x00\x80")
        self.assertEqual(S._parse_pdu_header(pkt)[0], 128)
        # 2^31-1 = four bytes tanpa padding
        pkt = S._encode_snmp_v1_get("pub", [BASE], request_id=2**31 - 1)
        self.assertEqual(S._parse_pdu_header(pkt)[0], 2**31 - 1)
        # rentang valid
        with self.assertRaises(ValueError):
            S._encode_ber_uint(2**31)
        with self.assertRaises(ValueError):
            S._encode_ber_uint(-1)


class _BulkWalkBase(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        S._BULK_UNSUPPORTED.clear()

    def tearDown(self):
        S._BULK_UNSUPPORTED.clear()


class BulkWalkTest(_BulkWalkBase):
    def _walk(self, agent, base=BASE, max_rows=64):
        with _patch_socket(agent):
            return S.snmp_walk(AGENT_IP, "pub", base, max_rows=max_rows)

    def test_bulk_beberapa_batch_sampai_habis(self):
        rows = [(f"{BASE}.{i}", 0x04, f"ether{i}".encode()) for i in range(1, 36)]
        agent = _Agent(rows)
        out = self._walk(agent)
        self.assertEqual([o[0] for o in out], [r[0] for r in rows])
        self.assertTrue(agent.bulk_reqs)  # lewat GETBULK
        self.assertEqual(agent.next_reqs, [])  # tidak jatuh ke GETNEXT
        self.assertNotIn(AGENT_IP, S._BULK_UNSUPPORTED)

    def test_berhenti_endofmibview_semestinya(self):
        rows = [(f"{BASE}.{i}", 0x02, bytes([i])) for i in (1, 2, 3)]
        agent = _Agent(rows)
        out = self._walk(agent)
        self.assertEqual(len(out), 3)
        # Permintaan kedua (setelah row terakhir) menjawab endOfMibView.
        self.assertGreaterEqual(len(agent.bulk_reqs), 2)

    def test_keluar_base_dipotong(self):
        rows = [(f"{BASE}.{i}", 0x04, b"desc") for i in range(1, 4)] + [
            (OPER_BASE + ".9", 0x02, b"\x01")  # di luar base
        ]
        agent = _Agent(rows)
        out = self._walk(agent)
        self.assertEqual(len(out), 3)
        self.assertTrue(all(S._oid_under(o[0], BASE) for o in out))

    def test_menghormati_max_rows(self):
        rows = [(f"{BASE}.{i}", 0x04, b"x") for i in range(1, 31)]
        agent = _Agent(rows)
        out = self._walk(agent, max_rows=7)
        self.assertEqual(len(out), 7)
        # Batch pertama dibatasi sisa baris: min(20, 7)
        self.assertEqual(agent.bulk_reqs[0][1], 7)

    def test_oid_tidak_naik_berhenti(self):
        dup = (f"{BASE}.5", 0x04, b"d")
        agent = _Agent([((f"{BASE}.{i}"), 0x04, b"x") for i in (1, 2)] + [dup, dup])
        out = self._walk(agent)
        # base.1, base.2, base.5 (duplikat KEDUA tidak diappend; walk berhenti)
        self.assertEqual([o[0] for o in out], [f"{BASE}.{i}" for i in (1, 2, 5)])

    def test_request_id_salah_diabaikan(self):
        rows = [(f"{BASE}.{i}", 0x04, b"x") for i in range(1, 4)]
        agent = _Agent(rows)
        agent.answer_bulk = True
        agent.bad_rid_once = True
        out = self._walk(agent)
        self.assertEqual(len(out), 3)  # tetap beres: rid salah dilewati

    def test_toobig_mengecilkan_repetisi(self):
        rows = [(f"{BASE}.{i}", 0x04, b"x") for i in range(1, 13)]
        agent = _Agent(rows)
        agent.too_big_above = 8  # terlalu besar selama > 8
        out = self._walk(agent)
        self.assertEqual(len(out), 12)
        reps = [rep for _cur, rep in agent.bulk_reqs]
        # Bergeser turun dari 20; tidak lebih besar dari setengah agar ok
        self.assertEqual(reps[0], 20)
        self.assertLess(reps[-1], 20)
        self.assertTrue(all(r >= 1 for r in reps))
        # Tidak jatuh ke GETNEXT
        self.assertEqual(agent.next_reqs, [])

    def test_fallback_getnext_dan_tercatat_1jam(self):
        rows = [(f"{BASE}.{i}", 0x04, b"x") for i in range(1, 4)]
        agent = _Agent(rows)
        agent.answer_bulk = False  # GETBULK didiamkan
        with _patch_socket(agent):
            S._SNMP_BULK_TIMEOUT_S, tmp = 0.05, S._SNMP_BULK_TIMEOUT_S
            try:
                out = S.snmp_walk(AGENT_IP, "pub", BASE, max_rows=64)
            finally:
                S._SNMP_BULK_TIMEOUT_S = tmp
        self.assertEqual([o[0] for o in out], [r[0] for r in rows])
        self.assertIn(AGENT_IP, S._BULK_UNSUPPORTED)
        # Walk berikutnya langsung lewat jalur GETNEXT (tanpa GETBULK lagi)
        before = list(agent.bulk_reqs)
        with _patch_socket(agent):
            out2 = S.snmp_walk(AGENT_IP, "pub", BASE, max_rows=64)
        self.assertEqual([o[0] for o in out2], [r[0] for r in rows])
        self.assertEqual(agent.bulk_reqs, before)  # bulk tidak dicoba lagi

    def test_bulk_unsupported_kedaluwarsa(self):
        S._BULK_UNSUPPORTED[AGENT_IP] = time.monotonic() - 7200
        self.assertTrue(S._bulk_supported(AGENT_IP, time.monotonic()))


class DiscoverInterfacesTest(_BulkWalkBase):
    def _rows(self):
        descr = [(f"{BASE}.{i}", 0x04, f"ether{i}".encode()) for i in (1, 2, 3)]
        oper = [
            (f"{OPER_BASE}.{i}", 0x02, bytes([0x02 if i % 2 else 0x01]))
            for i in (1, 2, 3)
        ]
        return descr + oper

    def test_discover_bulk_dan_getnext_punya_hasil_sama(self):
        agent_bulk = _Agent(self._rows())
        with _patch_socket(agent_bulk):
            out_bulk = S.discover_interfaces(AGENT_IP, "pub", max_if=8)
        agent_next = _Agent(self._rows())
        agent_next.answer_bulk = False
        with _patch_socket(agent_next):
            S._SNMP_BULK_TIMEOUT_S, tmp = 0.05, S._SNMP_BULK_TIMEOUT_S
            try:
                out_next = S.discover_interfaces(AGENT_IP, "pub", max_if=8)
            finally:
                S._SNMP_BULK_TIMEOUT_S = tmp
        self.assertEqual(out_bulk, out_next)
        self.assertEqual(
            out_bulk,
            [
                {"if_index": 1, "name": "ether1", "oper": 2},
                {"if_index": 2, "name": "ether2", "oper": 1},
                {"if_index": 3, "name": "ether3", "oper": 2},
            ],
        )


if __name__ == "__main__":
    unittest.main()
