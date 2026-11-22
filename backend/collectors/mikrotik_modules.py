"""Koleksi modul MikroTik (M1): kapabilitas, layer, log, audit keamanan.

Semua perintah di sini READ-ONLY kecuali fungsi ``perform_action`` yang
hanya dipanggil dari endpoint aksi tulis ber-allowlist (M5). Satu koneksi
API dipakai bersama oleh ``collectors.mikrotik.check_mikrotik``; modul ini
tidak membuka koneksi sendiri untuk pembacaan.

Prinsip:
- Path yang berbeda antar versi/paket dicoba berurutan. Path yang tidak ada
  atau ditolak izin disimpan sebagai kapabilitas ``supported: false`` dengan
  ``retry_after`` 6 jam, bukan error berulang.
- Kegagalan satu modul tidak menggagalkan modul lain.
- Tabel dibatasi ``MAX_ROWS`` baris dan diberi penanda ``truncated``.
- Tabel koneksi firewall hanya diambil jumlahnya (count-only), tidak pernah
  isi tabel penuh.
- Field password/secret router tidak pernah diminta (.proplist).
"""

import hashlib
import json
import re
import ssl
from datetime import datetime, timedelta, timezone

from librouteros import connect
from librouteros.exceptions import LibRouterosError, MultiTrapError, TrapError

from . import tls as tls_util

MAX_ROWS = 5000
RETRY_SECONDS = 6 * 60 * 60
DEFAULT_TIMEOUT = 5

# ---------------------------------------------------------
# Spesifikasi probe. Urutan kandidat = urutan percobaan.
# ---------------------------------------------------------

PROBE_SPECS = {
    "addresses": [
        {"path": "/ip/address/print", "fields": ".id,address,network,interface,disabled,dynamic,comment"},
    ],
    "ppp": [
        {"path": "/ppp/active/print",
         "fields": ".id,name,service,caller-id,address,uptime,bytes-in,bytes-out,encoding,session-id"},
    ],
    "hotspot": [
        {"path": "/ip/hotspot/active/print",
         "fields": ".id,user,address,mac-address,uptime,bytes-in,bytes-out,server,radius"},
    ],
    "wireless": [
        {"variant": "wireless", "path": "/interface/wireless/registration-table/print",
         "fields": ".id,interface,mac-address,signal-strength,tx-rate,rx-rate,uptime,bytes-in,bytes-out,packets-in,packets-out"},
        {"variant": "wifi", "path": "/interface/wifi/registration-table/print",
         "fields": ".id,interface,mac-address,signal,tx-rate,rx-rate,uptime,bytes-in,bytes-out,packets-in,packets-out"},
        {"variant": "wifiwave2", "path": "/interface/wifiwave2/registration-table/print",
         "fields": ".id,interface,mac-address,signal,tx-rate,rx-rate,uptime,bytes-in,bytes-out,packets-in,packets-out"},
        {"variant": "caps-man", "path": "/caps-man/registration-table/print",
         "fields": ".id,interface,mac-address,signal-strength,tx-rate,rx-rate,uptime,bytes-in,bytes-out"},
    ],
    "dhcp": [
        {"path": "/ip/dhcp-server/lease/print",
         "fields": ".id,address,mac-address,host-name,status,server,comment,expires-after,last-seen,disabled,dynamic,radius"},
    ],
    "dhcp_pools": [
        {"path": "/ip/pool/print", "fields": ".id,name,ranges"},
    ],
    "dhcp_servers": [
        {"path": "/ip/dhcp-server/print", "fields": ".id,name,address-pool,interface,disabled"},
    ],
    "arp": [
        {"path": "/ip/arp/print",
         "fields": ".id,address,mac-address,interface,complete,dynamic,disabled,invalid"},
    ],
    "neighbors": [
        {"path": "/ip/neighbor/print",
         "fields": ".id,identity,platform,version,interface,address,mac-address,address4,address6,board"},
    ],
    "routes": [
        {"path": "/ip/route/print",
         "fields": ".id,dst-address,gateway,distance,active,dynamic,static,connect,disabled,scope,target-scope,comment"},
    ],
    "routes6": [
        {"path": "/ipv6/route/print",
         "fields": ".id,dst-address,gateway,distance,active,dynamic,disabled,comment"},
    ],
    "bgp": [
        {"variant": "session", "path": "/routing/bgp/session/print",
         "fields": ".id,name,remote.address,remote.as,state,uptime,prefix-count,disabled"},
        {"variant": "peer", "path": "/routing/bgp/peer/print",
         "fields": ".id,name,remote-address,remote-as,state,uptime,prefix-count,disabled"},
    ],
    "ospf": [
        {"path": "/routing/ospf/neighbor/print",
         "fields": ".id,router-id,address,state,interface,priority,dead-time"},
    ],
    "firewall_filter": [
        {"path": "/ip/firewall/filter/print",
         "fields": ".id,chain,action,comment,packets,bytes,disabled"},
    ],
    "firewall_nat": [
        {"path": "/ip/firewall/nat/print",
         "fields": ".id,chain,action,comment,packets,bytes,disabled,to-addresses,to-ports"},
    ],
    "firewall_mangle": [
        {"path": "/ip/firewall/mangle/print",
         "fields": ".id,chain,action,comment,packets,bytes,disabled"},
    ],
    "connections": [
        {"variant": "count", "path": "/ip/firewall/connection/print", "count_only": True},
    ],
    "address_lists": [
        {"path": "/ip/firewall/address-list/print",
         "fields": ".id,list,address,timeout,comment,disabled,creation-time,dynamic"},
    ],
    "queue_simple": [
        {"path": "/queue/simple/print",
         "fields": ".id,name,target,max-limit,rate,packet-rate,bytes,packets,dropped,disabled,comment"},
    ],
    "queue_tree": [
        {"path": "/queue/tree/print",
         "fields": ".id,name,parent,max-limit,rate,bytes,packets,dropped,disabled,comment"},
    ],
    "services": [
        {"path": "/ip/service/print", "fields": ".id,name,port,address,disabled,invalid"},
    ],
    "users": [
        {"path": "/user/print", "fields": ".id,name,group,address,last-logged-in,disabled,comment"},
    ],
    "dns": [
        {"path": "/ip/dns/print", "fields": ".id,allow-remote-requests,servers,dynamic-servers"},
    ],
    "ntp": [
        {"path": "/system/ntp/client/print", "fields": ".id,enabled,mode,servers"},
    ],
    "packages": [
        {"path": "/system/package/print", "fields": ".id,name,version,disabled,size"},
    ],
    "routerboard": [
        {"path": "/system/routerboard/print",
         "fields": ".id,model,serial-number,current-firmware,upgrade-firmware,factory-firmware,routerboard"},
    ],
    "license": [
        {"path": "/system/license/print", "fields": ".id,nlevel,level,expires-at,features"},
    ],
    "clock": [
        {"path": "/system/clock/print", "fields": ".id,time,date,time-zone-name,gmt-offset,time-zone-autodetect"},
    ],
    "logs": [
        {"path": "/log/print", "fields": ".id,time,topics,message"},
    ],
}

# Modul yang dapat dimatikan per perangkat (sesuai permintaan M1).
MODULE_KEYS = {
    "ppp": "ppp",
    "hotspot": "hotspot",
    "wireless": "wireless",
    "dhcp": "dhcp",
    "queue": "queue",
    "routing": "routing",
    "log": "log",
    "security": "security",
}

# Layer → kunci probe. ``security`` bukan probe print; dihitung dari data lain.
LAYER_KEYS = {
    "fast": ["addresses", "clock", "ppp", "hotspot", "wireless", "logs"],
    "medium": [
        "dhcp", "dhcp_pools", "dhcp_servers", "arp", "neighbors", "routes", "routes6",
        "bgp", "ospf", "firewall_filter", "firewall_nat", "firewall_mangle",
        "connections", "address_lists", "queue_simple", "queue_tree",
    ],
    "slow": ["services", "users", "dns", "ntp", "packages", "routerboard", "license"],
}

MODULE_TO_PROBES = {
    "ppp": ["ppp"],
    "hotspot": ["hotspot"],
    "wireless": ["wireless"],
    "dhcp": ["dhcp", "dhcp_pools", "dhcp_servers", "arp"],
    "queue": ["queue_simple", "queue_tree"],
    "routing": ["routes", "routes6", "bgp", "ospf", "neighbors"],
    "log": ["logs"],
    "security": [],
}

