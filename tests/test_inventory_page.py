"""Tes halaman Inventory: Overview (/inventory) dan Hosts (/inventory/hosts)."""

import json
import os
import tempfile
import unittest

_tmp = tempfile.mkdtemp(prefix="nms_test_inventory_")
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

XRW = {"X-Requested-With": "XMLHttpRequest"}


def _inv(mode, **fields):
    return json.dumps({"mode": mode, "fields": fields}, ensure_ascii=False)


class InventoryPageTest(unittest.TestCase):
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
        c.execute("DELETE FROM hosts")
        c.execute(
            "INSERT INTO hosts (ip, alias, category, host_inventory) VALUES (?,?,?,?)",
            (
                "10.10.10.1",
                "SW-Core-1",
                "Core",
                _inv(
                    "manual",
                    name="SW-Core-1",
                    vendor="Cisco",
                    type="Switch",
                    model="C9300",
                    os="IOS-XE",
                    location="Kopegtel Pusat",
                    site_rack="R1",
                    asset_tag="AST-001",
                    serialno_a="SN-001",
                    deployment_status="aktif",
                    poc_1_name="Sari",
                ),
            ),
        )
        c.execute(
            "INSERT INTO hosts (ip, alias, category, host_inventory) VALUES (?,?,?,?)",
            (
                "10.10.10.2",
                "SW-Akses-2",
                "Akses",
                _inv(
                    "automatic",
                    name="SW-Akses-2",
                    vendor="Cisco",
                    type="Switch",
                    location="Kopegtel Pusat",
                    deployment_status="aktif",
                ),
            ),
        )
        c.execute(
            "INSERT INTO hosts (ip, alias, category, host_inventory) VALUES (?,?,?,?)",
            (
                "10.10.10.3",
                "RB-Cabang",
                "Router",
                _inv(
                    "manual",
                    name="RB-Cabang",
                    vendor="MikroTik",
                    type="Router",
                    location="Cabang Utara",
                    deployment_status="gudang",
                ),
            ),
        )
        c.execute(
            "INSERT INTO hosts (ip, alias, category, host_inventory) VALUES (?,?,?,?)",
            ("10.10.10.4", "Host-Kosong", "Uncategorized", _inv("disabled")),
        )
        conn.commit()
        conn.close()

    # ---------- halaman ----------
    def test_halaman_butuh_login(self):
        anon = m.app.test_client()
        r = anon.get("/inventory")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login", r.headers.get("Location", ""))
        r2 = anon.get("/inventory/hosts")
        self.assertEqual(r2.status_code, 302)
        self.assertIn("/login", r2.headers.get("Location", ""))

    def test_api_butuh_login(self):
        anon = m.app.test_client()
        for url in ("/api/inventory/overview", "/api/inventory/hosts"):
            r = anon.get(url, headers=XRW)
            self.assertEqual(r.status_code, 401, url)
        r = anon.patch(
            "/api/hosts/10.10.10.1/inventory",
            json={"fields": {"name": "X"}},
            headers=XRW,
        )
        self.assertEqual(r.status_code, 401)

    def test_halaman_overview_terender(self):
        r = self.client.get("/inventory")
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        self.assertIn("Host inventory overview", html)
        self.assertIn('href="/inventory/hosts"', html)
        self.assertIn("js/inventory.js", html)
        # Elemen filter & tabel mengikuti gaya halaman inventory Zabbix.
        self.assertIn("Host groups", html)
        self.assertIn("Grouping by", html)
        self.assertIn("not selected", html)
        self.assertIn("Host count", html)
        self.assertIn("btnInvSelectGroups", html)
        self.assertIn("invGroupDialog", html)
        self.assertIn("Field", html)
        # Dialog pemilih host groups: header "Name" + checkbox pilih semua,
        # baris diisi JS, footer Select/Cancel rata kanan.
        self.assertIn("inv-group-table", html)
        self.assertIn("invGroupSelectAll", html)
        self.assertIn(">Name</th>", html)
        self.assertIn('id="btnInvGroupApply"', html)
        self.assertIn('id="btnInvGroupCancel"', html)
        self.assertNotIn("invGroupSearch", html)

    def test_halaman_hosts_terender(self):
        r = self.client.get("/inventory/hosts")
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        self.assertIn("Host inventory", html)
        self.assertIn('href="/inventory"', html)
        self.assertIn("js/inventory_hosts.js", html)
        # Filter gaya Zabbix: Host groups + Field/operator/value.
        self.assertIn('id="invHostGroups"', html)
        self.assertIn('id="btnInvSelectGroups"', html)
        self.assertIn('id="invField"', html)
        self.assertIn('id="invOperator"', html)
        self.assertIn('id="invFieldValue"', html)
        self.assertIn(">Alias</option>", html)
        self.assertIn(">contains</option>", html)
        self.assertIn(">does not exist</option>", html)
        self.assertIn('id="btnApplyInvFilter"', html)
        self.assertIn('id="btnResetInvFilter"', html)
        # Dialog pemilih host groups: checkbox pilih semua + daftar group.
        self.assertIn('id="invGroupDialog"', html)
        self.assertIn('id="invGroupSelectAll"', html)
        self.assertIn('id="invGroupList"', html)
        self.assertIn('id="btnInvGroupApply"', html)
        self.assertIn('id="btnInvGroupCancel"', html)
        # Tabel 8 kolom dan footer bergaya Zabbix.
        self.assertIn('class="inv-col-host"', html)
        for label in (
            "Group",
            "Name",
            "Type",
            "OS",
            "Serial number A",
            "Tag",
            "MAC address A",
        ):
            self.assertIn(">" + label + "</th>", html)
        self.assertIn('id="invSortHost"', html)
        self.assertIn('aria-sort="ascending"', html)
        self.assertIn('id="invFootInfo"', html)
        self.assertIn("Displaying 0 of 0 found", html)
        self.assertIn("Zabbix 7.4.15", html)
        # Dialog edit inventory: enam field tabel + Save/Cancel.
        self.assertIn('id="invEditDialog"', html)
        self.assertIn('id="invEditForm"', html)
        self.assertIn('id="invEditHost"', html)
        for field_id, label in (
            ("invEditName", "Name"),
            ("invEditType", "Type"),
            ("invEditOs", "OS"),
            ("invEditSerial", "Serial number A"),
            ("invEditTag", "Tag"),
            ("invEditMac", "MAC address A"),
        ):
            self.assertIn('id="' + field_id + '"', html)
            self.assertIn(">" + label + "</label>", html)
        self.assertIn('id="btnSaveInvEdit"', html)
        self.assertIn('id="btnCancelInvEdit"', html)
        self.assertIn('id="btnCloseInvEdit"', html)

    def test_menu_inventory_ada_di_sidebar_halaman_lain(self):
        r = self.client.get("/hosts")
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        self.assertIn('href="/inventory" class="nav-link nav-sub-link"', html)
        self.assertIn('href="/inventory/hosts" class="nav-link nav-sub-link"', html)

    # ---------- API overview ----------
    def test_overview_ringkasan_dan_urutan_grup(self):
        r = self.client.get("/api/inventory/overview?group_by=vendor", headers=XRW)
        self.assertEqual(r.status_code, 200)
        data = r.get_json()

        self.assertEqual(data["group_by"], "vendor")
        summary = data["summary"]
        self.assertEqual(summary["hosts_total"], 4)
        self.assertEqual(summary["hosts_with_inventory"], 3)
        self.assertEqual(summary["hosts_filled"], 3)
        self.assertEqual(summary["vendors"], 2)
        self.assertEqual(summary["locations"], 2)

        groups = data["groups"]
        self.assertEqual(groups[0], {"value": "Cisco", "count": 2, "empty": False})
        self.assertEqual(groups[1]["value"], "MikroTik")
        self.assertEqual(groups[1]["count"], 1)
        self.assertTrue(groups[-1]["empty"])
        self.assertEqual(groups[-1]["value"], m.INVENTORY_EMPTY_LABEL)
        self.assertEqual(groups[-1]["count"], 1)

    def test_overview_group_by_tidak_dikenal_jadi_not_selected(self):
        r = self.client.get(
            "/api/inventory/overview?group_by=asal-sekolah", headers=XRW
        )
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertEqual(data["group_by"], "")
        self.assertEqual(data["groups"], [])

    def test_overview_awal_belum_pilih_field(self):
        """Tanpa group_by tabel dibiarkan kosong ("not selected")."""
        data = self.client.get("/api/inventory/overview", headers=XRW).get_json()
        self.assertEqual(data["group_by"], "")
        self.assertEqual(data["groups"], [])
        self.assertEqual(data["summary"]["hosts_total"], 4)

    def test_overview_daftar_host_groups(self):
        data = self.client.get("/api/inventory/overview", headers=XRW).get_json()
        self.assertEqual(
            data["host_groups"], ["Akses", "Core", "Router", "Uncategorized"]
        )

    def test_overview_filter_host_groups(self):
        r = self.client.get(
            "/api/inventory/overview?group_by=vendor&groups=Core", headers=XRW
        )
        data = r.get_json()
        self.assertEqual(data["summary"]["hosts_total"], 1)
        self.assertEqual(
            data["groups"], [{"value": "Cisco", "count": 1, "empty": False}]
        )
        # Daftar group tetap lengkap agar pemilih tidak kehilangan pilihan.
        self.assertEqual(
            data["host_groups"], ["Akses", "Core", "Router", "Uncategorized"]
        )

    def test_overview_filter_host_groups_banyak_nilai(self):
        data = self.client.get(
            "/api/inventory/overview?group_by=vendor&groups=Core,Router", headers=XRW
        ).get_json()
        self.assertEqual(data["summary"]["hosts_total"], 2)
        values = {g["value"]: g["count"] for g in data["groups"]}
        self.assertEqual(values, {"Cisco": 1, "MikroTik": 1})

    def test_overview_group_by_location(self):
        r = self.client.get("/api/inventory/overview?group_by=location", headers=XRW)
        data = r.get_json()
        self.assertEqual(data["group_by"], "location")
        values = {g["value"]: g["count"] for g in data["groups"]}
        self.assertEqual(values["Kopegtel Pusat"], 2)
        self.assertEqual(values["Cabang Utara"], 1)
        self.assertEqual(values[m.INVENTORY_EMPTY_LABEL], 1)

    def test_overview_daftar_field_untuk_pilihan(self):
        data = self.client.get("/api/inventory/overview", headers=XRW).get_json()
        keys = [f["key"] for f in data["fields"]]
        self.assertIn("vendor", keys)
        self.assertIn("location", keys)
        self.assertEqual(len(keys), len(set(keys)))

    # ---------- API hosts ----------
    def test_hosts_daftar_dan_field(self):
        data = self.client.get("/api/inventory/hosts", headers=XRW).get_json()
        self.assertEqual(data["total"], 4)
        self.assertEqual(data["matched"], 4)
        row = [r for r in data["rows"] if r["ip"] == "10.10.10.1"][0]
        self.assertEqual(row["name"], "SW-Core-1")
        self.assertEqual(row["url"], "/host/10.10.10.1")
        self.assertEqual(row["fields"]["vendor"], "Cisco")
        self.assertEqual(row["fields"]["serialno_a"], "SN-001")
        self.assertEqual(row["mode"], "manual")
        self.assertGreater(row["filled"], 5)

    def test_hosts_filter_q_vendor_dan_location(self):
        data = self.client.get("/api/inventory/hosts?q=core", headers=XRW).get_json()
        self.assertEqual([r["ip"] for r in data["rows"]], ["10.10.10.1"])

        data = self.client.get(
            "/api/inventory/hosts?vendor=Cisco", headers=XRW
        ).get_json()
        self.assertEqual(data["matched"], 2)

        data = self.client.get(
            "/api/inventory/hosts?location=Cabang%20Utara", headers=XRW
        ).get_json()
        self.assertEqual([r["ip"] for r in data["rows"]], ["10.10.10.3"])

        data = self.client.get(
            "/api/inventory/hosts?vendor=Cisco&deployment_status=gudang", headers=XRW
        ).get_json()
        self.assertEqual(data["rows"], [])
        self.assertEqual(data["total"], 4)

    def test_hosts_filter_option_hanya_nilai_terisi(self):
        data = self.client.get("/api/inventory/hosts", headers=XRW).get_json()
        self.assertEqual(data["options"]["vendor"], ["Cisco", "MikroTik"])
        self.assertEqual(
            data["options"]["location"], ["Cabang Utara", "Kopegtel Pusat"]
        )
        self.assertEqual(data["options"]["deployment_status"], ["aktif", "gudang"])

    def test_hosts_daftar_host_groups_dan_fields(self):
        data = self.client.get("/api/inventory/hosts", headers=XRW).get_json()
        self.assertEqual(
            data["host_groups"], ["Akses", "Core", "Router", "Uncategorized"]
        )
        labels = {f["key"]: f["label"] for f in data["fields"]}
        self.assertEqual(labels["alias"], "Alias")
        self.assertEqual(labels["serialno_a"], "Serial number A")
        self.assertEqual(labels["macaddress_a"], "MAC address A")
        self.assertEqual(len(labels), len(set(labels)))

    def test_hosts_filter_groups(self):
        data = self.client.get(
            "/api/inventory/hosts?groups=Core", headers=XRW
        ).get_json()
        self.assertEqual([r["ip"] for r in data["rows"]], ["10.10.10.1"])
        self.assertEqual(data["matched"], 1)
        self.assertEqual(data["total"], 4)
        # Daftar group tetap lengkap agar pemilih tidak kehilangan pilihan.
        self.assertEqual(
            data["host_groups"], ["Akses", "Core", "Router", "Uncategorized"]
        )

        data = self.client.get(
            "/api/inventory/hosts?groups=Core,Router", headers=XRW
        ).get_json()
        self.assertEqual([r["ip"] for r in data["rows"]], ["10.10.10.1", "10.10.10.3"])

        # Nama group tidak peka huruf besar/kecil.
        data = self.client.get(
            "/api/inventory/hosts?groups=core", headers=XRW
        ).get_json()
        self.assertEqual([r["ip"] for r in data["rows"]], ["10.10.10.1"])

    def test_hosts_filter_field_operator_value(self):
        data = self.client.get(
            "/api/inventory/hosts?field=alias&operator=contains&value=SW",
            headers=XRW,
        ).get_json()
        self.assertEqual([r["ip"] for r in data["rows"]], ["10.10.10.1", "10.10.10.2"])

        data = self.client.get(
            "/api/inventory/hosts?field=alias&operator=equals&value=RB-Cabang",
            headers=XRW,
        ).get_json()
        self.assertEqual([r["ip"] for r in data["rows"]], ["10.10.10.3"])

        data = self.client.get(
            "/api/inventory/hosts?field=alias&operator=does_not_contain&value=SW",
            headers=XRW,
        ).get_json()
        self.assertEqual([r["ip"] for r in data["rows"]], ["10.10.10.3", "10.10.10.4"])

        data = self.client.get(
            "/api/inventory/hosts?field=vendor&operator=exists", headers=XRW
        ).get_json()
        self.assertEqual(data["matched"], 3)

        data = self.client.get(
            "/api/inventory/hosts?field=vendor&operator=does_not_exist",
            headers=XRW,
        ).get_json()
        self.assertEqual([r["ip"] for r in data["rows"]], ["10.10.10.4"])

        data = self.client.get(
            "/api/inventory/hosts?field=os&operator=contains&value=IOS",
            headers=XRW,
        ).get_json()
        self.assertEqual([r["ip"] for r in data["rows"]], ["10.10.10.1"])

    def test_hosts_filter_field_dan_group_digabung(self):
        data = self.client.get(
            "/api/inventory/hosts?groups=Core,Akses&field=vendor"
            "&operator=equals&value=Cisco",
            headers=XRW,
        ).get_json()
        self.assertEqual([r["ip"] for r in data["rows"]], ["10.10.10.1", "10.10.10.2"])

        data = self.client.get(
            "/api/inventory/hosts?groups=Core&field=vendor&operator=equals&value=MikroTik",
            headers=XRW,
        ).get_json()
        self.assertEqual(data["rows"], [])
        self.assertEqual(data["matched"], 0)
        self.assertEqual(data["total"], 4)

    def test_hosts_filter_tidak_dikenal_jadi_default(self):
        data = self.client.get(
            "/api/inventory/hosts?field=bogus&operator=bogus&value=SW",
            headers=XRW,
        ).get_json()
        # Field/operator tidak dikenal jatuh ke alias/contains.
        self.assertEqual([r["ip"] for r in data["rows"]], ["10.10.10.1", "10.10.10.2"])

    def test_edit_inventory_merge_dan_pertahankan_field_lain(self):
        r = self.client.patch(
            "/api/hosts/10.10.10.1/inventory",
            json={
                "fields": {
                    "name": "SW-Core-1 Baru",
                    "type": "Switch Core",
                    "os": "IOS-XE 17",
                    "serialno_a": "SN-002",
                    "tag": "produksi",
                    "macaddress_a": "AA:BB:CC:DD:EE:01",
                }
            },
            headers=XRW,
        )
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["mode"], "manual")
        self.assertEqual(data["fields"]["name"], "SW-Core-1 Baru")
        self.assertEqual(data["fields"]["tag"], "produksi")
        # Field di luar dialog tetap dipertahankan.
        self.assertEqual(data["fields"]["vendor"], "Cisco")
        self.assertEqual(data["fields"]["location"], "Kopegtel Pusat")
        self.assertEqual(data["fields"]["asset_tag"], "AST-001")

        row = [
            r
            for r in self.client.get("/api/inventory/hosts", headers=XRW).get_json()[
                "rows"
            ]
            if r["ip"] == "10.10.10.1"
        ][0]
        self.assertEqual(row["fields"]["name"], "SW-Core-1 Baru")
        self.assertEqual(row["fields"]["vendor"], "Cisco")

    def test_edit_inventory_partial_tidak_menghapus_field_lain(self):
        r = self.client.patch(
            "/api/hosts/10.10.10.1/inventory",
            json={"fields": {"tag": "baru"}},
            headers=XRW,
        )
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertEqual(data["fields"]["tag"], "baru")
        # Field yang tidak dikirim tetap seperti semula.
        self.assertEqual(data["fields"]["name"], "SW-Core-1")
        self.assertEqual(data["fields"]["type"], "Switch")
        self.assertEqual(data["fields"]["os"], "IOS-XE")
        self.assertEqual(data["fields"]["serialno_a"], "SN-001")
        self.assertEqual(data["fields"]["vendor"], "Cisco")

    def test_edit_inventory_kosong_menghapus_field_dan_pertahankan_lain(self):
        r = self.client.patch(
            "/api/hosts/10.10.10.1/inventory",
            json={
                "fields": {
                    "name": "",
                    "type": "",
                    "os": "",
                    "serialno_a": "",
                    "tag": "",
                    "macaddress_a": "",
                }
            },
            headers=XRW,
        )
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        # Masih ada field lain, jadi mode tetap manual.
        self.assertEqual(data["mode"], "manual")
        for key in m.INVENTORY_EDIT_FIELDS:
            self.assertNotIn(key, data["fields"])
        # Field lain tidak ikut terhapus.
        self.assertEqual(data["fields"]["vendor"], "Cisco")

    def test_edit_inventory_semua_field_kosong_mode_disabled(self):
        conn, c = m.get_db()
        c.execute(
            "UPDATE hosts SET host_inventory=? WHERE ip=?",
            (_inv("manual", name="Sementara", tag="uji"), "10.10.10.4"),
        )
        conn.commit()
        conn.close()
        r = self.client.patch(
            "/api/hosts/10.10.10.4/inventory",
            json={"fields": {key: "" for key in m.INVENTORY_EDIT_FIELDS}},
            headers=XRW,
        )
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertEqual(data["mode"], "disabled")
        self.assertEqual(data["fields"], {})

    def test_edit_inventory_host_tidak_ditemukan(self):
        r = self.client.patch(
            "/api/hosts/10.99.99.99/inventory",
            json={"fields": {"name": "X"}},
            headers=XRW,
        )
        self.assertEqual(r.status_code, 404)

    def test_edit_inventory_format_salah(self):
        for payload in ({"fields": "bukan objek"}, []):
            r = self.client.patch(
                "/api/hosts/10.10.10.1/inventory",
                json=payload,
                headers=XRW,
            )
            self.assertEqual(r.status_code, 400, payload)

    def test_inventory_rusak_diabaikan(self):
        conn, c = m.get_db()
        c.execute(
            "UPDATE hosts SET host_inventory=? WHERE ip=?",
            ("{bukan json", "10.10.10.4"),
        )
        conn.commit()
        conn.close()
        data = self.client.get("/api/inventory/hosts", headers=XRW).get_json()
        row = [r for r in data["rows"] if r["ip"] == "10.10.10.4"][0]
        self.assertEqual(row["mode"], "disabled")
        self.assertEqual(row["fields"], {})


if __name__ == "__main__":
    unittest.main()
