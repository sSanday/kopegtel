"""E2-A — webhook: HMAC, SSRF, retry, penyembunyian secret di API."""

import hashlib
import hmac
import json
import os
import sys
import time
from urllib import error as urlerror

import pytest
from werkzeug.security import generate_password_hash

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, "backend"))

import app as app_module  # noqa: E402
import webhook  # noqa: E402


@pytest.fixture()
def client():
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as client:
        yield client


@pytest.fixture(autouse=True)
def _reset_storm():
    import alerts

    alerts._STORM["entries"].clear()
    alerts._STORM["active_until"] = 0.0
    yield
    alerts._STORM["entries"].clear()
    alerts._STORM["active_until"] = 0.0


def _mk_user(db, username, password, role="admin"):
    db.create_user(username, generate_password_hash(password), role=role, must_change_password=0)


def _login(client, username, password):
    return client.post("/api/login", json={"username": username, "password": password})


def _public_dns(monkeypatch):
    class _FakeSocket:
        IPPROTO_TCP = 6

        @staticmethod
        def getaddrinfo(host, port, proto=None):
            return [(2, 1, 6, "", ("93.184.216.34", port or 443))]

    monkeypatch.setattr(webhook, "socket", _FakeSocket)


# ---------------------------------------------------------
# Unit: validasi target
# ---------------------------------------------------------

def test_http_url_rejected_by_default(monkeypatch):
    with pytest.raises(ValueError):
        webhook.validate_webhook_target("http://contoh.com/hook", allow_http=False)


def test_private_target_rejected(monkeypatch):
    def _fake_dns(host, port, proto=None):
        return [(2, 1, 6, "", ("192.168.1.10", port or 443))]

    class _FakeSocket:
        IPPROTO_TCP = 6

        @staticmethod
        def getaddrinfo(host, port, proto=None):
            return _fake_dns(host, port, proto)

    monkeypatch.setattr(webhook, "socket", _FakeSocket)
    with pytest.raises(ValueError, match="publik"):
        webhook.validate_webhook_target("https://intranet.local/hook")


def test_loopback_hostname_rejected():
    with pytest.raises(ValueError):
        webhook.validate_webhook_target("https://localhost/hook")


def test_allow_private_env_allows_private_target(monkeypatch):
    monkeypatch.setenv("ALLOW_PRIVATE_WEBHOOKS", "1")
    assert webhook.validate_webhook_target("https://192.168.1.10/hook") is not None


def test_link_local_rejected(monkeypatch):
    class _FakeSocket:
        IPPROTO_TCP = 6

        @staticmethod
        def getaddrinfo(host, port, proto=None):
            return [(2, 1, 6, "", ("169.254.10.1", port or 80))]

    monkeypatch.setattr(webhook, "socket", _FakeSocket)
    with pytest.raises(ValueError):
        webhook.validate_webhook_target("https://linklocal/hook")


def test_url_with_credentials_rejected():
    with pytest.raises(ValueError):
        webhook.validate_webhook_target("https://user:pass@contoh.com/hook")


# ---------------------------------------------------------
# Unit: pengiriman + tanda tangan
# ---------------------------------------------------------

class _FakeResponse:
    def __init__(self, status=200, body=b"{\"ok\": true}"):
        self.status = status
        self._body = body

    def read(self, n=-1):
        return self._body[: n if n and n > 0 else len(self._body)]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _FakeOpener:
    def __init__(self, behavior):
        self.behavior = behavior
        self.requests = []

    def open(self, request, timeout=0):
        self.requests.append(request)
        outcome = self.behavior(request) if callable(self.behavior) else self.behavior
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _patch_opener(monkeypatch, behavior):
    opener = _FakeOpener(behavior)

    class _FakeUrlRequest:
        HTTPRedirectHandler = webhook.urlrequest.HTTPRedirectHandler
        HTTPError = webhook.urlrequest.HTTPError
        URLError = webhook.urlrequest.URLError
        Request = webhook.urlrequest.Request
        ProxyHandler = webhook.urlrequest.ProxyHandler

        @staticmethod
        def build_opener(*handlers):
            return opener

    monkeypatch.setattr(webhook, "urlrequest", _FakeUrlRequest)
    return opener


def _config(**overrides):
    base = {
        "url": "https://hooks.contoh.test/services/T123",
        "secret": "rahasia-webhook-0123456789abcdef",
        "preset": "generic",
        "allow_http": False,
        "allow_private": False,
        "headers": {"Authorization": "Bearer token-uji"},
        "max_response_bytes": 1024,
    }
    base.update(overrides)
    return base


