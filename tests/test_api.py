"""API: autentikasi, CSRF, peran, dan endpoint inti (Flask test client, DB uji)."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

import app as app_module  # noqa: E402


@pytest.fixture()
def client():
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as client:
        yield client


def _login(client, username="admin", password="admin-test-password"):
    return client.post("/api/login", json={"username": username, "password": password})


def test_login_wrong_and_me(client):
    response = _login(client, password="salah-password-1")
    assert response.status_code == 401
    response = _login(client)
    assert response.status_code == 200
    body = response.get_json()
    assert body["username"] == "admin"
    assert body["csrf_token"]
    me = client.get("/api/me")
    assert me.status_code == 200
    assert me.get_json()["role"] == "admin"


def test_csrf_required_for_mutations(client):
    _login(client)
    response = client.post("/api/groups", json={"name": "Grup CSRF"})
    assert response.status_code == 403
    assert response.get_json()["code"] == "csrf"


def test_public_config_no_auth(client):
    assert client.get("/api/public-config").status_code == 200


def test_unauthenticated_api(client):
    assert client.get("/api/devices").status_code == 401


def test_viewer_role_enforcement(client):
    # Login admin; wajib ganti kata sandi dahulu.
    response = _login(client)
    body = response.get_json()
    assert body.get("must_change_password") is True
    csrf = body["csrf_token"]
    change = client.post(
        "/api/users/me/password",
        json={"current_password": "admin-test-password", "new_password": "admin-test-password-2"},
        headers={"X-CSRF-Token": csrf},
    )
    assert change.status_code == 200
    response = _login(client, password="admin-test-password-2")
    csrf = response.get_json()["csrf_token"]
    create = client.post(
        "/api/users",
        json={"username": "viewerapi", "role": "viewer", "password": "viewer-password-1"},
        headers={"X-CSRF-Token": csrf},
    )
    assert create.status_code == 201
    client.post("/api/logout", headers={"X-CSRF-Token": csrf})

    # Login viewer; ganti password wajib; lalu coba aksi tulis → 403.
    response = _login(client, username="viewerapi", password="viewer-password-1")
    body = response.get_json()
    csrf_view = body["csrf_token"]
    client.post(
        "/api/users/me/password",
        json={"current_password": "viewer-password-1", "new_password": "viewer-password-2"},
        headers={"X-CSRF-Token": csrf_view},
    )
    assert client.post("/api/devices", json={"name": "x", "ip": "10.9.9.9", "type": "server"},
                       headers={"X-CSRF-Token": csrf_view}).status_code == 403
    assert client.get("/api/devices").status_code == 200
    assert client.get("/api/audit-log").status_code == 403


def test_login_rate_limit(client):
    for _ in range(6):
        client.post("/api/login", json={"username": "bruteforce", "password": "salah-salah-salah"})
    response = client.post("/api/login", json={"username": "bruteforce", "password": "salah-salah-salah"})
    assert response.status_code == 429
