"""Regresi M-5: formula injection pada ekspor CSV audit dan ketersediaan."""

import csv
import io
import os
import sys

import pytest
from werkzeug.security import generate_password_hash

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, "backend"))

import app as app_module  # noqa: E402
import db  # noqa: E402
from core.csvsafe import csv_cell  # noqa: E402


@pytest.fixture()
def client():
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as client:
        yield client


def _mk_admin(username="csv-admin", password="csv-admin-password"):
    db.create_user(
        username,
        generate_password_hash(password),
        role="admin",
        must_change_password=0,
    )
    return username, password


def _login(client, username="csv-admin", password="csv-admin-password"):
    return client.post("/api/login", json={"username": username, "password": password})


def _device_payload(name, ip, group_id=None):
    return {
        "name": name,
        "ip": ip,
        "type": "mikrotik",
        "location": "",
        "poll_interval": 60,
        "group_id": group_id,
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


def _csv_rows(response):
    assert response.status_code == 200
    assert "text/csv" in response.content_type
    body = response.get_data(as_text=True).removeprefix("\ufeff")
    return list(csv.reader(io.StringIO(body)))


@pytest.mark.parametrize(
    "value",
    [
        "=1+1",
        "+1+1",
        "-1+1",
        "@SUM(A1:A2)",
        "\t=1+1",
        "\r=1+1",
        "\n=1+1",
        "  =1+1",
    ],
)
def test_csv_cell_neutralizes_formula_prefixes(value):
    assert csv_cell(value) == "'" + value


def test_csv_cell_keeps_numbers_and_plain_text():
    assert csv_cell(-5) == -5
    assert csv_cell(3.5) == 3.5
    assert csv_cell("router-1") == "router-1"
    assert csv_cell(None) == ""


def test_audit_csv_neutralizes_anonymous_login_username(client, fresh_db):
    _mk_admin()
    payload = '=HYPERLINK("http://evil.example","klik")'
    failed = client.post(
        "/api/login",
        json={"username": payload, "password": "salah-password"},
        environ_base={"REMOTE_ADDR": "198.51.100.77"},
    )
    assert failed.status_code == 401
    assert _login(client).status_code == 200

    rows = _csv_rows(client.get("/api/audit-log.csv"))
    usernames = [row[1] for row in rows[1:]]
    assert "'" + payload in usernames


def test_availability_csv_neutralizes_device_and_group(client, fresh_db):
    _mk_admin()
    assert _login(client).status_code == 200
    group_name = "+cmd|' /C calc'!A0"
    device_name = '=HYPERLINK("http://evil.example","klik")'
    group_id = db.create_group(group_name)
    db.create_device(_device_payload(device_name, "192.0.2.90", group_id=group_id))

    rows = _csv_rows(client.get("/api/reports/availability.csv?period=7"))
    target = next(row for row in rows[1:] if row[1] == "192.0.2.90")
    assert target[0] == "'" + device_name
    assert target[3] == "'" + group_name
