"""Collector MikroTik melawan server RouterOS tiruan (tests/fake_routeros.py)."""

import pytest

from collectors import mikrotik
from fake_routeros import FakeRouterOS


def _config(port, password="secret", user="api"):
    return {"ip": "127.0.0.1", "port": port, "user": user, "password": password, "ssl": False}


def test_success_v7():
    server = FakeRouterOS()
    port = server.start()
    try:
        result = mikrotik.check_mikrotik(_config(port))
    finally:
        server.stop()

    assert result["ok"] is True
    assert result["identity"] == "fake-router"
    assert result["cpu_load"] == 23
    assert result["cpu_count"] == 4
    assert result["cpu_frequency"] == 1400
    assert result["total_memory"] == 1_000_000_000
    assert result["free_memory"] == 512_000_000
    assert result["total_hdd_space"] == 128_000_000_000
    assert result["version"] == "7.12.1"
    assert result["uptime_s"] == 3 * 7 * 86400 + 2 * 86400 + 5 * 3600 + 6 * 60 + 7
    assert result["temperature"] == 46.5
    assert result["cpu_temperature"] == 52.0
    assert result["voltage"] == 12.6
    assert result["model"] == "RB4011iGS+"
    assert result["serial_number"] == "SNFAKE123"
    assert result["current_firmware"] == "7.12.1"
    interfaces = {i["name"]: i for i in result["interfaces"]}
    assert interfaces["ether1"]["running"] is True
    assert interfaces["ether1"]["rx_byte"] == 1_000_000_000
    assert interfaces["ether2"]["disabled"] is True


def test_success_health_v6():
    server = FakeRouterOS(health_v6=True)
    port = server.start()
    try:
        result = mikrotik.check_mikrotik(_config(port))
    finally:
        server.stop()
    assert result["ok"] is True
    assert result["temperature"] == 46.5
    assert result["cpu_temperature"] == 52.0
    assert result["voltage"] == 12.6


def test_health_failure_is_tolerated():
    server = FakeRouterOS(trap_health=True)
    port = server.start()
    try:
        result = mikrotik.check_mikrotik(_config(port))
    finally:
        server.stop()
    # Kegagalan sebagian tidak menggagalkan keseluruhan: resource tetap tersimpan.
    assert result["ok"] is True
    assert result["cpu_load"] == 23
    assert result["temperature"] is None
    assert result["voltage"] is None


def test_wrong_password():
    server = FakeRouterOS()
    port = server.start()
    try:
        result = mikrotik.check_mikrotik(_config(port, password="salah"))
    finally:
        server.stop()
    assert result["ok"] is False
    assert result["reason"] == "auth_failed"


def test_missing_user():
    result = mikrotik.check_mikrotik(_config(1, user=""))
    assert result["ok"] is False
    assert result["reason"] == "auth_failed"


def test_connection_closed():
    server = FakeRouterOS()
    port = server.start()
    server.stop()  # port sudah pasti tertutup
    result = mikrotik.check_mikrotik(_config(port))
    assert result["ok"] is False
    assert result["reason"] in ("unreachable", "error", "timeout")


def test_timeout(monkeypatch):
    monkeypatch.setattr(mikrotik, "TIMEOUT_SECONDS", 0.5)

    def silent_server():
        import socket as sk

        sock = sk.socket(sk.AF_INET, sk.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port = sock.getsockname()[1]

        def accept_forever():
            try:
                sock.accept()  # terima lalu tidak pernah menjawab
            except OSError:
                pass

        import threading

        threading.Thread(target=accept_forever, daemon=True).start()
        return sock, port

    sock, port = silent_server()
    result = mikrotik.check_mikrotik(_config(port))
    assert result["ok"] is False
    assert result["reason"] in ("timeout", "unreachable", "error")
    sock.close()
