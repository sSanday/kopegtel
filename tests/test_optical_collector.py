"""Kolektor optik: RouterOS tiruan (tests/) + unit test grup sensor SNMP."""

import pytest

from collectors import optical
from fake_routeros import FakeRouterOS, attrs


def _sfp_rows(**overrides):
    sfp = {
        "sfp-module-present": "true",
        "sfp-rx-power": "-5.2143dBm",
        "sfp-tx-power": "-1.4157dBm",
        "sfp-temperature": "39.8437C",
        "sfp-supply-voltage": "3.3005V",
        "sfp-tx-bias-current": "6.5mA",
        "sfp-rx-loss": "false",
        "sfp-tx-fault": "false",
        "sfp-wavelength": "1310",
        "sfp-type": "SFP",
        "sfp-vendor-name": "FIBERCO",
        "sfp-vendor-part-number": "SFP-10G-LR",
        "sfp-vendor-serial": "SN0042",
        "sfp-link-length-sm": "10km",
    }
    sfp.update(overrides)
    return {
        "/interface/ethernet/print": [
            attrs(name="sfp-sfpplus1", running="true", disabled="false"),
            attrs(name="ether1", running="true", disabled="false"),
            attrs(name="sfp-sfpplus2", running="true", disabled="false"),
        ],
        "/interface/ethernet/monitor:sfp-sfpplus1": [attrs(**sfp)],
        "/interface/ethernet/monitor:ether1": [attrs(**{"full-duplex": "true", "speed": "1Gbps"})],
        "/interface/ethernet/monitor:sfp-sfpplus2": [attrs(**{"sfp-module-present": "false"})],
    }


def _config(port, password="secret", user="api"):
    return {"ip": "127.0.0.1", "port": port, "user": user, "password": password, "ssl": False}


def test_mikrotik_optical_success():
    server = FakeRouterOS(rows=_sfp_rows())
    port = server.start()
    try:
        result = optical.check_optical_mikrotik(_config(port))
    finally:
        server.stop()

    assert result["ok"] is True
    by_name = {p["if_name"]: p for p in result["ports"]}
    assert set(by_name) == {"sfp-sfpplus1", "sfp-sfpplus2"}

    main = by_name["sfp-sfpplus1"]
    assert main["rx_dbm"] == pytest.approx(-5.2143)
    assert main["tx_dbm"] == pytest.approx(-1.4157)
    assert main["temp_c"] == pytest.approx(39.8437)
    assert main["voltage_v"] == pytest.approx(3.3005)
    assert main["bias_ma"] == pytest.approx(6.5)
    assert main["ddm_supported"] is True
    assert main["module_present"] is True
    assert main["rx_loss"] is False
    assert main["vendor"] == "FIBERCO"
    assert main["part_no"] == "SFP-10G-LR"
    assert main["serial"] == "SN0042"
    assert main["wavelength_nm"] == 1310
    assert main["link_length_m"] == 10000

    empty = by_name["sfp-sfpplus2"]
    assert empty["module_present"] is False
    assert empty["ddm_supported"] is False
    assert empty["rx_dbm"] is None


def test_mikrotik_optical_power_in_mw_converted():
    rows = _sfp_rows(**{
        "sfp-rx-power": "0.024 mW",
        "sfp-tx-power": "0.077 mW",
    })
    server = FakeRouterOS(rows=rows)
    port = server.start()
    try:
        result = optical.check_optical_mikrotik(_config(port))
    finally:
        server.stop()

    import math

    assert result["ok"] is True
    main = next(p for p in result["ports"] if p["if_name"] == "sfp-sfpplus1")
    assert main["rx_dbm"] == pytest.approx(10 * math.log10(0.024))
    assert main["tx_dbm"] == pytest.approx(10 * math.log10(0.077))


def test_mikrotik_optical_los_and_no_ddm():
    rows = _sfp_rows(**{
        "sfp-rx-power": "-inf",
        "sfp-rx-loss": "true",
    })
    server = FakeRouterOS(rows=rows)
    port = server.start()
    try:
        result = optical.check_optical_mikrotik(_config(port))
    finally:
        server.stop()

    main = next(p for p in result["ports"] if p["if_name"] == "sfp-sfpplus1")
    assert main["rx_dbm"] is None       # -inf tidak boleh dikarang menjadi angka
    assert main["rx_loss"] is True      # LOS terdeteksi dari flag
    assert main["ddm_supported"] is True  # rx-power dilaporkan (meski -inf)


