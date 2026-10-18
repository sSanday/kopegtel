import base64
import hashlib
import hmac
import json
import os
import tempfile
import unittest
from unittest import mock

_tmp = tempfile.mkdtemp(prefix="nms_test_notify_")
os.environ.setdefault("NMS_DB_PATH", os.path.join(_tmp, "test.db"))
os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("DASHBOARD_USERNAME", "admin")
os.environ.setdefault("DASHBOARD_PASSWORD", "admin12345")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "")
os.environ.setdefault("TELEGRAM_CHAT_ID", "")
os.environ.setdefault("SMTP_HOST", "")
os.environ.setdefault("SMTP_PORT", "587")
os.environ.setdefault("SMTP_USER", "")
os.environ.setdefault("SMTP_PASSWORD", "")
os.environ.setdefault("SMTP_FROM", "")
os.environ.setdefault("ALERT_EMAIL_TO", "")
os.environ.setdefault("WEBHOOK_URL", "")
os.environ.setdefault("WEBHOOK_SECRET", "")
os.environ.setdefault("AGENT_API_KEY", "test-agent-key")
os.environ.setdefault("NMS_DISABLE_SCHEDULER", "1")

import app as m
from nms import notify as _nt

try:
    m.scheduler.shutdown(wait=False)
except Exception:
    pass

JSON_HDR = {"Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest"}


def _cfg(**kw):
    """Patch konstanta channel di nms.notify untuk satu konteks."""
    attrs = {
        "TELEGRAM_BOT_TOKEN": "",
        "TELEGRAM_CHAT_ID": "",
        "SMTP_HOST": "",
        "SMTP_PORT": 587,
        "SMTP_USER": "",
        "SMTP_PASSWORD": "",
        "SMTP_FROM": "",
        "ALERT_EMAIL_TO": "",
        "WEBHOOK_URL": "",
        "WEBHOOK_SECRET": "",
    }
    attrs.update(kw)
    return mock.patch.multiple(_nt, **attrs)


def _post_recorder():
    calls = []

    def _fake_post(url="", *a, **k):
        calls.append({"url": url, "args": a, "kwargs": k})
        resp = mock.Mock()
        resp.raise_for_status.return_value = None
        return resp

    return _fake_post, calls


class ChannelOffTest(unittest.TestCase):
    def test_channel_nonaktif_tak_dipanggil(self):
        fake_post, calls = _post_recorder()
        with (
            _cfg(),
            mock.patch.object(_nt.requests, "post", fake_post),
            mock.patch("smtplib.SMTP") as smtp_cls,
            mock.patch("smtplib.SMTP_SSL"),
        ):
            res = _nt.send_alert("pesan uji", severity="critical", event="t")
        self.assertEqual(calls, [])
        smtp_cls.assert_not_called()
        self.assertEqual(res, {})

    def test_status_konfig(self):
        with _cfg(
            TELEGRAM_BOT_TOKEN="tok",
            TELEGRAM_CHAT_ID="cid",
            SMTP_HOST="smtp.test",
            SMTP_FROM="nms@test",
            ALERT_EMAIL_TO="admin@example.com",
            WEBHOOK_URL="https://hook.test/x",
        ):
            st = _nt.channel_status()
        self.assertEqual(st, {"telegram": True, "email": True, "webhook": True})

    def test_email_nonaktif_bila_placeholder(self):
        # ALERT_EMAIL_TO "you@example.com" dianggap belum dikonfigurasi
        with _cfg(
            SMTP_HOST="smtp.test",
            SMTP_FROM="nms@test",
            ALERT_EMAIL_TO="you@example.com",
        ):
            st = _nt.channel_status()
        self.assertFalse(st["email"])
        self.assertFalse(st["telegram"])
        self.assertFalse(st["webhook"])


class SeverityFilterTest(unittest.TestCase):
    def test_filter_severity_default(self):
        fake_post, calls = _post_recorder()
        with (
            _cfg(
                TELEGRAM_BOT_TOKEN="tok",
                TELEGRAM_CHAT_ID="cid",
                SMTP_HOST="smtp.test",
                SMTP_FROM="nms@test",
                ALERT_EMAIL_TO="admin@example.com",
                WEBHOOK_URL="https://hook.test/x",
            ),
            mock.patch.object(_nt.requests, "post", fake_post),
            mock.patch("smtplib.SMTP") as smtp_cls,
        ):
            res = _nt.send_alert("peringatan", severity="warning")
        # email default critical -> hanya telegram + webhook yang terkirim
        urls = [c["url"] for c in calls]
        self.assertEqual(len(calls), 2)
        self.assertTrue(any("api.telegram.org" in u for u in urls))
        self.assertTrue(any("hook.test" in u for u in urls))
        smtp_cls.assert_not_called()
        self.assertEqual(res, {"telegram": "ok", "webhook": "ok"})

    def test_email_ikut_bila_ambang_diturunkan_db(self):
        conn, c = m.get_db()
        c.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            ("notify_email_min_sev", "info"),
        )
        conn.commit()
        conn.close()
        fake_post, calls = _post_recorder()
        with (
            _cfg(
                TELEGRAM_BOT_TOKEN="tok",
                TELEGRAM_CHAT_ID="cid",
                SMTP_HOST="smtp.test",
                SMTP_FROM="nms@test",
                ALERT_EMAIL_TO="admin@example.com",
            ),
            mock.patch.object(_nt.requests, "post", fake_post),
            mock.patch("smtplib.SMTP") as smtp_cls,
        ):
            res = _nt.send_alert("peringatan", severity="warning")
        smtp_cls.assert_called_once()
        self.assertEqual(res.get("email"), "ok")
        conn, c = m.get_db()
        c.execute("DELETE FROM settings WHERE key='notify_email_min_sev'")
        conn.commit()
        conn.close()


