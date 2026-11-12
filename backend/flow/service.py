"""E3 — penerima Traffic Flow (NetFlow v5/v9, IPFIX) + agregasi bucket 5 menit.

Prinsip (sama dengan penerima syslog/trap):
- Port default MATI; galat bind tidak menjatuhkan aplikasi.
- Hanya IP eksporter/perangkat terdaftar yang diterima.
- Batas laju per sumber, batas ukuran paket, cache template per
  (eksporter, jenis, observation domain) dengan TTL + batas ukuran.
- Flow mentah TIDAK disimpan; bucket 5 menit di memori, flush tiap 60 detik
  dalam SATU transaksi (termasuk top-N per kunci; sisanya ke "lainnya").
"""

import collections
import logging
import socket
import threading
import time
from datetime import datetime, timezone

import db
import netflow
from netflow.utils import get_export_version

from ingest.service import RateLimiter

log = logging.getLogger("nms.flow")

MAX_PACKET_BYTES = 9216
DEVICE_CACHE_SECONDS = 10
TEMPLATE_TTL_SECONDS = 2 * 3600
TEMPLATE_MAX = 500
FLUSH_SECONDS = 60

_PROTO_NAMES = {
    1: "ICMP", 2: "IGMP", 6: "TCP", 8: "EGP", 17: "UDP", 41: "IPv6-encap",
    47: "GRE", 50: "ESP", 51: "AH", 58: "ICMPv6", 66: "TCP-MD5", 89: "OSPF",
    103: "PIM", 112: "VRRP",
}
_APP_BY_PORT = {
    (6, 20): "FTP-data", (6, 21): "FTP", (6, 22): "SSH", (6, 23): "Telnet",
    (6, 25): "SMTP", (6, 53): "DNS", (6, 79): "Finger", (6, 80): "HTTP",
    (6, 88): "Kerberos", (6, 110): "POP3", (6, 113): "Ident", (6, 123): "NTP",
    (6, 143): "IMAP", (6, 161): "SNMP", (6, 179): "BGP", (6, 389): "LDAP",
    (6, 443): "HTTPS", (6, 445): "SMB", (6, 465): "SMTPS", (6, 514): "Syslog",
    (6, 587): "SMTP-submission", (6, 636): "LDAPS", (6, 993): "IMAPS",
    (6, 995): "POP3S", (6, 1433): "MSSQL", (6, 1723): "PPTP", (6, 3306): "MySQL",
    (6, 3389): "RDP", (6, 5432): "PostgreSQL", (6, 5900): "VNC", (6, 8080): "HTTP-alt",
    (6, 8443): "HTTPS-alt", (6, 8291): "Winbox", (6, 8728): "RouterOS-API",
    (6, 8729): "RouterOS-API-SSL", (17, 53): "DNS", (17, 67): "DHCP", (17, 68): "DHCP",
    (17, 123): "NTP", (17, 161): "SNMP", (17, 162): "SNMP-trap", (17, 500): "IPsec",
    (17, 4500): "IPsec-NAT-T", (1, 0): "ICMP", (58, 0): "ICMPv6",
}


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _proto_name(proto):
    try:
        number = int(proto)
    except (TypeError, ValueError):
        return "unknown"
    return _PROTO_NAMES.get(number, f"proto/{number}")


def _app_name(proto, dport, sport):
    try:
        p_no = int(proto)
    except (TypeError, ValueError):
        p_no = -1
    for candidate in ((p_no, int(dport or -1)), (p_no, int(sport or -1))):
        hit = _APP_BY_PORT.get(candidate)
        if hit:
            return hit
    return f"{_proto_name(p_no)}/{int(dport)}" if dport is not None else _proto_name(p_no)


def _pick(data, *names):
    for name in names:
        if name in data and data[name] is not None:
            return data[name]
    return None


