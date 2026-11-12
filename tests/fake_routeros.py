"""Server RouterOS API tiruan untuk tests/ (tidak pernah dipakai di produksi).

Mengimplementasikan subset protokol biner RouterOS API yang dibutuhkan
collector: handshake login (cara pasca-6.43) dan balasan sentence untuk
perintah print yang kita kirim. Semua data adalah contoh statis.
"""

import socket
import threading

from librouteros.protocol import encode_sentence, determine_length, decode_length


def read_sentence(conn):
    words = []
    while True:
        byte = conn.recv(1)
        if not byte:
            return None
        if byte == b"\x00":
            return words
        to_read = determine_length(byte)
        data = byte + (conn.recv(to_read) if to_read else b"")
        length = decode_length(data)
        word = b""
        while len(word) < length:
            chunk = conn.recv(length - len(word))
            if not chunk:
                return None
            word += chunk
        words.append(word.decode("utf-8", errors="ignore"))


def encode_reply(*words):
    return encode_sentence(*words, encoding="utf-8")


def attrs(**kwargs):
    return [f"={key}={value}" for key, value in kwargs.items()]


DEFAULT_ROWS = {
    "/system/identity/print": [
        attrs(name="fake-router"),
    ],
    "/system/resource/print": [
        attrs(**{
            "cpu-load": 23,
            "cpu-count": 4,
            "cpu-frequency": 1400,
            "free-memory": 512000000,
            "total-memory": 1000000000,
            "free-hdd-space": 100000000000,
            "total-hdd-space": 128000000000,
            "uptime": "3w2d05:06:07",
            "version": "7.12.1",
            "board-name": "RB4011iGS+",
            "architecture-name": "arm",
        }),
    ],
    "/system/health/print": [  # format v7: baris name/value
        attrs(name="temperature", value="46.5"),
        attrs(name="cpu-temperature", value="52.0"),
        attrs(name="voltage", value="12.6"),
    ],
    "/system/routerboard/print": [
        attrs(model="RB4011iGS+", **{"serial-number": "SNFAKE123", "current-firmware": "7.12.1"}),
    ],
    "/interface/print": [
        attrs(**{".id": "*1", "name": "ether1", "type": "ether", "mtu": 1500,
                 "mac-address": "AA:BB:CC:00:00:01", "running": "true", "disabled": "false",
                 "rx-byte": 1000000000, "tx-byte": 500000000}),
        attrs(**{".id": "*2", "name": "ether2", "type": "ether", "mtu": 1500,
                 "mac-address": "AA:BB:CC:00:00:02", "running": "false", "disabled": "true",
                 "rx-byte": 0, "tx-byte": 0}),
    ],
}


