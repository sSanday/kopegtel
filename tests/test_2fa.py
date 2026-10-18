"""Uji 2FA TOTP opsional (login web): alur pending, kode, recovery, blokir, reset."""

import json
import os
import tempfile
import time
import unittest
from unittest import mock

import pyotp

_tmp = tempfile.mkdtemp(prefix="nms_test_2fa_")
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

import app as m  # noqa: E402

try:
    m.scheduler.shutdown(wait=False)
except Exception:
    pass

JSON_HDR = {"Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest"}
ADMIN_PW = "PantauNms2026"


def _clear_admin_2fa():
    m.twofa.disable("admin")
    m.twofa.clear_2fa_failures("admin")


def _db_user_2fa(username="admin"):
    conn, c = m.get_db()
    try:
        row = c.execute(
            "SELECT totp_secret, totp_enabled, totp_last_step, totp_recovery, last_login "
            "FROM users WHERE username=?",
            (username,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _audit_exists(action):
    conn, c = m.get_db()
    try:
        r = c.execute(
            "SELECT COUNT(*) AS n FROM audit_logs WHERE action=?", (action,)
        ).fetchone()
        return (r["n"] or 0) > 0
    finally:
        conn.close()


def _next_window_code(secret):
    """Kode TOTP yang aplikasi akan TAMPILKAN pada window berikutnya."""
    totp = pyotp.TOTP(secret)
    return totp.generate_otp(int(time.time() // 30) + 1)


def _wire_totp(client):
    """Login + setup + enable 2FA untuk admin; kembalikan (secret, recovery_codes)."""
    r = client.post("/login", data={"username": "admin", "password": ADMIN_PW})
    assert r.status_code == 302 and "/login/2fa" not in (
        r.headers.get("Location") or ""
    )
    r = client.post(
        "/api/2fa/setup", json={"current_password": ADMIN_PW}, headers=JSON_HDR
    )
    assert r.status_code == 200, r.get_data(as_text=True)
    secret = r.get_json()["secret"]
    code = pyotp.TOTP(secret).now()
    r = client.post("/api/2fa/enable", json={"code": code}, headers=JSON_HDR)
    assert r.status_code == 200, r.get_data(as_text=True)
    return secret, r.get_json()["recovery_codes"]


def _ensure_tables():
    """init_db() sederhana & idempoten pada DB akt saat fase test.

    File ini dijalankan PALING AWAL (urutan abjad) — sebelum
    test_basic.setUpClass yang biasa menyiapkan tabel NMS_DB_PATH aktif.
    """
    m.init_db()


class _FakeUser:
    is_authenticated = True

    def __init__(self, username="tester", role="operator"):
        self.username = username
        self.role = role


class TwoFaSetupApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_tables()

    def setUp(self):
        _clear_admin_2fa()

    def tearDown(self):
        _clear_admin_2fa()

    def test_tanpa_2fa_login_normal(self):
        client = m.app.test_client()
        r = client.post("/login", data={"username": "admin", "password": ADMIN_PW})
        self.assertEqual(r.status_code, 302)
        self.assertNotIn("/login/2fa", r.headers.get("Location", ""))
        # Benar-benar masuk (bukan dilempar balik ke login)
        r2 = client.get("/dashboard")
        self.assertEqual(r2.status_code, 200)
        client.get("/logout")

    def test_setup_wajib_password(self):
        client = m.app.test_client()
        client.post("/login", data={"username": "admin", "password": ADMIN_PW})
        r = client.post("/api/2fa/setup", json={}, headers=JSON_HDR)
        self.assertEqual(r.status_code, 400)
        r = client.post(
            "/api/2fa/setup",
            json={"current_password": "salah-banget"},
            headers=JSON_HDR,
        )
        self.assertEqual(r.status_code, 400)
        client.get("/logout")

    def test_setup_menghasilkan_qr_dan_secret(self):
        client = m.app.test_client()
        client.post("/login", data={"username": "admin", "password": ADMIN_PW})
        r = client.post(
            "/api/2fa/setup", json={"current_password": ADMIN_PW}, headers=JSON_HDR
        )
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertIn("otpauth://totp/", data["otpauth_uri"])
        self.assertIn("NMS%20KOPEGTEL", data["otpauth_uri"])
        self.assertIn("<svg", data["qr_svg"])
        self.assertRegex(data["secret"], r"^[A-Z2-7]{16,}$")
        client.get("/logout")

    def test_secret_tersimpan_terenkripsi(self):
        client = m.app.test_client()
        client.post("/login", data={"username": "admin", "password": ADMIN_PW})
        r = client.post(
            "/api/2fa/setup", json={"current_password": ADMIN_PW}, headers=JSON_HDR
        )
        secret = r.get_json()["secret"]
        row = _db_user_2fa()
        self.assertIsNotNone(row)
        self.assertTrue(str(row["totp_secret"]).startswith("enc$"))
        self.assertNotIn(secret, str(row["totp_secret"]))
        client.get("/logout")

    def test_enable_wajib_kode_benar(self):
        client = m.app.test_client()
        client.post("/login", data={"username": "admin", "password": ADMIN_PW})
        client.post(
            "/api/2fa/setup", json={"current_password": ADMIN_PW}, headers=JSON_HDR
        )
        r = client.post("/api/2fa/enable", json={"code": "000000"}, headers=JSON_HDR)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(_db_user_2fa()["totp_enabled"], 0)

        secret = m.twofa.state("admin")["secret"]
        r = client.post(
            "/api/2fa/enable", json={"code": pyotp.TOTP(secret).now()}, headers=JSON_HDR
        )
        self.assertEqual(r.status_code, 200)
        codes = r.get_json()["recovery_codes"]
        self.assertEqual(len(codes), 8)
        row = _db_user_2fa()
        self.assertEqual(row["totp_enabled"], 1)
        # Hash saja yang tersimpan; plaintext recovery code tidak ada di DB.
        stored = row["totp_recovery"]
        for c in codes:
            self.assertNotIn(c, stored)
        self.assertEqual(len(json.loads(stored)), 8)
        client.get("/logout")

    def test_status_dan_disable_wajib_password_kode(self):
        client = m.app.test_client()
        client.post("/login", data={"username": "admin", "password": ADMIN_PW})
        r = client.get("/api/2fa/status", headers=JSON_HDR)
        self.assertEqual(r.get_json(), {"enabled": False, "recovery_left": 0})

        _secret, codes = _wire_totp(client)
        r = client.get("/api/2fa/status", headers=JSON_HDR)
        self.assertEqual(r.get_json(), {"enabled": True, "recovery_left": 8})

        # tanpa password / tanpa kode / password salah
        r = client.post("/api/2fa/disable", json={"code": "123456"}, headers=JSON_HDR)
        self.assertEqual(r.status_code, 400)
        r = client.post(
            "/api/2fa/disable", json={"current_password": ADMIN_PW}, headers=JSON_HDR
        )
        self.assertEqual(r.status_code, 400)
        r = client.post(
            "/api/2fa/disable",
            json={"current_password": "salah", "code": "123456"},
            headers=JSON_HDR,
        )
        self.assertEqual(r.status_code, 400)
        self.assertEqual(_db_user_2fa()["totp_enabled"], 1)
        self.assertTrue(_audit_exists("auth.2fa.disabled") is False)

        # Password benar + kode benar -> nonaktif + recovery ikut terhapus
        secret = m.twofa.state("admin")["secret"]
        r = client.post(
            "/api/2fa/disable",
            json={
                "current_password": ADMIN_PW,
                "code": pyotp.TOTP(secret).now(),
            },
            headers=JSON_HDR,
        )
        self.assertEqual(r.status_code == 400, False, r.get_data(as_text=True))
        row = _db_user_2fa()
        self.assertEqual(row["totp_enabled"], 0)
        self.assertEqual(row["totp_secret"], "")
        self.assertEqual(row["totp_recovery"], "")
        self.assertTrue(_audit_exists("auth.2fa.disabled"))
        client.get("/logout")


class TwoFaLoginFlowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_tables()

    def setUp(self):
        _clear_admin_2fa()

    def tearDown(self):
        _clear_admin_2fa()

    def _fresh_pending(self):
        """Klien baru yang sudah lewat tahap password (pending di session)."""
        client = m.app.test_client()
        r = client.post("/login", data={"username": "admin", "password": ADMIN_PW})
        assert r.status_code == 302
        assert "/login/2fa" in r.headers.get("Location", "")
        return client

    def test_belum_login_sebelum_kode_benar(self):
        secret, _codes = _wire_totp(m.app.test_client())
        client = self._fresh_pending()
        # GET halaman 2FA tampil
        r = client.get("/login/2fa")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Verifikasi Dua Faktor", r.get_data(as_text=True))
        # Belum terotentikasi: dashboard masih menolak
        r = client.get("/dashboard")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login", r.headers.get("Location", ""))

    def test_kode_salah_ditolak_dan_audit(self):
        _secret, _codes = _wire_totp(m.app.test_client())
        client = self._fresh_pending()
        r = client.post("/login/2fa", data={"code": "000000"})
        self.assertEqual(r.status_code, 200)
        body = r.get_data(as_text=True)
        self.assertIn("Kode salah", body)
        self.assertTrue(_audit_exists("auth.2fa.failed"))
        # Tetap belum masuk
        self.assertEqual(client.get("/dashboard").status_code, 302)

    def test_kode_benar_masuk_dan_replay_ditolak(self):
        secret, _codes = _wire_totp(m.app.test_client())
        self.assertGreater(m.twofa.state("admin")["last_step"], 0)

        client = self._fresh_pending()
        code = _next_window_code(secret)
        r = client.post("/login/2fa", data={"code": code})
        self.assertEqual(r.status_code, 302)
        self.assertNotIn("/login/2fa", r.headers.get("Location", ""))
        self.assertEqual(client.get("/dashboard").status_code, 200)
        self.assertTrue(_audit_exists("auth.2fa.success"))

        # Sesi kedua memakai KODE YANG SAMA -> replay ditolak
        client2 = self._fresh_pending()
        r = client2.post("/login/2fa", data={"code": code})
        self.assertEqual(r.status_code, 200)
        self.assertIn("sudah dipakai", r.get_data(as_text=True))
        self.assertEqual(client2.get("/dashboard").status_code, 302)

    def test_pending_kedaluwarsa(self):
        secret, _codes = _wire_totp(m.app.test_client())
        client = self._fresh_pending()
        with client.session_transaction() as sess:
            sess["2fa_pending"] = {
                "u": "admin",
                "t": time.time() - (m.twofa.PENDING_TTL_S + 10),
                "remember": False,
                "next": None,
            }
        r = client.post("/login/2fa", data={"code": _next_window_code(secret)})
        self.assertEqual(r.status_code, 302)
        with client.session_transaction() as sess:
            self.assertNotIn("2fa_pending", sess)
        self.assertEqual(client.get("/dashboard").status_code, 302)

    def test_tanpa_pending_dialihkan_ke_login(self):
        client = m.app.test_client()
        r = client.get("/login/2fa")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login", r.headers.get("Location", ""))
        r = client.post("/login/2fa", data={"code": "123456"})
        self.assertEqual(r.status_code, 302)

    def test_blokir_per_username_setelah_5_gagal(self):
        secret, _codes = _wire_totp(m.app.test_client())
        client = self._fresh_pending()
        for i, salah in enumerate(("000000", "111111", "222222", "333333", "444444")):
            r = client.post("/login/2fa", data={"code": salah})
            self.assertIn("Kode salah", r.get_data(as_text=True), f"attempt {i}")
        # Percobaan ke-6 MESKIPUN kode benar -> diblokir per username
        code = pyotp.TOTP(secret).now()
        r = client.post("/login/2fa", data={"code": code})
        self.assertEqual(r.status_code, 200)
        self.assertIn("Diblokir", r.get_data(as_text=True))
        self.assertEqual(client.get("/dashboard").status_code, 302)

    def test_recovery_sekali_pakai(self):
        _secret, codes = _wire_totp(m.app.test_client())
        recovery = codes[0]

        client = self._fresh_pending()
        r = client.post("/login/2fa", data={"code": recovery})
        self.assertEqual(r.status_code == 400, False, r.get_data(as_text=True))
        self.assertNotIn("/login/2fa", r.headers.get("Location", ""))
        self.assertEqual(client.get("/dashboard").status_code, 200)
        self.assertTrue(_audit_exists("auth.2fa.recovery_used"))
        # Status: recovery tersisa 7
        r = client.get("/api/2fa/status", headers=JSON_HDR)
        self.assertEqual(r.get_json()["recovery_left"], 7)

        # Recovery code yang sama tidak bisa dipakai lagi
        client2 = self._fresh_pending()
        r = client2.post("/login/2fa", data={"code": recovery})
        self.assertIn("Kode salah", r.get_data(as_text=True))

        # Recovery code KEDUA masih bisa (format dengan spasi/huruf kecil)
        client3 = self._fresh_pending()
        r = client3.post("/login/2fa", data={"code": " " + codes[1].lower() + " "})
        self.assertNotIn("/login/2fa", r.headers.get("Location", ""))

    def test_last_login_tercatat_pada_users(self):
        _secret, _codes = _wire_totp(m.app.test_client())
        row_sebelum = _db_user_2fa()
        client = self._fresh_pending()
        client.post("/login/2fa", data={"code": _next_window_code(_secret)})
        row_sesudah = _db_user_2fa()
        self.assertTrue(row_sesudah["last_login"], "last_login harus terisi")
        self.assertGreaterEqual(
            row_sesudah["last_login"], row_sebelum["last_login"] or ""
        )

    def test_renext_penuh_sinkron_audit_login(self):
        """next_page valid dibawa dari pending ke redirect akhir."""
        secret, _codes = _wire_totp(m.app.test_client())
        client = m.app.test_client()
        r = client.post(
            "/login?next=/audit_logs", data={"username": "admin", "password": ADMIN_PW}
        )
        self.assertIn("/login/2fa", r.headers.get("Location", ""))
        r = client.post("/login/2fa", data={"code": _next_window_code(secret)})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(
            r.headers.get("Location", "").rsplit("?", 1)[0].endswith("/audit_logs"),
            True,
        )


class TwoFaAdminResetTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_tables()

    def setUp(self):
        _clear_admin_2fa()

    def tearDown(self):
        _clear_admin_2fa()
        # bersihkan user uji
        conn, c = m.get_db()
        c.execute("DELETE FROM users WHERE username=?", ("opuser2fa",))
        conn.commit()
        conn.close()

    def _buat_operator(self):
        """Buat user operator lewat API (login admin dulu)."""
        client = m.app.test_client()
        rw = client.post("/login", data={"username": "admin", "password": ADMIN_PW})
        assert rw.status_code == 302
        r = client.post(
            "/api/users",
            json={
                "username": "opuser2fa",
                "password": "SandiOp#2026",
                "role": "operator",
            },
            headers=JSON_HDR,
        )
        assert r.status_code in (200, 400, 201), r.get_data(as_text=True)
        return client

    def test_butuh_role_admin(self):
        client = self._buat_operator()
        fake = _FakeUser(username="opuser2fa", role="operator")
        with mock.patch("nms.auth.current_user", fake):
            r = client.post(
                "/api/users/opuser2fa/2fa/reset",
                json={},
                headers=JSON_HDR,
            )
            self.assertEqual(r.status_code, 403, r.get_data(as_text=True))

    def test_admin_tak_bisa_reset_dirinya(self):
        client = self._buat_operator()
        r = client.post("/api/users/admin/2fa/reset", json={}, headers=JSON_HDR)
        self.assertEqual(r.status_code, 400)
        self.assertIn("/api/2fa/disable", r.get_json()["error"])
        client.get("/logout")

    def test_reset_user_lain(self):
        client = self._buat_operator()
        # isi state 2FA palsu untuk opuser2fa lewat helper db
        m.twofa.ensure_2fa_row("opuser2fa", role="operator")
        m.twofa.store_pending_secret("opuser2fa", "JBSWY3DPEHPK3PXP")
        m.twofa.enable("opuser2fa", [m.twofa.recovery_sha256("AAAA-BBBB")])
        self.assertTrue(m.twofa.state("opuser2fa")["enabled"])

        r = client.post("/api/users/opuser2fa/2fa/reset", json={}, headers=JSON_HDR)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        st = m.twofa.state("opuser2fa")
        self.assertFalse(st["enabled"])
        self.assertEqual(st["secret"], "")
        self.assertTrue(_audit_exists("auth.2fa.reset"))
        client.get("/logout")

    def test_reset_user_tak_dikenal(self):
        client = self._buat_operator()
        r = client.post("/api/users/nosuchuser999/2fa/reset", json={}, headers=JSON_HDR)
        self.assertEqual(r.status_code, 404)
        client.get("/logout")


if __name__ == "__main__":
    unittest.main()
