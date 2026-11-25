"""E2-B — eskalasi: kebijakan, mesin 30 detik, tahan restart, storm merge."""

import os
import sys
import time
from datetime import datetime, timedelta, timezone

import pytest
from werkzeug.security import generate_password_hash

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, "backend"))

import app as app_module  # noqa: E402
import alerts  # noqa: E402
import escalation  # noqa: E402


@pytest.fixture()
def client():
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as client:
        yield client


def _mk_device(db, name, ip, maintenance=False):
    return db.create_device({
        "name": name, "ip": ip, "type": "mikrotik", "location": "",
        "poll_interval": 60, "group_id": None, "parent_id": None,
        "maintenance": maintenance,
        "snmp_enabled": False, "snmp_version": "v2c", "snmp_port": 161,
        "snmp_v3_auth_proto": "SHA", "snmp_v3_priv_proto": "AES",
        "mikrotik_enabled": False, "mikrotik_port": 8728,
        "mikrotik_user": None, "mikrotik_password": None, "mikrotik_ssl": False,
    })


def _mk_user(db, username, password, role="admin", **contacts):
    user_id = db.create_user(username, generate_password_hash(password), role=role, must_change_password=0)
    if contacts:
        db.update_user_contacts(user_id, **contacts)
    return user_id


def _login(client, username, password):
    return client.post("/api/login", json={"username": username, "password": password})


def _mk_telegram_channel(db, name="tg-test"):
    return db.create_channel(name, "telegram", {"bot_token": "token-dummy", "chat_id": "12345"}, True)


def _mk_template(db, body="Problem {perangkat} {pesan}"):
    return db.create_template("tpl-esc", body)


def _mk_rule(db, channel_id, template_id, escalation_policy_id=None):
    return db.create_rule({
        "name": f"rule-esc-{channel_id}-{template_id}-{escalation_policy_id}",
        "min_severity": "information",
        "group_id": None, "device_id": None,
        "channel_id": channel_id, "template_id": template_id,
        "escalation_policy_id": escalation_policy_id, "enabled": True,
    })


@pytest.fixture()
def telegram_capture(monkeypatch):
    sent = []

    def _fake_send(config, text):
        sent.append({"chat_id": config.get("chat_id"), "text": text})
        return True, "Terkirim ke Telegram."

    monkeypatch.setattr(alerts, "send_telegram", _fake_send)
    return sent


@pytest.fixture(autouse=True)
def _reset_storm():
    alerts._STORM["entries"].clear()
    alerts._STORM["active_until"] = 0.0
    yield
    alerts._STORM["entries"].clear()
    alerts._STORM["active_until"] = 0.0


def _open_problem(db, device_id, kind="device_down", severity="high", message="tidak merespons"):
    problem_id, _ = db.open_problem(device_id, kind, severity, message)
    return problem_id


def _set_problem_started_at(db, problem_id, started_at):
    """Pin problem start time so business-hours tests are time-independent."""
    with db.connection._lock:
        conn = db.get_conn()
        conn.execute("UPDATE problems SET started_at=? WHERE id=?", (started_at, problem_id))
        conn.commit()


def test_policy_crud_and_validation(fresh_db):
    channel_id = _mk_telegram_channel(fresh_db, "tg-crud")
    created = fresh_db.create_policy({
        "name": "noop", "description": "", "enabled": True,
        "business_hours_enabled": False, "business_start": "08:00", "business_end": "17:00",
        "business_days": "1,2,3,4,5", "repeat_minutes": 0,
        "steps": [{"delay_minutes": 1, "target_type": "channel", "target_id": channel_id,
                   "business_hours_only": False}],
    })
    policy = fresh_db.get_policy(created)
    assert policy["name"] == "noop"
    assert len(policy["steps"]) == 1


def test_engine_sends_first_step_and_stops(fresh_db, telegram_capture):
    device_id = _mk_device(fresh_db, "esc-dev-1", "192.0.2.101")
    channel_id = _mk_telegram_channel(fresh_db, "tg-1")
    policy_id = fresh_db.create_policy({
        "name": "eskalasi-1", "enabled": True,
        "business_hours_enabled": False, "business_start": "08:00", "business_end": "17:00",
        "business_days": "1,2,3,4,5", "repeat_minutes": 0,
        "steps": [{"delay_minutes": 0, "target_type": "channel", "target_id": channel_id,
                   "business_hours_only": False}],
    })
    problem_id = _open_problem(fresh_db, device_id)
    fresh_db.register_escalation(problem_id, policy_id)

    result = escalation.engine_tick()
    assert result["sent"] == 1
    assert len(telegram_capture) == 1
    assert "Eskalasi" in telegram_capture[0]["text"]

    # Tahan restart: tick berikutnya tidak mengirim ulang langkah 1.
    result2 = escalation.engine_tick()
    assert len(telegram_capture) == 1


