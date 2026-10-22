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
        self.assertIn("Buat peta", body)
        # tautan lama yang mati (404) tidak boleh tersisa
        self.assertNotIn('/topo"', body)

    def test_halaman_dan_api_butuh_login(self):
        for url in ("/maps", "/api/maps"):
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
        j = self.create_map("Ekspor Saya")
        self.put_json(f"/api/maps/{j['id']}/content", self._cont())
        r = self.client.post("/api/maps/export", json={"ids": [j["id"]]})
        self.assertEqual(r.status_code, 200)
        self.assertIn("maps_export.json", r.headers.get("Content-Disposition", ""))
        payload = r.get_json()
        self.assertEqual(len(payload["maps"]), 1)
        mm = payload["maps"][0]
        self.assertEqual(mm["name"], "Ekspor Saya")
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


if __name__ == "__main__":
    unittest.main()
