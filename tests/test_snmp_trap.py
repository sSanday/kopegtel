"""Test SNMP trap: decoder/builder BER, receiver, alert dedupe, dan API.

Skenario:
- fungsi murni decode_trap untuk paket v2c/v1 (linkdown/linkup/coldstart/
  enterprise), paket rusak -> None, codec OID round-trip.
- handle_trap_datagram: simpan DB + system_logs + validasi community.
- send_alert: email Telegram channel (mock) via linkdown/coldstart,
  dedupe 60 detik per (ip, kategori), category lain tidak alert.
- receiver socket end-to-end pada port UDP acak + idempoten start.
- API /api/traps (butuh login) + validasi settings snmp_trap_*.
"""

import os
import socket
import tempfile
import time
import unittest
from unittest.mock import patch

_tmp = tempfile.mkdtemp(prefix="nms_test_snmp_trap_")
os.environ.setdefault("NMS_DB_PATH", os.path.join(_tmp, "test.db"))
os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("DASHBOARD_USERNAME", "admin")
os.environ.setdefault("DASHBOARD_PASSWORD", "PantauNms2026")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "")
os.environ.setdefault("TELEGRAM_CHAT_ID", "")
os.environ.setdefault("AGENT_API_KEY", "test-agent-key")
os.environ.setdefault("NMS_DISABLE_SCHEDULER", "1")
os.environ.setdefault("NMS_DISABLE_TRAP_RECEIVER", "1")

import app as m  # noqa: E402

try:
    m.scheduler.shutdown(wait=False)
except Exception:
    pass

from nms import snmp_trap as _tmod  # noqa: E402
from nms.snmp_trap import (  # noqa: E402
    _ber_oid,
    _encode_oid,
    build_v1_trap,
    build_v2c_trap,
    decode_trap,
)

XRW_HDR = {"X-Requested-With": "XMLHttpRequest"}


def _set_trap_setting(key, value):
    conn, c = m.get_db()
    try:
        c.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (key, str(value)),
        )
        m._commit_with_retry(conn)
    finally:
        conn.close()
    if key == "snmp_trap_community":
        _tmod._trap_alert_mem.clear()


def _trap_cleanup():
    conn, c = m.get_db()
    try:
        c.execute("DELETE FROM snmp_traps")
        c.execute("DELETE FROM system_logs WHERE event_type LIKE 'TRAP_%'")
        m._commit_with_retry(conn)
    finally:
        conn.close()
    _tmod._trap_alert_mem.clear()


def _trap_count(where="1=1", params=()):
    conn, c = m.get_db()
    try:
        c.execute(f"SELECT COUNT(*) AS n FROM snmp_traps WHERE {where}", params)
        return c.fetchone()["n"]
    finally:
        conn.close()


