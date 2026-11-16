"""Ping ICMP dengan timeout 2 detik.

Memakai utilitas ``ping`` bawaan sistem operasi karena raw-socket ICMP
di Windows membutuhkan hak admin. Output ping dikenali untuk bahasa
Inggris, Indonesia, Jerman, Prancis, Spanyol, Portugis, Italia, Belanda,
Polandia, Rusia, Jepang, dan Tiongkok (China). Locale lain akan dianggap
down (fail-safe); tambahkan tokennya di bawah bila diperlukan.
"""

import re
import subprocess
import sys

TIMEOUT_SECONDS = 2
_LATENCY_RE = re.compile(
    r"\b(?:time|waktu|zeit|temps|tiempo|tempo|durata|tijd|czas|время|時間|时间)[=<]\s*"
    r"(\d+(?:[.,]\d+)?)\s*(?:ms|мс)",
    re.IGNORECASE,
)
# Baris "balasan" Windows/Linux multi-bahasa. Tanpa token ini, output
# dianggap BUKAN balasan sukses (fail-safe "down").
_REPLY_RE = re.compile(
    r"(reply from|balasan dari|bytes from|"  # EN / ID+MS / Linux
    r"antwort von|"  # DE
    r"réponse de|reponse de|"  # FR (dengan/tanpa aksen)
    r"respuesta desde|"  # ES
    r"resposta de|"  # PT
    r"risposta da|"  # IT
    r"antwoord van|"  # NL
    r"odpowiedź z|odpowiedz z|"  # PL (dengan/tanpa aksen)
    r"ответ от|"  # RU
    r"からの応答|"  # JA
    r"的回复)",  # ZH
    re.IGNORECASE,
)
# Baris kegagalan Windows/Linux multi-bahasa. Windows dapat mengembalikan
# exit code 0 walau ICMP gagal (mis. "Destination host unreachable" dari
# stack lokal), jadi returncode == 0 SAJA tidak cukup (L-6).
# Catatan: token "TTL expired" baru tersedia untuk EN/ID; di locale lain
# kegagalan TTL tidak dikenali (tetap fail-safe bila tidak ada token
# balasan yang cocok, atau rc != 0).
_FAILURE_RE = re.compile(
    r"(timed out|habisan waktu|habis waktu|host unreachable|tidak dapat dijangkau|"
    r"destination host|general failure|kesalahan umum|ttl expired|ttl habis|"
    r"expired in transit|undeliverable|"  # EN / ID
    r"zeitüberschreitung|nicht erreichbar|"  # DE
    r"délai d'attente|impossible de joindre|"  # FR
    r"tiempo de espera|destino inaccesible|destino inalcanzable|"  # ES
    r"esgotado o tempo limite|tempo limite do pedido|destino inacessível|"  # PT
    r"richiesta scaduta|non raggiungibile|"  # IT
    r"time-out bij aanvraag|time-out tijdens|doelhost onbereikbaar|"  # NL
    r"przekroczono limit czasu|nie jest dostępny|"  # PL
    r"превышен интервал ожидания|узел назначения недоступен|"  # RU
    r"タイムアウト|到達できません|"  # JA
    r"请求超时|无法访问目标|无法到达目标)",  # ZH
    re.IGNORECASE,
)
# Sub-token untuk klasifikasi reason == "timeout" (vs "unreachable").
_TIMEOUT_RE = re.compile(
    r"(timed out|habis waktu|zeitüberschreitung|délai d'attente|tiempo de espera|"
    r"esgotado o tempo|tempo limite|richiesta scaduta|time-out|limit czasu|"
    r"интервал ожидания|タイムアウト|请求超时)",
    re.IGNORECASE,
)


def check_ping(ip):
    if sys.platform.startswith("win"):
        args = ["ping", "-n", "1", "-w", str(TIMEOUT_SECONDS * 1000), ip]
    else:
        args = ["ping", "-c", "1", "-W", str(TIMEOUT_SECONDS), ip]

    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS + 2,
        )
    except subprocess.TimeoutExpired:
        return {"method": "ping", "ok": False, "latency_ms": None, "reason": "timeout"}
    except OSError:
        return {"method": "ping", "ok": False, "latency_ms": None, "reason": "unreachable"}

    output = (result.stdout or "") + (result.stderr or "")
    latency_ms = None
    match = _LATENCY_RE.search(output)
    if match:
        latency_ms = float(match.group(1).replace(",", "."))

    # Returncode 0 tidak cukup: balasannya harus terdeteksi DAN bukan baris
    # kegagalan ("Destination host unreachable" bisa bernilai 0 di Windows).
    # Latency saja (mis. "Zeit<1ms") TIDAK cukup sebagai bukti hidup —
    # locale yang tidak dikenal bisa cocok di regex latency; hanya token
    # balasan multi-bahasa yang menentukan. Fail-safe: tidak cocok = down.
    reachable = (
        result.returncode == 0
        and _FAILURE_RE.search(output) is None
        and _REPLY_RE.search(output) is not None
    )

    if reachable:
        return {"method": "ping", "ok": True, "latency_ms": latency_ms, "reason": None}

    lowered = output.lower()
    if _TIMEOUT_RE.search(lowered):
        reason = "timeout"
    else:
        reason = "unreachable"
    return {"method": "ping", "ok": False, "latency_ms": None, "reason": reason}
