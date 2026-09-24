"""nms.mikrotik — konstanta dan helper evaluasi MikroTik."""

from datetime import datetime

from nms.db import get_setting
from nms.fiber import _SEV_RANK
from nms.format import _fmt_age

SYSUP_OID = "1.3.6.1.2.1.1.3.0"
IF_OPER_OID = "1.3.6.1.2.1.2.2.1.8"
IF_HC_IN_OID = "1.3.6.1.2.1.31.1.1.1.6"
IF_HC_OUT_OID = "1.3.6.1.2.1.31.1.1.1.10"
REBOOT_ALARM_WINDOW_S = 900
_MT_TICK_MAX_S = 2**32 / 100.0
_MT_WRAP_NEAR_S = 30 * 86400
_MT_WRAP_FRESH_S = 86400
MT_STALE_MIN = 15


def _mt_reboot_kind(prev_up, sysup):
    """Klasifikasi penurunan sysUpTime: 'reboot' | 'wrap' | None."""
    try:
        prev = float(prev_up)
        cur = float(sysup)
    except (ValueError, TypeError):
        return None
    if cur >= prev:
        return None
    if prev > _MT_TICK_MAX_S - _MT_WRAP_NEAR_S and cur < _MT_WRAP_FRESH_S:
        return "wrap"
    return "reboot"


def _mt_stale_threshold():
    try:
        m = int(float(get_setting("mt_stale_min", MT_STALE_MIN)))
    except (ValueError, TypeError):
        m = MT_STALE_MIN
    return min(1440, max(3, m))


def _mt_stale_info(last_seen, now=None):
    """(is_stale, age_txt) bila device_health terakhir melewati ambang."""
    if not last_seen:
        return False, None
    try:
        seen = datetime.strptime(str(last_seen).strip(), "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return False, None
    now = now or datetime.now()
    age_min = (now - seen).total_seconds() / 60.0
    if age_min < 0:
        return False, None
    if age_min >= _mt_stale_threshold():
        return True, _fmt_age(age_min)
    return False, None


def _mt_evaluate(host, cpu, mem, sto, temp):
    """Status gabungan device. Kembalikan (status, severity, advice)."""
    try:
        temp_warn = get_setting("temp_threshold", 60.0)
        temp_crit = get_setting("temp_crit", 75.0)
        cpu_thresh = get_setting("cpu_threshold", 85.0)
        mem_thresh = get_setting("ram_threshold", 90.0)
        st_thresh = get_setting("disk_threshold", 90.0)
    except Exception:
        temp_warn, temp_crit, cpu_thresh, mem_thresh, st_thresh = (
            60.0,
            75.0,
            85.0,
            90.0,
            90.0,
        )
    notes = []
    sev = None

    def _worse(s):
        nonlocal sev
        if _SEV_RANK.get(s, 0) > _SEV_RANK.get(sev or "", 0):
            sev = s

    if temp is not None:
        if temp >= temp_crit:
            notes.append(f"suhu kritis {temp}°C")
            _worse("high")
        elif temp >= temp_warn:
            notes.append(f"suhu tinggi {temp}°C")
            _worse("warning")
    if cpu is not None and cpu > cpu_thresh:
        notes.append(f"CPU {cpu}%")
        _worse("warning")
    if mem is not None and mem > mem_thresh:
        notes.append(f"memory {mem}%")
        _worse("warning")
    if sto is not None and sto > st_thresh:
        notes.append(f"storage {sto}%")
        _worse("warning")
    if cpu is None and mem is None and sto is None and temp is None:
        return "unknown", None, "Belum ada data SNMP."
    if not notes:
        return "normal", None, "CPU/RAM/storage/suhu normal."
    status = "critical" if sev == "high" else "warning"
    return status, sev, ", ".join(notes) + " melebihi batas."


__all__ = [
    "SYSUP_OID",
    "IF_OPER_OID",
    "IF_HC_IN_OID",
    "IF_HC_OUT_OID",
    "REBOOT_ALARM_WINDOW_S",
    "MT_STALE_MIN",
    "_mt_reboot_kind",
    "_mt_stale_threshold",
    "_mt_stale_info",
    "_mt_evaluate",
]
