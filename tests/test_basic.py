import os
import sqlite3
import tempfile
import unittest

_tmp = tempfile.mkdtemp(prefix="nms_test_")
os.environ["NMS_DB_PATH"] = os.path.join(_tmp, "test.db")
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["DASHBOARD_USERNAME"] = "admin"
os.environ["DASHBOARD_PASSWORD"] = "admin12345"
os.environ["TELEGRAM_BOT_TOKEN"] = ""
os.environ["TELEGRAM_CHAT_ID"] = ""
os.environ["AGENT_API_KEY"] = "test-agent-key"

os.environ["NMS_DISABLE_SCHEDULER"] = "1"

import app as m

try:
    m.scheduler.shutdown(wait=False)
except Exception:
    pass


class NmsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # DB_PATH di-bind sekali saat import app dan tak ikut override env
        # di atas; sinkronkan + init_db idempoten agar urutan import modul
        # test apa pun tetap jalan (tanpa ini: "no such table").
        m.DB_PATH = os.environ["NMS_DB_PATH"]
        m.init_db()
        cls.client = m.app.test_client()

        conn, c = m.get_db()
        c.execute("INSERT OR IGNORE INTO hosts (ip) VALUES (?)", ("10.99.99.99",))
        conn.commit()
        conn.close()
        r = cls.client.post("/login",
                            data={"username": "admin", "password": "admin12345"})


        assert r.status_code == 302, f"login gagal, status={r.status_code}"
        r2 = cls.client.get(r.headers.get("Location", "/"))
        assert r2.status_code == 200, f"sesudah login gagal, status={r2.status_code}"

    def test_health_tidak_bocor(self):
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertIn("status", body)
        self.assertNotIn("hosts", body)
        self.assertNotIn("db_rows", body)

    def test_history_invalid_400(self):
        r = self.client.get("/api/history?hours=abc")
        self.assertEqual(r.status_code, 400)

    def test_agent_tolak_cpu_string(self):
        r = self.client.post("/api/agent/report",
                             json={"host": "10.99.99.99", "cpu": "x", "ram": 10},
                             headers={"X-API-Key": "test-agent-key"})
        self.assertEqual(r.status_code, 400)

    def test_agent_tolak_host_tak_terdaftar(self):
        r = self.client.post("/api/agent/report",
                             json={"host": "10.99.99.98", "cpu": 10, "ram": 10},
                             headers={"X-API-Key": "test-agent-key"})
        self.assertEqual(r.status_code, 404)

    def test_agent_tolak_tanpa_key(self):
        r = self.client.post("/api/agent/report",
                             json={"host": "10.99.99.99", "cpu": 10, "ram": 10})
        self.assertEqual(r.status_code, 403)

    def test_agent_ok_dan_tercatat_source(self):
        r = self.client.post("/api/agent/report",
                             json={"host": "10.99.99.99", "cpu": 10, "ram": 10,
                                   "disk": 5, "net_in": 1, "net_out": 2},
                             headers={"X-API-Key": "test-agent-key"})
        self.assertEqual(r.status_code, 200)
        conn = sqlite3.connect(os.environ["NMS_DB_PATH"])
        row = conn.execute(
            "SELECT source FROM agent_metrics WHERE host=? ORDER BY id DESC LIMIT 1",
            ("10.99.99.99",)).fetchone()
        conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "agent")

    def test_csrf_ditolak_tanpa_json_atau_xrw(self):

        r = self.client.post("/api/events/clear", data={})
        self.assertEqual(r.status_code, 403)

    def test_ganti_password_validasi(self):
        r = self.client.post("/api/settings/password",
                             json={"current_password": "salah",
                                   "new_password": "baru12345"})
        self.assertEqual(r.status_code, 400)


    def test_down_row_tidak_dobel_saat_memori_reset(self):
        conn, c = m.get_db()
        c.execute("DELETE FROM down_events WHERE host='10.99.99.99'")
        c.execute("INSERT INTO down_events (host,started_at) VALUES "
                  "('10.99.99.99',datetime('now','localtime'))")
        conn.commit()
        conn.close()
        m.status_memory["10.99.99.99"] = False
        m.down_since.pop("10.99.99.99", None)
        orig = m.ping_host
        m.ping_host = lambda h: (-1, 100.0) if h == "10.99.99.99" else (0.5, 0.0)
        try:
            m.check_network()
        finally:
            m.ping_host = orig
        conn, c = m.get_db()
        n = c.execute("SELECT COUNT(*) FROM down_events WHERE host='10.99.99.99' "
                      "AND resolved_at IS NULL").fetchone()[0]
        c.execute("DELETE FROM down_events WHERE host='10.99.99.99'")
        conn.commit()
        conn.close()
        m.status_memory.pop("10.99.99.99", None)
        m.down_since.pop("10.99.99.99", None)
        self.assertEqual(n, 1)

    def test_agent_metrics_null_disk_tidak_500(self):
        conn, c = m.get_db()
        c.execute("INSERT INTO agent_metrics (host,cpu_percent,ram_percent,disk_percent,"
                  "net_in,net_out,timestamp,source) VALUES "
                  "('10.99.99.99',50,50,NULL,NULL,NULL,datetime('now','localtime'),'agent')")
        conn.commit()
        conn.close()
        r = self.client.get("/api/agent/metrics",
                            headers={"X-Requested-With": "XMLHttpRequest"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("10.99.99.99", r.get_json())

    def test_export_sanitasi_host_jahat(self):
        r = self.client.get("/api/export?host=a%0d%0aX-Injected%3A+1&hours=24",
                            headers={"X-Requested-With": "XMLHttpRequest"})
        self.assertEqual(r.status_code, 200)
        cd = r.headers.get("Content-Disposition") or ""
        self.assertNotIn("\r", cd)
        self.assertNotIn("\n", cd)

    def test_clear_events_reset_memory(self):
        m.status_memory["10.99.99.99"] = True
        m.down_since["10.99.99.99"] = m.datetime.now()
        conn, c = m.get_db()
        c.execute("INSERT INTO down_events (host,started_at) VALUES "
                  "('10.99.99.99',datetime('now','localtime'))")
        conn.commit()
        conn.close()
        r = self.client.post("/api/events/clear",
                             headers={"X-Requested-With": "XMLHttpRequest",
                                      "Content-Type": "application/json"})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(m.status_memory.get("10.99.99.99", False))
        self.assertNotIn("10.99.99.99", m.down_since)
        conn = sqlite3.connect(os.environ["NMS_DB_PATH"])
        n = conn.execute("SELECT COUNT(*) FROM down_events").fetchone()[0]
        conn.close()
        self.assertEqual(n, 0)

    def test_delete_event_404_dan_reset_ongoing(self):
        r = self.client.delete("/api/events/999999",
                               headers={"X-Requested-With": "XMLHttpRequest"})
        self.assertEqual(r.status_code, 404)
        m.status_memory["10.99.99.99"] = True
        conn, c = m.get_db()
        c.execute("INSERT INTO down_events (host,started_at) VALUES "
                  "('10.99.99.99',datetime('now','localtime'))")
        eid = c.lastrowid
        conn.commit()
        conn.close()
        r = self.client.delete(f"/api/events/{eid}",
                               headers={"X-Requested-With": "XMLHttpRequest"})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(m.status_memory.get("10.99.99.99", False))

    def test_delete_inventory_404(self):
        r = self.client.delete("/api/inventory/999999",
                               headers={"X-Requested-With": "XMLHttpRequest",
                                        "Content-Type": "application/json"})
        self.assertEqual(r.status_code, 404)

    def test_host_history_latency_nol_bukan_none(self):
        conn, c = m.get_db()
        c.execute("INSERT INTO ping_logs (host,latency,packet_loss,timestamp) VALUES "
                  "('10.99.99.99',0.0,0,datetime('now','localtime'))")
        conn.commit()
        conn.close()
        r = self.client.get("/api/host/10.99.99.99/history?hours=1&metric=latency")
        self.assertEqual(r.status_code, 200)
        self.assertIn(0.0, r.get_json()["values"])


if __name__ == "__main__":
    unittest.main()