def test_engine_holds_during_maintenance_and_sends_after(fresh_db, telegram_capture):
    device_id = _mk_device(fresh_db, "esc-maint", "192.0.2.102", maintenance=True)
    channel_id = _mk_telegram_channel(fresh_db, "tg-maint")
    policy_id = fresh_db.create_policy({
        "name": "eskalasi-maint", "enabled": True,
        "business_hours_enabled": False, "business_start": "08:00", "business_end": "17:00",
        "business_days": "1,2,3,4,5", "repeat_minutes": 0,
        "steps": [{"delay_minutes": 0, "target_type": "channel", "target_id": channel_id,
                   "business_hours_only": False}],
    })
    problem_id = _open_problem(fresh_db, device_id)
    fresh_db.register_escalation(problem_id, policy_id)

    escalation.engine_tick()
    assert telegram_capture == []

    with fresh_db._lock:
        conn = fresh_db.get_conn()
        conn.execute("UPDATE devices SET maintenance=0 WHERE id=?", (device_id,))
        conn.commit()

    escalation.engine_tick()
    assert len(telegram_capture) == 1


def test_engine_stops_on_ack(fresh_db, telegram_capture):
    device_id = _mk_device(fresh_db, "esc-ack", "192.0.2.103")
    channel_id = _mk_telegram_channel(fresh_db, "tg-ack")
    policy_id = fresh_db.create_policy({
        "name": "eskalasi-ack", "enabled": True,
        "business_hours_enabled": False, "business_start": "08:00", "business_end": "17:00",
        "business_days": "1,2,3,4,5", "repeat_minutes": 0,
        "steps": [{"delay_minutes": 0, "target_type": "channel", "target_id": channel_id,
                   "business_hours_only": False}],
    })
    problem_id = _open_problem(fresh_db, device_id)
    fresh_db.register_escalation(problem_id, policy_id)
    fresh_db.ack_problem(problem_id, "tester")

    escalation.engine_tick()
    assert telegram_capture == []
    state = fresh_db.get_escalation_state(problem_id)
    assert state["stopped_reason"] == "ack"


def test_engine_sends_recovery_on_resolve(fresh_db, telegram_capture):
    device_id = _mk_device(fresh_db, "esc-resolve", "192.0.2.104")
    channel_id = _mk_telegram_channel(fresh_db, "tg-resolve")
    policy_id = fresh_db.create_policy({
        "name": "eskalasi-resolve", "enabled": True,
        "business_hours_enabled": False, "business_start": "08:00", "business_end": "17:00",
        "business_days": "1,2,3,4,5", "repeat_minutes": 0,
        "steps": [{"delay_minutes": 0, "target_type": "channel", "target_id": channel_id,
                   "business_hours_only": False}],
    })
    problem_id = _open_problem(fresh_db, device_id)
    fresh_db.register_escalation(problem_id, policy_id)
    escalation.engine_tick()
    assert len(telegram_capture) == 1

    fresh_db.resolve_problem(device_id, "device_down")
    escalation.engine_tick()
    assert len(telegram_capture) == 2
    assert "PULIH" in telegram_capture[-1]["text"]

    # Tidak mengirim pemulihan dua kali.
    escalation.engine_tick()
    assert len(telegram_capture) == 2


def test_engine_holds_suppressed_by_parent(fresh_db, telegram_capture):
    device_id = _mk_device(fresh_db, "esc-supp", "192.0.2.105")
    channel_id = _mk_telegram_channel(fresh_db, "tg-supp")
    policy_id = fresh_db.create_policy({
        "name": "eskalasi-supp", "enabled": True,
        "business_hours_enabled": False, "business_start": "08:00", "business_end": "17:00",
        "business_days": "1,2,3,4,5", "repeat_minutes": 0,
        "steps": [{"delay_minutes": 0, "target_type": "channel", "target_id": channel_id,
                   "business_hours_only": False}],
    })
    problem_id = _open_problem(fresh_db, device_id)
    fresh_db.register_escalation(problem_id, policy_id)
    with fresh_db._lock:
        conn = fresh_db.get_conn()
        conn.execute("UPDATE problems SET suppressed_by_parent=? WHERE id=?", ("parent-olt", problem_id))
        conn.commit()
    escalation.engine_tick()
    assert telegram_capture == []
    with fresh_db._lock:
        conn = fresh_db.get_conn()
        conn.execute("UPDATE problems SET suppressed_by_parent=NULL WHERE id=?", (problem_id,))
        conn.commit()
    escalation.engine_tick()
    assert len(telegram_capture) == 1


