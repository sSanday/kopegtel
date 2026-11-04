"""Uji halaman /maps dan API peta topologi (gaya Zabbix).

Peta tersimpan di tabel maps/map_elements/map_links; status elemen host
mengambil status ping nyata dari memori monitor + jendela maintenance.
Ekspor/impor adalah JSON round-trip dengan uid elemen yang stabil.
"""

import os
import tempfile
import unittest

_tmp = tempfile.mkdtemp(prefix="nms_test_maps_")
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

TEST_IPS = ("10.78.7.1", "10.78.7.2")


class MapsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        m.DB_PATH = os.environ["NMS_DB_PATH"]
        m.init_db()
        cls.client = m.app.test_client()
        conn, c = m.get_db()
        c.execute(
            "INSERT OR IGNORE INTO hosts (ip, alias, category) VALUES (?,?,?)",
            ("10.78.7.1", "Router Uji", "Core"),
        )
        c.execute(
            "INSERT OR IGNORE INTO hosts (ip, alias, category) VALUES (?,?,?)",
            ("10.78.7.2", "", "Client"),
        )
        conn.commit()
        conn.close()
        r = cls.client.post(
            "/login", data={"username": "admin", "password": "PantauNms2026"}
        )
        assert r.status_code == 302, f"login gagal, status={r.status_code}"

    def setUp(self):
        conn, c = m.get_db()
        c.execute("DELETE FROM map_settings")
        c.execute("DELETE FROM maps")
        c.execute("DELETE FROM map_elements")
        c.execute("DELETE FROM map_links")
        c.execute("DELETE FROM hosts WHERE ip NOT IN ('10.78.7.1','10.78.7.2')")
        m.status_memory.clear()
        conn.commit()
        conn.close()

    def post_json(self, url, body, expect=201):
        r = self.client.post(url, json=body if body is not None else None)
        self.assertEqual(r.status_code, expect, msg=r.get_data(as_text=True))
        return r.get_json()

    def put_json(self, url, body, expect=200):
        r = self.client.put(url, json=body)
        self.assertEqual(r.status_code, expect, msg=r.get_data(as_text=True))
        return r.get_json()

    def patch_json(self, url, body, expect=200):
        r = self.client.patch(url, json=body)
        self.assertEqual(r.status_code, expect, msg=r.get_data(as_text=True))
        return r.get_json()

    def create_map(self, name="Jaringan Kantor"):
        return self.post_json("/api/maps", {"name": name})

    def detail(self, map_id):
        r = self.client.get(f"/api/maps/{map_id}")
        self.assertEqual(r.status_code, 200)
        return r.get_json()

    # ---------- halaman ----------

    def test_halaman_maps_200(self):
        r = self.client.get("/maps")
        self.assertEqual(r.status_code, 200)
        body = r.get_data(as_text=True)
        self.assertIn("Maps", body)
        self.assertIn('href="/maps" class="nav-link nav-sub-link active"', body)
        self.assertIn("Create map", body)
        self.assertIn('href="/maps/new"', body)
        self.assertIn("Apply", body)
        self.assertIn("Displaying", body)
        # tautan lama yang mati (404) tidak boleh tersisa
        self.assertNotIn('/topo"', body)

    def test_halaman_form_create_lengkap(self):
        r = self.client.get("/maps/new")
        self.assertEqual(r.status_code, 200)
        body = r.get_data(as_text=True)
        for text in (
            "Network maps",
            "Map",
            "Sharing",
            "Owner",
            "Select",
            "Name",
            "Width",
            "Height",
            'value="800"',
            'value="600"',
            "Background image",
            "Background scale",
            "Proportionally",
            "Automatic icon mapping",
            "show icon mappings",
            "Icon highlight",
            "Mark elements on trigger status change",
            "Display problems",
            "Expand single problem",
            "Number of problems",
            "Number of problems and expand most critical one",
            "Map element label type",
            "Advanced labels",
            "Host group label type",
            "Host label type",
            "Trigger label type",
            "Map label type",
            "Image label type",
            "Map element label location",
            "Show map element labels",
            "Auto hide",
            "Show link labels",
            "Problem display",
            "Minimum severity",
            "Not classified",
            "Information",
            "Warning",
            "Average",
            "High",
            "Disaster",
            "Show suppressed problems",
            "URLs",
            "Element",
            "Add",
            "Cancel",
            "Zabbix 7.4.15",
        ):
            self.assertIn(text, body)
        self.assertIn('href="/maps" class="nav-link nav-sub-link active"', body)
        self.assertIn('id="panelSharing"', body)
        self.assertIn('id="ownerModal"', body)
        self.assertIn('id="urlRowTpl"', body)
        self.assertIn('id="mLabelType"', body)
        # Modal pilih user group untuk List of user group shares.
        self.assertIn('id="groupModal"', body)
        self.assertIn('id="groupModalList"', body)
        self.assertIn('id="groupSelectAll"', body)
        self.assertIn("User groups", body)
        for group in (
            "Disabled",
            "Enabled debug mode",
            "Guests",
            "Internal",
            "No access to the frontend",
            "Zabbix administrators",
        ):
            self.assertIn(group, body)
        # Modal pilih user untuk List of user shares.
        self.assertIn('id="userModal"', body)
        self.assertIn('id="userModalList"', body)
        self.assertIn('id="userSelectAll"', body)
        self.assertIn('value="admin"', body)
        self.assertIn('value="guest"', body)
        self.assertIn("Zabbix", body)
        self.assertIn("Administrator", body)
        self.assertIn('<option value="label" selected>Label</option>', body)
        self.assertLess(
            body.index("Map element label type"),
            body.index("Map element label location"),
        )

    def test_halaman_dan_api_butuh_login(self):
        for url in ("/maps", "/maps/new", "/api/maps"):
            r = m.app.test_client().get(url)
            self.assertIn(r.status_code, (302, 401))

    def test_halaman_edit_404_untuk_peta_aneh(self):
        r = self.client.get("/maps/99999/edit")
        self.assertEqual(r.status_code, 404)

    # ---------- buat & daftar ----------

    def test_buat_dan_daftar(self):
        j = self.create_map("Jaringan Kantor")
        self.assertIn("id", j)
        r = self.client.get("/api/maps")
        data = r.get_json()
        self.assertEqual(data["count"], 1)
        row = data["maps"][0]
        self.assertEqual(row["name"], "Jaringan Kantor")
        self.assertEqual(row["width"], 960)
        self.assertEqual(row["height"], 540)
        self.assertEqual(row["elements"], 0)

    def test_buat_validasi(self):
        # nama kosong
        r = self.client.post("/api/maps", json={"name": "   "})
        self.assertEqual(r.status_code, 400)
        # dimensi di luar rentang
        r = self.client.post("/api/maps", json={"name": "X", "width": 50})
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/api/maps", json={"name": "X", "height": 70000})
        self.assertEqual(r.status_code, 400)
        # body hilang (diblok guard → 403)
        r = self.client.post("/api/maps")
        self.assertEqual(r.status_code, 403)
        # dimensi kustom tersimpan
        j = self.post_json("/api/maps", {"name": "Kecil", "width": 640, "height": 480})
        self.assertEqual(j["width"], 640)
        self.assertEqual(j["height"], 480)

    def test_buat_dengan_pengaturan_lengkap(self):
        settings = {
            "background_image": "none",
            "background_scale": "none",
            "automatic_icon_mapping": "manual",
            "icon_highlight": True,
            "mark_elements": True,
            "display_problems": "number_most_critical",
            "label_type": "name",
            "advanced_labels": False,
            "host_group_label_type": "name",
            "host_label_type": "ip",
            "trigger_label_type": "status",
            "map_label_type": "nothing",
            "image_label_type": "label",
            "label_location": "top",
            "show_element_labels": "auto_hide",
            "show_link_labels": "auto_hide",
            "problem_display": "separated",
            "minimum_severity": "high",
            "show_suppressed": True,
            "urls": [
                {
                    "name": "Latest data",
                    "url": "https://example.test/latest",
                    "element": "host",
                }
            ],
            "sharing": {
                "type": "private",
                "user_groups": [{"name": "Ops", "permission": "read-only"}],
                "users": [{"name": "admin", "permission": "read-write"}],
            },
        }
        j = self.post_json(
            "/api/maps",
            {
                "name": "Peta Lengkap",
                "width": 800,
                "height": 600,
                "owner": "admin",
                "settings": settings,
            },
        )
        self.assertEqual(j["owner"], "admin")
        d = self.detail(j["id"])
        self.assertEqual(d["owner"], "admin")
        self.assertEqual(d["settings"], settings)
        # default tetap dipakai untuk field yang tidak dikirim
        j2 = self.post_json("/api/maps", {"name": "Peta Default"})
        d2 = self.detail(j2["id"])
        self.assertEqual(d2["owner"], "admin")
        self.assertEqual(d2["settings"]["background_scale"], "proportional")
        self.assertEqual(d2["settings"]["label_type"], "label")
        self.assertEqual(d2["settings"]["minimum_severity"], "not_classified")
        self.assertEqual(d2["settings"]["sharing"]["type"], "public")
        self.assertEqual(d2["settings"]["urls"], [])

    def test_pengaturan_validasi_dan_patch(self):
        r = self.client.post(
            "/api/maps",
            json={"name": "X", "settings": {"minimum_severity": "unknown"}},
        )
        self.assertEqual(r.status_code, 400)
        r = self.client.post(
            "/api/maps",
            json={"name": "X", "settings": {"label_type": "unknown"}},
        )
        self.assertEqual(r.status_code, 400)
        r = self.client.post(
            "/api/maps",
            json={"name": "X", "settings": {"urls": [{"name": "A", "url": ""}]}},
        )
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/api/maps", json={"name": "X", "settings": "bukan objek"})
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/api/maps", json={"name": "X", "owner": "  "})
        self.assertEqual(r.status_code, 400)

        j = self.create_map()
        row = self.patch_json(
            f"/api/maps/{j['id']}",
            {
                "owner": "operator1",
                "settings": {"background_scale": "none", "show_suppressed": True},
            },
        )
        self.assertEqual(row["owner"], "operator1")
        self.assertEqual(row["settings"]["background_scale"], "none")
        self.assertTrue(row["settings"]["show_suppressed"])
        # field lain tetap memakai nilai sebelumnya/default
        self.assertEqual(row["settings"]["minimum_severity"], "not_classified")
        d = self.detail(j["id"])
        self.assertEqual(d["settings"]["background_scale"], "none")
        r = self.client.patch(
            f"/api/maps/{j['id']}",
            json={"settings": {"problem_display": "aneh"}},
        )
        self.assertEqual(r.status_code, 400)

    def test_filter_nama_dan_urutan(self):
        self.create_map("Highlight Kantor")
        self.create_map("Jaringan Cabang")
        data = self.client.get("/api/maps?name=cab").get_json()
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["maps"][0]["name"], "Jaringan Cabang")
        # urutan nama desc
        data = self.client.get("/api/maps?sort=name_desc").get_json()
        self.assertEqual(
            [d["name"] for d in data["maps"]],
            ["Jaringan Cabang", "Highlight Kantor"],
        )
        # sort tak dikenal kembali ke name_asc
        data = self.client.get("/api/maps?sort=abcdef").get_json()
        self.assertEqual(
            [d["name"] for d in data["maps"]],
            ["Highlight Kantor", "Jaringan Cabang"],
        )

    # ---------- konten peta ----------

    def _cont(self):
        return {
            "elements": [
                {
                    "uid": "h1",
                    "etype": "host",
                    "label": "Router Uji",
                    "ip": "10.78.7.1",
                    "x": 30,
                    "y": 40,
                },
                {"uid": "t1", "etype": "text", "label": "Jaringan", "x": 120, "y": 60},
            ],
            "links": [{"from": "h1", "to": "t1"}],
        }

    def test_isi_peta_dan_status_ping(self):
        j = self.create_map()
        res = self.put_json(f"/api/maps/{j['id']}/content", self._cont())
        self.assertEqual(res["elements"], 2)
        self.assertEqual(res["links"], 1)
        d = self.detail(j["id"])
        self.assertEqual(len(d["elements"]), 2)
        h = [e for e in d["elements"] if e["uid"] == "h1"][0]
        self.assertEqual(h["ip"], "10.78.7.1")
        self.assertEqual(h["status"], "unknown")  # belum ada data ping
        t = [e for e in d["elements"] if e["uid"] == "t1"][0]
        self.assertIsNone(t["status"])
        self.assertEqual(d["links"], [{"from": "h1", "to": "t1"}])
        # daftar host untuk picker
        self.assertIn("Router Uji", [h["name"] for h in d["hosts"]])
        # jumlah elemen terbaca pada daftar peta
        data = self.client.get("/api/maps").get_json()
        self.assertEqual(data["maps"][0]["elements"], 2)

        # status ping hidup tercermin dari memori monitor
        m.status_memory["10.78.7.1"] = True
        d = self.detail(j["id"])
        h = [e for e in d["elements"] if e["uid"] == "h1"][0]
        self.assertEqual(h["status"], "up")
        m.status_memory["10.78.7.1"] = False
        d = self.detail(j["id"])
        h = [e for e in d["elements"] if e["uid"] == "h1"][0]
        self.assertEqual(h["status"], "down")

    def test_isi_peta_ganti_semua(self):
        j = self.create_map()
        self.put_json(f"/api/maps/{j['id']}/content", self._cont())
        res = self.put_json(
            f"/api/maps/{j['id']}/content",
            {
                "elements": [{"uid": "t1", "etype": "text", "label": "Sisa teks"}],
                "links": [],
            },
        )
        self.assertEqual(res["elements"], 1)
        self.assertEqual(res["links"], 0)
        d = self.detail(j["id"])
        self.assertEqual([e["uid"] for e in d["elements"]], ["t1"])

    def test_isi_peta_validasi(self):
        j = self.create_map()
        url = f"/api/maps/{j['id']}/content"
        # uid ganda
        r = self.client.put(
            url,
            json={
                "elements": [
                    {"uid": "a", "etype": "text", "label": "x"},
                    {"uid": "a", "etype": "text", "label": "y"},
                ]
            },
        )
        self.assertEqual(r.status_code, 400)
        # etype aneh
        r = self.client.put(
            url, json={"elements": [{"uid": "a", "etype": "alien", "label": "x"}]}
        )
        self.assertEqual(r.status_code, 400)
        # tanpa label
        r = self.client.put(
            url, json={"elements": [{"uid": "a", "etype": "text", "label": "  "}]}
        )
        self.assertEqual(r.status_code, 400)
        # posisi tak valid
        r = self.client.put(
            url,
            json={
                "elements": [{"uid": "a", "etype": "text", "label": "x", "x": "abc"}]
            },
        )
        self.assertEqual(r.status_code, 400)
        # tautan uid tak dikenal dilewati (bukan 400)
        res = self.put_json(
            url,
            {
                "elements": [
                    {"uid": "a", "etype": "text", "label": "x"},
                    {"uid": "b", "etype": "text", "label": "y"},
                ],
                "links": [{"from": "a", "to": "zz"}],
            },
        )
        self.assertEqual(res["links"], 0)
        # tautan diri sendiri & ganda dilewati
        res = self.put_json(
            url,
            {
                "elements": [
                    {"uid": "a", "etype": "text", "label": "x"},
                    {"uid": "b", "etype": "text", "label": "y"},
                ],
                "links": [
                    {"from": "a", "to": "a"},
                    {"from": "a", "to": "b"},
                    {"from": "b", "to": "a"},
                ],
            },
        )
        self.assertEqual(res["links"], 1)
        # peta tak ada
        r = self.client.put("/api/maps/99999/content", json={"elements": []})
        self.assertEqual(r.status_code, 404)

    # ---------- properti (rename/resize) ----------

    def test_patch_properti(self):
        j = self.create_map()
        row = self.patch_json(
            f"/api/maps/{j['id']}", {"name": "Renamed", "width": 1200}
        )
        self.assertEqual(row["name"], "Renamed")
        self.assertEqual(row["width"], 1200)
        # validasi
        r = self.client.patch(f"/api/maps/{j['id']}", json={"width": 99})
        self.assertEqual(r.status_code, 400)
        r = self.client.patch("/api/maps/99999", json={"name": "X"})
        self.assertEqual(r.status_code, 404)
        # tanpa body JSON diblok guard
        r = self.client.patch(f"/api/maps/{j['id']}")
        self.assertEqual(r.status_code, 403)

    # ---------- hapus ----------

    def test_hapus_dan_isolasi(self):
        a = self.create_map("A")
        b = self.create_map("B")
        self.put_json(f"/api/maps/{a['id']}/content", self._cont())
        self.put_json(f"/api/maps/{b['id']}/content", self._cont())
        data = self.client.get("/api/maps").get_json()
        self.assertEqual({d["elements"] for d in data["maps"]}, {2})
        r = self.client.delete("/api/maps", json={"ids": [a["id"], 99999]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["deleted"], 1)
        conn, c = m.get_db()
        c.execute("SELECT COUNT(*) AS n FROM map_settings WHERE map_id=?", (a["id"],))
        self.assertEqual(c.fetchone()["n"], 0)
        conn.close()
        data = self.client.get("/api/maps").get_json()
        self.assertEqual(data["count"], 1)
        # peta B tetap utuh
        d = self.detail(b["id"])
        self.assertEqual(len(d["elements"]), 2)
        # validasi body
        r = self.client.delete("/api/maps", json={"ids": []})
        self.assertEqual(r.status_code, 400)
        r = self.client.delete("/api/maps", json={"ids": ["x"]})
        self.assertEqual(r.status_code, 400)

    # ---------- ekspor & impor ----------

    def test_ekspor_json(self):
        j = self.post_json(
            "/api/maps",
            {
                "name": "Ekspor Saya",
                "settings": {
                    "background_scale": "none",
                    "minimum_severity": "warning",
                    "urls": [
                        {
                            "name": "Docs",
                            "url": "https://example.test",
                            "element": "map",
                        }
                    ],
                },
            },
        )
        self.put_json(f"/api/maps/{j['id']}/content", self._cont())
        r = self.client.post("/api/maps/export", json={"ids": [j["id"]]})
        self.assertEqual(r.status_code, 200)
        self.assertIn("maps_export.json", r.headers.get("Content-Disposition", ""))
        payload = r.get_json()
        self.assertEqual(len(payload["maps"]), 1)
        mm = payload["maps"][0]
        self.assertEqual(mm["name"], "Ekspor Saya")
        self.assertEqual(mm["owner"], "admin")
        self.assertEqual(mm["settings"]["background_scale"], "none")
        self.assertEqual(mm["settings"]["minimum_severity"], "warning")
        self.assertEqual(len(mm["settings"]["urls"]), 1)
        self.assertEqual([e["uid"] for e in mm["elements"]], ["h1", "t1"])
        self.assertEqual(mm["links"], [{"from": "h1", "to": "t1"}])
        # id yang tidak ada dilewati; ids kosong → 400
        r = self.client.post("/api/maps/export", json={"ids": [99999]})
        self.assertEqual(r.get_json()["maps"], [])
        r = self.client.post("/api/maps/export", json={"ids": []})
        self.assertEqual(r.status_code, 400)

    def test_impor_json(self):
        payload = {
            "version": 1,
            "exported_at": "2026-10-20 08:00:00",
            "maps": [
                {
                    "name": "Impor Kantor",
                    "width": 800,
                    "height": 400,
                    "owner": "admin",
                    "settings": {
                        "minimum_severity": "average",
                        "sharing": {"type": "private"},
                    },
                    "elements": self._cont()["elements"],
                    "links": self._cont()["links"],
                },
                {
                    "name": "Rusak Produksi",
                    "width": 0,
                    "elements": [],
                },
            ],
        }
        r = self.client.post("/api/maps/import", json=payload)
        self.assertEqual(r.status_code, 201)
        body = r.get_json()
        self.assertEqual(body["count"], 1)
        self.assertEqual(len(body["errors"]), 1)
        d = self.detail(body["created"][0]["id"])
        self.assertEqual([e["uid"] for e in d["elements"]], ["h1", "t1"])
        self.assertEqual(d["links"], [{"from": "h1", "to": "t1"}])
        self.assertEqual(d["map"]["width"], 800)
        self.assertEqual(d["owner"], "admin")
        self.assertEqual(d["settings"]["minimum_severity"], "average")
        self.assertEqual(d["settings"]["sharing"]["type"], "private")
        # impor kosong / format salah
        r = self.client.post("/api/maps/import", json={"maps": []})
        self.assertEqual(r.status_code, 400)
        # tanpa body JSON diblok guard
        r = self.client.post("/api/maps/import")
        self.assertEqual(r.status_code, 403)

    def test_peta_tanpa_elemen(self):
        j = self.create_map()
        d = self.detail(j["id"])
        self.assertEqual(d["elements"], [])
        self.assertEqual(d["links"], [])
        self.assertEqual(d["map"]["name"], "Jaringan Kantor")

    # ---------- validasi nama, total, tampilan, ACL ----------

    def test_nama_peta_harus_unik(self):
        a = self.create_map("Jaringan Kantor")
        r = self.client.post("/api/maps", json={"name": "jaringan kantor "})
        self.assertEqual(r.status_code, 400, msg=r.get_data(as_text=True))
        b = self.create_map("Jaringan Cabang")
        r = self.client.patch(f"/api/maps/{b['id']}", json={"name": "Jaringan Kantor"})
        self.assertEqual(r.status_code, 400)
        # Menyimpan ulang nama sendiri (beda kapital) tetap boleh.
        r = self.client.patch(f"/api/maps/{a['id']}", json={"name": "JARINGAN KANTOR"})
        self.assertEqual(r.status_code, 200)

    def test_daftar_punya_total_sebelum_filter(self):
        for name in ("Alpha", "Beta", "Gamma"):
            self.create_map(name)
        data = self.client.get("/api/maps?name=bet").get_json()
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["total"], 3)
        self.assertEqual(data["maps"][0]["name"], "Beta")

    def test_halaman_lihat_peta(self):
        j = self.create_map("Peta Lihat")
        self.put_json(f"/api/maps/{j['id']}/content", self._cont())
        r = self.client.get(f"/maps/{j['id']}")
        self.assertEqual(r.status_code, 200)
        body = r.get_data(as_text=True)
        self.assertIn("Lihat peta", body)
        self.assertIn("Fullscreen", body)
        self.assertIn("Kiosk", body)
        self.assertIn("30", body)
        self.assertEqual(self.client.get("/maps/99999").status_code, 404)
        # Detail mengirim can_write agar tombol Edit bisa disembunyikan.
        self.assertTrue(self.detail(j["id"])["can_write"])

    def test_acl_peta_privat_dan_publik(self):
        private = {
            "sharing": {
                "type": "private",
                "user_groups": [],
                "users": [{"name": "operator1", "permission": "read-only"}],
            }
        }
        self.assertFalse(m._map_can_read("admin", private, "operator2", "viewer"))
        self.assertFalse(m._map_can_write("admin", private, "operator1", "operator"))
        self.assertTrue(m._map_can_read("admin", private, "operator1", "operator"))
        self.assertTrue(m._map_can_write("admin", private, "admin", "operator"))
        # Admin selalu boleh, terlepas dari sharing.
        self.assertTrue(m._map_can_read("admin", private, "anyone", "admin"))

        public = m._map_settings_defaults()
        self.assertTrue(m._map_can_read("budi", public, "siti", "viewer"))
        self.assertFalse(m._map_can_write("budi", public, "siti", "viewer"))

        rw = {
            "sharing": {
                "type": "private",
                "users": [{"name": "siti", "permission": "read-write"}],
            }
        }
        self.assertTrue(m._map_can_write("budi", rw, "siti", "operator"))

    def test_impor_nama_ganda_ditolak(self):
        payload = {
            "maps": [
                {"name": "Impor A", "elements": [], "links": []},
                {"name": "impor a", "elements": [], "links": []},
            ]
        }
        r = self.client.post("/api/maps/import", json=payload)
        self.assertEqual(r.status_code, 201)
        body = r.get_json()
        self.assertEqual(body["count"], 1)
        self.assertEqual(len(body["errors"]), 1)


if __name__ == "__main__":
    unittest.main()
