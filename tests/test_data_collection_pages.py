"""Tes halaman Data collection: menu sidebar, route, dan data tiap sub-halaman."""

import os
import tempfile
import unittest
from datetime import datetime, timedelta

_tmp = tempfile.mkdtemp(prefix="nms_test_data_collection_")
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

DATA_COLLECTION_URLS = [
    "/data-collection/template-groups",
    "/data-collection/host-groups",
    "/data-collection/templates",
    "/data-collection/hosts",
    "/data-collection/maintenance",
    "/data-collection/event-correlation",
    "/data-collection/discovery",
]

DATA_COLLECTION_TABS = [
    ("template-groups", "Template groups"),
    ("host-groups", "Host groups"),
    ("templates", "Templates"),
    ("hosts", "Hosts"),
    ("maintenance", "Maintenance"),
    ("event-correlation", "Event correlation"),
    ("discovery", "Discovery"),
]


def _now(offset_minutes=0):
    return (datetime.now() + timedelta(minutes=offset_minutes)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


class DataCollectionPagesTest(unittest.TestCase):
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
            c.execute("DELETE FROM alert_history")
            c.execute("DELETE FROM maintenance_windows")
            c.execute("DELETE FROM template_groups")
            c.execute("DELETE FROM host_groups")
            c.execute("DELETE FROM templates")
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
        for url in DATA_COLLECTION_URLS:
            r = anon.get(url)
            self.assertEqual(r.status_code, 302, url)
            self.assertIn("/login", r.headers.get("Location", ""), url)

    def test_parent_redirect_ke_template_groups(self):
        r = self.client.get("/data-collection")
        self.assertEqual(r.status_code, 302)
        self.assertTrue(
            r.headers["Location"].endswith("/data-collection/template-groups")
        )

    def test_halaman_tidak_dikenal_404(self):
        r = self.client.get("/data-collection/tidak-ada")
        self.assertEqual(r.status_code, 404)

    def test_menu_data_collection_ada_di_sidebar(self):
        html = self._page("/hosts")
        for page, label in DATA_COLLECTION_TABS:
            self.assertIn(f'href="/data-collection/{page}"', html, page)
            self.assertIn(f"<span>{label}</span>", html, label)
        self.assertIn("Data collection", html)
        # Grup muncul tepat setelah Reports dan sebelum Fiber.
        self.assertLess(
            html.index('data-nav="reports-system"'),
            html.index('data-nav="data-collection-template-groups"'),
        )
        self.assertLess(
            html.index('data-nav="data-collection-template-groups"'),
            html.index('data-nav="fiber"'),
        )

    def test_urutan_menu_data_collection(self):
        html = self._page("/hosts")
        positions = [
            html.index(f'data-nav="data-collection-{page}"')
            for page, _label in DATA_COLLECTION_TABS
        ]
        self.assertEqual(positions, sorted(positions))

    def test_navigasi_data_collection_tanpa_tab_internal(self):
        # Navigasi memakai submenu sidebar, bukan tab di dalam halaman.
        html = self._page("/data-collection/template-groups")
        self.assertNotIn('<nav class="rpt-tabs dc-tabs"', html)
        for page, _label in DATA_COLLECTION_TABS:
            self.assertIn(f'href="/data-collection/{page}"', html, page)

    # ---------- status aktif ----------
    def test_menu_data_collection_hosts_aktif(self):
        html = self._page("/data-collection/hosts")
        self.assertIn(
            'class="nav-link nav-sub-link active" data-nav="data-collection-hosts"',
            html,
        )
        self.assertIn('data-nav="data-collection-hosts" aria-current="page"', html)
        # Menu Monitoring Hosts tidak ikut aktif pada halaman Data collection.
        self.assertNotIn(
            'href="/hosts" class="nav-link nav-sub-link active"', html
        )

    def test_menu_monitoring_hosts_tetap_aktif_di_hosts(self):
        html = self._page("/hosts")
        self.assertIn(
            'href="/hosts" class="nav-link nav-sub-link active"', html
        )
        self.assertNotIn(
            'data-nav="data-collection-hosts" aria-current="page"', html
        )

    def test_menu_discovery_aktif_sesuai_halaman(self):
        html = self._page("/data-collection/discovery")
        self.assertIn(
            'class="nav-link nav-sub-link active" data-nav="data-collection-discovery"',
            html,
        )
        self.assertNotIn(
            'href="/discovery" class="nav-link nav-sub-link active"', html
        )

        html = self._page("/discovery")
        self.assertIn('href="/discovery" class="nav-link nav-sub-link active"', html)
        self.assertNotIn(
            'data-nav="data-collection-discovery" aria-current="page"', html
        )

    # ---------- render halaman ----------
    def test_semua_halaman_terender(self):
        expected = {
            "/data-collection/template-groups": "Discovery templates",
            "/data-collection/host-groups": "No host groups found",
            "/data-collection/templates": "MikroTik",
            "/data-collection/maintenance": "No maintenance periods found",
            "/data-collection/event-correlation": "No event correlation rules found",
            "/data-collection/hosts": "<h1>Hosts</h1>",
            "/data-collection/discovery": "Status of discovery",
        }
        for url, needle in expected.items():
            html = self._page(url)
            self.assertIn(needle, html, url)
            self.assertIn("Data collection", html, url)

    def test_halaman_tidak_menampilkan_menu_lain(self):
        html = self._page("/data-collection/templates")
        self.assertIn("Data collection", html)
        self.assertIn('aria-label="Templates"', html)
        self.assertIn("Template groups", html)
        self.assertIn("Event correlation", html)
        self.assertIn("Discovery", html)
        self.assertIn("Zabbix 7.4.15", html)

    # ---------- data nyata ----------
    def test_template_groups_dari_profil_snmp(self):
        conn, c = m.get_db()
        try:
            c.execute(
                "INSERT INTO hosts (ip, alias, category, snmp_profile) VALUES (?,?,?,?)",
                ("10.30.0.1", "ONT-Test", "ONT", "auto"),
            )
            c.execute(
                "INSERT INTO hosts (ip, alias, category, snmp_profile) VALUES (?,?,?,?)",
                ("10.30.0.2", "MT-Test", "Mikrotik", "auto"),
            )
            conn.commit()
        finally:
            conn.close()

        html = self._page("/data-collection/template-groups")
        self.assertIn("Discovery templates", html)
        self.assertIn("Network device templates", html)
        # Badge menghitung template nyata di setiap grup.
        self.assertIn('title="1 template">1</span>', html)
        self.assertIn('title="2 templates">2</span>', html)
        self.assertIn("Auto-detect", html)
        self.assertIn("MikroTik, Generic", html)
        self.assertIn("Displaying 2 of 2 found", html)

    def test_template_groups_elemen_tampilan(self):
        html = self._page("/data-collection/template-groups")
        for marker in (
            'id="dcCreateOpen"',
            'id="dcFilterToggle"',
            'id="dcFilterPanel"',
            'id="dcFilterName"',
            ">Apply</button>",
            ">Reset</a>",
            'id="dcSelectAll"',
            'class="dc-row-check"',
            'form="dc-delete-form"',
            'id="dcDeleteBtn"',
            "Displaying 2 of 2 found",
            'class="dc-count-badge"',
            'id="dcCreateModal"',
            "New template group",
            "Group name",
            ">Add</button>",
        ):
            self.assertIn(marker, html, marker)

    def test_template_groups_filter_nama(self):
        html = self._page("/data-collection/template-groups?name=network")
        self.assertIn("Displaying 1 of 1 found", html)
        self.assertIn(
            '<a class="dc-tg-link" href="/data-collection/templates">Network device templates</a>',
            html,
        )
        self.assertNotIn(
            '<a class="dc-tg-link" href="/data-collection/templates">Discovery templates</a>',
            html,
        )

        html = self._page("/data-collection/template-groups?name=tidak-ada")
        self.assertIn("No template groups match this filter", html)
        self.assertIn("Displaying 0 of 0 found", html)

    def test_template_groups_urut_nama(self):
        html = self._page("/data-collection/template-groups?sort=name&order=desc")
        network = html.index(
            '<a class="dc-tg-link" href="/data-collection/templates">Network device templates</a>'
        )
        discovery = html.index(
            '<a class="dc-tg-link" href="/data-collection/templates">Discovery templates</a>'
        )
        self.assertLess(network, discovery)

    def test_template_group_buatan_dapat_dibuat_dan_dihapus(self):
        response = self.client.post(
            "/data-collection/template-groups/create",
            data={"name": "Maintenance templates"},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Maintenance templates", html)
        self.assertIn('title="0 templates">0</span>', html)
        self.assertIn("No templates", html)

        conn, c = m.get_db()
        try:
            row = c.execute(
                "SELECT id, name FROM template_groups WHERE name=?",
                ("Maintenance templates",),
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)

        response = self.client.post(
            "/data-collection/template-groups/delete",
            data={"selected": [f"custom:{row['id']}"]},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("1 template group deleted.", response.get_data(as_text=True))
        html = self._page("/data-collection/template-groups")
        self.assertNotIn("Maintenance templates", html)
        self.assertIn("Displaying 2 of 2 found", html)

    def test_template_group_bawaan_tidak_dapat_dihapus(self):
        response = self.client.post(
            "/data-collection/template-groups/delete",
            data={"selected": ["builtin:Discovery templates"]},
            follow_redirects=True,
        )
        html = response.get_data(as_text=True)
        self.assertIn("Built-in template groups cannot be deleted.", html)
        self.assertIn("Discovery templates", html)

    def test_template_group_duplikat_ditolak(self):
        for name in ("Discovery templates", "Network device templates"):
            response = self.client.post(
                "/data-collection/template-groups/create",
                data={"name": name},
                follow_redirects=True,
            )
            self.assertIn("already exists", response.get_data(as_text=True))

        self.client.post(
            "/data-collection/template-groups/create",
            data={"name": "Custom group"},
        )
        response = self.client.post(
            "/data-collection/template-groups/create",
            data={"name": "custom group"},
            follow_redirects=True,
        )
        self.assertIn("already exists", response.get_data(as_text=True))

    def test_templates_menampilkan_profil_dan_jumlah_host(self):
        conn, c = m.get_db()
        try:
            c.execute(
                "INSERT INTO hosts (ip, alias, category, snmp_profile) VALUES (?,?,?,?)",
                ("10.30.0.1", "ONT-Test", "ONT", "mikrotik"),
            )
            conn.commit()
        finally:
            conn.close()

        html = self._page("/data-collection/templates")
        for label in ("Auto-detect", "MikroTik", "Generic", "Discovery templates"):
            self.assertIn(label, html, label)
        for header in (
            "Hosts",
            "Items",
            "Triggers",
            "Graphs",
            "Dashboards",
            "Discovery",
            "Web",
            "Vendor",
            "Version",
            "Linked templates",
            "Linked to templates",
            "Tags",
        ):
            self.assertIn(f"<th scope=\"col\">{header}</th>", html, header)
        # Jumlah item mengikuti profil, jumlah host mengikuti data host nyata.
        self.assertIn("Items <span class=\"dc-tpl-count\">5</span>", html)
        self.assertIn("Triggers <span class=\"dc-tpl-count\">4</span>", html)
        self.assertIn('title="1 host using this template"', html)
        self.assertIn("Displaying 1 to 3 of 3 found", html)
        self.assertIn("class: network", html)
        self.assertIn("target: mikrotik", html)
        self.assertIn(
            '<a class="dc-tpl-link" href="/data-collection/templates?name=Generic">Generic</a>',
            html,
        )

    def test_templates_elemen_tampilan(self):
        html = self._page("/data-collection/templates")
        for marker in (
            'id="dcCreateOpen"',
            'id="dcImportOpen"',
            'id="dcFilterToggle"',
            'id="dcFilterPanel"',
            'id="dcTemplateGroupFilter"',
            'id="dcLinkedTemplateFilter"',
            'id="dcFilterName"',
            'id="dcTemplateVendorFilter"',
            'id="dcTemplateVersionFilter"',
            'id="dcTagRows"',
            'id="dcAddTag"',
            'data-tag-eval="and"',
            'data-tag-eval="or"',
            'id="dcSelectAll"',
            'class="dc-row-check"',
            'id="dcDeleteBtn"',
            'id="dcDeleteClearBtn"',
            'id="dcExportBtn"',
            'id="dcExportToggle"',
            'id="dcExportMenu"',
            "Mass update",
            "Delete and clear",
            'id="dcCreateModal"',
            "New template",
            "Template group",
            "Linked templates",
            'id="dcImportModal"',
            "Import templates",
            "Template JSON",
            ">Apply</button>",
            ">Reset</a>",
        ):
            self.assertIn(marker, html, marker)

    def test_templates_filter_nama_vendor_dan_sort(self):
        html = self._page("/data-collection/templates?name=mikro")
        self.assertIn("Displaying 1 to 1 of 1 found", html)
        self.assertIn(">MikroTik</a>", html)
        self.assertNotIn(">Auto-detect</a>", html)

        html = self._page("/data-collection/templates?vendor=zabbix&version=7.4")
        self.assertIn("Displaying 1 to 3 of 3 found", html)

        html = self._page("/data-collection/templates?vendor=tidak-ada")
        self.assertIn("No templates match this filter", html)
        self.assertIn("Displaying 0 of 0 found", html)

        html = self._page("/data-collection/templates?sort=name&order=desc")
        self.assertLess(html.index(">MikroTik</a>"), html.index(">Generic</a>"))
        self.assertLess(html.index(">Generic</a>"), html.index(">Auto-detect</a>"))

    def test_templates_filter_grup_linked_dan_tag(self):
        html = self._page(
            "/data-collection/templates?template_group=Network+device"
        )
        self.assertIn("Displaying 1 to 2 of 2 found", html)
        self.assertNotIn(">Auto-detect</a>", html)

        html = self._page("/data-collection/templates?linked=Generic")
        self.assertIn("Displaying 1 to 1 of 1 found", html)
        self.assertIn(">MikroTik</a>", html)

        html = self._page(
            "/data-collection/templates?tag=target&tag_op=contains&tag_value=mikrotik"
        )
        self.assertIn("Displaying 1 to 1 of 1 found", html)
        self.assertIn(">MikroTik</a>", html)

        html = self._page(
            "/data-collection/templates?tag=target&tag_op=contains&tag_value=generic"
        )
        self.assertIn("Displaying 1 to 1 of 1 found", html)
        self.assertIn(">Generic</a>", html)

    def test_template_buatan_dapat_dibuat_dan_dihapus(self):
        response = self.client.post(
            "/data-collection/templates/create",
            data={
                "name": "Kopegtel Access",
                "template_group": "Custom templates",
                "vendor": "Kopegtel",
                "version": "1.0",
                "tags": "class: access, target: kopegtel",
                "description": "Access network profile.",
            },
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Kopegtel Access", html)
        self.assertIn("class: access", html)
        self.assertIn("target: kopegtel", html)
        self.assertIn("Displaying 1 to 4 of 4 found", html)

        conn, c = m.get_db()
        try:
            row = c.execute(
                "SELECT id, name FROM templates WHERE name=?",
                ("Kopegtel Access",),
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)

        response = self.client.post(
            "/data-collection/templates/delete",
            data={"selected": [f"custom:{row['id']}"]},
            follow_redirects=True,
        )
        self.assertIn("1 template deleted.", response.get_data(as_text=True))
        html = self._page("/data-collection/templates")
        self.assertNotIn("Kopegtel Access", html)
        self.assertIn("Displaying 1 to 3 of 3 found", html)

    def test_template_bawaan_tidak_dapat_dihapus(self):
        response = self.client.post(
            "/data-collection/templates/delete",
            data={"selected": ["builtin:auto"]},
            follow_redirects=True,
        )
        html = response.get_data(as_text=True)
        self.assertIn("Built-in templates cannot be deleted.", html)
        self.assertIn("Auto-detect", html)

    def test_template_duplikat_ditolak(self):
        response = self.client.post(
            "/data-collection/templates/create",
            data={"name": "Auto-detect"},
            follow_redirects=True,
        )
        self.assertIn("already exists", response.get_data(as_text=True))

    def test_template_import(self):
        payload = (
            '[{"name": "Imported template", "template_group": "Custom templates",'
            ' "vendor": "Zabbix", "version": "7.4.15",'
            ' "tags": [{"name": "class", "value": "imported"}]},'
            ' {"name": "Imported template", "vendor": "Zabbix"}]'
        )
        response = self.client.post(
            "/data-collection/templates/import",
            data={"payload": payload},
            follow_redirects=True,
        )
        html = response.get_data(as_text=True)
        self.assertIn("1 template imported.", html)
        self.assertIn("1 template skipped", html)
        self.assertIn("Imported template", html)
        self.assertIn("class: imported", html)

    def test_template_export_csv_dan_json(self):
        response = self.client.get("/data-collection/templates/export?format=csv")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "text/csv")
        csv_text = response.get_data(as_text=True)
        self.assertIn("Name,Template groups,Hosts,Items", csv_text)
        self.assertIn("MikroTik", csv_text)
        self.assertIn("class: network", csv_text)

        response = self.client.get(
            "/data-collection/templates/export?format=json&selected=builtin:mikrotik"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "application/json")
        payload = response.get_json()
        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0]["Name"], "MikroTik")
        self.assertEqual(payload[0]["Items"], 5)

    def test_host_groups_dari_kategori_host(self):
        conn, c = m.get_db()
        try:
            c.execute(
                "INSERT INTO hosts (ip, alias, category) VALUES (?,?,?)",
                ("10.30.0.1", "ONT-Test", "ONT"),
            )
            c.execute(
                "INSERT INTO hosts (ip, alias, category) VALUES (?,?,?)",
                ("10.30.0.2", "MT-Test", "Mikrotik"),
            )
            conn.commit()
        finally:
            conn.close()

        html = self._page("/data-collection/host-groups")
        self.assertIn('<a class="dc-tg-link" href="/hosts">ONT</a>', html)
        self.assertIn('<a class="dc-tg-link" href="/hosts">Mikrotik</a>', html)
        # Hosts menghitung host nyata di tiap grup.
        self.assertIn('title="1 host">1</span>', html)
        # Info menampilkan host yang tergabung, seperti di Zabbix.
        self.assertIn(
            '<a class="dc-host-link" href="/hosts">ONT-Test</a>', html
        )
        self.assertIn(
            '<a class="dc-host-link" href="/hosts">MT-Test</a>', html
        )
        self.assertIn("Displaying 2 of 2 found", html)

    def test_host_groups_elemen_tampilan(self):
        conn, c = m.get_db()
        try:
            c.execute(
                "INSERT INTO hosts (ip, alias, category) VALUES (?,?,?)",
                ("10.30.0.1", "ONT-Test", "ONT"),
            )
            conn.commit()
        finally:
            conn.close()

        html = self._page("/data-collection/host-groups")
        for marker in (
            'id="dcCreateOpen"',
            "Create host group",
            'id="dcFilterToggle"',
            'id="dcFilterPanel"',
            'id="dcFilterName"',
            ">Apply</button>",
            ">Reset</a>",
            'id="dcSelectAll"',
            'class="dc-row-check"',
            'form="dc-delete-form"',
            'id="dcDeleteBtn"',
            ">Enable hosts</button>",
            ">Disable hosts</button>",
            "Displaying 1 of 1 found",
            "dc-hg-table",
            'class="dc-hg-hosts"',
            'class="dc-hg-info"',
            'id="dcCreateModal"',
            "New host group",
            "Group name",
            ">Add</button>",
            'data-dc-entity="host group"',
        ):
            self.assertIn(marker, html, marker)

    def test_host_groups_filter_nama(self):
        conn, c = m.get_db()
        try:
            c.execute(
                "INSERT INTO hosts (ip, alias, category) VALUES (?,?,?)",
                ("10.30.0.1", "ONT-Test", "ONT"),
            )
            c.execute(
                "INSERT INTO hosts (ip, alias, category) VALUES (?,?,?)",
                ("10.30.0.2", "MT-Test", "Mikrotik"),
            )
            conn.commit()
        finally:
            conn.close()

        html = self._page("/data-collection/host-groups?name=mikro")
        self.assertIn("Displaying 1 of 1 found", html)
        self.assertIn('<a class="dc-tg-link" href="/hosts">Mikrotik</a>', html)
        self.assertNotIn('<a class="dc-tg-link" href="/hosts">ONT</a>', html)

        html = self._page("/data-collection/host-groups?name=tidak-ada")
        self.assertIn("No host groups match this filter", html)
        self.assertIn("Displaying 0 of 0 found", html)

    def test_host_groups_urut_nama(self):
        conn, c = m.get_db()
        try:
            c.execute(
                "INSERT INTO hosts (ip, alias, category) VALUES (?,?,?)",
                ("10.30.0.1", "ONT-Test", "ONT"),
            )
            c.execute(
                "INSERT INTO hosts (ip, alias, category) VALUES (?,?,?)",
                ("10.30.0.2", "MT-Test", "Mikrotik"),
            )
            conn.commit()
        finally:
            conn.close()

        html = self._page("/data-collection/host-groups?sort=name&order=asc")
        self.assertLess(
            html.index('<a class="dc-tg-link" href="/hosts">Mikrotik</a>'),
            html.index('<a class="dc-tg-link" href="/hosts">ONT</a>'),
        )

        html = self._page("/data-collection/host-groups?sort=name&order=desc")
        self.assertLess(
            html.index('<a class="dc-tg-link" href="/hosts">ONT</a>'),
            html.index('<a class="dc-tg-link" href="/hosts">Mikrotik</a>'),
        )

    def test_host_group_buatan_dapat_dibuat_dan_dihapus(self):
        response = self.client.post(
            "/data-collection/host-groups/create",
            data={"name": "Lab devices"},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Lab devices", html)
        self.assertIn('title="0 hosts">0</span>', html)
        self.assertIn("No hosts", html)

        conn, c = m.get_db()
        try:
            row = c.execute(
                "SELECT id, name FROM host_groups WHERE name=?",
                ("Lab devices",),
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)

        response = self.client.post(
            "/data-collection/host-groups/delete",
            data={"selected": [f"custom:{row['id']}"]},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("1 host group deleted.", response.get_data(as_text=True))
        html = self._page("/data-collection/host-groups")
        self.assertNotIn("Lab devices", html)

    def test_host_group_kategori_tidak_dapat_dihapus(self):
        conn, c = m.get_db()
        try:
            c.execute(
                "INSERT INTO hosts (ip, alias, category) VALUES (?,?,?)",
                ("10.30.0.1", "ONT-Test", "ONT"),
            )
            conn.commit()
        finally:
            conn.close()

        response = self.client.post(
            "/data-collection/host-groups/delete",
            data={"selected": ["builtin:ONT"]},
            follow_redirects=True,
        )
        html = response.get_data(as_text=True)
        self.assertIn(
            "Host groups derived from host categories cannot be deleted.", html
        )
        self.assertIn('<a class="dc-tg-link" href="/hosts">ONT</a>', html)

    def test_maintenance_menampilkan_status(self):
        conn, c = m.get_db()
        try:
            c.execute(
                "INSERT INTO hosts (ip, alias, category) VALUES (?,?,?)",
                ("10.30.0.1", "ONT-Test", "ONT"),
            )
            c.execute(
                "INSERT INTO maintenance_windows (host, start_at, end_at, reason, created_by, created_at) "
                "VALUES (?,?,?,?,?,?)",
                ("10.30.0.1", _now(-30), _now(30), "Upgrade firmware", "admin", _now(-60)),
            )
            c.execute(
                "INSERT INTO maintenance_windows (host, start_at, end_at, reason, created_by, created_at) "
                "VALUES (?,?,?,?,?,?)",
                ("10.30.0.1", _now(60), _now(120), "Pembersihan rutin", "admin", _now(-60)),
            )
            conn.commit()
        finally:
            conn.close()

        html = self._page("/data-collection/maintenance")
        self.assertIn("Upgrade firmware", html)
        self.assertIn("Pembersihan rutin", html)
        self.assertIn('<span class="dc-status active">Active</span>', html)
        self.assertIn('<span class="dc-status upcoming">Upcoming</span>', html)

    def test_event_correlation_kosong(self):
        html = self._page("/data-collection/event-correlation")
        self.assertIn("No event correlation rules found", html)
        self.assertIn("Displaying 0 of 0 found", html)

    def test_hosts_dan_discovery_memakai_halaman_yang_sama(self):
        html = self._page("/data-collection/hosts")
        self.assertIn("<title>Hosts", html)
        self.assertIn('id="hostModalBody"', html)

        html = self._page("/data-collection/discovery")
        self.assertIn("<title>Status of discovery", html)
        self.assertIn('id="btnScanOpen"', html)


if __name__ == "__main__":
    unittest.main()
