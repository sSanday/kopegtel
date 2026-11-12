"""Integrasi: evaluasi link fiber di poller membuka/menutup masalah.

Tanpa perangkat nyata — pembacaan optik diinsert lewat helper db (data tiruan
HANYA di tests/). Memverifikasi 2-pembacaan-berturut-turut untuk membuka
masalah, histeresis saat pulih, dan atribusi severity LOS.
"""

import sys

import pytest

sys.path.insert(0, "backend")

import poller


def _device_payload():
    return {
        "name": "Uji A",
        "ip": "10.0.0.1",
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


def _seed_link(db_module, device_id, name="Link Uji"):
    profile_id = db_module.create_optical_profile(
        {
            "name": f"Profil {name}",
            "description": "profil uji",
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
            "name": name,
            "device_a_id": device_id,
            "port_a": "sfp-sfpplus1",
            "device_b_id": None,
            "port_b": None,
            "end_b_label": "ODC Pilar 12",
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


def _reading(db_module, device_id, rx_dbm, rx_loss=False):
    db_module.record_optical_readings(
        device_id,
        db_module._now(),
        [
            {
                "if_name": "sfp-sfpplus1",
                "rx_dbm": rx_dbm,
                "tx_dbm": -1.0,
                "temp_c": 45.0,
                "voltage_v": 3.3,
                "bias_ma": 6.0,
                "rx_loss": rx_loss,
                "tx_fault": False,
                "module_present": True,
                "ddm_supported": True,
            }
        ],
    )


def test_rx_low_problem_requires_two_consecutive_evaluations(fresh_db):
    device_id = fresh_db.create_device(_device_payload())
    _seed_link(fresh_db, device_id, "Link RxLow")

    for _ in range(6):
        _reading(fresh_db, device_id, -25.0)  # jauh di bawah kritis (-19)
    poller._evaluate_fiber_links()
    kinds = [p["kind"] for p in fresh_db.list_problems("open", 10)]
    assert "fiber_rx_low" not in kinds, "Satu evaluasi buruk tidak boleh membuka masalah"

    poller._evaluate_fiber_links()
    kinds = [p["kind"] for p in fresh_db.list_problems("open", 10)]
    assert "fiber_rx_low" in kinds, "Dua pembacaan berturut-turut harus membuka masalah"

    problem = next(p for p in fresh_db.list_problems("open", 10) if p["kind"] == "fiber_rx_low")
    assert problem["severity"] == "high"
    assert problem["link_name"] == "Link RxLow"

    # Pulih: Rx kembali normal (histeresis: butuh ≥ warn_low + 1.0).
    for _ in range(6):
        _reading(fresh_db, device_id, -10.0)
    poller._evaluate_fiber_links()
    open_kinds = [p["kind"] for p in fresh_db.list_problems("open", 10)]
    assert "fiber_rx_low" not in open_kinds
    resolved = fresh_db.list_problems("resolved", 10)
    assert any(p["kind"] == "fiber_rx_low" for p in resolved)


def test_los_problem_opens_with_high_severity(fresh_db):
    device_id = fresh_db.create_device(_device_payload())
    _seed_link(fresh_db, device_id, "Link LOS")

    for _ in range(6):
        _reading(fresh_db, device_id, None, rx_loss=True)
    poller._evaluate_fiber_links()
    poller._evaluate_fiber_links()
    problems = fresh_db.list_problems("open", 10)
    los = [p for p in problems if p["kind"] == "fiber_los"]
    assert los, "LOS harus membuka problem fiber_los"
    assert los[0]["severity"] == "high"


def test_flap_between_evaluations_does_not_open(fresh_db):
    """Fase buruk sesaat diselingi fase baik → tidak boleh membuka masalah."""
    device_id = fresh_db.create_device(_device_payload())
    _seed_link(fresh_db, device_id, "Link Flap")

    for _ in range(3):
        for _ in range(3):  # 3 sampel -25 → rata-rata -25 (buruk)
            _reading(fresh_db, device_id, -25.0)
        poller._evaluate_fiber_links()
        for _ in range(3):  # 3 sampel -10 → rata-rata kembali -10 (baik)
            _reading(fresh_db, device_id, -10.0)
        poller._evaluate_fiber_links()
    kinds = [p["kind"] for p in fresh_db.list_problems("open", 10)]
    assert "fiber_rx_low" not in kinds
