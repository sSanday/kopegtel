"""Regresi format durasi masalah di pemantauan (monitoring.js)."""

import os
import re

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_problem_duration_uses_full_words():
    path = os.path.join(BASE_DIR, "assets", "js", "monitoring.js")
    with open(path, encoding="utf-8") as fh:
        src = fh.read()

    # Durasi harus memakai kata penuh agar tidak ambigu (h/j/m).
    assert "hari ${hours} jam" in src, "durasi hari harus memakai 'hari ... jam'"
    assert "jam ${mins} menit" in src, "durasi jam harus memakai 'jam ... menit'"
    assert "${mins} menit" in src, "durasi menit harus memakai 'menit'"

    # Singkatan lama tidak boleh dipakai lagi.
    assert "days}h ${hours}j" not in src
    assert "hours}j ${mins}m" not in src
    assert "${mins}m" not in src