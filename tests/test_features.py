import unittest
from datetime import datetime, timedelta

import app as m

try:
    m.scheduler.shutdown(wait=False)
except Exception:
    pass

m.trigger_manual_service_check = lambda: None
m.trigger_async_ssl_check = lambda: None

JSON_HDR = {"Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest"}
XRW_HDR = {"X-Requested-With": "XMLHttpRequest"}

MAINT_HOST = "10.99.99.10"
PUB_HOST_ALIAS = "10.99.99.11"
PUB_HOST_BARE = "10.99.99.12"


def _window(hours_ago=1, hours_ahead=2):
    now = datetime.now()
    start = (now - timedelta(hours=hours_ago)).strftime("%Y-%m-%d %H:%M")
    end = (now + timedelta(hours=hours_ahead)).strftime("%Y-%m-%d %H:%M")
    return start, end


class MaintenanceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = m.app.test_client()
        conn, c = m.get_db()
        c.execute("INSERT OR IGNORE INTO hosts (ip) VALUES (?)", (MAINT_HOST,))
        conn.commit()
        conn.close()
        r = cls.client.post("/login",
                            data={"username": "admin", "password": "admin12345"})
        assert r.status_code == 302, f"login gagal, status={r.status_code}"

    def tearDown(self):
        conn, c = m.get_db()
        c.execute("DELETE FROM maintenance_windows WHERE host=?", (MAINT_HOST,))
        conn.commit()
        conn.close()
        m.status_memory.pop(MAINT_HOST, None)
        m.down_since.pop(MAINT_HOST, None)

    def test_crud(self):
        start, end = _window()
        r = self.client.post("/api/maintenance",
                             json={"host": MAINT_HOST, "start_at": start,
                                   "end_at": end, "reason": "upgrade"},
                             headers=JSON_HDR)
        self.assertEqual(r.status_code, 201)
        mid = r.get_json()["id"]

        r = self.client.get("/api/maintenance", headers=XRW_HDR)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(any(w["id"] == mid for w in r.get_json()))

        r = self.client.get("/api/maintenance?active=1", headers=XRW_HDR)
        self.assertTrue(any(w["id"] == mid and w["is_active"] for w in r.get_json()))

        r = self.client.delete(f"/api/maintenance/{mid}", headers=XRW_HDR)
        self.assertEqual(r.status_code, 200)
        r = self.client.delete(f"/api/maintenance/{mid}", headers=XRW_HDR)
        self.assertEqual(r.status_code, 404)

    def test_validasi(self):
        start, end = _window()
        r = self.client.post("/api/maintenance",
                             json={"host": MAINT_HOST, "start_at": "salah",
                                   "end_at": end}, headers=JSON_HDR)
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/api/maintenance",
                             json={"host": MAINT_HOST, "start_at": end,
                                   "end_at": start}, headers=JSON_HDR)
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/api/maintenance",
                             json={"host": "10.99.99.250", "start_at": start,
                                   "end_at": end}, headers=JSON_HDR)
        self.assertEqual(r.status_code, 404)

        r = self.client.post("/api/maintenance",
                             json={"host": MAINT_HOST, "start_at": start,
                                   "end_at": end}, headers=JSON_HDR)
        self.assertEqual(r.status_code, 201)
        r = self.client.post("/api/maintenance",
                             json={"host": MAINT_HOST, "start_at": start,
                                   "end_at": end}, headers=JSON_HDR)
        self.assertEqual(r.status_code, 400)

    def test_suppress_telegram_dan_flag_event(self):
        start, end = _window()
        r = self.client.post("/api/maintenance",
                             json={"host": MAINT_HOST, "start_at": start,
                                   "end_at": end}, headers=JSON_HDR)
        self.assertEqual(r.status_code, 201)

        orig_ping = m.ping_host
        orig_tg = m.send_telegram_alert
        sent = []
        m.ping_host = lambda h: (-1, 100.0) if h == MAINT_HOST else (0.5, 0.0)
        m.send_telegram_alert = lambda msg: sent.append(msg)
        try:
            m.status_memory[MAINT_HOST] = False
            m.check_network()
        finally:
            m.ping_host = orig_ping
            m.send_telegram_alert = orig_tg

        self.assertFalse(any(MAINT_HOST in msg for msg in sent),
                         f"telegram bocor saat maintenance: {sent}")
        conn, c = m.get_db()
        try:
            c.execute("SELECT is_maintenance FROM down_events WHERE host=? "
                      "ORDER BY id DESC LIMIT 1", (MAINT_HOST,))
            row = c.fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(row["is_maintenance"], 1)

    def test_triggers_kategori_maintenance(self):
        start, end = _window()
        self.client.post("/api/maintenance",
                         json={"host": MAINT_HOST, "start_at": start,
                               "end_at": end, "reason": "uji"},
                         headers=JSON_HDR)
        m.status_memory[MAINT_HOST] = True
        r = self.client.get("/api/triggers", headers=XRW_HDR)
        self.assertEqual(r.status_code, 200)
        alarms = [a for a in r.get_json() if a["host"] == MAINT_HOST]
        self.assertTrue(any(a["category"] == "maintenance"
                            and a["severity"] == "warning" for a in alarms),
                        alarms)
        self.assertFalse(any(a["severity"] == "disaster" for a in alarms), alarms)


class SslServiceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = m.app.test_client()
        r = cls.client.post("/login",
                            data={"username": "admin", "password": "admin12345"})
        assert r.status_code == 302, f"login gagal, status={r.status_code}"

    def test_ssl_level(self):
        self.assertIsNone(m._ssl_level(None))
        self.assertIsNone(m._ssl_level(45))
        self.assertEqual(m._ssl_level(30), "warning")
        self.assertEqual(m._ssl_level(8), "warning")
        self.assertEqual(m._ssl_level(7), "high")
        self.assertEqual(m._ssl_level(0), "high")
        self.assertEqual(m._ssl_level(-1), "expired")

    def test_ssl_guard_non_https(self):
        self.assertEqual(m.get_ssl_expiry("http://example.com"), (None, None))
        self.assertEqual(m.get_ssl_expiry("https://"), (None, None))

    def test_history_api(self):
        r = self.client.post("/api/services",
                             json={"ip": "127.0.0.1", "name": "svc-hist-uji",
                                   "type": "tcp", "port": 22222},
                             headers=JSON_HDR)
        self.assertEqual(r.status_code, 201)
        sid = r.get_json()["id"]
        try:
            r = self.client.get(f"/api/services/{sid}/history?hours=abc",
                                headers=XRW_HDR)
            self.assertEqual(r.status_code, 400)
            r = self.client.get("/api/services/999999/history?hours=1",
                                headers=XRW_HDR)
            self.assertEqual(r.status_code, 404)

            conn, c = m.get_db()
            c.execute("DELETE FROM service_history WHERE service_id=?", (sid,))
            c.execute("INSERT INTO service_history (service_id,status,latency,timestamp)"
                      " VALUES (?,?,?,datetime('now','localtime'))",
                      (sid, "ONLINE", 12.5))
            c.execute("INSERT INTO service_history (service_id,status,latency,timestamp)"
                      " VALUES (?,?,?,datetime('now','localtime'))",
                      (sid, "OFFLINE", 0))
            conn.commit()
            conn.close()
            r = self.client.get(f"/api/services/{sid}/history?hours=1",
                                headers=XRW_HDR)
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.get_json()["values"], [12.5, None])

            conn, c = m.get_db()
            c.execute("UPDATE services SET status='OFFLINE' WHERE id=?", (sid,))
            conn.commit()
            conn.close()
            r = self.client.get("/api/triggers", headers=XRW_HDR)
            cats = {a["category"] for a in r.get_json()}
            self.assertIn("service", cats)
        finally:
            self.client.delete(f"/api/services/{sid}", headers=XRW_HDR)

    def test_ssl_alarm_di_triggers(self):
        r = self.client.post("/api/services",
                             json={"ip": "ex-uji", "name": "web-ssl-uji",
                                   "type": "http", "url": "https://contoh-uji.invalid"},
                             headers=JSON_HDR)
        self.assertEqual(r.status_code, 201)
        sid = r.get_json()["id"]
        try:
            conn, c = m.get_db()
            c.execute("UPDATE services SET ssl_days_left=5, "
                      "ssl_expires_at='2026-09-21 00:00:00' WHERE id=?", (sid,))
            conn.commit()
            conn.close()
            r = self.client.get("/api/triggers", headers=XRW_HDR)
            msgs = [a["message"] for a in r.get_json() if "SSL" in a["message"]]
            self.assertTrue(any("H-5" in x for x in msgs), msgs)
        finally:
            self.client.delete(f"/api/services/{sid}", headers=XRW_HDR)


class PublicStatusTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = m.app.test_client()
        r = cls.client.post("/login",
                            data={"username": "admin", "password": "admin12345"})
        assert r.status_code == 302, f"login gagal, status={r.status_code}"
        cls.anon = m.app.test_client()
        conn, c = m.get_db()
        c.execute("INSERT OR IGNORE INTO hosts (ip, alias) VALUES (?, ?)",
                  (PUB_HOST_ALIAS, "Router Uji"))
        c.execute("INSERT OR IGNORE INTO hosts (ip) VALUES (?)", (PUB_HOST_BARE,))
        c.execute("INSERT INTO ping_logs (host,latency,packet_loss,timestamp)"
                  " VALUES (?,?,?,datetime('now','localtime'))",
                  (PUB_HOST_ALIAS, 5.0, 0))
        conn.commit()
        conn.close()
        m.status_memory[PUB_HOST_ALIAS] = False

    def test_tanpa_login(self):
        r = self.anon.get("/status")
        self.assertEqual(r.status_code, 200)
        self.assertIn("STATUS", r.get_data(as_text=True))
        r = self.anon.get("/api/public/status")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers.get("Cache-Control"), "no-store")

    def test_tidak_bocor_ip(self):
        body = self.anon.get("/api/public/status").get_data(as_text=True)
        for secret in (PUB_HOST_ALIAS, PUB_HOST_BARE):
            self.assertNotIn(secret, body)
        j = self.anon.get("/api/public/status").get_json()
        self.assertEqual(set(j.keys()), {"generated_at", "summary", "nodes", "services"})
        names = [n["name"] for n in j["nodes"]]
        self.assertIn("Router Uji", names)
        self.assertTrue(any(n.startswith("node-") for n in names), names)
        self.assertTrue(all(set(n.keys()) <= {"name", "status", "uptime_24h",
                                              "avg_ms", "note"} for n in j["nodes"]))
        self.assertTrue(all(set(s.keys()) == {"name", "type", "status", "uptime_24h"}
                            for s in j["services"]))

    def test_api_privat_tetap_401(self):
        self.assertEqual(self.anon.get("/api/stats").status_code, 401)
        self.assertEqual(self.anon.get("/api/hosts").status_code, 401)


if __name__ == "__main__":
    unittest.main()