class GagalIsolasiTest(unittest.TestCase):
    def test_satu_channel_gagal_tak_mengganggu_lain(self):
        fake_post, calls = _post_recorder()

        def _telegram_timeout(**kwargs):
            raise TimeoutError("read timeout")

        def _call(url="", *a, **k):
            if "api.telegram.org" in url:
                return _telegram_timeout(**k)
            return fake_post(url, *a, **k)

        with (
            _cfg(
                TELEGRAM_BOT_TOKEN="tok",
                TELEGRAM_CHAT_ID="cid",
                WEBHOOK_URL="https://hook.test/x",
            ),
            mock.patch.object(_nt.requests, "post", _call),
        ):
            res = _nt.send_alert("pesan", severity="critical")
        self.assertEqual(res.get("webhook"), "ok")
        self.assertNotEqual(res.get("telegram"), "ok")
        # calls hanya diisi webhook (telegram raise sebelum append)
        self.assertEqual(len(calls), 1)
        self.assertIn("hook.test", calls[0]["url"])

    def test_wrapper_lama_ke_semua_channel(self):
        fake_post, calls = _post_recorder()
        with (
            _cfg(WEBHOOK_URL="https://hook.test/x"),
            mock.patch.object(_nt.requests, "post", fake_post),
        ):
            _nt.send_telegram_alert("alert lama")
        self.assertEqual(len(calls), 1)
        self.assertIn("hook.test", calls[0]["url"])


class WebhookSignatureTest(unittest.TestCase):
    def test_signature_hmac_benar(self):
        fake_post, calls = _post_recorder()
        secret = "s3cr3t-sign"
        with (
            _cfg(WEBHOOK_URL="https://hook.test/x", WEBHOOK_SECRET=secret),
            mock.patch.object(_nt.requests, "post", fake_post),
        ):
            _nt.send_webhook("pesan uji", "critical", "down.test")
        body = calls[0]["kwargs"]["data"]
        headers = calls[0]["kwargs"]["headers"]
        payload = json.loads(body.decode("utf-8"))
        self.assertEqual(payload["severity"], "critical")
        self.assertEqual(payload["event"], "down.test")
        self.assertEqual(payload["source"], "nms")
        self.assertEqual(payload["message"], "pesan uji")
        expect = (
            "sha256="
            + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        )
        self.assertEqual(headers["X-NMS-Signature"], expect)

    def test_markdown_dilepas(self):
        self.assertEqual(
            _nt._plain("⚠️ *ALARM!* nilai `x` dan _y_"), "⚠️ ALARM! nilai x dan y"
        )
        self.assertEqual(
            _nt._plain(_nt._first_line("baris satu\nbaris dua")), "baris satu"
        )


class EndpointNotifyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Isolasi: .env asli bisa berisi token Telegram/SMTP aktif;
        # test ini butuh kondisi SEMUA channel belum dikonfigurasi.
        cls._env_patcher = mock.patch.multiple(
            _nt,
            TELEGRAM_BOT_TOKEN="",
            TELEGRAM_CHAT_ID="",
            SMTP_HOST="",
            SMTP_PORT=587,
            SMTP_USER="",
            SMTP_PASSWORD="",
            SMTP_FROM="",
            ALERT_EMAIL_TO="",
            WEBHOOK_URL="",
            WEBHOOK_SECRET="",
        )
        cls._env_patcher.start()
        cls.addClassCleanup(cls._env_patcher.stop)
        cls.client = m.app.test_client()
        r = cls.client.post(
            "/login", data={"username": "admin", "password": "admin12345"}
        )
        assert r.status_code == 302

    def test_tanpa_channel_semua_skipped(self):
        r = self.client.post("/api/notify/test", headers=JSON_HDR)
        self.assertEqual(r.status_code, 200)
        j = r.get_json()
        for name in ("telegram", "email", "webhook"):
            self.assertFalse(j["results"][name]["active"])
            self.assertEqual(j["results"][name]["result"], "skipped")

    def test_akses_admin_saja(self):
        r = self.client.get("/api/notify/test")
        self.assertEqual(r.status_code, 405)


if __name__ == "__main__":
    unittest.main()
