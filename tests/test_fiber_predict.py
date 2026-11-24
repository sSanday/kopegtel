"""Unit test fungsi prediksi degradasi fiber (P1)."""

import sys

sys.path.insert(0, "backend")

import link_eval


def test_predict_crossing_basic_warn():
    # Rx -14.5, warn -15, slope -0.5 dB/minggu → ~7 hari
    r = link_eval.predict_crossing(-14.5, -0.5, -15.0, -18.0, 14, 0.1)
    assert r is not None
    assert r["target"] == "warn"
    assert r["threshold_dbm"] == -15.0
    assert 6 < r["days"] < 8


def test_predict_crossing_none_when_no_data():
    assert link_eval.predict_crossing(None, -0.4, -15.0, -18.0, 14, 0.1) is None
    assert link_eval.predict_crossing(-11.0, None, -15.0, -18.0, 14, 0.1) is None
    assert link_eval.predict_crossing(-11.0, -0.4, None, -18.0, 14, 0.1) is None


def test_predict_crossing_none_when_slope_not_negative_enough():
    assert link_eval.predict_crossing(-11.0, -0.05, -15.0, -18.0, 14, 0.1) is None
    assert link_eval.predict_crossing(-11.0, 0.3, -15.0, -18.0, 14, 0.1) is None


def test_predict_crossing_crit_when_between_warn_and_crit():
    r = link_eval.predict_crossing(-16.0, -0.4, -15.0, -18.0, 60, 0.1)
    assert r is not None
    assert r["target"] == "crit"
    assert r["threshold_dbm"] == -18.0


def test_predict_crossing_none_when_below_crit():
    assert link_eval.predict_crossing(-19.0, -0.4, -15.0, -18.0, 14, 0.1) is None


def test_predict_crossing_horizon_boundary():
    # di luar horizon → None
    assert link_eval.predict_crossing(-11.0, -0.5, -15.0, -18.0, 14, 0.1) is None
    # masuk horizon
    r = link_eval.predict_crossing(-14.0, -2.0, -15.0, -18.0, 14, 0.1)
    assert r is not None
    assert r["days"] <= 14


def test_predict_crossing_close_margin_extends_horizon():
    # tanpa margin: days = 0.5/(0.3/7)=11.67 <=14 → valid
    r = link_eval.predict_crossing(-14.5, -0.3, -15.0, -18.0, 14, 0.1, close_margin_days=3)
    assert r is not None


def test_predict_crossing_crit_target_reachable():
    r = link_eval.predict_crossing(-16.0, -0.5, -15.0, -18.0, 60, 0.1)
    assert r is not None
    assert r["target"] == "crit"
    assert r["threshold_dbm"] == -18.0


def test_predict_crossing_crit_none_when_no_crit_low():
    assert link_eval.predict_crossing(-16.0, -0.5, -15.0, None, 60, 0.1) is None