# ---------------------------------------------------------
# Utility
# ---------------------------------------------------------


def _now():
    return datetime.now(timezone.utc)


def _iso(dt=None):
    return (dt or _now()).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_ts(text):
    if not text:
        return None
    try:
        value = str(text).replace("Z", "+00:00")
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _text(value):
    if value is None:
        return ""
    return str(value).strip()


def _int(value):
    try:
        text = _text(value)
        if not text:
            return None
        return int(float(text))
    except (TypeError, ValueError):
        return None


def _float(value):
    try:
        text = _text(value)
        if not text:
            return None
        return float(text)
    except (TypeError, ValueError):
        return None


def _bool(value):
    return _text(value).lower() in ("true", "yes", "1", "on")


def _snake(value):
    return re.sub(r"[^a-z0-9]+", "_", _text(value).lower()).strip("_")


def _row_key(prefix, *parts):
    raw = "|".join(_text(part) for part in parts)
    if not raw.strip("|"):
        raw = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)
    digest = hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()[:24]
    return f"{prefix}:{digest}"


def _safe_uptime(value):
    """RouterOS uptime → detik (format sama dengan collector dasar)."""
    s = _text(value)
    if not s:
        return None
    total = 0
    matched = False
    for pattern, factor in ((r"(\d+)w", 7 * 86400), (r"(\d+)d", 86400)):
        found = re.search(pattern, s)
        if found:
            total += int(found.group(1)) * factor
            matched = True
    clock = re.search(r"(\d+):(\d{1,2}):(\d{1,2})", s)
    if clock:
        total += int(clock.group(1)) * 3600 + int(clock.group(2)) * 60 + int(clock.group(3))
        matched = True
    else:
        short = re.fullmatch(r"(\d{1,2}):(\d{1,2})", s)
        if short:
            total += int(short.group(1)) * 60 + int(short.group(2))
            matched = True
    return total if matched else None


# ---------------------------------------------------------
# Trap / error classification
# ---------------------------------------------------------

_UNSUPPORTED_WORDS = (
    "no such command", "bad command", "unknown command", "expected command",
    "no such item", "invalid command", "not found", "no such argument",
)
_PERMISSION_WORDS = (
    "not allowed", "permission", "not enough permissions", "access denied",
    "not permitted",
)
_AUTH_WORDS = ("invalid user name or password", "login", "authentication", "password")


def classify_error(exc):
    """Klasifikasikan exception RouterOS untuk status kapabilitas."""
    if isinstance(exc, tls_util.TlsVerificationError):
        return exc.code
    if isinstance(exc, ssl.SSLError):
        return "tls_error"
    message = str(exc or "").lower()
    if isinstance(exc, TimeoutError) or "timed out" in message or "timeout" in message:
        return "timeout"
    if any(word in message for word in _PERMISSION_WORDS):
        return "no_permission"
    if any(word in message for word in _UNSUPPORTED_WORDS):
        return "unsupported"
    if any(word in message for word in _AUTH_WORDS):
        return "auth_failed"
    return "error"


# ---------------------------------------------------------
# Parser log & login failure
# ---------------------------------------------------------

_LOGIN_PATTERNS = (
    re.compile(
        r"login\s+(?:failure|failed)\s+for\s+user\s+(?P<user>\S+)\s+from\s+"
        r"(?P<ip>[0-9a-fA-F:.]+)(?:\s+via\s+(?P<service>\S+))?",
        re.IGNORECASE,
    ),
    re.compile(
        r"user\s+(?P<user>\S+)\s+(?:authentication\s+)?failed\s+from\s+"
        r"(?P<ip>[0-9a-fA-F:.]+)(?:\s+via\s+(?P<service>\S+))?",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:login\s+)?failure\s+from\s+(?P<ip>[0-9a-fA-F:.]+)(?:\s+via\s+(?P<service>\S+))?",
        re.IGNORECASE,
    ),
)


def parse_login_failure(message):
    """Toleran terhadap variasi RouterOS. Return dict atau None."""
    text = _text(message)
    if not text:
        return None
    for pattern in _LOGIN_PATTERNS:
        match = pattern.search(text)
        if match:
            groups = match.groupdict()
            return {
                "username": (groups.get("user") or "").strip() or None,
                "source_ip": (groups.get("ip") or "").strip() or None,
                "service": (groups.get("service") or "").strip().rstrip(",") or None,
            }
    return None


def classify_log(row):
    """Normalisasi satu baris /log/print. Hanya entri penting yang disimpan."""
    topics = _text(row.get("topics"))
    message = _text(row.get("message"))
    lowered_topics = topics.lower()
    severity = "info"
    if "critical" in lowered_topics:
        severity = "critical"
    elif "error" in lowered_topics:
        severity = "error"
    elif "warning" in lowered_topics:
        severity = "warning"
    elif re.search(r"\b(critical|fatal)\b", message, re.IGNORECASE):
        severity = "critical"
    elif re.search(r"\berror\b", message, re.IGNORECASE):
        severity = "error"
    elif re.search(r"\bwarn(?:ing)?\b", message, re.IGNORECASE):
        severity = "warning"

    failure = parse_login_failure(message)
    relevant = bool(failure) or severity in ("critical", "error", "warning")
    if not relevant:
        return None

    return {
        "router_id": _text(row.get(".id")) or None,
        "log_time": _text(row.get("time")) or None,
        "topics": topics or None,
        "message": message or None,
        "severity": severity,
        "is_login_failure": 1 if failure else 0,
        "username": (failure or {}).get("username"),
        "source_ip": (failure or {}).get("source_ip"),
        "service": (failure or {}).get("service"),
        "row_key": _row_key("log", row.get(".id"), row.get("time"), topics, message),
    }


def logs_after_cursor(rows, cursor):
    """Ambil baris setelah kursor. Bila kursor tidak ditemukan (log berputar),
    proses semua baris — jangan sampai log baru hilang."""
    cursor = cursor or {}
    cursor_id = _text(cursor.get("id"))
    cursor_time = _text(cursor.get("time"))
    if not cursor_id and not cursor_time:
        return rows
    for index, row in enumerate(rows):
        if cursor_id and _text(row.get(".id")) == cursor_id:
            return rows[index + 1:]
        if not cursor_id and cursor_time and _text(row.get("time")) == cursor_time:
            return rows[index + 1:]
    return rows


def next_log_cursor(previous, rows):
    if not rows:
        return previous or {}
    last = rows[-1]
    return {"id": _text(last.get(".id")) or None, "time": _text(last.get("time")) or None}


# ---------------------------------------------------------
# Normalisasi baris
# ---------------------------------------------------------

def _normalize_interface(row):
    return {
        "name": _text(row.get("name")),
        "type": _text(row.get("type")),
        "mtu": _int(row.get("mtu")),
        "mac": _text(row.get("mac-address")) or None,
        "running": _bool(row.get("running")),
        "disabled": _bool(row.get("disabled")),
        "rx_byte": _int(row.get("rx-byte")),
        "tx_byte": _int(row.get("tx-byte")),
        "rx_error": _int(row.get("rx-error")),
        "tx_error": _int(row.get("tx-error")),
        "rx_drop": _int(row.get("rx-drop")),
        "tx_drop": _int(row.get("tx-drop")),
        "comment": _text(row.get("comment")) or None,
        "router_id": _text(row.get(".id")) or None,
        "if_index": _int(row.get("if-index")),
    }


def _normalize_address(row):
    return {
        "address": _text(row.get("address")),
        "network": _text(row.get("network")) or None,
        "interface": _text(row.get("interface")) or None,
        "disabled": _bool(row.get("disabled")),
        "dynamic": _bool(row.get("dynamic")),
        "comment": _text(row.get("comment")) or None,
        "router_id": _text(row.get(".id")) or None,
    }


def _normalize_ppp(row):
    name = _text(row.get("name"))
    caller = _text(row.get("caller-id"))
    address = _text(row.get("address"))
    router_id = _text(row.get(".id"))
    return {
        "row_key": _row_key("ppp", router_id, name, caller, address),
        "name": name or None,
        "service": _text(row.get("service")) or None,
        "caller_id": caller or None,
        "address": address or None,
        "uptime": _text(row.get("uptime")) or None,
        "bytes_in": _int(row.get("bytes-in")),
        "bytes_out": _int(row.get("bytes-out")),
        "encoding": _text(row.get("encoding")) or None,
        "session_id": _text(row.get("session-id")) or None,
        "router_id": router_id or None,
    }


