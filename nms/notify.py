"""nms.notify — notifikasi multi-channel (Telegram, Email/SMTP, Webhook) + audit trail."""

import hashlib
import hmac
import json
import logging
import smtplib
import ssl
from concurrent.futures import (
    TimeoutError as FutureTimeout,
    ThreadPoolExecutor,
    as_completed,
)
from datetime import datetime
from email.mime.text import MIMEText

import requests

from nms.config import (
    ALERT_EMAIL_TO,
    SMTP_FROM,
    SMTP_HOST,
    SMTP_PASSWORD,
    SMTP_PORT,
    SMTP_USER,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    WEBHOOK_SECRET,
    WEBHOOK_URL,
)

logger = logging.getLogger("nms.notify")

_SEV_RANK = {"info": 0, "warning": 1, "critical": 2}
_MIN_SEV_KEYS = {
    "telegram": ("notify_telegram_min_sev", "info"),
    "email": ("notify_email_min_sev", "critical"),
    "webhook": ("notify_webhook_min_sev", "warning"),
}
_CHAT_TIMEOUT_S = 10  # budget total dispatch (semua channel bersamaan)
_SEND_TIMEOUT_S = 10  # timeout jaringan per channel
_DISPATCH_WORKERS = 2


def _severity_rank(name, default="warning"):
    return _SEV_RANK.get(name, _SEV_RANK.get(default, 1))


def _channel_allows(channel, severity):
    """Benar bila severity >= ambang minimum channel dari settings DB."""
    key, default = _MIN_SEV_KEYS[channel]
    stored = default
    try:
        from nms.db import get_setting

        stored = get_setting(key, default, str)
    except Exception:
        stored = default
    return _severity_rank(stored, default) <= _severity_rank(severity, "warning")


def _plain(text):
    """Lepas karakter markdown bintang/underscore/backtick (email & webhook)."""
    for ch in ("*", "_", "`"):
        text = text.replace(ch, "")
    return text


def _first_line(text):
    return (text or "").strip().splitlines()[0] if (text or "").strip() else ""


def channel_status():
    """Status konfigurasi tiap channel: {name: aktif?}."""
    return {
        "telegram": bool(
            TELEGRAM_BOT_TOKEN
            and TELEGRAM_CHAT_ID
            and TELEGRAM_BOT_TOKEN != "your_telegram_bot_token_here"
            and TELEGRAM_CHAT_ID != "your_telegram_chat_id_here"
        ),
        "email": bool(SMTP_HOST and SMTP_PORT and SMTP_FROM and _email_recipients()),
        "webhook": bool(WEBHOOK_URL),
    }


def _active_channels(message, severity, event):
    """Kumpulkan channel aktif & lolos ambang severity. Radar DB di sini."""
    channels = []
    for name, fn in (
        ("telegram", lambda: send_telegram(message)),
        ("email", lambda: send_email(message, severity, event)),
        ("webhook", lambda: send_webhook(message, severity, event)),
    ):
        if not channel_status()[name]:
            continue
        if not _channel_allows(name, severity):
            continue
        channels.append((name, fn))
    return channels


def send_alert(message, severity="warning", event=""):
    """Kirim alert ke semua channel aktif tanpa membekukan job scheduler.

    Channel berjalan paralel (ThreadPoolExecutor, max_workers=2), tiap
    channel berbatas waktu ~10 detik, dan kegagalan satu channel tidak
    mengganggu channel lain. Kembalikan {channel: "ok"|error singkat}.
    """
    sev = severity if severity in _SEV_RANK else "warning"
    try:
        channels = _active_channels(message, sev, event)
    except Exception as exc:  # jangan sampai dispatch membunuh caller job
        logger.exception("penyusunan channel gagal (%s)", type(exc).__name__)
        return {}
    if not channels:
        return {}
    results = {}
    with ThreadPoolExecutor(max_workers=_DISPATCH_WORKERS) as executor:
        futures = {executor.submit(fn): name for name, fn in channels}
        try:
            for fut in as_completed(futures, timeout=_CHAT_TIMEOUT_S):
                name = futures[fut]
                try:
                    fut.result()
                    results[name] = "ok"
                except Exception as exc:
                    logger.warning(
                        "[NOTIFY] kirim %s gagal (%s)", name, type(exc).__name__
                    )
                    results[name] = type(exc).__name__
        except FutureTimeout:
            for fut, name in futures.items():
                if not fut.done():
                    fut.cancel()
                    results.setdefault(name, "timeout")
    return results


