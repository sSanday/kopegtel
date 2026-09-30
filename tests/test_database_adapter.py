import sqlite3
import unittest

from nms.database import CursorAdapter, _convert_qmark, backend_name


class DatabaseAdapterTests(unittest.TestCase):
    def test_sqlite_remains_default(self):
        self.assertEqual(backend_name(), "sqlite")

    def test_qmark_conversion(self):
        self.assertEqual(_convert_qmark("SELECT ? + ?", (1, 2)), "SELECT %s + %s")

    def test_cursor_adapter_keeps_sqlite_queries(self):
        connection = sqlite3.connect(":memory:")
        cursor = CursorAdapter(connection.cursor())
        cursor.execute("SELECT ? + ? AS total", (2, 3))
        self.assertEqual(cursor.fetchone()[0], 5)
        connection.close()


if __name__ == "__main__":
    unittest.main()
