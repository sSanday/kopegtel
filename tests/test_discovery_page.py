"""Uji halaman /discovery dan API status discovery (gaya Zabbix).

Halaman menampilkan kondisi terkini perangkat per aturan pemindaian
(tabel discovery_events; satu baris terakhir per sumber+network+ip).
Scan manual/terjadwal mencatat transisi 'discovered'/'lost'.
"""

import os
import tempfile
import unittest

_tmp = tempfile.mkdtemp(prefix="nms_test_disc_")
os.environ["NMS_DB_PATH"] = os.path.join(_tmp, "test.db")
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["DASHBOARD_USERNAME"] = "admin"
os.environ["DASHBOARD_PASSWORD"] = "PantauNms2026"
os.environ["TELEGRAM_BOT_TOKEN"] = ""
os.environ["TELEGRAM_CHAT_ID"] = ""
os.environ["AGENT_API_KEY"] = "test-agent-key"
os.environ["NMS_DISABLE_SCHEDULER"] = "1"

import app as m

try:
    m.scheduler.shutdown(wait=False)
except Exception:
    pass

DISC_KEYS = (
    "discovery_network",
    "discovery_community",
    "discovery_last_hosts",
    "discovery_last_scan",
)


def _dev(ip, name="", vendor="MikroTik"):
    return {
        "ip": ip,
        "sys_name": name,
        "sys_descr": "RouterOS",
        "vendor": vendor,
        "reachable": True,
    }


class DiscoveryPageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        m.DB_PATH = os.environ["NMS_DB_PATH"]
        m.init_db()
        cls.client = m.app.test_client()
        r = cls.client.post(
            "/login", data={"username": "admin", "password": "PantauNms2026"}
        )
        assert r.status_code == 302, f"login gagal, status={r.status_code}"

    def setUp(self):
        conn, c = m.get_db()
        c.execute("DELETE FROM discovery_events")
        c.execute("DELETE FROM hosts")
        c.execute("DELETE FROM settings WHERE key IN (?,?,?,?)", DISC_KEYS)
        conn.commit()
        conn.close()

    # ---------- halaman ----------

    def test_page_200_and_structure(self):
        r = self.client.get("/discovery")
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        self.assertIn("Status of discovery", html)
        self.assertIn('id="filterToggle"', html)
        self.assertIn('id="btnScanOpen"', html)
        self.assertIn('id="btnRunSched"', html)
        self.assertNotIn('id="btnScanOpen" hidden', html)
        self.assertNotIn('id="btnRunSched" hidden', html)
        self.assertIn('id="scanProg"', html)
        self.assertIn('id="loadingState"', html)
        self.assertIn('id="emptyAction"', html)
        self.assertIn('id="btnHelp"', html)
        self.assertIn("/api/discovery/status", html)
        # Modal pilih discovery rule.
        self.assertIn('id="ruleBack"', html)
        self.assertIn("Discovery rules", html)
        self.assertIn('id="ruleRows"', html)
        self.assertIn('id="ruleSelectAll"', html)
        self.assertIn('id="ruleSelect"', html)
        self.assertIn('id="ruleCancel"', html)
        self.assertIn("No data found", html)
        self.assertIn("function statusQuery", html)
        self.assertNotIn('id="rulePop"', html)
        self.assertNotIn("/topo", html)
        self.assertNotIn("js/discovery.js", html)  # js lama tidak dipakai lagi

    def test_page_login_required(self):
        fresh = m.app.test_client()
        r = fresh.get("/discovery")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login", r.headers.get("Location", ""))

    # ---------- API status ----------

    def test_status_login_required(self):
        fresh = m.app.test_client()
        r = fresh.get("/api/discovery/status")
        self.assertIn(r.status_code, (302, 401))

    def test_status_empty(self):
        r = self.client.get("/api/discovery/status")
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertEqual(d["rows"], [])
        self.assertEqual(d["rules"], [])
        self.assertEqual(d["count"], 0)

    # ---------- scan manual mencatat event ----------

    def test_manual_scan_records_events(self):
        original = m.discover_subnet
        try:
            m.discover_subnet = lambda *a, **k: (
                [_dev("10.79.9.10", "rt-utama"), _dev("10.79.9.11")],
                {},
            )
            r = self.client.post(
                "/api/discovery/snmp",
                json={"network": "10.79.9.0/24", "community": "public"},
            )
            self.assertEqual(r.status_code, 200, msg=r.get_data(as_text=True))
        finally:
            m.discover_subnet = original
        d = self.client.get("/api/discovery/status").get_json()
        self.assertEqual(d["count"], 2)
        self.assertEqual(d["total"], 2)
        self.assertEqual(d["total_all"], 2)
        self.assertEqual(len(d["rules"]), 1)
        self.assertEqual(d["rules"][0]["label"], "Manual · 10.79.9.0/24")
        self.assertEqual(d["rules"][0]["count"], 2)
        self.assertEqual(d["rules"][0]["key"], "manual|10.79.9.0/24")
        by_ip = {row["ip"]: row for row in d["rows"]}
        self.assertEqual(by_ip["10.79.9.10"]["name"], "rt-utama")
        self.assertEqual(by_ip["10.79.9.10"]["status"], "discovered")
        self.assertEqual(by_ip["10.79.9.11"]["name"], "10.79.9.11")  # nama kosong → IP
        self.assertFalse(by_ip["10.79.9.10"]["monitored"])

    def test_manual_scan_change_marks_lost(self):
        original = m.discover_subnet
        try:
            m.discover_subnet = lambda *a, **k: (
                [_dev("10.79.9.10", "rt-utama"), _dev("10.79.9.11", "sw-lantai1")],
                {},
            )
            self.client.post(
                "/api/discovery/snmp",
                json={"network": "10.79.9.0/24", "community": "public"},
            )
            # scan kedua: .11 hilang, .12 baru, .10 tetap
            m.discover_subnet = lambda *a, **k: (
                [_dev("10.79.9.10", "rt-utama"), _dev("10.79.9.12", "ap-baru")],
                {},
            )
            self.client.post(
                "/api/discovery/snmp",
                json={"network": "10.79.9.0/24", "community": "public"},
            )
        finally:
            m.discover_subnet = original
        d = self.client.get("/api/discovery/status").get_json()
        by_ip = {row["ip"]: row for row in d["rows"]}
        self.assertEqual(by_ip["10.79.9.11"]["status"], "lost")
        self.assertEqual(
            by_ip["10.79.9.11"]["name"], "sw-lantai1"
        )  # mewarisi nama lama
        self.assertEqual(by_ip["10.79.9.12"]["status"], "discovered")
        self.assertEqual(by_ip["10.79.9.10"]["status"], "discovered")
        # .10 tidak dicatat ulang (status tetap) → total 4 event
        conn, c = m.get_db()
        n = c.execute("SELECT COUNT(*) AS n FROM discovery_events").fetchone()["n"]
        conn.close()
        self.assertEqual(n, 4)

    # ---------- scan terjadwal mencatat event ----------

    def test_scheduled_run_records_events(self):
        self.client.post(
            "/api/settings",
            json={"discovery_network": "10.79.8.0/24", "discovery_community": "pub"},
        )
        original = (m.discover_subnet, m.send_telegram_alert)
        try:
            m.discover_subnet = lambda *a, **k: [_dev("10.79.8.5", "rt-backbone")]
            m.send_telegram_alert = lambda *a, **k: None
            r = self.client.post(
                "/api/discovery/scheduled/run",
                headers={"X-Requested-With": "XMLHttpRequest"},
            )
            self.assertEqual(r.status_code, 200, msg=r.get_data(as_text=True))
            self.assertEqual(r.get_json()["status"], "ok")
        finally:
            m.discover_subnet, m.send_telegram_alert = original
        d = self.client.get("/api/discovery/status").get_json()
        self.assertEqual([row["ip"] for row in d["rows"]], ["10.79.8.5"])
        self.assertTrue(d["rows"][0]["rule"].startswith("Terjadwal"))
        # jalankan ulang tanpa perubahan → tidak ada event baru
        conn, c = m.get_db()
        n_before = c.execute("SELECT COUNT(*) AS n FROM discovery_events").fetchone()[
            "n"
        ]
        conn.close()
        original = (m.discover_subnet, m.send_telegram_alert)
        try:
            m.discover_subnet = lambda *a, **k: [_dev("10.79.8.5", "rt-backbone")]
            m.send_telegram_alert = lambda *a, **k: None
            self.client.post(
                "/api/discovery/scheduled/run",
                headers={"X-Requested-With": "XMLHttpRequest"},
            )
        finally:
            m.discover_subnet, m.send_telegram_alert = original
        conn, c = m.get_db()
        n_after = c.execute("SELECT COUNT(*) AS n FROM discovery_events").fetchone()[
            "n"
        ]
        conn.close()
        self.assertEqual(n_after, n_before)

    # ---------- filter & urut ----------

    def test_filter_rule(self):
        self.client.post(
            "/api/settings",
            json={"discovery_network": "10.79.8.0/24", "discovery_community": "pub"},
        )
        original = (m.discover_subnet, m.send_telegram_alert)
        try:
            m.send_telegram_alert = lambda *a, **k: None
            m.discover_subnet = lambda *a, **k: [_dev("10.79.8.5", "rt-backbone")]
            self.client.post(
                "/api/discovery/scheduled/run",
                headers={"X-Requested-With": "XMLHttpRequest"},
            )
            m.discover_subnet = lambda *a, **k: ([_dev("10.79.9.10", "rt-utama")], {})
            self.client.post(
                "/api/discovery/snmp",
                json={"network": "10.79.9.0/24", "community": "public"},
            )
        finally:
            m.discover_subnet, m.send_telegram_alert = original
        d = self.client.get("/api/discovery/status?rule=10.79.8").get_json()
        self.assertEqual([row["ip"] for row in d["rows"]], ["10.79.8.5"])
        d = self.client.get("/api/discovery/status?rule=10.79.9").get_json()
        self.assertEqual([row["ip"] for row in d["rows"]], ["10.79.9.10"])
        d = self.client.get("/api/discovery/status?rule=10.55.0.0").get_json()
        self.assertEqual(d["count"], 0)
        # Beberapa rule sekaligus → OR (parameter berulang maupun dipisah koma).
        expected = ["10.79.8.5", "10.79.9.10"]
        d = self.client.get(
            "/api/discovery/status?rule=10.79.8&rule=10.79.9"
        ).get_json()
        self.assertEqual([row["ip"] for row in d["rows"]], expected)
        d = self.client.get("/api/discovery/status?rule=10.79.8,10.79.9").get_json()
        self.assertEqual([row["ip"] for row in d["rows"]], expected)
        d = self.client.get(
            "/api/discovery/status?rule=10.55.0.0&rule=10.79.9"
        ).get_json()
        self.assertEqual([row["ip"] for row in d["rows"]], ["10.79.9.10"])

    def test_sort_name(self):
        original = m.discover_subnet
        try:
            m.discover_subnet = lambda *a, **k: (
                [_dev("10.79.9.10", "aaa"), _dev("10.79.9.11", "zzz")],
                {},
            )
            self.client.post(
                "/api/discovery/snmp",
                json={"network": "10.79.9.0/24", "community": "public"},
            )
        finally:
            m.discover_subnet = original
        d = self.client.get("/api/discovery/status").get_json()
        self.assertEqual([row["name"] for row in d["rows"]], ["aaa", "zzz"])
        d = self.client.get("/api/discovery/status?sort=name_desc").get_json()
        self.assertEqual([row["name"] for row in d["rows"]], ["zzz", "aaa"])

    # ---------- flag monitored setelah impor ----------

    def test_monitored_flag_after_import(self):
        original = m.discover_subnet
        try:
            m.discover_subnet = lambda *a, **k: ([_dev("10.79.9.10", "rt-utama")], {})
            self.client.post(
                "/api/discovery/snmp",
                json={"network": "10.79.9.0/24", "community": "public"},
            )
        finally:
            m.discover_subnet = original
        r = self.client.post(
            "/api/discovery/import",
            json={
                "devices": [
                    {"ip": "10.79.9.10", "sys_name": "rt-utama", "vendor": "MikroTik"}
                ],
                "community": "public",
                "save_community": False,
                "category": "auto",
            },
        )
        self.assertEqual(r.status_code, 201, msg=r.get_data(as_text=True))
        d = self.client.get("/api/discovery/status").get_json()
        self.assertTrue(d["rows"][0]["monitored"])

    # ---------- validasi ----------

    def test_scan_validation_error(self):
        r = self.client.post(
            "/api/discovery/snmp", json={"network": "bad", "community": "public"}
        )
        self.assertEqual(r.status_code, 400)

    def test_scan_missing_body(self):
        r = self.client.post("/api/discovery/snmp")
        self.assertEqual(r.status_code, 403)

    def test_scan_rejects_when_another_scan_runs(self):
        self.assertTrue(m._DISC_SCAN_LOCK.acquire(blocking=False))
        try:
            r = self.client.post(
                "/api/discovery/snmp",
                json={"network": "10.79.9.0/24", "community": "public"},
            )
            self.assertEqual(r.status_code, 409)
            self.assertIn("sedang berjalan", r.get_json()["error"])
        finally:
            m._DISC_SCAN_LOCK.release()

    def test_schedule_settings_roundtrip(self):
        r = self.client.post(
            "/api/settings",
            json={"discovery_network": "10.79.8.0/24", "discovery_community": "c0mm"},
        )
        self.assertEqual(r.status_code, 200)
        s = self.client.get("/api/settings").get_json()
        self.assertEqual(s["discovery_network"], "10.79.8.0/24")
        self.assertEqual(s["discovery_community"], "c0mm")

    def test_ts_format(self):
        original = m.discover_subnet
        try:
            m.discover_subnet = lambda *a, **k: ([_dev("10.79.9.10")], {})
            self.client.post(
                "/api/discovery/snmp",
                json={"network": "10.79.9.0/24", "community": "public"},
            )
        finally:
            m.discover_subnet = original
        d = self.client.get("/api/discovery/status").get_json()
        self.assertRegex(d["rows"][0]["ts"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")

    # ---------- normalisasi & filter per sumber ----------

    def test_network_normalized_on_scan_and_settings(self):
        original = m.discover_subnet
        try:
            m.discover_subnet = lambda *a, **k: ([_dev("10.79.9.10")], {})
            r = self.client.post(
                "/api/discovery/snmp",
                json={"network": "10.79.9.5/24", "community": "public"},
            )
            self.assertEqual(r.status_code, 200, msg=r.get_data(as_text=True))
            self.assertEqual(r.get_json()["network"], "10.79.9.0/24")
        finally:
            m.discover_subnet = original
        d = self.client.get("/api/discovery/status").get_json()
        self.assertEqual(d["rows"][0]["network"], "10.79.9.0/24")
        self.assertEqual(d["rules"][0]["key"], "manual|10.79.9.0/24")
        # Filter lama yang menyimpan CIDR ber-host-bit tetap cocok.
        legacy = self.client.get("/api/discovery/status?rule=10.79.9.5/24").get_json()
        self.assertEqual(legacy["total"], 1)

        r = self.client.post("/api/settings", json={"discovery_network": "10.79.8.7/24"})
        self.assertEqual(r.status_code, 200, msg=r.get_data(as_text=True))
        s = self.client.get("/api/settings").get_json()
        self.assertEqual(s["discovery_network"], "10.79.8.0/24")

    def test_filter_by_source(self):
        self.client.post(
            "/api/settings",
            json={"discovery_network": "10.79.8.0/24", "discovery_community": "pub"},
        )
        original = (m.discover_subnet, m.send_telegram_alert)
        try:
            m.send_telegram_alert = lambda *a, **k: None
            m.discover_subnet = lambda *a, **k: [_dev("10.79.8.5", "rt-backbone")]
            self.client.post(
                "/api/discovery/scheduled/run",
                headers={"X-Requested-With": "XMLHttpRequest"},
            )
            m.discover_subnet = lambda *a, **k: ([_dev("10.79.9.10", "rt-utama")], {})
            self.client.post(
                "/api/discovery/snmp",
                json={"network": "10.79.9.0/24", "community": "public"},
            )
        finally:
            m.discover_subnet, m.send_telegram_alert = original

        d = self.client.get("/api/discovery/status?rule=manual|10.79.9.0/24").get_json()
        self.assertEqual([row["source"] for row in d["rows"]], ["manual"])
        self.assertEqual(d["total"], 1)
        self.assertEqual(d["total_all"], 2)

        d = self.client.get(
            "/api/discovery/status?rule=scheduled|10.79.8.0/24"
        ).get_json()
        self.assertEqual([row["source"] for row in d["rows"]], ["scheduled"])

        # Sumber saja (kompatibilitas teks lama) dan kombinasi yang salah.
        d = self.client.get("/api/discovery/status?rule=Terjadwal").get_json()
        self.assertEqual([row["source"] for row in d["rows"]], ["scheduled"])
        d = self.client.get("/api/discovery/status?rule=manual|10.79.8.0/24").get_json()
        self.assertEqual(d["total"], 0)
        self.assertEqual(d["total_all"], 2)

    def test_raw_and_normalized_network_merge(self):
        conn, c = m.get_db()
        try:
            for net in ("10.79.9.0/24", "10.79.9.5/24"):
                c.execute(
                    "INSERT INTO discovery_events (source, network, ip, sys_name, vendor, status, ts)"
                    " VALUES (?,?,?,?,?,?,?)",
                    ("manual", net, "10.79.9.10", "rt-utama", "MikroTik", "discovered",
                     "2026-10-30 10:00:00"),
                )
            conn.commit()
        finally:
            conn.close()
        d = self.client.get("/api/discovery/status").get_json()
        self.assertEqual(d["count"], 1)
        self.assertEqual(d["total_all"], 1)
        self.assertEqual(d["rows"][0]["network"], "10.79.9.0/24")

    def test_scheduled_run_updates_last_scan(self):
        self.client.post(
            "/api/settings",
            json={"discovery_network": "10.79.8.0/24", "discovery_community": "pub"},
        )
        original = (m.discover_subnet, m.send_telegram_alert)
        try:
            m.discover_subnet = lambda *a, **k: [_dev("10.79.8.5", "rt-backbone")]
            m.send_telegram_alert = lambda *a, **k: None
            r = self.client.post(
                "/api/discovery/scheduled/run",
                headers={"X-Requested-With": "XMLHttpRequest"},
            )
            self.assertEqual(r.status_code, 200, msg=r.get_data(as_text=True))
        finally:
            m.discover_subnet, m.send_telegram_alert = original
        d = self.client.get("/api/discovery/status").get_json()
        last = d["schedule"]["last"]
        self.assertIsNotNone(last)
        self.assertEqual(last["source"], "scheduled")
        self.assertEqual(last["network"], "10.79.8.0/24")
        self.assertEqual(last["count"], 1)


if __name__ == "__main__":
    unittest.main()
