"""Penerima kejadian E1 (syslog/trap) dan E3 (flow).

Modul ini sengaja tidak melakukan I/O jaringan saat diimpor. Penerima hanya
dinyalakan oleh ``serve.py`` atau saat admin mengaktifkannya.
"""

from .service import INGEST  # noqa: F401
