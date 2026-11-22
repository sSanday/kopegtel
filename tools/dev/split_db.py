"""Satu kali pakai (T5): pecah backend/db.py menjadi package backend/db/.

Strategi: parse AST, petakan fungsi/konstanta ke domain berdasarkan nama,
tulis ulang setiap modul dengan body yang persis sama (byte-for-byte).
Jalankan: python tools/dev/split_db.py
"""
import ast
import os
import sys

SRC_PATH = "backend/db.py"
OUT_DIR = "backend/db"

BUCKETS = {
    "connection": [
        "BASE_DIR", "DB_PATH", "_lock", "_conn", "_fernet",
        "_get_fernet", "encrypt_secret", "decrypt_secret", "get_conn",
        "_now", "_after_minutes", "_days_ago", "parse_ts", "_json_loads",
        "_LEASE_STALE_SECONDS", "_lease_fresh",
        "acquire_instance_lease", "instance_lease_status",
        "heartbeat_instance_lease", "release_instance_lease",
    ],
    "schema": ["init_db"],
    "settings": [
        "SETTING_DEFAULTS", "get_settings", "get_setting", "set_settings",
        "db_file_size", "table_counts", "purge_old_checks", "purge_old_data",
    ],
    "audit": ["add_audit_log", "list_audit_log", "recent_activity"],
    "users": [
        "find_user", "find_user_by_id", "create_user", "update_user",
        "delete_user", "user_exists", "list_users", "user_count",
        "count_recent_login_failures", "record_login_failure", "clear_login_failures",
    ],
    "problems": [
        "_SEVERITY_RANK",
        "open_problem", "resolve_problem", "resolve_device_status_problems",
        "list_problems", "list_problems_for_device", "ack_problem", "unack_problem",
        "top_problems", "open_problem_count",
    ],
    "services": [
        "list_services", "get_service", "service_name_exists", "create_service",
        "update_service", "delete_service", "set_service_devices", "service_availability",
    ],
    "alerts": [
        "list_channels", "get_channel", "channel_config", "create_channel",
        "update_channel", "delete_channel", "channel_name_exists",
        "list_templates", "get_template", "template_name_exists",
        "create_template", "update_template", "delete_template",
        "list_rules", "get_rule", "rule_name_exists", "create_rule",
        "update_rule", "delete_rule",
        "list_action_log", "add_action_log",
    ],
    "discovery": [
        "create_scan_job", "update_scan_job", "finish_scan_job",
        "get_scan_job", "list_scan_jobs",
    ],
    "devices": [
        "_LATEST_METRICS_JOIN", "_LATEST_METRICS_SELECT", "_STATUS_WEIGHT",
        "row_to_device", "list_devices", "availability_map", "query_devices",
        "get_device", "ip_exists", "name_exists", "create_device", "update_device",
        "delete_device", "record_check", "add_event", "recent_checks", "summary",
        "availability_all", "record_metric", "latest_metric", "update_device_snapshot",
        "set_device_metrics_error", "update_device_thresholds", "metric_points",
        "latest_interfaces", "interface_points", "interface_previous",
        "record_interface_stats", "availability", "inventory_summary", "topology_rows",
        "list_maintenance", "create_maintenance", "delete_maintenance",
        "device_in_maintenance",
        "list_groups", "get_group", "create_group", "update_group", "delete_group",
        "group_name_exists", "list_profiles", "upsert_profile", "apply_profile_to_type",
        "PROFILES_DEFAULT_THRESHOLDS",
    ],
    "fiber": [
        "FIBER_SETTING_DEFAULTS", "_FIBER_INT_KEYS", "_LINK_SELECT",
        "get_fiber_settings", "set_fiber_settings",
        "list_optical_profiles", "get_optical_profile", "optical_profile_name_exists",
        "create_optical_profile", "update_optical_profile", "delete_optical_profile",
        "list_fiber_links", "get_fiber_link", "fiber_link_name_exists",
        "create_fiber_link", "update_fiber_link", "delete_fiber_link",
        "fiber_links_using_port",
        "record_optical_readings", "latest_optical_readings", "recent_optical_readings",
        "optical_port_history_raw", "optical_hourly_rows", "optical_hourly_rx_values",
        "upsert_optical_module", "list_optical_modules",
        "get_optical_port_state", "get_optical_port_state_by_id",
        "upsert_optical_port_state", "list_optical_port_states",
        "get_link_state", "save_link_state", "optical_summary_counts",
        "list_fiber_measurements", "get_fiber_measurement",
        "create_fiber_measurement", "update_fiber_measurement", "delete_fiber_measurement",
    ],
    "mikrotik": [
        "MT_MODULE_DEFAULTS", "MT_INTERVAL_DEFAULTS", "MT_SNAPSHOT_TABLES",
        "ENCRYPTED_FIELDS",
        "_mt_bulk_enrich",
        "get_mt_device_settings", "save_mt_device_settings",
        "get_mt_capabilities", "save_mt_capabilities",
        "get_mt_system", "save_mt_system",
        "get_mt_poll_state", "set_mt_poll_state",
        "replace_mt_snapshot", "mark_mt_table_stale", "list_mt_rows", "mt_snapshot_updated_at",
        "record_mt_logs", "list_mt_logs", "mt_log_severity_counts", "mt_login_failure_counts",
        "replace_mt_findings", "list_mt_findings", "get_mt_finding", "set_mt_finding_ignored",
        "mt_finding_count", "record_mt_metrics", "mt_metric_points", "mt_metric_latest",
        "mt_metric_before", "mt_device_counts", "query_mt_devices", "mt_filter_options",
        "mt_summary", "set_device_parent",
        "add_mt_action", "list_mt_actions",
        "create_mt_backup", "list_mt_backups", "latest_mt_backup_at", "get_mt_backup",
        "get_previous_mt_backup", "mt_backup_diff",
        "encrypted_value_stats", "reencrypt_all",
    ],
}

