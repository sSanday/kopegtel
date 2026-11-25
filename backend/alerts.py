"""Notifikasi: pengiriman nyata ke Telegram (Bot API) dan SMTP, dengan retry.

- Dipanggil poller saat masalah dibuka (bukan saat pemeliharaan).
- Setiap percobaan dicatat ke tabel ``action_log`` (tanpa secret).
- Konfigurasi kanal tersimpan terenkripsi (lihat ``notification_channels``).
"""

import json
import logging
import smtplib
import socket
import ssl
import threading
import time
from collections import deque
from datetime import datetime, timezone
from email.message import EmailMessage
from urllib import request as urlrequest
from urllib import error as urlerror

import db
import netguard

log = logging.getLogger("nms.alerts")

_SEVERITY_RANK = {"disaster": 5, "high": 4, "average": 3, "warning": 2, "information": 1}
RETRY_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 1.0
TIMEOUT_SECONDS = 6


def render_template(body, problem, device):
    """Ganti placeholder {perangkat} {ip} {severity} {pesan} {waktu} {link} {port} {rx} {redaman}."""
    tz_name = db.get_setting("timezone", "Asia/Jakarta")
    now = datetime.now(timezone.utc)
    if tz_name == "Asia/Jakarta":
        from datetime import timedelta

        waktu = (now + timedelta(hours=7)).strftime("%d-%m-%Y %H:%M:%S WIB")
    else:
        waktu = now.strftime("%d-%m-%Y %H:%M:%S UTC")

    def _num(value, unit):
        return "-" if value is None else f"{float(value):.2f}{unit}"

    replacements = {
        "{perangkat}": device.get("name") or "-",
        "{ip}": device.get("ip") or "-",
        "{severity}": (problem.get("severity") or "").upper(),
        "{pesan}": problem.get("message") or "-",
        "{waktu}": waktu,
        "{link}": problem.get("link_name") or "-",
        "{port}": problem.get("port") or "-",
        "{rx}": _num(problem.get("rx"), " dBm"),
        "{redaman}": _num(problem.get("redaman"), " dB"),
    }
    text = body or ""
    for key, value in replacements.items():
        text = text.replace(key, value)
    return text


def _redact_token(text, bot_token):
    """Sembunyikan bot_token bila muncul di pesan (tidak pernah di-log/dikirim)."""
    if bot_token and isinstance(text, str):
        text = text.replace(bot_token, "***")
    return text


def send_telegram(config, text):
    bot_token = (config or {}).get("bot_token") or ""
    chat_id = (config or {}).get("chat_id") or ""
    if not bot_token or not chat_id:
        return False, "bot_token atau chat_id belum diisi."
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = json.dumps({"chat_id": chat_id, "text": text, "disable_web_page_preview": True}).encode("utf-8")
    req = urlrequest.Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
    # Middlebox jaringan kampus ini me-reset sebagian koneksi TLS 1.3 ke
    # api.telegram.org; TLS 1.2 terbukti stabil pada pengujian di jaringan ini.
    context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
    context.maximum_version = ssl.TLSVersion.TLSv1_2
    try:
        with urlrequest.urlopen(req, timeout=TIMEOUT_SECONDS, context=context) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urlerror.HTTPError as exc:
        # Detail respons Telegram hanya ke log; body bisa memuat teks arbitrer.
        log.warning("Telegram menolak (HTTP %s)", exc.code)
        return False, f"Telegram menolak permintaan (HTTP {exc.code}). Periksa bot_token/chat_id."
    except Exception as exc:
        # Detail mentah bisa memuat URL (yang berisi bot_token) - jangan
        # diteruskan ke API/klien; cukup ke log sebagai nama kelas exception.
        log.warning("Gagal menghubungi Telegram (%s)", type(exc).__name__)
        return False, "Gagal menghubungi Telegram. Periksa jaringan dan bot_token."
    if not data.get("ok"):
        description = _redact_token(str(data.get("description") or ""), bot_token)[:200]
        return False, f"Telegram menolak: {description or 'alasan tidak diketahui'}"
    return True, "Terkirim ke Telegram."


