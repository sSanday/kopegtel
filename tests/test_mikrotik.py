import os
import tempfile
import unittest

# bootstrap agar aman dijalankan standalone (sebelum import app):
# pakai DB sementara, jangan pernah menyentuh network.db produksi.
_tmp = tempfile.mkdtemp(prefix="nms_test_mikrotik_")
os.environ.setdefault("NMS_DB_PATH", os.path.join(_tmp, "test.db"))
os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("DASHBOARD_USERNAME", "admin")
os.environ.setdefault("DASHBOARD_PASSWORD", "admin12345")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "")
os.environ.setdefault("TELEGRAM_CHAT_ID", "")
os.environ.setdefault("AGENT_API_KEY", "test-agent-key")
os.environ.setdefault("NMS_DISABLE_SCHEDULER", "1")

import app as m

try:
    m.scheduler.shutdown(wait=False)
except Exception:
    pass

JSON_HDR = {"Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest"}
XRW_HDR = {"X-Requested-With": "XMLHttpRequest"}

MT_HOST = "10.99.99.21"
MT_HOST2 = "10.99.99.22"


def _cleanup():
    conn, c = m.get_db()
    try:
        for h in (MT_HOST, MT_HOST2):
            c.execute("DELETE FROM device_health WHERE host=?", (h,))
            c.execute("DELETE FROM hosts WHERE ip=?", (h,))
            m.mt_alarm_memory.pop(h, None)
            m.mt_is_mikrotik.pop(h, None)
        conn.commit()
    finally:
        conn.close()


class SnmpUnitTest(unittest.TestCase):
    def test_valid_oid(self):
        self.assertEqual(
            m._valid_oid("1.3.6.1.4.1.14988.1.1.3.11.0"), "1.3.6.1.4.1.14988.1.1.3.11.0"
        )
        self.assertIsNone(m._valid_oid(""))
        self.assertIsNone(m._valid_oid("1.3.6.1; rm -rf"))
        self.assertIsNone(m._valid_oid("abc"))
        self.assertIsNone(m._valid_oid("1.3.6.99999999999.1"))

    def test_bandwidth_utamakan_64bit(self):
        calls = []

        def fake_get(ip, community, oids, timeout=2.0):
            calls.append(oids)
            if "31.1.1.1.6" in oids[0]:
                return [100, 200]
            return [1, 2]

        orig = m._snmp_get
        m._snmp_get = fake_get
        try:
            self.assertEqual(m.get_snmp_bandwidth("1.2.3.4", "pub", 1), (100, 200))
        finally:
            m._snmp_get = orig

    def test_bandwidth_fallback_32bit(self):
        def fake_get(ip, community, oids, timeout=2.0):
            if "31.1.1.1.6" in oids[0]:
                return [None, None]
            return [7, 9]

        orig = m._snmp_get
        m._snmp_get = fake_get
        try:
            self.assertEqual(m.get_snmp_bandwidth("1.2.3.4", "pub", 2), (7, 9))
        finally:
            m._snmp_get = orig

    def test_bandwidth_ifindex_invalid(self):
        self.assertEqual(m.get_snmp_bandwidth("1.2.3.4", "pub", 0), (None, None))
        self.assertEqual(m.get_snmp_bandwidth("1.2.3.4", "pub", "x"), (None, None))

    def test_resolve_oids_override(self):
        row = {
            "cpu_oid": "1.3.6.1.4.1.1.1.0",
            "mem_oid": "",
            "storage_oid": "",
            "temp_oid": "",
        }
        oids = m.resolve_mt_oids(row)
        self.assertEqual(oids["cpu"], "1.3.6.1.4.1.1.1.0")
        self.assertEqual(oids["mem"], m.MT_DEFAULT_OIDS["mem"])


class MikrotikApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = m.app.test_client()
        _cleanup()
        r = cls.client.post(
            "/login", data={"username": "admin", "password": "admin12345"}
        )
        assert r.status_code == 302, f"login gagal, status={r.status_code}"

    @classmethod
    def tearDownClass(cls):
        _cleanup()

    def _add_host(self, ip, **kw):
        body = {"ip": ip, "snmp_community": "public", "snmp_profile": "mikrotik"}
        body.update(kw)
        r = self.client.post("/api/hosts", json=body, headers=JSON_HDR)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

    def test_host_snmp_fields_dan_validasi(self):
        r = self.client.post(
            "/api/hosts",
            json={"ip": MT_HOST, "snmp_profile": "salah"},
            headers=JSON_HDR,
        )
        self.assertEqual(r.status_code, 400)
        r = self.client.post(
            "/api/hosts", json={"ip": MT_HOST, "cpu_oid": "bukan-oid"}, headers=JSON_HDR
        )
        self.assertEqual(r.status_code, 400)
        self._add_host(MT_HOST, cpu_oid="1.3.6.1.4.1.1.9.0")
        try:
            r = self.client.get("/api/hosts", headers=XRW_HDR)
            h = next(o for o in r.get_json() if o["ip"] == MT_HOST)
            self.assertEqual(h["snmp_profile"], "mikrotik")
            self.assertEqual(h["cpu_oid"], "1.3.6.1.4.1.1.9.0")
            self.assertEqual(h["mem_oid"], "")
            # PATCH snmp
            r = self.client.patch(
                f"/api/hosts/{MT_HOST}/snmp",
                json={"snmp_profile": "auto", "if_index": 3},
                headers=JSON_HDR,
            )
            self.assertEqual(r.status_code, 200)
            r = self.client.patch(
                f"/api/hosts/{MT_HOST}/snmp", json={"if_index": 0}, headers=JSON_HDR
            )
            self.assertEqual(r.status_code, 400)
            r = self.client.patch(
                "/api/hosts/10.99.99.250/snmp",
                json={"snmp_profile": "auto"},
                headers=JSON_HDR,
            )
            self.assertEqual(r.status_code, 404)
        finally:
            self.client.delete(f"/api/hosts/{MT_HOST}", headers=XRW_HDR)

    def test_settings_temp_dan_oid(self):
        r = self.client.get("/api/settings", headers=XRW_HDR)
        self.assertIn("temp_threshold", r.get_json())
        self.assertIn("mt_cpu_oid", r.get_json())
        orig = r.get_json()
        try:
            r = self.client.post(
                "/api/settings",
                json={"temp_threshold": 80.0, "temp_crit": 70.0},
                headers=JSON_HDR,
            )
            self.assertEqual(r.status_code, 400)
            r = self.client.post(
                "/api/settings", json={"mt_cpu_oid": "xx"}, headers=JSON_HDR
            )
            self.assertEqual(r.status_code, 400)
            r = self.client.post(
                "/api/settings",
                json={"temp_threshold": 55.0, "temp_crit": 70.0},
                headers=JSON_HDR,
            )
            self.assertEqual(r.status_code, 200)
            self.assertEqual(m.get_setting("temp_threshold", 60.0), 55.0)
        finally:
            self.client.post(
                "/api/settings",
                json={
                    "temp_threshold": orig["temp_threshold"],
                    "temp_crit": orig["temp_crit"],
                },
                headers=JSON_HDR,
            )

    def test_poll_alarm_dan_pulih(self):
        self._add_host(MT_HOST)
        sent = []
        orig_health = m.get_mikrotik_health
        orig_tg = m.send_telegram_alert
        m.get_mikrotik_health = lambda ip, comm, oids: {
            "cpu": 95.0,
            "mem": 50.0,
            "storage": 10.0,
            "temp_raw": 800.0,
        }
        m.send_telegram_alert = lambda msg: sent.append(msg)
        try:
            m.poll_mikrotik_health()
            self.assertTrue(m.mt_is_mikrotik.get(MT_HOST))
            self.assertTrue(m.mt_alarm_memory[MT_HOST].get("cpu"))
            self.assertTrue(m.mt_alarm_memory[MT_HOST].get("temp_crit"))
            self.assertTrue(any("CPU" in s for s in sent))
            self.assertTrue(any("KRITIS" in s for s in sent))
            # poll kedua sama -> tidak spam ulang
            n = len(sent)
            m.poll_mikrotik_health()
            self.assertEqual(len(sent), n)
            # pulih
            m.get_mikrotik_health = lambda ip, comm, oids: {
                "cpu": 10.0,
                "mem": 20.0,
                "storage": 10.0,
                "temp_raw": 400.0,
            }
            m.poll_mikrotik_health()
            self.assertFalse(m.mt_alarm_memory[MT_HOST].get("cpu"))
            self.assertTrue(any("NORMAL" in s for s in sent))
            # triggers memuat kategori mikrotik saat alarm aktif lagi
            m.get_mikrotik_health = lambda ip, comm, oids: {
                "cpu": 95.0,
                "mem": 20.0,
                "storage": 10.0,
                "temp_raw": 400.0,
            }
            m.poll_mikrotik_health()
            r = self.client.get("/api/triggers", headers=XRW_HDR)
            cats = {(a["category"], a["host"]) for a in r.get_json()}
            self.assertTrue(any(c == "mikrotik" and MT_HOST in h for c, h in cats))
            # list + history
            r = self.client.get("/api/mikrotik", headers=XRW_HDR)
            item = next(o for o in r.get_json() if o["host"] == MT_HOST)
            self.assertEqual(item["cpu"], 95.0)
            self.assertAlmostEqual(item["temp_c"], 40.0)
            self.assertEqual(item["status"], "warning")
            r = self.client.get(
                f"/api/mikrotik/{MT_HOST}/history?hours=1&metric=cpu", headers=XRW_HDR
            )
            self.assertGreaterEqual(r.get_json()["count"], 1)
            r = self.client.get(
                f"/api/mikrotik/{MT_HOST}/history?hours=x", headers=XRW_HDR
            )
            self.assertEqual(r.status_code, 400)
        finally:
            m.get_mikrotik_health = orig_health
            m.send_telegram_alert = orig_tg
            self.client.delete(f"/api/hosts/{MT_HOST}", headers=XRW_HDR)
            self.assertNotIn(MT_HOST, m.mt_alarm_memory)

    def test_host_tanpa_respon_dilewati(self):
        self._add_host(MT_HOST2)
        orig_health = m.get_mikrotik_health
        m.get_mikrotik_health = lambda ip, comm, oids: {
            "cpu": None,
            "mem": None,
            "storage": None,
            "temp_raw": None,
        }
        try:
            m.poll_mikrotik_health()
            self.assertNotIn(MT_HOST2, m.mt_is_mikrotik)
            conn, c = m.get_db()
            n = c.execute(
                "SELECT COUNT(*) FROM device_health WHERE host=?", (MT_HOST2,)
            ).fetchone()[0]
            conn.close()
            self.assertEqual(n, 0)
        finally:
            m.get_mikrotik_health = orig_health
            self.client.delete(f"/api/hosts/{MT_HOST2}", headers=XRW_HDR)

    def test_halaman_mikrotik(self):
        r = self.client.get("/mikrotik")
        self.assertEqual(r.status_code, 200)
        self.assertIn("mikrotik", r.get_data(as_text=True).lower())


