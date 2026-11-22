"""E3 — kolektor Traffic Flow (UDP v5/v9/IPFIX).

Tidak melakukan I/O jaringan saat diimpor. Hanya dinyalakan oleh
``serve.py`` atau saat admin mengaktifkannya.
"""

from .service import FLOW  # noqa: F401
