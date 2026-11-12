"""E2 — mesin eskalasi: job tiap 30 detik, idempoten, tahan restart.

Alur:
- Aturan aksi dengan ``escalation_policy_id`` tidak mengirim langsung;
  mesin ini yang mengirim langkah-langkahnya (atau ringkasan badai untuk
  jalur kirim normal — lihat ``alerts.dispatch_problem``).
- Kondisi kirim: problem masih terbuka, belum diakui, tidak dalam jendela
  pemeliharaan, dan tidak tertekan parent.
- Ack menghentikan langkah berikutnya.
- Problem yang pulih mengirim notifikasi pemulihan ke target langkah
  terakhir yang terkirim (atau langkah pertama bila belum ada yang terkirim).
"""

import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import db

log = logging.getLogger("nms.escalation")

JAKARTA = ZoneInfo("Asia/Jakarta")


def _parse_iso(ts):
    if not ts:
        return None
    try:
        parsed = datetime.strptime(str(ts), "%Y-%m-%dT%H:%M:%SZ")
        return parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _now_jakarta(now_utc=None):
    current = now_utc or datetime.now(timezone.utc)
    return current.astimezone(JAKARTA)


def _parse_hhmm(text):
    try:
        hour, minute = str(text or "").split(":")[:2]
        return int(hour), int(minute)
    except (ValueError, TypeError):
        return None


def _business_open(policy, now_jkt):
    if not policy.get("business_hours_enabled"):
        return True
    days = str(policy.get("business_days") or "1,2,3,4,5")
    try:
        allowed = {int(item) for item in days.split(",") if item.strip()}
    except ValueError:
        allowed = {1, 2, 3, 4, 5}
    if now_jkt.isoweekday() not in allowed:
        return False
    start = _parse_hhmm(policy.get("business_start") or "08:00")
    end = _parse_hhmm(policy.get("business_end") or "17:00")
    if not start or not end:
        return True
    now_minutes = now_jkt.hour * 60 + now_jkt.minute
    start_minutes = start[0] * 60 + start[1]
    end_minutes = end[0] * 60 + end[1]
    if start_minutes <= end_minutes:
        return start_minutes <= now_minutes < end_minutes
    return now_minutes >= start_minutes or now_minutes < end_minutes


def _step_window_open(policy, step, now_jkt):
    if step.get("business_hours_only") or policy.get("business_hours_enabled"):
        if not _business_open(policy, now_jkt):
            return False
    return True


def _load_device(device_id):
    try:
        return db.get_device(device_id)
    except Exception:
        return None


def _send_via_channel(channel_id, subject, text, problem, override=None, to_addr=None):
    channel = db.get_channel(channel_id)
    if channel is None:
        return False, "Kanal tidak ditemukan.", 0
    if not channel["enabled"]:
        return False, "Kanal dinonaktifkan.", 0
    config = dict(override) if override is not None else dict(db.channel_config(channel_id) or {})
    if to_addr and channel["type"] == "smtp":
        config["to_addr"] = to_addr
    import alerts

    problem_extra = (problem or {}).get("webhook_problem") if problem else None
    return alerts._send_via_channel(dict(channel), config, subject, text, problem=problem_extra)


def _first_enabled_channel(channel_type):
    for channel in db.list_channels():
        if channel["type"] == channel_type and channel["enabled"]:
            return channel
    return None


def _send_to_user(user_id, subject, text):
    row = db.find_user_by_id(user_id)
    if row is None:
        return False, "Pengguna tidak ditemukan."
    user = dict(row)
    email = (user.get("email") or "").strip()
    chat_id = (user.get("telegram_chat_id") or "").strip()
    sent = []
    if chat_id:
        telegram = _first_enabled_channel("telegram")
        if telegram:
            config = dict(db.channel_config(telegram["id"]) or {})
            config["chat_id"] = chat_id  # kirim ke kontak pribadi, bukan grup kanal
            ok, detail, _ = _send_via_channel(telegram["id"], subject, text, None, override=config)
            sent.append(("telegram", ok, detail))
    if email:
        smtp = _first_enabled_channel("smtp")
        if smtp:
            ok, detail, _ = _send_via_channel(smtp["id"], subject, text, None, to_addr=email)
            sent.append(("smtp", ok, detail))
    if not sent:
        return False, "Pengguna tidak punya kontak (email/telegram) atau tidak ada kanal aktif yang cocok."
    failed = [detail for _, ok, detail in sent if not ok]
    if failed:
        return False, failed[0]
    return True, "Terkirim ke kontak pengguna."


def _target_label(step):
    if step["target_type"] == "user":
        user = db.find_user_by_id(step["target_id"])
        return f"pengguna {user['username']}" if user else f"pengguna #{step['target_id']}"
    channel = db.get_channel(step["target_id"])
    return f"kanal {channel['name']}" if channel else f"kanal #{step['target_id']}"


def _problem_context(state):
    return {
        "id": state["problem_id"],
        "kind": state.get("problem_kind"),
        "severity": state.get("problem_severity"),
        "message": state.get("problem_message"),
        "device_id": state.get("problem_device_id"),
        "started_at": state.get("problem_started_at"),
        "webhook_problem": {
            "severity": state.get("problem_severity"),
            "kind": state.get("problem_kind"),
            "problem_id": state["problem_id"],
            "device_id": state.get("problem_device_id"),
        },
    }


