"""Unit test logika murni fiber optik: konversi, redaman, anggaran, evaluasi link."""

import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, "backend")

import link_eval
import optical


def test_mw_to_dbm():
    assert optical.mw_to_dbm(1.0) == pytest.approx(0.0, abs=1e-9)
    assert optical.mw_to_dbm(0.1) == pytest.approx(-10.0, abs=1e-9)
    assert optical.mw_to_dbm(0.01) == pytest.approx(-20.0, abs=1e-9)
    assert optical.mw_to_dbm(0.001) == pytest.approx(-30.0, abs=1e-9)
    assert optical.mw_to_dbm(0) is None
    assert optical.mw_to_dbm(-1) is None
    assert optical.mw_to_dbm("tidak-angka") is None


def test_uw_to_dbm():
    assert optical.uw_to_dbm(1000.0) == pytest.approx(0.0, abs=1e-9)
    assert optical.uw_to_dbm(10.0) == pytest.approx(-20.0, abs=1e-9)
    assert optical.uw_to_dbm(0) is None


def test_parse_power_dbm_units():
    assert optical.parse_power_dbm("-5.2143dBm") == pytest.approx(-5.2143, abs=1e-9)
    assert optical.parse_power_dbm("-5,21 dBm") == pytest.approx(-5.21, abs=1e-9)
    assert optical.parse_power_dbm("0.31 mW") == pytest.approx(optical.mw_to_dbm(0.31), abs=1e-9)
    assert optical.parse_power_dbm("310 uW") == pytest.approx(optical.uw_to_dbm(310.0), abs=1e-9)
    assert optical.parse_power_dbm("310µW") == pytest.approx(optical.uw_to_dbm(310.0), abs=1e-9)
    assert optical.parse_power_dbm("0.0002 W") == pytest.approx(optical.mw_to_dbm(0.2), abs=1e-9)
    # Tanpa satuan → dianggap dBm (perilaku default RouterOS).
    assert optical.parse_power_dbm("-12.5") == pytest.approx(-12.5, abs=1e-9)
    # Nilai tak terukur.
    assert optical.parse_power_dbm("-inf") is None
    assert optical.parse_power_dbm("") is None
    assert optical.parse_power_dbm(None) is None
    assert optical.parse_power_dbm("n/a") is None


def test_parse_router_formats():
    assert optical.parse_temperature_c("39.84C") == pytest.approx(39.84)
    assert optical.parse_temperature_c("46.5 °C") == pytest.approx(46.5)
    assert optical.parse_voltage_v("3.3005V") == pytest.approx(3.3005)
    assert optical.parse_bias_ma("6.5mA") == pytest.approx(6.5)
    assert optical.parse_length_m("10km") == 10000
    assert optical.parse_length_m("500m") == 500
    assert optical.parse_wavelength_nm("1310") == 1310
    assert optical.parse_wavelength_nm("1550nm") == 1550
    assert optical.parse_sfp_bool("true") is True
    assert optical.parse_sfp_bool("false") is False


def test_attenuation_budget_margin():
    assert optical.attenuation_db(-1.0, -8.0) == pytest.approx(7.0)
    assert optical.attenuation_db(None, -8.0) is None
    assert optical.attenuation_db(-1.0, None) is None

    budget = optical.link_budget_db(
        10.0, 1310, splices=4, connectors=2, splitter_ratio="1:8",
        alpha_1310=0.35, alpha_1550=0.25, splice_loss=0.1, connector_loss=0.5,
        splitter_table={"1:8": 9.5},
    )
    # 10*0.35 + 4*0.1 + 2*0.5 + 9.5 = 3.5 + 0.4 + 1.0 + 9.5 = 14.4
    assert budget["total_db"] == pytest.approx(14.4)

    # 1550 nm pakai α berbeda.
    budget1550 = optical.link_budget_db(
        10.0, 1550, splices=0, connectors=2, splitter_ratio=None,
        alpha_1310=0.35, alpha_1550=0.25, splice_loss=0.1, connector_loss=0.5,
        splitter_table={},
    )
    assert budget1550["total_db"] == pytest.approx(10 * 0.25 + 2 * 0.5)

    # Splitter tidak dikenal → None (tidak mengarang).
    unknown = optical.link_budget_db(
        1.0, 1310, splices=0, connectors=0, splitter_ratio="1:128",
        alpha_1310=0.35, alpha_1550=0.25, splice_loss=0.1, connector_loss=0.5,
        splitter_table={"1:8": 9.5},
    )
    assert unknown is None

    assert optical.margin_db(-18.0, -24.0) == pytest.approx(6.0)
    assert optical.deviation_db(9.0, 6.0) == pytest.approx(3.0)
    assert optical.asymmetry_db(7.0, 4.0) == pytest.approx(3.0)


