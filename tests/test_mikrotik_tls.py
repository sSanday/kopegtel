"""Regresi M-11: verifikasi TLS untuk RouterOS API-SSL tidak boleh dimatikan.

Cakupan:
- Normalisasi fingerprint (format colon/spasi/strip/awalan sha256:).
- ``tls.ssl_wrapper`` terhadap TLS server lokal bersertifikat self-signed:
  fingerprint cocok diterima, tidak cocok ditolak sebelum kredensial dikirim,
  tanpa fingerprint ditolak oleh verifikasi CA standar.
- ``check_mikrotik`` end-to-end lewat RouterOS tiruan di atas TLS.
- ``perform_action`` memetakan kegagalan TLS ke ``ActionError.code``.
- Route perangkat: fingerprint disimpan ternormalisasi, validasi format,
  preserve/clear saat update, diteruskan ke uji koneksi.
- Ketiga lokasi temuan (mikrotik.py, optical.py, mikrotik_modules.py) tidak
  lagi memakai ``CERT_NONE``.
"""

import hashlib
import os
import socket
import ssl
import sys
import threading
from datetime import datetime, timedelta, timezone

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from werkzeug.security import generate_password_hash

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, "backend"))

import app as app_module  # noqa: E402
from collectors import mikrotik as mikrotik_collector  # noqa: E402
from collectors import mikrotik_modules  # noqa: E402
from collectors import tls as tls_util  # noqa: E402
import db  # noqa: E402
from routes import devices as devices_module  # noqa: E402
from fake_routeros import FakeRouterOS  # noqa: E402

OPERATOR = "operator-m11"
OPERATOR_PASSWORD = "operator-m11-password"
DEVICE_IP = "192.168.30.1"


# ---------------------------------------------------------
# Sertifikat self-signed sekali per modul untuk uji TLS.
# ---------------------------------------------------------

def _make_self_signed(tmp_path_factory):
    directory = tmp_path_factory.mktemp("tls")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=365))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(
                __import__("ipaddress").ip_address("127.0.0.1")
            )]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path = directory / "selfsigned.pem"
    key_path = directory / "selfsigned-key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    der = cert.public_bytes(serialization.Encoding.DER)
    fingerprint = hashlib.sha256(der).hexdigest()
    return str(cert_path), str(key_path), fingerprint


@pytest.fixture(scope="module")
def self_signed(tmp_path_factory):
    return _make_self_signed(tmp_path_factory)


class TlsFakeRouterOS(FakeRouterOS):
    """FakeRouterOS lewat TLS (sertifikat self-signed)."""

    def __init__(self, certfile, keyfile, **kwargs):
        super().__init__(**kwargs)
        self._certfile = certfile
        self._keyfile = keyfile

    def _serve(self):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self._certfile, self._keyfile)
        while True:
            try:
                conn, _ = self._sock.accept()
            except (OSError, AttributeError):
                return
            self.connections += 1
            try:
                tls_conn = context.wrap_socket(conn, server_side=True)
            except (ssl.SSLError, OSError, ConnectionError):
                # Klien memutus sebelum handshake selesai (mis. sertifikat
                # ditolak verifikasi) - bukan galat uji.
                try:
                    conn.close()
                except OSError:
                    pass
                continue
            threading.Thread(target=self._handle, args=(tls_conn,), daemon=True).start()


# ---------------------------------------------------------
# Normalisasi fingerprint
# ---------------------------------------------------------

def test_normalize_fingerprint_accepts_common_formats():
    plain = "ab" * 32
    assert tls_util.normalize_fingerprint(plain) == plain
    colon = ":".join(plain[i:i + 2] for i in range(0, 64, 2)).upper()
    assert tls_util.normalize_fingerprint(colon) == plain
    spaced = " ".join(plain[i:i + 2] for i in range(0, 64, 2))
    assert tls_util.normalize_fingerprint(spaced) == plain
    hyphen = "-".join(plain[i:i + 2] for i in range(0, 64, 2))
    assert tls_util.normalize_fingerprint(hyphen) == plain
    assert tls_util.normalize_fingerprint(f"sha256:{colon}") == plain
    assert tls_util.normalize_fingerprint(f"SHA-256 {colon}") == plain