def _ip_text(value):
    if isinstance(value, bytes):
        try:
            import ipaddress

            return ipaddress.ip_address(value).compressed
        except ValueError:
            try:
                import socket

                return socket.inet_ntoa(value) if len(value) == 4 else value.hex()
            except OSError:
                return value.hex()
    if isinstance(value, int):
        import socket

        if 0 <= value <= 0xFFFFFFFF:
            return socket.inet_ntoa(value.to_bytes(4, "big"))
        try:
            import ipaddress

            return ipaddress.ip_address(value).compressed
        except ValueError:
            return str(value)
    return str(value)


def _normalize_flow(flow, sampling_hint=1):
    """Ambil field esensial; return dict atau None bila tidak lengkap."""
    data = getattr(flow, "data", None)
    if not isinstance(data, dict):
        return None
    source = _pick(data, "IPV4_SRC_ADDR", "IPV6_SRC_ADDR",
                   "sourceIPv4Address", "sourceIPv6Address")
    destination = _pick(data, "IPV4_DST_ADDR", "IPV6_DST_ADDR",
                        "destinationIPv4Address", "destinationIPv6Address")
    proto = _pick(data, "PROTO", "PROTOCOL", "protocolIdentifier")
    bytes_ = _pick(data, "IN_BYTES", "IN_OCTETS", "octetDeltaCount",
                   "postOctetDeltaCount", "OUT_BYTES")
    packets = _pick(data, "IN_PKTS", "IN_PACKETS", "packetDeltaCount",
                    "postPacketDeltaCount")
    if source is None or destination is None or proto is None:
        return None
    try:
        bytes_ = int(bytes_ or 0)
        packets = int(packets or 0)
    except (TypeError, ValueError):
        return None
    sampling = _pick(data, "SAMPLING_INTERVAL", "samplingInterval") or sampling_hint or 1
    try:
        sampling = int(sampling)
    except (TypeError, ValueError):
        sampling = 1
    return {
        "src": _ip_text(source), "dst": _ip_text(destination), "proto": int(proto),
        "sport": _pick(data, "SRC_PORT", "L4_SRC_PORT", "sourceTransportPort"),
        "dport": _pick(data, "DST_PORT", "L4_DST_PORT", "destinationTransportPort"),
        "bytes": max(0, bytes_), "packets": max(0, packets),
        "in_if": _pick(data, "INPUT", "INPUT_SNMP", "ingressInterface"),
        "out_if": _pick(data, "OUTPUT", "OUTPUT_SNMP", "egressInterface"),
        "tcp_flags": _pick(data, "TCP_FLAGS", "tcpControlBits"),
        "sampling": max(1, sampling),
    }


def _if_label(device_id, if_index):
    if if_index is None:
        return None
    try:
        number = int(if_index)
    except (TypeError, ValueError):
        return None
    name = db.if_name_for_index(device_id, number)
    return name or f"ifIndex:{number}"