def test_send_webhook_success_signs_body(monkeypatch):
    _public_dns(monkeypatch)
    opener = _patch_opener(monkeypatch, _FakeResponse(200))
    config = _config()
    ok, detail, attempts = webhook.send_webhook(config, "Judul uji", "Isi uji",
                                                problem={"severity": "high", "kind": "device_down",
                                                         "problem_id": 42, "device_id": 7})
    assert ok is True
    assert attempts == 1
    assert len(opener.requests) == 1
    request = opener.requests[0]
    body = request.data
    timestamp = request.headers["X-nms-timestamp"]
    signature = request.headers["X-nms-signature"]
    expected = hmac.new(config["secret"].encode(), f"{timestamp}.".encode() + body,
                        hashlib.sha256).hexdigest()
    assert signature == f"sha256={expected}"
    assert abs(int(timestamp) - int(time.time())) < 60
    payload = json.loads(body.decode("utf-8"))
    assert payload["subject"] == "Judul uji"
    assert payload["severity"] == "high"
    assert payload["problem_id"] == 42
    assert request.headers["Authorization"] == "Bearer token-uji"


def test_send_webhook_slack_and_discord_payloads(monkeypatch):
    _public_dns(monkeypatch)
    for preset, key in (("slack", "text"), ("discord", "content")):
        opener = _patch_opener(monkeypatch, _FakeResponse(200))
        ok, _, _ = webhook.send_webhook(_config(preset=preset, headers={}), "S", "Isi")
        assert ok
        payload = json.loads(opener.requests[0].data.decode("utf-8"))
        assert key in payload
        assert payload[key] == "S\nIsi"


def test_send_webhook_http_requires_allow_http(monkeypatch):
    _public_dns(monkeypatch)
    ok, detail, attempts = webhook.send_webhook(_config(url="http://hooks.contoh.test/x"), "S", "T")
    assert ok is False
    assert "http" in detail.lower()
    _patch_opener(monkeypatch, _FakeResponse(200))
    ok, _, _ = webhook.send_webhook(_config(url="http://hooks.contoh.test/x", allow_http=True), "S", "T")
    assert ok is True


def test_send_webhook_does_not_follow_redirects(monkeypatch):
    _public_dns(monkeypatch)

    def _redirect_raise(request):
        raise urlerror.HTTPError(
            url=request.full_url, code=301, msg="Moved", hdrs=None, fp=None
        )

    _patch_opener(monkeypatch, _redirect_raise)
    ok, detail, attempts = webhook.send_webhook(_config(), "S", "T")
    assert ok is False
    assert "301" in detail
    assert attempts == 3


def test_send_webhook_retries_on_failure(monkeypatch):
    _public_dns(monkeypatch)
    calls = {"n": 0}

    def _always_500(request):
        calls["n"] += 1
        raise urlerror.HTTPError(url=request.full_url, code=500, msg="err", hdrs=None, fp=None)

    _patch_opener(monkeypatch, _always_500)
    ok, detail, attempts = webhook.send_webhook(_config(), "S", "T")
    assert ok is False
    assert attempts == 3
    assert calls["n"] == 3
    assert "500" in detail


def test_send_webhook_timeout(monkeypatch):
    _public_dns(monkeypatch)

    def _timeout(request):
        raise TimeoutError()

    _patch_opener(monkeypatch, _timeout)
    ok, detail, attempts = webhook.send_webhook(_config(), "S", "T")
    assert ok is False
    assert "timeout" in detail.lower()
    assert attempts == 3


def test_send_webhook_blocks_x_nms_override(monkeypatch):
    _public_dns(monkeypatch)
    opener = _patch_opener(monkeypatch, _FakeResponse(200))
    config = _config(headers={"X-NMS-Signature": "palsu", "Host": "jahat.test", "X-Custom": "oke"})
    ok, _, _ = webhook.send_webhook(config, "S", "T")
    assert ok
    headers = {k.lower(): v for k, v in opener.requests[0].headers.items()}
    assert headers["x-nms-signature"] != "palsu"
    assert headers.get("host") != "jahat.test"
    assert headers["x-custom"] == "oke"


def test_response_body_capped(monkeypatch):
    _public_dns(monkeypatch)
    read_sizes = []

    class _HugeResponse(_FakeResponse):
        def read(self, n=-1):
            read_sizes.append(n)
            return b"x" * (n or 10)

    _patch_opener(monkeypatch, _HugeResponse(200))
    webhook.send_webhook(_config(max_response_bytes=512), "S", "T")
    assert read_sizes and max(read_sizes) <= 512 + 1


# ---------------------------------------------------------
# API: buat/edit kanal webhook, uji kirim, action_log
# ---------------------------------------------------------

