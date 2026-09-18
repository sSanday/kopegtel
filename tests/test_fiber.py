import unittest

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


if __name__ == "__main__":
    unittest.main()