MODULE_DOCS = {
    "connection": "Koneksi SQLite + helper enkripsi (Fernet), lease instance, primitif bersama.",
    "schema": "Skema basis data (DDL) + migrasi idempotent saat init_db().",
    "settings": "Pengaturan aplikasi (tabel settings), info sistem, retensi pesan.",
    "audit": "Jejak audit (audit_log) + umpan aktivitas.",
    "users": "Pengguna & hak akses, batas percobaan login.",
    "problems": "Masalah (problems) terbuka/tertutup, severity, ack.",
    "services": "Layanan & SLA: daftar layanan, pemetaan perangkat, ketersediaan.",
    "alerts": "Kanal notifikasi, template pesan, aturan aksi, action_log.",
    "discovery": "Pemindaian CIDR (Data Collection): pekerjaan scan & hasil.",
    "devices": "Perangkat, host groups, profil, metrik, statistik interface, pemeliharaan, ringkasan, topologi.",
    "fiber": "Fiber optik: pengukuran DDM, modul optik, link, verifikasi.",
    "mikrotik": "MikroTik: snapshot, kapabilitas, logs, temuan, metrik, aksi, backup.",
}

NAME2MODULE = {}
for module, names in BUCKETS.items():
    for name in names:
        NAME2MODULE.setdefault(name, module)

# Urutan import package (tidak ada siklus: mikrotik mengimpor devices/connection)
IMPORT_ORDER = ["connection", "fiber", "schema", "settings", "audit", "users",
                "problems", "services", "alerts", "devices", "discovery", "mikrotik"]

source = open(SRC_PATH, encoding="utf-8").read()
lines = source.splitlines(keepends=True)
tree = ast.parse(source)

CONST_PREFIX = {"BASE_DIR", "DB_PATH", "_lock", "_conn", "_fernet",
                 "_LATEST_METRICS_JOIN", "_LATEST_METRICS_SELECT", "_STATUS_WEIGHT",
                 "SETTING_DEFAULTS", "PROFILES_DEFAULT_THRESHOLDS", "_SEVERITY_RANK",
                 "FIBER_SETTING_DEFAULTS", "_FIBER_INT_KEYS", "_LINK_SELECT",
                 "MT_MODULE_DEFAULTS", "MT_INTERVAL_DEFAULTS", "MT_SNAPSHOT_TABLES",
                 "ENCRYPTED_FIELDS", "_LEASE_STALE_SECONDS"}

