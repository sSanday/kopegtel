"""E3 — Traffic Flow: v5/v9/IPFIX, agregasi bucket, top-N, retensi, RBAC."""

import os
import socket
import struct
import sys
from datetime import datetime, timedelta, timezone
from werkzeug.security import generate_password_hash

import pytest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, "backend"))

import app as app_module  # noqa: E402
import db as db_module  # noqa: E402
from flow.service import FlowManager  # noqa: E402


def _ip(text):
    return struct.unpack("!I", socket.inet_aton(text))[0]


def _mk_device(db, name="nf-router", ip="192.0.2.254", **overrides):
    payload = {
        "name": name, "ip": ip, "type": "mikrotik", "location": "",
        "poll_interval": 60, "group_id": None, "parent_id": None,
        "maintenance": False, "snmp_enabled": False, "snmp_version": "v2c",
        "snmp_port": 161, "snmp_v3_auth_proto": "SHA", "snmp_v3_priv_proto": "AES",
        "mikrotik_enabled": False, "mikrotik_port": 8728,
        "mikrotik_user": None, "mikrotik_password": None, "mikrotik_ssl": False,
    }
    payload.update(overrides)
    return db.create_device(payload)


def _v5_packet(src="192.0.2.1", dst="203.0.113.5", proto=6, dport=443, sport=12345,
               bytes_=5000, packets=10, in_if=3, out_if=4, sampling=0):
    header = struct.pack("!HHIIIIBBH", 5, 1, 1000, 2000000000, 0, 1, 0, 0, sampling)
    flow = struct.pack(
        "!IIIHHIIIIHHxBBBHHBBxx",
        _ip(src), _ip(dst), 0, in_if, out_if, packets, bytes_, 100, 200,
        sport, dport, 0x18, proto, 0, 0, 0, 0, 0,
    )
    return header + flow


_V9_FIELDS = [(8, 4), (12, 4), (7, 2), (11, 2), (1, 4), (2, 4), (4, 1), (5, 1), (10, 4), (14, 4), (6, 2)]
_IPFIX_FIELDS = [(8, 4), (12, 4), (7, 2), (11, 2), (1, 8), (2, 4), (4, 1), (5, 1), (10, 4), (14, 4), (6, 2)]


def _v9_template_set(template_id=256):
    length = 4 + 4 + 4 * len(_V9_FIELDS)
    return (
        struct.pack("!HHHH", 0, length, template_id, len(_V9_FIELDS))
        + b"".join(struct.pack("!HH", ft, fl) for ft, fl in _V9_FIELDS)
    )


def _v9_data_set(template_id=256, src="192.0.2.2", dst="203.0.113.6", proto=6,
                 sport=5555, dport=80, bytes_=12000, packets=24, in_if=7, out_if=8):
    record = struct.pack(
        "!I I H H I I B B I I H",
        _ip(src), _ip(dst), sport, dport, bytes_, packets, proto, 0, in_if, out_if, 0x02,
    )
    return struct.pack("!HH", template_id, 4 + len(record)) + record


def _v9_packet(src="192.0.2.2", dst="203.0.113.6", **kwargs):
    template = _v9_template_set()
    data = _v9_data_set(src=src, dst=dst, **kwargs)
    header = struct.pack("!HHIIII", 9, 2, 1000, 2000000000, 1, 42)
    return header + template + data


def _ipfix_template_set(set_id=2, template_id=300):
    length = 4 + 4 + 4 * len(_IPFIX_FIELDS)
    return (
        struct.pack("!HHHH", set_id, length, template_id, len(_IPFIX_FIELDS))
        + b"".join(struct.pack("!HH", ft, fl) for ft, fl in _IPFIX_FIELDS)
    )


def _ipfix_data_set(template_id=300, src="192.0.2.3", dst="203.0.113.7", proto=6,
                    sport=6666, dport=443, bytes_=99999, packets=33, in_if=9, out_if=10):
    record = (
        socket.inet_aton(src) + socket.inet_aton(dst)
        + struct.pack("!HH", sport, dport)
        + struct.pack("!Q", bytes_)
        + struct.pack("!I", packets)
        + struct.pack("!BB", proto, 0)
        + struct.pack("!I", in_if)
        + struct.pack("!I", out_if)
        + struct.pack("!H", 0x10)
    )
    return struct.pack("!HH", template_id, 4 + len(record)) + record


