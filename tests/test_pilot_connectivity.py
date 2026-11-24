"""Uji logika verdict alat diagnostik konektivitas pilot."""

import importlib.util
import pathlib

_PATH = pathlib.Path(__file__).resolve().parents[1] / "tools" / "pilot_connectivity.py"
_spec = importlib.util.spec_from_file_location("pilot_connectivity", _PATH)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)

same_subnet = _module.same_subnet
verdict = _module.verdict


def test_same_subnet_24():
    assert same_subnet("192.168.30.10", "192.168.30.1", 24)
    assert not same_subnet("172.18.78.143", "192.168.30.1", 24)


def test_verdict_ok_when_ping_and_snmp_ok():
    assert verdict(True, True, True, "192.168.30.10", "192.168.30.1", 24) == "ok"


def test_verdict_ok_when_snmp_not_enabled():
    assert verdict(True, None, False, "192.168.30.10", "192.168.30.1", 24) == "ok"


def test_verdict_snmp_fail_when_ping_ok():
    assert verdict(True, False, True, "192.168.30.10", "192.168.30.1", 24) == "snmp_fail"


def test_verdict_wrong_network_when_outside_subnet():
    assert verdict(False, None, False, "172.18.78.143", "192.168.30.1", 24) == "wrong_network"


def test_verdict_unreachable_when_same_subnet_but_ping_fails():
    assert verdict(False, None, False, "192.168.30.10", "192.168.30.1", 24) == "unreachable"