class _PinnedSMTP(smtplib.SMTP):
    """SMTP yang terhubung ke IP hasil validasi, tetapi memakai nama asli
    untuk EHLO/SNI sehingga verifikasi sertifikat STARTTLS tetap benar.

    Semua IP hasil resolusi dicoba berurutan; bila satu alamat tidak dapat
    dihubungi, alamat publik berikutnya dipakai tanpa resolve DNS ulang.
    """

    def __init__(self, host, port, addresses, **kwargs):
        self._pinned_addresses = list(addresses)
        self._pinned_address = None
        super().__init__(host, port, **kwargs)

    def _get_socket(self, host, port, timeout):
        last_error = None
        for address in self._pinned_addresses:
            try:
                sock = socket.create_connection(
                    (address, port), timeout, self.source_address
                )
            except OSError as exc:
                last_error = exc
                continue
            self._pinned_address = address
            return sock
        raise last_error or OSError("Tidak ada alamat SMTP yang dapat dihubungi.")


def _smtp_failure_detail(exc):
    """Pesan galat SMTP yang aman ditampilkan ke API (tanpa banner server)."""
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return "Autentikasi SMTP ditolak."
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return "Server SMTP menolak alamat tujuan."
    if isinstance(exc, smtplib.SMTPSenderRefused):
        return "Server SMTP menolak alamat pengirim."
    if isinstance(exc, smtplib.SMTPDataError):
        return "Server SMTP menolak isi pesan."
    if isinstance(exc, smtplib.SMTPHeloError):
        return "Sapaan SMTP ditolak server."
    if isinstance(exc, smtplib.SMTPNotSupportedError):
        return "Server SMTP tidak mendukung fitur yang diminta."
    if isinstance(exc, smtplib.SMTPServerDisconnected):
        return "Server SMTP memutus koneksi."
    if isinstance(exc, smtplib.SMTPConnectError):
        return "Koneksi SMTP ditolak server."
    if isinstance(exc, smtplib.SMTPResponseException):
        return "Server SMTP menolak permintaan."
    if isinstance(exc, TimeoutError):
        return "Koneksi SMTP timeout."
    if isinstance(exc, ssl.SSLError):
        return "Negosiasi TLS SMTP gagal."
    if isinstance(exc, OSError):
        return "Koneksi SMTP gagal."
    return "Gagal mengirim lewat SMTP."


def send_smtp(config, subject, body):
    cfg = config or {}
    host = str(cfg.get("host") or "").strip()
    try:
        port = int(cfg.get("port") or 587)
    except (TypeError, ValueError):
        port = 587
    from_addr = cfg.get("from_addr") or cfg.get("username") or ""
    to_addr = cfg.get("to_addr") or ""
    if not host or not from_addr or not to_addr:
        return False, "host, from_addr, atau to_addr SMTP belum diisi."

    try:
        host = netguard.normalize_host(host)
        addresses = netguard.resolve_addresses(
            host,
            port,
            allow_private=netguard.as_bool(cfg.get("allow_private")),
        )
    except ValueError as exc:
        return False, f"Target SMTP ditolak: {exc}"

    use_tls = netguard.as_bool(cfg.get("use_tls", True))
    username = cfg.get("username") or ""
    password = cfg.get("password") or ""
    try:
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = from_addr
        message["To"] = to_addr
        message.set_content(body)
        with _PinnedSMTP(host, port, addresses, timeout=TIMEOUT_SECONDS) as server:
            server.ehlo()
            if use_tls:
                context = ssl.create_default_context()
                server.starttls(context=context)
                server.ehlo()
            if username:
                server.login(username, password)
            server.send_message(message)
    except ValueError:
        return False, "Alamat email tidak valid."
    except Exception as exc:  # noqa: BLE001 - detail mentah tidak dikirim ke klien
        log.warning(
            "SMTP gagal (%s) host=%s port=%s",
            type(exc).__name__,
            host,
            port,
        )
        return False, _smtp_failure_detail(exc)
    return True, f"Terkirim ke {to_addr}."


