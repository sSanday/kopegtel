"""Blueprint MikroTik (M1-M6): ringkasan armada, tab, temuan, metrik, aksi, backup."""

import csv
import io
import re
import time
from collections import defaultdict, deque

from flask import Blueprint, Response, g, jsonify, request, session

import backups
import db
import poller
from collectors import mikrotik_modules
from core.csvsafe import csv_row
from core.guard import audit, client_ip, roles
from core.metrics import downsample as _downsample, range_since as _range_since

bp = Blueprint("mikrotik", __name__)

# =========================================================
# Modul MikroTik (M1–M5)
# =========================================================

_MT_TABS = (
    "ringkasan", "interfaces", "routing", "dhcp", "sessions", "wireless",
    "firewall", "logs", "neighbors", "security",
)
_MT_TAB_LABELS = {
    "ringkasan": "Ringkasan",
    "interfaces": "Interface",
    "routing": "Routing",
    "dhcp": "DHCP & ARP",
    "sessions": "Sesi",
    "wireless": "Wireless",
    "firewall": "Firewall & Queue",
    "logs": "Log",
    "neighbors": "Neighbor",
    "security": "Keamanan",
}
_MT_TAB_PROBES = {
    "interfaces": [],
    "routing": ["routes", "routes6", "bgp", "ospf"],
    "dhcp": ["dhcp", "dhcp_pools", "dhcp_servers", "arp"],
    "sessions": ["ppp", "hotspot"],
    "wireless": ["wireless"],
    "firewall": ["firewall_filter", "firewall_nat", "firewall_mangle", "address_lists",
                 "queue_simple", "queue_tree"],
    "logs": ["logs"],
    "neighbors": ["neighbors"],
    "security": ["services", "users", "dns", "ntp", "routerboard", "license"],
}
_MT_TAB_MODULES = {
    "routing": ["routing"],
    "dhcp": ["dhcp"],
    "sessions": ["ppp", "hotspot"],
    "wireless": ["wireless"],
    "firewall": ["queue"],
    "logs": ["log"],
    "security": ["security"],
}
_MT_ERROR_LABELS = {
    "api_disabled": "API MikroTik belum diaktifkan untuk perangkat ini.",
    "auth_failed": "Autentikasi RouterOS gagal — periksa pengguna/kata sandi API.",
    "no_permission": "Pengguna API tidak memiliki izin membaca bagian ini.",
    "timeout": "RouterOS tidak menjawab dalam batas waktu.",
    "tls_error": "Verifikasi TLS ke RouterOS gagal - periksa sertifikat dan fingerprint perangkat.",
    "tls_fingerprint_mismatch": "Fingerprint sertifikat RouterOS tidak cocok dengan pengaturan perangkat.",
    "unsupported": "Perintah ini tidak tersedia pada versi/paket RouterOS perangkat.",
    "error": "Terjadi kesalahan saat membaca RouterOS.",
    "unknown": "Menunggu poll MikroTik pertama.",
    "disabled": "Modul ini dinonaktifkan pada pengaturan perangkat.",
    "ok": "Data tersedia.",
    "empty": "Belum ada data untuk bagian ini.",
}


def _mt_device(device_id):
    device = db.get_device(device_id)
    if device is None:
        return None
    if device.get("type") != "mikrotik" and not device.get("mikrotik_enabled"):
        return None
    return device


def _mt_public_settings(settings, role):
    data = {
        "device_id": settings.get("device_id"),
        "modules": settings.get("modules") or {},
        "intervals": settings.get("intervals") or {},
        "important_interfaces": settings.get("important_interfaces") or [],
        "write_enabled": bool(settings.get("write_enabled")),
        "has_write_credentials": bool(settings.get("has_write_credentials")),
        "backup_enabled": bool(settings.get("backup_enabled")),
    }
    if role == "admin":
        data.update(
            {
                "write_user": settings.get("write_user"),
                "write_port": settings.get("write_port"),
                "write_ssl": bool(settings.get("write_ssl")),
                "ssh_enabled": bool(settings.get("ssh_enabled")),
                "has_ssh_credentials": bool(settings.get("has_ssh_credentials")),
                "ssh_user": settings.get("ssh_user"),
                "ssh_host_key": settings.get("ssh_host_key"),
                "ssh_port": settings.get("ssh_port"),
                "backup_hour": settings.get("backup_hour"),
            }
        )
    return data


def _mt_tab_state(device, tab, settings, caps, snapshot_exists=False):
    if tab == "ringkasan":
        return {"state": "ok", "reason": None, "detail": _MT_ERROR_LABELS["ok"], "probes": []}
    if not device.get("mikrotik_enabled"):
        return {
            "state": "api_disabled",
            "reason": "api_disabled",
            "detail": _MT_ERROR_LABELS["api_disabled"],
            "probes": [],
        }
    modules = settings.get("modules") or {}
    module_keys = _MT_TAB_MODULES.get(tab) or []
    if module_keys and not any(modules.get(key, True) for key in module_keys):
        return {
            "state": "disabled",
            "reason": "disabled",
            "detail": _MT_ERROR_LABELS["disabled"],
            "probes": [],
        }
    probe_keys = _MT_TAB_PROBES.get(tab) or []
    probes = (caps or {}).get("probes") or {}
    probe_states = []
    supported = 0
    for key in probe_keys:
        state = probes.get(key)
        if state:
            probe_states.append({"probe": key, **state})
            if state.get("supported"):
                supported += 1
    if not probe_keys:
        if snapshot_exists or (caps or {}).get("checked_at"):
            return {"state": "ok", "reason": None, "detail": _MT_ERROR_LABELS["ok"], "probes": probe_states}
        return {
            "state": "unknown", "reason": "unknown",
            "detail": _MT_ERROR_LABELS["unknown"], "probes": probe_states,
        }
    if supported:
        return {"state": "ok", "reason": None, "detail": _MT_ERROR_LABELS["ok"], "probes": probe_states}
    if probe_states:
        priority = ["no_permission", "timeout", "tls_fingerprint_mismatch", "tls_error", "auth_failed", "error", "unsupported"]
        reason = "unsupported"
        for candidate in priority:
            if any(item.get("reason") == candidate for item in probe_states):
                reason = candidate
                break
        details = "; ".join(
            f"{item.get('probe')}: {item.get('detail') or item.get('reason') or 'gagal'}"
            for item in probe_states
        )[:500]
        return {
            "state": reason,
            "reason": reason,
            "detail": details or _MT_ERROR_LABELS.get(reason, _MT_ERROR_LABELS["error"]),
            "probes": probe_states,
        }
    metrics_error = (device.get("metrics_error") or "").strip().lower()
    if metrics_error in ("auth_failed", "timeout", "error", "unreachable", "tls_error", "tls_fingerprint_mismatch"):
        state = "timeout" if metrics_error == "timeout" else metrics_error
        return {
            "state": state,
            "reason": state,
            "detail": _MT_ERROR_LABELS.get(state, _MT_ERROR_LABELS["error"]),
            "probes": [],
        }
    return {
        "state": "unknown",
        "reason": "unknown",
        "detail": _MT_ERROR_LABELS["unknown"],
        "probes": [],
    }