def _ipfix_packet(src="192.0.2.3", dst="203.0.113.7", **kwargs):
    template = _ipfix_template_set()
    data = _ipfix_data_set(src=src, dst=dst, **kwargs)
    header = struct.pack("!HHIII", 10, 16 + len(template) + len(data), 1000, 1, 77)
    return header + template + data


def _mk_user(db, username, password, role="admin"):
    db.create_user(username, generate_password_hash(password), role=role, must_change_password=0)


@pytest.fixture()
def client():
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as client:
        yield client


@pytest.fixture()
def manager(fresh_db):
    _mk_device(fresh_db)
    return FlowManager()


# ---------------------------------------------------------
# v5 / v9 / IPFIX
# ---------------------------------------------------------

def test_v5_packet_accepted_and_persisted(manager, fresh_db):
    manager.process_flow_datagram(_v5_packet(), "192.0.2.254", "netflow_udp")
    stats = manager.status()["netflow_udp"]
    assert stats["accepted"] == 1
    assert stats["flows"] == 1
    manager.flush_buckets()
    with fresh_db._lock:
        rows = fresh_db.get_conn().execute(
            "SELECT kind, key, bytes FROM flow_buckets ORDER BY kind, key"
        ).fetchall()
    table = {(row["kind"], row["key"]): row["bytes"] for row in rows}
    assert table[("src", "192.0.2.1")] == 5000
    assert table[("pair", "192.0.2.1>203.0.113.5")] == 5000
    assert table[("app", "HTTPS")] == 5000
    assert table[("proto", "TCP")] == 5000
    assert table[("interface", "ifIndex:3:rx")] == 5000
    assert table[("interface", "ifIndex:4:tx")] == 5000


def test_v5_sampling_interval_scales_bytes(manager, fresh_db):
    # sampling_interval: low 14 bits = interval (bit 14 = mode)
    manager.process_flow_datagram(_v5_packet(bytes_=1000, sampling=4), "192.0.2.254", "netflow_udp")
    manager.flush_buckets()
    with fresh_db._lock:
        row = fresh_db.get_conn().execute(
            "SELECT bytes FROM flow_buckets WHERE kind='proto' AND key='TCP'"
        ).fetchone()
    assert row["bytes"] == 4000


def test_v9_with_template_accepted(manager, fresh_db):
    manager.process_flow_datagram(_v9_packet(), "192.0.2.254", "netflow_udp")
    stats = manager.status()["netflow_udp"]
    assert stats["accepted"] == 1
    assert stats["templates"] == 1
    manager.flush_buckets()
    with fresh_db._lock:
        row = fresh_db.get_conn().execute(
            "SELECT SUM(bytes) AS b FROM flow_buckets WHERE kind='proto'"
        ).fetchone()
    assert row["b"] == 12000


def test_ipfix_with_template_accepted(manager, fresh_db):
    manager.process_flow_datagram(_ipfix_packet(), "192.0.2.254", "ipfix_udp")
    stats = manager.status()["ipfix_udp"]
    assert stats["accepted"] == 1
    manager.flush_buckets()
    with fresh_db._lock:
        row = fresh_db.get_conn().execute(
            "SELECT SUM(bytes) AS b FROM flow_buckets WHERE kind='app'"
        ).fetchone()
    assert row["b"] == 99999


def test_data_before_template_counted_and_dropped(manager, fresh_db):
    # IPFIX hanya data set tanpa template paketnya
    data_set = _ipfix_data_set()
    header = struct.pack("!HHIII", 10, 16 + len(data_set), 1000, 1, 77)
    manager.process_flow_datagram(header + data_set, "192.0.2.254", "ipfix_udp")
    stats = manager.status()["ipfix_udp"]
    assert stats["dropped_no_template"] == 1
    assert stats["accepted"] == 0


def test_unknown_exporter_dropped(manager, fresh_db):
    manager.process_flow_datagram(_v5_packet(), "192.0.2.253", "netflow_udp")
    stats = manager.status()["netflow_udp"]
    assert stats["dropped_unknown"] == 1
    assert stats["accepted"] == 0


def test_rate_limited_source_dropped(manager, fresh_db):
    from ingest.service import RateLimiter

    manager._rate = RateLimiter(per_second=1, window_seconds=1)
    manager.process_flow_datagram(_v5_packet(), "192.0.2.254", "netflow_udp")
    manager.process_flow_datagram(_v5_packet(), "192.0.2.254", "netflow_udp")
    assert manager.status()["netflow_udp"]["dropped_rate"] == 1


