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


def audit(username, action, detail=""):
    try:
        log_system_event("AUDIT", username, f"{action} {detail}".strip())
    except Exception:
        pass