def send_with_retries(send_fn, attempts=RETRY_ATTEMPTS):
    last_ok, last_detail = False, "Belum dicoba."
    for attempt in range(1, attempts + 1):
        ok, detail = send_fn()
        last_ok, last_detail = ok, detail
        if ok:
            return True, detail, attempt
        if attempt < attempts:
            time.sleep(RETRY_DELAY_SECONDS)
    return False, last_detail, attempts


def channel_test_message():
    return (
        "Uji kirim NMS Kopegtel\n"
        "Ini pesan percobaan dari modul notifikasi.\n"
        "Jika Anda menerima pesan ini, kanal berfungsi dengan baik."
    )


def _send_via_channel(channel, config, subject, text, problem=None):
    if channel["type"] == "telegram":
        return send_with_retries(lambda: send_telegram(config, text))
    if channel["type"] == "smtp":
        return send_with_retries(lambda: send_smtp(config, subject, text))
    if channel["type"] == "webhook":
        import webhook

        return webhook.send_webhook(config, subject, text, problem=problem)
    return False, f"Tipe kanal tidak dikenal: {channel['type']}", 0


def send_test(channel_id):
    """Kirim pesan uji lewat kanal tertentu (dipakai tombol 'Uji kirim')."""
    channel = db.get_channel(channel_id)
    if channel is None:
        return False, "Kanal tidak ditemukan."
    config = db.channel_config(channel_id)
    if not channel["enabled"]:
        return False, "Kanal sedang dinonaktifkan."
    ok, detail, _attempts = _send_via_channel(
        dict(channel), config, "[NMS Kopegtel] Uji kirim", channel_test_message()
    )
    status = "sent" if ok else "failed"
    db.add_action_log(None, channel["id"], None, None, status, f"Uji kirim: {detail}")
    return ok, detail


def _rule_matches(rule, severity_rank, device):
    if not rule["enabled"]:
        return False
    if severity_rank < _SEVERITY_RANK.get(rule["min_severity"], 0):
        return False
    if rule.get("device_id") and rule["device_id"] != device["id"]:
        return False
    if rule.get("group_id") and rule["group_id"] != device.get("group_id"):
        return False
    return True


_STORM = {"entries": deque(maxlen=1000), "active_until": 0.0}
_STORM_LOCK = threading.Lock()
_STORM_WINDOW_SECONDS = 60.0
_STORM_MAX_LINES = 25


def _storm_threshold():
    try:
        value = int(db.get_setting("notification_storm_threshold", "5"))
    except (TypeError, ValueError):
        value = 5
    return min(100, max(2, value))


def _storm_gate(problem, device):
    """Kendali badai: >N masalah dalam 60 detik -> satu pesan ringkasan."""
    now = time.time()
    with _STORM_LOCK:
        entries = _STORM["entries"]
        while entries and now - entries[0]["ts"] >= _STORM_WINDOW_SECONDS:
            entries.popleft()
        entries.append({
            "ts": now,
            "problem_id": problem.get("id"),
            "kind": problem.get("kind"),
            "severity": problem.get("severity"),
            "device": device.get("name"),
            "message": (problem.get("message") or "")[:120],
        })
        if now < _STORM["active_until"]:
            return "suppressed"
        snapshot = list(entries)
    # Hitung di luar lock agar db.get_setting tidak menahan lock; cek ulang
    # active_until di dalam lock supaya hanya satu worker yang mengirim ringkasan.
    if len({entry["problem_id"] for entry in snapshot}) >= _storm_threshold():
        with _STORM_LOCK:
            if now < _STORM["active_until"]:
                return "suppressed"
            _STORM["active_until"] = now + _STORM_WINDOW_SECONDS
        return "summary"
    return None


