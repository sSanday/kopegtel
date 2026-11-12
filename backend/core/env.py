"""Versi aplikasi & waktu mulai (dipakai bersama)."""
import os
import time

APP_VERSION = os.environ.get("APP_VERSION", "1.0.0")
SERVER_STARTED = time.monotonic()
