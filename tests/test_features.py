import os
import tempfile
import unittest
from datetime import datetime, timedelta

_tmp = tempfile.mkdtemp(prefix="nms_test_features_")
os.environ.setdefault("NMS_DB_PATH", os.path.join(_tmp, "test.db"))
os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("DASHBOARD_USERNAME", "admin")
os.environ.setdefault("DASHBOARD_PASSWORD", "PantauNms2026")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "")
os.environ.setdefault("TELEGRAM_CHAT_ID", "")
os.environ.setdefault("AGENT_API_KEY", "test-agent-key")
os.environ.setdefault("NMS_DISABLE_SCHEDULER", "1")

import app as m
from nms import monitor as _monmod

try:
    m.scheduler.shutdown(wait=False)
except Exception:
    pass

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
        r = cls.client.post(
            "/login", data={"username": "admin", "password": "PantauNms2026"}
        )
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
        r = self.client.post(
            "/api/maintenance",
            json={
                "host": MAINT_HOST,
                "start_at": start,
                "end_at": end,
                "reason": "upgrade",
            },
            headers=JSON_HDR,
        )
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
        r = self.client.post(
            "/api/maintenance",
            json={"host": MAINT_HOST, "start_at": "salah", "end_at": end},
            headers=JSON_HDR,
        )
        self.assertEqual(r.status_code, 400)
        r = self.client.post(
            "/api/maintenance",
            json={"host": MAINT_HOST, "start_at": end, "end_at": start},
            headers=JSON_HDR,
        )
        self.assertEqual(r.status_code, 400)
        r = self.client.post(
            "/api/maintenance",
            json={"host": "10.99.99.250", "start_at": start, "end_at": end},
            headers=JSON_HDR,
        )
        self.assertEqual(r.status_code, 404)

        r = self.client.post(
            "/api/maintenance",
            json={"host": MAINT_HOST, "start_at": start, "end_at": end},
            headers=JSON_HDR,
        )
        self.assertEqual(r.status_code, 201)
        r = self.client.post(
            "/api/maintenance",
            json={"host": MAINT_HOST, "start_at": start, "end_at": end},
            headers=JSON_HDR,
        )
        self.assertEqual(r.status_code, 400)

    def test_suppress_telegram_dan_flag_event(self):
        start, end = _window()
        r = self.client.post(
            "/api/maintenance",
            json={"host": MAINT_HOST, "start_at": start, "end_at": end},
            headers=JSON_HDR,
        )
        self.assertEqual(r.status_code, 201)

        orig_ping = _monmod.ping_host
        orig_tg = _monmod.send_telegram_alert
        sent = []
        _monmod.ping_host = lambda h: (-1, 100.0) if h == MAINT_HOST else (0.5, 0.0)
        _monmod.send_telegram_alert = lambda msg: sent.append(msg)
        try:
            m.status_memory[MAINT_HOST] = False
            m.check_network()
        finally:
            _monmod.ping_host = orig_ping
            _monmod.send_telegram_alert = orig_tg

        self.assertFalse(
            any(MAINT_HOST in msg for msg in sent),
            f"telegram bocor saat maintenance: {sent}",
        )
        conn, c = m.get_db()
        try:
            c.execute(
                "SELECT is_maintenance FROM down_events WHERE host=? "
                "ORDER BY id DESC LIMIT 1",
                (MAINT_HOST,),
            )
            row = c.fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(row["is_maintenance"], 1)

class SlaAndInterfaceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = m.app.test_client()
        cls.client.post(
            "/login", data={"username": "admin", "password": "PantauNms2026"}
        )

    def test_sla_page_and_api(self):
        self.assertEqual(self.client.get("/sla").status_code, 200)
        r = self.client.get("/api/sla/sites?days=7", headers=XRW_HDR)
        self.assertEqual(r.status_code, 200)
        self.assertIn("sites", r.get_json())
        r = self.client.get("/api/sla/history?days=7", headers=XRW_HDR)
        self.assertEqual(r.status_code, 200)
        self.assertIn("history", r.get_json())
        r = self.client.get(
            "/api/interfaces/traffic?host=10.99.99.99&hours=24", headers=XRW_HDR
        )
        self.assertEqual(r.status_code, 200)
        self.assertIn("traffic", r.get_json())


class ScheduledDiscoveryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = m.app.test_client()
        cls.client.post(
            "/login", data={"username": "admin", "password": "PantauNms2026"}
        )

    def test_schedule_and_change_detection(self):
        original_discover = m.discover_subnet
        original_notify = m.send_telegram_alert
        sent = []
        m.discover_subnet = lambda *args, **kwargs: [
            {
                "ip": "10.99.99.21",
                "sys_name": "test",
                "sys_descr": "x",
                "vendor": "Generic",
            }
        ]
        m.send_telegram_alert = lambda message: sent.append(message)
        try:
            r = self.client.post(
                "/api/settings",
                json={
                    "discovery_network": "10.99.99.0/24",
                    "discovery_community": "public",
                },
                headers=JSON_HDR,
            )
            self.assertEqual(r.status_code, 200)
            result = m.run_scheduled_snmp_discovery()
            self.assertEqual(result["added"], ["10.99.99.21"])
            self.assertTrue(sent)
        finally:
            m.discover_subnet = original_discover
            m.send_telegram_alert = original_notify


class SnmpDiscoveryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = m.app.test_client()
        cls.client.post(
            "/login", data={"username": "admin", "password": "PantauNms2026"}
        )

    def test_discovery_validation_and_result(self):
        r = self.client.post(
            "/api/discovery/snmp",
            json={"network": "bad", "community": "public"},
            headers=JSON_HDR,
        )
        self.assertEqual(r.status_code, 400)
        original = m.discover_subnet
        m.discover_subnet = lambda *args, **kwargs: (
            [
                {
                    "ip": "10.99.99.20",
                    "sys_name": "router",
                    "sys_descr": "test",
                    "reachable": True,
                }
            ],
            {
                "hosts": 1,
                "responded": 1,
                "timeout": 0,
                "no_response": 0,
                "partial": 0,
                "community_or_error": 0,
            },
        )
        try:
            r = self.client.post(
                "/api/discovery/snmp",
                json={"network": "10.99.99.0/24", "community": "public"},
                headers=JSON_HDR,
            )
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.get_json()["count"], 1)
            r = self.client.post(
                "/api/discovery/import",
                json={
                    "community": "public",
                    "devices": [
                        {
                            "ip": "10.99.99.20",
                            "sys_name": "router",
                            "vendor": "MikroTik",
                        }
                    ],
                },
                headers=JSON_HDR,
            )
            self.assertEqual(r.status_code, 201)
            self.assertEqual(r.get_json()["count"], 1)
            conn, c = m.get_db()
            c.execute("DELETE FROM hosts WHERE ip=?", ("10.99.99.20",))
            conn.commit()
            conn.close()
        finally:
            m.discover_subnet = original


class IncidentDashboardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = m.app.test_client()
        cls.client.post(
            "/login", data={"username": "admin", "password": "PantauNms2026"}
        )

    def test_page_and_escalation(self):
        self.assertEqual(self.client.get("/incidents").status_code, 200)
        conn, c = m.get_db()
        ts = (datetime.now() - timedelta(minutes=20)).strftime("%Y-%m-%d %H:%M:%S")
        c.execute(
            "INSERT INTO alert_history (host, alert_type, severity, message, triggered_at) VALUES (?, ?, ?, ?, ?)",
            ("10.99.99.14", "test", "high", "escalate test", ts),
        )
        alert_id = c.lastrowid
        conn.commit()
        conn.close()
        sent = []
        original = m.send_telegram_alert
        m.send_telegram_alert = lambda message: sent.append(message)
        try:
            self.assertEqual(m.escalate_unacknowledged_alerts(), 1)
            self.assertTrue(sent)
            conn, c = m.get_db()
            c.execute("SELECT escalated_at FROM alert_history WHERE id=?", (alert_id,))
            self.assertIsNotNone(c.fetchone()["escalated_at"])
            conn.close()
        finally:
            m.send_telegram_alert = original
            conn, c = m.get_db()
            c.execute("DELETE FROM alert_history WHERE id=?", (alert_id,))
            conn.commit()
            conn.close()


class AlertCorrelationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = m.app.test_client()
        cls.client.post(
            "/login", data={"username": "admin", "password": "PantauNms2026"}
        )

    def test_incidents_groups_active_alerts(self):
        conn, c = m.get_db()
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        c.executemany(
            "INSERT INTO alert_history (host, alert_type, severity, message, triggered_at) VALUES (?, ?, ?, ?, ?)",
            [
                ("OLT:CORE-1", "fiber", "high", "olt down", ts),
                ("OLT:CORE-1", "fiber", "warning", "ont degraded", ts),
            ],
        )
        conn.commit()
        conn.close()
        try:
            r = self.client.get("/api/alerts/incidents", headers=XRW_HDR)
            self.assertEqual(r.status_code, 200)
            incident = next(
                i for i in r.get_json()["incidents"] if i["key"] == "OLT:CORE-1"
            )
            self.assertEqual(incident["count"], 2)
            self.assertEqual(incident["severity"], "high")
        finally:
            conn, c = m.get_db()
            c.execute("DELETE FROM alert_history WHERE host=?", ("OLT:CORE-1",))
            conn.commit()
            conn.close()


class AlertWorkflowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = m.app.test_client()
        cls.client.post(
            "/login", data={"username": "admin", "password": "PantauNms2026"}
        )

    def test_ack_assign_note(self):
        conn, c = m.get_db()
        c.execute(
            "INSERT INTO alert_history (host, alert_type, severity, message, triggered_at) VALUES (?, ?, ?, ?, ?)",
            (
                "10.99.99.13",
                "test",
                "high",
                "workflow test",
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
        alert_id = c.lastrowid
        conn.commit()
        conn.close()
        try:
            r = self.client.patch(
                f"/api/alerts/{alert_id}/workflow",
                json={
                    "acknowledged": True,
                    "assigned_to": "operator",
                    "note": "ditangani",
                },
                headers=JSON_HDR,
            )
            self.assertEqual(r.status_code, 200)
            conn, c = m.get_db()
            c.execute("SELECT * FROM alert_history WHERE id=?", (alert_id,))
            row = c.fetchone()
            conn.close()
            self.assertEqual(row["acknowledged_by"], "admin")
            self.assertEqual(row["assigned_to"], "operator")
            self.assertEqual(row["note"], "ditangani")
        finally:
            conn, c = m.get_db()
            c.execute("DELETE FROM alert_history WHERE id=?", (alert_id,))
            conn.commit()
            conn.close()


class PublicStatusTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = m.app.test_client()
        r = cls.client.post(
            "/login", data={"username": "admin", "password": "PantauNms2026"}
        )
        assert r.status_code == 302, f"login gagal, status={r.status_code}"
        cls.anon = m.app.test_client()
        conn, c = m.get_db()
        c.execute(
            "INSERT OR IGNORE INTO hosts (ip, alias) VALUES (?, ?)",
            (PUB_HOST_ALIAS, "Router Uji"),
        )
        c.execute("INSERT OR IGNORE INTO hosts (ip) VALUES (?)", (PUB_HOST_BARE,))
        c.execute(
            "INSERT INTO ping_logs (host,latency,packet_loss,timestamp)"
            " VALUES (?,?,?,datetime('now','localtime'))",
            (PUB_HOST_ALIAS, 5.0, 0),
        )
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
        self.assertEqual(
            set(j.keys()), {"generated_at", "summary", "nodes", "services"}
        )
        names = [n["name"] for n in j["nodes"]]
        self.assertIn("Router Uji", names)
        self.assertTrue(any(n.startswith("node-") for n in names), names)
        self.assertTrue(
            all(
                set(n.keys()) <= {"name", "status", "uptime_24h", "avg_ms", "note"}
                for n in j["nodes"]
            )
        )
        self.assertTrue(
            all(
                set(s.keys()) == {"name", "type", "status", "uptime_24h"}
                for s in j["services"]
            )
        )

    def test_api_privat_tetap_401(self):
        self.assertEqual(self.anon.get("/api/stats").status_code, 401)
        self.assertEqual(self.anon.get("/api/hosts").status_code, 401)


if __name__ == "__main__":
    unittest.main()
