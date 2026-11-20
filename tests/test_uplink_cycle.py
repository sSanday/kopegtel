"""Tes deteksi siklus rantai uplink panjang (task audit terbatas)."""

import backend.routes.devices as devices_route


def test_uplink_cycle_longer_than_50_detected(monkeypatch):
    def fake_get_device(device_id, include_secrets=False):
        if device_id == 1:
            return {"id": 1, "ip": "192.168.1.1", "parent_id": None}
        if 2 <= device_id <= 51:
            return {"id": device_id, "ip": "192.168.1.1", "parent_id": device_id + 1}
        if device_id == 52:
            return {"id": 52, "ip": "192.168.1.1", "parent_id": 1}
        return None

    monkeypatch.setattr(devices_route.db, "get_device", fake_get_device)

    _clean, error = devices_route._validate_device(
        {"name": "perangkat", "ip": "192.168.1.1", "parent_id": 2},
        existing_id=1,
    )

    assert error == "Uplink ini membuat siklus rantai uplink."


def test_uplink_small_cycle_detected(monkeypatch):
    def fake_get_device(device_id, include_secrets=False):
        if device_id == 1:
            return {"id": 1, "ip": "192.168.1.1", "parent_id": None}
        if device_id == 2:
            return {"id": 2, "ip": "192.168.1.1", "parent_id": 3}
        if device_id == 3:
            return {"id": 3, "ip": "192.168.1.1", "parent_id": 1}
        return None

    monkeypatch.setattr(devices_route.db, "get_device", fake_get_device)

    _clean, error = devices_route._validate_device(
        {"name": "perangkat", "ip": "192.168.1.1", "parent_id": 2},
        existing_id=1,
    )

    assert error == "Uplink ini membuat siklus rantai uplink."


def test_uplink_self_cycle_detected(monkeypatch):
    def fake_get_device(device_id, include_secrets=False):
        if device_id == 1:
            return {"id": 1, "ip": "192.168.1.1", "parent_id": None}
        return None

    monkeypatch.setattr(devices_route.db, "get_device", fake_get_device)

    _clean, error = devices_route._validate_device(
        {"name": "perangkat", "ip": "192.168.1.1", "parent_id": 1},
        existing_id=1,
    )

    assert error == "Perangkat tidak boleh menjadi uplink dirinya sendiri."


def test_uplink_long_chain_without_cycle_accepted(monkeypatch):
    def fake_get_device(device_id, include_secrets=False):
        if device_id == 1:
            return {"id": 1, "ip": "192.168.1.1", "parent_id": None}
        if 2 <= device_id <= 52:
            return {"id": device_id, "ip": "192.168.1.1", "parent_id": device_id + 1 if device_id < 52 else None}
        return None

    monkeypatch.setattr(devices_route.db, "get_device", fake_get_device)

    clean, error = devices_route._validate_device(
        {"name": "perangkat", "ip": "192.168.1.1", "parent_id": 2},
        existing_id=1,
    )

    assert error is None
    assert clean.get("parent_id") == 2