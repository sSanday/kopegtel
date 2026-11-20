"""Tes pengukuran durasi poll per perangkat (task audit terbatas)."""

import backend.poller as poller
import db


def test_poll_duration_recorded_per_device(monkeypatch):
    monkeypatch.setattr(
        db, "get_device", lambda device_id, include_secrets=True: {"id": device_id, "ip": "127.0.0.1"}
    )
    monkeypatch.setattr(poller, "_run_check_body", lambda device, force_mikrotik=False: None)

    poller.run_check(42)

    duration = poller.poll_duration(42)
    assert duration is not None
    assert duration >= 0
