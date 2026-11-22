"""Modul MikroTik M1-M5 terhadap server RouterOS tiruan (tests/fake_routeros.py)."""

from datetime import datetime, timedelta, timezone

import pytest

import db as db_module
import poller
from collectors import mikrotik
from collectors import mikrotik_modules
from fake_routeros import FakeRouterOS, attrs, module_rows


def _config(port):
    return {"ip": "127.0.0.1", "port": port, "user": "api", "password": "secret", "ssl": False}


def _plan(layers=("fast", "medium", "slow"), **overrides):
    plan = {"layers": list(layers), "modules": {}, "capabilities": {}, "log_cursor": {}}
    plan.update(overrides)
    return plan


def _collect(server, **plan_kwargs):
    port = server.start()
    try:
        return mikrotik.check_mikrotik(_config(port), plan=_plan(**plan_kwargs))
    finally:
        server.stop()


def test_v7_full_modules_and_security():
    server = FakeRouterOS(
        rows=module_rows(),
        counts={"/ip/firewall/connection/print": 1234},
    )
    result = _collect(server)

    assert result["ok"] is True
    assert result["module_ok"] is True
    counts = result["module_counts"]
    assert counts["ppp_active"] == 2
    assert counts["hotspot_active"] == 1
    assert counts["wireless_clients"] == 1
    assert counts["dhcp_leases"] == 2
    assert counts["routes"] == 2
    assert counts["routes6"] == 1
    assert counts["connections"] == 1234
    assert result["modules"]["connections"]["rows"] == []

    system = result["system"]
    assert system["major"] == 7
    assert system["firmware_status"] == "pending"
    assert system["router_utc"] is not None
    assert system["drift_seconds"] is not None
    assert {p["name"] for p in system["packages"]} == {"routeros", "wireless"}

    findings = result["modules"]["security"]["rows"]
    categories = {finding["category"] for finding in findings}
    assert "insecure_service" in categories
    assert "service_open" in categories
    assert "default_admin" in categories
    assert "dns_remote" in categories
    assert "ntp_disabled" in categories
    assert "firmware_pending" in categories
    assert "stale_user" in categories


def test_v7_wifi_variant_fallback_and_bgp_peer():
    rows = module_rows(**{
        "/interface/wifi/registration-table/print": [
            # signal (tanpa -strength) khas paket wifi v7
            attrs(**{
                ".id": "*41", "interface": "wifi1", "mac-address": "AA:BB:CC:DD:EE:21",
                "signal": "-55", "tx-rate": "600Mbps", "rx-rate": "600Mbps", "uptime": "2h",
            }),
        ],
        "/routing/bgp/peer/print": [
            attrs(**{
                ".id": "*82", "name": "peer-v6", "remote-address": "10.0.0.2",
                "remote-as": "64513", "state": "established", "uptime": "2d",
                "prefix-count": 10, "disabled": "false",
            }),
        ],
    })
    server = FakeRouterOS(
        rows=rows,
        traps={
            "/interface/wireless/registration-table/print": "no such command",
            "/routing/bgp/session/print": "no such command",
        },
    )
    result = _collect(server)
    caps = result["capabilities"]["probes"]
    assert caps["wireless"]["variant"] == "wifi"
    assert caps["wireless"]["path"] == "/interface/wifi/registration-table/print"
    assert caps["bgp"]["variant"] == "peer"
    assert caps["bgp"]["path"] == "/routing/bgp/peer/print"
    assert result["modules"]["wireless"]["count"] == 1
    assert result["modules"]["wireless"]["rows"][0]["signal_dbm"] == -55.0
    assert result["modules"]["bgp"]["rows"][0]["remote_address"] == "10.0.0.2"


def test_unsupported_probe_cached_with_retry_after():
    server = FakeRouterOS(
        rows=module_rows(),
        traps={"/ip/hotspot/active/print": "no such command"},
    )
    port = server.start()
    try:
        result = mikrotik.check_mikrotik(_config(port), plan=_plan(layers=("fast",)))
        probe = result["capabilities"]["probes"]["hotspot"]
        assert probe["supported"] is False
        assert probe["reason"] == "unsupported"
        assert probe["retry_after"] is not None
        # Kumpulan kapabilitas yang sama dipakai ulang: path tidak diprobe lagi.
        before = list(server.commands).count("/ip/hotspot/active/print")
        second = mikrotik.check_mikrotik(
            _config(port),
            plan=_plan(layers=("fast",), capabilities=result["capabilities"]),
        )
        after = list(server.commands).count("/ip/hotspot/active/print")
        assert second["capabilities"]["probes"]["hotspot"]["supported"] is False
        assert after == before
    finally:
        server.stop()


