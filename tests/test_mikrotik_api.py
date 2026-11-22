"""API modul MikroTik: fleet, tab, redaksi viewer, settings, temuan, topologi, aksi."""

import json
from datetime import datetime, timezone

import pytest
from werkzeug.security import generate_password_hash

import db as db_module


def _device_payload(name="mt-api", ip="10.20.0.1", enabled=True):
    return {
        "name": name, "ip": ip, "type": "mikrotik", "location": "",
        "poll_interval": 60, "group_id": None, "parent_id": None,
        "maintenance": False, "snmp_enabled": False, "snmp_version": "v2c",
        "snmp_port": 161, "snmp_v3_auth_proto": "SHA", "snmp_v3_priv_proto": "AES",
        "mikrotik_enabled": enabled, "mikrotik_port": 8728,
        "mikrotik_user": "api", "mikrotik_password": None, "mikrotik_ssl": False,
    }


def _caps(supported=True, **probe_overrides):
    probes = {}
    for key in (
        "addresses", "clock", "ppp", "hotspot", "wireless", "logs", "dhcp",
        "dhcp_pools", "dhcp_servers", "arp", "neighbors", "routes", "routes6",
        "bgp", "ospf", "firewall_filter", "firewall_nat", "firewall_mangle",
        "connections", "address_lists", "queue_simple", "queue_tree", "services",
        "users", "dns", "ntp", "packages", "routerboard", "license",
    ):
        probes[key] = {
            "supported": supported, "path": f"/{key}/print", "variant": None,
            "reason": None if supported else "unsupported",
            "detail": None if supported else "no such command",
            "checked_at": "2026-11-10T00:00:00Z",
            "retry_after": None if supported else "2026-11-10T06:00:00Z",
        }
    probes.update(probe_overrides)
    return {
        "version": "7.12.1", "major": 7, "packages": [],
        "checked_at": "2026-11-10T00:00:00Z", "probes": probes,
    }


def _seed_device(fresh_db, *, supported=True, **probe_overrides):
    device_id = fresh_db.create_device(_device_payload())
    fresh_db.save_mt_capabilities(device_id, _caps(supported=supported, **probe_overrides))
    fresh_db.save_mt_system(device_id, {
        "identity": "mt-api", "model": "RB4011iGS+", "serial": "SN1",
        "version": "7.12.1", "major": 7, "board": "RB4011",
        "architecture": "arm", "current_firmware": "6.49.7",
        "upgrade_firmware": "7.12.1", "firmware_status": "pending",
        "license_level": "6", "packages": [],
    })
    ts = "2026-11-10T00:00:00Z"
    fresh_db.replace_mt_snapshot("mt_ppp_sessions", device_id, [
        {"row_key": "ppp:1", "name": "pelanggan1", "address": "10.20.0.10",
         "caller_id": "AA:BB:CC:DD:EE:01", "service": "pppoe"},
        {"row_key": "ppp:2", "name": "pelanggan2", "address": "10.20.0.11",
         "caller_id": "AA:BB:CC:DD:EE:02", "service": "pppoe"},
    ], ts)
    fresh_db.replace_mt_snapshot("mt_hotspot_sessions", device_id, [
        {"row_key": "hotspot:1", "user": "hotspot1", "address": "172.16.0.10",
         "mac": "AA:BB:CC:DD:EE:10"},
    ], ts)
    fresh_db.replace_mt_snapshot("mt_interfaces", device_id, [
        {"row_key": "if:ether1", "name": "ether1", "type": "ether",
         "running": True, "disabled": False},
        {"row_key": "if:ether2", "name": "ether2", "type": "ether",
         "running": False, "disabled": False},
    ], ts)
    fresh_db.replace_mt_snapshot("mt_routes", device_id, [
        {"row_key": "route-ipv4:1", "family": "ipv4", "dst_address": "0.0.0.0/0",
         "gateway": "10.20.0.254", "active": True, "disabled": False},
    ], ts)
    fresh_db.replace_mt_snapshot("mt_neighbors", device_id, [
        {"row_key": "neighbor:1", "identity": "switch-tetangga", "platform": "MikroTik",
         "address": "10.20.0.2", "interface": "ether1", "mac": "AA:BB:CC:00:00:02"},
    ], ts)
    return device_id


