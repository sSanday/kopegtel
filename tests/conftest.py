"""Fixture pytest: database sementara per sesi, env uji (bukan .env asli)."""

import os
import sys
import tempfile

import pytest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_test_dir = tempfile.mkdtemp(prefix="nms-test-")
os.environ["DATABASE_PATH"] = os.path.join(_test_dir, "test.db")
os.environ["SECRET_KEY"] = "test-secret-key-0123456789abcdef0123456789abcdef"
os.environ["ENCRYPTION_KEY"] = "9fG9f2iVfTq7Zuk5rxlb3bMaOQrVvZ3kM7yD5KBmUE8="  # kunci Fernet khusus uji
os.environ["ADMIN_USERNAME"] = "admin"
os.environ["ADMIN_PASSWORD"] = "admin-test-password"
os.environ["ALLOWED_NETWORKS"] = ""

sys.path.insert(0, os.path.join(BASE_DIR, "backend"))

import db  # noqa: E402

db.init_db()


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    """Database sementara per-test (terisolasi)."""
    path = str(tmp_path / "test.db")
    monkeypatch.setenv("DATABASE_PATH", path)
    # db package: DB_PATH asli ada di connection; package namespace hanya alias.
    for target in (db, db.connection):
        monkeypatch.setattr(target, "DB_PATH", path)
        with db.connection._lock:
            if getattr(target, "_conn", None) is not None:
                target._conn.close()
            target._conn = None
    db.init_db()
    try:
        yield db
    finally:
        # Tutup koneksi sebelum monkeypatch mengembalikan DB_PATH. Tanpa ini
        # tes berikutnya mewarisi koneksi ke database sementara tes ini.
        with db.connection._lock:
            for target in (db, db.connection):
                if getattr(target, "_conn", None) is not None:
                    target._conn.close()
                target._conn = None