def _normalize_hotspot(row):
    user = _text(row.get("user"))
    mac = _text(row.get("mac-address"))
    address = _text(row.get("address"))
    router_id = _text(row.get(".id"))
    return {
        "row_key": _row_key("hotspot", router_id, user, mac, address),
        "user": user or None,
        "address": address or None,
        "mac": mac or None,
        "uptime": _text(row.get("uptime")) or None,
        "bytes_in": _int(row.get("bytes-in")),
        "bytes_out": _int(row.get("bytes-out")),
        "server": _text(row.get("server")) or None,
        "router_id": router_id or None,
    }


def _normalize_wireless(row, variant):
    mac = _text(row.get("mac-address"))
    interface = _text(row.get("interface"))
    router_id = _text(row.get(".id"))
    signal = _float(row.get("signal"))
    strength = _text(row.get("signal-strength"))
    return {
        "row_key": _row_key("wireless", router_id, mac, interface),
        "interface": interface or None,
        "mac": mac or None,
        "signal_dbm": signal,
        "signal_strength": strength or None,
        "tx_rate": _text(row.get("tx-rate")) or None,
        "rx_rate": _text(row.get("rx-rate")) or None,
        "uptime": _text(row.get("uptime")) or None,
        "bytes_in": _int(row.get("bytes-in")),
        "bytes_out": _int(row.get("bytes-out")),
        "variant": variant,
        "router_id": router_id or None,
    }


def _normalize_lease(row):
    address = _text(row.get("address"))
    mac = _text(row.get("mac-address"))
    server = _text(row.get("server"))
    router_id = _text(row.get(".id"))
    return {
        "row_key": _row_key("lease", router_id, address, mac, server),
        "address": address or None,
        "mac": mac or None,
        "host_name": _text(row.get("host-name")) or None,
        "status": _text(row.get("status")) or None,
        "server": server or None,
        "comment": _text(row.get("comment")) or None,
        "expires_after": _text(row.get("expires-after")) or None,
        "last_seen": _text(row.get("last-seen")) or None,
        "dynamic": _bool(row.get("dynamic")),
        "disabled": _bool(row.get("disabled")),
        "radius": _bool(row.get("radius")),
        "router_id": router_id or None,
    }


def _normalize_arp(row):
    address = _text(row.get("address"))
    interface = _text(row.get("interface"))
    router_id = _text(row.get(".id"))
    return {
        "row_key": _row_key("arp", router_id, address, interface),
        "address": address or None,
        "mac": _text(row.get("mac-address")) or None,
        "interface": interface or None,
        "complete": _bool(row.get("complete")),
        "dynamic": _bool(row.get("dynamic")),
        "disabled": _bool(row.get("disabled")),
        "invalid": _bool(row.get("invalid")),
        "router_id": router_id or None,
    }


def _normalize_neighbor(row):
    mac = _text(row.get("mac-address"))
    address = _text(row.get("address")) or _text(row.get("address4")) or _text(row.get("address6"))
    interface = _text(row.get("interface"))
    router_id = _text(row.get(".id"))
    return {
        "row_key": _row_key("neighbor", router_id, mac, address, interface),
        "identity": _text(row.get("identity")) or None,
        "platform": _text(row.get("platform")) or None,
        "version": _text(row.get("version")) or None,
        "interface": interface or None,
        "address": address or None,
        "address4": _text(row.get("address4")) or None,
        "address6": _text(row.get("address6")) or None,
        "mac": mac or None,
        "board": _text(row.get("board")) or None,
        "router_id": router_id or None,
    }


def _normalize_route(row, family):
    dst = _text(row.get("dst-address"))
    gateway = _text(row.get("gateway"))
    distance = _int(row.get("distance"))
    router_id = _text(row.get(".id"))
    return {
        "row_key": _row_key(f"route-{family}", router_id, dst, gateway, distance),
        "family": family,
        "dst_address": dst or None,
        "gateway": gateway or None,
        "distance": distance,
        "active": _bool(row.get("active")),
        "dynamic": _bool(row.get("dynamic")),
        "static": _bool(row.get("static")),
        "connect": _bool(row.get("connect")),
        "disabled": _bool(row.get("disabled")),
        "scope": _text(row.get("scope")) or None,
        "target_scope": _text(row.get("target-scope")) or None,
        "comment": _text(row.get("comment")) or None,
        "router_id": router_id or None,
    }


def _normalize_bgp(row, variant):
    name = _text(row.get("name"))
    remote_address = _text(row.get("remote.address")) or _text(row.get("remote-address"))
    remote_as = _text(row.get("remote.as")) or _text(row.get("remote-as"))
    state = _text(row.get("state"))
    router_id = _text(row.get(".id"))
    return {
        "row_key": _row_key("bgp", router_id, name, remote_address),
        "protocol": "bgp",
        "variant": variant,
        "name": name or None,
        "remote_address": remote_address or None,
        "remote_as": remote_as or None,
        "state": state or None,
        "uptime": _text(row.get("uptime")) or None,
        "prefix_count": _int(row.get("prefix-count")),
        "disabled": _bool(row.get("disabled")),
        "router_id": router_id or None,
    }


def _normalize_ospf(row):
    peer_id = _text(row.get("router-id"))
    address = _text(row.get("address"))
    state = _text(row.get("state"))
    interface = _text(row.get("interface"))
    router_id = _text(row.get(".id"))
    return {
        "row_key": _row_key("ospf", router_id, peer_id, address, interface),
        "protocol": "ospf",
        "peer_id": peer_id or None,
        "address": address or None,
        "state": state or None,
        "interface": interface or None,
        "priority": _int(row.get("priority")),
        "dead_time": _text(row.get("dead-time")) or None,
        "disabled": False,
        "router_id": router_id or None,
    }


def _normalize_firewall(row, kind):
    router_id = _text(row.get(".id"))
    return {
        "row_key": _row_key(f"rule-{kind}", router_id),
        "kind": kind,
        "chain": _text(row.get("chain")) or None,
        "action": _text(row.get("action")) or None,
        "comment": _text(row.get("comment")) or None,
        "packets": _int(row.get("packets")),
        "bytes": _int(row.get("bytes")),
        "disabled": _bool(row.get("disabled")),
        "to_addresses": _text(row.get("to-addresses")) or None,
        "to_ports": _text(row.get("to-ports")) or None,
        "router_id": router_id or None,
    }


def _normalize_address_list(row):
    address = _text(row.get("address"))
    list_name = _text(row.get("list"))
    comment = _text(row.get("comment"))
    router_id = _text(row.get(".id"))
    return {
        "row_key": _row_key("addrlist", router_id, list_name, address, comment),
        "list": list_name or None,
        "address": address or None,
        "timeout": _text(row.get("timeout")) or None,
        "comment": comment or None,
        "disabled": _bool(row.get("disabled")),
        "creation_time": _text(row.get("creation-time")) or None,
        "dynamic": _bool(row.get("dynamic")),
        "router_id": router_id or None,
    }


def _normalize_queue(row, kind):
    name = _text(row.get("name"))
    router_id = _text(row.get(".id"))
    return {
        "row_key": _row_key(f"queue-{kind}", router_id, name),
        "kind": kind,
        "name": name or None,
        "target": _text(row.get("target")) or None,
        "parent": _text(row.get("parent")) or None,
        "max_limit": _text(row.get("max-limit")) or None,
        "rate": _text(row.get("rate")) or None,
        "packet_rate": _text(row.get("packet-rate")) or None,
        "bytes": _int(row.get("bytes")),
        "packets": _int(row.get("packets")),
        "dropped": _int(row.get("dropped")),
        "disabled": _bool(row.get("disabled")),
        "comment": _text(row.get("comment")) or None,
        "router_id": router_id or None,
    }


def _normalize_service(row):
    name = _text(row.get("name"))
    router_id = _text(row.get(".id"))
    return {
        "row_key": _row_key("service", router_id, name),
        "name": name or None,
        "port": _int(row.get("port")),
        "address": _text(row.get("address")) or None,
        "disabled": _bool(row.get("disabled")),
        "invalid": _bool(row.get("invalid")),
        "router_id": router_id or None,
    }


def _normalize_user(row):
    name = _text(row.get("name"))
    router_id = _text(row.get(".id"))
    return {
        "row_key": _row_key("user", router_id, name),
        "name": name or None,
        "group": _text(row.get("group")) or None,
        "address": _text(row.get("address")) or None,
        "last_logged_in": _text(row.get("last-logged-in")) or None,
        "disabled": _bool(row.get("disabled")),
        "comment": _text(row.get("comment")) or None,
        "router_id": router_id or None,
    }