class DecodeBuilderTest(unittest.TestCase):
    """Fungsi murni: BER decode + builder + codec OID."""

    def test_decode_v2c_linkdown_lengkap(self):
        pkt = build_v2c_trap(
            "uji-coba",
            trap_oid=".1.3.6.1.6.3.1.1.5.3",
            if_index=3,
            if_oper=2,
            sysuptime=8640000,
            description="tes ifIndex 3",
        )
        d = decode_trap(pkt)
        self.assertIsNotNone(d)
        self.assertEqual(d["version"], "2c")
        self.assertEqual(d["community"], "uji-coba")
        self.assertEqual(d["trap_oid"], ".1.3.6.1.6.3.1.1.5.3")
        self.assertEqual(d["category"], "linkdown")
        self.assertEqual(d["if_index"], 3)
        self.assertEqual(d["if_oper"], 2)
        self.assertEqual(d["sys_uptime"], 8640000)
        oids = [oid for oid, _ in d["varbinds"]]
        self.assertIn(".1.3.6.1.2.1.1.3.0", oids)
        self.assertIn(".1.3.6.1.6.3.1.1.4.1.0", oids)

    def test_decode_v2c_coldstart_tanpa_ifindex(self):
        d = decode_trap(build_v2c_trap("c", trap_oid=".1.3.6.1.6.3.1.1.5.1"))
        self.assertIsNotNone(d)
        self.assertEqual(d["category"], "coldstart")
        self.assertIsNone(d["if_index"])
        self.assertIsNone(d["generic"])  # v2c beda level: generic dari OID saja

    def test_decode_v2c_enterprise_specific(self):
        d = decode_trap(build_v2c_trap("c", trap_oid=".1.3.6.1.4.1.8072.9999.17"))
        self.assertEqual(d["category"], "other")
        self.assertEqual(d["trap_oid"], ".1.3.6.1.4.1.8072.9999.17")

    def test_decode_v1_linkdown(self):
        pkt = build_v1_trap(
            "kopegtel",
            generic=2,
            specific=0,
            agent_ip="192.168.10.55",
            sysuptime=4320000,
            if_index=3,
            if_oper=2,
        )
        d = decode_trap(pkt)
        self.assertIsNotNone(d)
        self.assertEqual(d["version"], "v1")
        self.assertEqual(d["community"], "kopegtel")
        self.assertEqual(d["generic"], 2)
        self.assertEqual(d["specific"], 0)
        self.assertEqual(d["category"], "linkdown")
        self.assertEqual(d["if_index"], 3)
        self.assertEqual(d["sys_uptime"], 4320000)

    def test_decode_v1_warmstart_dan_authfail(self):
        d = decode_trap(build_v1_trap("c", generic=1, specific=0))
        self.assertEqual(d["category"], "warmstart")
        d2 = decode_trap(build_v1_trap("c", generic=4, specific=0))
        self.assertEqual(d2["category"], "authfailure")

    def test_decode_v1_uplink_uplink(self):
        d = decode_trap(build_v1_trap("c", generic=3, if_index=7, if_oper=1))
        self.assertEqual(d["category"], "linkup")
        self.assertEqual(d["if_index"], 7)

    def test_decode_paket_rusak(self):
        self.assertIsNone(decode_trap(b""))
        self.assertIsNone(decode_trap("bukan bytes"))
        self.assertIsNone(decode_trap(b"\x30\x03\x02\x01\x01"))
        self.assertIsNone(
            decode_trap(b"\x30\x0c\x02\x01\x01\x04\x03abc\xa2\x02\x00\x00")
        )
        self.assertIsNone(
            decode_trap(b"\x30\x0c\x02\x01\x01\x04\x03abc" + b"\x99\x02\x00\x00")
        )
        self.assertIsNone(decode_trap(b"\x31\x06\x02\x01\x01" + b"\x00" * 20))

    def test_oid_roundtrip(self):
        for oid in (
            ".1.3.6.1.6.3.1.1.5.3",
            ".1.3.6.1.4.1.8072.9999.17",
            ".2.999.1",  # sub-nilai pertama >127 (base-128 multi-byte)
            ".1.3.6.1.2.1.2.2.1.1.3",
        ):
            self.assertEqual(_ber_oid(_encode_oid(oid)), oid)
        # OID tidak valid: kurang dari 2 arc / arc kedua tak sah.
        self.assertEqual(_encode_oid(".1"), b"")
        self.assertEqual(_encode_oid(".1.99"), b"")
        self.assertEqual(_encode_oid(".8.1.1"), b"")
        self.assertEqual(_encode_oid(".1.3.abc"), b"")
        self.assertEqual(_ber_oid(b""), "")
        self.assertEqual(_encode_oid(None), b"")


class ReceiverHandleTest(unittest.TestCase):
    """handle_trap_datagram: DB + system_logs + community."""

    def setUp(self):
        _trap_cleanup()
        _set_trap_setting("snmp_trap_community", "")
        _set_trap_setting("snmp_trap_enabled", 1)

    def tearDown(self):
        _trap_cleanup()

    def test_simpan_dan_log_v2c_linkdown(self):
        pkt = build_v2c_trap("publik", if_index=3, if_oper=2, sysuptime=7)
        row = _tmod.handle_trap_datagram(pkt, ("192.0.2.7", 55555))
        self.assertIsNotNone(row)
        self.assertEqual(row["category"], "linkdown")
        self.assertEqual(row["source_ip"], "192.0.2.7")
        self.assertEqual(row["if_index"], 3)
        self.assertEqual(row["community_ok"], 1)
        self.assertEqual(_trap_count("category='linkdown'"), 1)
        # system_logs mendapat TRAP_LINKDOWN agar muncul di halaman Logs.
        conn, c = m.get_db()
        try:
            c.execute(
                "SELECT COUNT(*) AS n FROM system_logs WHERE event_type='TRAP_LINKDOWN'"
            )
            self.assertEqual(c.fetchone()["n"], 1)
        finally:
            conn.close()

    def test_community_salah_dicatat_tanpa_alert(self):
        _set_trap_setting("snmp_trap_community", "rahasia-123")
        pkt = build_v2c_trap("publik", if_index=9, if_oper=2)
        with patch("nms.notify.send_alert") as mk:
            row = _tmod.handle_trap_datagram(pkt, ("192.0.2.9", 4444))
            mk.assert_not_called()  # community tidak cocok -> senyap
        self.assertEqual(row["community_ok"], 0)
        # Trap tetap tersimpan (audit), hanya tidak di-alert.
        self.assertEqual(_trap_count("community_ok=0"), 1)

    def test_community_kosong_menerima_semua(self):
        pkt = build_v1_trap("apa-saja", generic=2, if_index=2)
        row = _tmod.handle_trap_datagram(pkt, ("192.0.2.2", 3333))
        self.assertIsNotNone(row)
        self.assertEqual(row["community_ok"], 1)
        self.assertEqual(row["snmp_version"], "v1")
        self.assertEqual(row["category"], "linkdown")