def test_interface_name_mapped_from_snapshot(manager, fresh_db):
    device_id = db_module.get_device_by_ip("192.0.2.254")["id"]
    import json

    with fresh_db._lock:
        conn = fresh_db.get_conn()
        conn.execute(
            "INSERT INTO mt_interfaces (device_id, row_key, polled_at, stale, data)"
            " VALUES (?,?,?,0,?)",
            (device_id, "if:3", "2026-11-10T00:00:00Z", json.dumps({"name": "ether3", "if_index": 3})),
        )
        conn.commit()
    manager.process_flow_datagram(_v5_packet(in_if=3), "192.0.2.254", "netflow_udp")
    manager.flush_buckets()
    with fresh_db._lock:
        row = fresh_db.get_conn().execute(
            "SELECT key FROM flow_buckets WHERE kind='interface' AND key LIKE 'ether3%'"
        ).fetchone()
    assert row is not None
    assert row["key"] == "ether3:rx"


# ---------------------------------------------------------
# Agregasi, top-N, retensi
# ---------------------------------------------------------

def test_top_n_overflow_merged_to_lainnya(fresh_db):
    _mk_device(fresh_db, "nf-topn", "192.0.2.240")
    from db import insert_bucket_rows

    rows = [
        (1, _now_bucket(), "app", f"app-{index}", 1000 * (index + 1), 1, 1)
        for index in range(6)
    ]
    insert_bucket_rows(rows, top_n=3)
    with fresh_db._lock:
        result = fresh_db.get_conn().execute(
            "SELECT key, bytes FROM flow_buckets WHERE kind='app' ORDER BY bytes DESC"
        ).fetchall()
    keys = [row["key"] for row in result]
    assert set(keys) == {"app-5", "app-4", "app-3", "lainnya"}
    lainnya = next(row for row in result if row["key"] == "lainnya")
    assert lainnya["bytes"] == 1000 + 2000 + 3000


def test_bucket_start_ts_floors_to_5_minutes():
    assert db_module.flow.bucket_start_ts("2026-11-11T07:03:59Z") == "2026-11-11T07:00:00Z"
    assert db_module.flow.bucket_start_ts("2026-11-11T07:07:00Z") == "2026-11-11T07:05:00Z"


def test_rollup_hourly_idempotent(fresh_db):
    _mk_device(fresh_db, "nf-rollup", "192.0.2.241")
    from db import insert_bucket_rows, rollup_hourly_buckets

    old_hour = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:") + "00:00Z"
    rows = [
        (1, old_hour[:16] + ":00:00Z", "app", "HTTP", 1000, 1, 1),
        (1, old_hour[:16] + ":05:00Z", "app", "HTTP", 2000, 1, 1),
    ]
    insert_bucket_rows(rows, top_n=10)
    rollup_hourly_buckets()
    rollup_hourly_buckets()  # panggil dua kali: tidak boleh menggandakan
    with fresh_db._lock:
        row = fresh_db.get_conn().execute(
            "SELECT bytes, flows FROM flow_buckets_hourly WHERE kind='app'"
        ).fetchone()
    assert row["bytes"] == 3000


def test_retention_purge_flow(fresh_db):
    _mk_device(fresh_db, "nf-retention", "192.0.2.242")
    old = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    fresh_db.set_settings({"retention_flow_days": "7", "retention_flow_hourly_days": "90"})
    with fresh_db._lock:
        conn = fresh_db.get_conn()
        conn.execute(
            "INSERT INTO flow_buckets (bucket_ts, exporter_device_id, kind, key, bytes, packets, flows)"
            " VALUES (?,?,?,?,?,?,?)",
            (old, 1, "app", "HTTP", 1000, 1, 1),
        )
        conn.commit()
    result = fresh_db.purge_old_data()
    assert result["flow_raw_deleted"] >= 1
    with fresh_db._lock:
        merged = fresh_db.get_conn().execute(
            "SELECT bytes FROM flow_buckets_hourly WHERE hour_ts = ?",
            (old[:13] + ":00:00Z",),
        ).fetchone()
    # Data lama tidak hilang: digabung ke tabel per jam sebelum mentahnya dibuang.
    assert merged is not None
    assert merged["bytes"] == 1000


def _now_bucket():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:00Z")