def _normalize_package(row):
    return {
        "name": _text(row.get("name")),
        "version": _text(row.get("version")) or None,
        "disabled": _bool(row.get("disabled")),
    }


def _normalize_dns(row):
    return {
        "router_id": _text(row.get(".id")) or None,
        "allow_remote_requests": _bool(row.get("allow-remote-requests")),
        "servers": _text(row.get("servers")) or None,
        "dynamic_servers": _text(row.get("dynamic-servers")) or None,
    }


def _normalize_ntp(row):
    return {
        "router_id": _text(row.get(".id")) or None,
        "enabled": _bool(row.get("enabled")),
        "mode": _text(row.get("mode")) or None,
        "servers": _text(row.get("servers")) or None,
    }


def _normalize_routerboard(row):
    return {
        "router_id": _text(row.get(".id")) or None,
        "model": _text(row.get("model")) or None,
        "serial_number": _text(row.get("serial-number")) or None,
        "current_firmware": _text(row.get("current-firmware")) or None,
        "upgrade_firmware": _text(row.get("upgrade-firmware")) or None,
        "factory_firmware": _text(row.get("factory-firmware")) or None,
        "routerboard": _bool(row.get("routerboard")),
    }


def _normalize_license(row):
    return {
        "router_id": _text(row.get(".id")) or None,
        "level": _text(row.get("level")) or _text(row.get("nlevel")) or None,
        "expires_at": _text(row.get("expires-at")) or None,
        "features": _text(row.get("features")) or None,
    }


def _normalize_clock(row):
    return {
        "router_id": _text(row.get(".id")) or None,
        "time": _text(row.get("time")) or None,
        "date": _text(row.get("date")) or None,
        "time_zone_name": _text(row.get("time-zone-name")) or None,
        "gmt_offset": _text(row.get("gmt-offset")) or None,
    }


def _parse_gmt_offset(value):
    text = _text(value)
    if not text:
        return None
    match = re.fullmatch(r"([+-]?)(\d{1,2}):(\d{2})", text)
    if not match:
        return None
    sign = -1 if match.group(1) == "-" else 1
    return sign * (int(match.group(2)) * 3600 + int(match.group(3)) * 60)


def clock_utc(clock_row):
    """Waktu router (date+time+gmt-offset) → datetime UTC, atau None."""
    if not clock_row:
        return None
    date_text = _text(clock_row.get("date"))
    time_text = _text(clock_row.get("time"))
    if not date_text or not time_text:
        return None
    offset = _parse_gmt_offset(clock_row.get("gmt-offset") or clock_row.get("gmt_offset"))
    if offset is None:
        return None
    try:
        local = datetime.strptime(f"{date_text} {time_text}", "%Y-%m-%d %H:%M:%S")
        local = local.replace(tzinfo=timezone(timedelta(seconds=offset)))
        return local.astimezone(timezone.utc)
    except ValueError:
        return None


# ---------------------------------------------------------
# Kapabilitas & session koleksi
# ---------------------------------------------------------

class ModuleSession:
    """Satu sesi API, cache per probe, dan pembaruan kapabilitas."""

    def __init__(self, api, capabilities, *, max_rows=MAX_ROWS, now=None):
        self.api = api
        self.capabilities = dict(capabilities or {})
        self.probes = dict(self.capabilities.get("probes") or {})
        self.max_rows = max_rows
        self.now = now or _now()
        self.cache = {}
        self.errors = []

    def _spec(self, key, path):
        for spec in PROBE_SPECS.get(key, []):
            if spec["path"] == path:
                return spec
        return None

    def _fetch(self, spec):
        path = spec["path"]
        fields = spec.get("fields")
        count_only = bool(spec.get("count_only"))
        try:
            if count_only:
                rows = list(self.api.rawCmd(path, "=count-only="))
                value = None
                for row in rows:
                    value = row.get("ret") if isinstance(row, dict) else None
                    if value is None and isinstance(row, dict):
                        value = row.get("value")
                return {"ok": True, "rows": [], "count": _int(value), "truncated": False}
            words = []
            if fields:
                words.append(f"=.proplist={fields}")
            rows = list(self.api.rawCmd(path, *words))
            truncated = len(rows) > self.max_rows
            if truncated:
                rows = rows[: self.max_rows]
            return {"ok": True, "rows": rows, "count": len(rows), "truncated": truncated}
        except TrapError as exc:
            message = str(exc).lower()
            if not count_only and fields and (
                "unknown parameter" in message or "no such argument" in message
            ):
                try:
                    rows = list(self.api.rawCmd(path))
                    truncated = len(rows) > self.max_rows
                    if truncated:
                        rows = rows[: self.max_rows]
                    return {"ok": True, "rows": rows, "count": len(rows), "truncated": truncated}
                except Exception as retry_exc:  # noqa: BLE001
                    return {"ok": False, "reason": classify_error(retry_exc), "detail": str(retry_exc)}
            return {"ok": False, "reason": classify_error(exc), "detail": str(exc)}
        except MultiTrapError as exc:
            return {"ok": False, "reason": classify_error(exc), "detail": str(exc)}
        except LibRouterosError as exc:
            return {"ok": False, "reason": classify_error(exc), "detail": str(exc)}
        except TimeoutError as exc:
            return {"ok": False, "reason": "timeout", "detail": str(exc) or "Timeout RouterOS."}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "reason": classify_error(exc), "detail": str(exc)}

    def query(self, key, *, force=False):
        """Ambil data probe; probe ulang path yang belum didukung setelah backoff."""
        if not force and key in self.cache:
            return self.cache[key]

        cap = self.probes.get(key) or {}
        if cap.get("supported") and cap.get("path"):
            spec = self._spec(key, cap["path"])
            if spec is None:
                cap = {}
            else:
                result = self._fetch(spec)
                if result["ok"]:
                    payload = {
                        "ok": True,
                        "rows": result["rows"],
                        "count": result["count"],
                        "truncated": result["truncated"],
                        "path": spec["path"],
                        "variant": spec.get("variant"),
                    }
                    self.cache[key] = payload
                    return payload
                reason = result.get("reason") or "error"
                if reason in ("unsupported", "no_permission", "auth_failed"):
                    self._mark_unsupported(key, reason, result.get("detail"))
                payload = {
                    "ok": False,
                    "reason": reason,
                    "detail": result.get("detail"),
                    "path": spec["path"],
                    "variant": spec.get("variant"),
                }
                self.cache[key] = payload
                return payload

        retry_after = _parse_ts(cap.get("retry_after"))
        if not force and retry_after and self.now < retry_after:
            payload = {
                "ok": False,
                "reason": cap.get("reason") or "unsupported",
                "detail": cap.get("detail"),
                "cached": True,
            }
            self.cache[key] = payload
            return payload

        errors = []
        for spec in PROBE_SPECS.get(key, []):
            result = self._fetch(spec)
            if result["ok"]:
                self.probes[key] = {
                    "supported": True,
                    "path": spec["path"],
                    "variant": spec.get("variant"),
                    "reason": None,
                    "detail": None,
                    "checked_at": _iso(self.now),
                    "retry_after": None,
                }
                payload = {
                    "ok": True,
                    "rows": result["rows"],
                    "count": result["count"],
                    "truncated": result["truncated"],
                    "path": spec["path"],
                    "variant": spec.get("variant"),
                }
                self.cache[key] = payload
                return payload
            reason = result.get("reason") or "error"
            errors.append({"path": spec["path"], "reason": reason, "detail": result.get("detail")})
            if reason in ("timeout", "error", "auth_failed", "tls_error", "tls_fingerprint_mismatch"):
                break

        reason = "unsupported"
        priority = {
            "no_permission": 4,
            "timeout": 3,
            "tls_fingerprint_mismatch": 3,
            "tls_error": 3,
            "auth_failed": 2,
            "error": 2,
            "unsupported": 1,
        }
        for error in errors:
            if priority.get(error["reason"], 0) > priority.get(reason, 0):
                reason = error["reason"]
        detail = "; ".join(
            f"{error['path']}: {error.get('detail') or error['reason']}" for error in errors
        )[:500]
        self._mark_unsupported(key, reason, detail, retry=reason in ("unsupported", "no_permission"))
        payload = {"ok": False, "reason": reason, "detail": detail}
        self.cache[key] = payload
        return payload

    def _mark_unsupported(self, key, reason, detail=None, retry=True):
        now = self.now
        self.probes[key] = {
            "supported": False,
            "path": None,
            "reason": reason,
            "detail": (detail or "")[:500] or None,
            "checked_at": _iso(now),
            "retry_after": _iso(now + timedelta(seconds=RETRY_SECONDS)) if retry else None,
        }
        self.errors.append({"probe": key, "reason": reason, "detail": (detail or "")[:300]})

    def public_capabilities(self):
        return {
            "version": self.capabilities.get("version"),
            "major": self.capabilities.get("major"),
            "packages": self.capabilities.get("packages") or [],
            "checked_at": _iso(self.now),
            "probes": self.probes,
        }