def test_permission_probe_reports_no_permission():
    server = FakeRouterOS(
        rows=module_rows(),
        permissions={"/user/print": "not allowed"},
    )
    result = _collect(server, layers=("slow",))
    probe = result["capabilities"]["probes"]["users"]
    assert probe["supported"] is False
    assert probe["reason"] == "no_permission"
    assert result["modules"]["users"]["ok"] is False
    assert result["modules"]["users"]["reason"] == "no_permission"


@pytest.mark.parametrize(
    "message,expected",
    [
        ("login failure for user admin from 192.168.1.5 via winbox", ("admin", "192.168.1.5", "winbox")),
        ("login failed for user 'operator' from 10.0.0.9 via api", ("'operator'", "10.0.0.9", "api")),
        ("user admin authentication failed from 10.0.0.10 via ssh", ("admin", "10.0.0.10", "ssh")),
        ("failure from 172.16.0.4 via telnet", (None, "172.16.0.4", "telnet")),
        ("login failure for user admin from 2001:db8::1 via winbox", ("admin", "2001:db8::1", "winbox")),
    ],
)
def test_parse_login_failure_variants(message, expected):
    parsed = mikrotik_modules.parse_login_failure(message)
    assert parsed is not None
    assert (parsed["username"], parsed["source_ip"], parsed["service"]) == expected


def test_parse_login_failure_ignores_unrelated_message():
    assert mikrotik_modules.parse_login_failure("dhcp lease added") is None


def test_truncation_marker_and_scoped_count():
    rows = module_rows(**{
        "/ppp/active/print": [
            attrs(**{
                ".id": f"*3{i}", "name": f"pelanggan{i}", "service": "pppoe",
                "caller-id": f"AA:BB:CC:DD:EE:0{i}", "address": f"10.10.10.1{i}",
            })
            for i in range(3)
        ],
    })
    server = FakeRouterOS(rows=rows)
    result = _collect(server, max_rows=2)
    assert result["modules"]["ppp"]["truncated"] is True
    assert result["module_truncated"]["ppp"] is True
    assert len(result["modules"]["ppp"]["rows"]) == 2


def test_firmware_status_and_pool_capacity():
    assert mikrotik_modules.firmware_status("7.12.1", "7.12.1") == "up-to-date"
    assert mikrotik_modules.firmware_status("7.12.1", "6.49.7") == "pending"
    assert mikrotik_modules.firmware_status("7.12.1", "6.49.7", "7.12.1") == "pending"
    assert mikrotik_modules._pool_capacity("192.168.1.10-192.168.1.20") == 11
    assert mikrotik_modules._pool_capacity("192.168.1.10-20,192.168.2.1") == 12


def test_neighbor_down_pool_high_and_sessions_drop_helpers():
    assert mikrotik_modules.neighbor_down([{"state": "established"}]) == []
    assert len(mikrotik_modules.neighbor_down([{"state": "idle"}])) == 1
    assert len(mikrotik_modules.neighbor_down([{}])) == 1
    assert len(mikrotik_modules.pool_high([{"usage_pct": 95}], 90)) == 1
    assert mikrotik_modules.pool_high([{"usage_pct": 50}], 90) == []
    assert mikrotik_modules.sessions_drop(100, 60, 30) == 40.0
    assert mikrotik_modules.sessions_drop(100, 80, 30) is None
    assert mikrotik_modules.sessions_drop(4, 1, 30) is None


def test_write_allowlist_and_management_interface_refusal():
    with pytest.raises(mikrotik_modules.ActionError) as exc:
        mikrotik_modules.perform_action({}, "free_form_command", {"target": "x"})
    assert exc.value.code == "not_allowed"

    assert mikrotik_modules.find_management_interface(
        "192.168.30.1", [{"address": "192.168.30.1/24", "interface": "ether1"}]
    ) == "ether1"
    assert mikrotik_modules.find_management_interface(
        "192.168.30.1", [{"address": "10.0.0.1/24", "interface": "ether2"}]
    ) is None


# ---------------------------------------------------------
# Persistensi poller & problem baru
# ---------------------------------------------------------


def _device_payload(name="mt-test", ip="10.9.0.1"):
    return {
        "name": name, "ip": ip, "type": "mikrotik", "location": "",
        "poll_interval": 60, "group_id": None, "parent_id": None,
        "maintenance": False, "snmp_enabled": False, "snmp_version": "v2c",
        "snmp_port": 161, "snmp_v3_auth_proto": "SHA", "snmp_v3_priv_proto": "AES",
        "mikrotik_enabled": True, "mikrotik_port": 8728,
        "mikrotik_user": "api", "mikrotik_password": None, "mikrotik_ssl": False,
    }