def test_normalize_fingerprint_empty_is_none():
    assert tls_util.normalize_fingerprint("") is None
    assert tls_util.normalize_fingerprint("   ") is None
    assert tls_util.normalize_fingerprint(None) is None


def test_normalize_fingerprint_rejects_invalid():
    for bad in ("xyz", "ab" * 31, "ab" * 33, "zz" * 32, "0x" + "ab" * 32):
        with pytest.raises(ValueError):
            tls_util.normalize_fingerprint(bad)


def test_format_fingerprint_round_trip():
    plain = "cd" * 32
    formatted = tls_util.format_fingerprint(plain)
    assert formatted == ":".join(["CD"] * 32)
    assert tls_util.normalize_fingerprint(formatted) == plain


# ---------------------------------------------------------
# ssl_wrapper end-to-end terhadap TLS server nyata
# ---------------------------------------------------------

def _run_tls_server(certfile, keyfile, ready):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile, keyfile)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(4)
    ready(sock.getsockname()[1])
    while True:
        try:
            conn, _ = sock.accept()
        except (OSError, AttributeError):
            return
        try:
            context.wrap_socket(conn, server_side=True).close()
        except (ssl.SSLError, OSError, ConnectionError):
            try:
                conn.close()
            except OSError:
                pass


@pytest.fixture()
def tls_server(self_signed):
    certfile, keyfile, fingerprint = self_signed
    holder = {}
    ready = threading.Event()

    def _ready(port):
        holder["port"] = port
        ready.set()

    thread = threading.Thread(
        target=_run_tls_server, args=(certfile, keyfile, _ready), daemon=True
    )
    thread.start()
    assert ready.wait(timeout=5)
    holder["fingerprint"] = fingerprint
    return holder


def test_pin_match_accepts_self_signed(tls_server):
    wrapper = tls_util.ssl_wrapper("127.0.0.1", tls_server["fingerprint"])
    raw = socket.create_connection(("127.0.0.1", tls_server["port"]), timeout=5)
    with wrapper(raw) as tls_conn:
        assert tls_conn.getpeercert(binary_form=True)


def test_pin_mismatch_rejected_before_any_data(tls_server):
    wrong = "00" * 32
    wrapper = tls_util.ssl_wrapper("127.0.0.1", wrong)
    raw = socket.create_connection(("127.0.0.1", tls_server["port"]), timeout=5)
    with pytest.raises(tls_util.TlsVerificationError) as excinfo:
        wrapper(raw)
    assert excinfo.value.code == "tls_fingerprint_mismatch"


def test_no_fingerprint_rejects_self_signed(tls_server):
    wrapper = tls_util.ssl_wrapper("127.0.0.1", None)
    raw = socket.create_connection(("127.0.0.1", tls_server["port"]), timeout=5)
    with pytest.raises(ssl.SSLError):
        wrapper(raw)


# ---------------------------------------------------------
# check_mikrotik end-to-end lewat TLS
# ---------------------------------------------------------

def test_check_mikrotik_ssl_with_pin(self_signed):
    certfile, keyfile, fingerprint = self_signed
    server = TlsFakeRouterOS(certfile, keyfile, user="api", password="secret")
    server.start()
    try:
        config = {
            "ip": "127.0.0.1", "port": server.port, "user": "api",
            "password": "secret", "ssl": True, "ssl_fingerprint": fingerprint,
        }
        result = mikrotik_collector.check_mikrotik(config)
        assert result["ok"] is True
        assert result["identity"] == "fake-router"
    finally:
        server.stop()


def test_check_mikrotik_ssl_wrong_pin(self_signed):
    certfile, keyfile, _ = self_signed
    server = TlsFakeRouterOS(certfile, keyfile, user="api", password="secret")
    server.start()
    try:
        config = {
            "ip": "127.0.0.1", "port": server.port, "user": "api",
            "password": "secret", "ssl": True, "ssl_fingerprint": "ff" * 32,
        }
        result = mikrotik_collector.check_mikrotik(config)
        assert result["ok"] is False
        assert result["reason"] == "tls_fingerprint_mismatch"
        assert "fingerprint" in result["detail"].lower()
    finally:
        server.stop()