class FlowManager:
    def __init__(self):
        self._lock = threading.RLock()
        self._receivers = {}
        self._threads = []
        self._stop = threading.Event()
        self._rate = None
        self._device_cache = {}
        self._device_cache_at = 0.0
        self._templates = {}
        self._buckets = {}
        self._stats = {}
        self._configs = {}
        self._flush_thread = None
        self._flush_stop = threading.Event()
        self._started = False

    # -------------------------------------------------
    # Siklus hidup
    # -------------------------------------------------
    def _new_stats(self):
        return {
            "received": 0, "accepted": 0, "dropped_unknown": 0, "dropped_rate": 0,
            "dropped_size": 0, "dropped_invalid": 0, "dropped_no_template": 0,
            "flows": 0, "bytes": 0, "last_packet_at": None, "last_error": None,
            "running": False, "started_at": None, "port": None, "bind_address": None,
            "templates": 0, "flush_pending": 0, "written_rows": 0, "last_flush_at": None,
        }

    def ensure_pipeline(self):
        if self._rate is None:
            settings = db.get_settings()
            self._rate = RateLimiter(
                settings.get("ingest_rate_limit_per_sec", 200),
                settings.get("ingest_rate_window_seconds", 10),
            )
        if not self._stats:
            self._stats = {kind: self._new_stats() for kind in ("netflow_udp", "ipfix_udp")}
        if self._configs:
            return
        self._configs = {}
        for kind in ("netflow_udp", "ipfix_udp"):
            self._configs[kind] = db.get_ingest_receiver(kind, include_secret=True) or {}

    def reload(self):
        with self._lock:
            self.stop()
            self._stop.clear()
            self.ensure_pipeline()
            self._rate = None
            self._configs = {}
            self._stats = {kind: self._new_stats() for kind in ("netflow_udp", "ipfix_udp")}
            self.ensure_pipeline()
            for kind in ("netflow_udp", "ipfix_udp"):
                config = self._configs.get(kind) or db.get_ingest_receiver(kind, include_secret=True) or {}
                self._configs[kind] = config
                if not config.get("enabled"):
                    continue
                self._stats[kind]["running"] = True
                self._stats[kind]["port"] = config.get("port")
                self._stats[kind]["bind_address"] = config.get("bind_address")
                self._stats[kind]["started_at"] = _now_iso()
                try:
                    self._start_udp(kind, config)
                except Exception as exc:
                    self._stats[kind]["running"] = False
                    self._stats[kind]["last_error"] = f"bind gagal: {exc}"
                    log.error("Penerima flow %s gagal bind %s:%s — %s", kind,
                              config.get("bind_address"), config.get("port"), exc)
            self._start_flush_thread()
            self._started = True
            return self.status()

    def start(self):
        if not self._started:
            self.reload()

    def stop(self):
        self._stop.set()
        self._flush_stop.set()
        for sock in list(self._receivers.values()):
            try:
                sock.close()
            except Exception:
                pass
        self._receivers.clear()
        for thread in list(self._threads):
            if thread.is_alive():
                thread.join(timeout=2.0)
        self._threads.clear()
        if self._flush_thread is not None and self._flush_thread.is_alive():
            self._flush_thread.join(timeout=3.0)
        self._flush_thread = None
        self.flush_buckets()
        for stats in self._stats.values():
            stats["running"] = False
        self._started = False
        self._flush_stop.clear()

    def _start_flush_thread(self):
        if self._flush_thread is not None and self._flush_thread.is_alive():
            return
        self._flush_stop.clear()
        self._flush_thread = threading.Thread(
            target=self._flush_loop, name="flow-flush", daemon=True
        )
        self._flush_thread.start()

    def _flush_loop(self):
        while not self._flush_stop.is_set():
            if self._flush_stop.wait(FLUSH_SECONDS):
                break
            try:
                self.flush_buckets()
            except Exception:
                log.exception("Flush flow bucket gagal; data dipertahankan.")

    # -------------------------------------------------
    # Penerima UDP
    # -------------------------------------------------
    def _start_udp(self, kind, config):
        bind = config.get("bind_address") or "0.0.0.0"
        port = int(config.get("port"))
        family = socket.AF_INET6 if ":" in bind else socket.AF_INET
        sock = socket.socket(family, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((bind, port))
        sock.settimeout(1.0)
        self._receivers[kind] = sock
        thread = threading.Thread(
            target=self._udp_loop, args=(kind, sock), name=f"flow-{kind}", daemon=True,
        )
        self._threads.append(thread)
        thread.start()

    def _udp_loop(self, kind, sock):
        while not self._stop.is_set():
            try:
                data, addr = sock.recvfrom(MAX_PACKET_BYTES + 1)
            except socket.timeout:
                continue
            except OSError:
                break
            except Exception:
                log.exception("Penerima flow %s gagal membaca paket.", kind)
                continue
            self._handle_packet(kind, data, addr[0])

    # -------------------------------------------------
    # Pemrosesan paket
    # -------------------------------------------------
    def _bump(self, kind, key, amount=1):
        stats = self._stats.setdefault(kind, self._new_stats())
        stats[key] = stats.get(key, 0) + amount

    def _device_for_ip(self, source_ip):
        now = time.monotonic()
        if now - self._device_cache_at > DEVICE_CACHE_SECONDS:
            self._device_cache = {}
            self._device_cache_at = now
        if source_ip not in self._device_cache:
            self._device_cache[source_ip] = db.get_device_by_ip(source_ip)
        return self._device_cache[source_ip]

    def _precheck(self, kind, data, source_ip):
        self.ensure_pipeline()
        stats = self._stats.setdefault(kind, self._new_stats())
        stats["received"] += 1
        stats["last_packet_at"] = _now_iso()
        if len(data) > MAX_PACKET_BYTES:
            self._bump(kind, "dropped_size")
            return None
        device = self._device_for_ip(source_ip)
        if device is None:
            self._bump(kind, "dropped_unknown")
            return None
        if not self._rate.allow(source_ip):
            self._bump(kind, "dropped_rate")
            return None
        return device

    def _template_cache(self, exporter_id, version_kind, domain_id):
        key = (int(exporter_id), version_kind, int(domain_id or 0))
        cache = self._templates.get(key)
        now = time.monotonic()
        if cache is None or now - cache["updated"] > TEMPLATE_TTL_SECONDS:
            inner = {}
            wrapper = {"netflow": inner} if version_kind == "netflow" else {"ipfix": inner}
            cache = {"wrapper": wrapper, "inner": inner, "updated": now}
            self._templates[key] = cache
            self._trim_templates()
        else:
            cache["updated"] = now
        return cache

    def _trim_templates(self):
        if len(self._templates) <= TEMPLATE_MAX:
            return
        ordered = sorted(self._templates.items(), key=lambda item: item[1]["updated"])
        for key, _ in ordered[: len(self._templates) - TEMPLATE_MAX]:
            self._templates.pop(key, None)

    def _handle_packet(self, kind, data, source_ip):
        device = self._precheck(kind, data, source_ip)
        if device is None:
            return
        stats = self._stats[kind]
        try:
            version = get_export_version(data)
        except Exception:
            self._bump(kind, "dropped_invalid")
            return
        try:
            if version == 5:
                packet = netflow.parse_packet(data)
                domain_id = 0
                sampling_hint = _v5_sampling(packet.header)
            elif version == 9:
                if kind != "netflow_udp":
                    self._bump(kind, "dropped_invalid")
                    return
                domain_id = _v9_domain(data)
                cache = self._template_cache(device["id"], "netflow", domain_id)
                packet = netflow.parse_packet(data, cache["wrapper"])
                stats["templates"] = len(cache["inner"])
                sampling_hint = 1
            elif version == 10:
                if kind != "ipfix_udp":
                    self._bump(kind, "dropped_invalid")
                    return
                domain_id = _ipfix_domain(data)
                cache = self._template_cache(device["id"], "ipfix", domain_id)
                packet = netflow.parse_packet(data, cache["wrapper"])
                stats["templates"] = len(cache["inner"])
                sampling_hint = 1
            else:
                self._bump(kind, "dropped_invalid")
                return
        except Exception as exc:
            if "TemplateNotRecognized" in type(exc).__name__:
                self._bump(kind, "dropped_no_template")
            else:
                self._bump(kind, "dropped_invalid")
                stats["last_error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
            return

        flows_added = 0
        bytes_total = 0
        for flow in packet.flows:
            normalized = _normalize_flow(flow, sampling_hint=sampling_hint)
            if normalized is None:
                continue
            scale = normalized["sampling"]
            if scale > 1:
                normalized["bytes"] *= scale
                normalized["packets"] *= scale
            bucket_iso = db.flow.bucket_start_ts()
            exporter_id = device["id"]
            rows = [
                (exporter_id, bucket_iso, "src", normalized["src"], normalized["bytes"], normalized["packets"], 1),
                (exporter_id, bucket_iso, "dst", normalized["dst"], normalized["bytes"], normalized["packets"], 1),
                (exporter_id, bucket_iso, "pair", f"{normalized['src']}>{normalized['dst']}",
                 normalized["bytes"], normalized["packets"], 1),
                (exporter_id, bucket_iso, "app",
                 _app_name(normalized["proto"], normalized["dport"], normalized["sport"]),
                 normalized["bytes"], normalized["packets"], 1),
                (exporter_id, bucket_iso, "proto", _proto_name(normalized["proto"]),
                 normalized["bytes"], normalized["packets"], 1),
            ]
            in_label = _if_label(exporter_id, normalized["in_if"])
            if in_label:
                rows.append((exporter_id, bucket_iso, "interface", f"{in_label}:rx",
                             normalized["bytes"], normalized["packets"], 1))
            out_label = _if_label(exporter_id, normalized["out_if"])
            if out_label:
                rows.append((exporter_id, bucket_iso, "interface", f"{out_label}:tx",
                             normalized["bytes"], normalized["packets"], 1))
            for row in rows:
                self._add_bucket(*row)
            flows_added += 1
            bytes_total += normalized["bytes"]
        if flows_added:
            self._bump(kind, "accepted")
            self._bump(kind, "flows", flows_added)
            self._bump(kind, "bytes", bytes_total)

    def _add_bucket(self, exporter_id, bucket_iso, kind, key, bytes_, packets, flows):
        key = (int(exporter_id), bucket_iso, kind, str(key)[:180])
        with self._lock:
            item = self._buckets.get(key)
            if item is None:
                self._buckets[key] = [bytes_, packets, flows]
            else:
                item[0] += bytes_
                item[1] += packets
                item[2] += flows

    # -------------------------------------------------
    # Flush
    # -------------------------------------------------
    def flush_buckets(self):
        with self._lock:
            batch = self._buckets
            self._buckets = {}
        if not batch:
            return {"rows": 0}
        rows = [
            (exporter_id, bucket_ts, bucket_kind, key, values[0], values[1], values[2])
            for (exporter_id, bucket_ts, bucket_kind, key), values in batch.items()
        ]
        try:
            top_n = int(db.get_setting("flow_top_n", "100"))
        except (TypeError, ValueError):
            top_n = 100
        written = db.insert_bucket_rows(rows, top_n=top_n)
        with self._lock:
            pending = len(self._buckets)
        for stats in self._stats.values():
            stats["written_rows"] = stats.get("written_rows", 0) + written
            stats["last_flush_at"] = _now_iso()
            stats["flush_pending"] = pending
        return {"rows": written}

    def status(self):
        with self._lock:
            result = {}
            for kind, stats in self._stats.items():
                result[kind] = dict(stats)
            result["templates"] = {
                f"{exporter_id}:{version_kind}:{domain}": len(cache["inner"])
                for (exporter_id, version_kind, domain), cache in self._templates.items()
            }
            result["bucket"] = {"pending": len(self._buckets)}
            return result

    def hourly_rollup(self):
        return db.flow.rollup_hourly_buckets()

    def process_flow_datagram(self, data, source_ip, kind="netflow_udp"):
        """API uji: proses satu paket tanpa socket."""
        self._handle_packet(kind, data, source_ip)


def _v5_sampling(header):
    """Bit sampling interval v5: 14 bit interval, 2 bit mode. >1 berarti sampel."""
    try:
        raw = int(getattr(header, "sampling_interval", 0) or 0)
    except (TypeError, ValueError):
        return 1
    return max(1, raw & 0x3FFF)


def _v9_domain(data):
    import struct

    try:
        return struct.unpack("!I", data[16:20])[0]
    except Exception:
        return 0


def _ipfix_domain(data):
    import struct

    try:
        return struct.unpack("!I", data[12:16])[0]
    except Exception:
        return 0


FLOW = FlowManager()
