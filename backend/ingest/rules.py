"""Mesin aturan kejadian E1: pencocokan, agregasi, cooldown, auto-resolve.

Aturan regex dibatasi panjangnya dan menolak quantifier bersarang untuk
mencegah ReDoS. Pesan selalu dipotong sebelum dicocokkan/disimpan.
"""

import logging
import re
import threading
import time

import db

log = logging.getLogger("nms.ingest.rules")

MAX_PATTERN_LENGTH = 200
MAX_MATCH_MESSAGE = 2048
QUICK_POLL_COOLDOWN = 10

_quick_poll_seen = {}
_quick_poll_lock = threading.Lock()


class PatternError(ValueError):
    pass


def validate_regex_pattern(pattern):
    """Validasi regex aturan. Raise PatternError bila tidak aman/tidak valid."""
    text = str(pattern or "")
    if not text:
        raise PatternError("Pola regex wajib diisi.")
    if len(text) > MAX_PATTERN_LENGTH:
        raise PatternError(f"Pola regex maksimal {MAX_PATTERN_LENGTH} karakter.")
    # Deteksi quantifier bersarang: grup yang berisi quantifier lalu diikuti
    # quantifier lagi, mis. (a+)+, (?:ab*)*, ((a{1,3})+).
    stack = []
    escaped = False
    in_class = False
    for index, char in enumerate(text):
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == "[":
            in_class = True
            continue
        if char == "]":
            in_class = False
            continue
        if in_class:
            continue
        if char == "(":
            stack.append({"quant": False})
        elif char == ")":
            if not stack:
                raise PatternError("Kurung tutup tanpa kurung buka.")
            group = stack.pop()
            following = text[index + 1:index + 2]
            if group["quant"] and following in ("+", "*", "{"):
                raise PatternError("Quantifier bersarang tidak diizinkan (risiko ReDoS).")
            if stack and group["quant"]:
                stack[-1]["quant"] = True
        elif char in ("+", "*", "{"):
            if stack:
                stack[-1]["quant"] = True
    if stack:
        raise PatternError("Kurung buka tidak ditutup.")
    try:
        re.compile(text)
    except re.error as exc:
        raise PatternError(f"Regex tidak valid: {exc}") from exc
    return text


def _message_matches(rule, message):
    text = str(message or "")[:MAX_MATCH_MESSAGE]
    pattern = str(rule.get("pattern") or "")
    if not pattern:
        return True
    match_type = (rule.get("match_type") or "contains").lower()
    if match_type == "contains":
        return pattern.lower() in text.lower()
    if match_type == "startswith":
        return text.lower().startswith(pattern.lower())
    if match_type == "regex":
        try:
            return re.search(pattern, text, re.I) is not None
        except re.error:
            return False
    return False


def _scope_matches(rule, device):
    if rule.get("device_id") and int(rule["device_id"]) != int(device["id"]):
        return False
    if rule.get("group_id") and int(rule["group_id"]) != int(device.get("group_id") or 0):
        return False
    return True


def _severity_rank(name):
    return {"disaster": 5, "high": 4, "average": 3, "warning": 2, "information": 1}.get(
        str(name or "").lower(), 0
    )


def rule_matches(rule, device, message, severity_name, trap_oid=None):
    if not rule.get("enabled"):
        return False
    if (rule.get("kind") or "syslog") == "trap":
        prefix = str(rule.get("oid_prefix") or "")
        if not prefix or not str(trap_oid or "").startswith(prefix):
            return False
    else:
        if not _message_matches(rule, message):
            return False
    if _severity_rank(severity_name) < _severity_rank(rule.get("min_severity")):
        return False
    return _scope_matches(rule, device)


def _quick_poll(device_id, reason):
    now = time.monotonic()
    with _quick_poll_lock:
        last = _quick_poll_seen.get(device_id, 0)
        if now - last < QUICK_POLL_COOLDOWN:
            return False
        _quick_poll_seen[device_id] = now
    try:
        import poller

        poller.schedule_immediate_check(device_id, delay_seconds=2)
        log.info("Poll cepat perangkat %s dipicu oleh kejadian: %s", device_id, reason)
        return True
    except Exception:
        log.debug("Poll cepat tidak tersedia untuk perangkat %s.", device_id, exc_info=True)
        return False