def _mt_filter_rows(rows, search, fields):
    needle = (search or "").strip().lower()
    if not needle:
        return list(rows)
    result = []
    for row in rows:
        haystack = " ".join(str(row.get(field) or "") for field in fields).lower()
        if needle in haystack:
            result.append(row)
    return result


def _mt_page(rows, page, per_page):
    page = max(1, int(page or 1))
    per_page = min(200, max(1, int(per_page or 50)))
    total = len(rows)
    start = (page - 1) * per_page
    return {
        "rows": rows[start:start + per_page],
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": max(1, -(-total // per_page)),
    }


def _mt_truncated_map(device_id):
    merged = {}
    for state in db.get_mt_poll_state(device_id):
        merged.update(state.get("truncated") or {})
    return merged


def _mt_section(device_id, table, label, *, search, fields, page, per_page,
                sort_key=None, truncated_keys=None, extra_rows=None):
    rows = db.list_mt_rows(table, device_id)
    if extra_rows:
        rows = rows + extra_rows
    filtered = _mt_filter_rows(rows, search, fields)
    if sort_key:
        filtered.sort(key=sort_key)
    paged = _mt_page(filtered, page, per_page)
    truncated_map = _mt_truncated_map(device_id)
    truncated = any(bool(truncated_map.get(key)) for key in (truncated_keys or []))
    return {
        "key": table,
        "label": label,
        "rows": paged["rows"],
        "total": paged["total"],
        "page": paged["page"],
        "per_page": paged["per_page"],
        "pages": paged["pages"],
        "truncated": truncated,
        "stale_count": sum(1 for row in filtered if row.get("_stale")),
        "polled_at": db.mt_snapshot_updated_at(table, device_id),
    }


def _mt_redacted_section(key, label, total, *, summary=None):
    return {
        "key": key,
        "label": label,
        "rows": [],
        "total": total,
        "page": 1,
        "per_page": 0,
        "pages": 1,
        "truncated": False,
        "stale_count": 0,
        "polled_at": None,
        "redacted": True,
        "summary": summary or {},
    }