class TrapAlertTest(unittest.TestCase):
    """send_alert + dedupe per (ip, kategori)."""

    def setUp(self):
        _trap_cleanup()
        _set_trap_setting("snmp_trap_community", "")

    def tearDown(self):
        _trap_cleanup()

    def test_linkdown_terkirim_dan_dedupe(self):
        pkt = build_v2c_trap("publik", if_index=5, if_oper=2)
        with patch("nms.notify.send_alert") as mk:
            _tmod.handle_trap_datagram(pkt, ("198.51.100.5", 1111))
            self.assertEqual(mk.call_count, 1)
            args, kwargs = mk.call_args
            self.assertIn("LINKDOWN", args[0])
            self.assertIn("198.51.100.5", args[0])
            self.assertEqual(kwargs.get("severity"), "warning")
            self.assertEqual(kwargs.get("event"), "snmp_trap")
            # Dedupe: trap yang sama walau baru -> tidak alert lagi.
            _tmod.handle_trap_datagram(pkt, ("198.51.100.5", 1112))
            _tmod.handle_trap_datagram(pkt, ("198.51.100.5", 1113))
            self.assertEqual(mk.call_count, 1)
        # IP berbeda -> alert tetap jalan (dedupe per sumber).
        with patch("nms.notify.send_alert") as mk2:
            _tmod.handle_trap_datagram(pkt, ("198.51.100.6", 1111))
            self.assertEqual(mk2.call_count, 1)

    def test_dedupe_expired(self):
        pkt = build_v2c_trap("publik", if_index=5, if_oper=2)
        key = ("198.0.113.1", "linkdown")
        timp = round(time.time() - (_tmod.TRAP_ALERT_DEDUPE_S + 5))
        _tmod._trap_alert_mem.set(key, timp)
        with patch("nms.notify.send_alert") as mk:
            _tmod.handle_trap_datagram(pkt, ("198.0.113.1", 2222))
            self.assertEqual(mk.call_count, 1)

    def test_coldstart_info_dan_linkup_senyap(self):
        cold = build_v2c_trap("publik", trap_oid=".1.3.6.1.6.3.1.1.5.1")
        up = build_v2c_trap("publik", trap_oid=".1.3.6.1.6.3.1.1.5.4")
        with patch("nms.notify.send_alert") as mk:
            _tmod.handle_trap_datagram(cold, ("203.0.113.1", 1234))
            self.assertEqual(mk.call_count, 1)
            _args, kwargs = mk.call_args
            self.assertEqual(kwargs.get("severity"), "info")
            # linkup tidak termasuk kategori alert.
            _tmod.handle_trap_datagram(up, ("203.0.113.1", 1235))
            self.assertEqual(mk.call_count, 1)


class TrapSocketTest(unittest.TestCase):
    """End-to-end via UDP socket asli (port acak) + idempoten."""

    def setUp(self):
        _trap_cleanup()
        _set_trap_setting("snmp_trap_community", "")
        _set_trap_setting("snmp_trap_enabled", 1)

    def tearDown(self):
        _tmod.stop_trap_receiver()
        _trap_cleanup()

    @staticmethod
    def _free_udp_port():
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        return port

    def test_receiver_menerima_paket_udp(self):
        port = self._free_udp_port()
        # Buka env disable (diset conftest) hanya untuk durasi test ini.
        with patch.dict(os.environ, {"NMS_DISABLE_TRAP_RECEIVER": ""}):
            self.assertTrue(_tmod.start_trap_receiver(port=port))
            # Idempoten: pemanggilan kedua saat hidup tidak buka socket baru.
            self.assertTrue(_tmod.start_trap_receiver(port=port))
            pkt = build_v2c_trap("uji-socket", if_index=11, if_oper=2, sysuptime=99)
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.sendto(pkt, ("127.0.0.1", port))
            finally:
                s.close()
            deadline = time.time() + 5.0
            n = 0
            while time.time() < deadline:
                n = _trap_count("if_index=11")
                if n:
                    break
                time.sleep(0.15)
        self.assertEqual(n, 1, "trap UDP tidak masuk dalam 5 detik")

    def test_receiver_mati_bila_disabled(self):
        _set_trap_setting("snmp_trap_enabled", 0)
        with patch.dict(os.environ, {"NMS_DISABLE_TRAP_RECEIVER": ""}):
            self.assertFalse(_tmod.start_trap_receiver(port=self._free_udp_port()))


