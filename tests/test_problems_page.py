"""Uji halaman /problems dan API /api/problems (gaya Zabbix).

Sumber data: tabel alert_history. Filter yang diuji: show, severity,
host, problem, type, ack, by_me, age_less_days, sort, format=csv.
"""

import os
import tempfile
import unittest
from datetime import datetime, timedelta

_tmp = tempfile.mkdtemp(prefix="nms_test_problems_")
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


def _stamp(dt):
    return dt.strftime(FMT)


class ProblemsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        m.DB_PATH = os.environ["NMS_DB_PATH"]
        m.init_db()
        cls.client = m.app.test_client()
        conn, c = m.get_db()
        c.execute("INSERT OR IGNORE INTO hosts (ip) VALUES (?)", ("10.88.88.88",))
        conn.commit()
        conn.close()
        r = cls.client.post(
            "/login", data={"username": "admin", "password": "PantauNms2026"}
        )
        assert r.status_code == 302, f"login gagal, status={r.status_code}"
        r2 = cls.client.get(r.headers.get("Location", "/"))
        assert r2.status_code == 200, f"sesudah login gagal, status={r2.status_code}"

    def setUp(self):
        conn, c = m.get_db()
        c.execute("DELETE FROM alert_history")
        now = datetime.now()
        data = [
            # host, type, severity, message, triggered, resolved
            (
                "10.0.0.1",
                "availability",
                "warning",
                "Host is DOWN (Unreachable)",
                now - timedelta(hours=1),
                None,
            ),
            (
                "10.0.0.2",
                "resource",
                "disaster",
                "High RAM Usage terdeteksi",
                now - timedelta(minutes=5),
                None,
            ),
            (
                "10.0.0.3",
                "service",
                "info",
                "Service recovered normal",
                now - timedelta(days=40),
                now - timedelta(days=39),
            ),
            (
                "10.0.0.4",
                "fiber",
                "high",
                "Fiber critical: Rx rendah",
                now - timedelta(hours=2),
                None,
            ),
        ]
        for host, typ, sev, msg, trig, res in data:
            c.execute(
                "INSERT INTO alert_history (host, alert_type, severity, message,"
                " triggered_at, resolved_at, acknowledged_at, acknowledged_by)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (
                    host,
                    typ,
                    sev,
                    msg,
                    _stamp(trig),
                    _stamp(res) if res else None,
                    _stamp(now - timedelta(minutes=30)) if host == "10.0.0.4" else None,
                    "admin" if host == "10.0.0.4" else None,
                ),
            )
        conn.commit()
        conn.close()

    # ---------- halaman ----------

    def test_halaman_problems_200(self):
        r = self.client.get("/problems")
        self.assertEqual(r.status_code, 200)
        body = r.get_data(as_text=True)
        self.assertIn("Problems", body)
        self.assertIn("Export to CSV", body)
        self.assertIn('href="/problems" class="nav-link nav-sub-link active"', body)

    def test_api_butuh_login(self):
        r = m.app.test_client().get("/api/problems")
        self.assertIn(r.status_code, (302, 401))

    # ---------- filter show ----------

    def test_show_problems_hanya_aktif(self):
        r = self.client.get("/api/problems?show=problems")
        self.assertEqual(r.status_code, 200)
        rows = r.get_json()["problems"]
        self.assertEqual(len(rows), 3)
        for row in rows:
            self.assertEqual(row["status"], "PROBLEM")
            self.assertIsNotNone(row["id"])

    def test_show_history_semua_status(self):
        rows = self.client.get("/api/problems?show=history").get_json()["problems"]
        self.assertEqual(len(rows), 4)
        status = {row["host"]: row["status"] for row in rows}
        self.assertEqual(status["10.0.0.3"], "RESOLVED")

    def test_show_recent_aktif_plus_pulih_dalam_jendela(self):
        rows = self.client.get("/api/problems?show=recent").get_json()["problems"]
        hosts = {row["host"] for row in rows}
        # pulih 39 hari lalu tidak masuk jendela default 7 hari
        self.assertNotIn("10.0.0.3", hosts)
        self.assertEqual(len(rows), 3)

    def test_recent_memuat_yang_pulih_muda(self):
        conn, c = m.get_db()
        now = datetime.now()
        c.execute(
            "INSERT INTO alert_history (host, alert_type, severity, message,"
            " triggered_at, resolved_at) VALUES (?,?,?,?,?,?)",
            (
                "10.0.0.9",
                "resource",
                "warning",
                "RAM pulih",
                _stamp(now - timedelta(days=2)),
                _stamp(now - timedelta(days=1)),
            ),
        )
        conn.commit()
        conn.close()
        rows = self.client.get("/api/problems?show=recent").get_json()["problems"]
        hosts = {row["host"] for row in rows}
        self.assertIn("10.0.0.9", hosts)

    # ---------- filter lain ----------

    def test_filter_severity(self):
        rows = self.client.get(
            "/api/problems?show=history&severity=warning,disaster"
        ).get_json()["problems"]
        self.assertEqual({row["severity"] for row in rows}, {"warning", "disaster"})
        self.assertEqual(len(rows), 2)

    def test_filter_host_problem_type(self):
        r1 = self.client.get("/api/problems?show=history&host=10.0.0.2")
        self.assertEqual(len(r1.get_json()["problems"]), 1)
        r2 = self.client.get("/api/problems?show=history&problem=High RAM")
        self.assertEqual(len(r2.get_json()["problems"]), 1)
        r3 = self.client.get("/api/problems?show=history&type=fiber")
        self.assertEqual(len(r3.get_json()["problems"]), 1)
        # wildcard SQL tidak boleh lolos: "%" dianggap karakter literal (tidak ada host berisi %)
        r4 = self.client.get("/api/problems?show=history&host=%")
        body = r4.get_json()
        self.assertEqual(len(body["problems"]), 0)

    def test_filter_ack_dan_by_me(self):
        unack = self.client.get("/api/problems?show=history&ack=unack").get_json()[
            "problems"
        ]
        self.assertEqual(len(unack), 3)
        acked = self.client.get("/api/problems?show=history&ack=ack").get_json()[
            "problems"
        ]
        self.assertEqual(len(acked), 1)
        self.assertEqual(acked[0]["acknowledged_by"], "admin")
        self.assertEqual(acked[0]["host"], "10.0.0.4")
        byme = self.client.get("/api/problems?show=history&ack=ack&by_me=1").get_json()[
            "problems"
        ]
        self.assertEqual(len(byme), 1)
        # user lain: by_me tidak cocok
        client2 = m.app.test_client()
        client2.post("/login", data={"username": "admin", "password": "PantauNms2026"})
        # (guna sama admin; skenario user beda cukup diuji oleh nilai acknowledged_by)
        empty = self.client.get(
            "/api/problems?show=history&ack=unack&by_me=1"
        ).get_json()["problems"]
        self.assertEqual(len(empty), 0)

    def test_filter_age_less_days(self):
        rows = self.client.get(
            "/api/problems?show=history&age_less_days=30"
        ).get_json()["problems"]
        self.assertEqual(len(rows), 3)
        self.assertNotIn("10.0.0.3", {row["host"] for row in rows})
        # nilai invalid diabaikan, bukan error
        r = self.client.get("/api/problems?show=history&age_less_days=abc")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.get_json()["problems"]), 4)

    # ---------- urutan ----------

    def test_sort_time_desc_default_dan_asc(self):
        j = self.client.get("/api/problems?show=history")
        self.assertEqual(j.get_json()["problems"][0]["host"], "10.0.0.2")
        asc = self.client.get("/api/problems?show=history&sort=time_asc").get_json()[
            "problems"
        ]
        self.assertEqual(asc[0]["host"], "10.0.0.3")

    def test_update_at_terisi(self):
        rows = self.client.get("/api/problems?show=history").get_json()["problems"]
        for row in rows:
            self.assertTrue(row.get("update_at"))

    # ---------- csv ----------

    def test_csv_export(self):
        r = self.client.get("/api/problems?show=history&format=csv")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.headers["Content-Type"].split(";")[0].strip() == "text/csv")
        self.assertIn("problems-", r.headers.get("Content-Disposition", ""))
        text = r.get_data(as_text=True)
        lines = [ln for ln in text.strip().splitlines()]
        self.assertEqual(
            lines[0],
            "id,time,severity,status,recovery_time,host,problem,duration_s,update,acknowledged_by,acknowledged_at,type,note",
        )
        self.assertEqual(len(lines), 5)  # header + 4 baris

    # ---------- perlindungan umum ----------

    def test_limit_invalid_fallback(self):
        r = self.client.get("/api/problems?show=history&limit=abc")
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(len(body["problems"]), 4)
        self.assertIn("host_options", body)


if __name__ == "__main__":
    unittest.main()
