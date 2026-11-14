"""Netralisasi sel CSV agar tidak dieksekusi sebagai formula spreadsheet.

Sel yang dibuka di Excel/LibreOffice dapat dianggap formula bila diawali
``=``, ``+``, ``-``, atau ``@``. Nilai tersebut berasal dari input pengguna
(mis. username login gagal, nama perangkat, atau ringkasan audit), jadi setiap
sel teks harus diberi awalan apostrof sebelum ditulis ke CSV.
"""

_FORMULA_PREFIXES = ("=", "+", "-", "@")
_CONTROL_PREFIXES = ("\t", "\r", "\n")


def csv_cell(value):
    """Kembalikan satu nilai yang aman ditulis sebagai sel CSV.

    Angka dan tipe non-teks dibiarkan apa adanya agar tetap terbaca sebagai
    angka. Untuk teks, karakter pertama yang terlihat juga diperiksa supaya
    spasi/tab di depan formula tidak menjadi celah bypass.
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        return value
    visible = value.lstrip()
    if value[:1] in _CONTROL_PREFIXES or visible[:1] in _FORMULA_PREFIXES:
        return "'" + value
    return value


def csv_row(values):
    """Sanitasi seluruh sel dalam satu baris CSV."""
    return [csv_cell(value) for value in values]
