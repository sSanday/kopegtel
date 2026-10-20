"""Uji halaman /latest-data dan API /api/latest-data (gaya Zabbix).

Item berasal dari data nyata: device_health (SNMP), agent_metrics (agent),
dan ping_logs (latensi). Nilai terakhir beserta delta terhadap baris
sebelumnya; baris ping gagal (latency -1) dilewati.
"""

import os
import tempfile
import unittest
from datetime import datetime, timedelta

_tmp = tempfile.mkdtemp(prefix="nms_test_latest_")
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
TEST_IPS = ("10.78.0.1", "10.78.0.2")


def _stamp(dt):
    return dt.strftime(FMT)


class LatestPageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        m.DB_PATH = os.environ["NMS_DB_PATH"]
        m.init_db()
        cls.client = m.app.test_client()
        conn, c = m.get_db()
        c.execute(
            "INSERT OR IGNORE INTO hosts (ip, alias, category) VALUES (?,?,?)",
            ("10.78.0.1", "Switch Utama", "Core"),
        )
        c.execute(
            "INSERT OR IGNORE INTO hosts (ip, alias, category) VALUES (?,?,?)",
            ("10.78.0.2", "", "Client"),
        )
        conn.commit()
        conn.close()
        r = cls.client.post(
            "/login", data={"username": "admin", "password": "PantauNms2026"}
        )
        assert r.status_code == 302, f"login gagal, status={r.status_code}"

    def setUp(self):
        now = datetime.now()
        conn, c = m.get_db()
        # buang host bawaan seed agar hitungan test pasti
        c.execute("DELETE FROM hosts WHERE ip NOT IN ('10.78.0.1','10.78.0.2')")
        for tbl in ("device_health", "agent_metrics", "ping_logs"):
            c.execute(f"DELETE FROM {tbl} WHERE host LIKE '10.78.%'")
        conn.commit()
        conn.close()

    def get_json(self, qs=""):
        r = self.client.get("/api/latest-data" + qs)
        self.assertEqual(r.status_code, 200)
        return r.get_json()

    def find(self, data, ip, name):
        for d in data["items"]:
            if d["ip"] == ip and d["name"] == name:
                return d
        return None

    # ---------- halaman ----------

    def test_halaman_latest_data_200(self):
        r = self.client.get("/latest-data")
        self.assertEqual(r.status_code, 200)
        body = r.get_data(as_text=True)
        self.assertIn("Latest data", body)
        self.assertIn('href="/latest-data" class="nav-link nav-sub-link active"', body)
        # stub tidak boleh tersisa di sidebar maupun teks lain
        self.assertNotIn("Halaman Latest Data dibangun", body)
        self.assertNotIn("nav-stub", body)

    def test_api_butuh_login(self):
        r = m.app.test_client().get("/api/latest-data")
        self.assertIn(r.status_code, (302, 401))

    # ---------- item SNMP ----------

    def test_item_snmp_dengan_delta(self):
        now = datetime.now()
        conn, c = m.get_db()
        c.execute(
            "INSERT INTO device_health (host, cpu, mem_used, storage_used, temp_c, timestamp)"
            " VALUES (?,?,?,?,?,?)",
            ("10.78.0.1", 10.0, 20.0, 30.0, 40.0, _stamp(now)),
        )
        c.execute(
            "INSERT INTO device_health (host, cpu, mem_used, storage_used, temp_c, timestamp)"
            " VALUES (?,?,?,?,?,?)",
            ("10.78.0.1", 8.0, 21.0, 29.0, 40.0, _stamp(now - timedelta(minutes=5))),
        )
        conn.commit()
        conn.close()
        data = self.get_json()
        cpu = self.find(data, "10.78.0.1", "CPU load")
        mem = self.find(data, "10.78.0.1", "Memory used")
        sto = self.find(data, "10.78.0.1", "Storage used")
        tem = self.find(data, "10.78.0.1", "Temperature")
        self.assertIsNotNone(cpu)
        self.assertEqual(cpu["value"], 10.0)
        self.assertEqual(cpu["delta"], 2.0)
        self.assertEqual(cpu["prev"], 8.0)
        self.assertEqual(cpu["unit"], "%")
        self.assertEqual(cpu["source"], "snmp")
        self.assertEqual(mem["delta"], -1.0)
        self.assertEqual(sto["delta"], 1.0)
        self.assertEqual(tem["delta"], 0.0)
        self.assertEqual(data["total"], 4)

    def test_host_tanpa_data_kosong(self):
        data = self.get_json("?host=10.78.0.2")
        self.assertEqual(data["count"], 0)
        self.assertEqual(data["items"], [])

    # ---------- item agent ----------

    def test_item_agent_dengan_unit_bandwidth(self):
        now = datetime.now()
        conn, c = m.get_db()
        c.execute(
            "INSERT INTO agent_metrics (host, cpu_percent, ram_percent,"
            " disk_percent, net_in, net_out, timestamp)"
            " VALUES (?,?,?,?,?,?,?)",
            ("10.78.0.1", 25.5, 55.0, 60.0, 123.4, 456.7, _stamp(now)),
        )
        conn.commit()
        conn.close()
        data = self.get_json("?source=agent")
        self.assertEqual(data["count"], 5)
        cpu = self.find(data, "10.78.0.1", "CPU (agent)")
        nin = self.find(data, "10.78.0.1", "Net in (agent)")
        self.assertIsNotNone(cpu)
        self.assertEqual(cpu["value"], 25.5)
        self.assertEqual(cpu["delta"], None)  # tak ada baris sebelumnya
        self.assertEqual(cpu["source"], "agent")
        self.assertEqual(nin["unit"], "KB/s")
        self.assertEqual(nin["value"], 123.4)
        self.assertEqual(
            self.find(data, "10.78.0.1", "Net out (agent)")["value"], 456.7
        )

    # ---------- item ping ----------

    def test_item_ping_dan_baris_gagal_dilewati(self):
        now = datetime.now()
        conn, c = m.get_db()
        c.execute(
            "INSERT INTO ping_logs (host, latency, timestamp) VALUES (?,?,?)",
            ("10.78.0.1", 12.5, _stamp(now)),
        )
        c.execute(
            "INSERT INTO ping_logs (host, latency, timestamp) VALUES (?,?,?)",
            ("10.78.0.1", 10.0, _stamp(now - timedelta(minutes=1))),
        )
        conn.commit()
        conn.close()
        data = self.get_json("?source=ping")
        self.assertEqual(data["count"], 1)
        p = self.find(data, "10.78.0.1", "Ping latency")
        self.assertEqual(p["value"], 12.5)
        self.assertEqual(p["delta"], 2.5)
        self.assertEqual(p["unit"], "ms")
        self.assertEqual(p["source"], "ping")

        # baris gagal (latency -1 seperti pada /api/history) bukan item valid
        conn, c = m.get_db()
        c.execute("DELETE FROM ping_logs WHERE host='10.78.0.1'")
        c.execute(
            "INSERT INTO ping_logs (host, latency, timestamp) VALUES (?,?,?)",
            ("10.78.0.1", -1, _stamp(now)),
        )
        conn.commit()
        conn.close()
        self.assertEqual(self.get_json("?source=ping")["count"], 0)

    # ---------- filter ----------

    def test_filter_nama_host_dan_group(self):
        now = datetime.now()
        conn, c = m.get_db()
        c.execute(
            "INSERT INTO device_health (host, cpu, timestamp) VALUES (?,?,?)",
            ("10.78.0.1", 10.0, _stamp(now)),
        )
        c.execute(
            "INSERT INTO device_health (host, cpu, timestamp) VALUES (?,?,?)",
            ("10.78.0.2", 50.0, _stamp(now)),
        )
        conn.commit()
        conn.close()
        data = self.get_json("?name=cpu")
        self.assertEqual([d["name"] for d in data["items"]], ["CPU load", "CPU load"])
        data = self.get_json("?host=Switch")
        self.assertEqual({d["ip"] for d in data["items"]}, {"10.78.0.1"})
        data = self.get_json("?group=Client")
        self.assertEqual({d["ip"] for d in data["items"]}, {"10.78.0.2"})
        # kategori/host tersedia untuk datalist
        self.assertIn("Core", self.get_json()["categories"])
        self.assertIn("Switch Utama", self.get_json()["hosts"])

    def test_filter_sumber(self):
        now = datetime.now()
        conn, c = m.get_db()
        c.execute(
            "INSERT INTO device_health (host, cpu, timestamp) VALUES (?,?,?)",
            ("10.78.0.1", 10.0, _stamp(now)),
        )
        c.execute(
            "INSERT INTO agent_metrics (host, cpu_percent, timestamp) VALUES (?,?,?)",
            ("10.78.0.1", 30.0, _stamp(now)),
        )
        conn.commit()
        conn.close()
        snmp = self.get_json("?source=snmp")
        self.assertEqual({d["source"] for d in snmp["items"]}, {"snmp"})
        self.assertEqual(snmp["count"], 1)  # hanya CPU tersimpan; lainnya NULL dilewati
        agent = self.get_json("?source=agent")
        self.assertEqual({d["source"] for d in agent["items"]}, {"agent"})
        # disk/net punya DEFAULT 0.0 di skema, jadi ikut jadi item
        self.assertEqual(agent["count"], 4)
        # sumber tak dikenal kembali ke "all"
        semua = self.get_json("?source=aneh")
        self.assertEqual(semua["count"], 5)

    # ---------- urutan ----------

    def test_sort_nama_dan_check(self):
        now = datetime.now()
        lama = now - timedelta(hours=2)
        conn, c = m.get_db()
        c.execute(
            "INSERT INTO device_health (host, cpu, timestamp) VALUES (?,?,?)",
            ("10.78.0.1", 10.0, _stamp(now)),
        )
        c.execute(
            "INSERT INTO device_health (host, cpu, timestamp) VALUES (?,?,?)",
            ("10.78.0.2", 50.0, _stamp(lama)),
        )
        conn.commit()
        conn.close()
        desc = self.get_json("?sort=name_desc")
        names = [d["name"] for d in desc["items"]]
        self.assertEqual(names, sorted(names, key=str.lower, reverse=True))
        terbaru = self.get_json("?sort=check_desc")
        self.assertEqual(terbaru["items"][0]["check"], _stamp(now))
        terlama = self.get_json("?sort=check_asc")
        self.assertEqual(terlama["items"][0]["check"], _stamp(lama))
        # sort tak dikenal kembali ke name_asc
        bawaan = self.get_json("?sort=abcdef")
        self.assertEqual(
            [d["name"] for d in bawaan["items"]][:2],
            ["CPU load", "CPU load"],
        )


if __name__ == "__main__":
    unittest.main()