def test_flow_top_query_respects_exporter_filter(fresh_db):
    _mk_device(fresh_db, "nf-exp-a", "192.0.2.243")
    _mk_device(fresh_db, "nf-exp-b", "192.0.2.244")
    bucket = _now_bucket()
    rows_a = [(1, bucket, "src", "10.0.0.1", 1000, 1, 1)]
    rows_b = [(2, bucket, "src", "10.0.0.2", 9000, 1, 1)]
    fresh_db.insert_bucket_rows(rows_a + rows_b, top_n=10)
    only_a = fresh_db.flow_top("src", "1h", exporter_id=1, limit=10)
    assert {row["key"] for row in only_a} == {"10.0.0.1"}


def test_host_name_resolution_from_leases(fresh_db):
    import json

    device_id = _mk_device(fresh_db, "nf-dhcp", "192.0.2.251")
    with fresh_db._lock:
        conn = fresh_db.get_conn()
        conn.execute(
            "INSERT INTO mt_dhcp_leases (device_id, row_key, polled_at, stale, data)"
            " VALUES (?,?,?,0,?)",
            (device_id, "lease:1", "2026-11-10T00:00:00Z",
             json.dumps({"address": "192.0.2.1", "host-name": "laptop-noc"})),
        )
        conn.commit()
    mapping = fresh_db.flow.lease_host_map()
    assert mapping["192.0.2.1"] == "laptop-noc"


# ---------------------------------------------------------
# API & RBAC
# ---------------------------------------------------------

def _login(client, username, password):
    return client.post("/api/login", json={"username": username, "password": password})


def test_flow_api_summary_and_rbac(client, fresh_db):
    _mk_device(fresh_db, "nf-api", "192.0.2.230")
    fresh_db.insert_bucket_rows(
        [(1, "2999-01-01T00:00:00Z", "src", "10.0.0.1", 1000, 1, 1),
         (1, "2999-01-01T00:00:00Z", "app", "HTTPS", 1000, 1, 1)],
        top_n=10,
    )
    _mk_user(fresh_db, "flow-admin", "flow-admin-password", role="admin")
    _mk_user(fresh_db, "flow-viewer", "flow-viewer-password", role="viewer")

    assert _login(client, "flow-admin", "flow-admin-password").status_code == 200
    summary = client.get("/api/flow/summary?range=1h")
    assert summary.status_code == 200
    assert "exporters" in summary.get_json()

    talkers = client.get("/api/flow/talkers?kind=src")
    assert talkers.status_code == 200

    client.post("/api/logout")
    assert _login(client, "flow-viewer", "flow-viewer-password").status_code == 200
    summary_viewer = client.get("/api/flow/summary?range=1h")
    assert summary_viewer.status_code == 200
    body = summary_viewer.get_json()
    assert body.get("viewer") is True
    exporter = body["exporters"][0]
    assert "series" not in exporter

    assert client.get("/api/flow/talkers").status_code == 403
    assert client.get("/api/flow/conversations").status_code == 403
    assert client.get("/api/flow/apps").status_code == 403
    assert client.get("/api/flow/status").status_code == 403


def test_flow_api_csv_export(client, fresh_db):
    _mk_device(fresh_db, "nf-csv", "192.0.2.231")
    fresh_db.insert_bucket_rows(
        [(1, "2999-01-01T00:00:00Z", "src", "10.0.0.1", 1000, 1, 1)],
        top_n=10,
    )
    _mk_user(fresh_db, "flow-csv", "flow-csv-password", role="admin")
    _login(client, "flow-csv", "flow-csv-password")
    response = client.get("/api/flow/talkers.csv?kind=src")
    assert response.status_code == 200
    assert "text/csv" in response.content_type
    assert "10.0.0.1" in response.get_data(as_text=True)


def test_receiver_flow_kinds_configurable(client, fresh_db, monkeypatch):
    import routes.events as events_routes

    class _StubIngest:
        def reload(self):
            return {"netflow_udp": {"running": False}}

    class _StubFlow:
        def reload(self):
            return {"netflow_udp": {"running": False}, "ipfix_udp": {"running": False}}

    monkeypatch.setattr(events_routes, "INGEST", _StubIngest())
    monkeypatch.setattr("flow.FLOW", _StubFlow())

    _mk_user(fresh_db, "flow-receiver", "flow-receiver-password", role="admin")
    r = _login(client, "flow-receiver", "flow-receiver-password")
    csrf = r.get_json()["csrf_token"]
    response = client.put(
        "/api/events/receiver",
        json={"kind": "netflow_udp", "enabled": False, "port": 2055, "bind_address": "0.0.0.0"},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200