# Data contoh untuk pengujian modul M1-M5 (bukan dipakai produksi).
MODULE_ROWS = {
    "/ip/address/print": [
        attrs(**{".id": "*10", "address": "192.168.30.1/24", "network": "192.168.30.0",
                 "interface": "ether1", "disabled": "false", "dynamic": "false"}),
        attrs(**{".id": "*11", "address": "10.10.10.1/24", "network": "10.10.10.0",
                 "interface": "bridge", "disabled": "false", "dynamic": "false"}),
    ],
    "/system/clock/print": [
        attrs(**{".id": "*20", "time": "07:00:00", "date": "2026-11-10",
                 "time-zone-name": "Asia/Jakarta", "gmt-offset": "+07:00"}),
    ],
    "/ppp/active/print": [
        attrs(**{".id": "*30", "name": "pelanggan1", "service": "pppoe",
                 "caller-id": "AA:BB:CC:DD:EE:01", "address": "10.10.10.10",
                 "uptime": "1h2m3s", "bytes-in": 1000, "bytes-out": 2000}),
        attrs(**{".id": "*31", "name": "pelanggan2", "service": "pppoe",
                 "caller-id": "AA:BB:CC:DD:EE:02", "address": "10.10.10.11",
                 "uptime": "2h3m4s", "bytes-in": 3000, "bytes-out": 4000}),
    ],
    "/ip/hotspot/active/print": [
        attrs(**{".id": "*32", "user": "hotspot1", "address": "172.16.0.10",
                 "mac-address": "AA:BB:CC:DD:EE:10", "uptime": "10m",
                 "bytes-in": 500, "bytes-out": 600}),
    ],
    "/interface/wireless/registration-table/print": [
        attrs(**{".id": "*40", "interface": "wlan1", "mac-address": "AA:BB:CC:DD:EE:20",
                 "signal-strength": "-61dBm", "tx-rate": "300Mbps", "rx-rate": "300Mbps",
                 "uptime": "1h", "bytes-in": 100, "bytes-out": 200}),
    ],
    "/log/print": [
        attrs(**{".id": "*50", "time": "07:01:00", "topics": "system,info",
                 "message": "router rebooted"}),
        attrs(**{".id": "*51", "time": "07:02:00", "topics": "system,error,critical",
                 "message": "login failure for user admin from 192.168.30.99 via winbox"}),
        attrs(**{".id": "*52", "time": "07:02:30", "topics": "system,error,critical",
                 "message": "login failure for user admin from 192.168.30.99 via winbox"}),
        attrs(**{".id": "*53", "time": "07:03:00", "topics": "system,warning",
                 "message": "dhcp lease pool exhausted"}),
    ],
    "/ip/dhcp-server/lease/print": [
        attrs(**{".id": "*60", "address": "192.168.30.100", "mac-address": "AA:BB:CC:DD:EE:30",
                 "host-name": "laptop-1", "status": "bound", "server": "dhcp1",
                 "comment": "", "dynamic": "true", "disabled": "false"}),
        attrs(**{".id": "*61", "address": "192.168.30.101", "mac-address": "AA:BB:CC:DD:EE:31",
                 "host-name": "laptop-2", "status": "bound", "server": "dhcp1",
                 "comment": "", "dynamic": "true", "disabled": "false"}),
    ],
    "/ip/pool/print": [
        attrs(**{".id": "*62", "name": "pool-dhcp", "ranges": "192.168.30.100-192.168.30.110"}),
    ],
    "/ip/dhcp-server/print": [
        attrs(**{".id": "*63", "name": "dhcp1", "address-pool": "pool-dhcp",
                 "interface": "bridge", "disabled": "false"}),
    ],
    "/ip/arp/print": [
        attrs(**{".id": "*64", "address": "192.168.30.100", "mac-address": "AA:BB:CC:DD:EE:30",
                 "interface": "bridge", "complete": "true", "dynamic": "true",
                 "disabled": "false", "invalid": "false"}),
    ],
    "/ip/neighbor/print": [
        attrs(**{".id": "*65", "identity": "fake-router", "platform": "MikroTik",
                 "version": "7.12.1", "interface": "ether1", "address": "192.168.30.1",
                 "mac-address": "AA:BB:CC:00:00:01", "board": "RB4011"}),
        attrs(**{".id": "*66", "identity": "tetangga-switch", "platform": "MikroTik",
                 "version": "6.49.7", "interface": "ether1", "address": "192.168.30.2",
                 "mac-address": "AA:BB:CC:00:00:03", "board": "CRS326"}),
    ],
    "/ip/route/print": [
        attrs(**{".id": "*70", "dst-address": "0.0.0.0/0", "gateway": "192.168.30.254",
                 "distance": 1, "active": "true", "dynamic": "false", "static": "true",
                 "connect": "false", "disabled": "false", "scope": "30", "target-scope": "10"}),
        attrs(**{".id": "*71", "dst-address": "192.168.30.0/24", "gateway": "bridge",
                 "distance": 0, "active": "true", "dynamic": "false", "static": "false",
                 "connect": "true", "disabled": "false"}),
    ],
    "/ipv6/route/print": [
        attrs(**{".id": "*72", "dst-address": "::/0", "gateway": "fe80::1",
                 "distance": 1, "active": "true", "dynamic": "false", "disabled": "false"}),
    ],
    "/routing/bgp/session/print": [
        attrs(**{".id": "*80", "name": "peer-isp", "remote.address": "10.0.0.1",
                 "remote.as": "64512", "state": "established", "uptime": "1d",
                 "prefix-count": 100, "disabled": "false"}),
    ],
    "/routing/ospf/neighbor/print": [
        attrs(**{".id": "*81", "router-id": "10.0.0.2", "address": "10.0.0.2",
                 "state": "Full", "interface": "ether1", "priority": 1, "dead-time": "40s"}),
    ],
    "/ip/firewall/filter/print": [
        attrs(**{".id": "*90", "chain": "input", "action": "accept", "comment": "mgmt",
                 "packets": 10, "bytes": 1000, "disabled": "false"}),
    ],
    "/ip/firewall/nat/print": [
        attrs(**{".id": "*91", "chain": "srcnat", "action": "masquerade", "comment": "nat",
                 "packets": 20, "bytes": 2000, "disabled": "false"}),
    ],
    "/ip/firewall/mangle/print": [],
    "/ip/firewall/connection/print": [],
    "/ip/firewall/address-list/print": [
        attrs(**{".id": "*92", "list": "blocked", "address": "203.0.113.9",
                 "timeout": "1h", "comment": "uji", "disabled": "false", "dynamic": "false"}),
    ],
    "/queue/simple/print": [
        attrs(**{".id": "*93", "name": "q1", "target": "10.10.10.10/32",
                 "max-limit": "10M/10M", "rate": "1M/1M", "bytes": 100, "packets": 10,
                 "dropped": 0, "disabled": "false"}),
    ],
    "/queue/tree/print": [],
    "/ip/service/print": [
        attrs(**{".id": "*100", "name": "telnet", "port": 23, "address": "",
                 "disabled": "false", "invalid": "false"}),
        attrs(**{".id": "*101", "name": "api", "port": 8728, "address": "",
                 "disabled": "false", "invalid": "false"}),
        attrs(**{".id": "*102", "name": "www", "port": 80, "address": "192.168.30.0/24",
                 "disabled": "true", "invalid": "false"}),
        attrs(**{".id": "*103", "name": "ssh", "port": 22, "address": "192.168.30.0/24",
                 "disabled": "false", "invalid": "false"}),
    ],
    "/user/print": [
        attrs(**{".id": "*110", "name": "admin", "group": "full", "address": "",
                 "last-logged-in": "2026-11-10 06:00:00", "disabled": "false"}),
        attrs(**{".id": "*111", "name": "operator", "group": "read", "address": "192.168.30.0/24",
                 "last-logged-in": "never", "disabled": "false"}),
    ],
    "/ip/dns/print": [
        attrs(**{".id": "*120", "allow-remote-requests": "true",
                 "servers": "1.1.1.1", "dynamic-servers": ""}),
    ],
    "/system/ntp/client/print": [
        attrs(**{".id": "*121", "enabled": "false", "mode": "unicast",
                 "servers": "pool.ntp.org"}),
    ],
    "/system/package/print": [
        attrs(**{".id": "*130", "name": "routeros", "version": "7.12.1",
                 "disabled": "false", "size": 100}),
        attrs(**{".id": "*131", "name": "wireless", "version": "7.12.1",
                 "disabled": "false", "size": 50}),
    ],
    "/system/routerboard/print": [
        attrs(**{".id": "*140", "model": "RB4011iGS+", "serial-number": "SNFAKE123",
                 "current-firmware": "6.49.7", "upgrade-firmware": "7.12.1",
                 "factory-firmware": "6.45", "routerboard": "true"}),
    ],
    "/system/license/print": [
        attrs(**{".id": "*150", "nlevel": "6", "level": "6", "features": "",
                 "expires-at": ""}),
    ],
}


