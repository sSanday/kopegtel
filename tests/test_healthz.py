"""/healthz: minimal, tanpa login, 200 hanya bila DB terbaca DAN poller segar."""

import time

import poller


def test_healthz_503_without_poller(fresh_db):
    import app as app_module

    poller.LAST_HEARTBEAT = None
    client = app_module.app.test_client()
    response = client.get("/healthz")
    assert response.status_code == 503
    assert response.get_json() == {"status": "error"}


def test_healthz_200_when_poller_fresh_and_db_ok(fresh_db):
    import app as app_module

    poller.LAST_HEARTBEAT = time.time()
    client = app_module.app.test_client()
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}
    poller.LAST_HEARTBEAT = None


def test_healthz_503_when_poller_stale(fresh_db):
    import app as app_module

    poller.LAST_HEARTBEAT = time.time() - 3600
    client = app_module.app.test_client()
    response = client.get("/healthz")
    assert response.status_code == 503
    poller.LAST_HEARTBEAT = None
