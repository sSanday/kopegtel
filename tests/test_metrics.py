"""E4 — metrik Prometheus: format, token, cache, kardinalitas, escaping."""

import os
import sys
from datetime import datetime, timezone

import pytest
from prometheus_client import parser as prom_parser
from werkzeug.security import generate_password_hash

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, "backend"))

import app as app_module  # noqa: E402
import db as db_module  # noqa: E402
import prometheus as prom  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_metrics_cache():
    prom.reset_cache()
    yield
    prom.reset_cache()


@pytest.fixture()
def client():
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as client:
        yield client


def _mk_user(db, username, password, role="admin"):
    db.create_user(username, generate_password_hash(password), role=role, must_change_password=0)


def _login(client, username, password):
    return client.post("/api/login", json={"username": username, "password": password})


def _mk_device(db, name="router-e4", ip="192.0.2.254"):
    return db.create_device({
        "name": name, "ip": ip, "type": "mikrotik", "location": "",
        "poll_interval": 60, "group_id": None, "parent_id": None,
        "maintenance": False, "snmp_enabled": False, "snmp_version": "v2c",
        "snmp_port": 161, "snmp_v3_auth_proto": "SHA", "snmp_v3_priv_proto": "AES",
        "mikrotik_enabled": False, "mikrotik_port": 8728,
        "mikrotik_user": None, "mikrotik_password": None, "mikrotik_ssl": False,
    })


def _enable_metrics(client, csrf):
    return client.put("/api/settings", json={"metrics_enabled": True, "metrics_interfaces": "important"},
                      headers={"X-CSRF-Token": csrf})


def _scrape(client, token):
    return client.get("/metrics", headers={"Authorization": f"Bearer {token}"})


# ---------------------------------------------------------
# Token: buat, list (secret hanya sekali), cabut, expires, IP
# ---------------------------------------------------------

def test_token_create_list_hides_secret(client, fresh_db):
    _mk_user(fresh_db, "metrics-admin", "metrics-admin-password", role="admin")
    r = _login(client, "metrics-admin", "metrics-admin-password")
    csrf = r.get_json()["csrf_token"]
    created = client.post("/api/metrics/tokens",
                          json={"name": "prometheus-prod", "expires_in_days": 30,
                                "allowed_ips": ["10.0.0.1"]},
                          headers={"X-CSRF-Token": csrf})
    assert created.status_code == 201
    body = created.get_json()
    assert body["token"].startswith("nms_")
    assert body["token_prefix"] == body["token"][:12]

    listed = client.get("/api/metrics/tokens").get_json()["tokens"]
    ours = next(t for t in listed if t["name"] == "prometheus-prod")
    assert "token" not in ours
    assert "token_hash" not in ours
    assert ours["token_prefix"] == body["token_prefix"]
    assert ours["allowed_ips"] == ["10.0.0.1"]
    assert ours["scopes"] == ["metrics"]


def test_token_auth_and_ip_allowlist(client, fresh_db, monkeypatch):
    _mk_user(fresh_db, "metrics-ip", "metrics-ip-password", role="admin")
    r = _login(client, "metrics-ip", "metrics-ip-password")
    csrf = r.get_json()["csrf_token"]
    created = client.post("/api/metrics/tokens",
                          json={"name": "prometheus-ip", "allowed_ips": ["10.0.0.1"]},
                          headers={"X-CSRF-Token": csrf})
    token = created.get_json()["token"]
    _enable_metrics(client, csrf)

    # IP tidak diizinkan
    response = _scrape(client, token)
    assert response.status_code == 401

    # IP diizinkan
    monkeypatch.setattr("routes.metrics.client_ip", lambda: "10.0.0.1")
    response = _scrape(client, token)
    assert response.status_code == 200


