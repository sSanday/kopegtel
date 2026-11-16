"""Penerima UDP/TCP berantrean berbatas untuk syslog dan SNMP trap.

Prinsip:
- Port default MATI; bind error tidak menjatuhkan aplikasi.
- Hanya IP perangkat terdaftar yang diterima.
- Batas laju per sumber, batas ukuran paket, antrean berbatas.
- Insert dilakukan batch oleh thread penulis, bukan satu baris per paket.
- Semua penghitung (diterima/dibuang) tersedia untuk UI status.
"""

import collections
import logging
import queue
import socket
import threading
import time
from datetime import datetime, timezone

import db
from . import parsers, rules, traps

log = logging.getLogger("nms.ingest")

MAX_PACKET_BYTES = 65535
DEVICE_CACHE_SECONDS = 10
FLOOD_COOLDOWN_SECONDS = 300


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class BoundedBatcher:
    """Antrean berbatas + penulis batch. Tidak pernah memblokir penerima."""

    def __init__(self, name, insert_fn, size=10000, batch_size=200, flush_seconds=1.0):
        self.name = name
        self.insert_fn = insert_fn
        self.batch_size = max(1, int(batch_size))
        self.flush_seconds = max(0.1, float(flush_seconds))
        self.queue = queue.Queue(maxsize=max(1, int(size)))
        self._stop = threading.Event()
        self._thread = None
        self._lock = threading.Lock()
        self.received = 0
        self.written = 0
        self.dropped = 0
        self.errors = 0
        self.batches = 0
        self.last_write_at = None

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=f"ingest-{self.name}", daemon=True)
        self._thread.start()

    def stop(self, timeout=3.0):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        self._flush()

    def put(self, row):
        with self._lock:
            self.received += 1
        try:
            self.queue.put_nowait(row)
            return True
        except queue.Full:
            with self._lock:
                self.dropped += 1
            return False

    def _flush(self):
        rows = []
        while True:
            try:
                rows.append(self.queue.get_nowait())
            except queue.Empty:
                break
        if not rows:
            return
        try:
            self.insert_fn(rows)
            with self._lock:
                self.written += len(rows)
                self.batches += 1
                self.last_write_at = _now_iso()
        except Exception:
            with self._lock:
                self.errors += len(rows)
            log.exception("Batch insert %s gagal (%d baris dibuang).", self.name, len(rows))

    def _run(self):
        pending = []
        deadline = time.monotonic() + self.flush_seconds
        while not self._stop.is_set():
            timeout = max(0.05, deadline - time.monotonic())
            try:
                pending.append(self.queue.get(timeout=timeout))
            except queue.Empty:
                pass
            now = time.monotonic()
            if len(pending) >= self.batch_size or now >= deadline:
                self._insert(pending)
                pending = []
                deadline = now + self.flush_seconds
        self._insert(pending)

    def _insert(self, rows):
        if not rows:
            return
        try:
            self.insert_fn(rows)
            with self._lock:
                self.written += len(rows)
                self.batches += 1
                self.last_write_at = _now_iso()
        except Exception:
            with self._lock:
                self.errors += len(rows)
            log.exception("Batch insert %s gagal (%d baris dibuang).", self.name, len(rows))

    def stats(self):
        with self._lock:
            return {
                "received": self.received, "written": self.written,
                "dropped": self.dropped, "errors": self.errors, "batches": self.batches,
                "queue_size": self.queue.qsize(), "last_write_at": self.last_write_at,
            }


class RateLimiter:
    """Sliding window per sumber: ``limit`` kejadian per ``window_seconds``.

    Dict sumber kedaluwarsa dibersihkan berkala (setiap ``sweep_every``
    panggilan dan saat penuh) agar sumber yang tidak pernah kembali tidak
    menumpuk selamanya.
    """

    MAX_SOURCES = 4096
    SWEEP_EVERY = 512

    def __init__(self, limit=200, window_seconds=10, max_sources=None):
        self.limit = max(1, int(limit))
        self.window = max(1, int(window_seconds))
        self._events = {}
        self._lock = threading.Lock()
        self._max_sources = max(1, int(max_sources or self.MAX_SOURCES))
        self._since_sweep = 0

    def _drop_expired(self, cutoff):
        for source, bucket in list(self._events.items()):
            if not bucket or bucket[-1] < cutoff:
                self._events.pop(source, None)

    def allow(self, source, now=None):
        current = now or time.monotonic()
        cutoff = current - self.window
        with self._lock:
            self._since_sweep += 1
            if self._since_sweep >= self.SWEEP_EVERY:
                self._since_sweep = 0
                self._drop_expired(cutoff)
            bucket = self._events.get(source)
            if bucket is None:
                if len(self._events) >= self._max_sources:
                    self._drop_expired(cutoff)
                    if len(self._events) >= self._max_sources:
                        # Penuh sumber aktif: tolak sumber baru (fail-safe).
                        return False
                bucket = self._events[source] = collections.deque()
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            if len(bucket) >= self.limit:
                allowed = False
            else:
                bucket.append(current)
                allowed = True
            if not bucket:
                self._events.pop(source, None)
            return allowed

    def reset(self):
        with self._lock:
            self._events.clear()