def _build_snmp_response(varbinds):
    """Bangun paket respons SNMP mentah: [(oid_arcs, tag, value_bytes)]."""

    def tlv(tag, val):
        n = len(val)
        if n < 128:
            return bytes([tag, n]) + val
        lb = n.to_bytes((n.bit_length() + 7) // 8, "big")
        return bytes([tag, 0x80 | len(lb)]) + lb + val

    def enc_oid(arcs):
        out = bytes([arcs[0] * 40 + arcs[1]])
        for p in arcs[2:]:
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

    vbs = b"".join(
        tlv(0x30, tlv(0x06, enc_oid(arcs)) + tlv(tag, val))
        for arcs, tag, val in varbinds
    )
    pdu = tlv(
        0xA2,
        tlv(0x02, b"\x01") + tlv(0x02, b"\x00") + tlv(0x02, b"\x00") + tlv(0x30, vbs),
    )
    return tlv(0x30, tlv(0x02, b"\x00") + tlv(0x04, b"pub") + pdu)


class SnmpWalkTest(unittest.TestCase):
    def test_decode_oid(self):
        self.assertEqual(
            m._decode_oid(bytes([43, 6, 1, 2, 1, 2, 2, 1, 8, 1])),
            "1.3.6.1.2.1.2.2.1.8.1",
        )
        self.assertIsNone(m._decode_oid(b""))
        self.assertIsNone(m._decode_oid(bytes([43, 6, 0x81])))

    def test_extract_varbinds_int_dan_string(self):
        resp = _build_snmp_response(
            [
                ([1, 3, 6, 1, 2, 1, 2, 2, 1, 8, 1], 0x02, b"\x01"),
                ([1, 3, 6, 1, 2, 1, 2, 2, 1, 2, 1], 0x04, b"ether1"),
            ]
        )
        vbs = m._extract_snmp_varbinds(resp)
        self.assertEqual(len(vbs), 2)
        self.assertEqual(vbs[0], ("1.3.6.1.2.1.2.2.1.8.1", 0x02, 1, None))
        self.assertEqual(vbs[1][0], "1.3.6.1.2.1.2.2.1.2.1")
        self.assertEqual(vbs[1][3], "ether1")

    def test_walk_berhenti_di_luar_base_dan_endofmib(self):
        seq = [
            ("1.3.6.1.2.1.2.2.1.2.1", 0x04, None, "ether1"),
            ("1.3.6.1.2.1.2.2.1.2.2", 0x04, None, "ether2"),
            ("1.3.6.1.2.1.2.2.1.3.1", 0x02, 1500, None),
        ]
        it = iter(seq)
        orig = m._snmp_getnext
        m._snmp_getnext = lambda ip, comm, oid, timeout=2.5: next(it)
        try:
            out = m.snmp_walk("1.2.3.4", "pub", "1.3.6.1.2.1.2.2.1.2")
            self.assertEqual(len(out), 2)
        finally:
            m._snmp_getnext = orig

    def test_walk_loop_dan_cap(self):
        orig = m._snmp_getnext
        m._snmp_getnext = lambda ip, comm, oid, timeout=2.5: (
            "1.3.6.1.2.1.2.2.1.2.9",
            0x04,
            None,
            "x",
        )
        try:
            out = m.snmp_walk("1.2.3.4", "pub", "1.3.6.1.2.1.2.2.1.2", max_rows=64)
            self.assertEqual(len(out), 1)  # oid sama berulang -> stop
        finally:
            m._snmp_getnext = orig

    def test_discover_join_descr_oper(self):
        orig = m.snmp_walk

        def fake_walk(ip, comm, base, max_rows=64):
            if base.endswith(".2"):
                return [
                    (base + ".1", 0x04, None, "ether1"),
                    (base + ".2", 0x04, None, "sfp-sfpplus1"),
                ]
            return [(base + ".1", 0x02, 1, None), (base + ".2", 0x02, 2, None)]

        m.snmp_walk = fake_walk
        try:
            out = m.discover_interfaces("1.2.3.4", "pub")
            self.assertEqual(
                out,
                [
                    {"if_index": 1, "name": "ether1", "oper": 1},
                    {"if_index": 2, "name": "sfp-sfpplus1", "oper": 2},
                ],
            )
        finally:
            m.snmp_walk = orig


MT_IF_HOST = "10.99.99.23"
MT_RB_HOST = "10.99.99.24"


def _cleanup2():
    conn, c = m.get_db()
    try:
        for h in (MT_IF_HOST, MT_RB_HOST):
            c.execute("DELETE FROM iface_traffic WHERE host=?", (h,))
            c.execute("DELETE FROM snmp_interfaces WHERE host=?", (h,))
            c.execute("DELETE FROM device_health WHERE host=?", (h,))
            c.execute("DELETE FROM hosts WHERE ip=?", (h,))
            m.mt_alarm_memory.pop(h, None)
            m.mt_is_mikrotik.pop(h, None)
            m.mt_sysup.pop(h, None)
        for k in [k for k in list(m.mt_iface_oper) if k[0] in (MT_IF_HOST, MT_RB_HOST)]:
            m.mt_iface_oper.pop(k, None)
        for k in [k for k in list(m.iface_state) if k[0] in (MT_IF_HOST, MT_RB_HOST)]:
            m.iface_state.pop(k, None)
        for k in [k for k in list(m.mt_iface_tg) if k[0] in (MT_IF_HOST, MT_RB_HOST)]:
            m.mt_iface_tg.pop(k, None)
        for k in [
            k for k in list(m.mt_iface_flaps) if k[0] in (MT_IF_HOST, MT_RB_HOST)
        ]:
            m.mt_iface_flaps.pop(k, None)
        conn.commit()
    finally:
        conn.close()


class MikrotikIfaceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = m.app.test_client()
        _cleanup2()
        r = cls.client.post(
            "/login", data={"username": "admin", "password": "admin12345"}
        )
        assert r.status_code == 302, f"login gagal, status={r.status_code}"

    @classmethod
    def tearDownClass(cls):
        _cleanup2()

    def _add(self, ip):
        r = self.client.post(
            "/api/hosts",
            json={"ip": ip, "snmp_community": "public", "snmp_profile": "mikrotik"},
            headers=JSON_HDR,
        )
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

    def test_discover_toggle_delete(self):
        self._add(MT_IF_HOST)
        try:
            orig = m.discover_interfaces
            m.discover_interfaces = lambda ip, comm, max_if=48: [
                {"if_index": 1, "name": "ether1", "oper": 1},
                {"if_index": 2, "name": "ether2", "oper": 2},
            ]
            try:
                r = self.client.post(
                    f"/api/mikrotik/{MT_IF_HOST}/interfaces/discover", headers=XRW_HDR
                )
                self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
                self.assertEqual(r.get_json()["count"], 2)
            finally:
                m.discover_interfaces = orig
            r = self.client.get(
                f"/api/mikrotik/{MT_IF_HOST}/interfaces", headers=XRW_HDR
            )
            self.assertEqual(len(r.get_json()), 2)
            r = self.client.patch(
                f"/api/mikrotik/{MT_IF_HOST}/interfaces",
                json={"if_index": 2, "monitor": False},
                headers=JSON_HDR,
            )
            self.assertEqual(r.status_code, 200)
            r = self.client.patch(
                f"/api/mikrotik/{MT_IF_HOST}/interfaces",
                json={"if_index": 99, "monitor": False},
                headers=JSON_HDR,
            )
            self.assertEqual(r.status_code, 404)
            r = self.client.delete(
                f"/api/mikrotik/{MT_IF_HOST}/interfaces/2", headers=XRW_HDR
            )
            self.assertEqual(r.status_code, 200)
            r = self.client.get(
                f"/api/mikrotik/{MT_IF_HOST}/interfaces", headers=XRW_HDR
            )
            self.assertEqual(len(r.get_json()), 1)
            # discover host tak dikenal / tanpa community
            r = self.client.post(
                "/api/mikrotik/10.99.99.250/interfaces/discover", headers=XRW_HDR
            )
            self.assertEqual(r.status_code, 404)
        finally:
            self.client.delete(f"/api/hosts/{MT_IF_HOST}", headers=XRW_HDR)

    def test_port_down_up_dan_traffic(self):
        import time as _t

        self._add(MT_IF_HOST)
        conn, c = m.get_db()
        c.execute(
            "INSERT INTO snmp_interfaces (host, if_index, name, oper, monitor)"
            " VALUES (?,?,?,1,1)",
            (MT_IF_HOST, 1, "ether1"),
        )
        conn.commit()
        conn.close()
        state = {"oper": 1, "in": 1000, "out": 2000}
        sent = []
        orig_check = m._mt_iface_check_one
        orig_tg = m.send_telegram_alert
        m._mt_iface_check_one = lambda args: (
            args[0],
            {
                "opers": {1: state["oper"]},
                "counters": {1: {"in": state["in"], "out": state["out"]}},
            },
        )
        m.send_telegram_alert = lambda msg: sent.append(msg)
        try:
            m.poll_mikrotik_ifaces()  # baseline, diam
            self.assertEqual(sent, [])
            state["oper"] = 2
            m.poll_mikrotik_ifaces()
            self.assertTrue(any("PORT DOWN" in s for s in sent))
            r = self.client.get("/api/triggers", headers=XRW_HDR)
            self.assertTrue(
                any(
                    a["category"] == "mikrotik" and "PORT DOWN" in a["message"]
                    for a in r.get_json()
                )
            )
            # flip balik cepat -> cooldown: tanpa telegram, tercatat flapping
            state["oper"] = 1
            m.poll_mikrotik_ifaces()
            self.assertFalse(any("PORT UP" in s for s in sent))
            self.assertEqual(m.mt_iface_flaps.get((MT_IF_HOST, 1)), 1)
            # DB oper + log tetap terupdate walau telegram disuppress
            conn, c = m.get_db()
            oper_db = c.execute(
                "SELECT oper FROM snmp_interfaces WHERE host=? AND if_index=1",
                (MT_IF_HOST,),
            ).fetchone()[0]
            up_log = c.execute(
                "SELECT COUNT(*) FROM system_logs WHERE host=? AND event_type='MT_PORT_UP'",
                (MT_IF_HOST,),
            ).fetchone()[0]
            conn.close()
            self.assertEqual(oper_db, 1)
            self.assertGreaterEqual(up_log, 1)
            # lewat cooldown + flip lagi -> telegram dengan catatan flapping
            m.mt_iface_tg[(MT_IF_HOST, 1)] = _t.time() - 601
            state["oper"] = 2
            m.poll_mikrotik_ifaces()
            down2 = [s for s in sent if "PORT DOWN" in s]
            self.assertEqual(len(down2), 2)
            self.assertIn("flapping", down2[-1])
            conn, c = m.get_db()
            n = c.execute(
                "SELECT COUNT(*) FROM iface_traffic WHERE host=?", (MT_IF_HOST,)
            ).fetchone()[0]
            conn.close()
            self.assertGreaterEqual(n, 1)
            r = self.client.get(
                f"/api/mikrotik/{MT_IF_HOST}/iface/1/history?hours=1", headers=XRW_HDR
            )
            self.assertGreaterEqual(r.get_json()["count"], 1)
            r = self.client.get(
                f"/api/mikrotik/{MT_IF_HOST}/iface/99/history?hours=1", headers=XRW_HDR
            )
            self.assertEqual(r.status_code, 404)
        finally:
            m._mt_iface_check_one = orig_check
            m.send_telegram_alert = orig_tg
            for _mem in (
                m.mt_iface_oper,
                m.iface_state,
                m.mt_iface_tg,
                m.mt_iface_flaps,
            ):
                _mem.pop((MT_IF_HOST, 1), None)
            self.client.delete(f"/api/hosts/{MT_IF_HOST}", headers=XRW_HDR)

    def test_reboot_detect_dan_trigger(self):
        self._add(MT_RB_HOST)
        ticks = [10000000]
        sent = []
        orig_health = m.get_mikrotik_health
        orig_get = m._snmp_get
        orig_tg = m.send_telegram_alert
        m.get_mikrotik_health = lambda ip, comm, oids: {
            "cpu": 10.0,
            "mem": 20.0,
            "storage": 10.0,
            "temp_raw": 400.0,
        }
        m._snmp_get = lambda ip, comm, oids, timeout=2.0: [ticks[0]]
        m.send_telegram_alert = lambda msg: sent.append(msg)
        try:
            m.poll_mikrotik_health()  # baseline
            self.assertFalse(any("REBOOT" in s for s in sent))
            ticks[0] = 5000  # reboot!
            m.poll_mikrotik_health()
            self.assertTrue(any("REBOOT" in s for s in sent))
            conn, c = m.get_db()
            row = c.execute(
                "SELECT uptime_s FROM device_health WHERE host=? ORDER BY id DESC LIMIT 1",
                (MT_RB_HOST,),
            ).fetchone()
            conn.close()
            self.assertAlmostEqual(row[0], 50.0)
            r = self.client.get("/api/triggers", headers=XRW_HDR)
            self.assertTrue(
                any(
                    a["category"] == "mikrotik" and "reboot" in a["message"]
                    for a in r.get_json()
                )
            )
            r = self.client.get("/api/mikrotik", headers=XRW_HDR)
            item = next(o for o in r.get_json() if o["host"] == MT_RB_HOST)
            self.assertIn("m", item["uptime"])
        finally:
            m.get_mikrotik_health = orig_health
            m._snmp_get = orig_get
            m.send_telegram_alert = orig_tg
            self.client.delete(f"/api/hosts/{MT_RB_HOST}", headers=XRW_HDR)


MT_ST_HOST = "10.99.99.25"


def _cleanup_st():
    conn, c = m.get_db()
    try:
        c.execute("DELETE FROM device_health WHERE host=?", (MT_ST_HOST,))
        c.execute("DELETE FROM hosts WHERE ip=?", (MT_ST_HOST,))
        m.mt_alarm_memory.pop(MT_ST_HOST, None)
        m.mt_is_mikrotik.pop(MT_ST_HOST, None)
        m.mt_sysup.pop(MT_ST_HOST, None)
        conn.commit()
    finally:
        conn.close()


class MikrotikStaleWrapTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = m.app.test_client()
        _cleanup_st()
        r = cls.client.post(
            "/login", data={"username": "admin", "password": "admin12345"}
        )
        assert r.status_code == 302, f"login gagal, status={r.status_code}"

    @classmethod
    def tearDownClass(cls):
        _cleanup_st()

    def _add(self, ip):
        r = self.client.post(
            "/api/hosts",
            json={"ip": ip, "snmp_community": "public", "snmp_profile": "mikrotik"},
            headers=JSON_HDR,
        )
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

    def _insert_health(self, ts, cpu=20.0):
        conn, c = m.get_db()
        c.execute(
            "INSERT INTO device_health (host, cpu, mem_used, storage_used,"
            " temp_c, uptime_s, timestamp, source)"
            " VALUES (?,?,?,?,?,?,?,'snmp-mikrotik')",
            (MT_ST_HOST, cpu, 30.0, 10.0, 45.0, 3600.0, ts),
        )
        conn.commit()
        conn.close()

    def test_stale_info_helper(self):
        from datetime import datetime, timedelta

        old = (datetime.now() - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
        fresh = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.assertTrue(m._mt_stale_info(old)[0])
        self.assertFalse(m._mt_stale_info(fresh)[0])
        self.assertFalse(m._mt_stale_info("")[0])
        self.assertFalse(m._mt_stale_info(None)[0])

    def test_stale_di_list_dan_triggers(self):
        from datetime import datetime, timedelta

        self._add(MT_ST_HOST)
        try:
            fresh = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._insert_health(fresh)
            r = self.client.get("/api/mikrotik", headers=XRW_HDR)
            item = next(o for o in r.get_json() if o["host"] == MT_ST_HOST)
            self.assertEqual(item["status"], "normal")
            self.assertFalse(item["stale"])
            old = (datetime.now() - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
            conn, c = m.get_db()
            c.execute(
                "UPDATE device_health SET timestamp=? WHERE host=?", (old, MT_ST_HOST)
            )
            conn.commit()
            conn.close()
            r = self.client.get("/api/mikrotik", headers=XRW_HDR)
            item = next(o for o in r.get_json() if o["host"] == MT_ST_HOST)
            self.assertEqual(item["status"], "stale")
            self.assertTrue(item["stale"])
            self.assertIn("SNMP", item["advice"])
            r = self.client.get("/api/triggers", headers=XRW_HDR)
            stale = [
                a
                for a in r.get_json()
                if a.get("category") == "mikrotik"
                and MT_ST_HOST in a.get("host", "")
                and "STALE" in a.get("message", "")
            ]
            self.assertTrue(stale)
            self.assertEqual(stale[0]["severity"], "warning")
        finally:
            self.client.delete(f"/api/hosts/{MT_ST_HOST}", headers=XRW_HDR)

    def test_reboot_kind_helper(self):
        self.assertEqual(m._mt_reboot_kind(100000.0, 50.0), "reboot")
        self.assertIsNone(m._mt_reboot_kind(50.0, 100000.0))
        self.assertIsNone(m._mt_reboot_kind(None, 50.0))
        wrap_prev = 2**32 / 100.0 - 1000.0
        self.assertEqual(m._mt_reboot_kind(wrap_prev, 50.0), "wrap")
        # jauh dari batas wrap walau turun -> tetap reboot
        self.assertEqual(m._mt_reboot_kind(1000000.0, 50.0), "reboot")

    def test_wrap_tidak_panik(self):
        self._add(MT_ST_HOST)
        sent = []
        orig_health = m.get_mikrotik_health
        orig_get = m._snmp_get
        orig_tg = m.send_telegram_alert
        wrap_prev = 2**32 / 100.0 - 1000.0
        ticks = [wrap_prev * 100]
        m.get_mikrotik_health = lambda ip, comm, oids: {
            "cpu": 10.0,
            "mem": 20.0,
            "storage": 10.0,
            "temp_raw": 400.0,
        }
        m._snmp_get = lambda ip, comm, oids, timeout=2.0: [ticks[0]]
        m.send_telegram_alert = lambda msg: sent.append(msg)
        try:
            m.poll_mikrotik_health()
            self.assertFalse(any("REBOOT" in s or "WRAP" in s for s in sent))
            ticks[0] = 5000
            m.poll_mikrotik_health()
            wrap = [s for s in sent if "WRAP" in s]
            self.assertTrue(wrap)
            self.assertFalse(any("REBOOT TERDETEKSI" in s for s in sent))
        finally:
            m.get_mikrotik_health = orig_health
            m._snmp_get = orig_get
            m.send_telegram_alert = orig_tg
            self.client.delete(f"/api/hosts/{MT_ST_HOST}", headers=XRW_HDR)

    def test_settings_mt_stale_min(self):
        r = self.client.get("/api/settings", headers=XRW_HDR)
        orig = r.get_json()
        try:
            r = self.client.post(
                "/api/settings", json={"mt_stale_min": 2}, headers=JSON_HDR
            )
            self.assertEqual(r.status_code, 400)
            r = self.client.post(
                "/api/settings", json={"mt_stale_min": 2000}, headers=JSON_HDR
            )
            self.assertEqual(r.status_code, 400)
            r = self.client.post(
                "/api/settings", json={"mt_stale_min": 30}, headers=JSON_HDR
            )
            self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
            r = self.client.get("/api/settings", headers=XRW_HDR)
            self.assertEqual(int(r.get_json()["mt_stale_min"]), 30)
        finally:
            self.client.post(
                "/api/settings",
                json={"mt_stale_min": orig["mt_stale_min"]},
                headers=JSON_HDR,
            )

    def test_iface_history_didisample(self):
        from datetime import datetime, timedelta

        self._add(MT_ST_HOST)
        conn, c = m.get_db()
        c.execute(
            "INSERT INTO snmp_interfaces (host, if_index, name, oper, monitor)"
            " VALUES (?,?,?,1,1)",
            (MT_ST_HOST, 1, "ether1"),
        )
        base = datetime.now() - timedelta(minutes=600)
        rows = [
            (
                MT_ST_HOST,
                1,
                10.0,
                5.0,
                (base + timedelta(minutes=i)).strftime("%Y-%m-%d %H:%M:%S"),
            )
            for i in range(600)
        ]
        c.executemany(
            "INSERT INTO iface_traffic (host, if_index, net_in, net_out, timestamp)"
            " VALUES (?,?,?,?,?)",
            rows,
        )
        conn.commit()
        conn.close()
        try:
            r = self.client.get(
                f"/api/mikrotik/{MT_ST_HOST}/iface/1/history?hours=336", headers=XRW_HDR
            )
            self.assertEqual(r.status_code, 200)
            j = r.get_json()
            self.assertLessEqual(j["count"], 500)
            self.assertEqual(j["count"], 300)  # 600 titik -> step 2
        finally:
            self.client.delete(f"/api/hosts/{MT_ST_HOST}", headers=XRW_HDR)

    def test_discover_besar_flag_truncated(self):
        self._add(MT_ST_HOST)
        orig = m.discover_interfaces
        m.discover_interfaces = lambda ip, comm, max_if=128: [
            {"if_index": i, "name": f"ether{i}", "oper": 1} for i in range(1, 129)
        ]
        try:
            r = self.client.post(
                f"/api/mikrotik/{MT_ST_HOST}/interfaces/discover", headers=XRW_HDR
            )
            self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
            j = r.get_json()
            self.assertEqual(j["count"], 128)
            self.assertTrue(j["truncated"])
        finally:
            m.discover_interfaces = orig
            self.client.delete(f"/api/hosts/{MT_ST_HOST}", headers=XRW_HDR)


MT_BK_HOST = "10.99.99.26"
CFG_V1 = "/system identity\nset name=R1\n/ip address\nadd address=1.1.1.1/24 interface=ether1\n"
CFG_V2 = "/system identity\nset name=R1-RENAMED\n/ip address\nadd address=1.1.1.1/24 interface=ether1\nadd address=2.2.2.2/24 interface=ether2\n"


def _cleanup_bk():
    conn, c = m.get_db()
    try:
        c.execute("DELETE FROM mt_backups WHERE host=?", (MT_BK_HOST,))
        c.execute("DELETE FROM hosts WHERE ip=?", (MT_BK_HOST,))
        conn.commit()
    finally:
        conn.close()


class MikrotikBackupTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = m.app.test_client()
        _cleanup_bk()
        r = cls.client.post(
            "/login", data={"username": "admin", "password": "admin12345"}
        )
        assert r.status_code == 302, f"login gagal, status={r.status_code}"

    @classmethod
    def tearDownClass(cls):
        _cleanup_bk()

    def _add(self):
        r = self.client.post(
            "/api/hosts",
            json={
                "ip": MT_BK_HOST,
                "snmp_community": "",
                "ssh_user": "admin",
                "ssh_pass": "rahasia",
                "ssh_port": 22,
                "backup_enable": 1,
            },
            headers=JSON_HDR,
        )
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

    def test_validasi_ssh(self):
        r = self.client.post(
            "/api/hosts",
            json={"ip": "10.99.99.27", "ssh_port": 99999},
            headers=JSON_HDR,
        )
        self.assertEqual(r.status_code, 400)
        self._add()
        try:
            r = self.client.patch(
                f"/api/hosts/{MT_BK_HOST}/snmp", json={"ssh_port": 0}, headers=JSON_HDR
            )
            self.assertEqual(r.status_code, 400)
            r = self.client.patch(
                f"/api/hosts/{MT_BK_HOST}/snmp",
                json={"ssh_port": 2222, "backup_enable": 1},
                headers=JSON_HDR,
            )
            self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
            r = self.client.get("/api/hosts", headers=XRW_HDR)
            body = r.get_data(as_text=True)
            self.assertNotIn("rahasia", body)
            self.assertNotIn("ssh_pass", body.replace("ssh_pass_set", ""))
            item = next(o for o in r.get_json() if o["ip"] == MT_BK_HOST)
            self.assertEqual(item["ssh_user"], "admin")
            self.assertTrue(item["ssh_pass_set"])
            self.assertEqual(item["ssh_port"], 2222)
            self.assertTrue(item["backup_enable"])
        finally:
            self.client.delete(f"/api/hosts/{MT_BK_HOST}", headers=XRW_HDR)

    def test_poll_baseline_dedup_berubah_prune(self):
        self._add()
        cfg = [CFG_V1]
        sent = []
        orig_fetch, orig_tg = m.fetch_mikrotik_config, m.send_telegram_alert
        m.fetch_mikrotik_config = lambda *a, **k: (True, cfg[0])
        m.send_telegram_alert = lambda msg: sent.append(msg)
        try:
            m.poll_mt_backups()  # baseline sunyi
            conn, c = m.get_db()
            n = c.execute(
                "SELECT COUNT(*) FROM mt_backups WHERE host=?", (MT_BK_HOST,)
            ).fetchone()[0]
            conn.close()
            self.assertEqual(n, 1)
            self.assertEqual(sent, [])
            m.poll_mt_backups()  # identik -> tak ada baris baru
            conn, c = m.get_db()
            n = c.execute(
                "SELECT COUNT(*) FROM mt_backups WHERE host=?", (MT_BK_HOST,)
            ).fetchone()[0]
            conn.close()
            self.assertEqual(n, 1)
            cfg[0] = CFG_V2
            m.poll_mt_backups()  # berubah -> telegram
            conn, c = m.get_db()
            row = c.execute(
                "SELECT changed FROM mt_backups WHERE host=? ORDER BY id DESC LIMIT 1",
                (MT_BK_HOST,),
            ).fetchone()
            conn.close()
            self.assertEqual(row[0], 1)
            self.assertTrue(any("BERUBAH" in s for s in sent))
            # prune ke 3 versi
            r = self.client.get("/api/settings", headers=XRW_HDR)
            orig = r.get_json()
            self.client.post(
                "/api/settings", json={"mt_backup_keep": 3}, headers=JSON_HDR
            )
            try:
                for txt in ("#v3\n", "#v4\n", "#v5\n"):
                    cfg[0] = CFG_V2 + txt
                    m.poll_mt_backups()
                conn, c = m.get_db()
                n = c.execute(
                    "SELECT COUNT(*) FROM mt_backups WHERE host=?", (MT_BK_HOST,)
                ).fetchone()[0]
                conn.close()
                self.assertEqual(n, 3)
            finally:
                self.client.post(
                    "/api/settings",
                    json={"mt_backup_keep": orig["mt_backup_keep"]},
                    headers=JSON_HDR,
                )
        finally:
            m.fetch_mikrotik_config, m.send_telegram_alert = orig_fetch, orig_tg
            self.client.delete(f"/api/hosts/{MT_BK_HOST}", headers=XRW_HDR)

    def test_gagal_tak_bertelegram_tapi_trigger(self):
        self._add()
        orig_fetch, orig_tg = m.fetch_mikrotik_config, m.send_telegram_alert
        m.fetch_mikrotik_config = lambda *a, **k: (False, "auth failed")
        sent = []
        m.send_telegram_alert = lambda msg: sent.append(msg)
        try:
            m.poll_mt_backups()
            self.assertEqual(sent, [])
            r = self.client.get("/api/hosts", headers=XRW_HDR)
            item = next(o for o in r.get_json() if o["ip"] == MT_BK_HOST)
            self.assertFalse(item["backup_ok"])
            r = self.client.get("/api/triggers", headers=XRW_HDR)
            bad = [
                a
                for a in r.get_json()
                if a.get("category") == "mikrotik"
                and MT_BK_HOST in a.get("host", "")
                and "Backup" in a.get("message", "")
            ]
            self.assertTrue(bad)
        finally:
            m.fetch_mikrotik_config, m.send_telegram_alert = orig_fetch, orig_tg
            self.client.delete(f"/api/hosts/{MT_BK_HOST}", headers=XRW_HDR)

    def test_endpoint_list_isi_download_diff_manual(self):
        self._add()
        cfg = [CFG_V1]
        orig_fetch = m.fetch_mikrotik_config
        m.fetch_mikrotik_config = lambda *a, **k: (True, cfg[0])
        try:
            r = self.client.post(f"/api/mikrotik/{MT_BK_HOST}/backup", headers=XRW_HDR)
            self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
            self.assertFalse(r.get_json()["notified"])
            cfg[0] = CFG_V2
            r = self.client.post(f"/api/mikrotik/{MT_BK_HOST}/backup", headers=XRW_HDR)
            self.assertTrue(r.get_json()["notified"])
            r = self.client.get(f"/api/mikrotik/{MT_BK_HOST}/backups", headers=XRW_HDR)
            rows = r.get_json()
            self.assertEqual(len(rows), 2)
            self.assertNotIn("content", r.get_data(as_text=True))
            bid = rows[0]["id"]
            r = self.client.get(
                f"/api/mikrotik/{MT_BK_HOST}/backups/{bid}", headers=XRW_HDR
            )
            self.assertIn("set name=R1-RENAMED", r.get_json()["content"])
            r = self.client.get(
                f"/api/mikrotik/{MT_BK_HOST}/backups/{bid}?download=1", headers=XRW_HDR
            )
            self.assertEqual(r.status_code, 200)
            self.assertIn("attachment", r.headers.get("Content-Disposition", ""))
            r = self.client.get(
                f"/api/mikrotik/{MT_BK_HOST}/backups/{bid}/diff", headers=XRW_HDR
            )
            j = r.get_json()
            self.assertTrue(any(l.startswith("+") and "ether2" in l for l in j["diff"]))
            r = self.client.get(f"/api/mikrotik/10.99.99.250/backups", headers=XRW_HDR)
            self.assertEqual(r.status_code, 404)
        finally:
            m.fetch_mikrotik_config = orig_fetch
            self.client.delete(f"/api/hosts/{MT_BK_HOST}", headers=XRW_HDR)


if __name__ == "__main__":
    unittest.main()