def _dispatch(problem_id, device, severity, message, kind):
    problem = db.get_problem(problem_id)
    if not problem:
        return
    try:
        import alerts

        alerts.dispatch_problem(
            {
                "id": problem["id"],
                "device_id": device["id"],
                "kind": kind,
                "severity": severity,
                "message": message,
                "started_at": problem.get("started_at"),
                "suppressed_by_parent": problem.get("suppressed_by_parent"),
            }
        )
    except Exception:
        log.exception("Dispatch notifikasi kejadian untuk problem %s gagal.", problem_id)


def evaluate_syslog(device, parsed):
    """Evaluasi satu kejadian syslog terhadap aturan aktif. Return jumlah aksi."""
    from db.ingest import syslog_severity_name

    severity_name = syslog_severity_name(parsed.get("severity"))
    message = parsed.get("message") or ""
    in_maintenance = db.device_in_maintenance(device)
    actions = 0
    for rule in db.list_ingest_rules():
        if (rule.get("kind") or "syslog") != "syslog":
            continue
        # Pemulihan diperiksa lebih dulu dan hanya butuh scope yang sama;
        # pesan pemulihan tidak harus cocok dengan pola pemicu.
        recovery = str(rule.get("recovery_pattern") or "")
        if recovery and _scope_matches(rule, device):
            if _message_matches(
                {"pattern": recovery, "match_type": rule.get("match_type")}, message
            ):
                if db.resolve_ingest_problem(device["id"], rule["id"]):
                    actions += 1
                    _quick_poll(device["id"], "pola pemulihan")
                continue
        if not rule_matches(rule, device, message, severity_name):
            continue
        actions += 1
        if rule.get("resolves_rule_id"):
            db.resolve_rule_problems(rule["resolves_rule_id"])
            _quick_poll(device["id"], "recovery rule")
            continue
        if in_maintenance:
            continue
        result = db.record_ingest_problem(
            rule, device["id"], rule.get("problem_severity") or "warning", message
        )
        if result["should_notify"]:
            _dispatch(result["id"], device, rule.get("problem_severity") or "warning",
                      message, rule.get("problem_kind") or "syslog_rule")
        if _is_relevant(message):
            _quick_poll(device["id"], message[:80])
    return actions


def evaluate_trap(device, decoded):
    trap_oid = decoded.get("trap_oid") or ""
    trap_name = decoded.get("trap_name") or trap_oid
    in_maintenance = db.device_in_maintenance(device)
    actions = 0
    for rule in db.list_ingest_rules():
        if (rule.get("kind") or "syslog") != "trap":
            continue
        if not rule_matches(rule, device, trap_name, "information", trap_oid):
            continue
        actions += 1
        if rule.get("resolves_rule_id"):
            db.resolve_rule_problems(rule["resolves_rule_id"])
            _quick_poll(device["id"], trap_name)
            continue
        if in_maintenance:
            continue
        if_name = db.if_name_for_index(device["id"], decoded.get("if_index"))
        message = f"{trap_name} ({trap_oid})"
        if if_name:
            message += f" interface {if_name}"
        result = db.record_ingest_problem(
            rule, device["id"], rule.get("problem_severity") or "warning", message
        )
        if result["should_notify"]:
            _dispatch(result["id"], device, rule.get("problem_severity") or "warning",
                      message, rule.get("problem_kind") or "trap_rule")
        if trap_name in ("linkDown", "linkUp", "coldStart", "warmStart"):
            _quick_poll(device["id"], trap_name)
    return actions


def _is_relevant(message):
    text = str(message or "").lower()
    return any(word in text for word in ("reboot", "link down", "link up", "started", "shutdown"))


def run_housekeeping():
    """Dipanggil job tiap menit: auto-resolve + evaluasi ulang problem tertekan."""
    resolved = db.auto_resolve_ingest_problems()
    ready = db.reevaluate_suppressed_problems()
    for problem in ready:
        device = db.get_device(problem["device_id"])
        if device:
            _dispatch(problem["id"], device, problem["severity"], problem["message"], problem["kind"])
    return {"resolved": resolved, "released": len(ready)}
