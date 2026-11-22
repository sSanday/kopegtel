"""E1 — penerima syslog & SNMP trap: parser, dedup, aturan, retensi, RBAC.

Data tiruan hanya dibuat di sini; tidak ada data contoh di aplikasi.
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest
from werkzeug.security import generate_password_hash

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, "backend"))

import db as db_module  # noqa: E402
import app as app_module  # noqa: E402

from ingest import rules as ingest_rules  # noqa: E402
from ingest.service import BoundedBatcher, IngestManager, RateLimiter  # noqa: E402
from ingest import traps as trap_module  # noqa: E402
from ingest import parsers  # noqa: E402


def _mk_device(db, name, ip, **overrides):
    payload = {
        "name": name,
        "ip": ip,
        "type": "mikrotik",
        "location": "",
        "poll_interval": 60,
        "group_id": None,
        "parent_id": None,
        "maintenance": False,
        "snmp_enabled": False,
        "snmp_version": "v2c",
        "snmp_port": 161,
        "snmp_v3_auth_proto": "SHA",
        "snmp_v3_priv_proto": "AES",
        "mikrotik_enabled": False,
        "mikrotik_port": 8728,
        "mikrotik_user": None,
        "mikrotik_password": None,
        "mikrotik_ssl": False,
    }
    payload.update(overrides)
    return db.create_device(payload)


def _mk_user(db, username, password, role="admin"):
    db.create_user(username, generate_password_hash(password), role=role, must_change_password=0)


def _ts(dt=None):
    return (dt or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _login(client, username, password):
    return client.post("/api/login", json={"username": username, "password": password})


@pytest.fixture()
def client():
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as client:
        yield client


# ---------------------------------------------------------
# Parser syslog
# ---------------------------------------------------------

def test_parse_syslog_rfc3164():
    parsed = parsers.parse_syslog("<134>Nov 11 07:00:00 MikroTik ether2 link down")
    assert parsed["facility"] == 16
    assert parsed["severity"] == 6
    assert parsed["hostname"] == "MikroTik"
    assert "ether2" in parsed["message"]


def test_parse_syslog_rfc5424():
    parsed = parsers.parse_syslog(
        "<29>1 2026-11-10T20:00:05.123+00:00 mylab system 1234 - - reboot requested"
    )
    assert parsed["facility"] == 3
    assert parsed["severity"] == 5
    assert parsed["hostname"] == "mylab"
    assert parsed["ts"].startswith("2026-11-10T20:00:05")
    assert parsed["message"] == "reboot requested"


def test_parse_syslog_plain_routeros():
    parsed = parsers.parse_syslog("ether2: link down")
    assert parsed["facility"] == 1
    assert parsed["severity"] == 6
    assert parsed["tag"] == "ether2"
    assert parsed["message"] == "link down"


def test_parse_syslog_truncates_at_8kb():
    parsed = parsers.parse_syslog("<134>" + "A" * 20000)
    assert len(parsed["message"]) == parsers.MAX_MESSAGE_BYTES
    assert len(parsed["raw"]) == parsers.MAX_MESSAGE_BYTES


def test_parse_syslog_empty_rejected():
    assert parsers.parse_syslog("") is None
    assert parsers.parse_syslog("   ") is None


# ---------------------------------------------------------
# Rate limiter & batcher
# ---------------------------------------------------------

def test_rate_limiter_window():
    limiter = RateLimiter(limit=2, window_seconds=1)
    base = 100.0
    assert limiter.allow("src", now=base) is True
    assert limiter.allow("src", now=base + 0.1) is True
    assert limiter.allow("src", now=base + 0.2) is False
    assert limiter.allow("src", now=base + 1.1) is True


def test_bounded_batcher_merges_inserts():
    inserted = []
    batcher = BoundedBatcher("test", lambda rows: inserted.extend(rows), size=10, batch_size=2, flush_seconds=0.2)
    for index in range(5):
        assert batcher.put({"n": index}) is True
    batcher._flush()
    assert [row["n"] for row in inserted] == [0, 1, 2, 3, 4]
    assert batcher.stats()["written"] == 5


# ---------------------------------------------------------
# Ingest manager: sumber tak dikenal, rate limit, batch insert
# ---------------------------------------------------------

def test_unknown_source_dropped(fresh_db):
    manager = IngestManager()
    manager.process_syslog_datagram(b"<134>ether2: down", "192.0.2.99")
    assert manager.status()["syslog_udp"]["dropped_unknown"] == 1
    assert fresh_db.ingest_table_counts()["syslog_events"] == 0


def test_registered_source_accepted_and_batched(fresh_db):
    _mk_device(fresh_db, "router-e1", "192.0.2.10")
    manager = IngestManager()
    manager.process_syslog_datagram(b"<134>Nov 11 07:00:00 router ether1: up", "192.0.2.10")
    manager.flush_now()
    counts = fresh_db.ingest_table_counts()
    assert counts["syslog_events"] == 1


def test_rate_limited_source_dropped(fresh_db):
    _mk_device(fresh_db, "router-flood", "192.0.2.11")
    manager = IngestManager()
    manager._rate = RateLimiter(limit=1, window_seconds=1)
    manager.process_syslog_datagram(b"<134>first: down", "192.0.2.11")
    manager.process_syslog_datagram(b"<134>second: down", "192.0.2.11")
    stats = manager.status()["syslog_udp"]
    assert stats["dropped_rate"] == 1
    problem = fresh_db.get_conn().execute(
        "SELECT kind, message FROM problems WHERE kind='ingest_flood'"
    ).fetchone()
    assert problem is not None
    assert "router-flood" in problem["message"]


# ---------------------------------------------------------
# Trap v2c / v1
# ---------------------------------------------------------

def _encode_v2c_trap(community="nms", trap_oid="1.3.6.1.6.3.1.1.5.3", request_id=101, if_index=2):
    from pyasn1.codec.ber import encoder
    from pysnmp.proto.api import v2c

    message = v2c.Message()
    v2c.apiMessage.set_defaults(message)
    v2c.apiMessage.set_community(message, community)
    pdu = v2c.SNMPv2TrapPDU()
    v2c.apiPDU.set_defaults(pdu)
    v2c.apiPDU.set_request_id(pdu, request_id)
    varbinds = [(v2c.ObjectIdentifier("1.3.6.1.6.3.1.1.4.1.0"), v2c.ObjectIdentifier(trap_oid))]
    if if_index is not None:
        varbinds.append((v2c.ObjectIdentifier("1.3.6.1.2.1.2.2.1.1"), v2c.Integer(if_index)))
    v2c.apiPDU.set_varbinds(pdu, varbinds)
    v2c.apiMessage.set_pdu(message, pdu)
    return encoder.encode(message)


def _encode_v1_trap(community="nms", generic=2):
    from pyasn1.codec.ber import encoder
    from pysnmp.proto.api import v1

    message = v1.Message()
    v1.apiMessage.set_defaults(message)
    v1.apiMessage.set_community(message, community)
    pdu = v1.TrapPDU()
    v1.apiTrapPDU.set_defaults(pdu)
    v1.apiTrapPDU.set_enterprise(pdu, v1.ObjectIdentifier("1.3.6.1.4.1.14988"))
    v1.apiTrapPDU.set_generic_trap(pdu, generic)
    v1.apiTrapPDU.set_specific_trap(pdu, 0)
    v1.apiTrapPDU.set_timestamp(pdu, 0)
    v1.apiTrapPDU.set_varbinds(
        pdu, [(v1.ObjectIdentifier("1.3.6.1.2.1.2.2.1.1"), v1.Integer(2))]
    )
    v1.apiMessage.set_pdu(message, pdu)
    return encoder.encode(message)


def test_decode_v2c_trap_link_down():
    decoded = trap_module.decode_trap(_encode_v2c_trap())
    assert decoded["version"] == "v2c"
    assert decoded["trap_oid"] == "1.3.6.1.6.3.1.1.5.3"
    assert decoded["trap_name"] == "linkDown"
    assert decoded["if_index"] == 2


def test_decode_v1_trap_generic():
    decoded = trap_module.decode_trap(_encode_v1_trap(generic=2))
    assert decoded["version"] == "v1"
    assert decoded["trap_oid"] == "1.3.6.1.6.3.1.1.5.3"
    assert decoded["trap_name"] == "linkDown"


def test_decode_invalid_trap_rejected():
    with pytest.raises(trap_module.TrapDecodeError):
        trap_module.decode_trap(b"\x00\x01not-ber")


def test_inform_response_builds_response_pdu():
    from pyasn1.codec.ber import decoder
    from pysnmp.proto.api import v2c

    from pyasn1.codec.ber import encoder  # noqa: F401

    message = v2c.Message()
    v2c.apiMessage.set_defaults(message)
    v2c.apiMessage.set_community(message, "nms")
    pdu = v2c.InformRequestPDU()
    v2c.apiPDU.set_defaults(pdu)
    v2c.apiPDU.set_request_id(pdu, 777)
    v2c.apiPDU.set_varbinds(
        pdu, [(v2c.ObjectIdentifier("1.3.6.1.6.3.1.1.4.1.0"), v2c.ObjectIdentifier("1.3.6.1.6.3.1.1.5.1"))]
    )
    v2c.apiMessage.set_pdu(message, pdu)
    from pyasn1.codec.ber import encoder as ber_encoder

    data = ber_encoder.encode(message)
    decoded = trap_module.decode_trap(data)
    assert decoded["is_inform"] is True
    response = trap_module.build_inform_response(decoded)
    parsed, rest = decoder.decode(response, asn1Spec=v2c.Message())
    assert rest == b""
    assert str(parsed["data"].getComponent().tagSet).endswith("32:2>")
    assert v2c.apiPDU.get_request_id(parsed["data"].getComponent()) == 777


def _trap_manager(fresh_db, communities, allow_v1=False):
    _mk_device(fresh_db, "router-trap", "192.0.2.20")
    fresh_db.update_ingest_receiver(
        "snmp_trap", enabled=False, port=1162, bind_address="0.0.0.0",
        config={"communities": communities, "allow_v1": allow_v1},
    )
    manager = IngestManager()
    manager.reload()  # hanya membaca konfigurasi; port tidak dibuka (enabled=False)
    return manager


def test_trap_wrong_community_dropped(fresh_db):
    manager = _trap_manager(fresh_db, ["nms"])
    manager.process_trap_datagram(_encode_v2c_trap(community="salah"), "192.0.2.20")
    assert manager.status()["snmp_trap"]["dropped_auth"] == 1
    manager.flush_now()
    assert fresh_db.ingest_table_counts()["snmp_traps"] == 0


def test_trap_v1_rejected_unless_allowed(fresh_db):
    manager = _trap_manager(fresh_db, ["nms"], allow_v1=False)
    manager.process_trap_datagram(_encode_v1_trap(community="nms"), "192.0.2.20")
    assert manager.status()["snmp_trap"]["dropped_auth"] == 1


def test_trap_link_down_maps_interface_name(fresh_db):
    manager = _trap_manager(fresh_db, ["nms"], allow_v1=True)
    device = fresh_db.get_device_by_ip("192.0.2.20")
    assert device is not None
    with fresh_db._lock:
        conn = fresh_db.get_conn()
        conn.execute(
            "INSERT INTO mt_interfaces (device_id, row_key, polled_at, stale, data) VALUES (?,?,?,0,?)",
            (device["id"], "log:1", _ts(), json.dumps({"name": "ether2", "if_index": 2})),
        )
        conn.commit()
    manager.process_trap_datagram(_encode_v2c_trap(community="nms"), "192.0.2.20")
    manager.flush_now()
    with fresh_db._lock:
        row = fresh_db.get_conn().execute(
            "SELECT trap_oid, trap_name, varbinds FROM snmp_traps"
        ).fetchone()
    assert row["trap_oid"] == "1.3.6.1.6.3.1.1.5.3"
    assert row["trap_name"] == "linkDown"
    # Aturan wajib membuka "Interface link down (trap)" dan menyebut nama interface.
    with fresh_db._lock:
        problem = fresh_db.get_conn().execute(
            "SELECT kind, message FROM problems WHERE kind='trap_rule' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert problem is not None
    assert "ether2" in problem["message"]


# ---------------------------------------------------------
# Aturan: cooldown, auto-resolve, recovery, ReDoS
# ---------------------------------------------------------

def _rule(db, **overrides):
    data = {
        "name": "atur-uji",
        "kind": "syslog",
        "match_type": "contains",
        "pattern": "zzz-token-e1",
        "oid_prefix": "",
        "min_severity": "information",
        "device_id": None,
        "group_id": None,
        "problem_kind": "syslog_rule",
        "problem_severity": "warning",
        "cooldown_seconds": 60,
        "auto_resolve_minutes": 30,
        "recovery_pattern": "",
        "resolves_rule_id": None,
        "enabled": 1,
    }
    data.update(overrides)
    return data


def test_syslog_rule_opens_and_aggregates(fresh_db):
    device_id = _mk_device(fresh_db, "router-rule", "192.0.2.30")
    device = fresh_db.get_device(device_id)
    fresh_db.create_ingest_rule(_rule(fresh_db))
    device = {**device, **fresh_db.get_device_by_ip("192.0.2.30")}
    calls = []

    class _StubAlerts:
        @staticmethod
        def dispatch_problem(problem):
            calls.append(problem)

    import alerts

    original = alerts.dispatch_problem
    alerts.dispatch_problem = _StubAlerts.dispatch_problem
    try:
        from ingest import rules as rules_module

        for _ in range(3):
            rules_module.evaluate_syslog(
                device, {"severity": 5, "message": "zzz-token-e1 admin"}
            )
    finally:
        alerts.dispatch_problem = original

    with fresh_db._lock:
        problem = fresh_db.get_conn().execute(
            "SELECT event_count, message, kind FROM problems WHERE device_id=? AND kind='syslog_rule'",
            (device_id,),
        ).fetchone()
    assert problem["event_count"] == 3
    # Cooldown: notifikasi hanya sekali walau ada 3 kejadian berturut-turut.
    assert len(calls) == 1


def test_syslog_rule_cooldown_releases(fresh_db):
    device_id = _mk_device(fresh_db, "router-cd", "192.0.2.31")
    with fresh_db._lock:
        conn = fresh_db.get_conn()
        conn.execute(
            "INSERT INTO problems (device_id, kind, severity, message, status, started_at, rule_id,"
            " event_count, last_seen_at, notified_at)"
            " VALUES (?, 'syslog_rule', 'warning', 'old', 'open', '2020-01-01T00:00:00Z', 1, 1,"
            " '2020-01-01T00:00:00Z', '2020-01-01T00:00:00Z')",
            (device_id,),
        )
        conn.commit()

    result = fresh_db.record_ingest_problem(
        {"id": 1, "problem_kind": "syslog_rule", "problem_severity": "warning", "cooldown_seconds": 60},
        device_id, "warning", "zzz-token-e1 again",
    )
    assert result["should_notify"] is True
    assert result["is_new"] is False


def test_auto_resolve_closes_stale_problems(fresh_db):
    device_id = _mk_device(fresh_db, "router-resolve", "192.0.2.32")
    rule_id = fresh_db.create_ingest_rule(_rule(fresh_db, name="atur-resolve"))
    with fresh_db._lock:
        conn = fresh_db.get_conn()
        conn.execute(
            "INSERT INTO problems (device_id, kind, severity, message, status, started_at, rule_id,"
            " event_count, last_seen_at, notified_at)"
            " VALUES (?, 'syslog_rule', 'warning', 'stale', 'open', '2020-01-01T00:00:00Z', ?, 1,"
            " '2020-01-01T00:00:00Z', '2020-01-01T00:00:00Z')",
            (device_id, rule_id),
        )
        conn.commit()
    resolved = fresh_db.auto_resolve_ingest_problems()
    assert resolved == 1
    with fresh_db._lock:
        status = fresh_db.get_conn().execute("SELECT status FROM problems").fetchone()["status"]
    assert status == "resolved"


def test_syslog_rule_recovery_pattern_resolves(fresh_db):
    device_id = _mk_device(fresh_db, "router-recovery", "192.0.2.33")
    device = fresh_db.get_device_by_ip("192.0.2.33")
    fresh_db.create_ingest_rule(
        _rule(fresh_db, name="atur-recovery", pattern="zzz-failtoken", recovery_pattern="zzz-oktoken")
    )
    from ingest import rules as rules_module

    rules_module.evaluate_syslog(device, {"severity": 5, "message": "zzz-failtoken user admin"})
    assert fresh_db.reevaluate_suppressed_problems() == []
    actions = rules_module.evaluate_syslog(device, {"severity": 5, "message": "zzz-oktoken user admin"})
    assert actions == 1
    with fresh_db._lock:
        status = fresh_db.get_conn().execute("SELECT status FROM problems").fetchone()["status"]
    assert status == "resolved"


def test_redos_patterns_rejected():
    for bad in ("(a+)+", "(?:a*)*", "((a{1,3})+)", "(" + "a" * 199 + ")+"):
        with pytest.raises(ingest_rules.PatternError):
            ingest_rules.validate_regex_pattern(bad)
    assert ingest_rules.validate_regex_pattern("^ether[0-9]+ link down$") == "^ether[0-9]+ link down$"
    with pytest.raises(ingest_rules.PatternError):
        ingest_rules.validate_regex_pattern("[a-z")


# ---------------------------------------------------------
# Dedup log MikroTik vs syslog, retensi
# ---------------------------------------------------------

def test_mt_logs_deduped_against_syslog(fresh_db):
    device_id = _mk_device(fresh_db, "router-dedup", "192.0.2.40")
    now = _ts()
    minute = now[:16]
    message = "login failure for user admin"
    hash_value = fresh_db.dedup_hash(device_id, minute, message)
    with fresh_db._lock:
        conn = fresh_db.get_conn()
        conn.execute(
            "INSERT INTO syslog_events (ts, received_at, device_id, source_ip, hostname, facility,"
            " severity, tag, message, raw, dedup_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (now, now, device_id, "192.0.2.40", "router", 4, 5, "auth", message, None, hash_value),
        )
        conn.commit()
    rows = [
        {"row_key": "a", "message": message, "polled_at": now},
        {"row_key": "b", "message": "unrelated", "polled_at": now},
    ]
    kept = fresh_db.filter_syslog_duplicates(device_id, rows, polled_at=now)
    assert [row["row_key"] for row in kept] == ["b"]


def test_retention_purges_syslog_and_traps(fresh_db):
    device_id = _mk_device(fresh_db, "router-retention", "192.0.2.41")
    now = _ts()
    old = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    fresh_db.set_settings({"retention_syslog_days": "1", "retention_trap_days": "1"})
    with fresh_db._lock:
        conn = fresh_db.get_conn()
        conn.execute(
            "INSERT INTO syslog_events (ts, received_at, device_id, source_ip, message) VALUES (?,?,?,?,?)",
            (old, old, device_id, "192.0.2.41", "old syslog"),
        )
        conn.execute(
            "INSERT INTO snmp_traps (ts, received_at, device_id, source_ip, trap_oid) VALUES (?,?,?,?,?)",
            (old, old, device_id, "192.0.2.41", "1.3.6.1.6.3.1.1.5.1"),
        )
        conn.commit()
    result = fresh_db.purge_old_data()
    assert result["syslog_deleted"] == 1
    assert result["traps_deleted"] == 1


# ---------------------------------------------------------
# API: RBAC, CSRF, audit, CSV, escape
# ---------------------------------------------------------

def test_events_api_requires_login(client):
    assert client.get("/api/events/syslog").status_code == 401
    assert client.get("/api/events/status").status_code == 401


def test_summary_visible_to_viewer_counts_only(client, fresh_db):
    device_id = _mk_device(fresh_db, "router-viewer", "192.0.2.50")
    now = _ts()
    fresh_db.add_syslog_events_batch([
        {"ts": now, "device_id": device_id, "source_ip": "192.0.2.50",
         "severity": 6, "message": "<script>alert(1)</script>"},
    ])
    _mk_user(fresh_db, "viewere1", "viewer-e1-password", role="viewer")
    assert _login(client, "viewere1", "viewer-e1-password").status_code == 200
    response = client.get("/api/events/summary")
    assert response.status_code == 200
    body = response.get_json()
    assert body["syslog_total"] == 1
    assert "message" not in body
    assert client.get("/api/events/syslog").status_code == 403


def test_operator_can_list_but_not_change_receiver(client, fresh_db, monkeypatch):
    _mk_user(fresh_db, "operatoe1", "operator-e1-password", role="operator")
    r = _login(client, "operatoe1", "operator-e1-password")
    csrf = r.get_json()["csrf_token"]
    assert client.get("/api/events/syslog").status_code == 200
    assert client.get("/api/events/status").status_code == 200
    refused = client.put(
        "/api/events/receiver",
        json={"kind": "syslog_udp", "enabled": True, "port": 5514, "bind_address": "0.0.0.0"},
        headers={"X-CSRF-Token": csrf},
    )
    assert refused.status_code == 403


def test_receiver_put_admin_only_and_port_checked(client, fresh_db, monkeypatch):
    import routes.events as events_routes

    class _StubIngest:
        def reload(self):
            return {"syslog_udp": {"running": False}}

    monkeypatch.setattr(events_routes, "INGEST", _StubIngest())
    _mk_user(fresh_db, "admine1", "admin-e1-password", role="admin")
    _mk_user(fresh_db, "motoperatore1", "op-e1-password", role="operator")
    r = _login(client, "admine1", "admin-e1-password")
    csrf = r.get_json()["csrf_token"]
    bad = client.put("/api/events/receiver", json={"kind": "syslog_udp", "enabled": False, "port": 80, "bind_address": "0.0.0.0"},
                     headers={"X-CSRF-Token": csrf})
    assert bad.status_code == 400
    ok = client.put("/api/events/receiver", json={"kind": "syslog_udp", "enabled": False, "port": 5514, "bind_address": "0.0.0.0"},
                    headers={"X-CSRF-Token": csrf})
    assert ok.status_code == 200
    assert client.put("/api/events/receiver", json={"kind": "syslog_udp", "enabled": False, "port": 5514, "bind_address": "999.1.1.1"},
                      headers={"X-CSRF-Token": csrf}).status_code == 400
    with fresh_db._lock:
        audit = fresh_db.get_conn().execute(
            "SELECT action FROM audit_log WHERE action='events.receiver_update'"
        ).fetchone()
    assert audit is not None


def test_rules_crud_and_audit(client, fresh_db):
    _mk_user(fresh_db, "ruleadmin", "rule-admin-password", role="admin")
    r = _login(client, "ruleadmin", "rule-admin-password")
    csrf = r.get_json()["csrf_token"]
    payload = {
        "name": "aturan-uji-api",
        "kind": "syslog",
        "match_type": "contains",
        "pattern": "login failure",
        "problem_severity": "warning",
        "cooldown_seconds": 60,
        "auto_resolve_minutes": 30,
        "enabled": True,
    }
    created = client.post("/api/events/rules", json=payload, headers={"X-CSRF-Token": csrf})
    assert created.status_code == 201
    rule_id = created.get_json()["id"]
    listed = client.get("/api/events/rules")
    assert any(rule["name"] == "aturan-uji-api" for rule in listed.get_json()["rules"])
    updated = client.put(f"/api/events/rules/{rule_id}", json={**payload, "enabled": False},
                         headers={"X-CSRF-Token": csrf})
    assert updated.status_code == 200
    deleted = client.delete(f"/api/events/rules/{rule_id}", headers={"X-CSRF-Token": csrf})
    assert deleted.status_code == 200
    with fresh_db._lock:
        audits = [
            row["action"]
            for row in fresh_db.get_conn().execute("SELECT action FROM audit_log").fetchall()
            if row["action"].startswith("events.rule")
        ]
    assert "events.rule_create" in audits
    assert "events.rule_update" in audits
    assert "events.rule_delete" in audits


def test_rule_rejects_redos_pattern(client, fresh_db):
    _mk_user(fresh_db, "redos-admin", "redos-admin-password", role="admin")
    r = _login(client, "redos-admin", "redos-admin-password")
    csrf = r.get_json()["csrf_token"]
    response = client.post(
        "/api/events/rules",
        json={"name": "aturan-redos", "kind": "syslog", "match_type": "regex", "pattern": "(a+)+"},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 400


def test_csv_export_escapes_formula(client, fresh_db):
    device_id = _mk_device(fresh_db, "router-csv", "192.0.2.60")
    now = _ts()
    fresh_db.add_syslog_events_batch([
        {"ts": now, "device_id": device_id, "source_ip": "192.0.2.60",
         "severity": 6, "message": "=HYPERLINK(\"http://evil\",\"click\")"},
    ])
    _mk_user(fresh_db, "csvadmin", "csv-admin-password", role="admin")
    _login(client, "csvadmin", "csv-admin-password")
    response = client.get("/api/events/syslog.csv")
    assert response.status_code == 200
    assert "text/csv" in response.content_type
    body = response.get_data(as_text=True)
    assert "'=HYPERLINK" in body


def test_event_seed_rules_present_and_toggleable(fresh_db):
    rules = fresh_db.list_ingest_rules()
    names = {rule["name"] for rule in rules}
    for expected in (
        "Interface link down (trap)",
        "Interface link up (trap)",
        "Authentication failure (trap)",
        "Reboot perangkat (trap)",
        "Login gagal (syslog)",
        "Perubahan konfigurasi (syslog)",
    ):
        assert expected in names
    # Aturan awal bisa dimatikan (enabled -> 0).
    login_rule = next(rule for rule in rules if rule["name"] == "Login gagal (syslog)")
    assert fresh_db.update_ingest_rule(
        login_rule["id"], {**login_rule, "enabled": 0}
    ) is True
    assert fresh_db.get_ingest_rule(login_rule["id"])["enabled"] == 0


def test_events_js_escapes_untrusted_text():
    path = os.path.join(BASE_DIR, "assets", "js", "events.js")
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    for token in ("escapeHtml(row.message", "escapeHtml(row.trap_oid", "escapeHtml(row.varbinds",
                  "escapeHtml(rule.name", "escapeHtml(conditionLabel(rule)"):
        assert token in src, f"events.js harus memakai escapeHtml untuk: {token}"