# ---------------------------------------------------------
# Audit keamanan
# ---------------------------------------------------------

def _normalize_version(value):
    return re.sub(r"[^0-9]+", ".", _text(value)).strip(".")


def _version_tuple(value):
    parts = []
    for part in _normalize_version(value).split("."):
        if part.isdigit():
            parts.append(int(part))
        if len(parts) >= 2:
            break
    return tuple(parts)


def firmware_status(version, current_firmware, upgrade_firmware=None):
    """Status firmware: pending/up-to-date/unknown.

    Perangkat dianggap tertunda bila firmware RouterBOARD atau firmware
    upgrade berbeda dari versi paket RouterOS yang berjalan.
    """
    version = _text(version)
    current = _text(current_firmware)
    upgrade = _text(upgrade_firmware)
    if upgrade and current and upgrade != current:
        return "pending"
    if not version or not current:
        return "unknown"
    if _version_tuple(version) and _version_tuple(current) and _version_tuple(version) != _version_tuple(current):
        return "pending"
    if _normalize_version(version) != _normalize_version(current):
        return "pending"
    return "up-to-date"


_SERVICE_LABELS = {
    "telnet": "Telnet",
    "ftp": "FTP",
    "www": "WWW (HTTP)",
    "www-ssl": "WWW-SSL (HTTPS)",
    "api": "API",
    "api-ssl": "API-SSL",
    "winbox": "Winbox",
    "ssh": "SSH",
    "www-ssl": "WWW-SSL",
}


def _is_open_address(address):
    text = _text(address)
    if not text:
        return True
    return any(part.strip() in ("0.0.0.0/0", "::/0", "0.0.0.0", "::") for part in text.split(","))


def _last_login_old(value, now, days):
    text = _text(value).lower()
    if not text or text in ("never", "none", "-"):
        return True
    parsed = _parse_ts(value)
    if parsed is None:
        # RouterOS dapat menampilkan "nov/10/2026 12:00:00" atau format lokal.
        for fmt in ("%b/%d/%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%b/%d/%Y"):
            try:
                parsed = datetime.strptime(_text(value), fmt).replace(tzinfo=timezone.utc)
                break
            except ValueError:
                continue
    if parsed is None:
        return False
    return (now - parsed).days >= days


def build_security_findings(version, base, data, now=None):
    """Bangun temuan audit keamanan dari data probe yang sudah dikumpulkan.

    ``data``: dict key probe → payload hasil ``ModuleSession.query`` dengan
    kunci ``ok`` dan ``rows``. Semua temuan hanya dari konfigurasi terbaca.
    """
    now = now or _now()
    findings = []

    def rows(key):
        payload = data.get(key) or {}
        return payload.get("rows") or [] if payload.get("ok") else []

    def add(key, category, severity, title, detail, remediation):
        findings.append(
            {
                "finding_key": key,
                "category": category,
                "severity": severity,
                "title": title,
                "detail": detail,
                "remediation": remediation,
            }
        )

    # Layanan tidak aman / tanpa pembatasan alamat.
    for row in rows("services"):
        if _bool(row.get("disabled")):
            continue
        name = _text(row.get("name")).lower()
        address = _text(row.get("address"))
        label = _SERVICE_LABELS.get(name, name or "layanan")
        if name in ("telnet", "ftp", "www"):
            add(
                f"insecure_service:{name}",
                "insecure_service",
                "high" if name in ("telnet", "ftp") else "average",
                f"Layanan {label} aktif",
                f"Layanan {label} terbuka"
                + (f" pada alamat {address}." if address else " untuk semua alamat."),
                f"Nonaktifkan layanan {label} bila tidak dipakai, atau batasi alamat sumbernya.",
            )
        if name in ("api", "winbox", "ssh", "www-ssl") and _is_open_address(address):
            add(
                f"service_open:{name}",
                "service_open",
                "high" if name in ("api", "winbox") else "average",
                f"Layanan {label} tanpa pembatasan alamat",
                f"Layanan {label} menerima koneksi dari semua alamat.",
                f"Batasi alamat sumber layanan {label} ke jaringan manajemen.",
            )

    # User bawaan admin.
    for row in rows("users"):
        if _text(row.get("name")).lower() == "admin" and not _bool(row.get("disabled")):
            add(
                "default_admin",
                "default_admin",
                "high",
                "User bawaan \"admin\" masih aktif",
                "Akun admin bawaan mudah ditebak dan menjadi target percobaan login.",
                "Buat user administrator baru, nonaktifkan akun \"admin\", lalu gunakan kata sandi kuat.",
            )

    # DNS allow-remote-requests.
    for row in rows("dns"):
        if _bool(row.get("allow-remote-requests")):
            add(
                "dns_remote",
                "dns_remote",
                "average",
                "DNS mengizinkan permintaan dari luar",
                "allow-remote-requests aktif sehingga router dapat dipakai sebagai resolver terbuka.",
                "Matikan allow-remote-requests bila router bukan resolver publik.",
            )
            break

    # NTP client mati.
    ntp_rows = rows("ntp")
    if ntp_rows and not any(_bool(row.get("enabled")) for row in ntp_rows):
        add(
            "ntp_disabled",
            "ntp_disabled",
            "warning",
            "Klien NTP tidak aktif",
            "Jam router dapat bergeser dan memengaruhi log serta sertifikat.",
            "Aktifkan klien NTP (/system ntp client) dan arahkan ke server waktu tepercaya.",
        )

    # Firmware tertunda.
    rb = (rows("routerboard") or [{}])[0]
    current = _text(rb.get("current_firmware")) or _text((base or {}).get("current_firmware"))
    upgrade = _text(rb.get("upgrade_firmware"))
    status = firmware_status(version, current, upgrade)
    if status == "pending":
        add(
            "firmware_pending",
            "firmware_pending",
            "warning",
            "Firmware RouterBOARD tertunda",
            f"Firmware berjalan {current or '—'}, versi paket {version or '—'}"
            + (f", firmware tersedia {upgrade}." if upgrade else "."),
            "Jadwalkan upgrade firmware RouterBOARD dan reboot pada jendela pemeliharaan.",
        )

    # User lama / tidak pernah login.
    try:
        stale_days = int((base or {}).get("stale_user_days") or 90)
    except (TypeError, ValueError):
        stale_days = 90
    for row in rows("users"):
        name = _text(row.get("name"))
        if not name or _bool(row.get("disabled")):
            continue
        if _last_login_old(row.get("last-logged-in"), now, stale_days):
            add(
                f"stale_user:{name}",
                "stale_user",
                "information",
                f"User router lama: {name}",
                f"User {name} tidak pernah login atau terakhir login lebih dari {stale_days} hari lalu.",
                "Nonaktifkan atau hapus user yang tidak lagi digunakan.",
            )

    # Lisensi kedaluwarsa (bila field tersedia).
    for row in rows("license"):
        expires = _text(row.get("expires-at"))
        if expires and _parse_ts(expires) and _parse_ts(expires) < now:
            add(
                "license_expired",
                "license_expired",
                "high",
                "Lisensi RouterOS kedaluwarsa",
                f"Lisensi berakhir pada {expires}.",
                "Perbarui lisensi RouterOS agar pembaruan dan dukungan tetap berjalan.",
            )
            break

    return findings


# ---------------------------------------------------------
# Koleksi layer
# ---------------------------------------------------------

def _module_enabled(modules, key):
    if not modules:
        return True
    return bool(modules.get(key, True))


def _probe_enabled_for_layer(key, layer):
    return key in LAYER_KEYS.get(layer, [])


def _payload_ok(payload):
    return bool(payload and payload.get("ok"))


def _truncated(payload):
    return bool(payload and payload.get("truncated"))


