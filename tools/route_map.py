"""Cetak daftar route aplikasi (method, rule, endpoint), terurut.

Dipakai untuk baseline/regresi route map (T0 & T5). Memakai DATABASE_PATH
sementara; tidak pernah menyentuh backend/nms.db.

Penggunaan:
    python tools/route_map.py             # cetak ke stdout
    python tools/route_map.py FILE        # tulis ke file
"""
import os
import sys
import tempfile
from contextlib import redirect_stdout

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")


def collect():
    tmp = tempfile.mkdtemp(prefix="nms-routemap-")
    # Lingkungan uji mandiri — tidak membaca .env nyata.
    os.environ.update(
        {
            "DATABASE_PATH": os.path.join(tmp, "routemap.db"),
            "SECRET_KEY": "route-map-secret-key-0123456789abcdef",
            "ENCRYPTION_KEY": "9fG9f2iVfTq7Zuk5rxlb3bMaOQrVvZ3kM7yD5KBmUE8=",
            "ADMIN_PASSWORD": "routemap-admin-password",
        }
    )
    sys.path.insert(0, os.path.join(BASE_DIR, "backend"))
    try:
        # Redam output seed admin saat import; tidak menyentuh stderr (error tetap tampil).
        with open(os.devnull, "w", encoding="utf-8") as sink:
            with redirect_stdout(sink):
                import app as app_module
            flask_app = app_module.app
        rows = []
        for rule in flask_app.url_map.iter_rules():
            for method in sorted(rule.methods or set()):
                if method in METHODS:
                    rows.append((rule.rule, method, rule.endpoint))
        rows.sort(key=lambda item: (item[0], item[1], item[2]))
        return [f"{method} {rule} -> {endpoint}" for rule, method, endpoint in rows]
    finally:
        try:
            import db as db_module

            conn = db_module.get_conn()
            conn.close()
        except Exception:
            pass


def main():
    lines = collect()
    text = "\n".join(lines) + f"\n# total: {len(lines)}\n"
    if len(sys.argv) > 1:
        with open(sys.argv[1], "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        print(f"menulis {len(lines)} baris ke {sys.argv[1]}")
    else:
        sys.stdout.write(text)


if __name__ == "__main__":
    main()