def test_check_mikrotik_ssl_without_fingerprint_rejects_self_signed(self_signed):
    certfile, keyfile, _ = self_signed
    server = TlsFakeRouterOS(certfile, keyfile, user="api", password="secret")
    server.start()
    try:
        config = {
            "ip": "127.0.0.1", "port": server.port, "user": "api",
            "password": "secret", "ssl": True,
        }
        result = mikrotik_collector.check_mikrotik(config)
        assert result["ok"] is False
        assert result["reason"] == "tls_error"
    finally:
        server.stop()


def test_check_mikrotik_invalid_stored_fingerprint_returns_tls_error():
    config = {
        "ip": "127.0.0.1", "port": 1, "user": "api", "password": "x",
        "ssl": True, "ssl_fingerprint": "bukan-hex",
    }
    result = mikrotik_collector.check_mikrotik(config)
    assert result["ok"] is False
    assert result["reason"] == "tls_error"


# ---------------------------------------------------------
# perform_action: kegagalan TLS -> ActionError.code
# ---------------------------------------------------------

def test_perform_action_maps_tls_mismatch(monkeypatch):
    def fake_connect(_config, timeout=5):
        raise tls_util.TlsVerificationError(
            "Fingerprint sertifikat RouterOS tidak cocok", "tls_fingerprint_mismatch"
        )

    monkeypatch.setattr(mikrotik_modules, "_action_connect", fake_connect)
    config = {
        "ip": "127.0.0.1", "port": 8729, "user": "api", "password": "x", "ssl": True,
    }
    with pytest.raises(mikrotik_modules.ActionError) as excinfo:
        mikrotik_modules.perform_action(config, "reboot", {"target": "x"})
    assert excinfo.value.code == "tls_fingerprint_mismatch"


# ---------------------------------------------------------
# Route / DB perangkat
# ---------------------------------------------------------

def _device_payload(name="mt-m11", ip=DEVICE_IP, **overrides):
    payload = {
        "name": name, "ip": ip, "type": "mikrotik", "location": "",
        "poll_interval": 60, "group_id": None, "parent_id": None,
        "maintenance": False, "snmp_enabled": False, "snmp_version": "v2c",
        "snmp_port": 161, "snmp_v3_auth_proto": "SHA", "snmp_v3_priv_proto": "AES",
        "mikrotik_enabled": True, "mikrotik_port": 8729,
        "mikrotik_user": "api", "mikrotik_password": "rahasia-m11",
        "mikrotik_ssl": True,
    }
    payload.update(overrides)
    return payload


# invalid fingerprint must never reach the wire; covered via routes below.


@pytest.fixture()
def client():
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as client:
        yield client


@pytest.fixture(autouse=True)
def reset_rate_limit():
    devices_module._test_calls.clear()
    yield
    devices_module._test_calls.clear()


@pytest.fixture()
def operator(fresh_db):
    fresh_db.create_user(
        OPERATOR,
        generate_password_hash(OPERATOR_PASSWORD),
        role="operator",
        must_change_password=0,
    )
    return OPERATOR


@pytest.fixture()
def collectors(monkeypatch):
    calls = {"mikrotik": []}

    def fake_ping(_ip):
        return {"ok": True}

    def fake_mikrotik(config):
        calls["mikrotik"].append(dict(config))
        return {"ok": True}

    monkeypatch.setattr(devices_module.ping_collector, "check_ping", fake_ping)
    monkeypatch.setattr(devices_module.snmp_collector, "check_snmp", lambda _c: {"ok": True})
    monkeypatch.setattr(devices_module.mikrotik_collector, "check_mikrotik", fake_mikrotik)
    return calls


def _login(client):
    response = client.post(
        "/api/login", json={"username": OPERATOR, "password": OPERATOR_PASSWORD}
    )
    assert response.status_code == 200, response.get_json()
    return response.get_json()["csrf_token"]


