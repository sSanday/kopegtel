import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

_tmp = tempfile.mkdtemp(prefix="nms_test_services_page_")
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

import app as m
from nms import service_checks as sc

try:
    m.scheduler.shutdown(wait=False)
except Exception:
    pass

JSON_HDR = {"Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest"}
XRW_HDR = {"X-Requested-With": "XMLHttpRequest"}


class _FakeResp:
    def __init__(self, body=b"Dashboard KOPEGTEL", status=200):
        self.status_code = status
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def iter_content(self, chunk_size=8192):
        return iter([self._body])


class ServiceChecksTest(unittest.TestCase):
    def test_tcp_valid_dan_invalid_port(self):
        with mock.patch.object(sc.socket, "create_connection") as conn:
            conn.return_value.__enter__.return_value = object()
            self.assertTrue(sc.check_tcp_service("10.0.0.1", 443))
            self.assertTrue(conn.called)
        self.assertFalse(sc.check_tcp_service("10.0.0.1", 0))
        self.assertFalse(sc.check_tcp_service("10.0.0.1", "bukan-angka"))

    def test_http_keyword_dan_status(self):
        allow = lambda url: True
        with mock.patch.object(sc.requests, "get", return_value=_FakeResp()):
            self.assertTrue(
                sc.check_http_service("https://example.com", "Dashboard", allow)
            )
            self.assertFalse(
                sc.check_http_service("https://example.com", "NotFound", allow)
            )
        with mock.patch.object(sc.requests, "get", return_value=_FakeResp(status=404)):
            self.assertFalse(sc.check_http_service("https://example.com", "", allow))
        self.assertFalse(
            sc.check_http_service("https://example.com", "", lambda url: False)
        )

    def test_http_body_dibatasi_64kb(self):
        allow = lambda url: True
        body = b"x" * (sc._HTTP_BODY_LIMIT + 10) + b"Dashboard"
        with mock.patch.object(sc.requests, "get", return_value=_FakeResp(body=body)):
            self.assertFalse(
                sc.check_http_service("https://example.com", "Dashboard", allow)
            )

    def test_icmp_loss(self):
        with mock.patch.object(sc, "ping_host", lambda host: (12.5, 30.0)):
            self.assertEqual(sc.check_icmp_service("10.9.9.9"), (True, 12.5))
        with mock.patch.object(sc, "ping_host", lambda host: (0.0, 100.0)):
            self.assertEqual(sc.check_icmp_service("10.9.9.9"), (False, 0.0))

    def test_run_check_tipe_tidak_dikenal_pending(self):
        status, latency = sc.run_check("udp", "10.0.0.1", 53, None)
        self.assertEqual(status, "PENDING")
        self.assertGreaterEqual(latency, 0)

    def test_ssl_level(self):
        self.assertIsNone(m._ssl_level(None))
        self.assertEqual(m._ssl_level(-1), "expired")
        self.assertEqual(m._ssl_level(7), "high")
        self.assertEqual(m._ssl_level(30), "warning")
        self.assertIsNone(m._ssl_level(31))


class ServicesApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        m.DB_PATH = os.path.join(_tmp, "test.db")
        m.init_db()
        m._seed_admin_from_env()
        cls.client = m.app.test_client()
        r = cls.client.post(
            "/login", data={"username": "admin", "password": "PantauNms2026"}
        )
        assert r.status_code == 302, f"login gagal: {r.status_code}"

    def setUp(self):
        conn, c = m.get_db()
        c.execute("DELETE FROM service_history")
        c.execute("DELETE FROM services")
        conn.commit()
        conn.close()

    def tearDown(self):
        conn, c = m.get_db()
        c.execute("DELETE FROM service_history")
        c.execute("DELETE FROM services")
        conn.commit()
        conn.close()

    def _post(self, body):
        with (
            mock.patch.object(m, "trigger_manual_service_check"),
            mock.patch.object(m, "trigger_async_ssl_check"),
        ):
            return self.client.post("/api/services", json=body, headers=JSON_HDR)

    def test_halaman_services_dan_sla(self):
        for path, marker in (
            ("/services", "New service"),
            ("/services/sla", "Create SLA"),
            ("/services/sla/report", "Pilih SLA untuk menampilkan laporan."),
        ):
            r = self.client.get(path)
            self.assertEqual(r.status_code, 200, path)
            html = r.get_data(as_text=True)
            self.assertIn(marker, html)
            self.assertIn('href="/services/sla"', html)
            self.assertIn('data-nav="sla-report"', html)
            if path == "/services/sla":
                for layout_marker in (
                    'id="btnCreateSla"',
                    'id="btnToggleSlaFilter"',
                    'id="slaFilterName"',
                    'id="slaFilterStatus"',
                    'data-status="enabled"',
                    'data-status="disabled"',
                    'id="slaFilterTagOp"',
                    'data-eval="and_or"',
                    'id="slaFilterTagList"',
                    'id="btnAddSlaTag"',
                    'placeholder="tag"',
                    'placeholder="value"',
                    'value="contains" selected>Contains',
                    "Does not exist",
                    "Does not equal",
                    "Does not contain",
                    'id="btnApplySlaFilter"',
                    'id="btnResetSlaFilter"',
                    'id="slaSortName"',
                    'id="slaBody"',
                    "Effective date",
                    "Reporting period",
                    "Timezone",
                    "Schedule",
                    "SLA report",
                    'id="slaSelectedCount"',
                    'id="btnSlaEnable"',
                    'id="btnSlaDisable"',
                    'id="btnSlaDelete"',
                    'id="slaTableInfo"',
                    "Memuat data",
                    'id="slaDialog"',
                    "New SLA",
                    "Excluded downtimes",
                    'id="slaDowntimesPane"',
                    'id="slaDowntimeList"',
                    'id="btnAddSlaDowntime"',
                    'id="downtimeDialog"',
                    'id="downtimeDialogTitle"',
                    "New excluded downtime",
                    'id="downtimeName"',
                    'placeholder="short description"',
                    'id="downtimeStart"',
                    'id="downtimeDatePicker"',
                    'id="btnDowntimeCalendar"',
                    'id="downtimeDays"',
                    'id="downtimeHours"',
                    'id="downtimeMinutes"',
                    'id="btnDowntimeAdd"',
                    'id="btnDowntimeCancel"',
                    "Start time",
                    "Duration",
                    "Actions",
                    "Days",
                    "Hours",
                    "Minutes",
                    'id="slaName"',
                    'id="slaSlo"',
                    'id="slaReporting"',
                    "Reporting period",
                    "Daily",
                    "Weekly",
                    "Monthly",
                    "Quarterly",
                    "Annually",
                    'id="slaTimezone"',
                    "System default: (UTC+07:00) Asia/Jakarta (WIB)",
                    'id="slaSchedule"',
                    "24x7",
                    "Custom",
                    'id="slaScheduleCustom"',
                    'data-schedule-day="sunday"',
                    'data-schedule-day="monday"',
                    'data-schedule-day="tuesday"',
                    'data-schedule-day="wednesday"',
                    'data-schedule-day="thursday"',
                    'data-schedule-day="friday"',
                    'data-schedule-day="saturday"',
                    'id="slaDaySundayHours"',
                    'id="slaDayMondayHours"',
                    'placeholder="8:00-17:00, ..."',
                    'value="8:00-17:00"',
                    'id="slaEffectiveDate"',
                    'id="slaFormTagList"',
                    "data-sla-form-tag",
                    "data-sla-tag-op",
                    "data-remove-sla-tag",
                    'id="btnAddSlaFormTag"',
                    'id="btnSlaCalendar"',
                    'aria-controls="slaCalendarPopover"',
                    'id="slaCalendarPopover"',
                    'id="slaCalendarTitle"',
                    'id="slaCalendarGrid"',
                    'data-cal-nav="-1"',
                    'data-cal-nav="1"',
                    "Operation",
                    'id="slaDescription"',
                    'id="slaEnabled"',
                    'id="btnSlaDialogAdd"',
                    'id="btnSlaDialogCancel"',
                ):
                    self.assertIn(layout_marker, html)
                option_order = [
                    html.index(">Exists<"),
                    html.index(">Equals<"),
                    html.index(">Contains<"),
                    html.index(">Does not exist<"),
                    html.index(">Does not equal<"),
                    html.index(">Does not contain<"),
                ]
                self.assertEqual(option_order, sorted(option_order))
                self.assertNotIn("No excluded downtimes.", html)
                day_order = [
                    html.index('data-schedule-day="sunday"'),
                    html.index('data-schedule-day="monday"'),
                    html.index('data-schedule-day="tuesday"'),
                    html.index('data-schedule-day="wednesday"'),
                    html.index('data-schedule-day="thursday"'),
                    html.index('data-schedule-day="friday"'),
                    html.index('data-schedule-day="saturday"'),
                ]
                self.assertEqual(day_order, sorted(day_order))
                self.assertIn(
                    'id="slaDaySundayHours" data-schedule-hours="sunday" '
                    'placeholder="8:00-17:00, ..." aria-label="Sunday hours" '
                    'autocomplete="off" disabled',
                    html,
                )
                self.assertIn(
                    'id="slaDayMondayHours" data-schedule-hours="monday" '
                    'value="8:00-17:00" placeholder="8:00-17:00, ..." '
                    'aria-label="Monday hours" autocomplete="off"',
                    html,
                )
            if path == "/services/sla/report":
                for layout_marker in (
                    'id="btnSlaReportHelp"',
                    'id="slaReportFilter"',
                    'id="btnToggleSlaReportFilter"',
                    'aria-expanded="true"',
                    'id="slaReportFilterForm"',
                    'id="slaReportSla"',
                    'id="slaReportService"',
                    'id="btnSelectSla"',
                    'id="btnSelectService"',
                    'placeholder="type here to search"',
                    'id="slaReportFrom"',
                    'id="slaReportTo"',
                    'id="slaReportFromPicker"',
                    'id="slaReportToPicker"',
                    'id="btnSlaReportFromCalendar"',
                    'id="btnSlaReportToCalendar"',
                    'placeholder="YYYY-MM-DD"',
                    'id="btnApplySlaReport"',
                    'id="btnResetSlaReport"',
                    'id="slaReportMessage"',
                    'id="slaPickerDialog"',
                    'id="slaPickerTitle"',
                    'id="slaPickerBody"',
                    'id="btnCloseSlaPicker"',
                    'id="btnSlaPickerCancel"',
                    'id="servicePickerDialog"',
                    'id="servicePickerTitle"',
                    'id="servicePickerName"',
                    'id="btnFilterServicePicker"',
                    'id="btnResetServicePicker"',
                    'id="servicePickerBody"',
                    'id="btnCloseServicePicker"',
                    'id="btnServicePickerCancel"',
                    ">Tags<",
                    ">Problem tags<",
                    "No data found",
                ):
                    self.assertIn(layout_marker, html)
                self.assertNotIn("Daily breakdown", html)
                self.assertNotIn("Overall SLA", html)
            if path == "/services":
                for layout_marker in (
                    "Reason",
                    "Last check",
                    "Created at",
                    "Displaying 0 of 0 found",
                    'class="zbx-table"',
                    'id="btnHelp"',
                    'id="btnCreateService"',
                    "Create check",
                    'id="btnView"',
                    'id="btnEdit"',
                    'id="filterStatus"',
                    'data-status="online"',
                    'data-status="offline"',
                    'data-status="nodata"',
                    'id="filterTagsScope"',
                    'data-scope="any"',
                    'data-scope="service"',
                    'data-scope="problem"',
                    'id="filterTagsOp"',
                    'data-eval="and_or"',
                    'id="filterTagList"',
                    'id="btnAddTag"',
                    'placeholder="tag"',
                    'placeholder="value"',
                    "Only services without children",
                    "Only services without problem tags",
                    'id="selectAll"',
                    'id="selectedCount"',
                    'id="btnMassUpdate"',
                    'id="btnBulkDelete"',
                    "Mass update",
                    'data-mode="view"',
                    "Contains",
                    "And/Or",
                    'id="addDialog"',
                    'id="pickerDialog"',
                    "Add parent services",
                    'id="pickerSearch"',
                    'id="btnPickerFilter"',
                    'id="btnPickerReset"',
                    'id="pickerSelectAll"',
                    'id="pickerError"',
                    "Advanced configuration",
                    "Parent services",
                    "Problem tags",
                    "Status calculation rule",
                    "Status propagation rule",
                    "Additional rules",
                    "Child services",
                    "Sort order",
                    "Weight",
                    'id="problemTagList"',
                    'id="serviceTagList"',
                    'id="childServiceList"',
                    'id="childSearch"',
                    'id="btnChildFilter"',
                    'id="btnChildReset"',
                    'class="zbx-child-table"',
                    'id="btnAddChild"',
                    'id="additionalRuleList"',
                    'id="ruleDialog"',
                    "New additional rule",
                    "Set status to",
                    'id="ruleNewStatus"',
                    'id="ruleCondition"',
                    'id="ruleConditionMenu"',
                    "If at least",
                    'class="zbx-rule-chip"',
                    'id="ruleLimit"',
                    'id="ruleLimitStatus"',
                    'id="btnRuleAdd"',
                    'id="btnRuleCancel"',
                    "Not classified",
                    'data-tab="service"',
                    'data-tab="tags"',
                    'data-tab="children"',
                ):
                    self.assertIn(layout_marker, html)
                # Fitur pohon layanan bisnis tidak ditampilkan pada mode
                # service check; field lama tetap ada untuk kompatibilitas.
                self.assertRegex(html, r'id="advancedSection" hidden')
                self.assertRegex(html, r'id="tabChildren"[^>]*hidden')
                self.assertRegex(html, r'id="btnMassUpdate"[^>]*hidden')
                self.assertRegex(
                    html,
                    r'<div class="zbx-form-row" hidden>\s*<label[^>]*for="svcParentSearch"',
                )

    def test_validasi_tipe_dan_dns(self):
        r = self._post({"ip": "10.1.1.1", "name": "x", "type": "udp", "port": 53})
        self.assertEqual(r.status_code, 400)
        r = self._post({"ip": "8.8.8.8", "name": "dns", "type": "dns"})
        self.assertEqual(r.status_code, 400)
        r = self._post(
            {"ip": "8.8.8.8", "name": "dns", "type": "dns", "query": "a..b.com"}
        )
        self.assertEqual(r.status_code, 400)

    def test_buat_duplikat_dan_hapus(self):
        r = self._post({"ip": "10.9.9.26", "name": "gw", "type": "icmp"})
        self.assertEqual(r.status_code, 201)
        sid = r.get_json()["id"]
        r2 = self._post({"ip": "10.9.9.26", "name": "gw2", "type": "icmp"})
        self.assertEqual(r2.status_code, 400)
        r3 = self.client.delete(f"/api/services/{sid}", headers=XRW_HDR)
        self.assertEqual(r3.status_code, 200)
        conn, c = m.get_db()
        self.assertIsNone(
            c.execute("SELECT id FROM services WHERE id=?", (sid,)).fetchone()
        )
        conn.close()

    def test_update_service(self):
        r = self._post({"ip": "10.9.9.50", "name": "gw-lama", "type": "icmp"})
        self.assertEqual(r.status_code, 201)
        sid = r.get_json()["id"]

        with (
            mock.patch.object(m, "trigger_manual_service_check"),
            mock.patch.object(m, "trigger_async_ssl_check"),
        ):
            r = self.client.put(
                f"/api/services/{sid}",
                json={
                    "ip": "10.9.9.51",
                    "name": "gw-baru",
                    "type": "http",
                    "url": "https://example.com",
                    "expected": "Dashboard",
                    "tags": "web, produksi",
                },
                headers=JSON_HDR,
            )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["id"], sid)

        conn, c = m.get_db()
        svc = dict(c.execute("SELECT * FROM services WHERE id=?", (sid,)).fetchone())
        conn.close()
        self.assertEqual(svc["name"], "gw-baru")
        self.assertEqual(svc["ip"], "10.9.9.51")
        self.assertEqual(svc["type"], "http")
        self.assertEqual(svc["url"], "https://example.com")
        self.assertEqual(svc["expected"], "Dashboard")
        self.assertEqual(svc["tags"], "web, produksi")
        self.assertTrue(svc["created_at"])

        # Update service yang sama tidak dianggap duplikat.
        with (
            mock.patch.object(m, "trigger_manual_service_check"),
            mock.patch.object(m, "trigger_async_ssl_check"),
        ):
            r = self.client.put(
                f"/api/services/{sid}",
                json={"ip": "10.9.9.51", "name": "gw-baru-2", "type": "icmp"},
                headers=JSON_HDR,
            )
        self.assertEqual(r.status_code, 200)

        # Service lain tidak boleh memakai target yang sama.
        r = self._post({"ip": "10.9.9.51", "name": "lain", "type": "icmp"})
        self.assertEqual(r.status_code, 400)

        with (
            mock.patch.object(m, "trigger_manual_service_check"),
            mock.patch.object(m, "trigger_async_ssl_check"),
        ):
            r = self.client.put(
                "/api/services/999999",
                json={"ip": "10.9.9.99", "name": "x", "type": "icmp"},
                headers=JSON_HDR,
            )
        self.assertEqual(r.status_code, 404)

        with (
            mock.patch.object(m, "trigger_manual_service_check"),
            mock.patch.object(m, "trigger_async_ssl_check"),
        ):
            r = self.client.put(
                f"/api/services/{sid}",
                json={"ip": "10.9.9.51", "name": "x", "type": "udp"},
                headers=JSON_HDR,
            )
        self.assertEqual(r.status_code, 400)

    def test_buat_service_dengan_konfigurasi_lengkap(self):
        r = self._post({"ip": "10.9.9.70", "name": "parent", "type": "icmp"})
        self.assertEqual(r.status_code, 201)
        parent_id = r.get_json()["id"]

        r = self._post(
            {
                "ip": "10.9.9.71",
                "name": "child",
                "type": "icmp",
                "parent_id": parent_id,
                "problem_tags": ["env: production", "team: noc"],
                "sort_order": 5,
                "status_rule": "most_critical_all",
                "description": "Child service",
                "propagation_rule": "increase",
                "weight": 10,
                "additional_rules": ["rule-a", "rule-b"],
            }
        )
        self.assertEqual(r.status_code, 201)
        child_id = r.get_json()["id"]

        r = self.client.get("/api/services?hours=24")
        self.assertEqual(r.status_code, 200)
        data = {item["id"]: item for item in r.get_json()}
        child = data[child_id]
        self.assertEqual(child["parent_id"], parent_id)
        self.assertEqual(child["parent_name"], "parent")
        self.assertEqual(child["problem_tags"], "env: production, team: noc")
        self.assertEqual(child["sort_order"], 5)
        self.assertEqual(child["status_rule"], "most_critical_all")
        self.assertEqual(child["description"], "Child service")
        self.assertEqual(child["propagation_rule"], "increase")
        self.assertEqual(child["weight"], 10)
        self.assertEqual(json.loads(child["additional_rules"]), ["rule-a", "rule-b"])
        self.assertEqual(data[parent_id]["child_count"], 1)

    def test_validasi_field_konfigurasi(self):
        r = self._post(
            {"ip": "10.9.9.73", "name": "bad", "type": "icmp", "sort_order": 1000}
        )
        self.assertEqual(r.status_code, 400)
        r = self._post({"ip": "10.9.9.74", "name": "bad", "type": "icmp", "weight": -1})
        self.assertEqual(r.status_code, 400)
        r = self._post(
            {
                "ip": "10.9.9.75",
                "name": "bad",
                "type": "icmp",
                "status_rule": "unknown",
            }
        )
        self.assertEqual(r.status_code, 400)
        r = self._post(
            {
                "ip": "10.9.9.76",
                "name": "bad",
                "type": "icmp",
                "propagation_rule": "unknown",
            }
        )
        self.assertEqual(r.status_code, 400)
        r = self._post(
            {"ip": "10.9.9.77", "name": "bad", "type": "icmp", "parent_id": 999999}
        )
        self.assertEqual(r.status_code, 400)

    def test_hierarki_dan_siklus(self):
        r = self._post({"ip": "10.9.9.80", "name": "a", "type": "icmp"})
        self.assertEqual(r.status_code, 201)
        a_id = r.get_json()["id"]
        r = self._post({"ip": "10.9.9.81", "name": "b", "type": "icmp"})
        self.assertEqual(r.status_code, 201)
        b_id = r.get_json()["id"]

        def put(svc_id, body):
            with (
                mock.patch.object(m, "trigger_manual_service_check"),
                mock.patch.object(m, "trigger_async_ssl_check"),
            ):
                return self.client.put(
                    f"/api/services/{svc_id}", json=body, headers=JSON_HDR
                )

        r = put(
            a_id,
            {
                "ip": "10.9.9.80",
                "name": "a",
                "type": "icmp",
                "child_ids": [b_id],
            },
        )
        self.assertEqual(r.status_code, 200)
        conn, c = m.get_db()
        b_row = c.execute(
            "SELECT parent_id FROM services WHERE id=?", (b_id,)
        ).fetchone()
        conn.close()
        self.assertEqual(b_row["parent_id"], a_id)

        r = put(
            a_id,
            {
                "ip": "10.9.9.80",
                "name": "a",
                "type": "icmp",
                "child_ids": [],
            },
        )
        self.assertEqual(r.status_code, 200)
        conn, c = m.get_db()
        b_row = c.execute(
            "SELECT parent_id FROM services WHERE id=?", (b_id,)
        ).fetchone()
        conn.close()
        self.assertIsNone(b_row["parent_id"])

        r = put(
            a_id,
            {
                "ip": "10.9.9.80",
                "name": "a",
                "type": "icmp",
                "child_ids": [b_id],
            },
        )
        self.assertEqual(r.status_code, 200)
        conn, c = m.get_db()
        b_row = c.execute(
            "SELECT parent_id FROM services WHERE id=?", (b_id,)
        ).fetchone()
        conn.close()
        self.assertEqual(b_row["parent_id"], a_id)

        r = put(
            a_id,
            {
                "ip": "10.9.9.80",
                "name": "a",
                "type": "icmp",
                "parent_id": b_id,
            },
        )
        self.assertEqual(r.status_code, 400)
        r = put(
            a_id,
            {
                "ip": "10.9.9.80",
                "name": "a",
                "type": "icmp",
                "child_ids": [a_id],
            },
        )
        self.assertEqual(r.status_code, 400)
        r = put(
            a_id,
            {
                "ip": "10.9.9.80",
                "name": "a",
                "type": "icmp",
                "parent_id": a_id,
            },
        )
        self.assertEqual(r.status_code, 400)
        r = put(
            a_id,
            {
                "ip": "10.9.9.80",
                "name": "a",
                "type": "icmp",
                "parent_id": b_id,
                "child_ids": [b_id],
            },
        )
        self.assertEqual(r.status_code, 400)

        r = self.client.delete(f"/api/services/{a_id}", headers=XRW_HDR)
        self.assertEqual(r.status_code, 200)
        conn, c = m.get_db()
        b_row = c.execute(
            "SELECT parent_id FROM services WHERE id=?", (b_id,)
        ).fetchone()
        conn.close()
        self.assertIsNone(b_row["parent_id"])

    def test_list_dan_history(self):
        r = self._post(
            {
                "ip": "192.0.2.10",
                "name": "Web",
                "type": "http",
                "url": "https://example.com",
            }
        )
        sid = r.get_json()["id"]
        conn, c = m.get_db()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        c.execute(
            "INSERT INTO service_history (service_id, status, latency, timestamp) VALUES (?, 'ONLINE', 12.5, ?)",
            (sid, now),
        )
        c.execute(
            "INSERT INTO service_history (service_id, status, latency, timestamp) VALUES (?, 'OFFLINE', 0, ?)",
            (sid, now),
        )
        conn.commit()
        conn.close()

        r = self.client.get("/api/services?hours=24")
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["checks"], 2)
        self.assertEqual(data[0]["availability"], 50.0)
        self.assertEqual(data[0]["avg_latency"], 12.5)
        self.assertEqual(data[0]["target"], "https://example.com")

        r = self.client.get(f"/api/services/{sid}/history?hours=24")
        self.assertEqual(r.status_code, 200)
        hist = r.get_json()
        self.assertEqual(hist["count"], 2)
        self.assertEqual(hist["values"], [12.5, None])

    def test_tags_dan_created_at(self):
        r = self._post(
            {
                "ip": "192.0.2.11",
                "name": "Web Tagged",
                "type": "http",
                "url": "https://example.com/tagged",
                "tags": "web, produksi, Web,  ",
            }
        )
        self.assertEqual(r.status_code, 201)
        sid = r.get_json()["id"]

        r = self.client.get("/api/services?hours=24")
        self.assertEqual(r.status_code, 200)
        svc = next(item for item in r.get_json() if item["id"] == sid)
        self.assertEqual(svc["tags"], "web, produksi")
        self.assertTrue(svc["created_at"])
        datetime.strptime(svc["created_at"], "%Y-%m-%d %H:%M:%S")

        r = self._post(
            {
                "ip": "192.0.2.12",
                "name": "tag panjang",
                "type": "icmp",
                "tags": "x" * 65,
            }
        )
        self.assertEqual(r.status_code, 400)
        r = self._post(
            {
                "ip": "192.0.2.13",
                "name": "tag banyak",
                "type": "icmp",
                "tags": ",".join(f"tag{i}" for i in range(21)),
            }
        )
        self.assertEqual(r.status_code, 400)

    def test_sla_dan_report(self):
        r = self._post(
            {
                "ip": "192.0.2.10",
                "name": "Web",
                "type": "http",
                "url": "https://example.com",
            }
        )
        sid = r.get_json()["id"]
        conn, c = m.get_db()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        c.executemany(
            "INSERT INTO service_history (service_id, status, latency, timestamp) VALUES (?, ?, ?, ?)",
            [
                (sid, "ONLINE", 10.0, now),
                (sid, "ONLINE", 20.0, now),
                (sid, "OFFLINE", 0, now),
            ],
        )
        conn.commit()
        conn.close()

        r = self.client.get("/api/services/sla?hours=24")
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertEqual(data["totals"]["checks"], 3)
        self.assertEqual(data["totals"]["online"], 2)
        self.assertEqual(data["totals"]["offline"], 1)
        self.assertEqual(data["totals"]["availability"], 66.67)
        self.assertEqual(len(data["timeline"]["labels"]), 24)
        self.assertEqual(data["services"][0]["id"], sid)

        today = datetime.now().strftime("%Y-%m-%d")
        r = self.client.get(
            f"/api/services/sla/report?from={today}&to={today}&service_id={sid}"
        )
        self.assertEqual(r.status_code, 200)
        report = r.get_json()
        self.assertEqual(report["days"], 1)
        self.assertEqual(report["totals"]["checks"], 3)
        self.assertEqual(report["daily"][0]["date"], today)
        self.assertEqual(report["daily"][0]["availability"], 66.67)

    def test_report_validasi_tanggal(self):
        r = self.client.get("/api/services/sla/report?from=2026-10-10&to=2026-10-01")
        self.assertEqual(r.status_code, 400)
        r = self.client.get("/api/services/sla/report?from=xx&to=2026-10-01")
        self.assertEqual(r.status_code, 400)

    def test_filter_service_sla_dan_report(self):
        ids = []
        for ip, name in (("10.9.9.31", "gw-a"), ("10.9.9.32", "gw-b")):
            r = self._post({"ip": ip, "name": name, "type": "icmp"})
            self.assertEqual(r.status_code, 201)
            ids.append(r.get_json()["id"])
        conn, c = m.get_db()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        c.executemany(
            "INSERT INTO service_history (service_id, status, latency, timestamp) VALUES (?, ?, ?, ?)",
            [
                (ids[0], "ONLINE", 10.0, now),
                (ids[0], "OFFLINE", 0, now),
                (ids[1], "ONLINE", 20.0, now),
            ],
        )
        conn.commit()
        conn.close()

        r = self.client.get(f"/api/services/sla?hours=24&service_id={ids[0]}")
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertEqual(len(data["services"]), 1)
        self.assertEqual(data["services"][0]["id"], ids[0])
        self.assertEqual(data["totals"]["checks"], 2)
        self.assertEqual(data["totals"]["services"], 1)
        self.assertEqual(sorted(o["id"] for o in data["service_options"]), sorted(ids))

        today = datetime.now().strftime("%Y-%m-%d")
        r = self.client.get(
            f"/api/services/sla/report?from={today}&to={today}&service_id={ids[1]}"
        )
        self.assertEqual(r.status_code, 200)
        report = r.get_json()
        self.assertEqual(len(report["services"]), 1)
        self.assertEqual(report["services"][0]["id"], ids[1])
        self.assertEqual(report["totals"]["checks"], 1)
        self.assertEqual(report["totals"]["services"], 1)
        self.assertEqual(
            sorted(o["id"] for o in report["service_options"]), sorted(ids)
        )

    def test_status_stale_dan_last_checked_age(self):
        r = self._post(
            {
                "ip": "192.0.2.40",
                "name": "web-stale",
                "type": "http",
                "url": "https://example.com/stale",
            }
        )
        sid = r.get_json()["id"]
        old = (datetime.now() - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
        conn, c = m.get_db()
        c.execute(
            "UPDATE services SET status='ONLINE', last_checked=? WHERE id=?",
            (old, sid),
        )
        conn.commit()
        conn.close()

        data = self.client.get("/api/services?hours=24").get_json()
        svc = next(item for item in data if item["id"] == sid)
        self.assertTrue(svc["stale"])
        self.assertGreaterEqual(svc["last_checked_age"], 590)

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn, c = m.get_db()
        c.execute(
            "UPDATE services SET last_checked=? WHERE id=?", (now, sid)
        )
        conn.commit()
        conn.close()
        data = self.client.get("/api/services?hours=24").get_json()
        svc = next(item for item in data if item["id"] == sid)
        self.assertFalse(svc["stale"])

    def test_check_hanya_satu_service(self):
        a = self._post({"ip": "192.0.2.41", "name": "a", "type": "icmp"})
        b = self._post({"ip": "192.0.2.42", "name": "b", "type": "icmp"})
        a_id = a.get_json()["id"]
        b_id = b.get_json()["id"]

        with mock.patch.object(
            m, "check_single_service", side_effect=lambda svc: (svc[0], "OFFLINE", 0)
        ):
            result = m._run_service_check(a_id)
        self.assertEqual(result, "OFFLINE")

        conn, c = m.get_db()
        a_row = c.execute(
            "SELECT status FROM services WHERE id=?", (a_id,)
        ).fetchone()
        b_row = c.execute(
            "SELECT status FROM services WHERE id=?", (b_id,)
        ).fetchone()
        hist = c.execute(
            "SELECT service_id FROM service_history ORDER BY id"
        ).fetchall()
        conn.close()
        self.assertEqual(a_row["status"], "OFFLINE")
        self.assertEqual(b_row["status"], "PENDING")
        self.assertEqual([row["service_id"] for row in hist], [a_id])

    def test_endpoint_check_satu_service(self):
        r = self._post({"ip": "192.0.2.44", "name": "one", "type": "icmp"})
        sid = r.get_json()["id"]
        with mock.patch.object(m.threading, "Thread") as thread:
            r = self.client.post(f"/api/services/{sid}/check", headers=XRW_HDR)
        self.assertEqual(r.status_code, 200)
        thread.assert_called_once()
        self.assertEqual(thread.call_args.kwargs.get("args"), (sid,))
        self.assertEqual(thread.call_args.kwargs.get("target"), m._run_service_check)

        r = self.client.post("/api/services/999999/check", headers=XRW_HDR)
        self.assertEqual(r.status_code, 404)

    def test_csrf_menolak_origin_lain(self):
        body = {"ip": "192.0.2.43", "name": "csrf", "type": "icmp"}
        r = self.client.post(
            "/api/services",
            json=body,
            headers={**JSON_HDR, "Origin": "http://evil.example"},
        )
        self.assertEqual(r.status_code, 403)

        with (
            mock.patch.object(m, "trigger_manual_service_check"),
            mock.patch.object(m, "trigger_async_ssl_check"),
        ):
            r = self.client.post(
                "/api/services",
                json=body,
                headers={**JSON_HDR, "Origin": "http://localhost"},
            )
        self.assertEqual(r.status_code, 201)

    def test_check_services_menulis_histori(self):
        r = self._post({"ip": "10.9.9.26", "name": "gw", "type": "icmp"})
        sid = r.get_json()["id"]
        with mock.patch.object(
            m, "check_single_service", return_value=(sid, "ONLINE", 8.5)
        ):
            m.check_services()
        conn, c = m.get_db()
        svc = dict(
            c.execute(
                "SELECT status, latency FROM services WHERE id=?", (sid,)
            ).fetchone()
        )
        hist = c.execute(
            "SELECT status, latency FROM service_history WHERE service_id=?", (sid,)
        ).fetchall()
        conn.close()
        self.assertEqual(svc["status"], "ONLINE")
        self.assertEqual(svc["latency"], 8.5)
        self.assertEqual(len(hist), 1)
        self.assertEqual(hist[0]["status"], "ONLINE")


if __name__ == "__main__":
    unittest.main()
