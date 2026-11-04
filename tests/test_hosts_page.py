"""Uji halaman /hosts dan API /api/hosts/zbx (gaya Zabbix).

Availability: ping (memori status), SNMP (kebiasaan device_health),
agent (kebiasaan agent_metrics). Filter: name, ip, group, severity,
status, only_problems, sort. Juga uji tombol Tambah host (POST /api/hosts).
"""

import json
import os
import re
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
        self.assertIn("Create host", body)
        self.assertIn('href="/hosts" class="nav-link nav-sub-link active"', body)
        self.assertIn('<option value="md2">MD2</option>', body)
        self.assertIn('<option value="rmcp+">RMCP+</option>', body)
        self.assertIn('<option value="user" selected>User</option>', body)
        self.assertIn('<option value="administrator">Admin</option>', body)
        self.assertIn('id="hostTagRows"', body)
        self.assertIn('placeholder="tag"', body)
        self.assertIn('id="hostMacroRows"', body)
        self.assertIn("Inherited and host macros", body)
        self.assertIn('id="hostMacroInheritedRows"', body)
        self.assertIn('id="hostMacroInheritedAdd"', body)
        self.assertIn("Effective value", body)
        self.assertIn("Template value", body)
        self.assertIn("Global value (configure)", body)
        self.assertNotIn("Template dan global macro belum tersedia", body)
        self.assertIn('placeholder="{$MACRO}"', body)
        # Inventory: tiga mode + 70 field sesuai urutan Zabbix.
        self.assertIn('id="hostInventory"', body)
        self.assertIn('id="inventoryModes"', body)
        self.assertIn('data-inv-mode="disabled" aria-pressed="true"', body)
        self.assertIn('data-inv-mode="manual"', body)
        self.assertIn('data-inv-mode="automatic"', body)
        inventory_keys = re.findall(r'data-inv-key="([^"]+)"', body)
        self.assertEqual(
            inventory_keys,
            [
                "type",
                "type_full",
                "name",
                "alias",
                "os",
                "os_full",
                "os_short",
                "serialno_a",
                "serialno_b",
                "tag",
                "asset_tag",
                "macaddress_a",
                "macaddress_b",
                "hardware",
                "hardware_full",
                "software",
                "software_full",
                "software_app_a",
                "software_app_b",
                "software_app_c",
                "software_app_d",
                "software_app_e",
                "contact",
                "location",
                "location_lat",
                "location_lon",
                "notes",
                "chassis",
                "model",
                "hw_arch",
                "vendor",
                "contract_number",
                "installer_name",
                "deployment_status",
                "url_a",
                "url_b",
                "url_c",
                "host_networks",
                "host_netmask",
                "host_router",
                "oob_ip",
                "oob_netmask",
                "oob_router",
                "date_hw_purchase",
                "date_hw_install",
                "date_hw_expiry",
                "date_hw_decomm",
                "site_address_a",
                "site_address_b",
                "site_address_c",
                "site_city",
                "site_state",
                "site_country",
                "site_zip",
                "site_rack",
                "site_notes",
                "poc_1_name",
                "poc_1_email",
                "poc_1_phone_a",
                "poc_1_phone_b",
                "poc_1_cell",
                "poc_1_screen",
                "poc_1_notes",
                "poc_2_name",
                "poc_2_email",
                "poc_2_phone_a",
                "poc_2_phone_b",
                "poc_2_cell",
                "poc_2_screen",
                "poc_2_notes",
            ],
        )
        self.assertEqual(
            len(re.findall(r"<textarea[^>]+data-inv-key=", body)),
            9,
        )
        self.assertIn("Hardware (Full details)", body)
        self.assertIn("Site state / province", body)
        self.assertIn("Secondary POC notes", body)
        # Encryption: dua arah koneksi sesuai screenshot Zabbix.
        self.assertIn('id="paneEncryption"', body)
        self.assertIn("Connections to host", body)
        self.assertIn("Connections from host", body)
        self.assertIn('id="encryptionToHost"', body)
        self.assertIn('data-enc-to="none" aria-pressed="true"', body)
        self.assertIn('data-enc-to="psk"', body)
        self.assertIn('data-enc-to="certificate"', body)
        self.assertIn('id="encryptionFromHost"', body)
        self.assertIn('data-enc-from="none" checked', body)
        self.assertIn('data-enc-from="psk"', body)
        self.assertIn('data-enc-from="certificate"', body)
        self.assertEqual(len(re.findall(r"data-enc-to=", body)), 3)
        self.assertEqual(len(re.findall(r"<input[^>]+data-enc-from=", body)), 3)
        # Detail PSK: tampil hanya saat PSK dipilih, identity + key wajib.
        self.assertIn('id="encryptionPskFields" hidden', body)
        self.assertIn('class="host-encryption-label req" for="encPskIdentity"', body)
        self.assertIn('class="host-encryption-label req" for="encPsk"', body)
        self.assertIn('id="encPskIdentity" type="text"', body)
        self.assertIn('id="encPsk" type="password"', body)
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

    def test_tambah_host_ipmi_tersimpan(self):
        r = self.client.post(
            "/api/hosts",
            json={
                "ip": "10.77.9.10",
                "alias": "Host IPMI",
                "category": "Tg",
                "ipmi_auth": "rmcp+",
                "ipmi_privilege": "operator",
                "ipmi_username": "admin-ipmi",
                "ipmi_password": "rahasia-ipmi",
                "tags": [
                    {"name": "site", "value": "jakarta"},
                    {"name": "env", "value": "prod"},
                    {"name": "", "value": ""},
                ],
                "macros": [
                    {
                        "name": "{$SITE}",
                        "value": "jakarta",
                        "description": "Lokasi",
                        "type": "text",
                    },
                    {
                        "name": "{$TOKEN}",
                        "value": "rahasia-macro",
                        "description": "Token API",
                        "type": "secret",
                    },
                    {"name": "", "value": "", "description": ""},
                ],
            },
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        self.assertEqual(r.status_code, 200)

        conn, c = m.get_db()
        row = c.execute(
            "SELECT ipmi_auth, ipmi_privilege, ipmi_username, ipmi_password,"
            " host_tags, host_macros FROM hosts WHERE ip='10.77.9.10'"
        ).fetchone()
        conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(row["ipmi_auth"], "rmcp+")
        self.assertEqual(row["ipmi_privilege"], "operator")
        self.assertEqual(row["ipmi_username"], "admin-ipmi")
        self.assertNotEqual(row["ipmi_password"], "rahasia-ipmi")
        self.assertTrue(row["ipmi_password"].startswith("enc$"))
        self.assertEqual(
            json.loads(row["host_tags"]),
            [
                {"name": "site", "value": "jakarta"},
                {"name": "env", "value": "prod"},
            ],
        )
        stored_macros = json.loads(row["host_macros"])
        self.assertEqual(stored_macros[0]["name"], "{$SITE}")
        self.assertEqual(stored_macros[0]["value"], "jakarta")
        self.assertEqual(stored_macros[0]["type"], "text")
        self.assertEqual(stored_macros[1]["name"], "{$TOKEN}")
        self.assertEqual(stored_macros[1]["type"], "secret")
        self.assertTrue(stored_macros[1]["value"].startswith("enc$"))
        self.assertNotEqual(stored_macros[1]["value"], "rahasia-macro")

        data = self.client.get("/api/hosts").get_json()
        host = next(h for h in data if h["ip"] == "10.77.9.10")
        self.assertEqual(host["ipmi_auth"], "rmcp+")
        self.assertEqual(host["ipmi_privilege"], "operator")
        self.assertEqual(host["ipmi_username"], "admin-ipmi")
        self.assertTrue(host["ipmi_password_set"])
        self.assertNotIn("ipmi_password", host)
        self.assertEqual(
            host["tags"],
            [
                {"name": "site", "value": "jakarta"},
                {"name": "env", "value": "prod"},
            ],
        )
        self.assertEqual(host["macros"][0]["name"], "{$SITE}")
        self.assertEqual(host["macros"][0]["value"], "jakarta")
        self.assertEqual(host["macros"][0]["type"], "text")
        self.assertEqual(host["macros"][1]["name"], "{$TOKEN}")
        self.assertEqual(host["macros"][1]["value"], "")
        self.assertTrue(host["macros"][1]["value_set"])
        self.assertEqual(host["macros"][1]["type"], "secret")
        self.assertNotIn("rahasia-macro", json.dumps(host))
        zbx_host = self.by_ip(self.get_json(), "10.77.9.10")
        self.assertEqual(
            zbx_host["tags"],
            [
                {"name": "site", "value": "jakarta"},
                {"name": "env", "value": "prod"},
            ],
        )
        self.assertEqual(zbx_host["macros"][0]["name"], "{$SITE}")
        self.assertEqual(zbx_host["macros"][0]["value"], "jakarta")
        self.assertEqual(zbx_host["macros"][1]["value"], "")
        self.assertTrue(zbx_host["macros"][1]["value_set"])
        self.assertNotIn("rahasia-macro", json.dumps(zbx_host))

    def test_tambah_host_tag_tanpa_nama_ditolak(self):
        r = self.client.post(
            "/api/hosts",
            json={
                "ip": "10.77.9.11",
                "category": "Tg",
                "tags": [{"name": "", "value": "jakarta"}],
            },
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        self.assertEqual(r.status_code, 400)
        self.assertIn("Nama tag", r.get_json()["error"])

    def test_tambah_host_macro_tidak_valid_ditolak(self):
        headers = {"X-Requested-With": "XMLHttpRequest"}
        r = self.client.post(
            "/api/hosts",
            json={
                "ip": "10.77.9.12",
                "category": "Tg",
                "macros": [
                    {"name": "", "value": "x", "description": "", "type": "text"}
                ],
            },
            headers=headers,
        )
        self.assertEqual(r.status_code, 400)
        self.assertIn("Nama macro", r.get_json()["error"])

        r2 = self.client.post(
            "/api/hosts",
            json={
                "ip": "10.77.9.12",
                "category": "Tg",
                "macros": [
                    {
                        "name": "SITE",
                        "value": "x",
                        "description": "",
                        "type": "text",
                    }
                ],
            },
            headers=headers,
        )
        self.assertEqual(r2.status_code, 400)
        self.assertIn("Format macro", r2.get_json()["error"])

    def test_tambah_host_inventory_tersimpan(self):
        inventory = {
            "mode": "manual",
            "fields": {
                "type": "Router",
                "name": "Core Router",
                "hardware_full": "line 1\nline 2",
                "location_lat": "-6.200000",
                "poc_1_name": "Budi",
                "unknown_field": "diabaikan",
            },
        }
        r = self.client.post(
            "/api/hosts",
            json={
                "ip": "10.77.9.13",
                "category": "Tg",
                "inventory": inventory,
            },
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        self.assertEqual(r.status_code, 200)

        conn, c = m.get_db()
        row = c.execute(
            "SELECT host_inventory FROM hosts WHERE ip='10.77.9.13'"
        ).fetchone()
        conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(
            json.loads(row["host_inventory"]),
            {
                "mode": "manual",
                "fields": {
                    "type": "Router",
                    "name": "Core Router",
                    "hardware_full": "line 1\nline 2",
                    "location_lat": "-6.200000",
                    "poc_1_name": "Budi",
                },
            },
        )

        data = self.client.get("/api/hosts").get_json()
        host = next(h for h in data if h["ip"] == "10.77.9.13")
        self.assertEqual(host["inventory"]["mode"], "manual")
        self.assertEqual(host["inventory"]["fields"]["hardware_full"], "line 1\nline 2")
        self.assertNotIn("unknown_field", host["inventory"]["fields"])
        zbx_host = self.by_ip(self.get_json(), "10.77.9.13")
        self.assertEqual(zbx_host["inventory"], host["inventory"])

    def test_tambah_host_inventory_tidak_valid_ditolak(self):
        headers = {"X-Requested-With": "XMLHttpRequest"}
        for inventory in (
            {"mode": "otomatis", "fields": {}},
            {"mode": "manual", "fields": []},
            ["bukan", "objek"],
        ):
            r = self.client.post(
                "/api/hosts",
                json={
                    "ip": "10.77.9.14",
                    "category": "Tg",
                    "inventory": inventory,
                },
                headers=headers,
            )
            self.assertEqual(r.status_code, 400)
            self.assertIn("inventory", r.get_json()["error"].lower())

    def test_tambah_host_encryption_tersimpan(self):
        psk_key = "0123456789abcdef" * 4
        encryption = {
            "to_host": "psk",
            "from_host": ["certificate", "psk"],
            "psk_identity": "zbx-host-psk",
            "psk": psk_key,
        }
        r = self.client.post(
            "/api/hosts",
            json={
                "ip": "10.77.9.15",
                "category": "Tg",
                "encryption": encryption,
            },
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        self.assertEqual(r.status_code, 200)

        conn, c = m.get_db()
        row = c.execute(
            "SELECT host_encryption FROM hosts WHERE ip='10.77.9.15'"
        ).fetchone()
        conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(json.loads(row["host_encryption"]), encryption)
        public_encryption = {
            "to_host": "psk",
            "from_host": ["certificate", "psk"],
            "psk_identity": "zbx-host-psk",
            "psk_set": True,
        }
        host = next(
            h
            for h in self.client.get("/api/hosts").get_json()
            if h["ip"] == "10.77.9.15"
        )
        self.assertEqual(host["encryption"], public_encryption)
        self.assertNotIn("psk", host["encryption"])
        self.assertNotIn(psk_key, json.dumps(host["encryption"]))
        self.assertEqual(
            self.by_ip(self.get_json(), "10.77.9.15")["encryption"],
            public_encryption,
        )

        # Host lama/tanpa payload encryption memakai default No encryption.
        r2 = self.client.post(
            "/api/hosts",
            json={"ip": "10.77.9.16", "category": "Tg"},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        self.assertEqual(r2.status_code, 200)
        conn, c = m.get_db()
        row2 = c.execute(
            "SELECT host_encryption FROM hosts WHERE ip='10.77.9.16'"
        ).fetchone()
        conn.close()
        self.assertEqual(
            json.loads(row2["host_encryption"]),
            {"to_host": "none", "from_host": ["none"]},
        )

    def test_tambah_host_encryption_tidak_valid_ditolak(self):
        headers = {"X-Requested-With": "XMLHttpRequest"}
        for encryption in (
            {"to_host": "tls", "from_host": ["none"]},
            {"to_host": "none", "from_host": "psk"},
            {"to_host": "none", "from_host": ["tls"]},
            {"to_host": "psk", "from_host": ["none"]},
            {
                "to_host": "none",
                "from_host": ["psk"],
                "psk_identity": "tanpa-key",
            },
            ["bukan", "objek"],
        ):
            r = self.client.post(
                "/api/hosts",
                json={
                    "ip": "10.77.9.17",
                    "category": "Tg",
                    "encryption": encryption,
                },
                headers=headers,
            )
            self.assertEqual(r.status_code, 400)
            message = r.get_json()["error"].lower()
            self.assertTrue("encryption" in message or "psk" in message, message)


if __name__ == "__main__":
    unittest.main()