def test_mikrotik_optical_module_without_ddm():
    rows = _sfp_rows(**{
        "sfp-rx-power": None,
        "sfp-tx-power": None,
        "sfp-temperature": None,
        "sfp-supply-voltage": None,
        "sfp-tx-bias-current": None,
    })
    # attrs() menulis =key=<value>; nilai None tidak bisa diserialisasikan → buat baris manual.
    sfp = rows["/interface/ethernet/monitor:sfp-sfpplus1"][0]
    rows["/interface/ethernet/monitor:sfp-sfpplus1"] = [
        [word for word in sfp if not any(
            word.startswith(f"={k}=")
            for k in ("sfp-rx-power", "sfp-tx-power", "sfp-temperature",
                      "sfp-supply-voltage", "sfp-tx-bias-current")
        )]
    ]
    server = FakeRouterOS(rows=rows)
    port = server.start()
    try:
        result = optical.check_optical_mikrotik(_config(port))
    finally:
        server.stop()

    main = next(p for p in result["ports"] if p["if_name"] == "sfp-sfpplus1")
    assert main["module_present"] is True
    assert main["ddm_supported"] is False
    assert main["rx_dbm"] is None


def test_mikrotik_optical_skip_ports_fresh_non_optical():
    server = FakeRouterOS(rows=_sfp_rows())
    port = server.start()
    try:
        result = optical.check_optical_mikrotik(_config(port), skip_ports={"ether1"})
    finally:
        server.stop()

    assert result["ok"] is True
    assert "ether1" not in result["scanned"]
    assert set(result["scanned"]) == {"sfp-sfpplus1", "sfp-sfpplus2"}


def test_snmp_group_sensors_direction_by_label():
    sensors = {
        "10": {"type": "14", "scale": "9", "precision": "2", "value": "-512"},   # -5.12 dBm
        "11": {"type": "14", "scale": "9", "precision": "2", "value": "-141"},   # -1.41 dBm
        "12": {"type": "8", "scale": "9", "precision": "1", "value": "398"},     # 39.8 °C
        "13": {"type": "4", "scale": "9", "precision": "3", "value": "3300"},    # 3.3 V
        "14": {"type": "5", "scale": "9", "precision": "1", "value": "65"},      # 6.5 mA
    }
    physicals = {
        "10": {"name": "sfp-sfpplus1 Rx Power", "descr": "sfp-sfpplus1-rx-power"},
        "11": {"name": "sfp-sfpplus1 Tx Power", "descr": "sfp-sfpplus1-tx-power"},
        "12": {"name": "sfp-sfpplus1 Temperature"},
        "13": {"name": "sfp-sfpplus1 Voltage"},
        "14": {"name": "sfp-sfpplus1 Bias Current"},
    }
    ports, notes = optical._group_sensors(sensors, physicals)
    assert len(ports) == 1
    port = ports[0]
    assert port["if_name"] == "sfp-sfpplus1"
    assert port["rx_dbm"] == pytest.approx(-5.12)
    assert port["tx_dbm"] == pytest.approx(-1.41)
    assert port["temp_c"] == pytest.approx(39.8)
    assert port["voltage_v"] == pytest.approx(3.3)
    assert port["bias_ma"] == pytest.approx(6.5)
    assert notes == []


def test_snmp_group_sensors_two_unlabeled_powers_use_physics_convention():
    # Dua sensor dBm tanpa arah pada port yang sama → konvensi fisika transceiver: Rx ≤ Tx.
    sensors = {
        "20": {"type": "14", "scale": "9", "precision": "2", "value": "-512"},
        "21": {"type": "14", "scale": "9", "precision": "2", "value": "-141"},
    }
    physicals = {
        "20": {"name": "sfp-sfpplus1", "descr": "sfp-sfpplus1"},
        "21": {"name": "sfp-sfpplus1", "descr": "sfp-sfpplus1"},
    }
    ports, notes = optical._group_sensors(sensors, physicals)
    assert len(ports) == 1
    assert ports[0]["rx_dbm"] == pytest.approx(-5.12)
    assert ports[0]["tx_dbm"] == pytest.approx(-1.41)
    assert any("konvensi" in note for note in notes)


def test_snmp_group_sensors_single_unlabeled_power_not_guessed():
    # Satu sensor dBm tanpa arah → dibiarkan kosong (tidak menebak Rx/Tx).
    sensors = {
        "30": {"type": "14", "scale": "9", "precision": "2", "value": "-512"},
    }
    physicals = {"30": {"name": "sfp-sfpplus9", "descr": "sfp-sfpplus9"}}
    ports, notes = optical._group_sensors(sensors, physicals)
    assert len(ports) == 1
    assert ports[0]["rx_dbm"] is None
    assert ports[0]["tx_dbm"] is None
    assert any("tanpa arah" in note for note in notes)


def test_snmp_group_sensors_celsius_scale_and_precision():
    sensors = {
        "40": {"type": "8", "scale": "10", "precision": "0", "value": "39800"},  # 39.8 °C (milli)
    }
    physicals = {"40": {"name": "sfp-sfpplus1 Temperature", "descr": "sfp-sfpplus1 temperature"}}
    ports, _ = optical._group_sensors(sensors, physicals)
    assert ports[0]["temp_c"] == pytest.approx(39.8)
