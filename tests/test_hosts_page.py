"""Uji halaman /hosts dan API /api/hosts/zbx (gaya Zabbix).

Availability: ping (memori status), SNMP (kebiasaan device_health),
agent (kebiasaan agent_metrics). Filter: name, ip, group, severity,
status, only_problems, sort. Juga uji tombol Tambah host (POST /api/hosts).
"""

import os
import tempfile
import unittest
from datetime import datetime, timedelta

_tmp = tempfile.mkdtemp(prefix="nms_test_hosts_")
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

FMT = "%Y-%m-%d %H:%M:%S"
TEST_IPS = ("10.77.0.1", "10.77.0.2", "10.77.0.3")


def _stamp(dt):
    return dt.strftime(FMT)


class HostsPageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        m.DB_PATH = os.environ["NMS_DB_PATH"]
        m.init_db()
        cls.client = m.app.test_client()
        conn, c = m.get_db()
        c.execute(
            "INSERT OR IGNORE INTO hosts (ip, alias, category) VALUES (?,?,?)",
            ("10.77.0.1", "Router Utama", "Core"),
        )
        c.execute(
            "INSERT OR IGNORE INTO hosts (ip, alias, category) VALUES (?,?,?)",
            ("10.77.0.2", "", "Client"),
        )
        c.execute(
            "INSERT OR IGNORE INTO hosts (ip, alias, category, snmp_community, snmp_profile)"
            " VALUES (?,?,?,?,?)",
            ("10.77.0.3", "Switch IDF", "Client", "public", "auto"),
        )
        conn.commit()
        conn.close()
        r = cls.client.post(
            "/login", data={"username": "admin", "password": "PantauNms2026"}
        )
        assert r.status_code == 302, f"login gagal, status={r.status_code}"
        r2 = cls.client.get(r.headers.get("Location", "/"))
        assert r2.status_code == 200, f"sesudah login gagal, status={r2.status_code}"

    def setUp(self):
        now = datetime.now()
        conn, c = m.get_db()
        # buang host bawaan seed > agar hitungan test pasti
        c.execute(
            "DELETE FROM hosts WHERE ip NOT IN" " ('10.77.0.1','10.77.0.2','10.77.0.3')"
        )
        for tbl in (
            "alert_history",
            "device_health",
            "agent_metrics",
            "maintenance_windows",
        ):
            c.execute(f"DELETE FROM {tbl} WHERE host LIKE '10.77.%'")
        # baris non-tes di alert_history dibersihkan utuh supaya hitungan pasti
        c.execute("DELETE FROM alert_history")
        conn.commit()
        conn.close()
        for ip in TEST_IPS:
            try:
                m.status_memory.pop(ip, None)
            except Exception:
                pass

    def get_json(self, qs=""):
        r = self.client.get("/api/hosts/zbx" + qs)
        self.assertEqual(r.status_code, 200)
        return r.get_json()

    def by_ip(self, data, ip):
        for d in data["hosts"]:
            if d["ip"] == ip:
                return d
        return None

    # ---------- halaman ----------

    def test_halaman_hosts_200(self):
        r = self.client.get("/hosts")
        self.assertEqual(r.status_code, 200)
        body = r.get_data(as_text=True)
        self.assertIn("Hosts", body)
        self.assertIn("Tambah host", body)
        self.assertIn('href="/hosts" class="nav-link nav-sub-link active"', body)
        # stub Hosts tidak boleh tersisa (stub Ke terkini memang masih ada)
        self.assertNotIn("Halaman Hosts dibangun", body)

    def test_api_butuh_login(self):
        r = m.app.test_client().get("/api/hosts/zbx")
        self.assertIn(r.status_code, (302, 401))

    # ---------- daftar & urutan ----------

    def test_daftar_dan_sort_nama(self):
        data = self.get_json()
        self.assertEqual(data["total"], 3)
        names = [d["name"] for d in data["hosts"]]
        self.assertEqual(names, ["10.77.0.2", "Router Utama", "Switch IDF"])
        ping_all = {d["ip"]: d["ping"] for d in data["hosts"]}
        self.assertEqual(ping_all, {ip: "unknown" for ip in TEST_IPS})

    def test_sort_name_desc(self):
        data = self.get_json("?sort=name_desc")
        names = [d["name"] for d in data["hosts"]]
        self.assertEqual(names, ["Switch IDF", "Router Utama", "10.77.0.2"])

    # ---------- availability ----------

    def test_ping_up_down_dari_memori_status(self):
        m.status_memory["10.77.0.1"] = True
        m.status_memory["10.77.0.2"] = False
        data = self.get_json()
        self.assertEqual(self.by_ip(data, "10.77.0.1")["ping"], "up")
        self.assertEqual(self.by_ip(data, "10.77.0.2")["ping"], "down")
        self.assertEqual(self.by_ip(data, "10.77.0.3")["ping"], "unknown")

    def test_badge_snmp_dari_device_health(self):
        now = datetime.now()
        conn, c = m.get_db()
        # host2 tanpa snmp -> none; host3 snmp + data baru -> ok; host1 tanpa snmp -> none
        c.execute(
            "INSERT INTO device_health (host, cpu, timestamp) VALUES (?,?,?)",
            ("10.77.0.3", 12.5, _stamp(now)),
        )
        conn.commit()
        conn.close()
        data = self.get_json()
        self.assertEqual(self.by_ip(data, "10.77.0.3")["snmp"], "ok")
        self.assertEqual(self.by_ip(data, "10.77.0.3")["snmp_monitored"], True)
        self.assertEqual(self.by_ip(data, "10.77.0.2")["snmp"], "none")
        self.assertEqual(self.by_ip(data, "10.77.0.1")["snmp"], "none")

        # data basi (> 120 menit) -> stale
        conn, c = m.get_db()
        c.execute("DELETE FROM device_health WHERE host='10.77.0.3'")
        c.execute(
            "INSERT INTO device_health (host, cpu, timestamp) VALUES (?,?,?)",
            ("10.77.0.3", 12.5, _stamp(now - timedelta(minutes=200))),
        )
        conn.commit()
        conn.close()
        data = self.get_json()
        self.assertEqual(self.by_ip(data, "10.77.0.3")["snmp"], "stale")

    def test_badge_agent_dari_agent_metrics(self):
        now = datetime.now()
        conn, c = m.get_db()
        c.execute(
            "INSERT INTO agent_metrics (host, cpu_percent, timestamp) VALUES (?,?,?)",
            ("10.77.0.1", 30.0, _stamp(now - timedelta(minutes=1))),
        )
        conn.commit()
        conn.close()
        self.assertEqual(self.by_ip(self.get_json(), "10.77.0.1")["agent"], "ok")
        conn, c = m.get_db()
        c.execute("DELETE FROM agent_metrics WHERE host='10.77.0.1'")
        c.execute(
            "INSERT INTO agent_metrics (host, cpu_percent, timestamp) VALUES (?,?,?)",
            ("10.77.0.1", 30.0, _stamp(now - timedelta(minutes=30))),
        )
        conn.commit()
        conn.close()
        self.assertEqual(self.by_ip(self.get_json(), "10.77.0.1")["agent"], "stale")

    def test_latest_count(self):
        now = datetime.now()
        conn, c = m.get_db()
        c.execute(
            "INSERT INTO device_health (host, cpu, timestamp) VALUES (?,?,?)",
            ("10.77.0.3", 12.5, _stamp(now)),
        )
        c.execute(
            "INSERT INTO device_health (host, cpu, timestamp) VALUES (?,?,?)",
            ("10.77.0.3", 13.5, _stamp(now - timedelta(hours=1))),
        )
        conn.commit()
        conn.close()
        self.assertEqual(self.by_ip(self.get_json(), "10.77.0.3")["latest_count"], 2)

    # ---------- masalah & filter ----------

    def test_problems_dan_filter_severity(self):
        now = datetime.now()
        conn, c = m.get_db()
        c.execute(
            "INSERT INTO alert_history (host, alert_type, severity, message, triggered_at)"
            " VALUES (?,?,?,?,?)",
            (
                "Router Utama (10.77.0.1)",
                "resource",
                "disaster",
                "High RAM",
                _stamp(now),
            ),
        )
        conn.commit()
        conn.close()
        data = self.get_json()
        h1 = self.by_ip(data, "10.77.0.1")
        self.assertEqual(h1["problems_count"], 1)
        self.assertEqual(h1["worst_severity"], "disaster")
        self.assertEqual(self.by_ip(data, "10.77.0.2")["problems_count"], 0)

        only = self.get_json("?only_problems=1")
        self.assertEqual([d["ip"] for d in only["hosts"]], ["10.77.0.1"])

        sev = self.get_json("?severity=disaster")
        self.assertEqual([d["ip"] for d in sev["hosts"]], ["10.77.0.1"])
        sev2 = self.get_json("?severity=warning")
        self.assertEqual(sev2["hosts"], [])

        most = self.get_json("?sort=problems_desc")
        self.assertEqual(most["hosts"][0]["ip"], "10.77.0.1")

    def test_filter_status_maintenance(self):
        now = datetime.now()
        conn, c = m.get_db()
        c.execute(
            "INSERT INTO maintenance_windows (host, start_at, end_at, reason, created_by, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (
                "10.77.0.1",
                _stamp(now - timedelta(hours=1)),
                _stamp(now + timedelta(hours=1)),
                "upgrade firmware",
                "admin",
                _stamp(now),
            ),
        )
        conn.commit()
        conn.close()
        maint = self.get_json("?status=maintenance")
        self.assertEqual([d["ip"] for d in maint["hosts"]], ["10.77.0.1"])
        self.assertEqual(maint["hosts"][0]["maintenance_reason"], "upgrade firmware")
        normal = self.get_json("?status=normal")
        self.assertEqual({d["ip"] for d in normal["hosts"]}, {"10.77.0.2", "10.77.0.3"})

    def test_filter_name_ip_group(self):
        data = self.get_json("?name=Router")
        self.assertEqual([d["ip"] for d in data["hosts"]], ["10.77.0.1"])
        data = self.get_json("?ip=0.2")
        self.assertEqual([d["ip"] for d in data["hosts"]], ["10.77.0.2"])
        data = self.get_json("?group=Client")
        self.assertEqual({d["ip"] for d in data["hosts"]}, {"10.77.0.2", "10.77.0.3"})
        # kategori tersedia untuk datalist
        self.assertIn("Core", self.get_json()["categories"])

    # ---------- tambah host ----------

    def test_tambah_host_via_post(self):
        r = self.client.post(
            "/api/hosts",
            json={"ip": "10.77.9.9", "alias": "Host Baru", "category": "Tg"},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json().get("status"), "success")
        data = self.get_json()
        baru = self.by_ip(data, "10.77.9.9")
        self.assertIsNotNone(baru)
        self.assertEqual(baru["name"], "Host Baru")
        self.assertEqual(baru["category"], "Tg")
        # duplikat ditolak
        r2 = self.client.post(
            "/api/hosts",
            json={"ip": "10.77.9.9"},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        self.assertEqual(r2.status_code, 400)


if __name__ == "__main__":
    unittest.main()