def _send_storm_summary(problem, device, matched):
    with _STORM_LOCK:
        entries = list(_STORM["entries"])
        _STORM["entries"].clear()
    lines = [f"Badai notifikasi: {len(entries)} problem dalam 60 detik terakhir:"]
    for entry in entries[-_STORM_MAX_LINES:]:
        lines.append(
            f"- [{(entry['severity'] or '').upper()}] {entry['kind']} "
            f"{entry['device']}: {entry['message']}"
        )
    text = "\n".join(lines)
    subject = f"[NMS Kopegtel] BADAI NOTIFIKASI - {len(entries)} problem"
    seen = set()
    if not matched:
        matched = [rule for rule in db.list_rules() if rule.get("enabled")]
    for rule in matched:
        channel_id = rule.get("channel_id")
        if channel_id in seen:
            continue
        seen.add(channel_id)
        channel = db.get_channel(channel_id)
        if channel is None:
            continue
        config = db.channel_config(channel_id)
        storm_problem = {
            "severity": "high", "kind": "notification_storm",
            "problem_id": problem.get("id"), "device_id": device.get("id"),
            "device_name": device.get("name"),
        }
        ok, detail, attempts = _send_via_channel(
            dict(channel), config, subject, text, problem=storm_problem
        )
        db.add_action_log(
            rule.get("id"), channel_id, device.get("id"), None,
            "sent" if ok else "failed",
            f"Ringkasan badai ({len(entries)} masalah): {detail} (percobaan {attempts}x)",
        )


def dispatch_problem(problem):
    """Kirim notifikasi untuk masalah yang BARU terbuka, sesuai aturan aksi."""
    device = db.get_device(problem["device_id"])
    if device is None:
        return []

    rules = db.list_rules()
    severity_rank = _SEVERITY_RANK.get(problem.get("severity"), 0)
    matched = [rule for rule in rules if _rule_matches(rule, severity_rank, device)]

    storm = _storm_gate(problem, device)
    if storm is not None:
        if storm == "suppressed":
            for rule in matched:
                db.add_action_log(
                    rule["id"], rule.get("channel_id"), device["id"], problem["id"],
                    "skipped", "Badai notifikasi: digabung ke ringkasan.",
                )
            return [(0, False, "Badai notifikasi (digabung)")]
        _send_storm_summary(problem, device, matched)
        return [(0, True, "Ringkasan badai terkirim")]

    outcomes = []

    for rule in matched:
        if rule.get("escalation_policy_id"):
            try:
                import escalation as escalation_engine

                escalation_engine.register_for_problem(problem["id"], rule["escalation_policy_id"])
                db.add_action_log(
                    rule["id"], rule.get("channel_id"), device["id"], problem["id"],
                    "skipped",
                    f"Eskalasi dijadwalkan sesuai kebijakan #{rule['escalation_policy_id']}.",
                )
                outcomes.append((rule["id"], True, "Eskalasi dijadwalkan"))
            except Exception:
                log.exception("Registrasi eskalasi untuk problem %s gagal.", problem.get("id"))
                db.add_action_log(
                    rule["id"], rule.get("channel_id"), device["id"], problem["id"],
                    "failed", "Registrasi eskalasi gagal.",
                )
                outcomes.append((rule["id"], False, "Registrasi eskalasi gagal"))
            continue

        template_row = db.get_template(rule["template_id"])
        channel_row = db.get_channel(rule["channel_id"])
        if template_row is None or channel_row is None:
            db.add_action_log(rule["id"], None, device["id"], problem["id"], "failed",
                              "Template atau kanal aturan ini sudah tidak ada.")
            outcomes.append((rule["id"], False, "Template/kanal hilang"))
            continue
        if not channel_row["enabled"]:
            db.add_action_log(rule["id"], channel_row["id"], device["id"], problem["id"], "skipped",
                              "Kanal dinonaktifkan.")
            outcomes.append((rule["id"], False, "Kanal nonaktif"))
            continue

        text = render_template(template_row["body"], problem, device)
        subject = f"[NMS Kopegtel] {(problem.get('severity') or '').upper()} - {device.get('name')}"
        config = db.channel_config(channel_row["id"])
        webhook_problem = {
            "severity": problem.get("severity"), "kind": problem.get("kind"),
            "problem_id": problem.get("id"), "device_id": device.get("id"),
            "device_name": device.get("name"),
        }
        ok, detail, attempts = _send_via_channel(
            dict(channel_row), config, subject, text, problem=webhook_problem
        )
        status = "sent" if ok else "failed"
        db.add_action_log(
            rule["id"], channel_row["id"], device["id"], problem["id"], status,
            f"{detail} (percobaan {attempts}x)",
        )
        outcomes.append((rule["id"], ok, detail))

    return outcomes
