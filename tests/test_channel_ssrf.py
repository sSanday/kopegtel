"""Regresi M-2: SSRF lewat kanal webhook dan SMTP.

- Operator tidak boleh mengaktifkan allow_private/allow_http pada webhook atau
  allow_private pada SMTP, dan tidak boleh mewarisinya dari kanal lama.
- Kanal privat/http hanya boleh diuji admin.
- Host di-resolve sekali; koneksi memakai IP hasil resolusi (anti-rebinding).
- Pesan galat SMTP tidak membocorkan banner/detail internal ke API.
"""

import os
import smtplib
import socket
import sys

import pytest
from werkzeug.security import generate_password_hash

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, "backend"))

import alerts  # noqa: E402
import app as app_module  # noqa: E402
import netguard  # noqa: E402
import webhook  # noqa: E402

PUBLIC_IP = "93.184.216.34"
PRIVATE_URL = "http://127.0.0.1:5500/hook"
PRIVATE_HTTPS_URL = "https://127.0.0.1:5500/hook"
PUBLIC_WEBHOOK_URL = "https://hooks.public.test/services/abc"
PUBLIC_SMTP_HOST = "smtp.public.test"


@pytest.fixture()
def client():
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as client:
        yield client


def _mk_user(db, username, password, role):
    db.create_user(
        username,
        generate_password_hash(password),
        role=role,
        must_change_password=0,
    )


