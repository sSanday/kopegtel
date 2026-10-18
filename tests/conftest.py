"""Konfigurasi bersama pytest.

Matikan rate limiter untuk seluruh session test: banyak file login
beruntun dalam window menit yang sama, tanpa ini login bisa terkena 429
TERGANTUNG urutan file (dulu dilakukan lewat import test_fiber.py).
Environment variable dibaca saat app.py diimport pertama kali.
"""

import os

os.environ.setdefault("NMS_DISABLE_LIMITER", "1")
