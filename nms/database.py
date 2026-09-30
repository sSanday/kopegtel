import os
import re
import sqlite3
import time


class DatabaseBackendError(RuntimeError):
    pass


def backend_name():
    return os.environ.get("DATABASE_BACKEND", "sqlite").strip().lower()


def _convert_qmark(query, params):
    if not params or "%s" in query or "?" not in query:
        return query
    parts = query.split("?")
    if len(parts) - 1 != len(params):
        return query
    return "%s".join(parts)


class CursorAdapter:
    def __init__(self, cursor, postgres=False):
        self._cursor = cursor
        self._postgres = postgres

    def execute(self, query, params=()):
        if self._postgres:
            query = _convert_qmark(query, params)
        self._cursor.execute(query, params)
        return self

    def executemany(self, query, params):
        if self._postgres:
            params = list(params)
            if params:
                query = _convert_qmark(query, params[0])
        self._cursor.executemany(query, params)
        return self

    def __getattr__(self, name):
        return getattr(self._cursor, name)


class ConnectionAdapter:
    def __init__(self, connection, postgres=False):
        self._connection = connection
        self._postgres = postgres

    def cursor(self):
        return CursorAdapter(self._connection.cursor(), self._postgres)

    def execute(self, query, params=()):
        return self.cursor().execute(query, params)

    def commit(self):
        return self._connection.commit()

    def rollback(self):
        return self._connection.rollback()

    def close(self):
        return self._connection.close()

    def __getattr__(self, name):
        return getattr(self._connection, name)


def connect(database_path=None, database_url=None):
    name = backend_name()
    if name == "sqlite":
        path = database_path or os.environ.get("NMS_DB_PATH", "network.db")
        connection = sqlite3.connect(path, timeout=30, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        return ConnectionAdapter(connection)
    if name == "postgresql":
        if not database_url:
            database_url = os.environ.get("DATABASE_URL")
        if not database_url:
            raise DatabaseBackendError("DATABASE_URL wajib diisi untuk PostgreSQL")
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise DatabaseBackendError(
                "Install psycopg[binary] untuk PostgreSQL"
            ) from exc
        connection = psycopg.connect(database_url, row_factory=dict_row)
        return ConnectionAdapter(connection, postgres=True)
    raise DatabaseBackendError(f"DATABASE_BACKEND tidak didukung: {name}")


def commit_with_retry(connection, retries=5):
    for attempt in range(retries):
        try:
            connection.commit()
            return True
        except Exception as exc:
            if attempt == retries - 1 or "lock" not in str(exc).lower():
                raise
            time.sleep(0.2 * (attempt + 1))
    return False
