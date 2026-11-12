"""Tes halaman Reports: menu sidebar, route, dan data tiap sub-halaman."""

import os
import re
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
            c.execute("DELETE FROM audit_logs")
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
            "/reports/audit": "Recordset ID",
            "/reports/actions": "rpt-action-table",
            "/reports/notifications": "notif-table",
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
        self.assertIn("Disaster", html)
        self.assertIn("Number of problems", html)
        self.assertIn("Host groups", html)
        self.assertIn("Problem tags", html)
        self.assertNotIn("Total alert", html)
        # Dialog "Select" gaya Zabbix: header Name + checkbox pilih semua,
        # tombol Select (solid) dan Cancel (outline).
        self.assertIn('id="rptPickerDialog"', html)
        self.assertIn('class="rpt-picker-thead"', html)
        self.assertIn(">Name</span>", html)
        self.assertIn('id="rptPickerAll"', html)
        self.assertIn('id="rptPickerSelect"', html)
        self.assertIn('id="rptPickerCancel"', html)
        self.assertIn("data-picker-values=", html)
        # Popup Hosts punya baris filter Host group + keadaan "Filter is not set".
        self.assertIn('id="rptPickerFilter"', html)
        self.assertIn(">Host group</label>", html)
        self.assertIn('id="rptPickerFilterInput"', html)
        self.assertIn('placeholder="type here to search"', html)
        self.assertIn('id="rptPickerFilterSelect"', html)
        self.assertIn("Filter is not set", html)
        self.assertIn("Use the filter to display results", html)
        self.assertIn('id="rptGroupDialog"', html)
        self.assertIn('data-picker-mode="hosts"', html)
        self.assertIn("data-picker-map=", html)
        # Filter disembunyikan di balik tombol "Filter" (gaya Zabbix 7.4).
        self.assertRegex(html, r'id="availabilityFilterButton"[^>]*aria-expanded="false"')
        self.assertRegex(html, r'id="availabilityFilterPanel"[^>]*hidden')

    def test_hosts_picker_filter_host_group(self):
        conn, c = m.get_db()
        try:
            c.execute(
                "INSERT INTO alert_history (host, alert_type, severity, message, triggered_at, duration_s) "
                "VALUES (?,?,?,?,?,?)",
                ("10.20.30.40", "availability", "high", "Link down", _now(-2), 60),
            )
            conn.commit()
        finally:
            conn.close()

        html = self._page("/reports/triggers?time=30d")
        self.assertIn('data-picker-mode="hosts"', html)
        self.assertIn("data-picker-map=", html)
        # Peta host group -> host dikirim ke halaman supaya popup Hosts bisa
        # menampilkan daftar host setelah filter Host group dipilih.
        self.assertRegex(html, r'data-picker-map=\'[^\']+\'')
        self.assertRegex(html, r'data-picker-groups=\'\[[^\]]*\]\'')

    def test_audit_log_tampilan_zabbix(self):
        html = self._page("/reports/audit")
        # Panel waktu terbuka seperti screenshot lama; panel filter tertutup.
        self.assertRegex(html, r'id="availabilityFilterButton"[^>]*aria-expanded="false"')
        self.assertRegex(html, r'id="availabilityFilterButton"[^>]*aria-controls="auditFilterPanel"')
        self.assertNotRegex(html, r'id="availabilityTimePanel"[^>]*hidden')
        self.assertRegex(html, r'id="auditFilterPanel"[^>]*hidden')
        self.assertIn(">From</label>", html)
        self.assertIn(">To</label>", html)
        self.assertIn('value="now-1h"', html)
        self.assertIn('value="now"', html)
        self.assertIn("Last 1 hour", html)
        self.assertIn("rpt-audit-presets", html)
        # Isi panel filter mengikuti Audit log Zabbix 7.4.
        for label in ("Users", "Resource", "Resource ID", "Recordset ID", "IP"):
            self.assertIn(f">{label}</label>", html, label)
        self.assertIn(">Actions</span>", html)
        for action in (
            "Add", "Configuration refresh", "Delete", "Execute", "Failed login",
            "History clear", "Login", "Logout", "Push", "Update",
        ):
            self.assertIn(f">{action}</span>", html, action)
        self.assertIn('name="actions"', html)
        self.assertIn('name="users"', html)
        self.assertIn('<option value="">All</option>', html)
        self.assertIn("rpt-audit-filter-apply", html)
        self.assertIn("rpt-audit-filter-reset", html)
        # Popup Users mengikuti screenshot Zabbix: checkbox, 3 kolom, Select/Cancel.
        self.assertIn('id="auditUsersDialog"', html)
        self.assertIn('id="auditUsersAll"', html)
        self.assertIn(">Username</span>", html)
        self.assertIn(">Name</span>", html)
        self.assertIn(">Last name</span>", html)
        for username in ("Admin", "guest", "System"):
            self.assertIn(f">{username}</span>", html, username)
        self.assertIn(">Zabbix</span>", html)
        self.assertIn(">Administrator</span>", html)
        self.assertIn('id="auditUsersDialogSelect"', html)
        self.assertIn('id="auditUsersDialogCancel"', html)
        # Kolom tabel mengikuti Audit log Zabbix 7.4.
        for label in ("Time", "User", "IP", "Resource", "ID", "Action", "Recordset ID", "Details"):
            self.assertIn(f">{label}</th>", html)
        self.assertIn("No data found", html)

    def test_audit_filter_panel_menyaring_data(self):
        conn, c = m.get_db()
        try:
            c.execute(
                "INSERT INTO audit_logs (timestamp, username, action, target, detail, ip_address) "
                "VALUES (?,?,?,?,?,?)",
                (_now(-2), "tester", "host.create", "SW-Baru", "tambah host", "10.9.8.7"),
            )
            c.execute(
                "INSERT INTO audit_logs (timestamp, username, action, target, detail, ip_address) "
                "VALUES (?,?,?,?,?,?)",
                (_now(-3), "admin", "auth.login", "", "login admin", "127.0.0.1"),
            )
            conn.commit()
        finally:
            conn.close()

        html = self._page("/reports/audit?users=tester&actions=add")
        self.assertIn("tambah host", html)
        self.assertNotIn("login admin", html)
        # Panel filter terbuka dan nilai filter tetap terisi.
        self.assertRegex(html, r'id="availabilityFilterButton"[^>]*aria-expanded="true"')
        self.assertNotRegex(html, r'id="auditFilterPanel"[^>]*hidden')
        self.assertIn('value="tester"', html)
        self.assertRegex(html, r'name="actions" value="add" checked')
        self.assertRegex(html, r'id="availabilityTimePanel"[^>]*hidden')

        html = self._page("/reports/audit?resource=host")
        self.assertIn("tambah host", html)
        self.assertNotIn("login admin", html)

        html = self._page("/reports/audit?ip=10.9.8.7")
        self.assertIn("tambah host", html)
        self.assertNotIn("login admin", html)

        html = self._page("/reports/audit?actions=login")
        self.assertIn("login admin", html)
        self.assertNotIn("tambah host", html)

        # Popup Users mengisi beberapa nama; filter menyaring salah satunya (OR).
        conn, c = m.get_db()
        try:
            c.execute(
                "INSERT INTO audit_logs (timestamp, username, action, target, detail, ip_address) "
                "VALUES (?,?,?,?,?,?)",
                (_now(-4), "operator", "host.delete", "SW-Lama", "hapus host lain", "10.9.8.8"),
            )
            conn.commit()
        finally:
            conn.close()
        html = self._page("/reports/audit?users=Admin%2C%20tester")
        self.assertIn("login admin", html)
        self.assertIn("tambah host", html)
        self.assertNotIn("hapus host lain", html)
        self.assertIn('value="Admin, tester"', html)
        self.assertRegex(html, r'value="Admin" checked')
        self.assertRegex(html, r'value="tester" checked')

    def test_audit_log_filter_waktu(self):
        conn, c = m.get_db()
        try:
            c.execute(
                "INSERT INTO audit_logs (timestamp, username, action, target, detail, ip_address) "
                "VALUES (?,?,?,?,?,?)",
                (_now(-1), "tester", "unit.test.action", "target-a", "detail unik", "127.0.0.1"),
            )
            c.execute(
                "INSERT INTO audit_logs (timestamp, username, action, target, detail, ip_address) "
                "VALUES (?,?,?,?,?,?)",
                (_now(-120), "tester", "unit.test.old", "", "baris lama", "127.0.0.1"),
            )
            conn.commit()
        finally:
            conn.close()

        html = self._page("/reports/audit")
        self.assertIn("detail unik", html)
        self.assertIn(">User</th>", html)
        self.assertNotIn("baris lama", html)
        # Action mentah tetap tersedia sebagai tooltip.
        self.assertIn('title="unit.test.action"', html)

        html = self._page("/reports/audit?time=30d")
        self.assertIn("baris lama", html)

    def test_action_log_tampilan_zabbix(self):
        html = self._page("/reports/actions")
        for marker in (
            'id="actionExport"',
            "Export to CSV",
            'id="availabilityTimeToggle"',
            'id="availabilityFilterButton"',
            'id="availabilityTimePanel"',
            "data-action-panel",
            "Zoom out",
            "Last 1 hour",
            "From",
            "To",
            "Apply",
            "rpt-action-table",
            "No data found",
        ):
            self.assertIn(marker, html, marker)

        headers = [">Time</th>", ">Action</th>", ">Media type</th>", ">Recipient</th>",
                   ">Message</th>", ">Status</th>", ">Info</th>"]
        positions = [html.index(marker) for marker in headers]
        self.assertEqual(positions, sorted(positions))
        self.assertNotIn("event operasional dari system_logs", html)

    def test_action_log_menyaring_rentang_waktu(self):
        conn, c = m.get_db()
        try:
            c.execute(
                "INSERT INTO system_logs (timestamp, event_type, host, message) VALUES (?,?,?,?)",
                (_now(-2), "EVENT_RECENT", "10.20.30.40", "Pesan baru"),
            )
            c.execute(
                "INSERT INTO system_logs (timestamp, event_type, host, message) VALUES (?,?,?,?)",
                (_now(-60 * 48), "EVENT_OLD", "10.20.30.41", "Pesan lama"),
            )
            conn.commit()
        finally:
            conn.close()

        html = self._page("/reports/actions")
        self.assertIn("Pesan baru", html)
        self.assertIn("10.20.30.40", html)
        self.assertNotIn("Pesan lama", html)

        html = self._page("/reports/actions?time=30d")
        self.assertIn("Pesan baru", html)
        self.assertIn("Pesan lama", html)

    def test_action_log_export_csv(self):
        conn, c = m.get_db()
        try:
            c.execute(
                "INSERT INTO system_logs (timestamp, event_type, host, message) VALUES (?,?,?,?)",
                (_now(-1), "UNIT_TEST", "10.20.30.40", "Pesan ekspor"),
            )
            c.execute(
                "INSERT INTO system_logs (timestamp, event_type, host, message) VALUES (?,?,?,?)",
                (_now(-60 * 48), "UNIT_OLD", "10.20.30.41", "Pesan lama"),
            )
            conn.commit()
        finally:
            conn.close()

        r = self.client.get("/reports/actions/export?time=1h")
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/csv", r.headers["Content-Type"])
        self.assertIn("attachment; filename=action_log_", r.headers["Content-Disposition"])
        body = r.get_data(as_text=True)
        self.assertIn("Time,Action,Media type,Recipient,Message,Status,Info", body)
        self.assertIn("Pesan ekspor", body)
        self.assertNotIn("Pesan lama", body)

    def test_action_log_panel_filter_zabbix(self):
        html = self._page("/reports/actions")
        for marker in (
            'id="actionFilterPanel"',
            "data-action-filter-panel",
            'id="actionFilterRecipients"',
            'id="actionFilterActions"',
            'id="actionFilterMedia"',
            'id="actionFilterSearch"',
            'name="status" value="in_progress"',
            'name="status" value="sent"',
            'name="status" value="failed"',
            "Recipients",
            "Media types",
            "Search string",
            "Sent/Executed",
            'class="rpt-action-select"',
            'id="actionFilterReset"',
        ):
            self.assertIn(marker, html, marker)

    def test_action_log_filter_search_actions_dan_status(self):
        conn, c = m.get_db()
        try:
            c.execute(
                "INSERT INTO system_logs (timestamp, event_type, host, message) VALUES (?,?,?,?)",
                (_now(-2), "EVENT_FILTER_A", "10.10.10.10", "Pesan filter unik"),
            )
            c.execute(
                "INSERT INTO system_logs (timestamp, event_type, host, message) VALUES (?,?,?,?)",
                (_now(-2), "EVENT_FILTER_B", "10.10.10.11", "Pesan lain"),
            )
            conn.commit()
        finally:
            conn.close()

        html = self._page("/reports/actions?search=filter+unik")
        self.assertIn("Pesan filter unik", html)
        self.assertNotIn("Pesan lain", html)

        html = self._page("/reports/actions?actions=EVENT_FILTER_B")
        self.assertIn("Pesan lain", html)
        self.assertNotIn("Pesan filter unik", html)

        html = self._page("/reports/actions?search=filter+unik&time=30d")
        self.assertIn("Pesan filter unik", html)

        html = self._page("/reports/actions?status=failed")
        self.assertIn("No data found", html)
        self.assertNotIn("Pesan filter unik", html)

        r = self.client.get("/reports/actions/export?time=1h&search=filter+unik")
        body = r.get_data(as_text=True)
        self.assertIn("Pesan filter unik", body)
        self.assertNotIn("Pesan lain", body)

    def test_notifikasi_tampilan_zabbix(self):
        html = self._page("/reports/notifications")
        for marker in (
            "Notifications",
            "Media type",
            "Period",
            "Year",
            "Weekly",
            "2026",
            "From",
            "Till",
            "notif-table",
            "notif-user-label",
            "Admin (Zabbix Administrator)",
            "guest",
            "2025-12-29 12:00 AM",
            "2026-01-05 12:00 AM",
        ):
            self.assertIn(marker, html, marker)

        self.assertIn('<option value="weekly" selected>Weekly</option>', html)
        self.assertIn('<option value="2026" selected>2026</option>', html)

    def test_notifikasi_menghitung_tes_per_minggu(self):
        conn, c = m.get_db()
        try:
            c.execute(
                "INSERT INTO audit_logs (timestamp, username, action, target, detail, ip_address) "
                "VALUES (?,?,?,?,?,?)",
                ("2026-01-07 10:00:00", "admin", "notify.test", "", "telegram=ok email=skipped", "127.0.0.1"),
            )
            conn.commit()
        finally:
            conn.close()

        html = self._page("/reports/notifications?period=weekly&year=2026&media=all")
        row = re.search(
            r"2026-01-05 12:00 AM.*?2026-01-12 12:00 AM.*?<td class=\"notif-cell\">(\d*)</td>",
            html,
            re.S,
        )
        self.assertIsNotNone(row)
        self.assertEqual(row.group(1), "1")

        html = self._page("/reports/notifications?period=weekly&year=2026&media=email")
        row = re.search(
            r"2026-01-05 12:00 AM.*?2026-01-12 12:00 AM.*?<td class=\"notif-cell\">(\d*)</td>",
            html,
            re.S,
        )
        self.assertIsNotNone(row)
        self.assertEqual(row.group(1), "")

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
