import os
import tempfile
import unittest
from datetime import datetime, timedelta

_tmp = tempfile.mkdtemp(prefix="nms_test_sla_")
os.environ.setdefault("NMS_DB_PATH", os.path.join(_tmp, "test.db"))
os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("DASHBOARD_USERNAME", "admin")
os.environ.setdefault("DASHBOARD_PASSWORD", "PantauNms2026")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "")
os.environ.setdefault("TELEGRAM_CHAT_ID", "")
os.environ.setdefault("AGENT_API_KEY", "test-agent-key")
os.environ.setdefault("NMS_DISABLE_SCHEDULER", "1")
os.environ.setdefault("NMS_DISABLE_LIMITER", "1")
os.environ.setdefault("NMS_DISABLE_TRAP_RECEIVER", "1")

import app as m  # noqa: E402

try:
    m.scheduler.shutdown(wait=False)
except Exception:
    pass

JSON_HDR = {"Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest"}


class SlaApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        m.DB_PATH = os.environ["NMS_DB_PATH"]
        m.init_db()
        m._seed_admin_from_env()
        cls.client = m.app.test_client()
        r = cls.client.post(
            "/login", data={"username": "admin", "password": "PantauNms2026"}
        )
        assert r.status_code == 302, f"login gagal: {r.status_code}"

    def setUp(self):
        conn, c = m.get_db()
        c.execute("DELETE FROM sla_tags")
        c.execute("DELETE FROM sla_downtimes")
        c.execute("DELETE FROM slas")
        c.execute("DELETE FROM service_history")
        c.execute("DELETE FROM services")
        conn.commit()
        conn.close()

    def tearDown(self):
        conn, c = m.get_db()
        c.execute("DELETE FROM sla_tags")
        c.execute("DELETE FROM sla_downtimes")
        c.execute("DELETE FROM slas")
        c.execute("DELETE FROM service_history")
        c.execute("DELETE FROM services")
        conn.commit()
        conn.close()

    def _service(self, name="Svc", tags="app: kopegtel"):
        conn, c = m.get_db()
        c.execute(
            "INSERT INTO services (ip, name, type, tags, created_at) VALUES (?, ?, ?, ?, ?)",
            ("10.10.10.10", name, "icmp", tags, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        sid = c.lastrowid
        conn.commit()
        conn.close()
        return sid

    def _payload(self, **overrides):
        payload = {
            "name": "Kopegtel 99.5 bulanan",
            "slo": 99.5,
            "reporting_period": "monthly",
            "timezone": "Asia/Jakarta",
            "schedule": {"mode": "24x7"},
            "effective_date": (datetime.now().date() - timedelta(days=1)).isoformat(),
            "tags": [{"name": "app", "op": "equals", "value": "kopegtel"}],
            "downtimes": [],
            "enabled": True,
        }
        payload.update(overrides)
        return payload

    def _create(self, **overrides):
        return self.client.post("/api/sla", json=self._payload(**overrides), headers=JSON_HDR)

    def _history(self, service_id, start, statuses):
        conn, c = m.get_db()
        for index, status in enumerate(statuses):
            c.execute(
                "INSERT INTO service_history (service_id, status, latency, timestamp) VALUES (?, ?, ?, ?)",
                (
                    service_id,
                    status,
                    1.0,
                    (start + timedelta(minutes=index)).strftime("%Y-%m-%d %H:%M:%S"),
                ),
            )
        conn.commit()
        conn.close()

    def test_crud_dan_nama_unik(self):
        sid = self._service()
        self.assertGreater(sid, 0)
        r = self._create()
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        sla = r.get_json()["sla"]
        self.assertEqual(sla["slo"], 99.5)
        self.assertEqual(sla["tags"][0]["value"], "kopegtel")

        duplicate = self._create(name="KOPEGTEL 99.5 BULANAN")
        self.assertEqual(duplicate.status_code, 409)

        listing = self.client.get("/api/sla", headers=JSON_HDR)
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(listing.get_json()["count"], 1)
        self.assertEqual(listing.get_json()["slas"][0]["matched_services"], 1)

        updated = self.client.put(
            f"/api/sla/{sla['id']}",
            json=self._payload(
                name="Kopegtel 99.9 jam kerja",
                slo=99.9,
                schedule={
                    "mode": "custom",
                    "days": {"monday": [["08:00", "17:00"]]},
                },
            ),
            headers=JSON_HDR,
        )
        self.assertEqual(updated.status_code, 200, updated.get_data(as_text=True))
        self.assertEqual(updated.get_json()["sla"]["schedule_label"], "Mon 08:00-17:00")

        toggled = self.client.post(
            f"/api/sla/{sla['id']}/enabled",
            json={"enabled": False},
            headers=JSON_HDR,
        )
        self.assertEqual(toggled.status_code, 200)
        self.assertFalse(toggled.get_json()["enabled"])

        deleted = self.client.delete(f"/api/sla/{sla['id']}", headers=JSON_HDR)
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(self.client.get("/api/sla", headers=JSON_HDR).get_json()["count"], 0)

    def test_validasi_payload(self):
        self._service()
        cases = [
            self._payload(tags=[]),
            self._payload(slo=101),
            self._payload(timezone="Mars/Olympus"),
            self._payload(schedule={"mode": "custom", "days": {"monday": [["17:00", "08:00"]]}}),
            self._payload(
                downtimes=[
                    {"name": "A", "start": "2026-10-01 10:00", "duration_seconds": 3600},
                    {"name": "B", "start": "2026-10-01 10:30", "duration_seconds": 1800},
                ]
            ),
        ]
        for payload in cases:
            r = self.client.post("/api/sla", json=payload, headers=JSON_HDR)
            self.assertEqual(r.status_code, 400, payload)

    def test_report_no_services_dan_no_data(self):
        r = self._create()
        sla_id = r.get_json()["sla"]["id"]
        report = self.client.get(f"/api/sla/{sla_id}/report", headers=JSON_HDR).get_json()
        self.assertEqual(report["status"], "no_services")
        self.assertIsNone(report["sli"])

        self._service()
        report = self.client.get(f"/api/sla/{sla_id}/report", headers=JSON_HDR).get_json()
        self.assertEqual(report["status"], "no_data")
        self.assertIsNone(report["sli"])
        self.assertNotEqual(report["sli"], 100)

    def test_report_sli_dan_error_budget(self):
        sid = self._service()
        now = datetime.now().replace(second=0, microsecond=0)
        start = now - timedelta(minutes=120)
        statuses = ["ONLINE"] * 60 + ["OFFLINE"] * 60
        self._history(sid, start, statuses)
        r = self._create()
        sla_id = r.get_json()["sla"]["id"]
        report = self.client.get(f"/api/sla/{sla_id}/report", headers=JSON_HDR).get_json()
        self.assertEqual(report["services_count"], 1)
        self.assertEqual(report["status"], "breached")
        self.assertAlmostEqual(report["sli"], 50.0, delta=1.0)
        self.assertGreater(report["downtime_seconds"], 0)
        self.assertLess(report["remaining_budget_seconds"], 0)
        self.assertAlmostEqual(
            report["allowed_downtime_seconds"],
            report["tracked_seconds"] * 0.005,
            delta=1.0,
        )

    def test_excluded_downtime(self):
        sid = self._service()
        now = datetime.now().replace(second=0, microsecond=0)
        start = now - timedelta(minutes=60)
        self._history(sid, start, ["ONLINE"] * 30 + ["OFFLINE"] * 30)
        r = self._create(
            downtimes=[
                {
                    "name": "Maintenance",
                    "start": (start + timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M"),
                    "duration_seconds": 3600,
                }
            ]
        )
        sla_id = r.get_json()["sla"]["id"]
        report = self.client.get(f"/api/sla/{sla_id}/report", headers=JSON_HDR).get_json()
        self.assertEqual(report["status"], "met")
        self.assertAlmostEqual(report["sli"], 100.0, delta=0.01)
        self.assertLess(report["downtime_seconds"], 1)

    def test_custom_schedule_dan_effective_date(self):
        sid = self._service()
        today = datetime.now().date()
        start = datetime.combine(today, datetime.min.time())
        # Data di luar jadwal tidak boleh dihitung.
        self._history(sid, start + timedelta(hours=5), ["OFFLINE"] * 60)
        self._history(sid, start, ["ONLINE"] * 60)
        weekday = [
            "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"
        ][today.weekday()]
        r = self._create(
            schedule={"mode": "custom", "days": {weekday: [["00:00", "01:00"]]}}
        )
        sla_id = r.get_json()["sla"]["id"]
        query = f"?from={today.isoformat()}&to={today.isoformat()}"
        report = self.client.get(f"/api/sla/{sla_id}/report{query}", headers=JSON_HDR).get_json()
        self.assertEqual(report["status"], "met")
        self.assertAlmostEqual(report["sli"], 100.0, delta=0.01)

        future = self._create(
            name="Future SLA",
            effective_date=(today + timedelta(days=2)).isoformat(),
        )
        future_id = future.get_json()["sla"]["id"]
        future_report = self.client.get(
            f"/api/sla/{future_id}/report{query}", headers=JSON_HDR
        ).get_json()
        self.assertEqual(future_report["status"], "not_started")
        self.assertIsNone(future_report["sli"])

    def test_rentang_tanggal_inklusif(self):
        self._service()
        created = self._create(effective_date="2025-01-01")
        sla_id = created.get_json()["sla"]["id"]
        report = self.client.get(
            f"/api/sla/{sla_id}/report?from=2025-01-01&to=2025-01-31",
            headers=JSON_HDR,
        ).get_json()
        self.assertEqual(report["from"], "2025-01-01")
        self.assertEqual(report["to"], "2025-01-31")

    def test_jalur_sql_sama_dengan_python(self):
        from nms import sla as sla_engine

        sid = self._service()
        start = datetime(2026, 10, 1, 0, 0)
        end = start + timedelta(hours=3)
        statuses = ["ONLINE"] * 40 + ["OFFLINE"] * 20 + ["ONLINE"] * 30
        conn, c = m.get_db()
        for index, status in enumerate(statuses):
            offset = index * 2
            if index >= 40:
                offset += 15  # celah 15 menit, melebihi MAX_SAMPLE_GAP
            ts = start + timedelta(minutes=offset)
            if ts > end:
                break
            c.execute(
                "INSERT INTO service_history (service_id, status, latency, timestamp) "
                "VALUES (?, ?, ?, ?)",
                (sid, status, 1.0, ts.strftime("%Y-%m-%d %H:%M:%S")),
            )
        conn.commit()
        c.execute(
            "SELECT service_id, status, timestamp FROM service_history "
            "WHERE service_id = ? ORDER BY timestamp",
            (sid,),
        )
        rows = [dict(row) for row in c.fetchall()]
        sql_totals = sla_engine.sql_totals(c, [sid], start, end)
        conn.close()
        expected = sla_engine.compute_service_seconds(rows, [(start, end)], end)
        actual = sql_totals[sid]
        for index in range(3):
            self.assertAlmostEqual(actual[index], expected[index], delta=0.6)

    def test_halaman_report_dan_auth(self):
        page = self.client.get("/services/sla/report")
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        for marker in (
            'id="slaReportSummary"',
            'id="slaReportBody"',
            'id="slaReportName"',
            'id="slaReportSli"',
            'id="slaReportBudget"',
            'id="slaReportInfo"',
        ):
            self.assertIn(marker, html)
        anonymous = m.app.test_client()
        self.assertEqual(anonymous.get("/api/sla").status_code, 401)
        self.assertEqual(anonymous.get("/api/sla/1/report").status_code, 401)


if __name__ == "__main__":
    unittest.main()
