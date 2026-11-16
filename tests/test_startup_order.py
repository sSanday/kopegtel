"""Regresi M-10: inisialisasi DB hanya setelah lease instance dipegang.

``import app`` tidak boleh menjalankan migrasi skema atau seed admin. Entrypoint
produksi mengambil lease lebih dulu, baru memanggil ``app.bootstrap()``; semua
komponen (poller, ingest, flow) berada dalam satu blok ``try/finally`` agar
kegagalan startup tetap melepas lease.
"""

import os
import sqlite3
import subprocess
import sys

import pytest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_DIR = os.path.join(BASE_DIR, "backend")
sys.path.insert(0, BACKEND_DIR)

import app as app_module  # noqa: E402
import db  # noqa: E402
import flow  # noqa: E402
import ingest  # noqa: E402
import serve  # noqa: E402


def _tables(path):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(
            "SELECT name FROM sqlite_master"
            " WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    finally:
        conn.close()


def test_importing_app_does_not_initialize_database(tmp_path):
    """Impor app murni tidak boleh membuat tabel/migrasi/seed admin."""
    db_path = tmp_path / "belum-diinisialisasi.db"
    env = os.environ.copy()
    env["DATABASE_PATH"] = str(db_path)
    env.pop("NMS_DB_PATH", None)

    result = subprocess.run(
        [sys.executable, "-c", "import app, serve"],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, result.stderr
    assert not db_path.exists() or _tables(str(db_path)) == []


def test_lease_can_be_acquired_on_empty_database(tmp_path, monkeypatch):
    """Lease harus bisa diambil sebelum migrasi penuh (database baru)."""
    path = str(tmp_path / "kosong.db")
    for target in (db, db.connection):
        monkeypatch.setattr(target, "DB_PATH", path)
        with db.connection._lock:
            if getattr(target, "_conn", None) is not None:
                target._conn.close()
            target._conn = None

    try:
        token = db.acquire_instance_lease()
        assert token
        assert db.instance_lease_status()["pid"] == os.getpid()

        with db._lock:
            tables = {
                row["name"]
                for row in db.get_conn()
                .execute("SELECT name FROM sqlite_master WHERE type = 'table'")
                .fetchall()
            }
        assert "instance_lease" in tables
        # Migrasi penuh/seed admin belum boleh berjalan sebelum bootstrap().
        assert "users" not in tables

        db.release_instance_lease(token)
        assert db.instance_lease_status() is None
    finally:
        with db.connection._lock:
            for target in (db, db.connection):
                if getattr(target, "_conn", None) is not None:
                    target._conn.close()
                target._conn = None


def test_bootstrap_creates_schema_and_admin(fresh_db):
    """bootstrap() eksplisit tetap membuat skema + akun admin awal."""
    assert fresh_db.user_count() == 0

    app_module.bootstrap()

    with fresh_db._lock:
        row = fresh_db.get_conn().execute(
            "SELECT username, role, must_change_password FROM users"
        ).fetchone()
    assert row["username"] == os.environ["ADMIN_USERNAME"]
    assert row["role"] == "admin"
    assert row["must_change_password"] == 1


def test_second_instance_stops_before_bootstrap(monkeypatch):
    """Lease gagal diambil → tidak ada migrasi, poller, atau pelepasan lease."""
    events = []
    monkeypatch.setattr(serve.db, "acquire_instance_lease", lambda: None)
    monkeypatch.setattr(
        serve.db,
        "instance_lease_status",
        lambda: {"pid": 1234, "heartbeat": "2026-11-16T04:00:00Z"},
    )
    monkeypatch.setattr(
        serve.app_module, "bootstrap", lambda: events.append("bootstrap")
    )
    monkeypatch.setattr(
        serve.poller, "start_poller", lambda: events.append("poller-start")
    )
    monkeypatch.setattr(
        serve.db, "release_instance_lease", lambda token: events.append("release")
    )

    assert serve.main(["--dev"]) == 1
    assert events == []


def test_bootstrap_runs_after_lease_and_cleanup_covers_poller(monkeypatch):
    """Urutan: lease → bootstrap → poller → server; shutdown tetap berjalan."""
    events = []
    monkeypatch.setattr(serve, "_lease_watchdog", lambda token: None)
    monkeypatch.setattr(
        serve.db,
        "acquire_instance_lease",
        lambda: (events.append("lease"), "tok")[1],
    )
    monkeypatch.setattr(
        serve.db,
        "release_instance_lease",
        lambda token: events.append(f"release:{token}"),
    )
    monkeypatch.setattr(
        serve.app_module, "bootstrap", lambda: events.append("bootstrap")
    )
    monkeypatch.setattr(
        serve.poller, "start_poller", lambda: events.append("poller-start")
    )
    monkeypatch.setattr(
        serve.poller, "shutdown_poller", lambda: events.append("poller-stop")
    )
    monkeypatch.setattr(
        ingest.INGEST, "start", lambda: events.append("ingest-start")
    )
    monkeypatch.setattr(
        ingest.INGEST, "stop", lambda: events.append("ingest-stop")
    )
    monkeypatch.setattr(flow.FLOW, "start", lambda: events.append("flow-start"))
    monkeypatch.setattr(flow.FLOW, "stop", lambda: events.append("flow-stop"))
    monkeypatch.setattr(
        serve.app_module.app, "run", lambda *args, **kwargs: events.append("serve")
    )

    assert serve.main(["--dev"]) == 0

    assert events.index("lease") < events.index("bootstrap")
    assert events.index("bootstrap") < events.index("poller-start")
    assert events.index("poller-start") < events.index("serve")
    assert events[-2:] == ["poller-stop", "release:tok"]
    assert "ingest-stop" in events and "flow-stop" in events


def test_poller_start_failure_still_releases_lease(monkeypatch):
    """start_poller() berada di dalam try/finally, bukan sebelum blok proteksi."""
    events = []
    monkeypatch.setattr(serve, "_lease_watchdog", lambda token: None)
    monkeypatch.setattr(
        serve.db,
        "acquire_instance_lease",
        lambda: (events.append("lease"), "tok")[1],
    )
    monkeypatch.setattr(
        serve.db,
        "release_instance_lease",
        lambda token: events.append(f"release:{token}"),
    )
    monkeypatch.setattr(
        serve.app_module, "bootstrap", lambda: events.append("bootstrap")
    )
    monkeypatch.setattr(
        serve.poller,
        "start_poller",
        lambda: (_ for _ in ()).throw(RuntimeError("poller gagal")),
    )
    monkeypatch.setattr(
        serve.poller, "shutdown_poller", lambda: events.append("poller-stop")
    )

    with pytest.raises(RuntimeError, match="poller gagal"):
        serve.main(["--dev"])

    assert events.index("lease") < events.index("bootstrap")
    assert events[-2:] == ["poller-stop", "release:tok"]
