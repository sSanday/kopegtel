import os
import tempfile
import unittest

# bootstrap agar aman dijalankan standalone (sebelum import app):
# pakai DB sementara, jangan pernah menyentuh network.db produksi.
_tmp = tempfile.mkdtemp(prefix="nms_test_fiber_")
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

SN_NORMAL = "TEST-FIBER-NORMAL-01"
SN_OVERLOAD = "TEST-FIBER-OVERLOAD-01"
SN_CRIT = "TEST-FIBER-CRIT-01"
SN_TXBAD = "TEST-FIBER-TXBAD-01"
ALL_SN = (SN_NORMAL, SN_OVERLOAD, SN_CRIT, SN_TXBAD)


def _cleanup_sn():
    conn, c = m.get_db()
    try:
        for sn in ALL_SN:
            c.execute("SELECT id FROM fiber_onts WHERE ont_sn=?", (sn,))
            row = c.fetchone()
            if row:
                c.execute("DELETE FROM fiber_history WHERE ont_id=?", (row["id"],))
                c.execute("DELETE FROM fiber_downtime WHERE ont_id=?", (row["id"],))
                c.execute("DELETE FROM fiber_onts WHERE id=?", (row["id"],))
                m.fiber_alarm_memory.pop(row["id"], None)
        conn.commit()
    finally:
        conn.close()


class FiberThresholdTest(unittest.TestCase):
    def test_rx_levels_default(self):
        th = {"overload": -8.0, "warn": -25.0, "crit": -27.0, "target": -18.0,
              "tx_min": 0.0, "tx_max": 5.0}
        self.assertEqual(m.fiber_status_for_rx(-19.0, th)[0], "normal")
        self.assertEqual(m.fiber_status_for_rx(-26.0, th)[0], "warning")
        self.assertEqual(m.fiber_status_for_rx(-28.5, th)[0], "critical")
        self.assertEqual(m.fiber_status_for_rx(None, th)[0], "unknown")

    def test_overload_saran_peredam(self):
        th = {"overload": -8.0, "warn": -25.0, "crit": -27.0, "target": -18.0,
              "tx_min": 0.0, "tx_max": 5.0}
        status, sev, advice, need = m.fiber_status_for_rx(-5.0, th)
        self.assertEqual(status, "overload")
        self.assertEqual(sev, "high")
        self.assertIn("PEREDAM", advice)
        self.assertGreater(need, 0)

    def test_tx_abnormal(self):
        th = {"overload": -8.0, "warn": -25.0, "crit": -27.0, "target": -18.0,
              "tx_min": 0.0, "tx_max": 5.0}
        self.assertEqual(m.fiber_status_for_tx(2.1, th)[0], "tx_ok")
        self.assertEqual(m.fiber_status_for_tx(None, th)[0], "tx_unknown")
        st, sev, _adv = m.fiber_status_for_tx(7.5, th)
        self.assertEqual(st, "tx_abnormal")
        self.assertIsNotNone(sev)
        st, sev, _adv = m.fiber_status_for_tx(-8.0, th)
        self.assertEqual(st, "tx_abnormal")
        self.assertEqual(sev, "high")

    def test_eval_gabungan_rx_tx(self):
        th = {"overload": -8.0, "warn": -25.0, "crit": -27.0, "target": -18.0,
              "tx_min": 0.0, "tx_max": 5.0}
        # rx normal + tx normal
        self.assertEqual(m.fiber_eval(-19.0, 2.0, th)[0], "normal")
        # rx normal + tx rusak -> warning ikut tx
        st, sev, adv, _n = m.fiber_eval(-19.0, 9.0, th)
        self.assertEqual(st, "warning")
        self.assertIn("Tx", adv)
        # rx critical + tx rusak -> critical menang (severity disaster)
        st, sev, _adv, _n = m.fiber_eval(-29.0, 9.0, th)
        self.assertEqual(st, "critical")
        self.assertEqual(sev, "disaster")

    def test_eval_tanpa_rx_status_konsisten_dengan_severity(self):
        st, sev, _a, _n = m.fiber_eval(None, -8.0)
        self.assertEqual(sev, "high")
        self.assertEqual(st, "critical")
        st, sev, _a, _n = m.fiber_eval(None, 9.0)
        self.assertEqual((st, sev), ("warning", "warning"))

    def test_threshold_dari_settings(self):
        conn, c = m.get_db()
        c.execute("SELECT value FROM settings WHERE key='fiber_rx_warn'")
        row = c.fetchone()
        conn.close()
        # seed default harus ada setelah init_db
        self.assertIsNotNone(row)
        th = m._fiber_thresholds()
        for k in ("overload", "warn", "crit", "target", "tx_min", "tx_max"):
            self.assertIn(k, th)


class FiberApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = m.app.test_client()
        _cleanup_sn()
        r = cls.client.post("/login",
                            data={"username": "admin", "password": "admin12345"})
        assert r.status_code == 302, f"login gagal, status={r.status_code}"

    @classmethod
    def tearDownClass(cls):
        _cleanup_sn()

    def _create(self, sn, rx, tx=2.0, source="manual"):
        r = self.client.post("/api/fiber",
                             json={"ont_sn": sn, "customer": "uji",
                                   "olt_name": "OLT-UJI", "pon_port": "1/1/1",
                                   "odp_name": "ODP-UJI", "rx_power": rx,
                                   "tx_power": tx, "source": source},
                             headers=JSON_HDR)
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        return r.get_json()["id"]

    def test_crud_dan_status(self):
        fid = self._create(SN_NORMAL, -19.5, 2.1)
        try:
            r = self.client.get("/api/fiber", headers=XRW_HDR)
            self.assertEqual(r.status_code, 200)
            item = next(o for o in r.get_json() if o["ont_sn"] == SN_NORMAL)
            self.assertEqual(item["calc_status"], "normal")

            r = self.client.put(f"/api/fiber/{fid}",
                                json={"ont_sn": SN_NORMAL, "rx_power": -26.0,
                                      "tx_power": 2.0, "source": "manual"},
                                headers=JSON_HDR)
            self.assertEqual(r.status_code, 200)
            r = self.client.get("/api/fiber", headers=XRW_HDR)
            item = next(o for o in r.get_json() if o["ont_sn"] == SN_NORMAL)
            self.assertEqual(item["calc_status"], "warning")

            r = self.client.get(f"/api/fiber/{fid}/history?hours=24",
                                headers=XRW_HDR)
            self.assertEqual(r.status_code, 200)
            self.assertGreaterEqual(r.get_json()["count"], 2)

            r = self.client.delete(f"/api/fiber/{fid}", headers=XRW_HDR)
            self.assertEqual(r.status_code, 200)
            r = self.client.delete(f"/api/fiber/{fid}", headers=XRW_HDR)
            self.assertEqual(r.status_code, 404)
        finally:
            conn, c = m.get_db()
            c.execute("DELETE FROM fiber_history WHERE ont_id=?", (fid,))
            c.execute("DELETE FROM fiber_downtime WHERE ont_id=?", (fid,))
            c.execute("DELETE FROM fiber_onts WHERE id=?", (fid,))
            conn.commit()
            conn.close()
            m.fiber_alarm_memory.pop(fid, None)

    def test_validasi(self):
        r = self.client.post("/api/fiber", json={"ont_sn": "x", "rx_power": -19},
                             headers=JSON_HDR)
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/api/fiber", json={"ont_sn": SN_NORMAL, "rx_power": 99},
                             headers=JSON_HDR)
        self.assertEqual(r.status_code, 400)

    def test_overload_critical_masuk_triggers(self):
        f1 = self._create(SN_OVERLOAD, -5.0, 2.0)
        f2 = self._create(SN_CRIT, -29.0, 2.0)
        f3 = self._create(SN_TXBAD, -19.0, 9.0)
        try:
            r = self.client.get("/api/triggers", headers=XRW_HDR)
            self.assertEqual(r.status_code, 200)
            fib = [a for a in r.get_json() if a.get("category") == "fiber"]
            by_host = {a["host"].split(" ")[0]: a for a in fib}
            self.assertIn(SN_OVERLOAD, by_host)
            self.assertEqual(by_host[SN_OVERLOAD]["severity"], "high")
            self.assertIn(SN_CRIT, by_host)
            self.assertEqual(by_host[SN_CRIT]["severity"], "disaster")
            self.assertIn(SN_TXBAD, by_host)
            self.assertIn("Tx", by_host[SN_TXBAD]["message"])
        finally:
            for fid in (f1, f2, f3):
                self.client.delete(f"/api/fiber/{fid}", headers=XRW_HDR)

    def test_export_csv(self):
        fid = self._create(SN_NORMAL, -19.5, 2.1)
        try:
            r = self.client.get("/api/fiber/export", headers=XRW_HDR)
            self.assertEqual(r.status_code, 200)
            body = r.get_data(as_text=True)
            self.assertIn("ont_sn", body)
            self.assertIn(SN_NORMAL, body)
        finally:
            self.client.delete(f"/api/fiber/{fid}", headers=XRW_HDR)

    def test_settings_fiber_validasi_dan_efek(self):
        # simpan asli untuk restore
        r = self.client.get("/api/settings", headers=XRW_HDR)
        self.assertEqual(r.status_code, 200)
        orig = r.get_json()
        try:
            # konsistensi ditolak
            r = self.client.post("/api/settings",
                                 json={"fiber_rx_crit": -20.0, "fiber_rx_warn": -25.0},
                                 headers=JSON_HDR)
            self.assertEqual(r.status_code, 400)
            r = self.client.post("/api/settings",
                                 json={"fiber_tx_min": 6.0, "fiber_tx_max": 5.0},
                                 headers=JSON_HDR)
            self.assertEqual(r.status_code, 400)
            # ubah warn jadi -20 -> rx -21 harusnya warning
            r = self.client.post("/api/settings",
                                 json={"fiber_rx_warn": -20.0},
                                 headers=JSON_HDR)
            self.assertEqual(r.status_code, 200)
            th = m._fiber_thresholds()
            self.assertEqual(th["warn"], -20.0)
            self.assertEqual(m.fiber_status_for_rx(-21.0)[0], "warning")
        finally:
            self.client.post("/api/settings",
                             json={k: orig[k] for k in
                                   ("fiber_rx_overload", "fiber_rx_warn", "fiber_rx_crit",
                                    "fiber_rx_target", "fiber_tx_min", "fiber_tx_max")},
                             headers=JSON_HDR)

    def test_poll_tidak_crash_dan_tidak_ubah_manual(self):
        fid = self._create(SN_NORMAL, -19.5, 2.1, source="manual")
        try:
            conn, c = m.get_db()
            before = c.execute("SELECT COUNT(*) FROM fiber_history WHERE ont_id=?",
                               (fid,)).fetchone()[0]
            conn.close()
            m.poll_fiber_monitor()
            conn, c = m.get_db()
            c.execute("SELECT rx_power, status FROM fiber_onts WHERE id=?", (fid,))
            row = c.fetchone()
            after = c.execute("SELECT COUNT(*) FROM fiber_history WHERE ont_id=?",
                              (fid,)).fetchone()[0]
            conn.close()
            self.assertAlmostEqual(row["rx_power"], -19.5)
            self.assertEqual(row["status"], "normal")
            # snapshot history tercatat walau source=manual (grafik tidak kosong)
            self.assertEqual(after, before + 1)
        finally:
            self.client.delete(f"/api/fiber/{fid}", headers=XRW_HDR)


class FiberLinkBudgetTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = m.app.test_client()
        _cleanup_sn()
        r = cls.client.post("/login",
                            data={"username": "admin", "password": "admin12345"})
        assert r.status_code == 302, f"login gagal, status={r.status_code}"

    @classmethod
    def tearDownClass(cls):
        _cleanup_sn()

    def test_hitung_matematika(self):
        # tx 3.0 - (7.2+10.5) - 2km*0.35 - 4*0.5 - 2*0.1 = 3.0-20.6 = -17.6
        r = m.fiber_link_budget(3.0, ["1:4", "1:8"], 2.0, 4, 2)
        self.assertAlmostEqual(r["losses"]["splitter_db"], 17.7)
        self.assertAlmostEqual(r["losses"]["fiber_db"], 0.7)
        self.assertAlmostEqual(r["losses"]["connector_db"], 2.0)
        self.assertAlmostEqual(r["losses"]["splice_db"], 0.2)
        self.assertAlmostEqual(r["losses"]["total_db"], 20.6)
        self.assertAlmostEqual(r["expected_rx"], -17.6)

    def test_verdict(self):
        v, _s, _a = m.fiber_budget_verdict(-17.6, -17.0)
        self.assertEqual(v, "ok")
        v, s, a = m.fiber_budget_verdict(-17.6, -25.0)
        self.assertEqual(v, "over_budget")
        self.assertEqual(s, "high")
        self.assertIn("bending", a)
        v, _s, _a = m.fiber_budget_verdict(-17.6, None)
        self.assertEqual(v, "no_data")

    def test_api_tanpa_pembanding(self):
        r = self.client.post("/api/fiber/link-budget",
                             json={"tx_dbm": 3.0, "splitters": ["1:4", "1:8"],
                                   "fiber_km": 2.0, "connectors": 4, "splices": 2},
                             headers=JSON_HDR)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        j = r.get_json()
        self.assertAlmostEqual(j["expected_rx"], -17.6)
        self.assertEqual(j["verdict"], "no_data")
        self.assertIsNone(j["actual_rx"])

    def test_api_validasi(self):
        r = self.client.post("/api/fiber/link-budget",
                             json={"tx_dbm": 3.0, "splitters": ["1:64"],
                                   "fiber_km": 2.0},
                             headers=JSON_HDR)
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/api/fiber/link-budget",
                             json={"tx_dbm": 3.0, "splitters": [],
                                   "fiber_km": 2.0},
                             headers=JSON_HDR)
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/api/fiber/link-budget",
                             json={"tx_dbm": 3.0, "splitters": ["1:8"],
                                   "fiber_km": 2.0, "ont_id": 999999},
                             headers=JSON_HDR)
        self.assertEqual(r.status_code, 404)

    def test_api_dengan_pembanding_ont(self):
        r = self.client.post("/api/fiber",
                             json={"ont_sn": SN_NORMAL, "customer": "uji",
                                   "rx_power": -25.0, "tx_power": 2.0,
                                   "source": "manual"},
                             headers=JSON_HDR)
        self.assertEqual(r.status_code, 201)
        fid = r.get_json()["id"]
        try:
            r = self.client.post("/api/fiber/link-budget",
                                 json={"tx_dbm": 3.0, "splitters": ["1:4", "1:8"],
                                       "fiber_km": 2.0, "connectors": 4,
                                       "splices": 2, "ont_id": fid},
                                 headers=JSON_HDR)
            self.assertEqual(r.status_code, 200)
            j = r.get_json()
            self.assertAlmostEqual(j["actual_rx"], -25.0)
            self.assertAlmostEqual(j["delta_db"], -7.4)
            self.assertEqual(j["verdict"], "over_budget")
        finally:
            self.client.delete(f"/api/fiber/{fid}", headers=XRW_HDR)


ODP_NAME = "TEST-ODP-01"
ODP_SN = "TEST-FIBER-ODP-01"


def _cleanup_odp():
    conn, c = m.get_db()
    try:
        c.execute("SELECT id FROM fiber_onts WHERE ont_sn=?", (ODP_SN,))
        row = c.fetchone()
        if row:
            c.execute("DELETE FROM fiber_history WHERE ont_id=?", (row["id"],))
            c.execute("DELETE FROM fiber_downtime WHERE ont_id=?", (row["id"],))
            c.execute("DELETE FROM fiber_onts WHERE id=?", (row["id"],))
            m.fiber_alarm_memory.pop(row["id"], None)
        c.execute("DELETE FROM odps WHERE name=?", (ODP_NAME,))
        conn.commit()
    finally:
        conn.close()


class OdpApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = m.app.test_client()
        _cleanup_odp()
        r = cls.client.post("/login",
                            data={"username": "admin", "password": "admin12345"})
        assert r.status_code == 302, f"login gagal, status={r.status_code}"

    @classmethod
    def tearDownClass(cls):
        _cleanup_odp()

    def test_validasi(self):
        r = self.client.post("/api/odps", json={"name": "x"}, headers=JSON_HDR)
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/api/odps",
                             json={"name": ODP_NAME, "capacity": 999},
                             headers=JSON_HDR)
        self.assertEqual(r.status_code, 400)

    def test_crud_dan_agregasi(self):
        r = self.client.post("/api/odps",
                             json={"name": ODP_NAME, "olt_name": "OLT-UJI",
                                   "capacity": 8, "location": "uji"},
                             headers=JSON_HDR)
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        oid = r.get_json()["id"]
        # duplikat ditolak
        r = self.client.post("/api/odps", json={"name": ODP_NAME},
                             headers=JSON_HDR)
        self.assertEqual(r.status_code, 400)
        try:
            # ONT critical di ODP ini
            r = self.client.post("/api/fiber",
                                 json={"ont_sn": ODP_SN, "customer": "uji",
                                       "odp_name": ODP_NAME, "rx_power": -29.0,
                                       "tx_power": 2.0, "source": "manual"},
                                 headers=JSON_HDR)
            self.assertEqual(r.status_code, 201)
            fid = r.get_json()["id"]
            try:
                r = self.client.get("/api/odps", headers=XRW_HDR)
                self.assertEqual(r.status_code, 200)
                item = next(o for o in r.get_json() if o["name"] == ODP_NAME)
                self.assertEqual(item["total"], 1)
                self.assertEqual(item["critical"], 1)
                self.assertEqual(item["worst"], "critical")
                self.assertAlmostEqual(item["fill_pct"], 12.5)
                # rename ODP memindahkan ONT
                r = self.client.put(f"/api/odps/{oid}",
                                    json={"name": ODP_NAME + "-R",
                                          "capacity": 8},
                                    headers=JSON_HDR)
                self.assertEqual(r.status_code, 200)
                r = self.client.get("/api/fiber", headers=XRW_HDR)
                ont = next(o for o in r.get_json() if o["ont_sn"] == ODP_SN)
                self.assertEqual(ont["odp_name"], ODP_NAME + "-R")
            finally:
                self.client.delete(f"/api/fiber/{fid}", headers=XRW_HDR)
        finally:
            # kembalikan nama lalu hapus
            conn, c = m.get_db()
            c.execute("UPDATE odps SET name=? WHERE id=?", (ODP_NAME, oid))
            conn.commit()
            conn.close()
            r = self.client.delete(f"/api/odps/{oid}", headers=XRW_HDR)
            self.assertEqual(r.status_code, 200)
            r = self.client.delete(f"/api/odps/{oid}", headers=XRW_HDR)
            self.assertEqual(r.status_code, 404)

    def test_odp_case_insensitive(self):
        r = self.client.post("/api/odps", json={"name": ODP_NAME, "capacity": 8},
                             headers=JSON_HDR)
        self.assertEqual(r.status_code, 201)
        oid = r.get_json()["id"]
        try:
            r = self.client.post("/api/fiber",
                                 json={"ont_sn": ODP_SN, "odp_name": ODP_NAME.lower(),
                                       "rx_power": -19.0, "source": "manual"},
                                 headers=JSON_HDR)
            self.assertEqual(r.status_code, 201)
            fid = r.get_json()["id"]
            try:
                r = self.client.get("/api/odps", headers=XRW_HDR)
                item = next(o for o in r.get_json() if o["name"] == ODP_NAME)
                self.assertEqual(item["total"], 1)
            finally:
                self.client.delete(f"/api/fiber/{fid}", headers=XRW_HDR)
        finally:
            self.client.delete(f"/api/odps/{oid}", headers=XRW_HDR)

    def test_ont_tanpa_odp_masuk_bucket(self):
        r = self.client.post("/api/fiber",
                             json={"ont_sn": ODP_SN, "rx_power": -19.0,
                                   "source": "manual"},
                             headers=JSON_HDR)
        self.assertEqual(r.status_code, 201)
        fid = r.get_json()["id"]
        try:
            r = self.client.get("/api/odps", headers=XRW_HDR)
            bucket = [o for o in r.get_json() if o["name"] == "(tanpa ODP)"]
            self.assertTrue(bucket and bucket[0]["total"] >= 1)
        finally:
            self.client.delete(f"/api/fiber/{fid}", headers=XRW_HDR)


