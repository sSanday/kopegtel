"""API prediksi degradasi fiber: validasi setting dan ringkasan link berisiko."""

import sys

import pytest
from werkzeug.security import generate_password_hash

sys.path.insert(0, "backend")

import app as app_module

PASSWORD = "fiber-predict-test-password"

_DEVICE = {
    "name": "Uji Prediksi API",
    "ip": "10.0.0.2",
    "type": "mikrotik",
    "location": "",
    "poll_interval": 60,
    "maintenance": False,
    "snmp_enabled": False,
    "snmp_version": "v2c",
    "snmp_community": None,
    "snmp_port": 161,
    "snmp_v3_user": None,
    "snmp_v3_auth_proto": "SHA",
    "snmp_v3_auth_password": None,
    "snmp_v3_priv_proto": "AES",
    "snmp_v3_priv_password": None,
    "mikrotik_enabled": False,
    "mikrotik_port": 8728,
    "mikrotik_user": None,
    "mikrotik_password": None,
    "mikrotik_ssl": False,
}


@pytest.fixture()
def client():
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as client:
        yield client


def _mk_admin(db_module):
    db_module.create_user(
        "predadmin",
        generate_password_hash(PASSWORD),
        role="admin",
        must_change_password=0,
    )


def _login(client):
    response = client.post(
        "/api/login", json={"username": "predadmin", "password": PASSWORD}
    )
    assert response.status_code == 200, response.get_json()
    return response.get_json()["csrf_token"]


def _seed_link(db_module, device_id):
    profile_id = db_module.create_optical_profile(
        {
            "name": "Profil prediksi API",
            "description": "",
            "rx_warn_low": -15.0,
            "rx_crit_low": -19.0,
            "rx_warn_high": -1.0,
            "rx_crit_high": 1.0,
            "temp_warn_c": 70.0,
            "temp_crit_c": 75.0,
            "rx_sensitivity_dbm": -24.0,
            "rx_overload_dbm": 0.0,
        }
    )
    return db_module.create_fiber_link(
        {
            "name": "Link Prediksi API",
            "device_a_id": device_id,
            "port_a": "sfp-sfpplus1",
            "device_b_id": None,
            "port_b": None,
            "end_b_label": "ODC Uji",
            "length_km": 10.0,
            "fiber_type": "SM G.652D",
            "wavelength_nm": 1310,
            "wavelength_b_nm": None,
            "splices": 0,
            "connectors": 2,
            "splitter_ratio": None,
            "profile_id": profile_id,
            "notes": "",
        }
    )


def test_optical_settings_validate_prediction_limits(client, fresh_db):
    _mk_admin(fresh_db)
    headers = {"X-CSRF-Token": _login(client)}

    ok = client.put(
        "/api/optical/settings",
        json={
            "fiber_predict_horizon_days": 21,
            "fiber_predict_min_slope_db_per_week": 0.2,
            "fiber_predict_close_margin_days": 5,
        },
        headers=headers,
    )
    assert ok.status_code == 200, ok.get_json()
    settings = ok.get_json()["settings"]
    assert settings["fiber_predict_horizon_days"] == 21
    assert settings["fiber_predict_min_slope_db_per_week"] == pytest.approx(0.2)
    assert settings["fiber_predict_close_margin_days"] == 5

    for bad in (
        {"fiber_predict_horizon_days": 0},
        {"fiber_predict_horizon_days": "bukan-angka"},
        {"fiber_predict_min_slope_db_per_week": 0},
        {"fiber_predict_min_slope_db_per_week": 6},
        {"fiber_predict_close_margin_days": 31},
    ):
        response = client.put("/api/optical/settings", json=bad, headers=headers)
        assert response.status_code == 400, bad


def test_optical_summary_exposes_at_risk_links(client, fresh_db):
    _mk_admin(fresh_db)
    _login(client)
    device_id = fresh_db.create_device(_DEVICE)
    link_id = _seed_link(fresh_db, device_id)
    fresh_db.save_link_state(
        link_id,
        consec={},
        status="ok",
        metrics={
            "rx_a_dbm": -14.6,
            "predicted_days_a": 4.0,
            "predicted_target_a": "warn",
            "predicted_threshold_a": -15.0,
            "predicted_slope_a": -0.5,
        },
        ts=fresh_db._now(),
    )

    response = client.get("/api/optical/summary")
    assert response.status_code == 200, response.get_json()
    summary = response.get_json()

    assert summary["links_at_risk"] == 1
    assert len(summary["at_risk_links"]) == 1
    item = summary["at_risk_links"][0]
    assert item["link_id"] == link_id
    assert item["side"] == "A"
    assert item["days"] == pytest.approx(4.0)
    assert item["target"] == "warn"
    assert item["threshold_dbm"] == pytest.approx(-15.0)
    assert item["slope_db_per_week"] == pytest.approx(-0.5)