class TrapApiTest(unittest.TestCase):
    """API /api/traps + validasi settings snmp_trap_*."""

    @classmethod
    def setUpClass(cls):
        _trap_cleanup()
        _set_trap_setting("snmp_trap_community", "")
        cls.client = m.app.test_client()
        r = cls.client.post(
            "/login", data={"username": "admin", "password": "PantauNms2026"}
        )
        assert r.status_code == 302, f"login gagal, status={r.status_code}"

    @classmethod
    def tearDownClass(cls):
        _trap_cleanup()

    def test_api_traps_unauth(self):
        anon = m.app.test_client()
        r = anon.get("/api/traps", headers=XRW_HDR)
        self.assertEqual(r.status_code, 401)

    def test_api_traps_data_dan_filter(self):
        pkt1 = build_v2c_trap("publik", if_index=3, if_oper=2)
        # v1 generic=1 -> warmstart (coldstart = 0); tidak termasuk kategori alert.
        pkt2 = build_v1_trap("publik", generic=1)
        _tmod.handle_trap_datagram(pkt1, ("198.51.100.10", 1299))
        _tmod.handle_trap_datagram(pkt2, ("198.51.100.11", 1300))
        r = self.client.get("/api/traps?limit=10", headers=XRW_HDR)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        j = r.get_json()
        self.assertGreaterEqual(j["total"], 2)
        cats = [t["category"] for t in j["traps"]]
        self.assertIn("linkdown", cats)
        self.assertIn("warmstart", cats)
        versions = {t["snmp_version"] for t in j["traps"]}
        self.assertIn("v1", versions)
        self.assertIn("2c", versions)
        first = j["traps"][0]
        self.assertIn("source_ip", first)
        self.assertIn("received_at", first)
        # filter kategori
        r2 = self.client.get("/api/traps?category=linkdown", headers=XRW_HDR)
        j2 = r2.get_json()
        self.assertTrue(all(t["category"] == "linkdown" for t in j2["traps"]))
        # filter sumber
        r3 = self.client.get("/api/traps?source=198.51.100.10", headers=XRW_HDR)
        j3 = r3.get_json()
        self.assertGreaterEqual(j3["total"], 1)
        self.assertTrue(all(t["source_ip"] == "198.51.100.10" for t in j3["traps"]))

    def test_settings_validasi(self):
        # Port di luar 1024-65535 ditolak.
        r = self.client.post(
            "/api/settings",
            json={"snmp_trap_port": 70000},
            headers=XRW_HDR,
        )
        self.assertEqual(r.status_code, 400)
        r = self.client.post(
            "/api/settings",
            json={"snmp_trap_port": 80},
            headers=XRW_HDR,
        )
        self.assertEqual(r.status_code, 400)
        # Port valid tersimpan + dibaca kembali.
        r = self.client.post(
            "/api/settings",
            json={"snmp_trap_port": 1162, "snmp_trap_enabled": 1},
            headers=XRW_HDR,
        )
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        g = self.client.get("/api/settings", headers=XRW_HDR).get_json()
        self.assertEqual(g.get("snmp_trap_port"), 1162)
        self.assertEqual(g.get("snmp_trap_enabled"), 1)
        # Community dipotong 128 karakter.
        long_comm = "x" * 150
        self.client.post(
            "/api/settings",
            json={"snmp_trap_community": long_comm},
            headers=XRW_HDR,
        )
        g2 = self.client.get("/api/settings", headers=XRW_HDR).get_json()
        self.assertEqual(len(g2.get("snmp_trap_community", "")), 128)
        # kembalikan default kosong
        self.client.post(
            "/api/settings",
            json={"snmp_trap_community": ""},
            headers=XRW_HDR,
        )


if __name__ == "__main__":
    unittest.main()
