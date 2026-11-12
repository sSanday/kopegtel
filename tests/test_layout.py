"""Regression struktur paket (T5): path default DB & modul hasil refactor."""

import os

import db


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_default_db_path_points_to_project_backend():
    # db/connection.py berada dua level di bawah root; BASE_DIR harus root proyek.
    assert db.connection.BASE_DIR == ROOT
    assert os.path.join(db.connection.BASE_DIR, "backend", "nms.db") == os.path.join(ROOT, "backend", "nms.db")


def test_db_package_reexports_public_names():
    for name in ("get_conn", "init_db", "query_mt_devices", "create_mt_backup", "reencrypt_all"):
        assert hasattr(db, name), name


def test_routes_and_core_packages_importable():
    import core.guard  # noqa: F401
    import core.metrics  # noqa: F401
    import routes.admin  # noqa: F401
    import routes.devices  # noqa: F401
    import routes.mikrotik  # noqa: F401