def _apply_result(device, *, ppp_count=2, drift=0, neighbor_state="idle", pool_usage=95):
    log_rows = [
        {
            "row_key": f"log:{index}", "log_time": "07:02:00", "topics": "system,error",
            "message": "login failure for user admin from 192.168.30.99 via winbox",
            "severity": "error", "is_login_failure": 1, "username": "admin",
            "source_ip": "192.168.30.99", "service": "winbox",
        }
        for index in range(10)
    ]
    result = {
        "capabilities": {"version": "7.12.1", "major": 7, "packages": [], "probes": {}},
        "system": {
            "identity": device["name"], "version": "7.12.1", "major": 7,
            "current_firmware": "6.49.7", "upgrade_firmware": "7.12.1",
            "firmware_status": "pending", "drift_seconds": drift,
        },
        "modules": {
            "interfaces": {"ok": True, "rows": [
                {"name": "ether1", "row_key": "if:ether1", "running": True, "disabled": False},
                {"name": "ether2", "row_key": "if:ether2", "running": False, "disabled": False},
            ]},
            "ppp": {"ok": True, "rows": [], "count": ppp_count},
            "logs": {"ok": True, "rows": log_rows, "count": len(log_rows), "cursor": {}},
            "security": {"ok": True, "rows": [{
                "finding_key": "insecure_service:telnet", "category": "insecure_service",
                "severity": "high", "title": "Layanan Telnet aktif",
                "detail": "Telnet terbuka", "remediation": "Nonaktifkan telnet.",
            }]},
            "bgp": {"ok": True, "rows": [{
                "row_key": "bgp:1", "protocol": "bgp", "name": "peer",
                "remote_address": "10.0.0.1", "state": neighbor_state, "disabled": False,
            }]},
            "dhcp_pools": {"ok": True, "rows": [{
                "row_key": "pool:1", "name": "pool-dhcp", "ranges": "10.0.0.1-10.0.0.10",
                "capacity": 10, "used": 9, "usage_pct": pool_usage,
            }]},
        },
        "module_counts": {"ppp_active": ppp_count},
        "layer_status": {
            "fast": {"ok": True, "errors": []},
            "medium": {"ok": True, "errors": []},
            "slow": {"ok": True, "errors": []},
        },
        "truncated": {},
        "log_cursor": {},
        "probe_errors": [],
    }
    plan = {
        "layers": ["fast", "medium", "slow"],
        "modules": {},
        "important_interfaces": ["ether1", "ether2"],
        "capabilities": {},
    }
    poller._mt_apply_result(device, result, plan, False)


def test_poller_persists_and_opens_all_mikrotik_problems(fresh_db):
    device_id = fresh_db.create_device(_device_payload())
    device = fresh_db.get_device(device_id)
    now = datetime.now(timezone.utc)
    # Nilai sesi sebelumnya 5 menit lalu agar deteksi penurunan bekerja.
    fresh_db.record_mt_metrics(
        device_id,
        (now - timedelta(minutes=6)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        {"ppp_active": 10},
    )
    _apply_result(device, ppp_count=2, drift=300, neighbor_state="idle", pool_usage=95)

    kinds = {row["kind"] for row in fresh_db.list_problems("open", 50)}
    assert {
        "mt_login_bruteforce", "mt_service_insecure", "mt_firmware_pending",
        "mt_time_drift", "mt_interface_down", "mt_routing_neighbor_down",
        "mt_dhcp_pool_high", "mt_sessions_drop",
    }.issubset(kinds)

    assert fresh_db.mt_snapshot_updated_at("mt_interfaces", device_id) is not None
    assert fresh_db.mt_snapshot_updated_at("mt_interfaces", device_id) is not None
    assert fresh_db.list_mt_rows("mt_ppp_sessions", device_id) == []
    findings = fresh_db.list_mt_findings(device_id)
    assert any(row["finding_key"] == "insecure_service:telnet" for row in findings)
    logs = fresh_db.list_mt_logs(device_id)[0]
    assert logs and logs[0]["is_login_failure"] == 1
    state = fresh_db.get_mt_poll_state(device_id, "fast")
    assert state is not None and state["last_ok"] is not None
    assert fresh_db.mt_device_counts(device_id)["ppp_active"] == 0
