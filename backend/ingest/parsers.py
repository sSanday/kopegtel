"""Parser syslog RFC 3164, RFC 5424, dan format polos RouterOS.

Tidak ada data yang dieksekusi. Semua field dipotong sebelum disimpan.
"""

import re
from datetime import datetime, timedelta, timezone

MAX_MESSAGE_BYTES = 8192
_PRI_RE = re.compile(r"^<(\d{1,3})>")
_RFC5424_RE = re.compile(
    r"^<(?P<pri>\d{1,3})>(?P<version>\d{1,2}) "
    r"(?P<ts>\S+) (?P<host>\S+) (?P<app>\S+) (?P<proc>\S+) (?P<msgid>\S+) "
    r"(?P<sd>(?:\[.*?\])+|-) ?(?P<msg>.*)$",
    re.S,
)
_RFC3164_RE = re.compile(
    r"^<(?P<pri>\d{1,3})>(?P<ts>[A-Z][a-z]{2}\s+[0-9]{1,2} \d\d:\d\d:\d\d)"
    r" +(?P<host>\S+) (?:(?P<tag>[^:\s]{1,64})(?:\[\d+\])?: ?)?(?P<msg>.*)$",
    re.S,
)
_PLAIN_TAG_RE = re.compile(r"^(?P<tag>[A-Za-z][A-Za-z0-9_.-]{0,63}):\s+(?P<msg>.*)$", re.S)


def _now_iso(now=None):
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_timestamp(text, now=None):
    raw = (text or "").strip()
    current = now or datetime.now(timezone.utc)
    if not raw or raw == "-":
        return _now_iso(current)
    # RFC 5424: ISO 8601 (boleh dengan pecahan detik / offset).
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        pass
    # RFC 3164: "Nov 11 07:00:00" tanpa tahun. Pakai tahun berjalan; bila
    # hasilnya lebih dari sehari di masa depan, anggap tahun lalu.
    match = re.match(
        r"^([A-Za-z]{3})\s+(\d{1,2})\s+(\d{2}):(\d{2}):(\d{2})$", raw
    )
    if match:
        month_names = (
            "Jan", "Feb", "Mar", "Apr", "May", "Jun",
            "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
        )
        month_text, day, hour, minute, second = match.groups()
        try:
            month = month_names.index(month_text.capitalize()) + 1
            candidate = datetime(
                current.year, month, int(day), int(hour), int(minute), int(second),
                tzinfo=timezone.utc,
            )
        except ValueError:
            return _now_iso(current)
        if candidate > current + timedelta(days=1):
            candidate = candidate.replace(year=current.year - 1)
        return candidate.strftime("%Y-%m-%dT%H:%M:%SZ")
    return _now_iso(current)


def _decode(data):
    if isinstance(data, str):
        text = data
    else:
        text = bytes(data or b"").decode("utf-8", "replace")
    return text.replace("\x00", "").strip()


def parse_syslog(data, now=None):
    """Parse satu paket/baris syslog. Return dict atau None bila kosong."""
    text = _decode(data)
    if not text:
        return None
    raw = text[:MAX_MESSAGE_BYTES]
    facility = 1
    severity = 6
    hostname = None
    tag = None
    ts = _now_iso(now)
    message = text

    match = _RFC5424_RE.match(text)
    if match:
        pri = int(match.group("pri"))
        facility, severity = pri // 8, pri % 8
        ts = _parse_timestamp(match.group("ts"), now)
        hostname = match.group("host") if match.group("host") != "-" else None
        app = match.group("app")
        tag = app if app and app != "-" else None
        message = match.group("msg")
    else:
        match = _RFC3164_RE.match(text)
        if match:
            pri = int(match.group("pri"))
            facility, severity = pri // 8, pri % 8
            ts = _parse_timestamp(match.group("ts"), now)
            hostname = match.group("host") if match.group("host") != "-" else None
            tag = (match.group("tag") or "").strip() or None
            message = match.group("msg")
        else:
            pri_match = _PRI_RE.match(text)
            if pri_match:
                pri = int(pri_match.group(1))
                if pri <= 191:
                    facility, severity = pri // 8, pri % 8
                text = text[pri_match.end():].lstrip()
                message = text
            plain = _PLAIN_TAG_RE.match(message)
            if plain:
                tag = plain.group("tag")
                message = plain.group("msg")

    message = (message or "").strip()[:MAX_MESSAGE_BYTES]
    if not message:
        return None
    return {
        "ts": ts,
        "hostname": (hostname or "")[:255] or None,
        "facility": facility,
        "severity": severity,
        "tag": (tag or "")[:255] or None,
        "message": message,
        "raw": raw,
    }