def test_engine_respects_business_hours(fresh_db, telegram_capture):
    device_id = _mk_device(fresh_db, "esc-hours", "192.0.2.106")
    channel_id = _mk_telegram_channel(fresh_db, "tg-hours")
    policy_id = fresh_db.create_policy({
        "name": "eskalasi-hours", "enabled": True,
        "business_hours_enabled": True, "business_start": "08:00", "business_end": "17:00",
        "business_days": "1,2,3,4,5", "repeat_minutes": 0,
        "steps": [{"delay_minutes": 0, "target_type": "channel", "target_id": channel_id,
                   "business_hours_only": True}],
    })
    problem_id = _open_problem(fresh_db, device_id)
    # Waktu mulai problem dipatok ke tanggal skenario agar tes tidak
    # bergantung pada jam berjalan saat suite dieksekusi.
    _set_problem_started_at(fresh_db, problem_id, "2026-11-11T00:00:00Z")
    fresh_db.register_escalation(problem_id, policy_id)

    # 2026-11-12 00:00 UTC = 07:00 WIB (luar jam kerja).
    escalation.engine_tick(now_utc=datetime(2026, 11, 11, 23, 59, 0, tzinfo=timezone.utc))
    assert telegram_capture == []

    # 2026-11-12 02:00 UTC = 09:00 WIB (dalam jam kerja, hari Rabu).
    escalation.engine_tick(now_utc=datetime(2026, 11, 12, 2, 0, 0, tzinfo=timezone.utc))
    assert len(telegram_capture) == 1


def test_rule_with_escalation_registers_instead_of_sending(fresh_db, telegram_capture):
    device_id = _mk_device(fresh_db, "esc-rule", "192.0.2.107")
    channel_id = _mk_telegram_channel(fresh_db, "tg-rule")
    template_id = _mk_template(fresh_db)
    policy_id = fresh_db.create_policy({
        "name": "eskalasi-rule", "enabled": True,
        "business_hours_enabled": False, "business_start": "08:00", "business_end": "17:00",
        "business_days": "1,2,3,4,5", "repeat_minutes": 0,
        "steps": [{"delay_minutes": 0, "target_type": "channel", "target_id": channel_id,
                   "business_hours_only": False}],
    })
    _mk_rule(fresh_db, channel_id, template_id, escalation_policy_id=policy_id)
    problem_id = _open_problem(fresh_db, device_id)
    outcomes = alerts.dispatch_problem(
        {"id": problem_id, "device_id": device_id, "kind": "device_down",
         "severity": "high", "message": "tidak merespons", "started_at": "2026-11-11T00:00:00Z"}
    )
    assert telegram_capture == []
    state = fresh_db.get_escalation_state(problem_id)
    assert state is not None
    escalation.engine_tick()
    assert len(telegram_capture) == 1


def test_storm_merge_batches_notifications(fresh_db, telegram_capture):
    device_id = _mk_device(fresh_db, "esc-storm", "192.0.2.108")
    channel_id = _mk_telegram_channel(fresh_db, "tg-storm")
    template_id = _mk_template(fresh_db)
    _mk_rule(fresh_db, channel_id, template_id)
    fresh_db.set_settings({"notification_storm_threshold": "2"})

    ids = [
        _open_problem(fresh_db, device_id, kind=f"storm-kind-{n}", message=f"storm-{n}")
        for n in range(3)
    ]
    for pid in ids[:2]:
        alerts.dispatch_problem(
            {"id": pid, "device_id": device_id, "kind": f"storm-kind", "severity": "high",
             "message": "tidak merespons", "started_at": "2026-11-11T00:00:00Z"}
        )
    # Di atas ambang: pesan ringkasan terkirim (bukan per-problem).
    alerts.dispatch_problem(
        {"id": ids[2], "device_id": device_id, "kind": "storm-kind", "severity": "high",
         "message": "tidak merespons", "started_at": "2026-11-11T00:00:00Z"}
    )
    sent_archive = list(telegram_capture)
    assert any("Badai notifikasi" in item["text"] for item in sent_archive)
    with fresh_db._lock:
        rows = fresh_db.get_conn().execute(
            "SELECT status, detail FROM action_log ORDER BY id"
        ).fetchall()
    assert any("digabung" in (row["detail"] or "").lower() for row in rows)


def test_storm_gate_uses_snapshot_when_entries_mutated(monkeypatch):
    """Regresi: jangan iterasi deque live saat worker lain mengubahnya."""
    alerts._STORM["entries"].clear()
    alerts._STORM["active_until"] = 0.0
    monkeypatch.setattr(alerts, "_storm_threshold", lambda: 3)

    mutated = {"done": False}

    class _MutatingId(int):
        def __hash__(self):
            if not mutated["done"]:
                mutated["done"] = True
                alerts._STORM["entries"].append({
                    "ts": time.time(), "problem_id": 999, "kind": "other",
                    "severity": "high", "device": "d", "message": "m",
                })
            return int.__hash__(self)

    alerts._STORM["entries"].append({
        "ts": time.time(), "problem_id": _MutatingId(1), "kind": "k",
        "severity": "high", "device": "d", "message": "m",
    })

    result = alerts._storm_gate(
        {"id": 2, "kind": "k", "severity": "high", "message": "m"},
        {"name": "d"},
    )

    assert mutated["done"] is True
    assert result is None


