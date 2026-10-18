"""Bagian 1 — pengerasan DASHBOARD_PASSWORD (seed admin pertama)."""

import io
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from werkzeug.security import generate_password_hash

_tmp = tempfile.mkdtemp(prefix="nms_test_pwd_")
os.environ.setdefault("NMS_DB_PATH", os.path.join(_tmp, "test.db"))
os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("DASHBOARD_USERNAME", "admin")
os.environ.setdefault("DASHBOARD_PASSWORD", "PantauNms2026")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "")
os.environ.setdefault("TELEGRAM_CHAT_ID", "")
os.environ.setdefault("SMTP_HOST", "")
os.environ.setdefault("SMTP_FROM", "")
os.environ.setdefault("ALERT_EMAIL_TO", "")
os.environ.setdefault("WEBHOOK_URL", "")
os.environ.setdefault("AGENT_API_KEY", "test-agent-key")
os.environ.setdefault("NMS_DISABLE_SCHEDULER", "1")

import app as m
from nms.config import dashboard_password_is_weak

try:
    m.scheduler.shutdown(wait=False)
except Exception:
    pass


class WeakPasswordFnTest(unittest.TestCase):
    def test_daftar_kata_lemah_case_insensitive(self):
        for pw in (
            "",
            None,
            "admin123",
            "ADMIN123",
            "Password",
            "123456",
            "1234567890",
            "qwerty123",
            "changeme",
            "ganti-dengan-password-kuat-min-10-karakter",
            "your_strong_password_here",
        ):
            self.assertTrue(dashboard_password_is_weak(pw), repr(pw))

    def test_kata_kuat_diterima(self):
        for pw in (
            "PantauNms2026",
            "Kopegtel#Str0ng-2026",
            "x" * 10,
            "Aa1!bb1!cc1!",
        ):
            self.assertFalse(dashboard_password_is_weak(pw), repr(pw))

    def test_panjang_minimum_10(self):
        # Bahkan combinasi "kuat", < 10 karakter tetap dianggap lemah.
        self.assertTrue(dashboard_password_is_weak("Aa1!x"))
        self.assertTrue(dashboard_password_is_weak("Spasi123 "))


class _IsoDb:
    """Mini settings-table terpisah untuk menguji _seed_admin_from_env."""

    def __init__(self):
        self.path = os.path.join(tempfile.mkdtemp(prefix="nms_seed_"), "s.db")
        conn = sqlite3.connect(self.path)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS settings "
            "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        conn.commit()
        conn.close()

    def get_db(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn, conn.cursor()

    def settings(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            return {
                r["key"]: r["value"]
                for r in conn.execute("SELECT key, value FROM settings")
            }
        finally:
            conn.close()

    def set_settings(self, kv):
        conn = sqlite3.connect(self.path)
        try:
            for k, v in kv.items():
                conn.execute(
                    "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                    (k, v),
                )
            conn.commit()
        finally:
            conn.close()


class SeedPolicyTest(unittest.TestCase):
    """Pola [FATAL] sys.exit sama dengan pola AGENT_API_KEY."""

    def _patch(self, iso, env_pw, flask_env):
        return (
            mock.patch.object(m, "get_db", iso.get_db),
            mock.patch.object(m, "DASHBOARD_PASSWORD", env_pw),
            mock.patch.dict(os.environ, {"FLASK_ENV": flask_env}),
        )

    def test_seed_pertama_lemah_fatal_di_produksi(self):
        iso = _IsoDb()
        out = io.StringIO()
        exited = []

        def fake_exit(code):
            exited.append(code)
            raise SystemExit(code)

        p1, p2, p3 = self._patch(iso, "admin123", "production")
        with (
            p1,
            p2,
            p3,
            mock.patch.object(sys, "exit", side_effect=fake_exit),
            redirect_stdout(out),
        ):
            with self.assertRaises(SystemExit):
                m._seed_admin_from_env()
        self.assertEqual(exited, [1])
        self.assertIn("[FATAL]", out.getvalue())
        self.assertNotIn("admin_user", iso.settings())

    def test_seed_pertama_lemah_development_cukup_warning(self):
        iso = _IsoDb()
        out = io.StringIO()
        p1, p2, p3 = self._patch(iso, "admin123", "development")
        with p1, p2, p3, redirect_stdout(out):
            m._seed_admin_from_env()
        self.assertIn("[WARN]", out.getvalue())
        self.assertNotIn("[FATAL]", out.getvalue())
        s = iso.settings()
        self.assertEqual(s["admin_user"], "admin")
        self.assertIn("admin_pass_hash", s)

    def test_seed_pertama_kuat_produksi_dibuat(self):
        iso = _IsoDb()
        out = io.StringIO()
        p1, p2, p3 = self._patch(iso, "PantauNms2026", "production")
        with p1, p2, p3, redirect_stdout(out):
            m._seed_admin_from_env()
        self.assertNotIn("[FATAL]", out.getvalue())
        s = iso.settings()
        self.assertEqual(s["admin_user"], "admin")
        self.assertIn("admin_pass_hash", s)

    def test_admin_sudah_ada_env_lemah_hanya_warning(self):
        iso = _IsoDb()
        iso.set_settings(
            {
                "admin_user": "admin",
                "admin_pass_hash": generate_password_hash("KuatShared99!"),
            }
        )
        out = io.StringIO()
        p1, p2, p3 = self._patch(iso, "admin123", "production")
        with p1, p2, p3, redirect_stdout(out):
            with mock.patch.object(m.logger, "warning") as warn:
                m._seed_admin_from_env()
        self.assertTrue(warn.called)
        self.assertIn(
            "DASHBOARD_PASSWORD", " ".join(str(c) for c in warn.call_args_list)
        )
        self.assertNotIn("[FATAL]", out.getvalue())
        # hash DB tidak ditimpa
        self.assertIn("admin_pass_hash", iso.settings())

    def test_admin_sudah_ada_env_kuat_tanpa_warning(self):
        iso = _IsoDb()
        iso.set_settings(
            {
                "admin_user": "admin",
                "admin_pass_hash": generate_password_hash("PantauNms2026"),
            }
        )
        p1, p2, p3 = self._patch(iso, "PantauNms2026", "production")
        with p1, p2, p3:
            with mock.patch.object(m.logger, "warning") as warn:
                m._seed_admin_from_env()
        self.assertFalse(warn.called)


if __name__ == "__main__":
    unittest.main()