def test_token_expires_and_revoke(client, fresh_db):
    _mk_user(fresh_db, "metrics-expire", "metrics-expire-password", role="admin")
    r = _login(client, "metrics-expire", "metrics-expire-password")
    csrf = r.get_json()["csrf_token"]

    created = client.post("/api/metrics/tokens",
                          json={"name": "prometheus-expire", "expires_in_days": 1},
                          headers={"X-CSRF-Token": csrf})
    token = created.get_json()["token"]

    # Paksa kedaluwarsa: majukan waktu dengan menulis tanggal lampau.
    with fresh_db._lock:
        conn = fresh_db.get_conn()
        conn.execute(
            "UPDATE api_tokens SET expires_at='2000-01-01T00:00:00Z'"
        )
        conn.commit()
    assert fresh_db.verify_api_token(token, ip="127.0.0.1") is None

    created2 = client.post("/api/metrics/tokens",
                           json={"name": "prometheus-revoke"},
                           headers={"X-CSRF-Token": csrf})
    token2 = created2.get_json()["token"]
    token_id = next(t["id"] for t in client.get("/api/metrics/tokens").get_json()["tokens"]
                    if t["name"] == "prometheus-revoke")
    revoked = client.delete(f"/api/metrics/tokens/{token_id}", headers={"X-CSRF-Token": csrf})
    assert revoked.status_code == 200
    _enable_metrics(client, csrf)
    assert _scrape(client, token2).status_code == 401
    # Token tidak bisa dipakai untuk endpoint /api: tanpa sesi -> 401,
    # dengan sesi admin -> 200 (Authorization header diabaikan).
    fresh_client = app_module.app.test_client()
    assert fresh_client.get("/api/metrics/tokens",
                            headers={"Authorization": f"Bearer {token2}"}).status_code == 401


def test_token_endpoints_admin_only(client, fresh_db):
    _mk_user(fresh_db, "metrics-op", "metrics-op-password", role="operator")
    _mk_user(fresh_db, "metrics-view", "metrics-viewer-password", role="viewer")

    assert _login(client, "metrics-op", "metrics-op-password").status_code == 200
    assert client.post("/api/metrics/tokens", json={"name": "x"}).status_code == 403

    client.post("/api/logout")
    assert _login(client, "metrics-view", "metrics-viewer-password").status_code == 200
    assert client.get("/api/metrics/tokens").status_code == 403


# ---------------------------------------------------------
# /metrics: mati default, format, parse, escaping, kardinalitas
# ---------------------------------------------------------

def test_metrics_disabled_by_default(client, fresh_db):
    response = client.get("/metrics")
    assert response.status_code == 404


def _bootstrap_metrics(client, fresh_db):
    _mk_user(fresh_db, "metrics-boo", "metrics-boo-password", role="admin")
    r = _login(client, "metrics-boo", "metrics-boo-password")
    csrf = r.get_json()["csrf_token"]
    created = client.post("/api/metrics/tokens",
                          json={"name": "prometheus-boo"},
                          headers={"X-CSRF-Token": csrf})
    token = created.get_json()["token"]
    _enable_metrics(client, csrf)
    return token


def test_metrics_format_parses(client, fresh_db):
    _mk_device(fresh_db, "router-parse", "192.0.2.250")
    ts = fresh_db._now()
    with fresh_db._lock:
        conn = fresh_db.get_conn()
        conn.execute(
            "INSERT INTO device_metrics (device_id, ts, cpu_pct, mem_used, mem_total,"
            " disk_used, disk_total, temp_c, uptime_s, ping_ms)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (1, ts, 40.0, 536870912, 4294967296, 4294967296, 17179869184, 45.0, 31536000, 1.0),
        )
        conn.commit()
    db_module.record_check(1, "online", 1.0, {})

    token = _bootstrap_metrics(client, fresh_db)
    response = _scrape(client, token)
    assert response.status_code == 200
    assert response.headers["Content-Type"].startswith("text/plain")
    text = response.get_data(as_text=True)
    families = list(prom_parser.text_string_to_metric_families(text))
    names = {family.name for family in families}
    assert "nms_build_info" in names
    assert "nms_devices_total" in names
    assert "nms_device_up" in names
    assert "nms_poller_running" in names
    up = next(family for family in families if family.name == "nms_device_up")
    assert any(sample.labels.get("device") == "router-parse" for sample in up.samples)
    assert all(
        "<" not in line and ">" not in line
        for line in text.splitlines() if not line.startswith("#") and "{" in line
    )


