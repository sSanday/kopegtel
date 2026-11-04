"""Tes halaman Reports: menu sidebar, route, dan data tiap sub-halaman."""

import os
import tempfile
import unittest
from datetime import datetime, timedelta

_tmp = tempfile.mkdtemp(prefix="nms_test_reports_")
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

try:
    m.scheduler.shutdown(wait=False)
except Exception:
    pass

REPORT_URLS = [
    "/reports/system",
    "/reports/scheduled",
    "/reports/availability",
    "/reports/triggers",
    "/reports/audit",
    "/reports/actions",
    "/reports/notifications",
]


def _now(offset_minutes=0):
    return (datetime.now() + timedelta(minutes=offset_minutes)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


class ReportsPagesTest(unittest.TestCase):
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
        try:
            c.execute("DELETE FROM ping_logs")
            c.execute("DELETE FROM alert_history")
            c.execute("DELETE FROM down_events")
            c.execute("DELETE FROM system_logs")
            c.execute("DELETE FROM hosts")
            conn.commit()
        finally:
            conn.close()

    def _page(self, url):
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200, url)
        return r.get_data(as_text=True)

    # ---------- route & menu ----------
    def test_halaman_butuh_login(self):
        anon = m.app.test_client()
        for url in REPORT_URLS:
            r = anon.get(url)
            self.assertEqual(r.status_code, 302, url)
            self.assertIn("/login", r.headers.get("Location", ""), url)

    def test_parent_redirect_ke_system(self):
        r = self.client.get("/reports")
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers["Location"].endswith("/reports/system"))

    def test_halaman_tidak_dikenal_404(self):
        r = self.client.get("/reports/tidak-ada")
        self.assertEqual(r.status_code, 404)

    def test_menu_reports_ada_di_sidebar(self):
        html = self._page("/hosts")
        for href in (
            "/reports/system",
            "/reports/scheduled",
            "/reports/availability",
            "/reports/triggers",
            "/reports/audit",
            "/reports/actions",
            "/reports/notifications",
        ):
            self.assertIn(f'href="{href}"', html, href)
        self.assertIn("Reports", html)
        self.assertLess(html.index("Inventory"), html.index('data-nav="reports-system"'))
        self.assertLess(html.index('data-nav="reports-system"'), html.index("Fiber"))

    def test_semua_halaman_terender(self):
        expected = {
            "/reports/system": "Informasi runtime",
            "/reports/scheduled": "Last sent",
            "/reports/availability": "Availability report",
            "/reports/triggers": "Top 100 triggers",
            "/reports/audit": "Audit viewer lengkap",
            "/reports/actions": "event operasional dari system_logs",
            "/reports/notifications": "Status channel notifikasi",
        }
        for url, needle in expected.items():
            html = self._page(url)
            self.assertIn(needle, html, url)

    # ---------- data ----------
    def test_availability_menghitung_ping_24_jam(self):
        conn, c = m.get_db()
        try:
            c.execute(
                "INSERT INTO hosts (ip, alias, category) VALUES (?,?,?)",
                ("10.20.30.40", "SW-Test", "Core"),
            )
            c.execute(
                "INSERT INTO ping_logs (host, latency, packet_loss, timestamp) VALUES (?,?,?,?)",
                ("10.20.30.40", 2.0, 0.0, _now(-5)),
            )
            c.execute(
                "INSERT INTO ping_logs (host, latency, packet_loss, timestamp) VALUES (?,?,?,?)",
                ("10.20.30.40", -1.0, 100.0, _now(-10)),
            )
            c.execute(
                "INSERT INTO ping_logs (host, latency, packet_loss, timestamp) VALUES (?,?,?,?)",
                ("10.20.30.40", 3.0, 0.0, _now(-60 * 48)),
            )
            conn.commit()
        finally:
            conn.close()

        html = self._page("/reports/availability?range=24h")
        self.assertIn("SW-Test", html)
        self.assertIn("50.00%", html)
        self.assertIn(">Name <span", html)
        self.assertIn("ICMP ping", html)
        self.assertIn('data-name="ICMP ping"', html)
        self.assertIn("Problems", html)
        self.assertIn("Ok", html)
        self.assertIn("Graph", html)
        self.assertIn("/reports/availability/graph?host=10.20.30.40", html)
        # Zabbix layout has no KPI cards above the table.
        self.assertNotIn("rpt-availability-kpis", html)

    def test_availability_graph_page_shows_ping_history(self):
        conn, c = m.get_db()
        try:
            c.execute(
                "INSERT INTO hosts (ip, alias, category) VALUES (?,?,?)",
                ("10.20.30.41", "SW-Graph", "Core"),
            )
            c.execute(
                "INSERT INTO ping_logs (host, latency, packet_loss, timestamp) VALUES (?,?,?,?)",
                ("10.20.30.41", 5.0, 0.0, _now(-5)),
            )
            conn.commit()
        finally:
            conn.close()

        html = self._page("/reports/availability/graph?host=10.20.30.41&from=now-1h&to=now")
        self.assertIn("Availability report graph", html)
        self.assertIn("SW-Graph", html)
        self.assertIn("availabilityGraph", html)
        self.assertIn("OK (%)", html)
        self.assertIn("Problems (%)", html)

    def test_availability_mode_trigger_and_calendar_ranges(self):
        conn, c = m.get_db()
        try:
            c.execute(
                "INSERT INTO hosts (ip, alias, category) VALUES (?,?,?)",
                ("10.20.30.42", "SW-Trigger", "Core"),
            )
            c.execute(
                "INSERT INTO alert_history (host, alert_type, severity, message, triggered_at, duration_s) "
                "VALUES (?,?,?,?,?,?)",
                ("10.20.30.42", "availability", "warning", "Link down", _now(-30), 600),
            )
            c.execute(
                "INSERT INTO alert_history (host, alert_type, severity, message, triggered_at, duration_s) "
                "VALUES (?,?,?,?,?,?)",
                ("10.20.30.42", "interface_down", "high", "Link down", _now(-20), 300),
            )
            conn.commit()
        finally:
            conn.close()

        html = self._page("/reports/availability?mode=trigger&time=1h")
        self.assertIn("SW-Trigger", html)
        self.assertIn("Link down", html)
        self.assertIn('value="trigger" selected', html)
        self.assertIn("Graph", html)
        self.assertIn('data-sort-key="name"', html)
        # Trigger identity follows the alert message, not alert_type.
        self.assertIn('data-name="Link down"', html)
        self.assertIn('data-host-alias="SW-Trigger"', html)
        self.assertIn('data-problems="2"', html)
        self.assertIn('id="availabilityCategoryFilter"', html)
        self.assertIn('id="availabilityHostFilter"', html)
        self.assertIn('data-picker="availabilityCategoryFilter"', html)
        self.assertNotIn('id="availabilityTriggerFilter"', html)

    def test_availability_trigger_normalizes_host_with_ip_suffix(self):
        conn, c = m.get_db()
        try:
            c.execute(
                "INSERT INTO hosts (ip, alias, category) VALUES (?,?,?)",
                ("10.20.30.43", "MT-Test", "Router"),
            )
            c.execute(
                "INSERT INTO alert_history (host, alert_type, severity, message, triggered_at, duration_s) "
                "VALUES (?,?,?,?,?,?)",
                ("MT-Test (10.20.30.43)", "mikrotik", "high", "Port down", _now(-15), 60),
            )
            conn.commit()
        finally:
            conn.close()

        html = self._page("/reports/availability?mode=trigger&time=1h")
        self.assertIn('data-host="MT-Test (10.20.30.43)"', html)
        self.assertIn('data-host-ip="10.20.30.43"', html)
        self.assertIn('data-host-alias="MT-Test"', html)
        self.assertIn('data-name="Port down"', html)
        self.assertIn('href="/host/10.20.30.43"', html)
        self.assertNotIn('href="/host/MT-Test%20(10.20.30.43)"', html)

    def test_availability_host_without_ping_shows_no_data(self):
        conn, c = m.get_db()
        try:
            c.execute(
                "INSERT INTO hosts (ip, alias, category) VALUES (?,?,?)",
                ("10.20.30.44", "SW-NoData", "Core"),
            )
            conn.commit()
        finally:
            conn.close()

        html = self._page(
            "/reports/availability?from=2020-01-01%2000:00&to=2020-01-02%2000:00"
        )
        self.assertIn("SW-NoData", html)
        self.assertIn("rpt-availability-ok-empty", html)
        self.assertIn(">No data</span>", html)

    def test_availability_presets_use_calendar_boundaries_and_relative_values(self):
        for preset in ("yesterday", "previous_week", "this_month"):
            html = self._page("/reports/availability?time=" + preset)
            self.assertIn('class="rpt-availability-selected-time"', html)
        html = self._page("/reports/availability?time=1h")
        self.assertIn('value="now-1h"', html)
        self.assertIn('value="now"', html)

    def test_top_triggers_dikelompokkan(self):
        conn, c = m.get_db()
        try:
            for _ in range(2):
                c.execute(
                    "INSERT INTO alert_history (host, alert_type, severity, message, triggered_at, duration_s) "
                    "VALUES (?,?,?,?,?,?)",
                    ("10.20.30.40", "availability", "disaster", "Link down", _now(-3), 120),
                )
            c.execute(
                "INSERT INTO alert_history (host, alert_type, severity, message, triggered_at, duration_s) "
                "VALUES (?,?,?,?,?,?)",
                ("10.20.30.40", "interface_utilization", "warning", "Utilization high", _now(-8), 60),
            )
            conn.commit()
        finally:
            conn.close()

        html = self._page("/reports/triggers")
        self.assertIn("Link down", html)
        self.assertIn("Utilization high", html)
        self.assertIn("DISASTER", html)
        self.assertIn("Number of problems", html)
        self.assertNotIn("Total alert", html)

    def test_audit_filter(self):
        conn, c = m.get_db()
        try:
            c.execute(
                "INSERT INTO audit_logs (timestamp, username, action, target, detail, ip_address) "
                "VALUES (?,?,?,?,?,?)",
                (_now(-1), "tester", "unit.test.action", "target-a", "detail unik", "127.0.0.1"),
            )
            conn.commit()
        finally:
            conn.close()

        html = self._page("/reports/audit?q=unit.test.action")
        self.assertIn("unit.test.action", html)
        self.assertIn("target-a", html)
        self.assertIn("detail unik", html)

    def test_action_log_filter_event(self):
        conn, c = m.get_db()
        try:
            c.execute(
                "INSERT INTO system_logs (timestamp, event_type, host, message) VALUES (?,?,?,?)",
                (_now(-2), "UNIT_TEST", "10.20.30.40", "Pesan event uji"),
            )
            c.execute(
                "INSERT INTO system_logs (timestamp, event_type, host, message) VALUES (?,?,?,?)",
                (_now(-1), "NETWORK_UP", "10.20.30.41", "Host pulih"),
            )
            conn.commit()
        finally:
            conn.close()

        html = self._page("/reports/actions?event=UNIT_TEST")
        self.assertIn("Pesan event uji", html)
        self.assertNotIn("Host pulih", html)

    def test_notifikasi_menampilkan_channel(self):
        html = self._page("/reports/notifications")
        self.assertIn("Telegram", html)
        self.assertIn("Email (SMTP)", html)
        self.assertIn("Webhook", html)
        self.assertIn("Ambang minimum", html)

    def test_system_information_menampilkan_tabel_parameter(self):
        html = self._page("/reports/system")
        self.assertIn("NMS Kopegtel", html)
        self.assertIn("SQLite", html)
        self.assertIn("NMS server berjalan", html)
        self.assertIn("Parameter", html)
        self.assertIn("Details", html)

    def test_scheduled_filter_nama(self):
        html = self._page("/reports/scheduled?name=fiber")
        self.assertIn("Ringkasan fiber", html)
        self.assertNotIn("Heartbeat harian", html)
        self.assertNotIn("No data found", html)

    def test_scheduled_filter_dibuat_oleh_saya_kosong(self):
        html = self._page("/reports/scheduled?show=mine")
        self.assertIn("No data found", html)

    def test_scheduled_status_kedaluwarsa_kosong(self):
        html = self._page("/reports/scheduled?status=expired")
        self.assertIn("No data found", html)

    def test_scheduled_list_elemen_zabbix(self):
        html = self._page("/reports/scheduled")
        self.assertIn("rpt-sched-list-foot", html)
        self.assertIn("Displaying 1 to 3 of 3 found", html)
        self.assertIn("rpt-sched-status-toggle", html)
        self.assertIn("rptActionsBar", html)
        self.assertIn("rpt-sched-info-cell", html)
        self.assertIn("Previous day", html)
        self.assertIn('title="Setiap hari, 08:00 WIB"', html)

    def test_scheduled_bulk_butuh_penjadwal_aktif(self):
        r = self.client.post(
            "/api/reports/scheduled/bulk",
            json={"action": "disable", "ids": ["send_daily_report"]},
        )
        self.assertEqual(r.status_code, 409)

    def test_scheduled_create_view(self):
        html = self._page("/reports/scheduled?action=create")
        self.assertIn("Owner", html)
        self.assertIn("Dashboard", html)
        self.assertIn("Period", html)
        self.assertIn("Subscriptions", html)
        self.assertIn("Add user", html)
        self.assertIn("Test", html)
        self.assertIn("Cancel", html)
        self.assertIn("rptUsersModal", html)
        self.assertIn("Username", html)
        self.assertIn("Last name", html)
        self.assertIn("Admin", html)
        self.assertIn("guest", html)
        self.assertIn("rptDashboardsModal", html)
        self.assertIn("Dashboards", html)
        self.assertIn("Zabbix server health", html)
        self.assertIn("rptSubscriptionModal", html)
        self.assertIn("rptSubscriptionGroupsModal", html)
        self.assertIn("Generate report by", html)
        self.assertIn("Current user", html)
        self.assertIn("Exclude", html)
        self.assertIn("rptOwnerBox", html)
        self.assertIn("rptOwnerChipText", html)
        self.assertIn("type here to search", html)

    def test_scheduled_create_alias(self):
        r = self.client.get("/reports/scheduled/create", follow_redirects=False)
        self.assertEqual(r.status_code, 302)
        self.assertIn("action=create", r.headers.get("Location", ""))

    def test_scheduled_create_api(self):
        r = self.client.post(
            "/api/reports/scheduled/create",
            json={"name": "Weekly summary", "period": "Previous week"},
        )
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertEqual(data.get("status"), "success")

    def test_scheduled_create_api_validation(self):
        r = self.client.post("/api/reports/scheduled/create", json={"name": ""})
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()