def test_channel_api_hides_secret_and_audit(client, fresh_db, monkeypatch):
    _public_dns(monkeypatch)
    _mk_user(fresh_db, "wh-admin", "webhook-admin-password", role="admin")
    r = _login(client, "wh-admin", "webhook-admin-password")
    csrf = r.get_json()["csrf_token"]
    response = client.post(
        "/api/channels",
        json={
            "name": "webhook-uji",
            "type": "webhook",
            "enabled": True,
            "config": {
                "url": "https://hooks.contoh.test/services/abc",
                "preset": "slack",
                "secret": "rahasia-kanal-0123456789abcdef",
                "headers": {"Authorization": "Bearer x"},
            },
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 201, response.get_json()
    channel_id = response.get_json()["id"]

    listed = client.get("/api/channels").get_json()["channels"]
    ours = next(ch for ch in listed if ch["id"] == channel_id)
    assert "secret" not in ours["config"]
    assert ours["config"]["has_secret"] is True
    assert ours["config"]["headers"] == {"Authorization": None}
    assert ours["config"]["has_headers"] is True

    # Edit hanya nama: secret & header lama dipertahankan.
    updated = client.put(
        f"/api/channels/{channel_id}",
        json={"name": "webhook-uji-ubah", "type": "webhook", "enabled": True,
              "config": {"url": "https://hooks.contoh.test/services/abc", "preset": "slack",
                         "secret": "", "headers": {}}},
        headers={"X-CSRF-Token": csrf},
    )
    assert updated.status_code == 200, updated.get_json()
    with fresh_db._lock:
        cfg = fresh_db.channel_config(channel_id)
    assert cfg["secret"] == "rahasia-kanal-0123456789abcdef"
    assert cfg["headers"] == {"Authorization": "Bearer x"}

    with fresh_db._lock:
        audit = fresh_db.get_conn().execute(
            "SELECT action FROM audit_log WHERE action IN ('channel.create', 'channel.update')"
        ).fetchall()
    actions = {row["action"] for row in audit}
    assert {"channel.create", "channel.update"} <= actions


def test_channel_api_rejects_bad_webhook_target(client, fresh_db, monkeypatch):
    _public_dns(monkeypatch)
    _mk_user(fresh_db, "wh-reject", "webhook-reject-password", role="admin")
    r = _login(client, "wh-reject", "webhook-reject-password")
    csrf = r.get_json()["csrf_token"]
    response = client.post(
        "/api/channels",
        json={
            "name": "webhook-loopback",
            "type": "webhook",
            "config": {"url": "https://localhost/hook", "preset": "generic", "secret": "x"},
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 400


def test_action_log_does_not_leak_secret(client, fresh_db, monkeypatch):
    _public_dns(monkeypatch)
    _patch_opener(monkeypatch, _FakeResponse(200, b"{\"ok\":true}"))
    device_id = fresh_db.create_device({
        "name": "wh-dev", "ip": "192.0.2.71", "type": "mikrotik", "location": "",
        "poll_interval": 60, "group_id": None, "parent_id": None, "maintenance": False,
        "snmp_enabled": False, "snmp_version": "v2c", "snmp_port": 161,
        "snmp_v3_auth_proto": "SHA", "snmp_v3_priv_proto": "AES",
        "mikrotik_enabled": False, "mikrotik_port": 8728,
        "mikrotik_user": None, "mikrotik_password": None, "mikrotik_ssl": False,
    })
    channel_id = fresh_db.create_channel(
        "wh-log", "webhook",
        {"url": "https://hooks.contoh.test/x", "preset": "generic",
         "secret": "rahasia-jangan-bocor-0123456789", "headers": {}, "max_response_bytes": 512},
        True,
    )
    template_id = fresh_db.create_template("tpl-uji", "Problem {perangkat}: {pesan}")
    rule_id = fresh_db.create_rule({
        "name": "rule-uji", "min_severity": "information",
        "channel_id": channel_id, "template_id": template_id,
        "group_id": None, "device_id": None, "enabled": True,
    })
    problem = {
        "id": 1, "device_id": device_id, "kind": "device_down",
        "severity": "high", "message": "ping timeout", "started_at": "2026-11-10T20:00:00Z",
    }
    import alerts

    alerts.dispatch_problem(problem)
    with fresh_db._lock:
        rows = fresh_db.get_conn().execute(
            "SELECT status, detail FROM action_log ORDER BY id DESC LIMIT 5"
        ).fetchall()
    assert rows, "action_log kosong"
    for row in rows:
        assert "rahasia-jangan-bocor-0123456789" not in row["detail"]
    assert any(row["status"] == "sent" for row in rows)
