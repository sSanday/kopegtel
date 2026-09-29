"""nms.notify — notifikasi Telegram dan audit trail."""

import requests

from nms.config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
from nms.db import log_system_event


def send_telegram_alert(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[WARN] Token/Chat ID Telegram belum dikonfigurasi.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        resp = requests.post(url, json=payload, timeout=5)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"[ERROR] Gagal kirim Telegram: {e}")


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
