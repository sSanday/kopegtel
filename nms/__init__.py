"""nms — paket modular NMS Dashboard.

Pola migrasi bertahap (strangler): konstanta dan helper dipindah ke sini
satu domain per modul, app.py mengimpornya kembali sampai tiap blueprint
berdiri sendiri. Modul ini tanpa efek samping selain load_dotenv().
"""