def test_linear_trend():
    now = datetime.now(timezone.utc)
    points = []
    # Menurun 0.5 dB per minggu persis.
    for day in range(14):
        ts = now - timedelta(days=13 - day)
        epoch_days = ts.timestamp() / 86400.0
        value = -10.0 - (day * (0.5 / 7.0))
        points.append((epoch_days, value))
    slope = optical.linear_trend_per_week(points)
    assert slope == pytest.approx(-0.5, abs=0.02)
    assert optical.linear_trend_per_week([(1.0, -10.0)]) is None


def test_hysteresis():
    # Rendah makin buruk: pulih hanya melewati warn + 1 dB.
    assert optical.hysteresis_level_low(-10.0, -15.0, -19.0, 1.0, False) == "ok"
    assert optical.hysteresis_level_low(-16.0, -15.0, -19.0, 1.0, False) == "warn"
    assert optical.hysteresis_level_low(-20.0, -15.0, -19.0, 1.0, False) == "crit"
    # Sedang bermasalah: -14.5 belum pulih (butuh ≥ -14.0).
    assert optical.hysteresis_level_low(-14.5, -15.0, -19.0, 1.0, True) == "warn"
    assert optical.hysteresis_level_low(-14.0, -15.0, -19.0, 1.0, True) == "ok"

    assert optical.hysteresis_level_high(2.0, 3.0, None, 1.0, False) == "ok"
    assert optical.hysteresis_level_high(3.5, 3.0, None, 1.0, False) == "warn"
    assert optical.hysteresis_level_high(2.2, 3.0, None, 1.0, True) == "warn"   # belum pulih (butuh ≤ 2.0)
    assert optical.hysteresis_level_high(2.0, 3.0, None, 1.0, True) == "ok"


FIBER_SETTINGS = {
    "fiber_alpha_1310": 0.35,
    "fiber_alpha_1550": 0.25,
    "fiber_splice_loss": 0.1,
    "fiber_connector_loss": 0.5,
    "fiber_splitter_losses": {"1:2": 3.5, "1:4": 6.5, "1:8": 9.5, "1:16": 12.5, "1:32": 15.5, "1:64": 18.5},
    "fiber_deviation_warn": 3.0,
    "fiber_deviation_crit": 5.0,
    "fiber_margin_warn": 3.0,
    "fiber_margin_crit": 1.0,
    "fiber_asymmetry_warn": 3.0,
    "fiber_drop_db": 3.0,
    "fiber_drop_baseline_days": 7,
    "fiber_trend_db_per_week": 0.5,
    "fiber_trend_days": 14,
    "fiber_consec_required": 2,
    "fiber_hysteresis_db": 1.0,
    "fiber_avg_readings": 3,
}

PROFILE = {
    "rx_warn_low": -15.0,
    "rx_crit_low": -19.0,
    "rx_warn_high": -1.0,
    "rx_crit_high": 1.0,
    "temp_warn_c": 70.0,
    "temp_crit_c": 75.0,
    "rx_sensitivity_dbm": -24.0,
    "rx_overload_dbm": 0.0,
}


def _link(length_km=10.0):
    return {
        "id": 1,
        "name": "Link Uji",
        "length_km": length_km,
        "wavelength_nm": 1310,
        "wavelength_b_nm": None,
        "splices": 2,
        "connectors": 2,
        "splitter_ratio": None,
        "profile_id": 1,
    }


def _reading(rx, tx, *, if_name="sfp-sfpplus1", loss=False, fault=False, present=True, ddm=True):
    return {
        "if_name": if_name,
        "rx_dbm": rx,
        "tx_dbm": tx,
        "temp_c": 45.0,
        "voltage_v": 3.3,
        "bias_ma": 6.0,
        "rx_loss": loss,
        "tx_fault": fault,
        "module_present": present,
        "ddm_supported": ddm,
    }


def test_evaluate_link_nominal():
    # TxA=-1.0 → RxB=-8.0 → att 7.0; budget 10*0.35+2*0.1+2*0.5=5.5; deviasi 1.5; margin 16.
    ra = _reading(-8.0, -1.0, if_name="sfp-a")
    rb = _reading(-8.0, -1.0, if_name="sfp-b")
    result = link_eval.evaluate_link(_link(), PROFILE, FIBER_SETTINGS, ra, rb,
                                     baseline_rx_a=-8.0, baseline_rx_b=-8.0)
    assert result["attenuation_ab_db"] == pytest.approx(7.0)
    assert result["attenuation_ba_db"] == pytest.approx(7.0)
    assert result["asymmetry_db"] == pytest.approx(0.0)
    # budget = 10*0.35 + 2*0.1 (splice) + 2*0.5 (konektor) = 4.7
    assert result["deviation_ab_db"] == pytest.approx(7.0 - 4.7)
    assert result["margin_a_db"] == pytest.approx(16.0)
    assert result["status"] == "ok"
    assert result["issues"] == []


def test_evaluate_link_los():
    ra = _reading(-42.0, -1.0, if_name="sfp-a", loss=True)
    rb = _reading(-8.0, -1.0, if_name="sfp-b")
    result = link_eval.evaluate_link(_link(), PROFILE, FIBER_SETTINGS, ra, rb)
    assert result["los_a"] is True
    assert result["status"] == "crit"
    assert any(i["kind"] == "fiber_los" for i in result["issues"])


