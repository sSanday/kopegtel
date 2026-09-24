import os
import sqlite3
import tempfile
import unittest

_tmp = tempfile.mkdtemp(prefix="nms_test_crypto_")
os.environ.setdefault("NMS_DB_PATH", os.path.join(_tmp, "test.db"))
os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("DASHBOARD_USERNAME", "admin")
os.environ.setdefault("DASHBOARD_PASSWORD", "admin12345")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "")
os.environ.setdefault("TELEGRAM_CHAT_ID", "")
os.environ.setdefault("AGENT_API_KEY", "test-agent-key")
os.environ.setdefault("NMS_DISABLE_SCHEDULER", "1")

import app as m
from nms import crypto as cr
from nms import mikrotik_poll as _mtmod

try:
    m.scheduler.shutdown(wait=False)
except Exception:
    pass

JSON_HDR = {"Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest"}

CR_HOST = "10.99.99.60"
CR_PASS = "s3cr3t-r4h4s1a"


class CryptoTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        m.DB_PATH = os.environ["NMS_DB_PATH"]
        m.init_db()
        cls.client = m.app.test_client()
        r = cls.client.post(
            "/login", data={"username": "admin", "password": "admin12345"}
        )
        assert r.status_code == 302, f"login gagal, status={r.status_code}"

    @classmethod
    def tearDownClass(cls):
        conn, c = m.get_db()
        c.execute("DELETE FROM hosts WHERE ip=?", (CR_HOST,))
        conn.commit()
        conn.close()

    def _stored(self):
        conn, c = m.get_db()
        c.execute("SELECT ssh_pass FROM hosts WHERE ip=?", (CR_HOST,))
        row = c.fetchone()
        conn.close()
        return row["ssh_pass"] if row else None

    def test_roundtrip(self):
        tok = cr.encrypt_secret(CR_PASS)
        self.assertTrue(tok.startswith("enc$"))
        self.assertNotIn(CR_PASS, tok)
        self.assertEqual(cr.decrypt_secret(tok), CR_PASS)

    def test_kosong_tetap_kosong(self):
        self.assertEqual(cr.encrypt_secret(""), "")
        self.assertEqual(cr.decrypt_secret(""), "")
        self.assertEqual(cr.decrypt_secret(None), "")

    def test_legacy_plaintext_lolos(self):
        self.assertEqual(cr.decrypt_secret("plainlegacy"), "plainlegacy")

    def test_token_rusak_fail_closed(self):
        self.assertEqual(cr.decrypt_secret("enc$tidak-valid!!!"), "")

    def test_api_simpan_terenkripsi_dan_masked(self):
        r = self.client.post(
            "/api/hosts",
            json={
                "ip": CR_HOST,
                "ssh_user": "admin",
                "ssh_pass": CR_PASS,
                "ssh_port": 22,
                "backup_enable": 0,
            },
            headers=JSON_HDR,
        )
        self.assertIn(r.status_code, (200, 201), r.get_data(as_text=True))
        stored = self._stored()
        self.assertIsNotNone(stored)
        self.assertTrue(stored.startswith("enc$"))
        self.assertNotIn(CR_PASS, stored)
        r = self.client.get("/api/hosts", headers=JSON_HDR)
        self.assertEqual(r.status_code, 200)
        rows = [h for h in r.get_json() if h.get("ip") == CR_HOST]
        self.assertTrue(rows)
        self.assertNotIn("ssh_pass", rows[0])
        self.assertTrue(rows[0].get("ssh_pass_set"))

    def test_backup_candidate_didekripsi(self):
        conn, c = m.get_db()
        c.execute(
            "UPDATE hosts SET ssh_user='admin', ssh_pass=?, ssh_port=22,"
            " backup_enable=1 WHERE ip=?",
            (cr.encrypt_secret(CR_PASS), CR_HOST),
        )
        conn.commit()
        conn.close()
        try:
            cands = _mtmod._mt_backup_candidates()
            mine = [h for h in cands if h.get("ip") == CR_HOST]
            self.assertTrue(mine)
            self.assertEqual(mine[0]["ssh_pass"], CR_PASS)
        finally:
            conn, c = m.get_db()
            c.execute("UPDATE hosts SET backup_enable=0 WHERE ip=?", (CR_HOST,))
            conn.commit()
            conn.close()


if __name__ == "__main__":
    unittest.main()