def collect(api, *, base, layers, modules, capabilities, log_cursor=None, max_rows=MAX_ROWS, now=None):
    """Kumpulkan modul sesuai layer. Return dict siap disimpan poller."""
    now = now or _now()
    session = ModuleSession(api, capabilities, max_rows=max_rows, now=now)
    version = _text((base or {}).get("version"))
    major = None
    match = re.match(r"(\d+)", version)
    if match:
        major = int(match.group(1))
    if session.capabilities.get("version") != version:
        # Versi berubah: probe ulang seluruh path (paket bisa berubah).
        session.probes = {}
        session.cache = {}
    session.capabilities["version"] = version
    session.capabilities["major"] = major

    modules_out = {}
    counts = {}
    layer_status = {}
    truncated = {}
    probes_used = []

    def run_query(key):
        payload = session.query(key)
        if key not in probes_used:
            probes_used.append(key)
        return payload

    # ---------- FAST ----------
    if "fast" in layers:
        errors = []
        # Interface dasar selalu ada dari collector dasar.
        interfaces = (base or {}).get("interfaces") or []
        modules_out["interfaces"] = {
            "ok": True,
            "rows": interfaces[:max_rows],
            "count": len(interfaces),
            "truncated": len(interfaces) > max_rows,
        }
        if len(interfaces) > max_rows:
            truncated["interfaces"] = True

        addresses = run_query("addresses")
        modules_out["addresses"] = _module_payload(addresses, lambda row: _normalize_address(row))
        if not addresses.get("ok"):
            errors.append(("addresses", addresses.get("reason")))

        clock = run_query("clock")
        modules_out["clock"] = _module_payload(clock, lambda row: _normalize_clock(row))
        if not clock.get("ok"):
            errors.append(("clock", clock.get("reason")))

        for key, module_key, normalizer in (
            ("ppp", "ppp", lambda row: _normalize_ppp(row)),
            ("hotspot", "hotspot", lambda row: _normalize_hotspot(row)),
        ):
            if not _module_enabled(modules, module_key):
                modules_out[key] = {"ok": True, "disabled": True, "rows": [], "count": 0}
                continue
            payload = run_query(key)
            modules_out[key] = _module_payload(payload, normalizer)
            if not payload.get("ok"):
                errors.append((key, payload.get("reason")))

        if _module_enabled(modules, "wireless"):
            wireless = run_query("wireless")
            variant = wireless.get("variant")
            modules_out["wireless"] = _module_payload(
                wireless, lambda row: _normalize_wireless(row, variant)
            )
            if not wireless.get("ok"):
                errors.append(("wireless", wireless.get("reason")))
        else:
            modules_out["wireless"] = {"ok": True, "disabled": True, "rows": [], "count": 0}

        if _module_enabled(modules, "log"):
            logs_payload = run_query("logs")
            if logs_payload.get("ok"):
                raw_rows = logs_payload.get("rows") or []
                new_rows = logs_after_cursor(raw_rows, log_cursor)
                relevant = []
                for row in new_rows:
                    parsed = classify_log(row)
                    if parsed:
                        relevant.append(parsed)
                modules_out["logs"] = {
                    "ok": True,
                    "rows": relevant[:max_rows],
                    "count": len(relevant),
                    "truncated": len(relevant) > max_rows,
                    "cursor": next_log_cursor(log_cursor, raw_rows),
                    "fetched": len(raw_rows),
                }
                if len(relevant) > max_rows:
                    truncated["logs"] = True
            else:
                modules_out["logs"] = _module_payload(logs_payload, lambda row: classify_log(row))
                errors.append(("logs", logs_payload.get("reason")))
        else:
            modules_out["logs"] = {"ok": True, "disabled": True, "rows": [], "count": 0}

        layer_status["fast"] = {"ok": not errors, "errors": errors}

    # ---------- MEDIUM ----------
    if "medium" in layers:
        errors = []
        if _module_enabled(modules, "dhcp"):
            for key, normalizer in (
                ("dhcp", lambda row: _normalize_lease(row)),
                ("arp", lambda row: _normalize_arp(row)),
                ("dhcp_pools", lambda row: {
                    "row_key": _row_key("pool", row.get("name")),
                    "name": _text(row.get("name")) or None,
                    "ranges": _text(row.get("ranges")) or None,
                    "capacity": _pool_capacity(row.get("ranges")),
                }),
                ("dhcp_servers", lambda row: {
                    "row_key": _row_key("dhcpserver", row.get("name")),
                    "name": _text(row.get("name")) or None,
                    "address_pool": _text(row.get("address-pool")) or None,
                    "interface": _text(row.get("interface")) or None,
                    "disabled": _bool(row.get("disabled")),
                    "router_id": _text(row.get(".id")) or None,
                }),
            ):
                payload = run_query(key)
                modules_out[key] = _module_payload(payload, normalizer)
                if not payload.get("ok"):
                    errors.append((key, payload.get("reason")))
            modules_out["dhcp_pools"] = _with_pool_usage(
                modules_out.get("dhcp_pools"), modules_out.get("dhcp"), modules_out.get("dhcp_servers")
            )
        else:
            for key in ("dhcp", "arp", "dhcp_pools", "dhcp_servers"):
                modules_out[key] = {"ok": True, "disabled": True, "rows": [], "count": 0}

        payload = run_query("neighbors")
        modules_out["neighbors"] = _module_payload(payload, lambda row: _normalize_neighbor(row))
        if not payload.get("ok"):
            errors.append(("neighbors", payload.get("reason")))

        if _module_enabled(modules, "routing"):
            payload = run_query("routes")
            modules_out["routes"] = _module_payload(payload, lambda row: _normalize_route(row, "ipv4"))
            if not payload.get("ok"):
                errors.append(("routes", payload.get("reason")))
            payload = run_query("routes6")
            modules_out["routes6"] = _module_payload(payload, lambda row: _normalize_route(row, "ipv6"))
            if not payload.get("ok"):
                errors.append(("routes6", payload.get("reason")))
            for key, normalizer in (
                ("bgp", lambda row: _normalize_bgp(row, "bgp")),
                ("ospf", lambda row: _normalize_ospf(row)),
            ):
                payload = run_query(key)
                modules_out[key] = _module_payload(payload, normalizer)
                if not payload.get("ok"):
                    errors.append((key, payload.get("reason")))
        else:
            for key in ("routes", "routes6", "bgp", "ospf"):
                modules_out[key] = {"ok": True, "disabled": True, "rows": [], "count": 0}

        for key, kind in (("firewall_filter", "filter"), ("firewall_nat", "nat"), ("firewall_mangle", "mangle")):
            payload = run_query(key)
            modules_out[key] = _module_payload(payload, lambda row, k=kind: _normalize_firewall(row, k))
            if not payload.get("ok"):
                errors.append((key, payload.get("reason")))

        payload = run_query("connections")
        if payload.get("ok"):
            modules_out["connections"] = {
                "ok": True,
                "count": payload.get("count"),
                "rows": [],
                "truncated": False,
            }
        else:
            modules_out["connections"] = _module_payload(payload, lambda row: row)
            errors.append(("connections", payload.get("reason")))

        payload = run_query("address_lists")
        modules_out["address_lists"] = _module_payload(payload, lambda row: _normalize_address_list(row))
        if not payload.get("ok"):
            errors.append(("address_lists", payload.get("reason")))

        if _module_enabled(modules, "queue"):
            for key, kind in (("queue_simple", "simple"), ("queue_tree", "tree")):
                payload = run_query(key)
                modules_out[key] = _module_payload(payload, lambda row, k=kind: _normalize_queue(row, k))
                if not payload.get("ok"):
                    errors.append((key, payload.get("reason")))
        else:
            for key in ("queue_simple", "queue_tree"):
                modules_out[key] = {"ok": True, "disabled": True, "rows": [], "count": 0}

        layer_status["medium"] = {"ok": not errors, "errors": errors}

    # ---------- SLOW ----------
    if "slow" in layers:
        errors = []
        for key, normalizer in (
            ("services", lambda row: _normalize_service(row)),
            ("users", lambda row: _normalize_user(row)),
            ("dns", lambda row: _normalize_dns(row)),
            ("ntp", lambda row: _normalize_ntp(row)),
            ("packages", lambda row: _normalize_package(row)),
            ("routerboard", lambda row: _normalize_routerboard(row)),
            ("license", lambda row: _normalize_license(row)),
        ):
            payload = run_query(key)
            modules_out[key] = _module_payload(payload, normalizer)
            if not payload.get("ok"):
                errors.append((key, payload.get("reason")))

        # Temuan keamanan dihitung dari data yang sudah diambil.
        if _module_enabled(modules, "security"):
            security_data = {
                key: session.query(key, force=False)
                for key in ("services", "users", "dns", "ntp", "routerboard", "license")
            }
            findings = build_security_findings(
                version, {**(base or {}), "stale_user_days": _stale_user_days(base)}, security_data, now
            )
            modules_out["security"] = {"ok": True, "rows": findings, "count": len(findings)}
        else:
            modules_out["security"] = {"ok": True, "disabled": True, "rows": [], "count": 0}
        layer_status["slow"] = {"ok": not errors, "errors": errors}

    # ---------- Ringkasan sistem ----------
    system = _build_system(base, modules_out, now)

    # ---------- Hitung metrik ----------
    for key, metric in (
        ("ppp", "ppp_active"), ("hotspot", "hotspot_active"),
        ("wireless", "wireless_clients"), ("dhcp", "dhcp_leases"),
        ("arp", "arp_entries"), ("routes", "routes"),
        ("routes6", "routes6"),
    ):
        payload = modules_out.get(key) or {}
        if payload.get("ok") and not payload.get("disabled"):
            counts[metric] = payload.get("count") or 0
    if (modules_out.get("connections") or {}).get("ok"):
        counts["connections"] = modules_out["connections"].get("count")

    for key, payload in modules_out.items():
        if payload.get("truncated"):
            truncated[key] = True

    return {
        "ok": True,
        "layers": list(layers),
        "modules": modules_out,
        "counts": counts,
        "system": system,
        "capabilities": session.public_capabilities(),
        "log_cursor": (modules_out.get("logs") or {}).get("cursor") or log_cursor or {},
        "layer_status": layer_status,
        "truncated": truncated,
        "probe_errors": session.errors,
    }