SN_MUTE = "TEST-FIBER-MUTE-01"
SN_OVERRIDE = "TEST-FIBER-OVR-01"
SN_MAINT = "TEST-FIBER-MAINT-01"
SN_SINGLE = "TEST-FIBER-SINGLE-01"
SN_IMPORT_A = "TEST-FIBER-IMP-A"
SN_IMPORT_B = "TEST-FIBER-IMP-B"
OLT_TEST = "TEST-OLT-01"
SN_SNMP = "TEST-FIBER-SNMP-01"


def _cleanup_extra(client):
    for sn in (SN_MUTE, SN_OVERRIDE, SN_MAINT, SN_SINGLE, SN_IMPORT_A, SN_IMPORT_B, SN_SNMP):
        try:
            conn, c = m.get_db()
            try:
                c.execute("SELECT id FROM fiber_onts WHERE ont_sn=?", (sn,))
                row = c.fetchone()
                if row:
                    c.execute("DELETE FROM fiber_history WHERE ont_id=?", (row["id"],))
                    c.execute("DELETE FROM fiber_downtime WHERE ont_id=?", (row["id"],))
                    c.execute("DELETE FROM fiber_onts WHERE id=?", (row["id"],))
                    m.fiber_alarm_memory.pop(row["id"], None)
                c.execute("DELETE FROM maintenance_windows WHERE host=?", (sn,))
                conn.commit()
            finally:
                conn.close()
        except Exception:
            pass
    try:
        conn, c = m.get_db()
        try:
            c.execute("DELETE FROM olts WHERE name=?", (OLT_TEST,))
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


class FiberMuteOverrideTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = m.app.test_client()
        _cleanup_extra(cls.client)
        r = cls.client.post("/login",
                            data={"username": "admin", "password": "admin12345"})
        assert r.status_code == 302, f"login gagal, status={r.status_code}"

    @classmethod
    def tearDownClass(cls):
        _cleanup_extra(cls.client)

    def test_mute_disembunyikan_dari_triggers(self):
        r = self.client.post("/api/fiber",
                             json={"ont_sn": SN_MUTE, "rx_power": -29.0,
                                   "tx_power": 2.0, "mute_alarm": 1},
                             headers=JSON_HDR)
        self.assertEqual(r.status_code, 201)
        fid = r.get_json()["id"]
        try:
            r = self.client.get("/api/triggers", headers=XRW_HDR)
            fib = [a for a in r.get_json()
                   if a.get("category") == "fiber" and SN_MUTE in a.get("host", "")]
            self.assertEqual(fib, [])
            # unmute -> muncul lagi
            r = self.client.put(f"/api/fiber/{fid}",
                                json={"ont_sn": SN_MUTE, "rx_power": -29.0,
                                      "tx_power": 2.0, "mute_alarm": 0},
                                headers=JSON_HDR)
            self.assertEqual(r.status_code, 200)
            r = self.client.get("/api/triggers", headers=XRW_HDR)
            fib = [a for a in r.get_json()
                   if a.get("category") == "fiber" and SN_MUTE in a.get("host", "")]
            self.assertTrue(fib)
            self.assertEqual(fib[0]["severity"], "disaster")
        finally:
            self.client.delete(f"/api/fiber/{fid}", headers=XRW_HDR)

    def test_override_threshold_per_ont(self):
        # rx -21 globalnya normal (< -25 warn), tapi override warn -20 -> warning
        r = self.client.post("/api/fiber",
                             json={"ont_sn": SN_OVERRIDE, "rx_power": -21.0,
                                   "tx_power": 2.0, "rx_warn": -20.0, "rx_crit": -27.0},
                             headers=JSON_HDR)
        self.assertEqual(r.status_code, 201)
        fid = r.get_json()["id"]
        try:
            r = self.client.get("/api/fiber", headers=XRW_HDR)
            item = next(o for o in r.get_json() if o["ont_sn"] == SN_OVERRIDE)
            self.assertEqual(item["calc_status"], "warning")
            # validasi: crit >= warn ditolak
            r = self.client.put(f"/api/fiber/{fid}",
                                json={"ont_sn": SN_OVERRIDE, "rx_power": -21.0,
                                      "rx_warn": -20.0, "rx_crit": -19.0},
                                headers=JSON_HDR)
            self.assertEqual(r.status_code, 400)
        finally:
            self.client.delete(f"/api/fiber/{fid}", headers=XRW_HDR)

    def test_single_check_langsung_set_memory(self):
        # tanpa menunggu scheduler, memory langsung terisi setelah create
        r = self.client.post("/api/fiber",
                             json={"ont_sn": SN_SINGLE, "rx_power": -29.0,
                                   "tx_power": 2.0},
                             headers=JSON_HDR)
        self.assertEqual(r.status_code, 201)
        fid = r.get_json()["id"]
        try:
            self.assertEqual(m.fiber_alarm_memory.get(fid), "critical")
        finally:
            self.client.delete(f"/api/fiber/{fid}", headers=XRW_HDR)


class FiberMaintenanceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = m.app.test_client()
        _cleanup_extra(cls.client)
        r = cls.client.post("/login",
                            data={"username": "admin", "password": "admin12345"})
        assert r.status_code == 302, f"login gagal, status={r.status_code}"

    @classmethod
    def tearDownClass(cls):
        _cleanup_extra(cls.client)

    def test_maintenance_suppress_fiber(self):
        from datetime import datetime, timedelta
        now = datetime.now()
        start = (now - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M")
        end = (now + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M")
        r = self.client.post("/api/fiber",
                             json={"ont_sn": SN_MAINT, "rx_power": -29.0,
                                   "tx_power": 2.0},
                             headers=JSON_HDR)
        self.assertEqual(r.status_code, 201)
        fid = r.get_json()["id"]
        try:
            r = self.client.post("/api/maintenance",
                                 json={"host": SN_MAINT, "start_at": start,
                                       "end_at": end, "reason": "perbaikan jalur"},
                                 headers=JSON_HDR)
            self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
            r = self.client.get("/api/triggers", headers=XRW_HDR)
            alarms = r.get_json()
            fib = [a for a in alarms
                   if a.get("category") == "fiber" and SN_MAINT in a.get("host", "")]
            self.assertEqual(fib, [])
            maint = [a for a in alarms
                     if a.get("category") == "maintenance" and SN_MAINT in a.get("host", "")]
            self.assertTrue(maint)
            # poll tak mengirim telegram: memory tetap terset, tak ada ledakan
            before = dict(m.fiber_alarm_memory)
            m.poll_fiber_monitor()
            self.assertEqual(m.fiber_alarm_memory.get(fid), "critical")
            self.assertIn(fid, before)
        finally:
            conn, c = m.get_db()
            c.execute("DELETE FROM maintenance_windows WHERE host=?", (SN_MAINT,))
            conn.commit()
            conn.close()
            self.client.delete(f"/api/fiber/{fid}", headers=XRW_HDR)


class FiberImportOltTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = m.app.test_client()
        _cleanup_extra(cls.client)
        r = cls.client.post("/login",
                            data={"username": "admin", "password": "admin12345"})
        assert r.status_code == 302, f"login gagal, status={r.status_code}"

    @classmethod
    def tearDownClass(cls):
        _cleanup_extra(cls.client)

    def test_import_csv_duplikat_diskip(self):
        import io as _io
        r = self.client.post("/api/fiber",
                             json={"ont_sn": SN_IMPORT_A, "rx_power": -19.0},
                             headers=JSON_HDR)
        self.assertEqual(r.status_code, 201)
        fid_a = r.get_json()["id"]
        try:
            csv_text = ("ont_sn,customer,olt_name,rx_power,tx_power,source\n"
                        f"{SN_IMPORT_A},duplikat,, -19.0,2.0,manual\n"
                        f"{SN_IMPORT_B},baru,, -26.0,2.0,manual\n")
            r = self.client.post("/api/fiber/import",
                                 data={"file": (_io.BytesIO(csv_text.encode()),
                                                "ont.csv")},
                                 headers=XRW_HDR)
            self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
            j = r.get_json()
            self.assertEqual(j["created"], 1)
            self.assertEqual(j["skipped"], 1)
            r = self.client.get("/api/fiber", headers=XRW_HDR)
            item = next(o for o in r.get_json() if o["ont_sn"] == SN_IMPORT_B)
            self.assertEqual(item["calc_status"], "warning")
            conn, c = m.get_db()
            c.execute("SELECT id FROM fiber_onts WHERE ont_sn=?", (SN_IMPORT_B,))
            fid_b = c.fetchone()["id"]
            conn.close()
            self.client.delete(f"/api/fiber/{fid_b}", headers=XRW_HDR)
        finally:
            self.client.delete(f"/api/fiber/{fid_a}", headers=XRW_HDR)

    def test_settings_target_divalidasi(self):
        r = self.client.get("/api/settings", headers=XRW_HDR)
        orig = r.get_json()
        try:
            r = self.client.post("/api/settings", json={"fiber_rx_target": 0.0},
                                 headers=JSON_HDR)
            self.assertEqual(r.status_code, 400)
        finally:
            self.client.post("/api/settings",
                             json={"fiber_rx_target": orig["fiber_rx_target"]},
                             headers=JSON_HDR)

    def test_olt_crud_dan_poll_snmp(self):
        r = self.client.post("/api/olts",
                             json={"name": OLT_TEST, "ip": "bukan-ip"},
                             headers=JSON_HDR)
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/api/olts",
                             json={"name": OLT_TEST, "ip": "10.99.99.250",
                                   "community": "public", "vendor": "zte",
                                   "rx_base": "1.3.6.1.4.1.3902.1.1",
                                   "tx_base": "1.3.6.1.4.1.3902.1.2",
                                   "div": 100},
                             headers=JSON_HDR)
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        oid = r.get_json()["id"]
        try:
            r = self.client.post("/api/olts", json={"name": OLT_TEST},
                                 headers=JSON_HDR)
            self.assertEqual(r.status_code, 400)
            r = self.client.post("/api/fiber",
                                 json={"ont_sn": SN_SNMP, "olt_name": OLT_TEST,
                                       "ont_index": "7", "source": "snmp"},
                                 headers=JSON_HDR)
            self.assertEqual(r.status_code, 201)
            fid = r.get_json()["id"]
            try:
                real = m._snmp_get
                m._snmp_get = lambda ip, comm, oids, timeout=2.0: [-1995, 210]
                try:
                    m.poll_fiber_snmp()
                finally:
                    m._snmp_get = real
                conn, c = m.get_db()
                c.execute("SELECT rx_power, tx_power FROM fiber_onts WHERE id=?", (fid,))
                row = c.fetchone()
                conn.close()
                self.assertAlmostEqual(row["rx_power"], -19.95)
                self.assertAlmostEqual(row["tx_power"], 2.10)
            finally:
                self.client.delete(f"/api/fiber/{fid}", headers=XRW_HDR)
        finally:
            self.client.delete(f"/api/olts/{oid}", headers=XRW_HDR)


class SchedulerRefTest(unittest.TestCase):
    def test_semua_job_scheduler_terdefinisi(self):
        # Regresi: scheduler aktif saat produksi (NMS_DISABLE_SCHEDULER=0),
        # tapi test mematikannya -> NameError saat deploy tak terdeteksi.
        # Pastikan tiap func=... di blok scheduler ada sebagai callable,
        # dan didefinisikan SEBELUM blok scheduler (aman saat import).
        import re
        src = open(m.__file__).read()
        names = re.findall(r"scheduler\.add_job\(func=([A-Za-z_][A-Za-z0-9_]*)", src)
        self.assertTrue(names)
        sched_pos = src.index("SCHEDULER_ENABLED = ")
        for n in names:
            self.assertTrue(callable(getattr(m, n, None)), f"job {n} tidak terdefinisi")
            self.assertLess(src.index(f"def {n}("), sched_pos,
                            f"job {n} didefinisikan setelah blok scheduler -> NameError produksi")


SN_MUTEEXP = "TEST-FIBER-MUTEEXP-01"
SN_UPSERT = "TEST-FIBER-UPSERT-01"
SN_STALE = "TEST-FIBER-STALE-01"
SN_DEG = "TEST-FIBER-DEG-01"
ODP_H = "TEST-ODP-H-01"
OLT_H = "TEST-OLT-H-01"
SN_HIER = "TEST-FIBER-HIER-01"


def _cleanup_new(client):
    for sn in (SN_MUTEEXP, SN_UPSERT, SN_HIER, SN_STALE, SN_DEG):
        try:
            conn, c = m.get_db()
            try:
                c.execute("SELECT id FROM fiber_onts WHERE ont_sn=?", (sn,))
                row = c.fetchone()
                if row:
                    c.execute("DELETE FROM fiber_history WHERE ont_id=?", (row["id"],))
                    c.execute("DELETE FROM fiber_downtime WHERE ont_id=?", (row["id"],))
                    c.execute("DELETE FROM fiber_onts WHERE id=?", (row["id"],))
                    m.fiber_alarm_memory.pop(row["id"], None)
                c.execute("DELETE FROM maintenance_windows WHERE host=?", (sn,))
                conn.commit()
            finally:
                conn.close()
        except Exception:
            pass
    try:
        conn, c = m.get_db()
        try:
            c.execute("DELETE FROM maintenance_windows WHERE host IN (?, ?)",
                      (f"ODP:{ODP_H}", f"OLT:{OLT_H}"))
            c.execute("DELETE FROM odps WHERE name=?", (ODP_H,))
            c.execute("DELETE FROM olts WHERE name=?", (OLT_H,))
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


class FiberMuteExpiryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = m.app.test_client()
        _cleanup_new(cls.client)
        r = cls.client.post("/login",
                            data={"username": "admin", "password": "admin12345"})
        assert r.status_code == 302, f"login gagal, status={r.status_code}"

    @classmethod
    def tearDownClass(cls):
        _cleanup_new(cls.client)

    def test_is_mute_active_helper(self):
        self.assertFalse(m._is_mute_active({"mute_alarm": 0}))
        self.assertTrue(m._is_mute_active({"mute_alarm": 1, "mute_until": ""}))
        self.assertTrue(m._is_mute_active({"mute_alarm": 1, "mute_until": None}))
        from datetime import datetime, timedelta
        fut = (datetime.now() + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M")
        past = (datetime.now() - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M")
        self.assertTrue(m._is_mute_active({"mute_alarm": 1, "mute_until": fut}))
        self.assertFalse(m._is_mute_active({"mute_alarm": 1, "mute_until": past}))

    def test_mute_kedaluawarsa_muncul_lagi_di_triggers(self):
        from datetime import datetime, timedelta
        fut = (datetime.now() + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M")
        past = (datetime.now() - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M")
        r = self.client.post("/api/fiber",
                             json={"ont_sn": SN_MUTEEXP, "rx_power": -29.0,
                                   "tx_power": 2.0, "mute_alarm": 1,
                                   "mute_until": fut, "mute_reason": "tunggu teknisi"},
                             headers=JSON_HDR)
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        fid = r.get_json()["id"]
        try:
            r = self.client.get("/api/fiber", headers=XRW_HDR)
            item = next(o for o in r.get_json() if o["ont_sn"] == SN_MUTEEXP)
            self.assertTrue(item["mute_active"])
            r = self.client.get("/api/triggers", headers=XRW_HDR)
            fib = [a for a in r.get_json()
                   if a.get("category") == "fiber" and SN_MUTEEXP in a.get("host", "")]
            self.assertEqual(fib, [])
            # kedaluawarsa -> alarm aktif lagi
            r = self.client.put(f"/api/fiber/{fid}",
                                json={"ont_sn": SN_MUTEEXP, "rx_power": -29.0,
                                      "tx_power": 2.0, "mute_alarm": 1,
                                      "mute_until": past},
                                headers=JSON_HDR)
            self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
            r = self.client.get("/api/fiber", headers=XRW_HDR)
            item = next(o for o in r.get_json() if o["ont_sn"] == SN_MUTEEXP)
            self.assertFalse(item["mute_active"])
            self.assertEqual(item["mute_alarm"], 1)
            r = self.client.get("/api/triggers", headers=XRW_HDR)
            fib = [a for a in r.get_json()
                   if a.get("category") == "fiber" and SN_MUTEEXP in a.get("host", "")]
            self.assertTrue(fib)
            self.assertEqual(fib[0]["severity"], "disaster")
        finally:
            self.client.delete(f"/api/fiber/{fid}", headers=XRW_HDR)

    def test_mute_until_format_invalid_ditolak(self):
        r = self.client.post("/api/fiber",
                             json={"ont_sn": SN_MUTEEXP, "rx_power": -19.0,
                                   "mute_alarm": 1, "mute_until": "kapan-kapan"},
                             headers=JSON_HDR)
        self.assertEqual(r.status_code, 400)


class FiberHierarchyMaintenanceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = m.app.test_client()
        _cleanup_new(cls.client)
        r = cls.client.post("/login",
                            data={"username": "admin", "password": "admin12345"})
        assert r.status_code == 302, f"login gagal, status={r.status_code}"
        r = cls.client.post("/api/odps", json={"name": ODP_H, "capacity": 8},
                            headers=JSON_HDR)
        assert r.status_code == 201, r.get_data(as_text=True)
        r = cls.client.post("/api/olts",
                            json={"name": OLT_H, "ip": "10.99.99.251",
                                  "community": "public"},
                            headers=JSON_HDR)
        assert r.status_code == 201, r.get_data(as_text=True)

    @classmethod
    def tearDownClass(cls):
        _cleanup_new(cls.client)

    def _window(self):
        from datetime import datetime, timedelta
        now = datetime.now()
        return ((now - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M"),
                (now + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M"))

    def test_maintenance_odp_mensuppress_ont(self):
        start, end = self._window()
        r = self.client.post("/api/fiber",
                             json={"ont_sn": SN_HIER, "rx_power": -29.0,
                                   "tx_power": 2.0, "odp_name": ODP_H,
                                   "olt_name": OLT_H},
                             headers=JSON_HDR)
        self.assertEqual(r.status_code, 201)
        fid = r.get_json()["id"]
        try:
            r = self.client.post("/api/maintenance",
                                 json={"host": f"ODP:{ODP_H}", "start_at": start,
                                       "end_at": end, "reason": "ganti splitter"},
                                 headers=JSON_HDR)
            self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
            mid = r.get_json()["id"]
            try:
                r = self.client.get("/api/triggers", headers=XRW_HDR)
                alarms = r.get_json()
                fib = [a for a in alarms
                       if a.get("category") == "fiber" and SN_HIER in a.get("host", "")]
                self.assertEqual(fib, [])
                maint = [a for a in alarms
                         if a.get("category") == "maintenance" and SN_HIER in a.get("host", "")]
                self.assertTrue(maint)
                self.assertIn("ODP", maint[0]["message"])
            finally:
                self.client.delete(f"/api/maintenance/{mid}", headers=XRW_HDR)
        finally:
            self.client.delete(f"/api/fiber/{fid}", headers=XRW_HDR)

    def test_maintenance_olt_mensuppress_ont(self):
        start, end = self._window()
        r = self.client.post("/api/fiber",
                             json={"ont_sn": SN_HIER, "rx_power": -29.0,
                                   "olt_name": OLT_H},
                             headers=JSON_HDR)
        self.assertEqual(r.status_code, 201)
        fid = r.get_json()["id"]
        try:
            r = self.client.post("/api/maintenance",
                                 json={"host": f"olt:{OLT_H.lower()}", "start_at": start,
                                       "end_at": end, "reason": "upgrade firmware"},
                                 headers=JSON_HDR)
            self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
            mid = r.get_json()["id"]
            try:
                r = self.client.get("/api/triggers", headers=XRW_HDR)
                alarms = r.get_json()
                fib = [a for a in alarms
                       if a.get("category") == "fiber" and SN_HIER in a.get("host", "")]
                self.assertEqual(fib, [])
            finally:
                self.client.delete(f"/api/maintenance/{mid}", headers=XRW_HDR)
        finally:
            self.client.delete(f"/api/fiber/{fid}", headers=XRW_HDR)

    def test_maintenance_odp_tidak_terdaftar_ditolak(self):
        start, end = self._window()
        r = self.client.post("/api/maintenance",
                             json={"host": "ODP:TIDAK-ADA-XYZ", "start_at": start,
                                   "end_at": end},
                             headers=JSON_HDR)
        self.assertEqual(r.status_code, 404)


class FiberImportUpsertTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = m.app.test_client()
        _cleanup_new(cls.client)
        r = cls.client.post("/login",
                            data={"username": "admin", "password": "admin12345"})
        assert r.status_code == 302, f"login gagal, status={r.status_code}"

    @classmethod
    def tearDownClass(cls):
        _cleanup_new(cls.client)

    def test_upsert_update_rx_tanpa_hapus_customer(self):
        import io as _io
        r = self.client.post("/api/fiber",
                             json={"ont_sn": SN_UPSERT, "customer": "Bpk Uji",
                                   "rx_power": -19.0, "tx_power": 2.0,
                                   "source": "manual"},
                             headers=JSON_HDR)
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        fid = r.get_json()["id"]
        try:
            conn, c = m.get_db()
            before = c.execute("SELECT COUNT(*) FROM fiber_history WHERE ont_id=?",
                               (fid,)).fetchone()[0]
            conn.close()
            # CSV hanya berisi rx baru (tanpa customer/odp) -> merge, bukan wipe
            csv_text = ("ont_sn,rx_power,tx_power,source\n"
                        f"{SN_UPSERT},-26.0,2.0,manual\n")
            r = self.client.post("/api/fiber/import?mode=upsert",
                                 data={"file": (_io.BytesIO(csv_text.encode()), "ont.csv")},
                                 headers=XRW_HDR)
            self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
            j = r.get_json()
            self.assertEqual(j["created"], 0)
            self.assertEqual(j["updated"], 1)
            self.assertEqual(j["skipped"], 0)
            r = self.client.get("/api/fiber", headers=XRW_HDR)
            item = next(o for o in r.get_json() if o["ont_sn"] == SN_UPSERT)
            self.assertAlmostEqual(item["rx_power"], -26.0)
            self.assertEqual(item["calc_status"], "warning")
            self.assertEqual(item["customer"], "Bpk Uji")
            conn, c = m.get_db()
            after = c.execute("SELECT COUNT(*) FROM fiber_history WHERE ont_id=?",
                              (fid,)).fetchone()[0]
            conn.close()
            self.assertEqual(after, before + 1)
            # mode default tetap skip
            r = self.client.post("/api/fiber/import",
                                 data={"file": (_io.BytesIO(csv_text.encode()), "ont.csv")},
                                 headers=XRW_HDR)
            self.assertEqual(r.status_code, 200)
            j = r.get_json()
            self.assertEqual(j["skipped"], 1)
            self.assertEqual(j.get("updated", 0), 0)
            # mode invalid ditolak
            r = self.client.post("/api/fiber/import?mode=bogus",
                                 data={"file": (_io.BytesIO(csv_text.encode()), "ont.csv")},
                                 headers=XRW_HDR)
            self.assertEqual(r.status_code, 400)
        finally:
            self.client.delete(f"/api/fiber/{fid}", headers=XRW_HDR)


class FiberStaleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = m.app.test_client()
        _cleanup_new(cls.client)
        r = cls.client.post("/login",
                            data={"username": "admin", "password": "admin12345"})
        assert r.status_code == 302, f"login gagal, status={r.status_code}"

    @classmethod
    def tearDownClass(cls):
        _cleanup_new(cls.client)

    def _age_last_seen(self, sn, hours):
        from datetime import datetime, timedelta
        old = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
        conn, c = m.get_db()
        c.execute("UPDATE fiber_onts SET last_seen=? WHERE ont_sn=?", (old, sn))
        conn.commit()
        conn.close()

    def test_stale_info_helper(self):
        from datetime import datetime, timedelta
        old = (datetime.now() - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")
        fresh = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # manual tak pernah stale walau data lama
        self.assertEqual(m._fiber_stale_info(
            {"source": "manual", "rx_power": -19.0, "last_seen": old})[0], False)
        # snmp segar -> tidak stale
        self.assertEqual(m._fiber_stale_info(
            {"source": "snmp", "rx_power": -19.0, "last_seen": fresh})[0], False)
        # snmp 3 jam (ambang default 60 mnt) -> stale
        is_stale, age = m._fiber_stale_info(
            {"source": "snmp", "rx_power": -19.0, "last_seen": old})
        self.assertTrue(is_stale)
        self.assertIn("jam", age)
        # tanpa pengukuran / tanpa last_seen -> bukan stale
        self.assertEqual(m._fiber_stale_info(
            {"source": "snmp", "rx_power": None, "tx_power": None,
             "last_seen": old})[0], False)
        self.assertEqual(m._fiber_stale_info(
            {"source": "snmp", "rx_power": -19.0, "last_seen": ""})[0], False)

    def test_stale_overlay_di_list_dan_triggers(self):
        r = self.client.post("/api/fiber",
                             json={"ont_sn": SN_STALE, "rx_power": -19.0,
                                   "tx_power": 2.0, "source": "snmp",
                                   "ont_index": "9"},
                             headers=JSON_HDR)
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        fid = r.get_json()["id"]
        try:
            r = self.client.get("/api/fiber", headers=XRW_HDR)
            item = next(o for o in r.get_json() if o["ont_sn"] == SN_STALE)
            self.assertEqual(item["calc_status"], "normal")
            self.assertFalse(item["stale"])
            self._age_last_seen(SN_STALE, 3)
            r = self.client.get("/api/fiber", headers=XRW_HDR)
            item = next(o for o in r.get_json() if o["ont_sn"] == SN_STALE)
            self.assertEqual(item["calc_status"], "stale")
            self.assertTrue(item["stale"])
            self.assertIn("LOS", item["advice"])
            r = self.client.get("/api/triggers", headers=XRW_HDR)
            stale = [a for a in r.get_json()
                     if a.get("category") == "fiber" and SN_STALE in a.get("host", "")]
            self.assertTrue(stale)
            self.assertEqual(stale[0]["severity"], "warning")
            self.assertIn("STALE", stale[0]["message"])
            # data segar masuk lagi -> kembali normal
            r = self.client.put(f"/api/fiber/{fid}",
                                json={"ont_sn": SN_STALE, "rx_power": -19.0,
                                      "tx_power": 2.0, "source": "snmp",
                                      "ont_index": "9"},
                                headers=JSON_HDR)
            self.assertEqual(r.status_code, 200)
            r = self.client.get("/api/fiber", headers=XRW_HDR)
            item = next(o for o in r.get_json() if o["ont_sn"] == SN_STALE)
            self.assertEqual(item["calc_status"], "normal")
            self.assertFalse(item["stale"])
        finally:
            self.client.delete(f"/api/fiber/{fid}", headers=XRW_HDR)

    def test_settings_stale_dan_degrade_divalidasi(self):
        r = self.client.get("/api/settings", headers=XRW_HDR)
        orig = r.get_json()
        try:
            r = self.client.post("/api/settings", json={"fiber_degrade_db": 0.1},
                                 headers=JSON_HDR)
            self.assertEqual(r.status_code, 400)
            r = self.client.post("/api/settings", json={"fiber_degrade_days": 99},
                                 headers=JSON_HDR)
            self.assertEqual(r.status_code, 400)
            r = self.client.post("/api/settings", json={"fiber_stale_min": 5},
                                 headers=JSON_HDR)
            self.assertEqual(r.status_code, 400)
            r = self.client.post("/api/settings",
                                 json={"fiber_degrade_db": 2.5, "fiber_degrade_days": 5,
                                       "fiber_stale_min": 120},
                                 headers=JSON_HDR)
            self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
            r = self.client.get("/api/settings", headers=XRW_HDR)
            j = r.get_json()
            self.assertAlmostEqual(j["fiber_degrade_db"], 2.5)
            self.assertEqual(int(j["fiber_degrade_days"]), 5)
            self.assertEqual(int(j["fiber_stale_min"]), 120)
        finally:
            self.client.post("/api/settings",
                             json={k: orig[k] for k in
                                   ("fiber_degrade_db", "fiber_degrade_days",
                                    "fiber_stale_min")},
                             headers=JSON_HDR)


class FiberDegradationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = m.app.test_client()
        _cleanup_new(cls.client)
        r = cls.client.post("/login",
                            data={"username": "admin", "password": "admin12345"})
        assert r.status_code == 302, f"login gagal, status={r.status_code}"

    @classmethod
    def tearDownClass(cls):
        _cleanup_new(cls.client)
        m.fiber_degrade_memory.pop("x", None)

    def test_degradasi_terdeteksi_dan_masuk_triggers(self):
        from datetime import datetime, timedelta
        r = self.client.get("/api/settings", headers=XRW_HDR)
        orig = r.get_json()
        self.client.post("/api/settings",
                         json={"fiber_degrade_db": 3.0, "fiber_degrade_days": 7},
                         headers=JSON_HDR)
        try:
            r = self.client.post("/api/fiber",
                                 json={"ont_sn": SN_DEG, "rx_power": -16.0,
                                       "tx_power": 2.0, "source": "manual"},
                                 headers=JSON_HDR)
            self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
            fid = r.get_json()["id"]
            try:
                # history baru saja -> rentang < 24 jam -> belum dinilai
                degr, drop = m._fiber_degradation(fid, -16.0)
                self.assertFalse(degr)
                # tanam titik lama: 6 hari lalu Rx -16, kini -20 (turun 4 dB)
                old_ts = (datetime.now() - timedelta(days=6)).strftime("%Y-%m-%d %H:%M:%S")
                conn, c = m.get_db()
                c.execute("INSERT INTO fiber_history (ont_id, rx_power, tx_power, timestamp)"
                          " VALUES (?, ?, ?, ?)", (fid, -16.0, 2.0, old_ts))
                conn.commit()
                conn.close()
                r = self.client.put(f"/api/fiber/{fid}",
                                    json={"ont_sn": SN_DEG, "rx_power": -20.0,
                                          "tx_power": 2.0, "source": "manual"},
                                    headers=JSON_HDR)
                self.assertEqual(r.status_code, 200)
                degr, drop = m._fiber_degradation(fid, -20.0)
                self.assertTrue(degr)
                self.assertAlmostEqual(drop, 4.0)
                # poll mengisi memory -> list & triggers menampilkan
                m.poll_fiber_monitor()
                mem = m.fiber_degrade_memory.get(fid) or {}
                self.assertTrue(mem.get("degrading"))
                r = self.client.get("/api/fiber", headers=XRW_HDR)
                item = next(o for o in r.get_json() if o["ont_sn"] == SN_DEG)
                self.assertEqual(item["calc_status"], "normal")
                self.assertTrue(item["degrading"])
                self.assertAlmostEqual(item["degrade_drop_db"], 4.0)
                r = self.client.get("/api/triggers", headers=XRW_HDR)
                deg = [a for a in r.get_json()
                       if a.get("category") == "fiber" and SN_DEG in a.get("host", "")
                       and "DEGRADASI" in a.get("message", "")]
                self.assertTrue(deg)
                self.assertEqual(deg[0]["severity"], "warning")
            finally:
                self.client.delete(f"/api/fiber/{fid}", headers=XRW_HDR)
                m.fiber_degrade_memory.pop(fid, None)
        finally:
            self.client.post("/api/settings",
                             json={"fiber_degrade_db": orig["fiber_degrade_db"],
                                   "fiber_degrade_days": orig["fiber_degrade_days"]},
                             headers=JSON_HDR)


OLT_P = "TEST-OLT-PRESET"
OLT_P2 = "TEST-OLT-P2"
OLT_T = "TEST-OLT-T"
OLT_D = "TEST-OLT-D"


def _cleanup_olt_p(client):
    try:
        conn, c = m.get_db()
        try:
            for _olt in (OLT_P, OLT_P2, OLT_T, OLT_D):
                c.execute("SELECT id FROM fiber_onts WHERE olt_name COLLATE NOCASE = ?", (_olt,))
                for r in c.fetchall():
                    c.execute("DELETE FROM fiber_history WHERE ont_id=?", (r["id"],))
                    c.execute("DELETE FROM fiber_downtime WHERE ont_id=?", (r["id"],))
                    m.fiber_alarm_memory.pop(r["id"], None)
                    m.fiber_degrade_memory.pop(r["id"], None)
                c.execute("DELETE FROM fiber_onts WHERE olt_name COLLATE NOCASE = ?", (_olt,))
                c.execute("DELETE FROM olts WHERE name=?", (_olt,))
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


class FiberOltPresetTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = m.app.test_client()
        _cleanup_olt_p(cls.client)
        r = cls.client.post("/login",
                            data={"username": "admin", "password": "admin12345"})
        assert r.status_code == 302, f"login gagal, status={r.status_code}"

    @classmethod
    def tearDownClass(cls):
        _cleanup_olt_p(cls.client)

    def _get_olt(self, name):
        r = self.client.get("/api/olts", headers=XRW_HDR)
        return next(o for o in r.get_json() if o["name"] == name)

    def test_presets_endpoint(self):
        r = self.client.get("/api/olts/presets", headers=XRW_HDR)
        self.assertEqual(r.status_code, 200)
        j = r.get_json()
        for v in ("zte", "huawei", "generic"):
            self.assertIn(v, j)
        self.assertIn("3902.1012.3.50.12.1.1.10", j["zte"]["rx_base"])
        self.assertAlmostEqual(j["zte"]["scale"], 0.002)
        self.assertAlmostEqual(j["zte"]["offset"], -30.0)
        self.assertIn("2011.6.128.1.1.2.51.1.4", j["huawei"]["rx_base"])
        self.assertAlmostEqual(j["huawei"]["scale"], 0.01)
        self.assertAlmostEqual(j["huawei"]["offset"], -100.0)

    def test_transform_helper(self):
        self.assertAlmostEqual(
            m._olt_raw_to_dbm(5000, {"div": 1.0, "scale": 0.002, "offset": -30.0}), -20.0)
        self.assertAlmostEqual(
            m._olt_raw_to_dbm(7500, {"div": 1.0, "scale": 0.01, "offset": -100.0}), -25.0)
        self.assertAlmostEqual(m._olt_raw_to_dbm(-1995, {"div": 100.0}), -19.95)
        self.assertIsNone(m._olt_raw_to_dbm("bukan-angka", {"div": 100.0}))

    def test_create_auto_preset_dan_validasi(self):
        r = self.client.post("/api/olts",
                             json={"name": OLT_P, "ip": "10.99.99.252",
                                   "community": "public", "vendor": "zte"},
                             headers=JSON_HDR)
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        o = self._get_olt(OLT_P)
        self.assertIn("3902.1012.3.50.12.1.1.10", o["rx_base"])
        self.assertAlmostEqual(o["scale"], 0.002)
        self.assertAlmostEqual(o["offset"], -30.0)
        # kustomisasi eksplisit tak tertimpa preset
        r = self.client.post("/api/olts",
                             json={"name": OLT_P2, "ip": "10.99.99.253",
                                   "community": "public", "vendor": "zte",
                                   "rx_base": "1.3.6.1.4.1.1", "div": 100},
                             headers=JSON_HDR)
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        o = self._get_olt(OLT_P2)
        self.assertEqual(o["rx_base"], "1.3.6.1.4.1.1")
        self.assertAlmostEqual(o["scale"], 1.0)
        # validasi scale/offset
        r = self.client.post("/api/olts",
                             json={"name": "TEST-OLT-X", "vendor": "zte",
                                   "scale": 0},
                             headers=JSON_HDR)
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/api/olts",
                             json={"name": "TEST-OLT-X", "vendor": "zte",
                                   "offset": 99999},
                             headers=JSON_HDR)
        self.assertEqual(r.status_code, 400)

    def test_olt_test_endpoint(self):
        r = self.client.post("/api/olts",
                             json={"name": OLT_T, "ip": "10.99.99.253",
                                   "community": "public", "vendor": "generic",
                                   "rx_base": "1.3.6.1.4.1.9999.1",
                                   "tx_base": "1.3.6.1.4.1.9999.2",
                                   "div": 100},
                             headers=JSON_HDR)
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        oid = r.get_json()["id"]
        real_get, real_next = m._snmp_get, m._snmp_getnext
        try:
            m._snmp_get = lambda ip, comm, oids, timeout=2.0: [360000]
            m._snmp_getnext = lambda ip, comm, base, timeout=2.5: (
                (base + ".7", 0x02, -1995, None) if base.endswith(".1")
                else (base + ".7", 0x02, 210, None))
            r = self.client.post(f"/api/olts/{oid}/test", headers=XRW_HDR)
            self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
            j = r.get_json()
            self.assertTrue(j["reachable"])
            self.assertTrue(j["ok"])
            self.assertTrue(j["rx"]["ok"])
            self.assertEqual(j["rx"]["index"], "7")
            self.assertAlmostEqual(j["rx"]["dbm"], -19.95)
            self.assertAlmostEqual(j["tx"]["dbm"], 2.10)
            o = self._get_olt(OLT_T)
            self.assertEqual(o["last_test_ok"], 1)
            # OLT mati -> ok False
            m._snmp_get = lambda ip, comm, oids, timeout=2.0: [None]
            r = self.client.post(f"/api/olts/{oid}/test", headers=XRW_HDR)
            self.assertEqual(r.status_code, 200)
            self.assertFalse(r.get_json()["ok"])
            r = self.client.post("/api/olts/999999/test", headers=XRW_HDR)
            self.assertEqual(r.status_code, 404)
        finally:
            m._snmp_get, m._snmp_getnext = real_get, real_next

    def test_discover_bulk_upsert(self):
        r = self.client.post("/api/olts",
                             json={"name": OLT_D, "ip": "10.99.99.252",
                                   "community": "public", "vendor": "generic",
                                   "rx_base": "1.3.6.1.4.1.9999.1",
                                   "tx_base": "1.3.6.1.4.1.9999.2",
                                   "div": 100},
                             headers=JSON_HDR)
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        oid = r.get_json()["id"]
        # baris manual dengan index sama -> harus dilewati, tak ditimpa
        r = self.client.post("/api/fiber",
                             json={"ont_sn": "TEST-MANUAL-DISC", "customer": "Jangan Timpa",
                                   "olt_name": OLT_D, "ont_index": "1.1",
                                   "rx_power": -18.0, "source": "manual"},
                             headers=JSON_HDR)
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        real_get, real_walk = m._snmp_get, m.snmp_walk
        base = "1.3.6.1.4.1.9999.1"
        try:
            m.snmp_walk = lambda ip, comm, b, max_rows=64: [
                (base + ".1.1", 0x02, -1995, None),
                (base + ".1.2", 0x02, -2600, None),
            ]

            def _fake_get(ip, comm, oids, timeout=2.0):
                if oids == [m.SYSUP_OID]:
                    return [360000]
                return [210 if o.endswith(".1.1") else 215 for o in oids]

            m._snmp_get = _fake_get
            r = self.client.post(f"/api/olts/{oid}/discover",
                                 json={"limit": 10}, headers=JSON_HDR)
            self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
            j = r.get_json()
            self.assertEqual(j["walked"], 2)
            self.assertEqual(j["created"], 1)
            self.assertEqual(j["skipped_manual"], 1)
            r = self.client.get("/api/fiber", headers=XRW_HDR)
            onts = [o for o in r.get_json() if o.get("olt_name") == OLT_D]
            auto = [o for o in onts if o.get("ont_index") == "1.2"]
            self.assertTrue(auto)
            self.assertEqual(auto[0]["source"], "snmp")
            self.assertAlmostEqual(auto[0]["rx_power"], -26.0)
            self.assertEqual(auto[0]["calc_status"], "warning")
            man = next(o for o in onts if o["ont_sn"] == "TEST-MANUAL-DISC")
            self.assertEqual(man["customer"], "Jangan Timpa")
            self.assertAlmostEqual(man["rx_power"], -18.0)
            # discover kedua -> update, bukan create
            r = self.client.post(f"/api/olts/{oid}/discover",
                                 json={"limit": 10}, headers=JSON_HDR)
            j = r.get_json()
            self.assertEqual(j["created"], 0)
            self.assertEqual(j["updated"], 1)
        finally:
            m._snmp_get, m.snmp_walk = real_get, real_walk
            try:
                conn, c = m.get_db()
                c.execute("SELECT id FROM fiber_onts WHERE ont_sn=?", ("TEST-MANUAL-DISC",))
                row = c.fetchone()
                if row:
                    c.execute("DELETE FROM fiber_history WHERE ont_id=?", (row["id"],))
                    c.execute("DELETE FROM fiber_downtime WHERE ont_id=?", (row["id"],))
                    c.execute("DELETE FROM fiber_onts WHERE id=?", (row["id"],))
                    m.fiber_alarm_memory.pop(row["id"], None)
                conn.commit()
                conn.close()
            except Exception:
                pass


SN_PG_A = "TEST-FIBER-PG-A"
SN_PG_B = "TEST-FIBER-PG-B"
SN_PG_C = "TEST-FIBER-PG-C"
SN_SUM_A = "TEST-FIBER-SUM-A"
SN_SUM_B = "TEST-FIBER-SUM-B"
SN_SUM_M = "TEST-FIBER-SUM-M"
OLT_PG = "TEST-OLT-PG"


def _cleanup_pg(client):
    for sn in (SN_PG_A, SN_PG_B, SN_PG_C, SN_SUM_A, SN_SUM_B, SN_SUM_M):
        try:
            conn, c = m.get_db()
            try:
                c.execute("SELECT id FROM fiber_onts WHERE ont_sn=?", (sn,))
                row = c.fetchone()
                if row:
                    c.execute("DELETE FROM fiber_history WHERE ont_id=?", (row["id"],))
                    c.execute("DELETE FROM fiber_downtime WHERE ont_id=?", (row["id"],))
                    c.execute("DELETE FROM fiber_onts WHERE id=?", (row["id"],))
                    m.fiber_alarm_memory.pop(row["id"], None)
                    m.fiber_degrade_memory.pop(row["id"], None)
                conn.commit()
            finally:
                conn.close()
        except Exception:
            pass


class FiberPagingSummaryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = m.app.test_client()
        _cleanup_pg(cls.client)
        r = cls.client.post("/login",
                            data={"username": "admin", "password": "admin12345"})
        assert r.status_code == 302, f"login gagal, status={r.status_code}"
        for sn, rx in ((SN_PG_A, -19.0), (SN_PG_B, -26.0), (SN_PG_C, -29.0)):
            r = cls.client.post("/api/fiber",
                                json={"ont_sn": sn, "customer": "uji paging",
                                      "olt_name": OLT_PG, "rx_power": rx,
                                      "tx_power": 2.0, "source": "manual"},
                                headers=JSON_HDR)
            assert r.status_code == 201, r.get_data(as_text=True)

    @classmethod
    def tearDownClass(cls):
        _cleanup_pg(cls.client)

    def test_legacy_tanpa_param_tetap_array(self):
        r = self.client.get("/api/fiber", headers=XRW_HDR)
        self.assertEqual(r.status_code, 200)
        j = r.get_json()
        self.assertIsInstance(j, list)
        sns = {o["ont_sn"] for o in j}
        self.assertTrue({SN_PG_A, SN_PG_B, SN_PG_C} <= sns)

    def test_paginasi_dan_sort(self):
        r = self.client.get("/api/fiber?page=1&per_page=2", headers=XRW_HDR)
        self.assertEqual(r.status_code, 200)
        j = r.get_json()
        self.assertEqual(len(j["items"]), 2)
        self.assertGreaterEqual(j["total"], 3)
        self.assertEqual(j["page"], 1)
        r = self.client.get("/api/fiber?page=2&per_page=2", headers=XRW_HDR)
        self.assertEqual(r.get_json()["page"], 2)
        # sort rx terburuk dulu
        r = self.client.get(f"/api/fiber?sort=rx_asc&olt={OLT_PG}&per_page=50",
                            headers=XRW_HDR)
        items = r.get_json()["items"]
        self.assertEqual([o["ont_sn"] for o in items], [SN_PG_C, SN_PG_B, SN_PG_A])
        r = self.client.get(f"/api/fiber?sort=rx_desc&olt={OLT_PG}&per_page=50",
                            headers=XRW_HDR)
        items = r.get_json()["items"]
        self.assertEqual([o["ont_sn"] for o in items], [SN_PG_A, SN_PG_B, SN_PG_C])
        r = self.client.get("/api/fiber?sort=bogus", headers=XRW_HDR)
        self.assertEqual(r.status_code, 400)
        r = self.client.get("/api/fiber?page=bogus", headers=XRW_HDR)
        self.assertEqual(r.status_code, 400)

    def test_filter_status_q_olt(self):
        r = self.client.get("/api/fiber?status=critical&per_page=50", headers=XRW_HDR)
        items = r.get_json()["items"]
        self.assertTrue(all(o["calc_status"] == "critical" for o in items))
        self.assertIn(SN_PG_C, {o["ont_sn"] for o in items})
        r = self.client.get("/api/fiber?q=pg-b&per_page=50", headers=XRW_HDR)
        items = r.get_json()["items"]
        self.assertEqual({o["ont_sn"] for o in items}, {SN_PG_B})
        r = self.client.get(f"/api/fiber?olt={OLT_PG}&per_page=50", headers=XRW_HDR)
        items = r.get_json()["items"]
        self.assertEqual({o["ont_sn"] for o in items}, {SN_PG_A, SN_PG_B, SN_PG_C})

    def test_summary(self):
        r = self.client.get("/api/fiber/summary", headers=XRW_HDR)
        self.assertEqual(r.status_code, 200)
        j = r.get_json()
        self.assertGreaterEqual(j["counts"]["total"], 3)
        self.assertGreaterEqual(j["counts"]["critical"], 1)
        sns = [o["ont_sn"] for o in j["worst_rx"]]
        self.assertIn(SN_PG_C, sns)
        self.assertLess(sns.index(SN_PG_C), sns.index(SN_PG_A) if SN_PG_A in sns else len(sns))
        self.assertIn(OLT_PG, j["olt_names"])
        self.assertTrue(any(o["ont_sn"] == SN_PG_A for o in j["ont_options"]))

    def test_history_export_dan_range_720(self):
        conn, c = m.get_db()
        c.execute("SELECT id FROM fiber_onts WHERE ont_sn=?", (SN_PG_A,))
        fid = c.fetchone()["id"]
        conn.close()
        r = self.client.get(f"/api/fiber/{fid}/history/export?hours=24", headers=XRW_HDR)
        self.assertEqual(r.status_code, 200)
        body = r.get_data(as_text=True)
        self.assertIn("timestamp,rx_dbm,tx_dbm", body)
        self.assertGreaterEqual(len(body.strip().splitlines()), 2)
        r = self.client.get("/api/fiber/999999/history/export", headers=XRW_HDR)
        self.assertEqual(r.status_code, 404)
        r = self.client.get(f"/api/fiber/{fid}/history?hours=720", headers=XRW_HDR)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["hours"], 720)


class FiberDailySummaryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = m.app.test_client()
        _cleanup_pg(cls.client)
        r = cls.client.post("/login",
                            data={"username": "admin", "password": "admin12345"})
        assert r.status_code == 302, f"login gagal, status={r.status_code}"

    @classmethod
    def tearDownClass(cls):
        _cleanup_pg(cls.client)

    def test_tanpa_ont_tidak_kirim(self):
        from unittest import mock as _mock

        class _EmptyC:
            def execute(self, *a, **k):
                return self

            def fetchall(self):
                return []

        class _EmptyConn:
            def close(self):
                pass

        sent = []
        real = m.send_telegram_alert
        m.send_telegram_alert = lambda msg: sent.append(msg)
        try:
            with _mock.patch.object(m, "get_db",
                                    return_value=(_EmptyConn(), _EmptyC())):
                m.send_fiber_summary()
        finally:
            m.send_telegram_alert = real
        self.assertEqual(sent, [])

    def test_isi_laporan(self):
        for sn, rx, extra in ((SN_SUM_A, -29.0, {}),
                              (SN_SUM_B, -19.0, {}),
                              (SN_SUM_M, -30.0, {"mute_alarm": 1})):
            body = {"ont_sn": sn, "customer": "uji ringkasan",
                    "rx_power": rx, "tx_power": 2.0, "source": "manual"}
            body.update(extra)
            r = self.client.post("/api/fiber", json=body, headers=JSON_HDR)
            self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        conn, c = m.get_db()
        c.execute("SELECT id FROM fiber_onts WHERE ont_sn=?", (SN_SUM_B,))
        fid_b = c.fetchone()["id"]
        conn.close()
        m.fiber_degrade_memory[fid_b] = {"degrading": True, "drop_db": 3.5}
        sent = []
        real = m.send_telegram_alert
        m.send_telegram_alert = lambda msg: sent.append(msg)
        try:
            m.send_fiber_summary()
        finally:
            m.send_telegram_alert = real
            m.fiber_degrade_memory.pop(fid_b, None)
        self.assertEqual(len(sent), 1)
        msg = sent[0]
        self.assertIn("Laporan Harian Fiber", msg)
        self.assertIn("Total 3 ONT", msg)
        self.assertIn(SN_SUM_A, msg)
        self.assertIn("CRITICAL", msg)
        # muted dihitung tapi tak masuk daftar perhatian
        self.assertIn("mute 1", msg)
        self.assertNotIn(SN_SUM_M, msg)
        # normal + degradasi -> masuk seksi degradasi dini
        self.assertIn(SN_SUM_B, msg)
        self.assertIn("3.5 dB", msg)


SN_DT_A = "TEST-FIBER-DT-A"
SN_DT_B = "TEST-FIBER-DT-B"
SN_DT_W = "TEST-FIBER-DT-W"
SN_DT_C = "TEST-FIBER-DT-C"
SN_FLAP = "TEST-FIBER-FLAP-01"


def _cleanup_dt(client):
    for sn in (SN_DT_A, SN_DT_B, SN_DT_W, SN_DT_C, SN_FLAP):
        try:
            conn, c = m.get_db()
            try:
                c.execute("SELECT id FROM fiber_onts WHERE ont_sn=?", (sn,))
                row = c.fetchone()
                if row:
                    c.execute("DELETE FROM fiber_history WHERE ont_id=?", (row["id"],))
                    c.execute("DELETE FROM fiber_downtime WHERE ont_id=?", (row["id"],))
                    c.execute("DELETE FROM fiber_onts WHERE id=?", (row["id"],))
                    m.fiber_alarm_memory.pop(row["id"], None)
                    m.fiber_degrade_memory.pop(row["id"], None)
                else:
                    c.execute("DELETE FROM fiber_downtime WHERE ont_sn=?", (sn,))
                conn.commit()
            finally:
                conn.close()
        except Exception:
            pass


class FiberDowntimeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = m.app.test_client()
        _cleanup_dt(cls.client)
        r = cls.client.post("/login",
                            data={"username": "admin", "password": "admin12345"})
        assert r.status_code == 302, f"login gagal, status={r.status_code}"

    @classmethod
    def tearDownClass(cls):
        _cleanup_dt(cls.client)

    def _downtime(self, fid):
        r = self.client.get(f"/api/fiber/{fid}/downtime", headers=XRW_HDR)
        self.assertEqual(r.status_code, 200)
        return r.get_json()

    def test_open_langsung_saat_create_critical(self):
        r = self.client.post("/api/fiber",
                             json={"ont_sn": SN_DT_A, "rx_power": -29.0,
                                   "tx_power": 2.0, "source": "manual"},
                             headers=JSON_HDR)
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        fid = r.get_json()["id"]
        try:
            rows = self._downtime(fid)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["state"], "ongoing")
            self.assertEqual(rows[0]["status"], "critical")
            # list menandai down_ongoing
            r = self.client.get("/api/fiber", headers=XRW_HDR)
            item = next(o for o in r.get_json() if o["ont_sn"] == SN_DT_A)
            self.assertTrue(item["down_ongoing"])
            self.assertTrue(item["down_since"])
            r = self.client.get("/api/fiber/summary", headers=XRW_HDR)
            self.assertGreaterEqual(r.get_json()["counts"]["down_ongoing"], 1)
        finally:
            self.client.delete(f"/api/fiber/{fid}", headers=XRW_HDR)

    def test_close_saat_pulih_dengan_durasi(self):
        r = self.client.post("/api/fiber",
                             json={"ont_sn": SN_DT_A, "rx_power": -29.0,
                                   "source": "manual"},
                             headers=JSON_HDR)
        fid = r.get_json()["id"]
        try:
            r = self.client.put(f"/api/fiber/{fid}",
                                json={"ont_sn": SN_DT_A, "rx_power": -19.0,
                                      "source": "manual"},
                                headers=JSON_HDR)
            self.assertEqual(r.status_code, 200)
            rows = self._downtime(fid)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["state"], "resolved")
            self.assertIsNotNone(rows[0]["duration_s"])
            # log pemulihan memuat durasi
            conn, c = m.get_db()
            c.execute("SELECT message FROM system_logs WHERE host=? AND event_type='FIBER_NORMAL'"
                      " ORDER BY id DESC LIMIT 1", (SN_DT_A,))
            log = c.fetchone()
            conn.close()
            self.assertIsNotNone(log)
            self.assertIn("Durasi gangguan", log["message"])
            r = self.client.get("/api/fiber", headers=XRW_HDR)
            item = next(o for o in r.get_json() if o["ont_sn"] == SN_DT_A)
            self.assertFalse(item["down_ongoing"])
        finally:
            self.client.delete(f"/api/fiber/{fid}", headers=XRW_HDR)

    def test_warning_tidak_membuka_catatan(self):
        r = self.client.post("/api/fiber",
                             json={"ont_sn": SN_DT_W, "rx_power": -26.0,
                                   "source": "manual"},
                             headers=JSON_HDR)
        fid = r.get_json()["id"]
        try:
            m.poll_fiber_monitor()
            self.assertEqual(self._downtime(fid), [])
        finally:
            self.client.delete(f"/api/fiber/{fid}", headers=XRW_HDR)

    def test_stale_membiarkan_catatan_terbuka(self):
        from datetime import datetime, timedelta
        r = self.client.post("/api/fiber",
                             json={"ont_sn": SN_DT_B, "rx_power": -29.0,
                                   "source": "snmp", "ont_index": "5"},
                             headers=JSON_HDR)
        fid = r.get_json()["id"]
        try:
            self.assertEqual(len(self._downtime(fid)), 1)
            old = (datetime.now() - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")
            conn, c = m.get_db()
            c.execute("UPDATE fiber_onts SET last_seen=? WHERE id=?", (old, fid))
            conn.commit()
            conn.close()
            m.poll_fiber_monitor()
            r = self.client.get("/api/fiber", headers=XRW_HDR)
            item = next(o for o in r.get_json() if o["ont_sn"] == SN_DT_B)
            # stale tak menutupi critical (tetap butuh kunjungan teknisi)
            self.assertEqual(item["calc_status"], "critical")
            self.assertTrue(item["stale"])
            rows = self._downtime(fid)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["state"], "ongoing")
            r = self.client.put(f"/api/fiber/{fid}",
                                json={"ont_sn": SN_DT_B, "rx_power": -19.0,
                                      "source": "snmp", "ont_index": "5"},
                                headers=JSON_HDR)
            self.assertEqual(r.status_code, 200)
            self.assertEqual(self._downtime(fid)[0]["state"], "resolved")
        finally:
            self.client.delete(f"/api/fiber/{fid}", headers=XRW_HDR)

    def test_sla_dan_validasi_days(self):
        from datetime import datetime, timedelta
        r = self.client.post("/api/fiber",
                             json={"ont_sn": SN_DT_A, "rx_power": -29.0,
                                   "source": "manual"},
                             headers=JSON_HDR)
        fid = r.get_json()["id"]
        try:
            now = datetime.now()
            start = (now - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
            end = (now - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
            conn, c = m.get_db()
            c.execute("UPDATE fiber_downtime SET started_at=?, resolved_at=?,"
                      " duration_s=3600 WHERE ont_id=? AND resolved_at IS NULL", (start, end, fid))
            conn.commit()
            conn.close()
            r = self.client.get(f"/api/fiber/{fid}/sla?days=30", headers=XRW_HDR)
            self.assertEqual(r.status_code, 200)
            j = r.get_json()
            self.assertEqual(j["incidents"], 1)
            self.assertEqual(j["total_downtime_s"], 3600)
            self.assertAlmostEqual(j["uptime_pct"], round((30 * 86400 - 3600) / (30 * 86400) * 100, 2))
            r = self.client.get(f"/api/fiber/{fid}/sla?days=5", headers=XRW_HDR)
            self.assertEqual(r.status_code, 400)
            r = self.client.get("/api/fiber/999999/sla", headers=XRW_HDR)
            self.assertEqual(r.status_code, 404)
        finally:
            self.client.delete(f"/api/fiber/{fid}", headers=XRW_HDR)

    def test_delete_menghapus_riwayat_downtime(self):
        r = self.client.post("/api/fiber",
                             json={"ont_sn": SN_DT_C, "rx_power": -29.0,
                                   "source": "manual"},
                             headers=JSON_HDR)
        fid = r.get_json()["id"]
        self.assertEqual(len(self._downtime(fid)), 1)
        r = self.client.delete(f"/api/fiber/{fid}", headers=XRW_HDR)
        self.assertEqual(r.status_code, 200)
        conn, c = m.get_db()
        n = c.execute("SELECT COUNT(*) FROM fiber_downtime WHERE ont_id=?", (fid,)).fetchone()[0]
        conn.close()
        self.assertEqual(n, 0)


class FiberFlapTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = m.app.test_client()
        _cleanup_dt(cls.client)
        r = cls.client.post("/login",
                            data={"username": "admin", "password": "admin12345"})
        assert r.status_code == 302, f"login gagal, status={r.status_code}"

    @classmethod
    def tearDownClass(cls):
        _cleanup_dt(cls.client)

    def test_flap_terdeteksi_dan_masuk_triggers(self):
        from datetime import datetime, timedelta
        r = self.client.get("/api/settings", headers=XRW_HDR)
        orig = r.get_json()
        self.client.post("/api/settings",
                         json={"fiber_rx_warn": -25.0, "fiber_rx_crit": -27.0,
                               "fiber_flap_flips": 4, "fiber_flap_hours": 24},
                         headers=JSON_HDR)
        try:
            r = self.client.post("/api/fiber",
                                 json={"ont_sn": SN_FLAP, "rx_power": -19.0,
                                       "tx_power": 2.0, "source": "manual"},
                                 headers=JSON_HDR)
            self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
            fid = r.get_json()["id"]
            try:
                # baru 1 titik -> belum flap
                flapping, flips = m._fiber_flap(fid)
                self.assertFalse(flapping)
                self.assertEqual(flips, 0)
                # tanam riwayat bolak-balik: -19,-26,-19,-26,-19 (4 flip)
                now = datetime.now()
                conn, c = m.get_db()
                for h, rx in ((5, -19.0), (4, -26.0), (3, -19.0),
                              (2, -26.0), (1, -19.0)):
                    ts = (now - timedelta(hours=h)).strftime("%Y-%m-%d %H:%M:%S")
                    c.execute("INSERT INTO fiber_history (ont_id, rx_power, tx_power, timestamp)"
                              " VALUES (?,?,?,?)", (fid, rx, 2.0, ts))
                conn.commit()
                conn.close()
                r = self.client.put(f"/api/fiber/{fid}",
                                    json={"ont_sn": SN_FLAP, "rx_power": -19.0,
                                          "tx_power": 2.0, "source": "manual"},
                                    headers=JSON_HDR)
                self.assertEqual(r.status_code, 200)
                flapping, flips = m._fiber_flap(fid)
                self.assertTrue(flapping)
                self.assertEqual(flips, 4)
                m.poll_fiber_monitor()
                mem = m.fiber_flap_memory.get(fid) or {}
                self.assertTrue(mem.get("flapping"))
                r = self.client.get("/api/fiber", headers=XRW_HDR)
                item = next(o for o in r.get_json() if o["ont_sn"] == SN_FLAP)
                self.assertEqual(item["calc_status"], "normal")
                self.assertTrue(item["flapping"])
                self.assertEqual(item["flap_count"], 4)
                r = self.client.get("/api/fiber/summary", headers=XRW_HDR)
                self.assertGreaterEqual(r.get_json()["counts"]["flapping"], 1)
                r = self.client.get("/api/triggers", headers=XRW_HDR)
                flap = [a for a in r.get_json()
                        if a.get("category") == "fiber" and SN_FLAP in a.get("host", "")
                        and "FLAPPING" in a.get("message", "")]
                self.assertTrue(flap)
                self.assertEqual(flap[0]["severity"], "warning")
            finally:
                self.client.delete(f"/api/fiber/{fid}", headers=XRW_HDR)
                m.fiber_flap_memory.pop(fid, None)
        finally:
            self.client.post("/api/settings",
                             json={"fiber_rx_warn": orig["fiber_rx_warn"],
                                   "fiber_rx_crit": orig["fiber_rx_crit"],
                                   "fiber_flap_flips": orig["fiber_flap_flips"],
                                   "fiber_flap_hours": orig["fiber_flap_hours"]},
                             headers=JSON_HDR)

    def test_settings_flap_divalidasi(self):
        r = self.client.post("/api/settings", json={"fiber_flap_flips": 1},
                             headers=JSON_HDR)
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/api/settings", json={"fiber_flap_hours": 0},
                             headers=JSON_HDR)
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/api/settings", json={"fiber_flap_flips": 21},
                             headers=JSON_HDR)
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()
