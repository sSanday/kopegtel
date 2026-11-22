"""Regresi H-2: kanal notifikasi tidak boleh membocorkan URL/kredensial ke viewer.

URL webhook Slack/Discord dapat dipakai siapa pun untuk memposting alert palsu,
jadi URL itu diperlakukan sebagai kredensial. Host/username SMTP dan chat ID
juga tidak dikembalikan oleh API; klien hanya menerima penanda ``has_*``.
"""

import os
import sys

import pytest
from werkzeug.security import generate_password_hash

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, "backend"))

import app as app_module  # noqa: E402
from routes import alerts as alerts_module  # noqa: E402

OPERATOR = "operator-h2"
OPERATOR_PASSWORD = "operator-h2-password"
VIEWER = "viewer-h2"
VIEWER_PASSWORD = "viewer-h2-password"

WEBHOOK_URL = "https://hooks.slack.com/services/T000/B000/rahasia-url-h2"
WEBHOOK_SECRET = "rahasia-hmac-h2-0123456789abcdef"
WEBHOOK_HEADER_VALUE = "Bearer rahasia-header-h2"
TELEGRAM_TOKEN = "123456:rahasia-bot-token-h2"
TELEGRAM_CHAT_ID = "-1009876543210"
SMTP_HOST = "smtp.internal.kopegtel.test"
SMTP_USERNAME = "rahasia-smtp-user-h2"
SMTP_PASSWORD = "rahasia-smtp-password-h2"


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


def _channels(fresh_db):
    webhook_id = fresh_db.create_channel(
        "Webhook H-2",
        "webhook",
        {
            "url": WEBHOOK_URL,
            "preset": "slack",
            "secret": WEBHOOK_SECRET,
            "headers": {"Authorization": WEBHOOK_HEADER_VALUE},
            "allow_http": False,
            "allow_private": False,
            "max_response_bytes": 1024,
        },
        True,
    )
    telegram_id = fresh_db.create_channel(
        "Telegram H-2",
        "telegram",
        {"bot_token": TELEGRAM_TOKEN, "chat_id": TELEGRAM_CHAT_ID},
        True,
    )
    smtp_id = fresh_db.create_channel(
        "SMTP H-2",
        "smtp",
        {
            "host": SMTP_HOST,
            "port": 587,
            "use_tls": True,
            "username": SMTP_USERNAME,
            "password": SMTP_PASSWORD,
            "from_addr": "nms@contoh.test",
            "to_addr": "noc@contoh.test",
        },
        True,
    )
    return webhook_id, telegram_id, smtp_id


def test_viewer_cannot_list_channels(client, fresh_db):
    _channels(fresh_db)
    _mk_user(fresh_db, VIEWER, VIEWER_PASSWORD, role="viewer")
    _login(client, VIEWER, VIEWER_PASSWORD)

    response = client.get("/api/channels")

    assert response.status_code == 403
    assert response.get_json()["code"] == "forbidden"


def test_channel_list_never_returns_webhook_url_or_other_sensitive_config(client, fresh_db):
    _channels(fresh_db)
    _mk_user(fresh_db, OPERATOR, OPERATOR_PASSWORD, role="operator")
    _login(client, OPERATOR, OPERATOR_PASSWORD)

    response = client.get("/api/channels")

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    for secret in (
        WEBHOOK_URL,
        WEBHOOK_SECRET,
        WEBHOOK_HEADER_VALUE,
        TELEGRAM_TOKEN,
        TELEGRAM_CHAT_ID,
        SMTP_HOST,
        SMTP_USERNAME,
        SMTP_PASSWORD,
    ):
        assert secret not in body

    configs = {channel["type"]: channel["config"] for channel in response.get_json()["channels"]}
    webhook = configs["webhook"]
    assert "url" not in webhook
    assert "secret" not in webhook
    assert webhook["has_url"] is True
    assert webhook["has_secret"] is True
    assert webhook["has_headers"] is True
    assert webhook["headers"] == {"Authorization": None}

    telegram = configs["telegram"]
    assert "bot_token" not in telegram
    assert "chat_id" not in telegram
    assert telegram["has_bot_token"] is True
    assert telegram["has_chat_id"] is True

    smtp = configs["smtp"]
    assert "host" not in smtp
    assert "username" not in smtp
    assert "password" not in smtp
    assert smtp["has_host"] is True
    assert smtp["has_username"] is True
    assert smtp["has_password"] is True


def test_webhook_edit_with_blank_url_keeps_stored_target(client, fresh_db):
    channel_id = _channels(fresh_db)[0]
    _mk_user(fresh_db, OPERATOR, OPERATOR_PASSWORD, role="operator")
    csrf = _login(client, OPERATOR, OPERATOR_PASSWORD)

    response = client.put(
        f"/api/channels/{channel_id}",
        json={
            "name": "Webhook H-2 diubah",
            "type": "webhook",
            "enabled": True,
            "config": {
                "url": "",
                "preset": "slack",
                "secret": "",
                "allow_http": False,
                "allow_private": False,
            },
        },
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 200, response.get_json()
    config = fresh_db.channel_config(channel_id)
    assert config["url"] == WEBHOOK_URL
    assert config["secret"] == WEBHOOK_SECRET
    assert config["headers"] == {"Authorization": WEBHOOK_HEADER_VALUE}


def test_smtp_and_telegram_edit_with_blank_fields_keep_stored_values(client, fresh_db):
    _webhook_id, telegram_id, smtp_id = _channels(fresh_db)
    _mk_user(fresh_db, OPERATOR, OPERATOR_PASSWORD, role="operator")
    csrf = _login(client, OPERATOR, OPERATOR_PASSWORD)

    smtp_response = client.put(
        f"/api/channels/{smtp_id}",
        json={
            "name": "SMTP H-2 diubah",
            "type": "smtp",
            "enabled": True,
            "config": {
                "host": "",
                "port": 587,
                "use_tls": True,
                "username": "",
                "password": "",
                "from_addr": "nms@contoh.test",
                "to_addr": "noc@contoh.test",
            },
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert smtp_response.status_code == 200, smtp_response.get_json()
    smtp_config = fresh_db.channel_config(smtp_id)
    assert smtp_config["host"] == SMTP_HOST
    assert smtp_config["username"] == SMTP_USERNAME
    assert smtp_config["password"] == SMTP_PASSWORD

    telegram_response = client.put(
        f"/api/channels/{telegram_id}",
        json={
            "name": "Telegram H-2 diubah",
            "type": "telegram",
            "enabled": True,
            "config": {"bot_token": "", "chat_id": ""},
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert telegram_response.status_code == 200, telegram_response.get_json()
    telegram_config = fresh_db.channel_config(telegram_id)
    assert telegram_config["bot_token"] == TELEGRAM_TOKEN
    assert telegram_config["chat_id"] == TELEGRAM_CHAT_ID


def test_viewer_cannot_trigger_channel_test(client, fresh_db, monkeypatch):
    channel_id = _channels(fresh_db)[0]
    _mk_user(fresh_db, VIEWER, VIEWER_PASSWORD, role="viewer")
    csrf = _login(client, VIEWER, VIEWER_PASSWORD)
    calls = []
    monkeypatch.setattr(
        alerts_module.alerts,
        "send_test",
        lambda channel: calls.append(channel) or (True, "terkirim"),
    )

    response = client.post(
        f"/api/channels/{channel_id}/test",
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 403
    assert calls == []
