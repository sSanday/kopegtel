"""Test URL publik + tautan halaman host di notifikasi Telegram."""
import os
import tempfile
import unittest

_tmp = tempfile.mkdtemp(prefix="nms_test_publicurl_")
os.environ.setdefault("NMS_DB_PATH", os.path.join(_tmp, "test.db"))
os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("DASHBOARD_USERNAME", "admin")
os.environ.setdefault("DASHBOARD_PASSWORD", "admin12345")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "")
os.environ.setdefault("TELEGRAM_CHAT_ID", "")
os.environ.setdefault("AGENT_API_KEY", "test-agent-key")
os.environ.setdefault("NMS_DISABLE_SCHEDULER", "1")

import app as m
from nms import monitor as _monmod

try:
    m.scheduler.shutdown(wait=False)
except Exception:
    pass

JSON_HDR = {"Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest"}
LINK_HOST = "10.99.88.10"


class PublicUrlTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = m.app.test_client()
        conn, c = m.get_db()
        c.execute("INSERT OR IGNORE INTO hosts (ip) VALUES (?)", (LINK_HOST,))
        conn.commit()
        conn.close()
        r = cls.client.post(
            "/login", data={"username": "admin", "password": "admin12345"}
        )
        assert r.status_code == 302, f"login gagal, status={r.status_code}"

    @classmethod
    def tearDownClass(cls):
        cls.client.post("/api/settings", json={"public_url": ""}, headers=JSON_HDR)

    def test_public_url_validation(self):
        for bad in ["ftp://example.com", "not a url", "http://", "//nozsch", "x" * 300]:
            r = self.client.post(
                "/api/settings", json={"public_url": bad}, headers=JSON_HDR
            )
            self.assertEqual(r.status_code, 400, f"{bad!r} harus ditolak")
        r = self.client.post(
            "/api/settings",
            json={"public_url": "http://192.168.1.5:5000/"},
            headers=JSON_HDR,
        )
        self.assertEqual(r.status_code, 200)
        r = self.client.get("/api/settings", headers=JSON_HDR)
        # trailing slash dinormalisasi
        self.assertEqual(r.get_json()["public_url"], "http://192.168.1.5:5000")
        r = self.client.post(
            "/api/settings", json={"public_url": ""}, headers=JSON_HDR
        )
        self.assertEqual(r.status_code, 200)

    def test_down_dan_pulih_memuat_tautan(self):
        self.client.post(
            "/api/settings",
            json={"public_url": "http://192.168.1.5:5000"},
            headers=JSON_HDR,
        )
        orig_ping = _monmod.ping_host
        orig_tg = _monmod.send_telegram_alert
        sent = []
        _monmod.ping_host = lambda h: (-1, 100.0) if h == LINK_HOST else (0.5, 0.0)
        _monmod.send_telegram_alert = lambda msg: sent.append(msg)
        try:
            m.status_memory[LINK_HOST] = False
            m.check_network()
            downs = [s for s in sent if "ALARM!" in s and LINK_HOST in s]
            self.assertTrue(downs, f"pesan DOWN tidak terkirim: {sent}")
            self.assertTrue(
                any(
                    "http://192.168.1.5:5000/host/10.99.88.10" in s
                    and "Buka halaman host" in s
                    for s in downs
                ),
                f"tautan detail hilang: {downs}",
            )
            sent.clear()
            _monmod.ping_host = lambda h: (0.5, 0.0)
            m.check_network()
            ups = [s for s in sent if "PULIH!" in s and LINK_HOST in s]
            self.assertTrue(ups, f"pesan PULIH tidak terkirim: {sent}")
            self.assertTrue(
                any("http://192.168.1.5:5000/host/10.99.88.10" in s for s in ups),
                f"tautan detail hilang: {ups}",
            )
        finally:
            _monmod.ping_host = orig_ping
            _monmod.send_telegram_alert = orig_tg
            m.status_memory.pop(LINK_HOST, None)
            m.down_since.pop(LINK_HOST, None)
            conn, c = m.get_db()
            c.execute("DELETE FROM down_events WHERE host=?", (LINK_HOST,))
            c.execute(
                "DELETE FROM settings WHERE key=?", ("public_url",)
            )
            conn.commit()
            conn.close()

    def test_tanpa_public_url_tidak_ada_tautan(self):
        conn, c = m.get_db()
        c.execute("DELETE FROM settings WHERE key=?", ("public_url",))
        conn.commit()
        conn.close()
        self.assertEqual(_monmod._host_detail_link(LINK_HOST), "")
        conn, c = m.get_db()
        c.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            ("public_url", "ftp://salah"),
        )
        conn.commit()
        conn.close()
        try:
            self.assertEqual(_monmod._host_detail_link(LINK_HOST), "")
        finally:
            conn, c = m.get_db()
            c.execute("DELETE FROM settings WHERE key=?", ("public_url",))
            conn.commit()
            conn.close()


if __name__ == "__main__":
    unittest.main()