def test_device_create_route_normalizes_and_rejects(client, fresh_db, operator):
    csrf = _login(client)
    colon = ":".join(["AB"] * 32)

    created = client.post(
        "/api/devices",
        json=_device_payload(name="mt-m11-create", ip="192.168.30.70",
                             mikrotik_ssl_fingerprint=colon),
        headers={"X-CSRF-Token": csrf},
    )
    assert created.status_code == 201, created.get_json()
    device_id = created.get_json()["device"]["id"]
    assert fresh_db.get_device(device_id)["mikrotik_ssl_fingerprint"] == "ab" * 32

    bad = client.post(
        "/api/devices",
        json=_device_payload(name="mt-m11-bad", ip="192.168.30.73",
                             mikrotik_ssl_fingerprint="bukan-hex"),
        headers={"X-CSRF-Token": csrf},
    )
    assert bad.status_code == 400


def test_device_update_route_preserve_and_clear(client, fresh_db, operator):
    device_id = fresh_db.create_device(
        _device_payload(name="mt-m11-update", ip="192.168.30.71",
                        mikrotik_ssl_fingerprint="cd" * 32)
    )
    csrf = _login(client)
    full = _device_payload(name="mt-m11-update", ip="192.168.30.71",
                           mikrotik_ssl_fingerprint="cd" * 32)

    # Field fingerprint tidak dikirim -> nilai tersimpan dipertahankan.
    without = dict(full)
    without.pop("mikrotik_ssl_fingerprint")
    res = client.put(f"/api/devices/{device_id}", json=without,
                     headers={"X-CSRF-Token": csrf})
    assert res.status_code == 200, res.get_json()
    assert fresh_db.get_device(device_id)["mikrotik_ssl_fingerprint"] == "cd" * 32

    # String kosong -> pinning dihapus.
    res = client.put(f"/api/devices/{device_id}",
                     json=dict(full, mikrotik_ssl_fingerprint=""),
                     headers={"X-CSRF-Token": csrf})
    assert res.status_code == 200, res.get_json()
    assert fresh_db.get_device(device_id)["mikrotik_ssl_fingerprint"] is None

    # Fingerprint tidak valid -> 400.
    res = client.put(f"/api/devices/{device_id}",
                     json=dict(full, mikrotik_ssl_fingerprint="zz" * 32),
                     headers={"X-CSRF-Token": csrf})
    assert res.status_code == 400


def test_device_test_forwards_and_validates_fingerprint(
    client, fresh_db, operator, collectors
):
    device_id = fresh_db.create_device(
        _device_payload(name="mt-m11-test", ip="192.168.30.72",
                        mikrotik_ssl_fingerprint="ef" * 32)
    )
    csrf = _login(client)

    ok = client.post(
        "/api/devices/test",
        json={
            "id": device_id, "ip": "192.168.30.72",
            "mikrotik_enabled": True, "mikrotik_port": 8729,
            "mikrotik_ssl": True,
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert ok.status_code == 200, ok.get_json()
    # Field tidak dikirim -> pakai fingerprint tersimpan.
    assert collectors["mikrotik"][0]["ssl_fingerprint"] == "ef" * 32

    bad = client.post(
        "/api/devices/test",
        json={
            "id": device_id, "ip": "192.168.30.72",
            "mikrotik_enabled": True, "mikrotik_port": 8729,
            "mikrotik_ssl": True, "mikrotik_ssl_fingerprint": "1234",
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert bad.status_code == 400
    assert len(collectors["mikrotik"]) == 1


# ---------------------------------------------------------
# Penjaga regresi: lokasi temuan tidak memakai CERT_NONE lagi.
# ---------------------------------------------------------

def test_reported_files_no_longer_disable_verification():
    reported = (
        os.path.join(BASE_DIR, "backend", "collectors", "mikrotik.py"),
        os.path.join(BASE_DIR, "backend", "collectors", "optical.py"),
        os.path.join(BASE_DIR, "backend", "collectors", "mikrotik_modules.py"),
    )
    for path in reported:
        with open(path, encoding="utf-8") as handle:
            source = handle.read()
        assert "CERT_NONE" not in source, f"{os.path.basename(path)} masih memakai CERT_NONE"


def test_poller_and_write_action_configs_carry_fingerprint():
    with open(os.path.join(BASE_DIR, "backend", "poller.py"), encoding="utf-8") as handle:
        assert "ssl_fingerprint" in handle.read()
    with open(os.path.join(BASE_DIR, "backend", "routes", "mikrotik.py"), encoding="utf-8") as handle:
        assert '"ssl_fingerprint"' in handle.read()