segments = []  # (kind, name, start, end) kind: const | func
for node in tree.body:
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        continue
    if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
        continue  # docstring
    if isinstance(node, ast.Assign):
        target = node.targets[0]
        if isinstance(target, ast.Name) and target.id in CONST_PREFIX:
            segments.append(("const", target.id, node.lineno, node.end_lineno))
        continue
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        if node.target.id in CONST_PREFIX:
            segments.append(("const", node.target.id, node.lineno, node.end_lineno))
        continue
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        segments.append(("func", node.name, node.lineno, node.end_lineno))
        continue
    print(f"WARN: mengabaikan statement top-level tidak dikenal di baris {node.lineno}: {ast.dump(node)[:120]}")

assigned = set()
module_segments = {module: [] for module in BUCKETS}
for kind, name, start, end in segments:
    module = NAME2MODULE.get(name)
    if module is None:
        print(f"ERROR: tidak ada bucket untuk {name} (baris {start})")
        sys.exit(1)
    module_segments[module].append((start, end, kind, name))
    assigned.add(name)

missing = [n for n in NAME2MODULE if n not in assigned]
if missing:
    print(f"ERROR: nama tidak ditemukan di db.py: {missing}")
    sys.exit(1)

print("partition OK:", {m: len(v) for m, v in module_segments.items()})


def names_used(body_text):
    tree_ = ast.parse(body_text)
    names = set()
    for node in ast.walk(tree_):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            pass
    return names


def segment_text(start, end):
    return "".join(lines[start - 1:end])


for module in IMPORT_ORDER:
    parts = module_segments[module]
    parts.sort(key=lambda item: item[0])
    body = "".join(segment_text(start, end).rstrip() + "\n\n" for start, end, _kind, _name in parts)
    used = names_used(body)

    # Import stdlib yang relevan
    std_imports = []
    if "json" in used:
        std_imports.append("import json")
    if "os" in used:
        std_imports.append("import os")
    if "sqlite3" in used:
        std_imports.append("import sqlite3")
    if "threading" in used:
        std_imports.append("import threading")
    if "datetime" in used or "timedelta" in used or "timezone" in used:
        std_imports.append("from datetime import datetime, timedelta, timezone")
    if "re" in used:
        std_imports.append("import re")
    if "uuid" in used:
        std_imports.append("import uuid")
    if "Fernet" in used or "InvalidToken" in used:
        std_imports.append("from cryptography.fernet import Fernet, InvalidToken")

    conn_names = sorted(NAME2MODULE[n] == "connection" and n in used and n not in BUCKETS[module] for n in [] )  # unused
    # Nama dari connection yang dipakai (selain yang didefinisikan di sini)
    conn_used = sorted(n for n in used if NAME2MODULE.get(n) == "connection" and NAME2MODULE.get(n) != module)
    cross = {}
    for n in used:
        owner = NAME2MODULE.get(n)
        if owner and owner != module and owner != "connection":
            cross.setdefault(owner, []).append(n)

    header = [f'"""{MODULE_DOCS[module]}"""\n\n']
    header += [line + "\n" for line in std_imports]
    header += ["\n"]
    if conn_used:
        header += ["from .connection import (\n"]
        for n in conn_used:
            header += [f"    {n},\n"]
        header += [")\n"]
    for owner, names in sorted(cross.items()):
        header += [f"from .{owner} import (\n"]
        for n in sorted(set(names)):
            header += [f"    {n},\n"]
        header += [")\n"]

    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, f"{module}.py"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write("".join(header) + "\n" + body)
    print(f"ditulis {OUT_DIR}/{module}.py  ({len(parts)} bagian)")

init_lines = ['"""Package db — me-re-export semua nama publik db.py lama."""\n\n']
init_lines += [f"from . import {module}\n" for module in IMPORT_ORDER]
init_lines += ["\n"]
init_lines += ["for _module in [" + ", ".join(IMPORT_ORDER) + "]:\n"]
init_lines += ["    globals().update({k: v for k, v in vars(_module).items() if not k.startswith('__')})\n"]
init_lines += ["del _module\n"]
with open(os.path.join(OUT_DIR, "__init__.py"), "w", encoding="utf-8", newline="\n") as fh:
    fh.write("".join(init_lines))
print(f"ditulis {OUT_DIR}/__init__.py")
