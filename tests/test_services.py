import os
import tempfile
import unittest
from unittest import mock

_tmp = tempfile.mkdtemp(prefix="nms_test_services_")
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
from nms import service_checks as sc

try:
    m.scheduler.shutdown(wait=False)
except Exception:
    pass

JSON_HDR = {"Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest"}


class IcmpCheckTest(unittest.TestCase):
    def test_online_bila_loss_kurang_100(self):
        with mock.patch.object(sc, "ping_host", lambda h: (12.5, 30.0)):
            ok, lat = sc.check_icmp_service("10.9.9.9")
        self.assertTrue(ok)
        self.assertEqual(lat, 12.5)

    def test_online_full_loss_nol(self):
        with mock.patch.object(sc, "ping_host", lambda h: (5.0, 0.0)):
            ok, _ = sc.check_icmp_service("10.9.9.9")
        self.assertTrue(ok)

    def test_offline_loss_100(self):
        with mock.patch.object(sc, "ping_host", lambda h: (-1, 100.0)):
            ok, lat = sc.check_icmp_service("10.9.9.9")
        self.assertFalse(ok)
        self.assertEqual(lat, 0.0)

    def test_offline_ping_host_error(self):
        def boom(h):
            raise RuntimeError("down")

        with mock.patch.object(sc, "ping_host", boom):
            ok, _ = sc.check_icmp_service("10.9.9.9")
        self.assertFalse(ok)

    def test_wrapper_lama_tuple_6(self):
        # (id, ip, name, type, port, url) — tuple lama tanpa query/expected
        with mock.patch.object(sc, "ping_host", lambda h: (9.9, 0.0)):
            sid, status, lat = m.check_single_service(
                (42, "10.9.9.9", "gw", "icmp", None, None)
            )
        self.assertEqual((sid, status), (42, "ONLINE"))
        self.assertEqual(lat, 9.9)

    def test_run_check_online_dan_offline(self):
        with mock.patch.object(sc, "ping_host", lambda h: (3.0, 0.0)):
            status, lat = sc.run_check("icmp", "10.9.9.9", None, None)
        self.assertEqual(status, "ONLINE")
        self.assertGreaterEqual(lat, 0)
        with mock.patch.object(sc, "ping_host", lambda h: (-1, 100.0)):
            status, lat = sc.run_check("icmp", "10.9.9.9", None, None)
        self.assertEqual((status, lat), ("OFFLINE", 0))

    def test_tipe_tak_dikenal_pending(self):
        status, _ = sc.run_check("udp", "10.9.9.9", 53, None)
        self.assertEqual(status, "PENDING")


class _FakeAnswer:
    def __init__(self, text):
        self._t = text

    def __str__(self):
        return self._t


class _FakeResolver:
    """Resolver dnspython tiruan; behaviour diatur lewat resolver_factory."""

    instances = []

    def __init__(self, behaviour=None):
        self.configure = True
        self.nameservers = []
        self.lifetime = 0
        self.timeout = 0
        self.behaviour = behaviour or {}
        _FakeResolver.instances.append(self)

    def resolve(self, query, rtype):
        fn = self.behaviour.get(rtype)
        if fn is None:
            raise Exception("SERVFAIL")
        return fn(query, rtype)