class IngestManager:
    def __init__(self):
        self._lock = threading.RLock()
        self._receivers = {}
        self._threads = []
        self._stop = threading.Event()
        self._syslog_batcher = None
        self._trap_batcher = None
        self._rate = None
        self._device_cache = {}
        self._device_cache_at = 0.0
        self._stats = {}
        self._last_flood = {}
        self._rate_events = collections.defaultdict(collections.deque)
        self._started = False
        self._configs = {}

    # -----------------------------------------------------
    # Konfigurasi & siklus hidup
    # -----------------------------------------------------
    def _new_stats(self):
        return {
            "received": 0, "accepted": 0, "dropped_unknown": 0, "dropped_rate": 0,
            "dropped_auth": 0, "dropped_invalid": 0, "dropped_queue": 0,
            "last_packet_at": None, "last_error": None, "running": False,
            "started_at": None, "port": None, "bind_address": None,
        }

    def reload(self):
        """Baca konfigurasi DB dan (re)start penerima. Aman dipanggil kapan saja."""
        with self._lock:
            self.stop()
            self._stop.clear()
            settings = db.get_settings()
            try:
                rate_per_sec = int(settings.get("ingest_rate_limit_per_sec", 200))
                rate_window = int(settings.get("ingest_rate_window_seconds", 10))
            except (TypeError, ValueError):
                rate_per_sec, rate_window = 200, 10
            self._rate = RateLimiter(
                limit=rate_per_sec * max(1, rate_window),
                window_seconds=max(1, rate_window),
            )
            self._syslog_batcher = BoundedBatcher(
                "syslog", db.add_syslog_events_batch,
                size=settings.get("ingest_queue_size", 10000),
                batch_size=settings.get("ingest_batch_size", 200),
                flush_seconds=settings.get("ingest_flush_seconds", 1),
            )
            self._trap_batcher = BoundedBatcher(
                "trap", db.add_snmp_traps_batch,
                size=settings.get("ingest_queue_size", 10000),
                batch_size=settings.get("ingest_batch_size", 200),
                flush_seconds=settings.get("ingest_flush_seconds", 1),
            )
            self._stats = {kind: self._new_stats() for kind in ("syslog_udp", "syslog_tcp", "snmp_trap")}
            self._rate_events.clear()
            self._configs = {}
            for kind in ("syslog_udp", "syslog_tcp", "snmp_trap"):
                config = db.get_ingest_receiver(kind, include_secret=True) or {}
                self._configs[kind] = config
                if not config.get("enabled"):
                    continue
                self._stats[kind]["running"] = True
                self._stats[kind]["port"] = config.get("port")
                self._stats[kind]["bind_address"] = config.get("bind_address")
                self._stats[kind]["started_at"] = _now_iso()
                try:
                    if kind == "syslog_udp":
                        self._start_udp(kind, config, self._handle_syslog)
                    elif kind == "snmp_trap":
                        self._start_udp(kind, config, self._handle_trap)
                    else:
                        self._start_tcp(kind, config)
                except Exception as exc:
                    self._stats[kind]["running"] = False
                    self._stats[kind]["last_error"] = f"bind gagal: {exc}"
                    log.error("Penerima %s gagal bind %s:%s — %s", kind, config.get("bind_address"),
                              config.get("port"), exc)
            if self._stats.get("syslog_udp", {}).get("running") or self._stats.get("syslog_tcp", {}).get("running"):
                self._syslog_batcher.start()
            if self._stats.get("snmp_trap", {}).get("running"):
                self._trap_batcher.start()
            self._started = True
            return self.status()

    def stop(self):
        self._stop.set()
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
        for batcher in (self._syslog_batcher, self._trap_batcher):
            if batcher is not None:
                try:
                    batcher.stop()
                except Exception:
                    log.debug("Penghentian batcher gagal.", exc_info=True)
        self._syslog_batcher = None
        self._trap_batcher = None
        for stats in self._stats.values():
            stats["running"] = False
        self._started = False

    def start(self):
        if not self._started:
            self.reload()

    # -----------------------------------------------------
    # Penerima UDP / TCP
    # -----------------------------------------------------
    def _start_udp(self, kind, config, handler):
        bind = config.get("bind_address") or "0.0.0.0"
        port = int(config.get("port"))
        family = socket.AF_INET6 if ":" in bind else socket.AF_INET
        sock = socket.socket(family, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((bind, port))
        sock.settimeout(1.0)
        self._receivers[kind] = sock
        thread = threading.Thread(
            target=self._udp_loop, args=(kind, sock, handler),
            name=f"ingest-{kind}", daemon=True,
        )
        self._threads.append(thread)
        thread.start()

    def _udp_loop(self, kind, sock, handler):
        while not self._stop.is_set():
            try:
                data, addr = sock.recvfrom(MAX_PACKET_BYTES + 1)
            except socket.timeout:
                continue
            except OSError:
                break
            except Exception:
                log.exception("Penerima %s gagal membaca paket.", kind)
                continue
            source_ip = addr[0]
            handler(kind, data, source_ip, sock, addr)

    def _start_tcp(self, kind, config):
        bind = config.get("bind_address") or "0.0.0.0"
        port = int(config.get("port"))
        family = socket.AF_INET6 if ":" in bind else socket.AF_INET
        server = socket.socket(family, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((bind, port))
        server.listen(20)
        server.settimeout(1.0)
        self._receivers[kind] = server
        thread = threading.Thread(target=self._tcp_loop, args=(kind, server), name=f"ingest-{kind}", daemon=True)
        self._threads.append(thread)
        thread.start()

    def _tcp_loop(self, kind, server):
        while not self._stop.is_set():
            try:
                conn, addr = server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            except Exception:
                log.exception("Penerima TCP %s gagal accept.", kind)
                continue
            threading.Thread(
                target=self._tcp_client, args=(kind, conn, addr[0]),
                name=f"ingest-tcp-client", daemon=True,
            ).start()

    def _tcp_client(self, kind, conn, source_ip):
        buffer = b""
        conn.settimeout(30.0)
        try:
            while not self._stop.is_set():
                chunk = conn.recv(4096)
                if not chunk:
                    break
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    if line.strip():
                        self._handle_syslog(kind, line[:MAX_PACKET_BYTES], source_ip, conn, None)
                if len(buffer) > MAX_PACKET_BYTES:
                    self._bump(kind, "dropped_invalid")
                    buffer = b""
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

    # -----------------------------------------------------
    # Pemrosesan paket
    # -----------------------------------------------------
    def _bump(self, kind, key):
        stats = self._stats.setdefault(kind, self._new_stats())
        stats[key] = stats.get(key, 0) + 1

    def _mark_rate(self, kind):
        with self._lock:
            events = self._rate_events[kind]
            now = time.monotonic()
            events.append(now)
            cutoff = now - 10
            while events and events[0] < cutoff:
                events.popleft()

    def _rate_per_second(self, kind):
        with self._lock:
            events = self._rate_events.get(kind)
            if not events:
                return 0.0
            now = time.monotonic()
            cutoff = now - 10
            count = sum(1 for item in events if item >= cutoff)
            return round(count / 10, 1)

    def ensure_batchers(self):
        """Siapkan batcher tanpa membuka port (dipakai tes/API proses langsung)."""
        if self._syslog_batcher is None:
            settings = db.get_settings()
            self._syslog_batcher = BoundedBatcher(
                "syslog", db.add_syslog_events_batch,
                size=settings.get("ingest_queue_size", 10000),
                batch_size=settings.get("ingest_batch_size", 200),
                flush_seconds=settings.get("ingest_flush_seconds", 1),
            )
        if self._trap_batcher is None:
            settings = db.get_settings()
            self._trap_batcher = BoundedBatcher(
                "trap", db.add_snmp_traps_batch,
                size=settings.get("ingest_queue_size", 10000),
                batch_size=settings.get("ingest_batch_size", 200),
                flush_seconds=settings.get("ingest_flush_seconds", 1),
            )
        if self._rate is None:
            settings = db.get_settings()
            try:
                rate_per_sec = int(settings.get("ingest_rate_limit_per_sec", 200))
                rate_window = int(settings.get("ingest_rate_window_seconds", 10))
            except (TypeError, ValueError):
                rate_per_sec, rate_window = 200, 10
            self._rate = RateLimiter(
                limit=rate_per_sec * max(1, rate_window),
                window_seconds=max(1, rate_window),
            )

    def _device_for_ip(self, source_ip):
        now = time.monotonic()
        if now - self._device_cache_at > DEVICE_CACHE_SECONDS:
            self._device_cache = {}
            self._device_cache_at = now
        if source_ip not in self._device_cache:
            self._device_cache[source_ip] = db.get_device_by_ip(source_ip)
        return self._device_cache[source_ip]

    def _precheck(self, kind, data, source_ip):
        self.ensure_batchers()
        stats = self._stats.setdefault(kind, self._new_stats())
        stats["received"] += 1
        stats["last_packet_at"] = _now_iso()
        if len(data) > MAX_PACKET_BYTES:
            self._bump(kind, "dropped_invalid")
            return None
        device = self._device_for_ip(source_ip)
        if device is None:
            self._bump(kind, "dropped_unknown")
            return None
        if not self._rate.allow(source_ip):
            self._bump(kind, "dropped_rate")
            self._check_flood(device, source_ip)
            return None
        return device

    def _handle_syslog(self, kind, data, source_ip, sock=None, addr=None):
        device = self._precheck(kind, data, source_ip)
        if device is None:
            return
        parsed = parsers.parse_syslog(data)
        if parsed is None:
            self._bump(kind, "dropped_invalid")
            return
        row = dict(parsed)
        row.update({
            "device_id": device["id"], "source_ip": source_ip,
            "received_at": _now_iso(),
        })
        row["dedup_hash"] = db.dedup_hash(device["id"], row["ts"], row["message"])
        self.ensure_batchers()
        if not self._syslog_batcher.put(row):
            self._bump(kind, "dropped_queue")
            return
        self._bump(kind, "accepted")
        self._mark_rate(kind)
        try:
            rules.evaluate_syslog(device, parsed)
        except Exception:
            log.exception("Evaluasi aturan syslog gagal (paket tetap disimpan).")

    def _handle_trap(self, kind, data, source_ip, sock=None, addr=None):
        device = self._precheck(kind, data, source_ip)
        if device is None:
            return
        try:
            decoded = traps.decode_trap(data)
        except traps.TrapDecodeError:
            self._bump(kind, "dropped_invalid")
            return
        config = self._configs.get(kind) or {}
        communities = set(config.get("config", {}).get("communities") or [])
        if decoded.get("version") == "v1" and not config.get("config", {}).get("allow_v1"):
            self._bump(kind, "dropped_auth")
            return
        if communities and decoded.get("community") not in communities:
            self._bump(kind, "dropped_auth")
            return
        if not communities:
            self._bump(kind, "dropped_auth")
            return
        row = {
            "ts": _now_iso(), "received_at": _now_iso(), "device_id": device["id"],
            "source_ip": source_ip, "trap_oid": decoded["trap_oid"],
            "trap_name": decoded.get("trap_name"),
            "varbinds": traps.varbinds_json(decoded.get("varbinds") or []),
            "raw": "",
        }
        row["dedup_hash"] = db.dedup_hash(device["id"], row["ts"], row["varbinds"])
        self.ensure_batchers()
        if not self._trap_batcher.put(row):
            self._bump(kind, "dropped_queue")
            return
        self._bump(kind, "accepted")
        self._mark_rate(kind)
        if decoded.get("is_inform") and sock is not None and addr is not None:
            try:
                response = traps.build_inform_response(decoded)
                if response:
                    sock.sendto(response, addr)
            except Exception:
                log.debug("Balasan inform gagal dikirim.", exc_info=True)
        try:
            rules.evaluate_trap(device, decoded)
        except Exception:
            log.exception("Evaluasi aturan trap gagal (paket tetap disimpan).")

    # -----------------------------------------------------
    # Banjir & status
    # -----------------------------------------------------
    def _check_flood(self, device, source_ip):
        now = time.monotonic()
        last = self._last_flood.get(device["id"], 0)
        if now - last < FLOOD_COOLDOWN_SECONDS:
            return
        self._last_flood[device["id"]] = now
        try:
            message = (
                f"{device['name']}: laju kejadian melewati batas "
                f"({self._rate.limit} pesan/{self._rate.window} dtk); sebagian pesan dibuang."
            )
            problem_id, is_new = db.open_problem(device["id"], "ingest_flood", "warning", message)
            if is_new:
                import alerts

                alerts.dispatch_problem({
                    "id": problem_id, "device_id": device["id"], "kind": "ingest_flood",
                    "severity": "warning", "message": message, "started_at": _now_iso(),
                })
        except Exception:
            log.exception("Gagal membuka problem ingest_flood untuk %s.", source_ip)

    def status(self):
        with self._lock:
            result = {}
            for kind, stats in self._stats.items():
                item = dict(stats)
                batcher = self._trap_batcher if kind == "snmp_trap" else self._syslog_batcher
                item["queue"] = batcher.stats() if batcher else {}
                item["rate_per_sec"] = self._rate_per_second(kind)
                result[kind] = item
            return result

    def flush_now(self):
        """Tulis antrean segera (dipakai tes/CLI)."""
        self.ensure_batchers()
        self._syslog_batcher._flush()
        self._trap_batcher._flush()

    def process_syslog_datagram(self, data, source_ip, kind="syslog_udp"):
        """API uji: proses satu paket tanpa socket."""
        self._handle_syslog(kind, data, source_ip)

    def process_trap_datagram(self, data, source_ip, kind="snmp_trap"):
        self._handle_trap(kind, data, source_ip)


INGEST = IngestManager()