def _module_payload(payload, normalizer):
    if not payload:
        return {"ok": False, "reason": "error", "rows": [], "count": 0}
    if not payload.get("ok"):
        return {
            "ok": False,
            "reason": payload.get("reason") or "error",
            "detail": payload.get("detail"),
            "cached": payload.get("cached", False),
            "rows": [],
            "count": 0,
        }
    rows = []
    for row in payload.get("rows") or []:
        try:
            normalized = normalizer(row)
        except Exception:  # noqa: BLE001
            continue
        if normalized:
            rows.append(normalized)
    return {
        "ok": True,
        "rows": rows,
        "count": payload.get("count") if payload.get("count") is not None else len(rows),
        "truncated": bool(payload.get("truncated")),
        "variant": payload.get("variant"),
        "path": payload.get("path"),
    }


def _stale_user_days(base):
    return (base or {}).get("stale_user_days") or 90


def _pool_capacity(ranges):
    """Hitung kapasitas pool dari string ranges RouterOS."""
    total = 0
    for chunk in _text(ranges).split(","):
        piece = chunk.strip()
        if not piece:
            continue
        if "-" in piece:
            start, end = piece.split("-", 1)
            try:
                start_ip = start.strip()
                end_ip = end.strip()
                # Dukung 192.168.1.100-192.168.1.200 dan 192.168.1.100-200.
                if "." not in end_ip:
                    end_ip = start_ip.rsplit(".", 1)[0] + "." + end_ip
                from ipaddress import ip_address

                total += int(ip_address(end_ip)) - int(ip_address(start_ip)) + 1
            except (ValueError, TypeError):
                continue
        else:
            total += 1
    return total


def _with_pool_usage(pools_payload, leases_payload, servers_payload):
    if not pools_payload or not pools_payload.get("ok"):
        return pools_payload or {"ok": False, "rows": [], "count": 0}
    leases = (leases_payload or {}).get("rows") or []
    servers = (servers_payload or {}).get("rows") or []
    server_pool = {
        _text(row.get("name")): _text(row.get("address_pool"))
        for row in servers
        if _text(row.get("name"))
    }
    used = {}
    for lease in leases:
        if lease.get("disabled"):
            continue
        server = _text(lease.get("server"))
        pool = server_pool.get(server) or server
        if pool:
            used[pool] = used.get(pool, 0) + 1
    rows = []
    for pool in pools_payload.get("rows") or []:
        name = _text(pool.get("name"))
        capacity = pool.get("capacity")
        count = used.get(name, 0)
        usage = round(100.0 * count / capacity, 1) if capacity and capacity > 0 else None
        row = dict(pool)
        row["used"] = count
        row["usage_pct"] = usage
        rows.append(row)
    return {
        "ok": True,
        "rows": rows,
        "count": len(rows),
        "truncated": pools_payload.get("truncated", False),
    }


def _build_system(base, modules_out, now):
    rb_payload = modules_out.get("routerboard") or {}
    rb = (rb_payload.get("rows") or [{}])[0] if rb_payload.get("ok") else {}
    license_payload = modules_out.get("license") or {}
    license_row = (license_payload.get("rows") or [{}])[0] if license_payload.get("ok") else {}
    clock_payload = modules_out.get("clock") or {}
    clock = (clock_payload.get("rows") or [{}])[0] if clock_payload.get("ok") else {}
    packages_payload = modules_out.get("packages") or {}
    packages = [
        {"name": row.get("name"), "version": row.get("version"), "disabled": row.get("disabled")}
        for row in (packages_payload.get("rows") or [])
        if packages_payload.get("ok")
    ]
    version = _text((base or {}).get("version"))
    current_firmware = _text(rb.get("current_firmware")) or _text((base or {}).get("current_firmware"))
    upgrade_firmware = _text(rb.get("upgrade_firmware"))
    router_utc = clock_utc(clock)
    drift = None
    if router_utc:
        drift = int((router_utc - now).total_seconds())
    return {
        "identity": (base or {}).get("identity"),
        "model": rb.get("model") or (base or {}).get("model"),
        "serial": rb.get("serial_number") or (base or {}).get("serial_number"),
        "version": version,
        "major": int(re.match(r"(\d+)", version).group(1)) if re.match(r"(\d+)", version) else None,
        "board": (base or {}).get("board_name"),
        "architecture": (base or {}).get("architecture_name"),
        "current_firmware": current_firmware or None,
        "upgrade_firmware": upgrade_firmware or None,
        "firmware_status": firmware_status(version, current_firmware, upgrade_firmware),
        "license_level": license_row.get("level"),
        "license_expires": license_row.get("expires_at"),
        "clock_time": clock.get("time"),
        "clock_date": clock.get("date"),
        "gmt_offset": clock.get("gmt_offset"),
        "time_zone_name": clock.get("time_zone_name"),
        "router_utc": router_utc.strftime("%Y-%m-%dT%H:%M:%SZ") if router_utc else None,
        "drift_seconds": drift,
        "packages": packages,
        "packages_supported": bool(packages_payload.get("ok")),
    }


# ---------------------------------------------------------
# Status untuk problem poller
# ---------------------------------------------------------

_NEIGHBOR_UP_STATES = {"established", "full", "up", "2way", "loading", "exchange"}


def neighbor_down(rows):
    """Return daftar neighbor routing yang tidak Established/Full/Up."""
    result = []
    for row in rows or []:
        if row.get("disabled"):
            continue
        state = _text(row.get("state")).lower()
        if state and state not in _NEIGHBOR_UP_STATES:
            result.append(row)
        elif not state:
            result.append(row)
    return result


def pool_high(rows, threshold=90):
    result = []
    for row in rows or []:
        usage = row.get("usage_pct")
        if usage is not None and usage >= float(threshold):
            result.append(row)
    return result


def sessions_drop(previous, current, threshold_pct=30):
    """True bila sesi turun > threshold dari nilai sebelumnya (min 5 sesi)."""
    try:
        previous = float(previous)
        current = float(current)
    except (TypeError, ValueError):
        return None
    if previous < 5:
        return None
    drop = 100.0 * (previous - current) / previous
    if drop > float(threshold_pct):
        return round(drop, 1)
    return None


# ---------------------------------------------------------
# Aksi tulis (M5) — allowlist, tidak ada perintah bebas
# ---------------------------------------------------------

ALLOWED_ACTIONS = {
    "disconnect_ppp",
    "disconnect_hotspot",
    "address_list_add",
    "address_list_remove",
    "interface_enable",
    "interface_disable",
    "reboot",
}

