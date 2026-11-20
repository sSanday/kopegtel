"""Tes penanganan nilai retensi optik ekstrem (task audit terbatas)."""

import db


def test_get_fiber_settings_handles_overflow(fresh_db):
    fresh_db.set_fiber_settings({"retention_optical_hourly_days": "1e999"})

    settings = db.get_fiber_settings()

    assert settings["retention_optical_hourly_days"] == 90


def test_purge_old_data_handles_overflow_setting(fresh_db):
    fresh_db.set_fiber_settings({"retention_optical_hourly_days": "1e999"})

    # Tidak boleh melempar OverflowError/ValueError.
    db.purge_old_data()
