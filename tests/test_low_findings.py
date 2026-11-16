"""L-1..L-9: regresi untuk temuan severity Low (keamanan & ketahanan input).

L-1  login tidak boleh membedakan akun tidak ada vs password salah, dan
     kebijakan MFA yang memblokir login tidak boleh membalas 403 mfa_required
     (oracle "password benar").
L-2  Input angka non-terhingga (NaN/Infinity dari JSON) tidak boleh memicu
     HTTP 500 atau lolos validasi.
L-3  Token API dengan expires_at rusak diperlakukan kedaluwarsa (bukan 500).
L-4  RateLimiter /metrics benar-benar 30 scrape per menit per IP; dict
     per-sumber dibersihkan.
L-5  Scan CIDR tidak boleh dibiarkan "running" selamanya bila gagal; jumlah
     scan paralel dibatasi; kredensial RouterOS tidak disemprotkan ke subnet.
L-6  Ping: balasan "Destination host unreachable" dengan rc=0 bukan reachable.
L-7  Galat mentah (URL Telegram, detail RouterOS) tidak dikirim ke klien.
L-8  PORT/env angka tidak crash; variabel env terdokumentasi di .env.example.
L-9  Boolean dari string ("0"/"false") tidak dianggap True pada API settings.
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone

import pytest
from werkzeug.security import generate_password_hash

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, "backend"))

import app as app_module  # noqa: E402
import db  # noqa: E402
import alerts  # noqa: E402
from routes import discovery as discovery_module  # noqa: E402
from ingest.service import RateLimiter  # noqa: E402
from collectors import ping as ping_collector  # noqa: E402


@pytest.fixture()
def client():
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as client:
        yield client


def _mk_user(fresh_db, username, password, role="admin"):
    fresh_db.create_user(
        username, generate_password_hash(password), role=role, must_change_password=0
    )


def _login(client, username, password):
    return client.post("/api/login", json={"username": username, "password": password})


def _csrf(client, username, password):
    response = _login(client, username, password)
    assert response.status_code == 200, response.get_json()
    return response.get_json()["csrf_token"]


# ---------------------------------------------------------
# L-1
# ---------------------------------------------------------

def test_login_unknown_user_matches_wrong_password(client, fresh_db):
    _mk_user(fresh_db, "l1-user", "l1-user-password")
    wrong = _login(client, "l1-user", "password-yang-salah")
    unknown = _login(client, "user-yang-tidak-ada", "password-yang-salah")
    assert wrong.status_code == 401
    assert unknown.status_code == 401
    # Respons harus persis sama (tidak membedakan akun ada vs tidak ada).
    assert wrong.get_json() == unknown.get_json()


def test_login_policy_blocked_is_indistinguishable(client, fresh_db):
    _mk_user(fresh_db, "l1-policy", "l1-policy-password", role="admin")
    old = datetime.now(timezone.utc).timestamp() - 30 * 86400
    started = datetime.fromtimestamp(old, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    fresh_db.set_settings({
        "mfa_required_roles": json.dumps(["admin"]),
        "mfa_grace_days": "7",
        "mfa_policy_started_at": started,
    })
    response = _login(client, "l1-policy", "l1-policy-password")
    assert response.status_code == 401
    assert response.get_json()["error"] == "Nama pengguna atau kata sandi salah."
    assert "mfa_required" not in response.get_data(as_text=True)


# ---------------------------------------------------------
# L-2
# ---------------------------------------------------------

def test_settings_reject_infinity_and_nan(client, fresh_db):
    _mk_user(fresh_db, "l2-admin", "l2-admin-password", role="admin")
    csrf = _csrf(client, "l2-admin", "l2-admin-password")
    for payload in (
        {"default_poll_interval": float("inf")},
        {"default_poll_interval": float("nan")},
        {"default_poll_interval": 10**30},
        {"idle_timeout_minutes": float("-inf")},
    ):
        response = client.put("/api/settings", json=payload, headers={"X-CSRF-Token": csrf})
        assert response.status_code == 400, (payload, response.status_code)
        assert response.get_json().get("error")


def test_service_sla_target_rejects_nan_and_inf(client, fresh_db):
    _mk_user(fresh_db, "l2-svc", "l2-svc-password", role="admin")
    csrf = _csrf(client, "l2-svc", "l2-svc-password")
    for sla in (float("nan"), float("inf"), float("-inf")):
        response = client.post(
            "/api/services",
            json={"name": f"svc-{sla}", "sla_target": sla},
            headers={"X-CSRF-Token": csrf},
        )
        assert response.status_code == 400, response.get_json()


def test_device_test_rejects_non_numeric_id(client, fresh_db):
    _mk_user(fresh_db, "l2-dev", "l2-dev-password", role="admin")
    csrf = _csrf(client, "l2-dev", "l2-dev-password")
    for bad_id in (float("inf"), float("nan"), "not-a-number", 1e999):
        response = client.post(
            "/api/devices/test",
            json={"id": bad_id},
            headers={"X-CSRF-Token": csrf},
        )
        assert response.status_code == 400, (bad_id, response.status_code)


def test_service_device_ids_reject_non_numeric(client, fresh_db):
    _mk_user(fresh_db, "l2-dev2", "l2-dev2-password", role="admin")
    csrf = _csrf(client, "l2-dev2", "l2-dev2-password")
    response = client.post(
        "/api/services",
        json={"name": "svc-l2"},
        headers={"X-CSRF-Token": csrf},
    )
    service_id = response.get_json()["service"]["id"]
    response = client.put(
        f"/api/services/{service_id}/devices",
        json={"device_ids": [float("inf")]},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 400


def test_api_token_expires_in_days_capped_and_finite(fresh_db):
    token, record = db.create_api_token("l2-token-huge", expires_in_days=10**30)
    assert token is None
    assert isinstance(record, str)
    token, record = db.create_api_token("l2-token-nan", expires_in_days=float("nan"))
    assert token is None
    assert isinstance(record, str)
    token, record = db.create_api_token("l2-token-ok", expires_in_days=30)
    assert token is not None
    assert record is not None


# ---------------------------------------------------------
# L-3
# ---------------------------------------------------------

def test_verify_api_token_with_broken_expires_at_is_rejected(fresh_db):
    token, record = db.create_api_token("l3-token", expires_in_days=30)
    assert token
    with fresh_db._lock:
        fresh_db.get_conn().execute(
            "UPDATE api_tokens SET expires_at = 'bukan-tanggal' WHERE id = ?",
            (record["id"],),
        )
        fresh_db.get_conn().commit()
    assert db.verify_api_token(token, ip="127.0.0.1") is None


# ---------------------------------------------------------
# L-4
# ---------------------------------------------------------

def test_metrics_rate_limited_to_30_per_minute_per_ip(client, fresh_db):
    _mk_user(fresh_db, "l4-admin", "l4-admin-password", role="admin")
    csrf = _csrf(client, "l4-admin", "l4-admin-password")
    created = client.post(
        "/api/metrics/tokens",
        json={"name": "l4-token"},
        headers={"X-CSRF-Token": csrf},
    )
    token = created.get_json()["token"]
    fresh_db.set_settings({"metrics_enabled": "1"})

    from routes import metrics as metrics_routes

    metrics_routes._RATE.reset()
    ip = "203.0.113.77"
    for attempt in range(30):
        response = client.get(
            "/metrics",
            headers={"Authorization": f"Bearer {token}"},
            environ_base={"REMOTE_ADDR": ip},
        )
        assert response.status_code == 200, (attempt, response.status_code)
    response = client.get(
        "/metrics",
        headers={"Authorization": f"Bearer {token}"},
        environ_base={"REMOTE_ADDR": ip},
    )
    assert response.status_code == 429
    # IP lain tidak ikut terblokir.
    other = client.get(
        "/metrics",
        headers={"Authorization": f"Bearer {token}"},
        environ_base={"REMOTE_ADDR": "203.0.113.78"},
    )
    assert other.status_code == 200
    metrics_routes._RATE.reset()


def test_rate_limiter_sweeps_stale_sources():
    limiter = RateLimiter(limit=1, window_seconds=10)
    limiter.allow("sumber-lama", now=1000.0)
    limiter._since_sweep = 512  # paksa sweep pada panggilan berikutnya
    limiter.allow("sumber-baru", now=2000.0)
    assert "sumber-lama" not in limiter._events
    assert "sumber-baru" in limiter._events


def test_rate_limiter_rejects_new_sources_when_full():
    limiter = RateLimiter(limit=1, window_seconds=10, max_sources=2)
    assert limiter.allow("a", now=1000.0) is True
    assert limiter.allow("b", now=1000.0) is True
    assert limiter.allow("c", now=1001.0) is False


# ---------------------------------------------------------
# L-5
# ---------------------------------------------------------

def test_scan_job_marked_error_when_scan_raises(fresh_db, monkeypatch):
    monkeypatch.setattr(
        discovery_module,
        "_scan_addresses",
        lambda job_id, cidr, community: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    job_id = fresh_db.create_scan_job("192.168.50.0/30", 2, "l5-op")
    # Simulasikan siklus acquire→thread secara eksplisit dan sinkron:
    # slot harus kembali terisi penuh setelah _run_scan selesai gagal.
    assert discovery_module._SCAN_JOB_SLOTS.acquire(blocking=False)  # slot untuk job
    discovery_module._run_scan(job_id, "192.168.50.0/30", None)  # gagal + release di finally
    assert discovery_module._SCAN_JOB_SLOTS.acquire(blocking=False)
    assert discovery_module._SCAN_JOB_SLOTS.acquire(blocking=False)
    discovery_module._SCAN_JOB_SLOTS.release()
    discovery_module._SCAN_JOB_SLOTS.release()
    assert fresh_db.get_scan_job(job_id)["status"] == "error"


def test_scan_slots_bounded(client, fresh_db, monkeypatch):
    _mk_user(fresh_db, "l5-op3", "l5-op3-password", role="operator")
    csrf = _csrf(client, "l5-op3", "l5-op3-password")
    monkeypatch.setattr(
        discovery_module,
        "_scan_addresses",
        lambda job_id, cidr, community: None,  # selesai seketika (tanpa thread scan asli)
    )
    # Isi kedua slot secara manual: endpoint melaporkan 429.
    assert discovery_module._SCAN_JOB_SLOTS.acquire(blocking=False)
    assert discovery_module._SCAN_JOB_SLOTS.acquire(blocking=False)
    response = client.post(
        "/api/discovery/scan",
        json={"cidr": "192.168.53.0/30"},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 429
    discovery_module._SCAN_JOB_SLOTS.release()
    discovery_module._SCAN_JOB_SLOTS.release()


def test_scan_request_ignores_routeros_credentials(client, fresh_db, monkeypatch):
    _mk_user(fresh_db, "l5-op4", "l5-op4-password", role="operator")
    csrf = _csrf(client, "l5-op4", "l5-op4-password")
    seen = {}

    def fake_run(job_id, cidr, community):
        seen.update({"community": community})
        # _run_scan asli melepas slot di finally; tiruan harus melakukan hal sama.
        discovery_module._SCAN_JOB_SLOTS.release()

    monkeypatch.setattr(discovery_module, "_run_scan", fake_run)
    response = client.post(
        "/api/discovery/scan",
        json={
            "cidr": "192.168.54.0/30",
            "community": "community-l5",
            "mikrotik_user": "attacker-kontrol",
            "mikrotik_password": "password-jangan-bocor",
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 202
    assert seen == {"community": "community-l5"}


# ---------------------------------------------------------
# L-6
# ---------------------------------------------------------

def _fake_ping(returncode, stdout):
    return subprocess.CompletedProcess(args=["ping"], returncode=returncode, stdout=stdout, stderr="")


def test_ping_windows_destination_unreachable_is_not_ok(monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: _fake_ping(
            0,
            "Pinging 192.168.30.1 with 32 bytes of data:\n"
            "Reply from 10.0.0.2: Destination host unreachable.\n",
        ),
    )
    result = ping_collector.check_ping("192.168.30.1")
    assert result["ok"] is False


def test_ping_windows_request_timed_out_is_not_ok(monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: _fake_ping(1, "Request timed out.\n"),
    )
    result = ping_collector.check_ping("192.168.30.1")
    assert result["ok"] is False


def test_ping_windows_reply_is_ok(monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: _fake_ping(
            0,
            "Reply from 192.168.30.1: bytes=32 time<1ms TTL=64\n",
        ),
    )
    result = ping_collector.check_ping("192.168.30.1")
    assert result["ok"] is True
    assert result["latency_ms"] == pytest.approx(1.0)  # "time<1ms"


def test_ping_linux_reply_is_ok(monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: _fake_ping(
            0,
            "64 bytes from 192.168.30.1: icmp_seq=1 ttl=64 time=0.5 ms\n",
        ),
    )
    result = ping_collector.check_ping("192.168.30.1")
    assert result["ok"] is True
    assert result["latency_ms"] == pytest.approx(0.5)


# L-6: ping multi-bahasa. Tiap locale harus mengenali baris "balasan"-nya
# (ok) dan baris gagalnya; locale yang tidak dikenal tetap fail-safe (down).
LOCALE_REPLY_OUTPUTS = {
    "de": "Antwort von 192.168.30.1: Bytes=32 Zeit<1ms TTL=64\n",
    "fr": "Réponse de 192.168.30.1 : octets=32 temps<1ms TTL=64\n",
    "es": "Respuesta desde 192.168.30.1: bytes=32 tiempo<1ms TTL=64\n",
    "pt": "Resposta de 192.168.30.1: bytes=32 tempo<1ms TTL=64\n",
    "it": "Risposta da 192.168.30.1: byte=32 durata<1ms TTL=64\n",
    "nl": "Antwoord van 192.168.30.1: bytes=32 tijd<1ms TTL=64\n",
    "pl": "Odpowiedź z 192.168.30.1: bajty=32 czas<1ms TTL=64\n",
    "ru": "Ответ от 192.168.30.1: байт=32 время<1мс TTL=64\n",
    "ja": "192.168.30.1 からの応答: バイト数 = 32 時間<1ms TTL=64\n",
    "zh": "来自 192.168.30.1 的回复: 字节=32 时间<1ms TTL=64\n",
}
LOCALE_FAILURE_OUTPUTS = {
    # rc == 0 walau gagal (kasus "Destination host unreachable" lokal)
    "de": "Antwort von 10.0.0.2: Zielhost nicht erreichbar.\n",
    "fr": "Réponse de 10.0.0.2 : Impossible de joindre l'hôte de destination.\n",
    "es": "Respuesta desde 10.0.0.2: Host de destino inaccesible.\n",
    "pt": "Resposta de 10.0.0.2: Host de destino inacessível.\n",
    "it": "Risposta da 10.0.0.2: Host di destinazione non raggiungibile.\n",
    "nl": "Antwoord van 10.0.0.2: Doelhost onbereikbaar.\n",
    "pl": "Odpowiedź z 10.0.0.2: Host docelowy nie jest dostępny.\n",
    "ru": "Ответ от 10.0.0.2: Узел назначения недоступен.\n",
    "ja": "192.168.30.1 からの応答: 宛先ホストに到達できません。\n",
    "zh": "来自 192.168.30.1 的回复: 无法访问目标主机。\n",
}


@pytest.mark.parametrize("locale", sorted(LOCALE_REPLY_OUTPUTS))
def test_ping_locale_reply_is_ok(monkeypatch, locale):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: _fake_ping(0, LOCALE_REPLY_OUTPUTS[locale]),
    )
    result = ping_collector.check_ping("192.168.30.1")
    assert result["ok"] is True
    assert result["latency_ms"] == pytest.approx(1.0)


@pytest.mark.parametrize("locale", sorted(LOCALE_FAILURE_OUTPUTS))
def test_ping_locale_failure_with_rc0_is_not_ok(monkeypatch, locale):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: _fake_ping(0, LOCALE_FAILURE_OUTPUTS[locale]),
    )
    result = ping_collector.check_ping("192.168.30.1")
    assert result["ok"] is False


@pytest.mark.parametrize(
    "locale,output",
    [
        ("de", "Zeitüberschreitung der Anforderung.\n"),
        ("fr", "Le délai d'attente de la demande est dépassé.\n"),
        ("es", "Tiempo de espera agotado para esta solicitud.\n"),
        ("pt", "Esgotado o tempo limite do pedido.\n"),
        ("it", "Richiesta scaduta.\n"),
        ("nl", "Time-out bij aanvraag.\n"),
        ("pl", "Przekroczono limit czasu żądania.\n"),
        ("ru", "Превышен интервал ожидания для запроса.\n"),
        ("ja", "要求がタイムアウトしました。\n"),
        ("zh", "请求超时。\n"),
    ],
)
def test_ping_locale_timeout_is_not_ok(monkeypatch, locale, output):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: _fake_ping(1, output),
    )
    result = ping_collector.check_ping("192.168.30.1")
    assert result["ok"] is False
    assert result["reason"] == "timeout"


def test_ping_unknown_locale_is_fail_safe_down(monkeypatch):
    # Locale yang tidak dikenal: tidak ada token balasan yang cocok,
    # walau rc == 0. Harus dianggap down, bukan online.
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: _fake_ping(
            0,
            "AntwortXX 192.168.30.1: Bytes=32 Zeit<1ms TTL=64\n",
        ),
    )
    result = ping_collector.check_ping("192.168.30.1")
    assert result["ok"] is False


# ---------------------------------------------------------
# L-7
# ---------------------------------------------------------

def test_send_telegram_error_hides_bot_token(monkeypatch):
    bot_token = "123456:RAHASIA-TOKEN-TELEGRAM"

    def fake_urlopen(req, timeout=None, context=None):
        assert bot_token in req.full_url  # server-side URL memang berisi token
        raise Exception(f"Gagal: https://api.telegram.org/bot{bot_token}/sendMessage refused")

    monkeypatch.setattr(alerts.urlrequest, "urlopen", fake_urlopen)
    ok, detail = alerts.send_telegram(
        {"bot_token": bot_token, "chat_id": "1"}, "teks"
    )
    assert ok is False
    assert bot_token not in detail
    assert "api.telegram.org" not in detail


def test_send_telegram_http_error_hides_bot_token(monkeypatch):
    import urllib.error

    bot_token = "654321:TOKEN-RAHASIA-KEDUA"

    def fake_urlopen(req, timeout=None, context=None):
        raise urllib.error.HTTPError(
            req.full_url, 401, "Unauthorized", {}, None
        )

    monkeypatch.setattr(alerts.urlrequest, "urlopen", fake_urlopen)
    ok, detail = alerts.send_telegram(
        {"bot_token": bot_token, "chat_id": "1"}, "teks"
    )
    assert ok is False
    assert bot_token not in detail
    assert "401" in detail


def test_mikrotik_action_error_is_generic(client, fresh_db, monkeypatch):
    import db as db_module

    device_id = fresh_db.create_device({
        "name": "l7-mt", "ip": "10.30.0.1", "type": "mikrotik", "location": "",
        "poll_interval": 60, "group_id": None, "parent_id": None,
        "maintenance": False, "snmp_enabled": False, "snmp_version": "v2c",
        "snmp_port": 161, "snmp_v3_auth_proto": "SHA", "snmp_v3_priv_proto": "AES",
        "mikrotik_enabled": True, "mikrotik_port": 8728,
        "mikrotik_user": "api", "mikrotik_password": None, "mikrotik_ssl": False,
    })
    _mk_user(fresh_db, "l7-op", "l7-op-password", role="admin")
    csrf = _csrf(client, "l7-op", "l7-op-password")

    fresh_db.set_settings({"mt_write_actions_enabled": "1"})
    fresh_db.save_mt_device_settings(device_id, {
        "write_enabled": True, "write_user": "api-write", "write_password": "rahasia",
    })

    def fake_action(config, action, params):
        raise RuntimeError("password=rahasia-prefix-router internal 10.30.0.1 login failed")

    import app as app_module_again

    monkeypatch.setattr(app_module_again.mikrotik_modules, "perform_action", fake_action)
    response = client.post(
        f"/api/mikrotik/devices/{device_id}/actions",
        json={"action": "reboot", "confirm_name": "l7-mt"},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 500
    body = response.get_json()
    assert "10.30.0.1" not in (body.get("error") or "")
    assert "rahasia" not in (body.get("error") or "")
    assert body.get("code") == "action_failed"


# ---------------------------------------------------------
# L-8
# ---------------------------------------------------------

def test_config_errors_flag_invalid_numeric_env():
    import config

    errors = config.config_errors({
        "SECRET_KEY": "x" * 40,
        "ENCRYPTION_KEY": "9fG9f2iVfTq7Zuk5rxlb3bMaOQrVvZ3kM7yD5KBmUE8=",
        "ADMIN_PASSWORD": "password-admin-yang-aman-123",
        "PORT": "bukan-angka",
        "POLL_MAX_WORKERS": "0",
        "DEGRADED_LATENCY_MS": "nan",
    })
    assert any("PORT" in error for error in errors)
    assert any("POLL_MAX_WORKERS" in error for error in errors)
    assert any("DEGRADED_LATENCY_MS" in error for error in errors)


def test_env_int_rejects_invalid_values(monkeypatch):
    import core.env as core_env

    monkeypatch.setenv("PORT", "nan")
    with pytest.raises(ValueError):
        core_env.env_int("PORT", 5500)
    monkeypatch.setenv("PORT", "bukan-angka")
    with pytest.raises(ValueError):
        core_env.env_int("PORT", 5500)
    monkeypatch.setenv("PORT", "99999999999999999999")
    with pytest.raises(ValueError):
        core_env.env_int("PORT", 5500, maximum=65535)
    monkeypatch.setenv("PORT", "8080")
    assert core_env.env_int("PORT", 5500, maximum=65535) == 8080


def test_env_example_documents_used_vars():
    example = open(os.path.join(BASE_DIR, ".env.example"), encoding="utf-8").read()
    for name in (
        "ALLOW_PRIVATE_WEBHOOKS", "ALLOW_PRIVATE_SMTP",
        "NMS_DB_PATH", "DATABASE_PATH",
        "DB_BACKUP_ENABLED", "DB_BACKUP_DIR", "DB_BACKUP_KEEP",
        "POLL_MAX_WORKERS",
    ):
        assert name in example, name


# ---------------------------------------------------------
# L-9
# ---------------------------------------------------------

def test_settings_string_flags_parse_correctly(client, fresh_db):
    _mk_user(fresh_db, "l9-admin", "l9-admin-password", role="admin")
    csrf = _csrf(client, "l9-admin", "l9-admin-password")

    response = client.put(
        "/api/settings",
        json={"db_backup_enabled": "0", "metrics_enabled": "true",
              "mt_write_actions_enabled": "1"},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200, response.get_json()
    settings = response.get_json()["settings"]
    assert settings["db_backup_enabled"] == "0"
    assert settings["metrics_enabled"] == "1"
    assert settings["mt_write_actions_enabled"] == "1"

    response = client.put(
        "/api/settings",
        json={"db_backup_enabled": "yes"},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200
    assert response.get_json()["settings"]["db_backup_enabled"] == "1"

    response = client.put(
        "/api/settings",
        json={"db_backup_enabled": "tidak"},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200
    assert response.get_json()["settings"]["db_backup_enabled"] == "0"