def test_metrics_escapes_label_values(client, fresh_db):
    evil = 'evil"><script>alert(1)</script>'
    device_id = _mk_device(fresh_db, evil, "192.0.2.251")
    db_module.record_check(device_id, "online", 1.0, {})
    token = _bootstrap_metrics(client, fresh_db)
    response = _scrape(client, token)
    assert response.status_code == 200
    text = response.get_data(as_text=True)
    lines = [line for line in text.splitlines() if "evil" in line]
    assert lines, "perangkat tidak muncul di metrik"
    for line in lines:
        assert 'evil"><script>' not in line  # harus sudah ter-escape
        assert 'evil\\"' in line or "evil" in line
    # Parser resmi Prometheus tetap berhasil membaca.
    families = list(prom_parser.text_string_to_metric_families(text))
    assert any(family.name == "nms_device_up" for family in families)


def test_metrics_omits_stale_devices(client, fresh_db):
    device_id = _mk_device(fresh_db, "router-stale", "192.0.2.252")
    old = "2001-01-01T00:00:00Z"
    with fresh_db._lock:
        conn = fresh_db.get_conn()
        conn.execute(
            "INSERT INTO device_metrics (device_id, ts, cpu_pct) VALUES (?,?,?)",
            (device_id, old, 50.0),
        )
        conn.execute(
            "UPDATE devices SET last_seen=? WHERE id=?",
            (old, device_id),
        )
        conn.commit()
    token = _bootstrap_metrics(client, fresh_db)
    text = _scrape(client, token).get_data(as_text=True)
    assert "router-stale" not in text


def test_metrics_series_limit_truncates(client, fresh_db, monkeypatch):
    device_id = _mk_device(fresh_db, "router-limit", "192.0.2.253")
    db_module.record_check(device_id, "online", 1.0, {})
    ts = fresh_db._now()
    with fresh_db._lock:
        fresh_db.get_conn().execute(
            "INSERT INTO device_metrics (device_id, ts, cpu_pct, mem_used, mem_total,"
            " disk_used, disk_total, temp_c, uptime_s, ping_ms)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (device_id, ts, 40.0, 536870912, 4294967296, 4294967296,
             17179869184, 45.0, 31536000, 1.0),
        )
        fresh_db.get_conn().commit()
    prom.reset_cache()
    fresh_db.set_settings({"metrics_series_limit": "10"})
    token = _bootstrap_metrics(client, fresh_db)
    response = _scrape(client, token)
    text = response.get_data(as_text=True)
    assert "nms_metrics_series_truncated 1" in text


def test_metrics_cache(client, fresh_db, monkeypatch):
    _mk_device(fresh_db, "router-cache", "192.0.2.254")
    token = _bootstrap_metrics(client, fresh_db)
    prom.reset_cache()
    first = _scrape(client, token).get_data()
    # Ubah data: cache masih mengembalikan payload yang sama.
    with fresh_db._lock:
        fresh_db.get_conn().execute(
            "INSERT INTO device_metrics (device_id, ts, cpu_pct) VALUES (1, ?, 99.0)",
            (fresh_db._now(),),
        )
        fresh_db.get_conn().commit()
    second = _scrape(client, token).get_data()
    assert first == second
    prom.reset_cache()
    third = _scrape(client, token).get_data()
    assert third != first


def test_settings_metrics_validation(client, fresh_db):
    _mk_user(fresh_db, "metrics-set", "metrics-set-password", role="admin")
    r = _login(client, "metrics-set", "metrics-set-password")
    csrf = r.get_json()["csrf_token"]
    bad = client.put("/api/settings", json={"metrics_interfaces": "bogus"},
                     headers={"X-CSRF-Token": csrf})
    assert bad.status_code == 400
    bad2 = client.put("/api/settings", json={"metrics_series_limit": "5"},
                      headers={"X-CSRF-Token": csrf})
    assert bad2.status_code == 400
    ok = client.put("/api/settings",
                    json={"metrics_interfaces": "all", "metrics_series_limit": "500"},
                    headers={"X-CSRF-Token": csrf})
    assert ok.status_code == 200