def _send_step(state, device, step, *, recovery=False):
    target_type = step["target_type"]
    device_name = (device or {}).get("name") or f"#{state.get('problem_device_id')}"
    if recovery:
        subject = f"[NMS Kopegtel] PULIH - {device_name}"
        text = (
            f"PULIH — problem {state.get('problem_kind')} pada {device_name} telah pulih.\n"
            f"Pesan awal: {(state.get('problem_message') or '')[:300]}"
        )
    else:
        severity = (state.get("problem_severity") or "warning").upper()
        subject = f"[NMS Kopegtel] ESKALASI {severity} - {device_name}"
        text = (
            f"Eskalasi kebijakan {state.get('policy_name')} (langkah {step['step_order']}).\n"
            f"[{(state.get('problem_severity') or 'warning').upper()}] {state.get('problem_kind')}"
            f" - perangkat #{state.get('problem_device_id')}: {(state.get('problem_message') or '')[:300]}\n"
            "Problem masih terbuka dan belum diakui."
        )
    problem = _problem_context(state)
    problem["webhook_problem"]["device_name"] = device_name
    if target_type == "channel":
        ok, detail, attempts = _send_via_channel(step["target_id"], subject, text, problem)
    else:
        ok, detail = _send_to_user(step["target_id"], subject, text)
    return ok, detail


def _log(rule_id, channel_id, device_id, problem_id, status, detail):
    try:
        db.add_action_log(rule_id, channel_id, device_id, problem_id, status, str(detail)[:400])
    except Exception:
        log.exception("action_log gagal dicatat.")


def engine_tick(now_utc=None):
    """Jalankan satu iterasi mesin. Return ringkasan kecil untuk logging/uji."""
    current_utc = now_utc or datetime.now(timezone.utc)
    now_jkt = _now_jakarta(current_utc)
    sent = recovered = skipped = 0
    for state in db.pending_escalations():
        problem_id = state["problem_id"]
        device = _load_device(state["problem_device_id"])

        if state["problem_status"] == "resolved":
            steps = db.steps_for(state["policy_id"])
            anchor = max(
                (step for step in steps if step["step_order"] <= state["last_step"]),
                key=lambda step: step["step_order"], default=None,
            ) or (steps[0] if steps else None)
            if anchor:
                ok, detail = _send_step(state, device, anchor, recovery=True)
                _log(None, anchor["target_id"] if anchor["target_type"] == "channel" else None,
                     state.get("problem_device_id"), problem_id,
                     "sent" if ok else "failed", f"Pemulihan eskalasi: {detail}")
                if ok:
                    recovered += 1
                else:
                    skipped += 1
            db.mark_escalation_recovered(problem_id)
            continue

        if state.get("problem_acked_at"):
            db.stop_escalation(problem_id, "ack")
            continue

        if state.get("problem_suppressed"):
            continue  # ditahan oleh parent offline; evaluasi lagi saat parent pulih

        if device and db.device_in_maintenance(device):
            continue  # jendela pemeliharaan menahan pengiriman

        if not state.get("policy_enabled", 1):
            db.stop_escalation(problem_id, "kebijakan dinonaktifkan")
            continue

        steps = db.steps_for(state["policy_id"])
        if not steps:
            db.stop_escalation(problem_id, "tidak ada langkah")
            continue

        next_order = state["last_step"] + 1
        anchor = _parse_iso(state["last_sent_at"]) if state["last_sent_at"] else _parse_iso(
            state["problem_started_at"])
        if next_order <= len(steps):
            step = steps[next_order - 1]
            due = anchor + timedelta(minutes=int(step["delay_minutes"]))
            if current_utc < due:
                continue
            if not _step_window_open(_policy_from_state(state), step, now_jkt):
                continue  # tahan sampai jendela jam kerja terbuka
            ok, detail = _send_step(state, device, step)
            _record_send(state, step, ok, detail)
            sent += 1 if ok else 0
            skipped += 0 if ok else 1
            if ok:
                db.mark_escalation_step(problem_id, step["step_order"])
            continue

        repeat = int(state.get("policy_repeat_minutes") or 0)
        if repeat > 0 and state["last_sent_at"]:
            anchor = _parse_iso(state["last_sent_at"])
            if anchor and current_utc >= anchor + timedelta(minutes=repeat):
                step = steps[-1]
                if _step_window_open(_policy_from_state(state), step, now_jkt):
                    ok, detail = _send_step(state, device, step)
                    _record_send(state, step, ok, detail)
                    if ok:
                        db.mark_escalation_step(problem_id, step["step_order"])
                    sent += 1 if ok else 0
            continue
        db.stop_escalation(problem_id, "selesai")
    return {"sent": sent, "recovered": recovered, "skipped": skipped}


def _policy_from_state(state):
    return {
        "business_hours_enabled": bool(state.get("policy_business_hours_enabled")),
        "business_start": state.get("policy_business_start"),
        "business_end": state.get("policy_business_end"),
        "business_days": state.get("policy_business_days"),
    }


def _record_send(state, step, ok, detail):
    channel_id = step["target_id"] if step["target_type"] == "channel" else None
    label = _target_label(step)
    _log(
        None, channel_id, state.get("problem_device_id"), state["problem_id"],
        "sent" if ok else "failed",
        f"Eskalasi {state.get('policy_name')} langkah {step['step_order']} -> {label}: {detail}",
    )


def register_for_problem(problem_id, policy_id):
    """Daftarkan problem ke mesin eskalasi (idempoten)."""
    policy = db.get_policy(policy_id)
    if policy is None or not policy.get("enabled"):
        raise ValueError("Kebijakan eskalasi tidak ditemukan atau nonaktif.")
    db.register_escalation(problem_id, policy_id)


def job():
    try:
        result = engine_tick()
        if result.get("sent") or result.get("recovered"):
            log.info("Eskalasi: %s terkirim, %s pemulihan.", result["sent"], result["recovered"])
        return result
    except Exception:
        log.exception("Mesin eskalasi gagal pada tick ini.")
        return {"sent": 0, "recovered": 0, "skipped": 0}
