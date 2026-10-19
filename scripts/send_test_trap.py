#!/usr/bin/env python3
"""Kirim SNMP trap v2c/v1 ke NMS untuk menguji receiver (UDP, tanpa pysnmp).

Contoh persis alur uji:
    python scripts/send_test_trap.py --host 127.0.0.1 --port 1162 \
        --community kopegtel --type linkdown --ifindex 3

Tipe yang tersedia: linkdown | linkup | coldstart | warmstart.

Trap masuk dicatat di:
  - halaman Logs (baris system_logs dengan event_type TRAP_LINKDOWN dll.)
  - tabel snmp_traps (bisa dilihat via GET /api/traps)
  - GTL/NMS alert (Telegram/email/webhook) untuk linkdown & coldstart,
    dengan dedupe 60 detik per sumber.
"""

import argparse
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nms.snmp_trap import SNMP_TRAP_PORT_MAX, SNMP_TRAP_PORT_MIN, TRAP_TYPES


def _mask(value):
    """Sembunyikan isi community pada output agar aman dibagikan."""
    if not value:
        return "(kosong)"
    if len(value) <= 2:
        return "**"
    return value[:2] + "***"


def main():
    p = argparse.ArgumentParser(description="Kirim satu SNMP trap uji ke receiver NMS.")
    p.add_argument("--host", required=True, help="IP/hostname NMS receiver")
    p.add_argument(
        "--port", type=int, default=1162, help="port receiver (default 1162)"
    )
    p.add_argument("--community", default="public", help="community v1/v2c")
    p.add_argument(
        "--type",
        dest="trap_type",
        default="linkdown",
        choices=sorted(TRAP_TYPES.keys()),
        help="jenis trap",
    )
    p.add_argument(
        "--ifindex", type=int, default=3, help="ifIndex yang dites (default 3)"
    )
    p.add_argument(
        "--version",
        dest="version",
        default="2c",
        choices=("1", "2c"),
        help="versi SNMP (default 2c)",
    )
    p.add_argument("--source-ip", default="127.0.0.1", help="agent-addr untuk trap v1")
    p.add_argument("--description", default=None, help="teks bebas sebagai varbind")
    p.add_argument("--specific", type=int, default=0, help="specific-trap untuk v1")
    p.add_argument(
        "--enterprise",
        default="1.3.6.1.4.1.8072.9999",
        help="enterprise OID untuk v1",
    )
    args = p.parse_args()

    if not SNMP_TRAP_PORT_MIN <= args.port <= SNMP_TRAP_PORT_MAX:
        p.error(f"--port harus {SNMP_TRAP_PORT_MIN}-{SNMP_TRAP_PORT_MAX}")

    suffix, v1_generic = TRAP_TYPES[args.trap_type]
    if_oper = (
        2
        if args.trap_type == "linkdown"
        else (1 if args.trap_type == "linkup" else None)
    )

    from nms.snmp_trap import (
        OID_SNMPTRAP_BASE,
        build_v1_trap,
        build_v2c_trap,
    )

    if args.version == "2c":
        pkt = build_v2c_trap(
            args.community,
            trap_oid=OID_SNMPTRAP_BASE + str(suffix),
            if_index=args.ifindex,
            if_oper=if_oper,
            description=args.description,
        )
    else:
        pkt = build_v1_trap(
            args.community,
            generic=v1_generic,
            specific=args.specific,
            enterprise_oid=args.enterprise,
            agent_ip=args.source_ip,
            if_index=args.ifindex,
            if_oper=if_oper,
            description=args.description,
        )

    dst = (args.host, args.port)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(5.0)
    try:
        s.sendto(pkt, dst)
        # Beri waktu receiver memproses; UDP tidak punya ACK, jadi hanya info.
        time.sleep(0.3)
    except OSError as e:
        print(f"GAGAL kirim ke {dst[0]}:{dst[1]}: {e}")
        return 2
    finally:
        s.close()

    print(
        f"OK  trap {args.trap_type} (v{args.version}) {len(pkt)} byte "
        f"-> {dst[0]}:{dst[1]} | ifIndex {args.ifindex} | "
        f"community {_mask(args.community)}"
    )
    print(
        "Cek hasilnya di halaman Logs NMS (TRAP_*) atau GET /api/traps. "
        "UDP tanpa ACK - bila tidak muncul, periksa snmp_trap_enabled/port/community receiver."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