class DnsCheckTest(unittest.TestCase):
    def setUp(self):
        _FakeResolver.instances = []

    def _patch(self, behaviour):
        return mock.patch.object(
            sc.dns.resolver,
            "Resolver",
            lambda configure=False: _FakeResolver(behaviour),
        )

    def test_cocok_a_record(self):
        behaviour = {
            "A": lambda q, r: [_FakeAnswer("93.184.216.34")],
            "AAAA": lambda q, r: [],
        }
        with self._patch(behaviour):
            self.assertTrue(
                sc.check_dns_service("8.8.8.8", "example.com", "93.184.216.34")
            )
            self.assertTrue(sc.check_dns_service("8.8.8.8", "example.com", ""))
            self.assertFalse(sc.check_dns_service("8.8.8.8", "example.com", "1.2.3.4"))

    def test_cocok_aaaa_kanonik(self):
        behaviour = {
            "A": lambda q, r: [],
            "AAAA": lambda q, r: [_FakeAnswer("2001:db8::1")],
        }
        with self._patch(behaviour):
            # bentuk disingkat vs panjang tetap dianggap sama
            self.assertTrue(
                sc.check_dns_service("8.8.8.8", "example.com", "2001:0db8:0000::1")
            )

    def test_timeout_atau_nxdomain_offline(self):
        behaviour = {
            "A": lambda q, r: (_ for _ in ()).throw(TimeoutError("timed out")),
            "AAAA": lambda q, r: [],
        }
        with self._patch(behaviour):
            self.assertFalse(sc.check_dns_service("8.8.8.8", "example.com", ""))

    def test_nameserver_hostname_diterjemahkan(self):
        behaviour = {"A": lambda q, r: [_FakeAnswer("1.1.1.1")], "AAAA": None}
        with (
            self._patch(behaviour),
            mock.patch.object(
                sc.socket,
                "getaddrinfo",
                lambda host, port, type=0: [(2, 1, 17, "", ("9.9.9.9", 53))],
            ),
        ):
            self.assertTrue(
                sc.check_dns_service("dns.example", "example.com", "1.1.1.1")
            )
        cfg = _FakeResolver.instances[-1]
        self.assertEqual(cfg.nameservers, ["9.9.9.9"])

    def test_query_kosong_offline(self):
        with self._patch({"A": lambda q, r: [_FakeAnswer("1.1.1.1")]}):
            self.assertFalse(sc.check_dns_service("8.8.8.8", "  ", ""))


class _FakeResp:
    def __init__(self, body=b"Dashboard KOPEGTEL", status=200):
        self.status_code = status
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def iter_content(self, chunk_size=8192):
        return iter([self._body])


class HttpKeywordTest(unittest.TestCase):
    ALLOW_ALL = lambda self, url: True

    def _run(
        self, body=b"Dashboard KOPEGTEL", status=200, expected="Dashboard", allow=None
    ):
        with mock.patch.object(
            sc.requests, "get", return_value=_FakeResp(body, status)
        ):
            return sc.check_http_service(
                "https://example.com",
                expected,
                allow if allow is not None else self.ALLOW_ALL,
            )

    def test_keyword_ada(self):
        self.assertTrue(self._run())

    def test_keyword_tidak_ada(self):
        self.assertFalse(self._run(expected="NotFound"))

    def test_status_brok_beku_gagal(self):
        self.assertFalse(self._run(status=404, expected="Dashboard"))

    def test_tanpa_keyword_status_ok(self):
        self.assertTrue(self._run(expected=""))

    def test_keyword_melewati_batas_64kb(self):
        big = b"x" * (sc._HTTP_BODY_LIMIT + 1024) + b"Dashboard"
        self.assertFalse(self._run(body=big, expected="Dashboard"))

    def test_batasi_dalam_64kb(self):
        body = b"x" * (sc._HTTP_BODY_LIMIT - 100) + b"Dashboard" + b"y" * 10
        self.assertTrue(self._run(body=body, expected="Dashboard"))

    def test_url_ditolak_lewat_allow_fn(self):
        self.assertFalse(self._run(allow=lambda u: False))


class ApiValidationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = m.app.test_client()
        r = cls.client.post(
            "/login", data={"username": "admin", "password": "PantauNms2026"}
        )
        assert r.status_code == 302

    def _post(self, body):
        return self.client.post("/api/services", json=body, headers=JSON_HDR)

    def _cleanup(self):
        conn, c = m.get_db()
        c.execute("DELETE FROM services")
        conn.commit()
        conn.close()

    def setUp(self):
        self._cleanup()

    def tearDown(self):
        self._cleanup()

    def test_tipe_invalid(self):
        r = self._post({"ip": "10.1.1.1", "name": "x", "type": "udp", "port": 53})
        self.assertEqual(r.status_code, 400)

    def test_dns_tanpa_query(self):
        r = self._post({"ip": "8.8.8.8", "name": "dns", "type": "dns"})
        self.assertEqual(r.status_code, 400)

    def test_dns_query_panjang(self):
        r = self._post(
            {"ip": "8.8.8.8", "name": "dns", "type": "dns", "query": "a" * 254}
        )
        self.assertEqual(r.status_code, 400)

    def test_dns_query_format_salah(self):
        for bad in ("-abc.com", "abc-.com", "test_uji.com", "a..b.com", "spasi in.com"):
            r = self._post(
                {"ip": "8.8.8.8", "name": "dns", "type": "dns", "query": bad}
            )
            self.assertEqual(r.status_code, 400, bad)

    def test_expected_panjang(self):
        r = self._post(
            {
                "ip": "8.8.8.8",
                "name": "dns",
                "type": "dns",
                "query": "google.com",
                "expected": "x" * 201,
            }
        )
        self.assertEqual(r.status_code, 400)
        r = self._post(
            {
                "ip": "10.1.1.9",
                "name": "web",
                "type": "http",
                "url": "https://example.com",
                "expected": "x" * 201,
            }
        )
        self.assertEqual(r.status_code, 400)

    def test_buat_icmp_ok_dan_duplikat(self):
        r = self._post({"ip": "10.9.9.26", "name": "gw", "type": "icmp"})
        self.assertEqual(r.status_code, 201)
        r2 = self._post({"ip": "10.9.9.26", "name": "gw2", "type": "icmp"})
        self.assertEqual(r2.status_code, 400)
        r3 = self._post({"ip": "10.9.9.27", "name": "gw3", "type": "icmp"})
        self.assertEqual(r3.status_code, 201)

    def test_buat_dns_ok_dan_duplikat_per_query(self):
        r = self._post(
            {
                "ip": "8.8.8.8",
                "name": "dns",
                "type": "dns",
                "query": "google.com",
                "expected": "",
            }
        )
        self.assertEqual(r.status_code, 201)
        # DNS server sama + query sama -> duplikat
        r2 = self._post(
            {"ip": "8.8.8.8", "name": "dns2", "type": "dns", "query": "google.com"}
        )
        self.assertEqual(r2.status_code, 400)
        # DNS server sama, query beda -> boleh
        r3 = self._post(
            {"ip": "8.8.8.8", "name": "dns3", "type": "dns", "query": "kopegtel.id"}
        )
        self.assertEqual(r3.status_code, 201)
        conn, c = m.get_db()
        row = c.execute(
            "SELECT query, expected, url, port FROM services WHERE type='dns' AND query='google.com'"
        ).fetchone()
        conn.close()
        self.assertEqual(dict(row)["query"], "google.com")
        self.assertIsNone(dict(row)["url"])
        self.assertIsNone(dict(row)["port"])

    def test_icmp_tak_menyimpan_url_dan_expected(self):
        r = self._post(
            {
                "ip": "10.9.9.26",
                "name": "gw",
                "type": "icmp",
                "expected": "harusnya diabaikan",
                "port": 5,
            }
        )
        self.assertEqual(r.status_code, 201)
        conn, c = m.get_db()
        row = c.execute(
            "SELECT url, port, query, expected FROM services WHERE type='icmp'"
        ).fetchone()
        conn.close()
        d = dict(row)
        self.assertIsNone(d["url"])
        self.assertIsNone(d["port"])
        self.assertEqual(d["query"], "")
        self.assertEqual(d["expected"], "")


class CheckServicesIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = m.app.test_client()
        r = cls.client.post(
            "/login", data={"username": "admin", "password": "PantauNms2026"}
        )
        assert r.status_code == 302

    def setUp(self):
        conn, c = m.get_db()
        c.execute("DELETE FROM services")
        conn.commit()
        conn.close()

    def tearDown(self):
        conn, c = m.get_db()
        c.execute("DELETE FROM services")
        c.execute("DELETE FROM service_history")
        conn.commit()
        conn.close()

    def _row(self, svc_id):
        conn, c = m.get_db()
        r = c.execute(
            "SELECT status, latency FROM services WHERE id=?", (svc_id,)
        ).fetchone()
        conn.close()
        return dict(r)

    def test_icmp_dan_dns_masuk_histori(self):
        r = self.client.post(
            "/api/services",
            json={"ip": "10.9.9.26", "name": "gw", "type": "icmp"},
            headers=JSON_HDR,
        )
        sid_icmp = r.get_json()["id"]
        r = self.client.post(
            "/api/services",
            json={
                "ip": "8.8.8.8",
                "name": "dns-google",
                "type": "dns",
                "query": "google.com",
                "expected": "142.250.4.139",
            },
            headers=JSON_HDR,
        )
        sid_dns = r.get_json()["id"]

        with (
            mock.patch.object(sc, "ping_host", lambda h: (7.5, 0.0)),
            mock.patch.object(
                sc.dns.resolver,
                "Resolver",
                lambda configure=False: _FakeResolver(
                    {
                        "A": lambda q, t: [_FakeAnswer("142.250.4.139")],
                        "AAAA": lambda q, t: [],
                    }
                ),
            ),
        ):
            m.check_services()

        self.assertEqual(self._row(sid_icmp)["status"], "ONLINE")
        self.assertAlmostEqual(self._row(sid_icmp)["latency"], 7.5, places=1)
        self.assertEqual(self._row(sid_dns)["status"], "ONLINE")
        conn, c = m.get_db()
        n = c.execute(
            "SELECT COUNT(*) FROM service_history WHERE service_id IN (?, ?)",
            (sid_icmp, sid_dns),
        ).fetchone()[0]
        conn.close()
        self.assertEqual(n, 2)

    def test_http_keyword_gagal_histori_tetap_offline(self):
        r = self.client.post(
            "/api/services",
            json={
                "ip": "10.1.1.9",
                "name": "web",
                "type": "http",
                "url": "https://example.com",
                "expected": "TidakAdaKeyword",
            },
            headers=JSON_HDR,
        )
        sid = r.get_json()["id"]
        with mock.patch.object(sc.requests, "get", return_value=_FakeResp()):
            m.check_services()
        self.assertEqual(self._row(sid)["status"], "OFFLINE")


class PublicStatusTest(unittest.TestCase):
    """Status halaman publik tidak error untuk tipe baru."""

    @classmethod
    def setUpClass(cls):
        cls.client = m.app.test_client()

    def test_status_api_memuat_service_baru(self):
        conn, c = m.get_db()
        c.execute("DELETE FROM services")
        c.execute(
            "INSERT INTO services (ip, name, type, status) VALUES (?, ?, ?, ?)",
            ("10.9.9.26", "gw-icmp", "icmp", "ONLINE"),
        )
        c.execute(
            "INSERT INTO services (ip, name, type, status, query) VALUES (?, ?, ?, ?, ?)",
            ("8.8.8.8", "dns-google", "dns", "OFFLINE", "google.com"),
        )
        conn.commit()
        conn.close()
        try:
            r = self.client.get("/api/public/status")
            self.assertEqual(r.status_code, 200)
            j = r.get_json()
            types = {s["type"] for s in j["services"]}
            self.assertIn("icmp", types)
            self.assertIn("dns", types)
        finally:
            conn, c = m.get_db()
            c.execute("DELETE FROM services")
            conn.commit()
            conn.close()


if __name__ == "__main__":
    unittest.main()