_ACTION_PATHS = {
    "disconnect_ppp": "/ppp/active/remove",
    "disconnect_hotspot": "/ip/hotspot/active/remove",
    "address_list_add": "/ip/firewall/address-list/add",
    "address_list_remove": "/ip/firewall/address-list/remove",
    "interface_enable": "/interface/set",
    "interface_disable": "/interface/set",
    "reboot": "/system/reboot",
}


class ActionError(Exception):
    def __init__(self, message, code="action_failed"):
        super().__init__(message)
        self.code = code


def _action_connect(config, timeout=DEFAULT_TIMEOUT):
    kwargs = {"timeout": timeout, "port": int(config.get("port") or 8728)}
    if bool(config.get("ssl")):
        # M-11: aksi tulis memakai verifikasi TLS yang sama dengan pembacaan.
        try:
            kwargs["ssl_wrapper"] = tls_util.ssl_wrapper(
                config["ip"], config.get("ssl_fingerprint")
            )
        except ValueError as exc:
            raise ActionError(str(exc), "tls_error") from exc
    return connect(
        config["ip"],
        username=(config.get("user") or "").strip(),
        password=config.get("password") or "",
        **kwargs,
    )


def _safe_rows(api, path, fields):
    try:
        return list(api.rawCmd(path, f"=.proplist={fields}"))
    except Exception:  # noqa: BLE001
        return []


def find_management_interface(management_ip, address_rows):
    """Nama interface yang memegang alamat IP manajemen, atau None."""
    try:
        from ipaddress import ip_address, ip_network

        target = ip_address(_text(management_ip))
    except (ValueError, TypeError):
        return None
    for row in address_rows or []:
        address = _text(row.get("address"))
        if not address:
            continue
        try:
            network = ip_network(address, strict=False)
        except ValueError:
            continue
        if target in network:
            return _text(row.get("interface")) or None
    return None


def perform_action(config, action, params):
    """Jalankan satu aksi allowlist lewat kredensial tulis, lalu baca ulang.

    Return dict ``{ok, action, target, before, after, verified, detail}``.
    Tidak pernah menerima perintah bebas dari pemanggil.
    """
    if action not in ALLOWED_ACTIONS:
        raise ActionError("Aksi tidak ada dalam allowlist.", "not_allowed")
    params = params or {}
    target = _text(params.get("target"))
    if not target:
        raise ActionError("Target aksi wajib diisi.", "invalid_target")

    api = None
    try:
        api = _action_connect(config)
        if action == "disconnect_ppp":
            before = _safe_rows(api, "/ppp/active/print", ".id,name,caller-id,address")
            match = next((row for row in before if _text(row.get(".id")) == target), None)
            if match is None:
                raise ActionError("Sesi PPP tidak ditemukan (mungkin sudah terputus).", "not_found")
            api.rawCmd("/ppp/active/remove", f"=.id={target}")
            after = _safe_rows(api, "/ppp/active/print", ".id,name,caller-id,address")
            verified = not any(_text(row.get(".id")) == target for row in after)
            return {"ok": True, "action": action, "target": target, "before": match,
                    "after": None, "verified": verified,
                    "detail": "Sesi PPP diputuskan." if verified else "Perintah dikirim; sesi masih terlihat."}

        if action == "disconnect_hotspot":
            before = _safe_rows(api, "/ip/hotspot/active/print", ".id,user,address,mac-address")
            match = next((row for row in before if _text(row.get(".id")) == target), None)
            if match is None:
                raise ActionError("Sesi hotspot tidak ditemukan (mungkin sudah terputus).", "not_found")
            api.rawCmd("/ip/hotspot/active/remove", f"=.id={target}")
            after = _safe_rows(api, "/ip/hotspot/active/print", ".id,user,address,mac-address")
            verified = not any(_text(row.get(".id")) == target for row in after)
            return {"ok": True, "action": action, "target": target, "before": match,
                    "after": None, "verified": verified,
                    "detail": "Sesi hotspot diputuskan." if verified else "Perintah dikirim; sesi masih terlihat."}

        if action in ("address_list_add", "address_list_remove"):
            list_name = _text(params.get("list"))
            address = _text(params.get("address"))
            if not list_name or not address:
                raise ActionError("Nama address-list dan alamat wajib diisi.", "invalid_target")
            before_rows = _safe_rows(api, "/ip/firewall/address-list/print",
                                     ".id,list,address,timeout,comment")
            if action == "address_list_add":
                timeout = _text(params.get("timeout")) or "1h"
                comment = _text(params.get("comment"))[:200]
                if not re.fullmatch(r"\d+[smhdw]?", timeout):
                    raise ActionError("Timeout harus berupa angka + satuan (mis. 1h, 30m).", "invalid_timeout")
                if params.get("dry_run"):
                    raise ActionError("Aksi uji tidak dijalankan.", "dry_run")
                api.rawCmd(
                    "/ip/firewall/address-list/add",
                    f"=list={list_name}", f"=address={address}",
                    f"=timeout={timeout}", f"=comment={comment}",
                )
                after_rows = _safe_rows(api, "/ip/firewall/address-list/print",
                                        ".id,list,address,timeout,comment")
                match = next(
                    (row for row in after_rows
                     if _text(row.get("list")) == list_name and _text(row.get("address")) == address
                     and _text(row.get("comment")) == comment),
                    None,
                )
                return {"ok": True, "action": action, "target": f"{list_name}:{address}",
                        "before": before_rows[:10], "after": match, "verified": match is not None,
                        "detail": "Entri address-list ditambahkan." if match else "Entri belum terlihat setelah ditambahkan."}
            router_id = target
            match = next((row for row in before_rows if _text(row.get(".id")) == router_id), None)
            if match is None:
                raise ActionError("Entri address-list tidak ditemukan.", "not_found")
            api.rawCmd("/ip/firewall/address-list/remove", f"=.id={router_id}")
            after_rows = _safe_rows(api, "/ip/firewall/address-list/print",
                                    ".id,list,address,timeout,comment")
            verified = not any(_text(row.get(".id")) == router_id for row in after_rows)
            return {"ok": True, "action": action, "target": target, "before": match,
                    "after": None, "verified": verified,
                    "detail": "Entri address-list dihapus." if verified else "Entri masih terlihat."}

        if action in ("interface_enable", "interface_disable"):
            interface_name = target
            address_rows = _safe_rows(api, "/ip/address/print", ".id,address,interface")
            management_interface = find_management_interface(config.get("management_ip"), address_rows)
            if action == "interface_disable" and management_interface and management_interface == interface_name:
                raise ActionError(
                    "Interface ini memegang alamat IP manajemen perangkat; penonaktifan ditolak.",
                    "management_interface",
                )
            interface_rows = _safe_rows(api, "/interface/print", ".id,name,disabled,running")
            match = next((row for row in interface_rows if _text(row.get("name")) == interface_name), None)
            if match is None:
                raise ActionError("Interface tidak ditemukan.", "not_found")
            disabled = "yes" if action == "interface_disable" else "no"
            api.rawCmd("/interface/set", f"=.id={_text(match.get('.id'))}", f"=disabled={disabled}")
            after_rows = _safe_rows(api, "/interface/print", ".id,name,disabled,running")
            after = next((row for row in after_rows if _text(row.get("name")) == interface_name), None)
            want_disabled = action == "interface_disable"
            verified = bool(after) and _bool(after.get("disabled")) == want_disabled
            return {"ok": True, "action": action, "target": interface_name, "before": match,
                    "after": after, "verified": verified,
                    "detail": "Interface diperbarui." if verified else "Perubahan belum terverifikasi."}

        if action == "reboot":
            api.rawCmd("/system/reboot")
            return {"ok": True, "action": action, "target": target, "before": None,
                    "after": None, "verified": None,
                    "detail": "Perintah reboot dikirim; verifikasi dilakukan setelah perangkat kembali online."}

        raise ActionError("Aksi tidak dikenal.", "not_allowed")
    except ActionError:
        raise
    except (TrapError, MultiTrapError) as exc:
        raise ActionError(f"RouterOS menolak aksi: {exc}", classify_error(exc)) from exc
    except tls_util.TlsVerificationError as exc:
        raise ActionError(str(exc), exc.code) from exc
    except (LibRouterosError, TimeoutError, OSError) as exc:
        raise ActionError(f"Gagal menjalankan aksi: {exc}", "unreachable") from exc
    finally:
        if api is not None:
            try:
                api.close()
            except Exception:  # noqa: BLE001
                pass