def _mt_tab_sections(device, tab, *, search, page, per_page, role, args):
    device_id = device["id"]
    settings = db.get_mt_device_settings(device_id)
    important = set(settings.get("important_interfaces") or [])
    viewer = role == "viewer"
    sections = []

    if tab == "interfaces":
        section = _mt_section(
            device_id, "mt_interfaces", "Interface",
            search=search, fields=("name", "type", "mac", "comment"),
            page=page, per_page=per_page,
            sort_key=lambda row: (bool(row.get("disabled")), not bool(row.get("running")),
                                  str(row.get("name") or "").lower()),
            truncated_keys=("interfaces",),
        )
        for row in section["rows"]:
            row["important"] = row.get("name") in important
        sections.append(section)
        sections.append(_mt_section(
            device_id, "mt_ip_addresses", "Alamat IP",
            search=search, fields=("interface", "address", "network"),
            page=page, per_page=per_page,
            sort_key=lambda row: (str(row.get("interface") or ""), str(row.get("address") or "")),
            truncated_keys=("addresses",),
        ))

    elif tab == "routing":
        sections.append(_mt_section(
            device_id, "mt_routes", "Rute",
            search=search,
            fields=("dst_address", "gateway", "comment", "family"),
            page=page, per_page=per_page,
            sort_key=lambda row: (not bool(row.get("active")), bool(row.get("disabled")),
                                  str(row.get("dst_address") or "")),
            truncated_keys=("routes", "routes6"),
        ))
        sections.append(_mt_section(
            device_id, "mt_routing_neighbors", "Neighbor Routing",
            search=search,
            fields=("name", "peer_id", "remote_address", "address", "state", "protocol"),
            page=page, per_page=per_page,
            sort_key=lambda row: (str(row.get("protocol") or ""), str(row.get("state") or "")),
            truncated_keys=("bgp", "ospf"),
        ))

    elif tab == "dhcp":
        sections.append(_mt_section(
            device_id, "mt_dhcp_pools", "Pool",
            search=search, fields=("name", "ranges"),
            page=page, per_page=per_page,
            sort_key=lambda row: str(row.get("name") or ""),
            truncated_keys=("dhcp_pools",),
        ))
        if viewer:
            leases = _mt_filter_rows(
                db.list_mt_rows("mt_dhcp_leases", device_id),
                search, ("address", "mac", "host_name", "server"),
            )
            sections.append(_mt_redacted_section(
                "mt_dhcp_leases", "Lease", len(leases),
                summary={"active": sum(1 for row in leases if row.get("status") == "bound")},
            ))
            arp = _mt_filter_rows(
                db.list_mt_rows("mt_arp", device_id),
                search, ("address", "mac", "interface"),
            )
            sections.append(_mt_redacted_section("mt_arp", "ARP", len(arp)))
        else:
            sections.append(_mt_section(
                device_id, "mt_dhcp_leases", "Lease",
                search=search, fields=("address", "mac", "host_name", "server", "comment", "status"),
                page=page, per_page=per_page,
                sort_key=lambda row: (str(row.get("status") or ""), str(row.get("address") or "")),
                truncated_keys=("dhcp",),
            ))
            sections.append(_mt_section(
                device_id, "mt_arp", "ARP",
                search=search, fields=("address", "mac", "interface"),
                page=page, per_page=per_page,
                sort_key=lambda row: str(row.get("address") or ""),
                truncated_keys=("arp",),
            ))

    elif tab == "sessions":
        if viewer:
            ppp = _mt_filter_rows(
                db.list_mt_rows("mt_ppp_sessions", device_id),
                search, ("name", "caller_id", "address", "service"),
            )
            hotspot = _mt_filter_rows(
                db.list_mt_rows("mt_hotspot_sessions", device_id),
                search, ("user", "address", "mac", "server"),
            )
            sections.append(_mt_redacted_section("mt_ppp_sessions", "Sesi PPP", len(ppp)))
            sections.append(_mt_redacted_section("mt_hotspot_sessions", "Sesi Hotspot", len(hotspot)))
        else:
            sections.append(_mt_section(
                device_id, "mt_ppp_sessions", "Sesi PPP",
                search=search, fields=("name", "caller_id", "address", "service", "encoding"),
                page=page, per_page=per_page,
                sort_key=lambda row: str(row.get("name") or ""),
                truncated_keys=("ppp",),
            ))
            sections.append(_mt_section(
                device_id, "mt_hotspot_sessions", "Sesi Hotspot",
                search=search, fields=("user", "address", "mac", "server"),
                page=page, per_page=per_page,
                sort_key=lambda row: str(row.get("user") or ""),
                truncated_keys=("hotspot",),
            ))

    elif tab == "wireless":
        if viewer:
            rows = _mt_filter_rows(
                db.list_mt_rows("mt_wireless_clients", device_id),
                search, ("interface", "mac", "signal_strength"),
            )
            sections.append(_mt_redacted_section(
                "mt_wireless_clients", "Klien Wireless", len(rows),
                summary={"interfaces": len({row.get("interface") for row in rows if row.get("interface")})},
            ))
        else:
            sections.append(_mt_section(
                device_id, "mt_wireless_clients", "Klien Wireless",
                search=search, fields=("interface", "mac", "signal_strength", "tx_rate", "rx_rate"),
                page=page, per_page=per_page,
                sort_key=lambda row: (str(row.get("interface") or ""), str(row.get("mac") or "")),
                truncated_keys=("wireless",),
            ))

    elif tab == "firewall":
        sections.append(_mt_section(
            device_id, "mt_firewall_rules", "Aturan Firewall",
            search=search, fields=("kind", "chain", "action", "comment"),
            page=page, per_page=per_page,
            sort_key=lambda row: (str(row.get("kind") or ""), str(row.get("chain") or ""),
                                  str(row.get("action") or "")),
            truncated_keys=("firewall_filter", "firewall_nat", "firewall_mangle"),
        ))
        sections.append(_mt_section(
            device_id, "mt_address_lists", "Address List",
            search=search, fields=("list", "address", "comment"),
            page=page, per_page=per_page,
            sort_key=lambda row: (str(row.get("list") or ""), str(row.get("address") or "")),
            truncated_keys=("address_lists",),
        ))
        sections.append(_mt_section(
            device_id, "mt_queues", "Queue",
            search=search, fields=("name", "target", "kind", "comment"),
            page=page, per_page=per_page,
            sort_key=lambda row: (str(row.get("kind") or ""), str(row.get("name") or "")),
            truncated_keys=("queue_simple", "queue_tree"),
        ))

    elif tab == "logs":
        severity = (args.get("severity") or "").strip().lower()
        topics = (args.get("topics") or "").strip() or None
        counts = db.mt_log_severity_counts(device_id)
        if viewer:
            total = sum(counts.values())
            sections.append(_mt_redacted_section("mt_logs", "Log", total, summary=counts))
        else:
            rows, total = db.list_mt_logs(
                device_id, severity=severity or None, topics=topics,
                search=search, limit=per_page, offset=(max(1, page) - 1) * per_page,
            )
            sections.append({
                "key": "mt_logs",
                "label": "Log",
                "rows": rows,
                "total": total,
                "page": max(1, page),
                "per_page": per_page,
                "pages": max(1, -(-total // per_page)),
                "truncated": False,
                "stale_count": 0,
                "polled_at": rows[0]["polled_at"] if rows else None,
                "summary": counts,
            })

    elif tab == "neighbors":
        sections.append(_mt_section(
            device_id, "mt_neighbors", "Neighbor MNDP/LLDP",
            search=search, fields=("identity", "platform", "address", "mac", "interface", "board"),
            page=page, per_page=per_page,
            sort_key=lambda row: str(row.get("identity") or row.get("address") or ""),
            truncated_keys=("neighbors",),
        ))

    elif tab == "security":
        findings = db.list_mt_findings(device_id, include_ignored=True, include_stale=False)
        if viewer:
            findings = [row for row in findings if row.get("category") != "stale_user"]
        findings = _mt_filter_rows(
            findings, search,
            ("title", "detail", "remediation", "category", "severity"),
        )
        findings.sort(key=lambda row: (
            {"high": 0, "average": 1, "warning": 2, "information": 3}.get(row.get("severity"), 4),
            bool(row.get("ignored")),
            str(row.get("title") or ""),
        ))
        paged = _mt_page(findings, page, per_page)
        sections.append({
            "key": "mt_security_findings",
            "label": "Temuan Keamanan",
            "rows": paged["rows"],
            "total": paged["total"],
            "page": paged["page"],
            "per_page": paged["per_page"],
            "pages": paged["pages"],
            "truncated": False,
            "stale_count": 0,
            "polled_at": None,
            "open_count": sum(1 for row in findings if not row.get("ignored")),
            "ignored_count": sum(1 for row in findings if row.get("ignored")),
        })
        sections.append(_mt_section(
            device_id, "mt_services", "Layanan Router",
            search=search, fields=("name", "address", "port"),
            page=page, per_page=per_page,
            sort_key=lambda row: str(row.get("name") or ""),
            truncated_keys=("services",),
        ))
        if not viewer:
            sections.append(_mt_section(
                device_id, "mt_users", "Pengguna Router",
                search=search, fields=("name", "group", "address", "comment"),
                page=page, per_page=per_page,
                sort_key=lambda row: str(row.get("name") or ""),
                truncated_keys=("users",),
            ))

    return sections


@bp.get("/api/mikrotik/summary")
def api_mt_summary():
    return jsonify({"summary": db.mt_summary()})


@bp.get("/api/mikrotik/filter-options")
def api_mt_filter_options():
    return jsonify(db.mt_filter_options())


@bp.get("/api/mikrotik/devices")
def api_mt_devices():
    try:
        page = max(1, int(request.args.get("page", 1)))
        per_page = min(100, max(1, int(request.args.get("per_page", 25))))
    except (TypeError, ValueError):
        page, per_page = 1, 25
    return jsonify(
        db.query_mt_devices(
            search=request.args.get("search", "").strip(),
            status=request.args.get("status", "").strip().lower(),
            version=request.args.get("version", "").strip(),
            firmware=request.args.get("firmware", "").strip(),
            model=request.args.get("model", "").strip(),
            page=page,
            per_page=per_page,
        )
    )


@bp.get("/api/mikrotik/devices/<int:device_id>/overview")
def api_mt_overview(device_id):
    device = _mt_device(device_id)
    if device is None:
        return jsonify({"error": "Perangkat MikroTik tidak ditemukan."}), 404
    role = g.user["role"]
    settings = db.get_mt_device_settings(device_id)
    caps = db.get_mt_capabilities(device_id)
    system = db.get_mt_system(device_id) or {}
    counts = db.mt_device_counts(device_id)
    tabs = {}
    for tab in _MT_TABS:
        exists = False
        if tab == "interfaces":
            exists = db.mt_snapshot_updated_at("mt_interfaces", device_id) is not None
        tabs[tab] = {
            "label": _MT_TAB_LABELS[tab],
            **_mt_tab_state(device, tab, settings, caps, snapshot_exists=exists),
        }
    findings = db.list_mt_findings(device_id, include_ignored=True, include_stale=False)
    if role == "viewer":
        findings = [row for row in findings if row.get("category") != "stale_user"]
    probe_errors = [
        {"probe": key, **state}
        for key, state in (caps.get("probes") or {}).items()
        if state and not state.get("supported")
    ]
    data = {
        "device": device,
        "system": system or None,
        "capabilities": caps,
        "probe_errors": probe_errors,
        "counts": counts,
        "problems": db.list_problems_for_device(device_id),
        "tabs": tabs,
        "poll_state": db.get_mt_poll_state(device_id),
        "findings": {
            "total": len(findings),
            "open": sum(1 for row in findings if not row.get("ignored")),
            "ignored": sum(1 for row in findings if row.get("ignored")),
            "rows": findings[:20],
        },
        "connections": db.mt_metric_latest(device_id, "connections"),
        "settings": _mt_public_settings(settings, role),
        "write_actions": {
            "global_enabled": db.get_setting("mt_write_actions_enabled", "0") == "1",
            "device_enabled": bool(settings.get("write_enabled")),
            "has_credentials": bool(settings.get("has_write_credentials")),
            "available": (
                db.get_setting("mt_write_actions_enabled", "0") == "1"
                and bool(settings.get("write_enabled"))
                and bool(settings.get("has_write_credentials"))
            ),
        },
    }
    if role in ("admin", "operator"):
        data["actions"] = db.list_mt_actions(device_id, limit=20)
        data["settings"] = _mt_public_settings(settings, role)
    return jsonify(data)


@bp.get("/api/mikrotik/devices/<int:device_id>/tab/<tab>")
def api_mt_tab(device_id, tab):
    if tab not in _MT_TABS or tab == "ringkasan":
        return jsonify({"error": "Tab MikroTik tidak dikenal."}), 404
    device = _mt_device(device_id)
    if device is None:
        return jsonify({"error": "Perangkat MikroTik tidak ditemukan."}), 404
    role = g.user["role"]
    try:
        page = max(1, int(request.args.get("page", 1)))
        per_page = min(200, max(1, int(request.args.get("per_page", 50))))
    except (TypeError, ValueError):
        page, per_page = 1, 50
    settings = db.get_mt_device_settings(device_id)
    caps = db.get_mt_capabilities(device_id)
    exists = db.mt_snapshot_updated_at("mt_interfaces", device_id) is not None
    state = _mt_tab_state(device, tab, settings, caps, snapshot_exists=exists)
    sections = []
    if state["state"] in ("ok", "empty"):
        sections = _mt_tab_sections(
            device, tab,
            search=request.args.get("search", "").strip(),
            page=page, per_page=per_page, role=role, args=request.args,
        )
    return jsonify(
        {
            "tab": tab,
            "label": _MT_TAB_LABELS[tab],
            "state": state,
            "sections": sections,
            "settings": _mt_public_settings(settings, role),
        }
    )


_mt_refresh_requests = {}
MT_REFRESH_COOLDOWN = 10


@bp.post("/api/mikrotik/devices/<int:device_id>/refresh")
@roles("admin", "operator")
def api_mt_refresh(device_id):
    device = _mt_device(device_id)
    if device is None:
        return jsonify({"error": "Perangkat MikroTik tidak ditemukan."}), 404
    if not device.get("mikrotik_enabled"):
        return jsonify({"error": "API MikroTik belum diaktifkan untuk perangkat ini."}), 400
    now = time.monotonic()
    last = _mt_refresh_requests.get(device_id, 0.0)
    if now - last < MT_REFRESH_COOLDOWN:
        remaining = int(MT_REFRESH_COOLDOWN - (now - last)) + 1
        return jsonify({"error": f"Tunggu {remaining} detik sebelum memperbarui lagi."}), 429
    _mt_refresh_requests[device_id] = now
    poller.run_check(device_id, force_mikrotik=True)
    audit("mikrotik.refresh", f"device:{device_id}", f"Poll modul MikroTik manual {device['name']}")
    return jsonify({"ok": True})


@bp.get("/api/mikrotik/devices/<int:device_id>/settings")
@roles("admin", "operator")
def api_mt_get_settings(device_id):
    device = _mt_device(device_id)
    if device is None:
        return jsonify({"error": "Perangkat MikroTik tidak ditemukan."}), 404
    settings = db.get_mt_device_settings(device_id)
    return jsonify({"settings": _mt_public_settings(settings, g.user["role"])})


@bp.put("/api/mikrotik/devices/<int:device_id>/settings")
@roles("admin", "operator")
def api_mt_update_settings(device_id):
    device = _mt_device(device_id)
    if device is None:
        return jsonify({"error": "Perangkat MikroTik tidak ditemukan."}), 404
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({"error": "Payload tidak valid."}), 400
    role = g.user["role"]
    clean = {}

    if "important_interfaces" in data:
        names = data.get("important_interfaces")
        if not isinstance(names, list):
            return jsonify({"error": "important_interfaces harus berupa daftar nama interface."}), 400
        values = []
        for item in names:
            name = str(item or "").strip()
            if not name:
                continue
            if len(name) > 120:
                return jsonify({"error": "Nama interface maksimal 120 karakter."}), 400
            if name not in values:
                values.append(name)
        if len(values) > 200:
            return jsonify({"error": "Maksimal 200 interface penting."}), 400
        clean["important_interfaces"] = values

    if role == "admin":
        if "modules" in data:
            modules = data.get("modules")
            if not isinstance(modules, dict):
                return jsonify({"error": "modules harus berupa objek."}), 400
            clean["modules"] = {
                key: bool(modules.get(key, True)) for key in db.MT_MODULE_DEFAULTS
            }
        if "intervals" in data:
            intervals = data.get("intervals")
            if not isinstance(intervals, dict):
                return jsonify({"error": "intervals harus berupa objek."}), 400
            limits = {"fast": (30, 86400), "medium": (60, 86400), "slow": (300, 604800)}
            clean["intervals"] = {}
            for key, (lo, hi) in limits.items():
                if key not in intervals:
                    continue
                try:
                    value = int(intervals[key])
                except (TypeError, ValueError):
                    return jsonify({"error": f"Interval {key} harus berupa angka."}), 400
                if not (lo <= value <= hi):
                    return jsonify({"error": f"Interval {key} harus antara {lo} dan {hi} detik."}), 400
                clean["intervals"][key] = value
        for key in ("write_enabled", "write_ssl", "ssh_enabled", "backup_enabled"):
            if key in data:
                clean[key] = bool(data[key])
        for key in ("write_user", "write_password", "ssh_user", "ssh_password", "ssh_host_key"):
            if key in data:
                value = str(data.get(key) or "").strip()
                if len(value) > 300:
                    return jsonify({"error": f"{key} terlalu panjang."}), 400
                clean[key] = value
        if "write_port" in data and str(data.get("write_port") or "").strip():
            try:
                port = int(data["write_port"])
            except (TypeError, ValueError):
                return jsonify({"error": "Port API tulis harus berupa angka."}), 400
            if not (1 <= port <= 65535):
                return jsonify({"error": "Port API tulis harus antara 1 dan 65535."}), 400
            clean["write_port"] = port
        if "ssh_port" in data and str(data.get("ssh_port") or "").strip():
            try:
                port = int(data["ssh_port"])
            except (TypeError, ValueError):
                return jsonify({"error": "Port SSH harus berupa angka."}), 400
            if not (1 <= port <= 65535):
                return jsonify({"error": "Port SSH harus antara 1 dan 65535."}), 400
            clean["ssh_port"] = port
        if "backup_hour" in data:
            try:
                hour = int(data["backup_hour"])
            except (TypeError, ValueError):
                return jsonify({"error": "Jam backup harus berupa angka 0–23."}), 400
            if not (0 <= hour <= 23):
                return jsonify({"error": "Jam backup harus antara 0 dan 23."}), 400
            clean["backup_hour"] = hour

    if not clean:
        return jsonify({"error": "Tidak ada pengaturan yang dikirim."}), 400
    db.save_mt_device_settings(device_id, clean)
    audit(
        "mikrotik.settings.update", f"device:{device_id}",
        f"Mengubah pengaturan MikroTik {device['name']}: {', '.join(sorted(clean))}",
    )
    return jsonify({
        "ok": True,
        "settings": _mt_public_settings(db.get_mt_device_settings(device_id), role),
    })


@bp.post("/api/mikrotik/devices/<int:device_id>/findings/<int:finding_id>/ignore")
@roles("admin", "operator")
def api_mt_finding_ignore(device_id, finding_id):
    device = _mt_device(device_id)
    if device is None:
        return jsonify({"error": "Perangkat MikroTik tidak ditemukan."}), 404
    finding = db.get_mt_finding(finding_id)
    if finding is None or finding.get("device_id") != device_id:
        return jsonify({"error": "Temuan tidak ditemukan."}), 404
    data = request.get_json(silent=True) or {}
    ignored = bool(data.get("ignored", True))
    reason = str(data.get("reason") or "").strip()
    if ignored and len(reason) < 3:
        return jsonify({"error": "Alasan mengabaikan temuan wajib diisi (minimal 3 karakter)."}), 400
    db.set_mt_finding_ignored(
        finding_id, ignored=ignored, username=g.user["username"],
        reason=reason if ignored else None,
    )
    audit(
        "mikrotik.finding.ignore" if ignored else "mikrotik.finding.unignore",
        f"mikrotik_finding:{finding_id}",
        f"{'Mengabaikan' if ignored else 'Mengaktifkan kembali'} temuan "
        f"{finding.get('title') or finding_id} pada {device['name']}"
        + (f" — alasan: {reason}" if ignored else ""),
    )
    return jsonify({"ok": True})


def _mt_neighbor_addresses(row):
    values = []
    for key in ("address", "address4", "address6"):
        raw = row.get(key)
        if not raw:
            continue
        for chunk in str(raw).replace(";", ",").split(","):
            value = chunk.strip().split("%", 1)[0].strip()
            if value and value not in values:
                values.append(value)
    return values


def _mt_device_rank(device):
    return {
        "firewall": 0,
        "router": 1,
        "olt": 1,
        "mikrotik": 1,
        "switch": 2,
        "server": 2,
        "access-point": 3,
        "ont": 4,
    }.get(device.get("type"), 2)


def _mt_topology_suggestions():
    all_devices = db.list_devices()
    devices = [item for item in all_devices if item.get("type") == "mikrotik" or item.get("mikrotik_enabled")]
    by_ip = {item["ip"]: item for item in all_devices}
    by_name = {str(item["name"]).strip().lower(): item for item in all_devices}
    suggestions = {}
    seen_pairs = set()
    for source in devices:
        rows = db.list_mt_rows("mt_neighbors", source["id"], include_stale=False)
        for row in rows:
            target = None
            match_reason = None
            for address in _mt_neighbor_addresses(row):
                if address in by_ip and by_ip[address]["id"] != source["id"]:
                    target = by_ip[address]
                    match_reason = f"IP {address}"
                    break
            if target is None:
                identity = str(row.get("identity") or "").strip().lower()
                if identity and identity in by_name and by_name[identity]["id"] != source["id"]:
                    target = by_name[identity]
                    match_reason = f"identitas {row.get('identity')}"
            if target is None:
                continue
            source_rank, target_rank = _mt_device_rank(source), _mt_device_rank(target)
            uncertain = False
            if source_rank < target_rank:
                parent, child = source, target
            elif target_rank < source_rank:
                parent, child = target, source
            else:
                parent, child = source, target
                uncertain = True
            pair = (child["id"], parent["id"])
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            already = child.get("parent_id") == parent["id"]
            if already:
                continue
            confidence = "rendah" if uncertain else ("tinggi" if match_reason.startswith("IP ") else "sedang")
            reason = (
                f"Ditemukan via MNDP/LLDP dari {source['name']} "
                f"(interface {row.get('interface') or '—'}, {match_reason})"
            )
            if row.get("platform"):
                reason += f", platform {row.get('platform')}"
            suggestions[pair] = {
                "child_id": child["id"],
                "child_name": child["name"],
                "child_ip": child["ip"],
                "child_type": child["type"],
                "parent_id": parent["id"],
                "parent_name": parent["name"],
                "parent_ip": parent["ip"],
                "parent_type": parent["type"],
                "current_parent_id": child.get("parent_id"),
                "current_parent_name": child.get("parent_name"),
                "interface": row.get("interface"),
                "match_reason": match_reason,
                "platform": row.get("platform"),
                "identity": row.get("identity"),
                "confidence": confidence,
                "uncertain": uncertain,
                "reason": reason,
            }
    return sorted(suggestions.values(), key=lambda item: (item["child_name"].lower(), item["parent_name"].lower()))


@bp.get("/api/mikrotik/topology/suggestions")
def api_mt_topology_suggestions():
    return jsonify({"suggestions": _mt_topology_suggestions()})


@bp.post("/api/mikrotik/topology/apply")
@roles("admin", "operator")
def api_mt_topology_apply():
    data = request.get_json(silent=True) or {}
    try:
        child_id = int(data.get("child_id"))
        parent_id = int(data.get("parent_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "child_id dan parent_id wajib berupa angka."}), 400
    allowed = {
        (item["child_id"], item["parent_id"]) for item in _mt_topology_suggestions()
    }
    if (child_id, parent_id) not in allowed:
        return jsonify({"error": "Relasi ini bukan saran topologi yang valid saat ini."}), 400
    ok, message = db.set_device_parent(child_id, parent_id, g.user["username"])
    if not ok:
        return jsonify({"error": message}), 400
    audit("mikrotik.topology.apply", f"device:{child_id}", message)
    return jsonify({"ok": True, "message": message})


def _mt_fleet_rows():
    rows = []
    page = 1
    while True:
        data = db.query_mt_devices(page=page, per_page=100)
        rows.extend(data.get("devices") or [])
        if page >= data.get("pages", 1):
            break
        page += 1
        if page > 50:
            break
    return rows


@bp.get("/api/mikrotik/reports/fleet")
def api_mt_report_fleet():
    rows = _mt_fleet_rows()
    return jsonify({"rows": rows, "count": len(rows)})


def _mt_fmt_pct(value):
    return "—" if value is None else f"{value:.1f}"


def _mt_fmt_uptime(value):
    if value is None:
        return "—"
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return "—"
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days} hari {hours} jam"
    if hours:
        return f"{hours} jam {minutes} menit"
    return f"{minutes} menit"


@bp.get("/api/mikrotik/reports/fleet.csv")
def api_mt_report_fleet_csv():
    rows = _mt_fleet_rows()
    audit("report.mikrotik_fleet_csv", "report", f"Mengunduh laporan armada MikroTik ({len(rows)} perangkat)")
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Perangkat", "IP", "Model", "RouterOS", "Firmware", "Status Firmware",
        "Uptime", "CPU %", "Memori %", "Interface Up/Total", "Sesi Aktif",
        "Temuan Keamanan", "Masalah Terbuka", "Terakhir Poll",
    ])
    for row in rows:
        system = row.get("system") or {}
        writer.writerow(csv_row([
            row.get("name"), row.get("ip"),
            system.get("model") or row.get("mikrotik_model") or "—",
            system.get("version") or row.get("routeros_version") or "—",
            system.get("current_firmware") or row.get("mikrotik_firmware") or "—",
            system.get("firmware_status") or "unknown",
            _mt_fmt_uptime(row.get("uptime_s")),
            _mt_fmt_pct(row.get("cpu_pct")),
            _mt_fmt_pct(row.get("mem_pct")),
            f"{row.get('interfaces_up', 0)}/{row.get('interfaces_total', 0)}",
            row.get("active_sessions") or 0,
            row.get("security_open") or 0,
            row.get("open_problems") or 0,
            row.get("metrics_ts") or "—",
        ]))
    return Response(
        "﻿" + output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=armada-mikrotik.csv"},
    )


# ----- Aksi tulis M5: allowlist + konfirmasi + rate limit -----

_mt_action_calls = defaultdict(deque)


def _mt_action_limited(device_id, action):
    user_id = session.get("user_id") or "anon"
    slow = action in ("reboot", "interface_disable", "interface_enable")
    window = 600 if slow else 60
    limit = 3 if slow else 20
    key = (user_id, device_id, "slow" if slow else "fast")
    now = time.monotonic()
    calls = _mt_action_calls[key]
    while calls and now - calls[0] > window:
        calls.popleft()
    if len(calls) >= limit:
        return True
    calls.append(now)
    return False


# ---------------------------------------------------------
# Metrik numerik MikroTik (grafik jumlah sesi/lease/koneksi)
# ---------------------------------------------------------


@bp.get("/api/mikrotik/devices/<int:device_id>/metrics")
def api_mt_session_metrics(device_id):
    device = _mt_device(device_id)
    if device is None:
        return jsonify({"error": "Perangkat MikroTik tidak ditemukan."}), 404
    since = _range_since((request.args.get("range") or "24h").lower())
    if since is None:
        return jsonify({"error": "Rentang tidak valid. Gunakan 1h, 24h, atau 7d."}), 400
    metrics = {}
    for metric in ("ppp_active", "hotspot_active", "connections", "dhcp_leases"):
        metrics[metric] = _downsample(db.mt_metric_points(device_id, metric, since))
    return jsonify({"range": (request.args.get("range") or "24h").lower(), "metrics": metrics})


# ---------------------------------------------------------
# Backup konfigurasi MikroTik via SSH (M6, admin)
# ---------------------------------------------------------

_mt_backup_requests = {}
MT_BACKUP_COOLDOWN = 60


@bp.get("/api/mikrotik/devices/<int:device_id>/backups")
@roles("admin")
def api_mt_backups(device_id):
    device = _mt_device(device_id)
    if device is None:
        return jsonify({"error": "Perangkat MikroTik tidak ditemukan."}), 404
    entries = db.list_mt_backups(device_id, limit=30)
    for entry in entries:
        entry["sha256_short"] = (entry.get("sha256") or "")[:12]
    return jsonify({"backups": entries})


@bp.post("/api/mikrotik/devices/<int:device_id>/backups")
@roles("admin")
def api_mt_backups_run(device_id):
    device = _mt_device(device_id)
    if device is None:
        return jsonify({"error": "Perangkat MikroTik tidak ditemukan."}), 404
    now = time.monotonic()
    last = _mt_backup_requests.get(device_id, 0.0)
    if now - last < MT_BACKUP_COOLDOWN:
        remaining = int(MT_BACKUP_COOLDOWN - (now - last)) + 1
        return jsonify({"error": f"Tunggu {remaining} detik sebelum backup lagi."}), 429
    _mt_backup_requests[device_id] = now
    try:
        backup = backups.run_backup(
            device_id, source="manual",
            actor=g.user["username"], ip=client_ip(),
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:  # noqa: BLE001 — kegagalan SSH/ekspor sudah diaudit di backups.run_backup
        return jsonify({"error": f"Backup gagal: {exc}"}), 502
    return jsonify({
        "ok": True,
        "backup": {
            "id": backup["id"],
            "created_at": backup["created_at"],
            "source": backup["source"],
            "filename": backup["filename"],
            "size": backup["size"],
            "sha256": backup["sha256"],
            "sha256_short": (backup.get("sha256") or "")[:12],
        },
    })


@bp.get("/api/mikrotik/devices/<int:device_id>/backups/<int:backup_id>/diff")
@roles("admin")
def api_mt_backup_diff(device_id, backup_id):
    device = _mt_device(device_id)
    if device is None:
        return jsonify({"error": "Perangkat MikroTik tidak ditemukan."}), 404
    with_raw = request.args.get("with", "").strip()
    with_id = int(with_raw) if with_raw.isdigit() else None
    result = backups.backup_diff(device_id, backup_id, with_id)
    if result is None:
        return jsonify({"error": "Backup tidak ditemukan."}), 404
    if result.get("status") == 404:
        return jsonify({"error": result["error"]}), 404
    audit(
        "mikrotik.backup.diff", f"device:{device_id}",
        f"Melihat diff backup {result.get('to')} vs {result.get('from')}",
    )
    return jsonify(result)


@bp.get("/api/mikrotik/devices/<int:device_id>/backups/<int:backup_id>/download")
@roles("admin")
def api_mt_backup_download(device_id, backup_id):
    device = _mt_device(device_id)
    if device is None:
        return jsonify({"error": "Perangkat MikroTik tidak ditemukan."}), 404
    backup = db.get_mt_backup(device_id, backup_id)
    if backup is None:
        return jsonify({"error": "Backup tidak ditemukan."}), 404
    audit(
        "mikrotik.backup.download", f"device:{device_id}",
        f"Mengunduh backup {backup['filename']} ({backup.get('size') or 0} byte)",
    )
    filename = re.sub(r'[^A-Za-z0-9._-]+', '-', str(backup.get("filename") or "backup.rsc"))[:120] or "backup.rsc"
    return Response(
        backup.get("content") or "",
        mimetype="text/plain; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@bp.get("/api/mikrotik/devices/<int:device_id>/actions")
@roles("admin", "operator")
def api_mt_actions(device_id):
    device = _mt_device(device_id)
    if device is None:
        return jsonify({"error": "Perangkat MikroTik tidak ditemukan."}), 404
    return jsonify({"actions": db.list_mt_actions(device_id, limit=100)})


@bp.post("/api/mikrotik/devices/<int:device_id>/actions")
@roles("admin", "operator")
def api_mt_action(device_id):
    device = _mt_device(device_id)
    if device is None:
        return jsonify({"error": "Perangkat MikroTik tidak ditemukan."}), 404
    data = request.get_json(silent=True) or {}
    action = str(data.get("action") or "").strip()
    if action not in mikrotik_modules.ALLOWED_ACTIONS:
        return jsonify({"error": "Aksi tidak ada dalam allowlist."}), 400
    if db.get_setting("mt_write_actions_enabled", "0") != "1":
        return jsonify({"error": "Aksi tulis MikroTik dinonaktifkan secara global oleh admin."}), 403
    if not device.get("mikrotik_enabled"):
        return jsonify({"error": "API MikroTik belum diaktifkan untuk perangkat ini."}), 400
    settings = db.get_mt_device_settings(device_id, include_secrets=True)
    if not settings.get("write_enabled") or not settings.get("has_write_credentials"):
        return jsonify({"error": "Kredensial tulis belum diaktifkan/diatur untuk perangkat ini."}), 403
    if _mt_action_limited(device_id, action):
        return jsonify({"error": "Terlalu banyak aksi tulis. Coba lagi nanti."}), 429

    if action in ("reboot", "interface_disable"):
        if str(data.get("confirm_name") or "") != device["name"]:
            return jsonify({
                "error": f"Ketik nama perangkat \"{device['name']}\" untuk mengonfirmasi aksi ini."
            }), 400

    params = data.get("params")
    params = params if isinstance(params, dict) else {}
    target = str(data.get("target") or "").strip()
    if action == "reboot":
        target = device["name"]
    if action in ("address_list_add", "address_list_remove"):
        params.setdefault("list", data.get("list"))
        params.setdefault("address", data.get("address"))
        params.setdefault("timeout", data.get("timeout"))
        params.setdefault("comment", data.get("comment"))
    if not target and action == "address_list_add":
        target = str(params.get("address") or "").strip()

    try:
        password = db.decrypt_secret(settings.get("write_password"))
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 500
    config = {
        "ip": device["ip"],
        "port": settings.get("write_port") or device.get("mikrotik_port") or 8728,
        "user": settings.get("write_user") or "",
        "password": password or "",
        "ssl": bool(settings.get("write_ssl")),
        "ssl_fingerprint": device.get("mikrotik_ssl_fingerprint"),
        "management_ip": device["ip"],
    }

    try:
        result = mikrotik_modules.perform_action(config, action, {**params, "target": target})
    except mikrotik_modules.ActionError as exc:
        db.add_mt_action(
            device_id, username=g.user["username"], action=action, target=target,
            params=params, result=f"Gagal: {exc}", verified=None, before=None, after=None,
            ip=client_ip(),
        )
        audit(
            "mikrotik.action.failed", f"device:{device_id}",
            f"Aksi {action} pada {target or '—'} gagal: {exc}",
        )
        status = 404 if exc.code == "not_found" else 409 if exc.code == "management_interface" else 400
        if exc.code in ("unreachable", "auth_failed", "timeout", "tls_error", "tls_fingerprint_mismatch"):
            status = 502
        return jsonify({"error": str(exc), "code": exc.code}), status
    except Exception as exc:  # noqa: BLE001
        db.add_mt_action(
            device_id, username=g.user["username"], action=action, target=target,
            params=params, result=f"Gagal: {exc}", verified=None, before=None, after=None,
            ip=client_ip(),
        )
        audit("mikrotik.action.failed", f"device:{device_id}", f"Aksi {action} gagal: {exc}")
        return jsonify({"error": f"Aksi gagal: {exc}"}), 500

    db.add_mt_action(
        device_id, username=g.user["username"], action=action, target=result.get("target") or target,
        params=params, result=result.get("detail") or "Selesai", verified=result.get("verified"),
        before=result.get("before"), after=result.get("after"), ip=client_ip(),
    )
    audit(
        "mikrotik.action", f"device:{device_id}",
        f"Aksi {action} pada {result.get('target') or target} — {result.get('detail')} "
        f"(terverifikasi: {result.get('verified')})",
    )
    if action != "reboot":
        try:
            poller.schedule_immediate_check(device_id)
        except Exception:  # noqa: BLE001
            pass
    return jsonify(result)