def test_evaluate_link_rx_low_and_hysteresis():
    # Link 40 km (budget 15.2 dB): redaman 15.5 → deviasi kecil, yang waspada hanya Rx.
    ra = _reading(-16.5, -1.0, if_name="sfp-a")
    rb = _reading(-16.5, -1.0, if_name="sfp-b")
    result = link_eval.evaluate_link(_link(40), PROFILE, FIBER_SETTINGS, ra, rb)
    assert any(i["kind"] == "fiber_rx_low" and i["severity"] == "warning" for i in result["issues"])
    assert result["status"] == "warn"

    # Sedang bermasalah: -14.5 belum pulih (butuh ≥ -14.0).
    ra_almost = _reading(-14.5, -1.0, if_name="sfp-a")
    rb_almost = _reading(-14.5, -1.0, if_name="sfp-b")
    result2 = link_eval.evaluate_link(_link(40), PROFILE, FIBER_SETTINGS, ra_almost, rb_almost,
                                      was_bad={"fiber_rx_low"})
    assert any(i["kind"] == "fiber_rx_low" for i in result2["issues"])
    # Pulih penuh.
    ra_ok = _reading(-13.0, -1.0, if_name="sfp-a")
    rb_ok = _reading(-13.0, -1.0, if_name="sfp-b")
    result3 = link_eval.evaluate_link(_link(40), PROFILE, FIBER_SETTINGS, ra_ok, rb_ok,
                                      was_bad={"fiber_rx_low"})
    assert not any(i["kind"] == "fiber_rx_low" for i in result3["issues"])


def test_evaluate_link_deviation_crit():
    # Budget 5.5; buat redaman terukur 11.5 → deviasi 6 (> 5 kritis).
    ra = _reading(-13.5, -1.0, if_name="sfp-a")   # RxA sangat rendah tapi ≠ LOS
    rb = _reading(-12.5, -1.0, if_name="sfp-b")   # att_ab = -1 - (-12.5) = 11.5
    result = link_eval.evaluate_link(_link(), PROFILE, FIBER_SETTINGS, ra, rb)
    assert result["deviation_ab_db"] == pytest.approx(11.5 - 4.7)
    assert result["status"] == "crit"


def test_evaluate_link_margin_low():
    # Profil dengan sensitivitas -16: Rx -14 → margin 2.0 (< 3, waspada),
    # tapi Rx masih di atas ambang rendah (-15) sehingga hanya margin yang waspada.
    # Link 40 km agar deviasi tetap di bawah ambang.
    profile = dict(PROFILE, rx_sensitivity_dbm=-16.0)
    ra = _reading(-14.0, -1.0, if_name="sfp-a")
    rb = _reading(-14.0, -1.0, if_name="sfp-b")
    result = link_eval.evaluate_link(_link(40), profile, FIBER_SETTINGS, ra, rb)
    assert any(i["kind"] == "fiber_margin_low" for i in result["issues"])
    assert result["status"] == "warn"


def test_evaluate_link_asymmetry():
    # A→B = 7.0, B→A = 3.5 → asimetri 3.5 > 3.
    ra = _reading(-4.5, -8.0, if_name="sfp-a")    # att_ba = -8 - (-4.5) = -3.5? tidak.
    rb = _reading(-8.0, -1.0, if_name="sfp-b")
    # att_ba = tx_b - rx_a = -1 - (-4.5) = 3.5 ; att_ab = tx_a - rx_b = -8 - (-8) = 0? 
    result = link_eval.evaluate_link(_link(), PROFILE, FIBER_SETTINGS, ra, rb)
    assert result["attenuation_ab_db"] == pytest.approx(0.0)
    assert result["attenuation_ba_db"] == pytest.approx(3.5)
    assert result["asymmetry_db"] == pytest.approx(3.5)
    assert any(i["kind"] == "fiber_asymmetry" for i in result["issues"])


def test_evaluate_link_drop_and_trend():
    ra = _reading(-12.0, -1.0, if_name="sfp-a")
    rb = _reading(-8.0, -1.0, if_name="sfp-b")
    # Baseline A = -8 → turun 4 dB (> 3).
    result = link_eval.evaluate_link(_link(), PROFILE, FIBER_SETTINGS, ra, rb,
                                     baseline_rx_a=-8.0, baseline_rx_b=-8.0,
                                     trend_rx_a=-0.6, trend_rx_b=None)
    kinds = [i["kind"] for i in result["issues"]]
    assert "fiber_drop" in kinds
    assert "fiber_trend" in kinds


def test_evaluate_link_unmonitored_b():
    ra = _reading(-8.0, -1.0, if_name="sfp-a")
    result = link_eval.evaluate_link(_link(), PROFILE, FIBER_SETTINGS, ra, None)
    assert result["attenuation_ab_db"] is None
    assert result["attenuation_ba_db"] is None
    assert result["margin_b_db"] is None
    assert any(s["ok"] is False for s in result["steps"])