# ---------------------------------------------------------
# API: kebijakan eskalasi & target
# ---------------------------------------------------------

def test_escalation_policy_api(client, fresh_db):
    channel_id = _mk_telegram_channel(fresh_db, "tg-api")
    _mk_user(fresh_db, "esc-admin", "esc-admin-password", role="admin")
    _mk_user(fresh_db, "esc-operator", "esc-op-password", role="operator")
    r = _login(client, "esc-admin", "esc-admin-password")
    csrf = r.get_json()["csrf_token"]

    bad = client.post(
        "/api/escalation-policies",
        json={"name": "x-bad", "steps": [{"delay_minutes": 1, "target_type": "channel", "target_id": 99999}]},
        headers={"X-CSRF-Token": csrf},
    )
    assert bad.status_code == 400

    created = client.post(
        "/api/escalation-policies",
        json={"name": "policy-api", "enabled": True,
              "business_hours_enabled": False, "business_days": "1,2,3,4,5",
              "steps": [{"delay_minutes": 3, "target_type": "channel", "target_id": channel_id}]},
        headers={"X-CSRF-Token": csrf},
    )
    assert created.status_code == 201, created.get_json()
    policy_id = created.get_json()["id"]

    client.post("/api/logout", headers={"X-CSRF-Token": csrf})
    r = _login(client, "esc-operator", "esc-op-password")
    csrf_op = r.get_json()["csrf_token"]
    listed = client.get("/api/escalation-policies")
    assert listed.status_code == 200
    assert any(p["name"] == "policy-api" for p in listed.get_json()["policies"])

    edited = client.put(
        f"/api/escalation-policies/{policy_id}",
        json={"name": "policy-api-2", "enabled": False,
              "business_hours_enabled": False, "business_days": "1,2,3,4,5",
              "steps": [{"delay_minutes": 5, "target_type": "channel", "target_id": channel_id}]},
        headers={"X-CSRF-Token": csrf_op},
    )
    assert edited.status_code == 200
    with fresh_db._lock:
        actions = [
            row["action"]
            for row in fresh_db.get_conn().execute("SELECT action FROM audit_log").fetchall()
        ]
    assert "escalation.create" in actions
    assert "escalation.update" in actions

    deleted = client.delete(
        f"/api/escalation-policies/{policy_id}", headers={"X-CSRF-Token": csrf_op}
    )
    assert deleted.status_code == 200
    _ = _mk_user(fresh_db, "esc-viewer", "esc-viewer-password", role="viewer")
    client.post("/api/logout", headers={"X-CSRF-Token": csrf_op})
    _login(client, "esc-viewer", "esc-viewer-password")
    assert client.get("/api/escalation-policies").status_code == 403


def test_escalation_targets_hide_secrets(client, fresh_db):
    _mk_telegram_channel(fresh_db, "tg-targets")
    _mk_user(fresh_db, "esc-target-admin", "esc-target-admin-password", role="admin")
    _login(client, "esc-target-admin", "esc-target-admin-password")
    response = client.get("/api/escalation-targets")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "token-dummy" not in body
    assert "password" not in body
    users = response.get_json()["users"]
    assert all(set(user) >= {"id", "username"} for user in users)


def test_user_contacts_update_and_validate(client, fresh_db):
    _mk_user(fresh_db, "contact-admin", "contact-admin-password", role="admin")
    user_id = _mk_user(fresh_db, "contact-target", "contact-target-password", role="viewer")
    r = _login(client, "contact-admin", "contact-admin-password")
    csrf = r.get_json()["csrf_token"]

    bad = client.put(
        f"/api/users/{user_id}",
        json={"email": "bukan-email"},
        headers={"X-CSRF-Token": csrf},
    )
    assert bad.status_code == 400

    ok = client.put(
        f"/api/users/{user_id}",
        json={"email": "target@kopegtel.co.id", "telegram_chat_id": "123456789"},
        headers={"X-CSRF-Token": csrf},
    )
    assert ok.status_code == 200
    with fresh_db._lock:
        row = fresh_db.get_conn().execute(
            "SELECT email, telegram_chat_id FROM users WHERE id=?", (user_id,)
        ).fetchone()
    assert row["email"] == "target@kopegtel.co.id"
    assert row["telegram_chat_id"] == "123456789"