def _set_device_fields(fresh_db, device_id, **fields):
    assignments = ", ".join(f"{key}=?" for key in fields)
    with fresh_db._lock:
        conn = fresh_db.get_conn()
        conn.execute(f"UPDATE devices SET {assignments} WHERE id=?", (*fields.values(), device_id))
        conn.commit()


def _login(client, username, password):
    response = client.post("/api/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.get_json()
    token = response.get_json().get("csrf_token")
    client.environ_base["HTTP_X_CSRF_TOKEN"] = token


def _make_user(fresh_db, username, role, password="test-password-123"):
    fresh_db.create_user(username, generate_password_hash(password), role=role, must_change_password=0)
    return username, password


@pytest.fixture()
def app_module(fresh_db):
    import app as module

    module.app.config["TESTING"] = True
    return module


def _client(app_module, fresh_db, role):
    username, password = _make_user(fresh_db, f"{role}-test", role)
    client = app_module.app.test_client()
    _login(client, username, password)
    return client


def test_fleet_summary_and_filter_options(app_module, fresh_db):
    device_id = _seed_device(fresh_db)
    _set_device_fields(fresh_db, device_id, routeros_version="7.12.1", mikrotik_model="RB4011iGS+")
    client = _client(app_module, fresh_db, "operator")

    summary = client.get("/api/mikrotik/summary").get_json()["summary"]
    assert summary["routers"] == 1
    assert summary["firmware_pending"] == 1

    fleet = client.get("/api/mikrotik/devices").get_json()
    assert fleet["total"] == 1
    assert fleet["devices"][0]["name"] == "mt-api"
    assert fleet["devices"][0]["system"]["version"] == "7.12.1"

    filtered = client.get("/api/mikrotik/devices?firmware=pending").get_json()
    assert filtered["total"] == 1
    filtered = client.get("/api/mikrotik/devices?firmware=up-to-date").get_json()
    assert filtered["total"] == 0

    options = client.get("/api/mikrotik/filter-options").get_json()
    assert options["versions"] == ["7.12.1"]
    assert options["models"] == ["RB4011iGS+"]


def test_overview_and_tab_data_operator(app_module, fresh_db):
    device_id = _seed_device(fresh_db)
    client = _client(app_module, fresh_db, "operator")

    overview = client.get(f"/api/mikrotik/devices/{device_id}/overview").get_json()
    assert overview["device"]["name"] == "mt-api"
    assert overview["system"]["firmware_status"] == "pending"
    assert overview["tabs"]["sessions"]["state"] == "ok"
    assert overview["counts"]["ppp_active"] == 2
    assert overview["counts"]["active_sessions"] == 3
    assert overview["settings"]["modules"]["ppp"] is True

    tab = client.get(f"/api/mikrotik/devices/{device_id}/tab/sessions").get_json()
    assert tab["state"]["state"] == "ok"
    ppp = next(section for section in tab["sections"] if section["key"] == "mt_ppp_sessions")
    assert ppp["total"] == 2
    assert ppp["rows"][0]["name"] == "pelanggan1"

    search = client.get(
        f"/api/mikrotik/devices/{device_id}/tab/sessions?search=pelanggan2"
    ).get_json()
    ppp = next(section for section in search["sections"] if section["key"] == "mt_ppp_sessions")
    assert ppp["total"] == 1
    assert ppp["rows"][0]["name"] == "pelanggan2"


def test_viewer_gets_counts_not_customer_details(app_module, fresh_db):
    device_id = _seed_device(fresh_db)
    client = _client(app_module, fresh_db, "viewer")

    tab = client.get(f"/api/mikrotik/devices/{device_id}/tab/sessions").get_json()
    ppp = next(section for section in tab["sections"] if section["key"] == "mt_ppp_sessions")
    assert ppp["redacted"] is True
    assert ppp["total"] == 2
    assert ppp["rows"] == []

    dhcp = client.get(f"/api/mikrotik/devices/{device_id}/tab/dhcp").get_json()
    lease = next(section for section in dhcp["sections"] if section["key"] == "mt_dhcp_leases")
    assert lease["redacted"] is True
    assert lease["rows"] == []

    overview = client.get(f"/api/mikrotik/devices/{device_id}/overview").get_json()
    assert overview["settings"].get("write_user") is None
    assert "actions" not in overview


def test_unsupported_and_api_disabled_states(app_module, fresh_db):
    device_id = _seed_device(
        fresh_db,
        routes={"supported": False, "path": None, "reason": "no_permission",
                "detail": "not allowed", "checked_at": "2026-11-10T00:00:00Z"},
        routes6={"supported": False, "path": None, "reason": "no_permission",
                 "detail": "not allowed", "checked_at": "2026-11-10T00:00:00Z"},
        bgp={"supported": False, "path": None, "reason": "unsupported",
             "detail": "no such command", "checked_at": "2026-11-10T00:00:00Z"},
        ospf={"supported": False, "path": None, "reason": "unsupported",
              "detail": "no such command", "checked_at": "2026-11-10T00:00:00Z"},
    )
    client = _client(app_module, fresh_db, "operator")
    tab = client.get(f"/api/mikrotik/devices/{device_id}/tab/routing").get_json()
    assert tab["state"]["state"] == "no_permission"

    fresh_db.save_mt_capabilities(device_id, _caps())
    _set_device_fields(fresh_db, device_id, mikrotik_enabled=0)
    overview = client.get(f"/api/mikrotik/devices/{device_id}/overview").get_json()
    assert overview["tabs"]["routing"]["state"] == "api_disabled"


def test_finding_ignore_requires_reason_and_is_audited(app_module, fresh_db):
    device_id = _seed_device(fresh_db)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    fresh_db.replace_mt_findings(device_id, [{
        "finding_key": "insecure_service:telnet", "category": "insecure_service",
        "severity": "high", "title": "Layanan Telnet aktif",
        "detail": "Telnet terbuka", "remediation": "Nonaktifkan telnet.",
    }], detected_at=now)
    finding = fresh_db.list_mt_findings(device_id)[0]
    client = _client(app_module, fresh_db, "operator")

    response = client.post(
        f"/api/mikrotik/devices/{device_id}/findings/{finding['id']}/ignore",
        json={"ignored": True, "reason": ""},
    )
    assert response.status_code == 400
    response = client.post(
        f"/api/mikrotik/devices/{device_id}/findings/{finding['id']}/ignore",
        json={"ignored": True, "reason": "Layanan sengaja aktif untuk uji"},
    )
    assert response.status_code == 200
    stored = fresh_db.get_mt_finding(finding["id"])
    assert stored["ignored"] == 1
    assert stored["ignored_by"] == "operator-test"
    audit = fresh_db.list_audit_log(action="mikrotik.finding.ignore")
    assert any(row["action"] == "mikrotik.finding.ignore" for row in audit["entries"])

    response = client.post(
        f"/api/mikrotik/devices/{device_id}/findings/{finding['id']}/ignore",
        json={"ignored": False},
    )
    assert response.status_code == 200
    assert fresh_db.get_mt_finding(finding["id"])["ignored"] == 0


def test_settings_operator_and_admin_permissions(app_module, fresh_db):
    device_id = _seed_device(fresh_db)
    operator = _client(app_module, fresh_db, "operator")

    assert operator.put(
        f"/api/mikrotik/devices/{device_id}/settings",
        json={"modules": {"ppp": False}},
    ).status_code == 400
    response = operator.put(
        f"/api/mikrotik/devices/{device_id}/settings",
        json={"important_interfaces": ["ether1", "ether2"]},
    )
    assert response.status_code == 200
    assert fresh_db.get_mt_device_settings(device_id)["important_interfaces"] == ["ether1", "ether2"]

    admin = _client(app_module, fresh_db, "admin")
    response = admin.put(
        f"/api/mikrotik/devices/{device_id}/settings",
        json={"modules": {"ppp": False, "hotspot": True}, "intervals": {"fast": 90}},
    )
    assert response.status_code == 200
    settings = fresh_db.get_mt_device_settings(device_id)
    assert settings["modules"]["ppp"] is False
    assert settings["intervals"]["fast"] == 90

    response = admin.put(
        "/api/settings", json={"mt_write_actions_enabled": True, "mt_bruteforce_threshold": 5}
    )
    assert response.status_code == 200
    assert fresh_db.get_setting("mt_write_actions_enabled") == "1"
    assert fresh_db.get_setting("mt_bruteforce_threshold") == "5"
    response = admin.put("/api/settings", json={"mt_pool_high_pct": 101})
    assert response.status_code == 400


def test_topology_suggestions_and_apply(app_module, fresh_db):
    device_id = _seed_device(fresh_db)
    switch_id = fresh_db.create_device(_device_payload(name="switch-tetangga", ip="10.20.0.2"))
    _set_device_fields(fresh_db, switch_id, type="switch")
    client = _client(app_module, fresh_db, "operator")

    data = client.get("/api/mikrotik/topology/suggestions").get_json()
    assert len(data["suggestions"]) == 1
    suggestion = data["suggestions"][0]
    assert suggestion["child_id"] == switch_id
    assert suggestion["parent_id"] == device_id
    assert suggestion["confidence"] == "tinggi"

    response = client.post(
        "/api/mikrotik/topology/apply",
        json={"child_id": switch_id, "parent_id": device_id},
    )
    assert response.status_code == 200
    assert fresh_db.get_device(switch_id)["parent_id"] == device_id

    response = client.post(
        "/api/mikrotik/topology/apply",
        json={"child_id": switch_id, "parent_id": 99999},
    )
    assert response.status_code == 400


def test_fleet_report_csv(app_module, fresh_db):
    _seed_device(fresh_db)
    client = _client(app_module, fresh_db, "operator")
    response = client.get("/api/mikrotik/reports/fleet.csv")
    assert response.status_code == 200
    text = response.get_data(as_text=True)
    assert "Perangkat" in text
    assert "mt-api" in text
    assert "pending" in text


def test_write_action_requires_toggle_credentials_and_confirmation(app_module, fresh_db, monkeypatch):
    device_id = _seed_device(fresh_db)
    client = _client(app_module, fresh_db, "operator")

    response = client.post(
        f"/api/mikrotik/devices/{device_id}/actions",
        json={"action": "reboot", "confirm_name": "mt-api"},
    )
    assert response.status_code == 403

    fresh_db.set_settings({"mt_write_actions_enabled": "1"})
    response = client.post(
        f"/api/mikrotik/devices/{device_id}/actions",
        json={"action": "reboot", "confirm_name": "mt-api"},
    )
    assert response.status_code == 403  # belum ada kredensial tulis

    fresh_db.save_mt_device_settings(device_id, {
        "write_enabled": True, "write_user": "api-write", "write_password": "rahasia",
    })
    response = client.post(
        f"/api/mikrotik/devices/{device_id}/actions",
        json={"action": "reboot", "confirm_name": "salah"},
    )
    assert response.status_code == 400

    captured = {}

    def fake_action(config, action, params):
        captured.update({"config": config, "action": action, "params": params})
        return {
            "ok": True, "action": action, "target": params["target"],
            "before": None, "after": None, "verified": None,
            "detail": "Perintah reboot dikirim (uji).",
        }

    monkeypatch.setattr(app_module.mikrotik_modules, "perform_action", fake_action)
    response = client.post(
        f"/api/mikrotik/devices/{device_id}/actions",
        json={"action": "reboot", "confirm_name": "mt-api"},
    )
    assert response.status_code == 200
    assert captured["config"]["password"] == "rahasia"
    assert captured["action"] == "reboot"
    actions = fresh_db.list_mt_actions(device_id)
    assert actions and actions[0]["action"] == "reboot"
    assert actions[0]["username"] == "operator-test"

    response = client.post(
        f"/api/mikrotik/devices/{device_id}/actions",
        json={"action": "free_form", "target": "x"},
    )
    assert response.status_code == 400
