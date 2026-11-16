"""Regresi M-9: transaksi SQLite tidak boleh menggantung di koneksi bersama.

Koneksi SQLite dipakai bersama oleh request, poller, dan ingest. Bila satu
fungsi menulis beberapa statement lalu gagal sebelum commit, transaksi lama
tidak boleh terbawa ke commit thread lain. Semua penulisan kini memakai
``db.transaction()`` dan ``_lock`` menjadi jaring pengaman terakhir.
"""

import sqlite3
import time
from datetime import datetime, timedelta, timezone

import pytest

import app as app_module
import poller


def _iso(days=0):
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _audit_count(db, action, summary):
    with db._lock:
        return db.get_conn().execute(
            "SELECT COUNT(*) AS n FROM audit_log WHERE action = ? AND summary = ?",
            (action, summary),
        ).fetchone()["n"]


def _insert_audit(conn, summary):
    conn.execute(
        "INSERT INTO audit_log (ts, username, action, object, summary)"
        " VALUES (?, 'm9', 'm9-test', '', ?)",
        (_iso(), summary),
    )


def test_failed_write_cannot_leak_into_next_commit(fresh_db):
    """Fungsi multi-statement gagal sebelum commit → tidak ada sisa transaksi."""
    with pytest.raises(RuntimeError):
        with fresh_db._lock:
            conn = fresh_db.get_conn()
            _insert_audit(conn, "partial")
            raise RuntimeError("gagal di tengah penulisan")

    # Simulasi thread lain yang commit setelah kegagalan di atas.
    with fresh_db.transaction() as conn:
        _insert_audit(conn, "complete")

    assert _audit_count(fresh_db, "m9-test", "partial") == 0
    assert _audit_count(fresh_db, "m9-test", "complete") == 1


def test_lock_boundary_rolls_back_forgotten_commit(fresh_db):
    """Blok lock yang lupa commit tidak boleh meninggalkan transaksi terbuka."""
    with fresh_db._lock:
        conn = fresh_db.get_conn()
        _insert_audit(conn, "no-commit")
        assert conn.in_transaction is True

    assert fresh_db.get_conn().in_transaction is False
    assert _audit_count(fresh_db, "m9-test", "no-commit") == 0


def test_orphan_transaction_is_rolled_back_before_next_lock_scope(fresh_db):
    """Transaksi dari jalur luar lock dibersihkan saat lock berikutnya diambil."""
    conn = fresh_db.get_conn()
    _insert_audit(conn, "orphan")
    assert conn.in_transaction is True

    with fresh_db._lock:
        assert fresh_db.get_conn().in_transaction is False

    assert _audit_count(fresh_db, "m9-test", "orphan") == 0


def test_nested_failure_poisons_outer_transaction(fresh_db):
    """Kegagalan inner yang ditelan pemanggil tidak boleh menyimpan sebagian."""
    with pytest.raises(sqlite3.OperationalError):
        with fresh_db.transaction() as conn:
            _insert_audit(conn, "outer")
            try:
                with fresh_db.transaction() as inner:
                    _insert_audit(inner, "inner")
                    raise RuntimeError("gagal di transaksi dalam")
            except RuntimeError:
                pass
            # Commit luar harus ditolak karena transaksi sudah tercemar.

    assert _audit_count(fresh_db, "m9-test", "outer") == 0
    assert _audit_count(fresh_db, "m9-test", "inner") == 0


def _mk_device(db):
    return db.create_device(
        {
            "name": "m9-retention",
            "ip": "10.99.0.1",
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
    )


def test_failed_purge_rolls_back_earlier_deletes(fresh_db):
    """Kegagalan di tengah purge_old_data tidak boleh menyimpan DELETE sebelumnya."""
    device_id = _mk_device(fresh_db)
    fresh_db.set_settings(
        {
            "retention_raw_days": "2",
            "retention_events_days": "2",
            "retention_hourly_days": "30",
        }
    )
    with fresh_db.transaction() as conn:
        conn.execute(
            "INSERT INTO device_metrics (device_id, ts, cpu_pct) VALUES (?,?,?)",
            (device_id, _iso(10), 10.0),
        )
        conn.execute(
            "INSERT INTO events (device_id, new_status, message, created_at)"
            " VALUES (?,?,?,?)",
            (device_id, "online", "m9-event", _iso(10)),
        )
        conn.execute(
            "CREATE TRIGGER m9_block_events BEFORE DELETE ON events"
            " BEGIN SELECT RAISE(ABORT, 'm9 block'); END"
        )

    with pytest.raises(sqlite3.DatabaseError):
        fresh_db.purge_old_data()

    # DELETE device_metrics sudah dijalankan sebelum trigger gagal; rollback
    # harus mengembalikannya.
    with fresh_db._lock:
        conn = fresh_db.get_conn()
        assert conn.in_transaction is False
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM device_metrics WHERE device_id = ?",
            (device_id,),
        ).fetchone()["n"] == 1
    with fresh_db.transaction() as conn:
        conn.execute("DROP TRIGGER m9_block_events")

    result = fresh_db.purge_old_data()
    assert result["metrics_deleted"] == 1
    assert result["events_deleted"] == 1


def test_healthz_uses_readonly_connection_not_shared(fresh_db):
    """Health check tidak boleh menyentuh koneksi bersama milik thread lain."""
    poller.LAST_HEARTBEAT = time.time()

    class Boom:
        in_transaction = False

        def execute(self, *args, **kwargs):
            raise AssertionError("healthz memakai koneksi bersama")

        def close(self):
            pass

    # Set langsung (bukan monkeypatch.setattr): teardown fresh_db menutup dan
    # mengosongkan _conn, sehingga tes berikutnya tidak mewarisi koneksi Boom
    # atau koneksi database sementara tes ini.
    fresh_db.connection._conn = Boom()
    fresh_db._conn = Boom()

    response = app_module.app.test_client().get("/healthz")
    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}
    poller.LAST_HEARTBEAT = None