def module_rows(**overrides):
    """Gabungan data dasar + data modul untuk tests/."""
    rows = dict(DEFAULT_ROWS)
    rows.update(MODULE_ROWS)
    rows.update(overrides)
    return rows


class FakeRouterOS:
    """Jalankan server tiruan di thread latar; beri port nyata ke test."""

    def __init__(self, rows=None, user="api", password="secret", trap_health=False,
                 health_v6=False, wrong_password_trap="invalid user name or password",
                 traps=None, permissions=None, counts=None):
        self.rows = rows or DEFAULT_ROWS
        self.user = user
        self.password = password
        self.trap_health = trap_health
        self.health_v6 = health_v6
        self.wrong_password_trap = wrong_password_trap
        self.traps = dict(traps or {})
        self.permissions = dict(permissions or {})
        self.counts = dict(counts or {})
        self._sock = None
        self._thread = None
        self.port = None
        self.connections = 0
        self.commands = []

    def start(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(4)
        self.port = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        return self.port

    def _serve(self):
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            self.connections += 1
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        try:
            while True:
                sentence = read_sentence(conn)
                if sentence is None:
                    return
                if not sentence:
                    continue
                command, words = sentence[0], sentence[1:]
                self.commands.append(command)
                params = {}
                for word in words:
                    if word.startswith("="):
                        parts = word.split("=", 2)
                        if len(parts) == 3:
                            params[parts[1]] = parts[2]
                    elif word.startswith("?"):
                        parts = word[1:].split("=", 1)
                        if len(parts) == 2:
                            params[parts[0]] = parts[1]

                if command == "/login":
                    if not words:
                        # Login cara lama (token) — jawab dengan challenge palsu.
                        conn.sendall(encode_reply("!done", "=ret=00112233445566778899aabbccddeeff"))
                        continue
                    if params.get("name") == self.user and params.get("password") == self.password:
                        conn.sendall(encode_reply("!done"))
                    else:
                        conn.sendall(encode_reply("!trap", f"=message={self.wrong_password_trap}"))
                        conn.sendall(encode_reply("!done"))
                    continue

                if command == "/system/health/print" and self.trap_health:
                    conn.sendall(encode_reply("!trap", "=message=health not available"))
                    conn.sendall(encode_reply("!done"))
                    continue

                if command in self.traps:
                    conn.sendall(encode_reply("!trap", f"=message={self.traps[command]}"))
                    conn.sendall(encode_reply("!done"))
                    continue

                if command in self.permissions:
                    conn.sendall(encode_reply("!trap", f"=message={self.permissions[command]}"))
                    conn.sendall(encode_reply("!done"))
                    continue

                if command.endswith("/print") and any(
                    word == "=count-only=" or word.lstrip("=").split("=", 1)[0] == "count-only"
                    for word in words
                ):
                    count = self.counts.get(command, len(self.rows.get(command) or []))
                    conn.sendall(encode_reply("!done", f"=ret={count}"))
                    continue

                if command == "/interface/ethernet/monitor":
                    # Kunci "/interface/ethernet/monitor:<nama>" untuk balasan per port.
                    monitor_key = f"/interface/ethernet/monitor:{params.get('name')}"
                    rows = list(self.rows.get(monitor_key) or self.rows.get(command) or [])
                    for row in rows:
                        conn.sendall(encode_reply("!re", *row))
                    conn.sendall(encode_reply("!done"))
                    continue

                rows = list(self.rows.get(command) or [])
                if command == "/system/health/print" and self.health_v6:
                    rows = [attrs(temperature="46.5", **{"cpu-temperature": "52.0", "voltage": "12.6"})]

                for row in rows:
                    conn.sendall(encode_reply("!re", *row))
                conn.sendall(encode_reply("!done"))
        except (ConnectionError, OSError):
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def stop(self):
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock = None