def _login(client, username, password):
    response = client.post("/api/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.get_json()
    return response.get_json()["csrf_token"]


def _as_admin(client, fresh_db):
    _mk_user(fresh_db, "admin-m2", "admin-m2-password", role="admin")
    return _login(client, "admin-m2", "admin-m2-password")


def _as_operator(client, fresh_db):
    _mk_user(fresh_db, "operator-m2", "operator-m2-password", role="operator")
    return _login(client, "operator-m2", "operator-m2-password")


def _fake_dns(monkeypatch, address_for_call):
    """Ganti getaddrinfo global; address_for_call(call_index) -> IP."""
    calls = []

    def fake_getaddrinfo(host, port, *args, **kwargs):
        calls.append((host, port))
        address = address_for_call(len(calls)) if callable(address_for_call) else address_for_call
        return [
            (
                socket.AF_INET6 if ":" in address else socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                (address, port),
            )
        ]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    return calls


def _no_connect(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("koneksi keluar tidak boleh terjadi")

    monkeypatch.setattr(socket, "create_connection", fail)


def _record_failed_connect(monkeypatch, error=None):
    calls = []

    def fake_create_connection(address, *args, **kwargs):
        calls.append(address)
        raise error or OSError("koneksi uji gagal")

    monkeypatch.setattr(socket, "create_connection", fake_create_connection)
    return calls


def test_operator_cannot_enable_private_or_http_webhook(client, fresh_db):
    csrf = _as_operator(client, fresh_db)

    cases = (
        ({"url": PRIVATE_URL, "preset": "generic", "secret": "x", "allow_private": True}, "Hanya admin"),
        ({"url": PRIVATE_URL, "preset": "generic", "secret": "x", "allow_http": True}, "Hanya admin"),
        ({"url": PRIVATE_URL, "preset": "generic", "secret": "x"}, "URL ditolak"),
        ({"url": "http://93.184.216.34/hook", "preset": "generic", "secret": "x"}, "URL ditolak"),
    )
    for index, (config, expected) in enumerate(cases):
        response = client.post(
            "/api/channels",
            json={"name": f"webhook-operator-{index}", "type": "webhook", "config": config},
            headers={"X-CSRF-Token": csrf},
        )
        assert response.status_code == 400, (index, response.get_json())
        assert expected in response.get_json()["error"]

    assert fresh_db.list_channels() == []


def test_operator_cannot_create_private_smtp_channel(client, fresh_db):
    csrf = _as_operator(client, fresh_db)
    base = {
        "port": 587,
        "use_tls": False,
        "from_addr": "nms@contoh.test",
        "to_addr": "noc@contoh.test",
    }

    cases = (
        ({"host": "127.0.0.1", "allow_private": True}, "Hanya admin"),
        ({"host": "127.0.0.1"}, "Host SMTP ditolak"),
        ({"host": "::1"}, "Host SMTP ditolak"),
        ({"host": "169.254.169.254"}, "Host SMTP ditolak"),
        ({"host": "10.20.30.40"}, "Host SMTP ditolak"),
    )
    for index, (config, expected) in enumerate(cases):
        response = client.post(
            "/api/channels",
            json={
                "name": f"smtp-operator-{index}",
                "type": "smtp",
                "config": {**base, **config},
            },
            headers={"X-CSRF-Token": csrf},
        )
        assert response.status_code == 400, (index, response.get_json())
        assert expected in response.get_json()["error"]

    assert fresh_db.list_channels() == []


def test_admin_can_configure_private_targets_but_must_opt_in(client, fresh_db):
    csrf = _as_admin(client, fresh_db)

    webhook_response = client.post(
        "/api/channels",
        json={
            "name": "webhook-admin-privat",
            "type": "webhook",
            "config": {
                "url": PRIVATE_URL,
                "preset": "generic",
                "secret": "rahasia-m2-webhook",
                "allow_http": True,
                "allow_private": True,
            },
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert webhook_response.status_code == 201, webhook_response.get_json()
    webhook_config = fresh_db.channel_config(webhook_response.get_json()["id"])
    assert webhook_config["allow_private"] is True
    assert webhook_config["allow_http"] is True

    smtp_response = client.post(
        "/api/channels",
        json={
            "name": "smtp-admin-privat",
            "type": "smtp",
            "config": {
                "host": "127.0.0.1",
                "port": 2525,
                "use_tls": False,
                "allow_private": True,
                "from_addr": "nms@contoh.test",
                "to_addr": "noc@contoh.test",
            },
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert smtp_response.status_code == 201, smtp_response.get_json()
    assert fresh_db.channel_config(smtp_response.get_json()["id"])["allow_private"] is True

    # Admin pun harus memilih izin secara eksplisit.
    rejected = client.post(
        "/api/channels",
        json={
            "name": "smtp-admin-tanpa-izin",
            "type": "smtp",
            "config": {
                "host": "127.0.0.1",
                "port": 2525,
                "use_tls": False,
                "from_addr": "nms@contoh.test",
                "to_addr": "noc@contoh.test",
            },
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert rejected.status_code == 400
    assert "Host SMTP ditolak" in rejected.get_json()["error"]


def test_operator_cannot_retarget_or_test_admin_private_webhook(
    client, fresh_db, monkeypatch
):
    admin_csrf = _as_admin(client, fresh_db)
    created = client.post(
        "/api/channels",
        json={
            "name": "webhook-privat-lama",
            "type": "webhook",
            "config": {
                "url": PRIVATE_URL,
                "preset": "generic",
                "secret": "rahasia-m2-lama",
                "allow_http": True,
                "allow_private": True,
            },
        },
        headers={"X-CSRF-Token": admin_csrf},
    )
    channel_id = created.get_json()["id"]

    operator_client = app_module.app.test_client()
    operator_csrf = _as_operator(operator_client, fresh_db)
    for target in ("http://127.0.0.1:5500/evil", "https://10.0.0.7/evil"):
        response = operator_client.put(
            f"/api/channels/{channel_id}",
            json={
                "name": "webhook-privat-lama",
                "type": "webhook",
                "config": {"url": target, "preset": "generic"},
            },
            headers={"X-CSRF-Token": operator_csrf},
        )
        assert response.status_code == 400, response.get_json()
        assert "URL ditolak" in response.get_json()["error"]

    # Edit tanpa menyentuh target mempertahankan izin yang sudah diatur admin;
    # operator tetap tidak boleh menguji kanal tersebut.
    renamed = operator_client.put(
        f"/api/channels/{channel_id}",
        json={
            "name": "webhook-privat-lama-ubah",
            "type": "webhook",
            "config": {"preset": "generic"},
        },
        headers={"X-CSRF-Token": operator_csrf},
    )
    assert renamed.status_code == 200, renamed.get_json()
    stored = fresh_db.channel_config(channel_id)
    assert stored["url"] == PRIVATE_URL
    assert stored["allow_private"] is True
    assert stored["allow_http"] is True

    calls = []
    monkeypatch.setattr(
        alerts,
        "send_test",
        lambda channel: calls.append(channel) or (True, "terkirim"),
    )
    forbidden = operator_client.post(
        f"/api/channels/{channel_id}/test",
        headers={"X-CSRF-Token": operator_csrf},
    )
    assert forbidden.status_code == 403
    assert forbidden.get_json()["code"] == "forbidden"
    assert calls == []

    # Admin tetap boleh menguji kanal tersebut.
    allowed = client.post(
        f"/api/channels/{channel_id}/test",
        headers={"X-CSRF-Token": admin_csrf},
    )
    assert allowed.status_code == 200, allowed.get_json()
    assert calls == [channel_id]


def test_operator_retargeting_webhook_drops_admin_only_flags(client, fresh_db, monkeypatch):
    _fake_dns(monkeypatch, PUBLIC_IP)
    admin_csrf = _as_admin(client, fresh_db)
    created = client.post(
        "/api/channels",
        json={
            "name": "webhook-pindah-publik",
            "type": "webhook",
            "config": {
                "url": PRIVATE_URL,
                "preset": "generic",
                "secret": "rahasia-m2-pindah",
                "allow_http": True,
                "allow_private": True,
            },
        },
        headers={"X-CSRF-Token": admin_csrf},
    )
    channel_id = created.get_json()["id"]

    operator_client = app_module.app.test_client()
    operator_csrf = _as_operator(operator_client, fresh_db)
    response = operator_client.put(
        f"/api/channels/{channel_id}",
        json={
            "name": "webhook-pindah-publik",
            "type": "webhook",
            "config": {"url": PUBLIC_WEBHOOK_URL, "preset": "slack"},
        },
        headers={"X-CSRF-Token": operator_csrf},
    )
    assert response.status_code == 200, response.get_json()

    stored = fresh_db.channel_config(channel_id)
    assert stored["url"] == PUBLIC_WEBHOOK_URL
    assert stored["allow_private"] is False
    assert stored["allow_http"] is False


def test_operator_retargeting_smtp_drops_private_privilege(client, fresh_db, monkeypatch):
    _fake_dns(monkeypatch, PUBLIC_IP)
    admin_csrf = _as_admin(client, fresh_db)
    created = client.post(
        "/api/channels",
        json={
            "name": "smtp-pindah-publik",
            "type": "smtp",
            "config": {
                "host": "127.0.0.1",
                "port": 2525,
                "use_tls": False,
                "allow_private": True,
                "from_addr": "nms@contoh.test",
                "to_addr": "noc@contoh.test",
            },
        },
        headers={"X-CSRF-Token": admin_csrf},
    )
    assert created.status_code == 201, created.get_json()
    channel_id = created.get_json()["id"]

    operator_client = app_module.app.test_client()
    operator_csrf = _as_operator(operator_client, fresh_db)
    moved = operator_client.put(
        f"/api/channels/{channel_id}",
        json={
            "name": "smtp-pindah-publik",
            "type": "smtp",
            "config": {
                "host": PUBLIC_SMTP_HOST,
                "port": 2525,
                "use_tls": False,
                "from_addr": "nms@contoh.test",
                "to_addr": "noc@contoh.test",
            },
        },
        headers={"X-CSRF-Token": operator_csrf},
    )
    assert moved.status_code == 200, moved.get_json()
    assert fresh_db.channel_config(channel_id)["allow_private"] is False

    for target in ("127.0.0.1", "10.20.30.40", "169.254.169.254"):
        response = operator_client.put(
            f"/api/channels/{channel_id}",
            json={
                "name": "smtp-pindah-publik",
                "type": "smtp",
                "config": {
                    "host": target,
                    "port": 2525,
                    "use_tls": False,
                    "from_addr": "nms@contoh.test",
                    "to_addr": "noc@contoh.test",
                },
            },
            headers={"X-CSRF-Token": operator_csrf},
        )
        assert response.status_code == 400, (target, response.get_json())
        assert "Host SMTP ditolak" in response.get_json()["error"]


def test_webhook_send_rejects_private_and_pins_resolved_dns(monkeypatch):
    _no_connect(monkeypatch)
    ok, detail, attempts = webhook.send_webhook(
        {"url": PRIVATE_URL, "secret": "x", "preset": "generic"}, "uji", "uji"
    )
    assert ok is False
    assert attempts == 0
    assert "URL ditolak" in detail

    # Resolusi pertama publik, panggilan berikutnya privat: koneksi harus tetap
    # memakai IP publik pertama (tidak ada resolusi kedua).
    dns_calls = _fake_dns(
        monkeypatch,
        lambda index: PUBLIC_IP if index == 1 else "127.0.0.1",
    )
    connects = _record_failed_connect(monkeypatch)
    monkeypatch.setattr(webhook.time, "sleep", lambda seconds: None)
    ok, detail, attempts = webhook.send_webhook(
        {"url": "https://rebind.test/hook", "secret": "x", "preset": "generic"},
        "uji",
        "uji",
    )
    assert ok is False
    assert attempts == 3
    assert "Koneksi gagal" in detail
    assert len(dns_calls) == 1
    assert connects == [(PUBLIC_IP, 443)] * 3

    # Admin yang mengizinkan target privat tetap terhubung ke IP tersebut.
    connects = _record_failed_connect(monkeypatch)
    webhook.send_webhook(
        {
            "url": PRIVATE_URL,
            "secret": "x",
            "preset": "generic",
            "allow_http": True,
            "allow_private": True,
        },
        "uji",
        "uji",
    )
    assert connects == [("127.0.0.1", 5500)] * 3


def test_webhook_send_rejects_hostname_resolving_to_private(monkeypatch):
    _fake_dns(monkeypatch, "10.1.2.3")
    _no_connect(monkeypatch)
    ok, detail, attempts = webhook.send_webhook(
        {"url": "https://internal.test/hook", "secret": "x", "preset": "generic"},
        "uji",
        "uji",
    )
    assert ok is False
    assert attempts == 0
    assert "Target bukan IP publik" in detail


def test_webhook_send_rejects_mixed_public_and_private_dns(monkeypatch):
    def mixed(host, port, *args, **kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (PUBLIC_IP, port)),
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", port)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", mixed)
    _no_connect(monkeypatch)
    ok, detail, attempts = webhook.send_webhook(
        {"url": "https://campur.test/hook", "secret": "x", "preset": "generic"},
        "uji",
        "uji",
    )
    assert ok is False
    assert attempts == 0
    assert "Target bukan IP publik" in detail
    assert "127.0.0.1" not in detail


def test_admin_can_use_localhost_with_explicit_private_permission(client, fresh_db):
    admin_csrf = _as_admin(client, fresh_db)
    created = client.post(
        "/api/channels",
        json={
            "name": "webhook-localhost-admin",
            "type": "webhook",
            "config": {
                "url": "https://localhost:8443/hook",
                "preset": "generic",
                "secret": "x",
                "allow_private": True,
            },
        },
        headers={"X-CSRF-Token": admin_csrf},
    )
    assert created.status_code == 201, created.get_json()
    assert fresh_db.channel_config(created.get_json()["id"])["allow_private"] is True

    operator_client = app_module.app.test_client()
    operator_csrf = _as_operator(operator_client, fresh_db)
    rejected = operator_client.post(
        "/api/channels",
        json={
            "name": "webhook-localhost-operator",
            "type": "webhook",
            "config": {
                "url": "https://localhost:8443/hook",
                "preset": "generic",
                "secret": "x",
            },
        },
        headers={"X-CSRF-Token": operator_csrf},
    )
    assert rejected.status_code == 400
    assert "loopback" in rejected.get_json()["error"]


def test_smtp_tries_all_validated_addresses(monkeypatch):
    calls = []

    class _Sock:
        pass

    def fake_create_connection(address, *args, **kwargs):
        calls.append(address)
        if ":" in address[0]:
            raise OSError("IPv6 tidak tersedia")
        return _Sock()

    monkeypatch.setattr(socket, "create_connection", fake_create_connection)
    smtp = alerts._PinnedSMTP.__new__(alerts._PinnedSMTP)
    smtp._pinned_addresses = ["2606:4700:4700::1111", PUBLIC_IP]
    smtp._pinned_address = None
    smtp.source_address = None
    sock = smtp._get_socket(PUBLIC_SMTP_HOST, 587, 6)
    assert isinstance(sock, _Sock)
    assert calls == [("2606:4700:4700::1111", 587), (PUBLIC_IP, 587)]
    assert smtp._pinned_address == PUBLIC_IP

    # send_smtp meneruskan seluruh IP hasil validasi, bukan hanya yang pertama.
    def two_addresses(host, port, *args, **kwargs):
        return [
            (
                socket.AF_INET6,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("2606:4700:4700::1111", port),
            ),
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                (PUBLIC_IP, port),
            ),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", two_addresses)
    captured = {}

    class _Capture:
        def __init__(self, host, port, addresses, **kwargs):
            captured["addresses"] = list(addresses)
            raise OSError("koneksi uji gagal")

    monkeypatch.setattr(alerts, "_PinnedSMTP", _Capture)
    ok, detail = alerts.send_smtp(
        {
            "host": PUBLIC_SMTP_HOST,
            "port": 587,
            "use_tls": False,
            "from_addr": "nms@contoh.test",
            "to_addr": "noc@contoh.test",
        },
        "uji",
        "uji",
    )
    assert ok is False
    assert detail == "Koneksi SMTP gagal."
    assert captured["addresses"] == ["2606:4700:4700::1111", PUBLIC_IP]


def test_smtp_send_rejects_private_and_sanitizes_raw_errors(monkeypatch):
    _no_connect(monkeypatch)
    ok, detail = alerts.send_smtp(
        {
            "host": "127.0.0.1",
            "port": 25,
            "use_tls": False,
            "from_addr": "nms@contoh.test",
            "to_addr": "noc@contoh.test",
        },
        "uji",
        "uji",
    )
    assert ok is False
    assert "Target SMTP ditolak" in detail

    # Host publik: koneksi gagal dengan pesan mentah, respons harus generik.
    dns_calls = _fake_dns(monkeypatch, PUBLIC_IP)
    connects = _record_failed_connect(
        monkeypatch, OSError("220 banner internal-rahasia jangan bocor")
    )
    ok, detail = alerts.send_smtp(
        {
            "host": PUBLIC_SMTP_HOST,
            "port": 587,
            "use_tls": False,
            "from_addr": "nms@contoh.test",
            "to_addr": "noc@contoh.test",
        },
        "uji",
        "uji",
    )
    assert ok is False
    assert detail == "Koneksi SMTP gagal."
    assert "banner" not in detail and "rahasia" not in detail
    assert len(dns_calls) == 1
    assert connects == [(PUBLIC_IP, 587)]

    # Galat autentikasi juga tidak boleh mengembalikan banner server.
    class _RejectingSMTP:
        def __init__(self, *args, **kwargs):
            raise smtplib.SMTPAuthenticationError(
                535, b"535 5.7.8 banner internal-rahasia"
            )

    _fake_dns(monkeypatch, PUBLIC_IP)
    monkeypatch.setattr(alerts, "_PinnedSMTP", _RejectingSMTP)
    ok, detail = alerts.send_smtp(
        {
            "host": PUBLIC_SMTP_HOST,
            "port": 587,
            "use_tls": False,
            "username": "user",
            "password": "password",
            "from_addr": "nms@contoh.test",
            "to_addr": "noc@contoh.test",
        },
        "uji",
        "uji",
    )
    assert ok is False
    assert detail == "Autentikasi SMTP ditolak."
    assert "banner" not in detail and "rahasia" not in detail


def test_smtp_send_pins_resolved_dns(monkeypatch):
    dns_calls = _fake_dns(
        monkeypatch,
        lambda index: PUBLIC_IP if index == 1 else "127.0.0.1",
    )
    connects = _record_failed_connect(monkeypatch)
    ok, detail = alerts.send_smtp(
        {
            "host": PUBLIC_SMTP_HOST,
            "port": 2525,
            "use_tls": False,
            "from_addr": "nms@contoh.test",
            "to_addr": "noc@contoh.test",
        },
        "uji",
        "uji",
    )
    assert ok is False
    assert detail == "Koneksi SMTP gagal."
    assert len(dns_calls) == 1
    assert connects == [(PUBLIC_IP, 2525)]


def test_channel_test_endpoint_does_not_leak_smtp_banner(client, fresh_db, monkeypatch):
    _fake_dns(monkeypatch, PUBLIC_IP)
    admin_csrf = _as_admin(client, fresh_db)
    created = client.post(
        "/api/channels",
        json={
            "name": "smtp-banner",
            "type": "smtp",
            "config": {
                "host": PUBLIC_SMTP_HOST,
                "port": 587,
                "use_tls": False,
                "from_addr": "nms@contoh.test",
                "to_addr": "noc@contoh.test",
            },
        },
        headers={"X-CSRF-Token": admin_csrf},
    )
    assert created.status_code == 201, created.get_json()
    channel_id = created.get_json()["id"]

    class _RejectingSMTP:
        def __init__(self, *args, **kwargs):
            raise smtplib.SMTPResponseException(
                421, b"421 4.3.2 banner internal-rahasia"
            )

    _fake_dns(monkeypatch, PUBLIC_IP)
    monkeypatch.setattr(alerts, "_PinnedSMTP", _RejectingSMTP)
    monkeypatch.setattr(alerts, "RETRY_DELAY_SECONDS", 0)

    response = client.post(
        f"/api/channels/{channel_id}/test",
        headers={"X-CSRF-Token": admin_csrf},
    )

    assert response.status_code == 200, response.get_json()
    payload = response.get_json()
    assert payload["ok"] is False
    assert payload["detail"] == "Server SMTP menolak permintaan."
    body = response.get_data(as_text=True)
    assert "banner" not in body and "internal-rahasia" not in body


def test_env_private_escape_hatch_is_admin_only(client, fresh_db, monkeypatch):
    monkeypatch.setenv("ALLOW_PRIVATE_WEBHOOKS", "1")
    monkeypatch.setenv("ALLOW_PRIVATE_SMTP", "1")
    _fake_dns(monkeypatch, PUBLIC_IP)

    operator_client = app_module.app.test_client()
    operator_csrf = _as_operator(operator_client, fresh_db)
    webhook_response = operator_client.post(
        "/api/channels",
        json={
            "name": "webhook-env-operator",
            "type": "webhook",
            "config": {"url": PRIVATE_HTTPS_URL, "preset": "generic", "secret": "x"},
        },
        headers={"X-CSRF-Token": operator_csrf},
    )
    assert webhook_response.status_code == 400
    assert "URL ditolak" in webhook_response.get_json()["error"]

    smtp_response = operator_client.post(
        "/api/channels",
        json={
            "name": "smtp-env-operator",
            "type": "smtp",
            "config": {
                "host": "127.0.0.1",
                "port": 2525,
                "use_tls": False,
                "from_addr": "nms@contoh.test",
                "to_addr": "noc@contoh.test",
            },
        },
        headers={"X-CSRF-Token": operator_csrf},
    )
    assert smtp_response.status_code == 400
    assert "Host SMTP ditolak" in smtp_response.get_json()["error"]

    # Admin yang menyimpan di lingkungan escape hatch mendapat izin eksplisit.
    admin_csrf = _as_admin(client, fresh_db)
    created = client.post(
        "/api/channels",
        json={
            "name": "webhook-env-admin",
            "type": "webhook",
            "config": {"url": PRIVATE_HTTPS_URL, "preset": "generic", "secret": "x"},
        },
        headers={"X-CSRF-Token": admin_csrf},
    )
    assert created.status_code == 201, created.get_json()
    channel_id = created.get_json()["id"]
    assert fresh_db.channel_config(channel_id)["allow_private"] is True

    created_smtp = client.post(
        "/api/channels",
        json={
            "name": "smtp-env-admin",
            "type": "smtp",
            "config": {
                "host": "127.0.0.1",
                "port": 2525,
                "use_tls": False,
                "from_addr": "nms@contoh.test",
                "to_addr": "noc@contoh.test",
            },
        },
        headers={"X-CSRF-Token": admin_csrf},
    )
    assert created_smtp.status_code == 201, created_smtp.get_json()
    smtp_id = created_smtp.get_json()["id"]
    assert fresh_db.channel_config(smtp_id)["allow_private"] is True

    # Target publik tidak boleh ikut dipromosikan hanya karena env aktif.
    public = client.post(
        "/api/channels",
        json={
            "name": "webhook-env-publik",
            "type": "webhook",
            "config": {
                "url": PUBLIC_WEBHOOK_URL,
                "preset": "generic",
                "secret": "x",
            },
        },
        headers={"X-CSRF-Token": admin_csrf},
    )
    assert public.status_code == 201, public.get_json()
    assert fresh_db.channel_config(public.get_json()["id"])["allow_private"] is False

    # Admin yang secara eksplisit menolak target privat tetap ditolak.
    explicit_false = client.post(
        "/api/channels",
        json={
            "name": "webhook-env-tolak-privat",
            "type": "webhook",
            "config": {
                "url": PRIVATE_HTTPS_URL,
                "preset": "generic",
                "secret": "x",
                "allow_private": False,
            },
        },
        headers={"X-CSRF-Token": admin_csrf},
    )
    assert explicit_false.status_code == 400
    assert "URL ditolak" in explicit_false.get_json()["error"]

    calls = []
    monkeypatch.setattr(
        alerts,
        "send_test",
        lambda channel: calls.append(channel) or (True, "terkirim"),
    )
    for channel_id_for_test in (channel_id, smtp_id):
        forbidden = operator_client.post(
            f"/api/channels/{channel_id_for_test}/test",
            headers={"X-CSRF-Token": operator_csrf},
        )
        assert forbidden.status_code == 403
    assert calls == []


def test_operator_cannot_scan_internal_port_5500(client, fresh_db, monkeypatch):
    _no_connect(monkeypatch)
    csrf = _as_operator(client, fresh_db)

    webhook_cases = (
        (
            {
                "url": "http://127.0.0.1:5500/scan",
                "preset": "generic",
                "secret": "x",
                "allow_private": True,
                "allow_http": True,
            },
            "Hanya admin",
        ),
        (
            {
                "url": "https://127.0.0.1:5500/scan",
                "preset": "generic",
                "secret": "x",
            },
            "URL ditolak",
        ),
        (
            {
                "url": "https://[::1]:5500/scan",
                "preset": "generic",
                "secret": "x",
            },
            "URL ditolak",
        ),
    )
    for index, (config, expected) in enumerate(webhook_cases):
        response = client.post(
            "/api/channels",
            json={
                "name": f"webhook-port-5500-{index}",
                "type": "webhook",
                "config": config,
            },
            headers={"X-CSRF-Token": csrf},
        )
        assert response.status_code == 400, (index, response.get_json())
        assert expected in response.get_json()["error"]
        assert "127.0.0.1" not in response.get_data(as_text=True)
        assert "::1" not in response.get_data(as_text=True)

    smtp_base = {
        "port": 5500,
        "use_tls": False,
        "from_addr": "nms@contoh.test",
        "to_addr": "noc@contoh.test",
    }
    smtp_cases = (
        ({"host": "127.0.0.1"}, "Host SMTP ditolak"),
        ({"host": "127.0.0.1", "allow_private": True}, "Hanya admin"),
        ({"host": "10.20.30.40"}, "Host SMTP ditolak"),
        ({"host": "169.254.169.254"}, "Host SMTP ditolak"),
        ({"host": "::1"}, "Host SMTP ditolak"),
    )
    for index, (config, expected) in enumerate(smtp_cases):
        response = client.post(
            "/api/channels",
            json={
                "name": f"smtp-port-5500-{index}",
                "type": "smtp",
                "config": {**smtp_base, **config},
            },
            headers={"X-CSRF-Token": csrf},
        )
        assert response.status_code == 400, (index, response.get_json())
        assert expected in response.get_json()["error"]
        body = response.get_data(as_text=True)
        for leaked in ("127.0.0.1", "10.20.30.40", "169.254.169.254", "::1"):
            assert leaked not in body

    assert fresh_db.list_channels() == []


def test_operator_test_endpoint_blocks_dns_rebinding_to_internal(
    client, fresh_db, monkeypatch
):
    # Dua penyimpanan awal (webhook & SMTP) melihat DNS publik; saat /test,
    # DNS berubah ke loopback. Tidak boleh ada koneksi dan detail tidak boleh
    # membocorkan alamat internal.
    dns_calls = _fake_dns(
        monkeypatch,
        lambda index: PUBLIC_IP if index in (1, 2) else "127.0.0.1",
    )
    connects = _record_failed_connect(monkeypatch)
    monkeypatch.setattr(alerts, "RETRY_DELAY_SECONDS", 0)
    csrf = _as_operator(client, fresh_db)

    webhook_response = client.post(
        "/api/channels",
        json={
            "name": "webhook-rebind",
            "type": "webhook",
            "config": {
                "url": "https://rebind-web.test/hook",
                "preset": "generic",
                "secret": "x",
            },
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert webhook_response.status_code == 201, webhook_response.get_json()
    webhook_id = webhook_response.get_json()["id"]

    smtp_response = client.post(
        "/api/channels",
        json={
            "name": "smtp-rebind",
            "type": "smtp",
            "config": {
                "host": "smtp.rebind.test",
                "port": 587,
                "use_tls": False,
                "from_addr": "nms@contoh.test",
                "to_addr": "noc@contoh.test",
            },
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert smtp_response.status_code == 201, smtp_response.get_json()
    smtp_id = smtp_response.get_json()["id"]

    webhook_test = client.post(
        f"/api/channels/{webhook_id}/test",
        headers={"X-CSRF-Token": csrf},
    )
    assert webhook_test.status_code == 200, webhook_test.get_json()
    assert webhook_test.get_json()["ok"] is False
    assert "Target bukan IP publik" in webhook_test.get_json()["detail"]
    assert "127.0.0.1" not in webhook_test.get_data(as_text=True)

    smtp_test = client.post(
        f"/api/channels/{smtp_id}/test",
        headers={"X-CSRF-Token": csrf},
    )
    assert smtp_test.status_code == 200, smtp_test.get_json()
    assert smtp_test.get_json()["ok"] is False
    assert "Target SMTP ditolak" in smtp_test.get_json()["detail"]
    assert "Target bukan IP publik" in smtp_test.get_json()["detail"]
    assert "127.0.0.1" not in smtp_test.get_data(as_text=True)

    assert connects == []
    assert len(dns_calls) >= 4
    assert dns_calls[0][0] == "rebind-web.test"
    assert dns_calls[1][0] == "smtp.rebind.test"


def test_netguard_blocks_internal_ranges_and_bad_hosts():
    for address in (
        "127.0.0.1",
        "10.0.0.1",
        "172.16.0.1",
        "192.168.1.1",
        "169.254.169.254",
        "100.64.0.1",
        "0.0.0.0",
        "::1",
        "fe80::1",
        "fec0::1",
        "fc00::1",
        "::ffff:127.0.0.1",
    ):
        with pytest.raises(ValueError):
            netguard.resolve_addresses(address, 80)

    for host in ("", "127.0.0.1:80", "user@host", "http://host", "host/path", "bad host"):
        with pytest.raises(ValueError):
            netguard.normalize_host(host)

    assert netguard.normalize_host("[::1]") == "::1"
    assert netguard.as_bool("false") is False
    assert netguard.as_bool("on") is True