# ---- Channel: Telegram ------------------------------------------------------


def send_telegram(message):
    """Kirim Markdown ke Telegram; raise Exception bila gagal."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    resp = requests.post(url, json=payload, timeout=_SEND_TIMEOUT_S)
    resp.raise_for_status()
    return resp


# ---- Channel: Email (SMTP) --------------------------------------------------


def _email_recipients():
    out = []
    for addr in (ALERT_EMAIL_TO or "").split(","):
        addr = addr.strip()
        if addr and addr != "you@example.com":
            out.append(addr)
    return out


def send_email(message, severity, event):
    """Kirim email SMTP (STARTTLS atau SSL); raise Exception bila gagal.

    Subjek: "[NMS][SEVERITY] baris pertama", isi bebas karakter markdown.
    """
    tos = _email_recipients()
    if not tos:
        raise ValueError("ALERT_EMAIL_TO kosong")
    subject = f"[NMS][{severity.upper()}] {_plain(_first_line(message))}"[:200]
    msg = MIMEText(_plain(message), "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = SMTP_FROM
    msg["To"] = ", ".join(tos)
    if int(SMTP_PORT) == 465:
        with smtplib.SMTP_SSL(
            SMTP_HOST, int(SMTP_PORT), timeout=_SEND_TIMEOUT_S
        ) as smtp:
            if SMTP_USER and SMTP_PASSWORD:
                smtp.login(SMTP_USER, SMTP_PASSWORD)
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(SMTP_HOST, int(SMTP_PORT), timeout=_SEND_TIMEOUT_S) as smtp:
            smtp.ehlo()
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo()
            if SMTP_USER and SMTP_PASSWORD:
                smtp.login(SMTP_USER, SMTP_PASSWORD)
            smtp.send_message(msg)


# ---- Channel: Webhook generik -----------------------------------------------


def send_webhook(message, severity, event):
    """POST JSON ke WEBHOOK_URL; raise Exception bila gagal.

    Body: {severity, event, message, timestamp, source:"nms"} tanpa
    karakter markdown. WEBHOOK_SECRET opsional -> header
    X-NMS-Signature = "sha256=<HMAC-SHA256(body)>". Secret tidak dikirim.
    """
    payload = {
        "severity": severity,
        "event": event or "",
        "message": _plain(message),
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "source": "nms",
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if WEBHOOK_SECRET:
        sig = hmac.new(WEBHOOK_SECRET.encode("utf-8"), body, hashlib.sha256).hexdigest()
        headers["X-NMS-Signature"] = "sha256=" + sig
    resp = requests.post(
        WEBHOOK_URL, data=body, headers=headers, timeout=_SEND_TIMEOUT_S
    )
    resp.raise_for_status()
    return resp


# ---- Pembungkus lama: 26 pemanggil tak perlu diubah -------------------------


def send_telegram_alert(message):
    """Pembungkus lama: kirim ke SEMUA channel aktif (Default warning)."""
    try:
        send_alert(message, severity="warning", event="legacy")
    except Exception as exc:
        logger.warning("[NOTIFY] dispatch gagal (%s)", type(exc).__name__)


def audit(username, action, detail="", target="", ip_address=""):
    try:
        from flask import request, has_request_context
        from nms.db import get_db
        from datetime import datetime

        if not ip_address and has_request_context():
            ip_address = request.headers.get("X-Forwarded-For", request.remote_addr)
            if ip_address:
                ip_address = ip_address.split(",")[0].strip()

        conn, c = get_db()
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        c.execute(
            "INSERT INTO audit_logs (timestamp, username, action, target, detail, ip_address) VALUES (?, ?, ?, ?, ?, ?)",
            (ts, username, action, target, detail, ip_address),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[AUDIT ERROR] {e}")
