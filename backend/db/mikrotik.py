"""MikroTik: snapshot, kapabilitas, logs, temuan, metrik, aksi, backup."""

import json

from .connection import (
    _json_loads,
    _lock,
    transaction,
    _now,
    encrypt_secret,
    get_conn,
)
from .devices import (
    _LATEST_METRICS_JOIN,
    _LATEST_METRICS_SELECT,
    get_device,
    row_to_device,
)
from core.validation import QUERY_PAGE_MAX, SQLITE_INT64_MAX, parse_query_int

MT_MODULE_DEFAULTS = {
    "ppp": True,
    "hotspot": True,
    "wireless": True,
    "dhcp": True,
    "queue": True,
    "routing": True,
    "log": True,
    "security": True,
}

MT_INTERVAL_DEFAULTS = {"fast": 60, "medium": 300, "slow": 21600}

MT_SNAPSHOT_TABLES = {
    "mt_dhcp_leases",
    "mt_dhcp_pools",
    "mt_arp",
    "mt_neighbors",
    "mt_routes",
    "mt_routing_neighbors",
    "mt_ppp_sessions",
    "mt_hotspot_sessions",
    "mt_wireless_clients",
    "mt_interfaces",
    "mt_ip_addresses",
    "mt_firewall_rules",
    "mt_address_lists",
    "mt_queues",
    "mt_services",
    "mt_users",
}

def get_mt_device_settings(device_id, include_secrets=False):
    """Pengaturan per perangkat MikroTik; default bila belum ada baris."""
    with _lock:
        row = get_conn().execute(
            "SELECT * FROM mt_device_settings WHERE device_id = ?", (device_id,)
        ).fetchone()
    modules = dict(MT_MODULE_DEFAULTS)
    intervals = dict(MT_INTERVAL_DEFAULTS)
    important = []
    data = {
        "device_id": device_id,
        "modules": modules,
        "intervals": intervals,
        "important_interfaces": important,
        "write_enabled": False,
        "has_write_credentials": False,
        "write_user": None,
        "ssh_enabled": False,
        "has_ssh_credentials": False,
        "ssh_user": None,
        "ssh_host_key": None,
        "backup_enabled": False,
        "backup_hour": 2,
    }
    if row is None:
        return data
    modules.update(_json_loads(row["modules"], {}))
    intervals.update(_json_loads(row["intervals"], {}))
    important = _json_loads(row["important_interfaces"], [])
    data.update(
        {
            "modules": {key: bool(modules.get(key, True)) for key in MT_MODULE_DEFAULTS},
            "intervals": {
                "fast": max(30, int(intervals.get("fast") or 60)),
                "medium": max(60, int(intervals.get("medium") or 300)),
                "slow": max(300, int(intervals.get("slow") or 21600)),
            },
            "important_interfaces": [str(item) for item in important if str(item).strip()],
            "write_enabled": bool(row["write_enabled"]),
            "has_write_credentials": bool(row["write_user"] and row["write_password"]),
            "write_user": row["write_user"],
            "ssh_enabled": bool(row["ssh_enabled"]),
            "has_ssh_credentials": bool(row["ssh_user"] and row["ssh_password"]),
            "ssh_user": row["ssh_user"],
            "ssh_host_key": row["ssh_host_key"],
            "backup_enabled": bool(row["backup_enabled"]),
            "backup_hour": int(row["backup_hour"]) if row["backup_hour"] is not None else 2,
        }
    )
    if include_secrets:
        data["write_password"] = row["write_password"]
        data["write_port"] = row["write_port"]
        data["write_ssl"] = bool(row["write_ssl"])
        data["ssh_password"] = row["ssh_password"]
        data["ssh_port"] = int(row["ssh_port"] or 22)
    return data

def save_mt_device_settings(device_id, data):
    """Simpan sebagian pengaturan per perangkat. Secret kosong = pertahankan."""
    existing = get_mt_device_settings(device_id, include_secrets=True)
    now = _now()
    modules = dict(existing["modules"])
    intervals = dict(existing["intervals"])
    if isinstance(data.get("modules"), dict):
        for key in MT_MODULE_DEFAULTS:
            if key in data["modules"]:
                modules[key] = bool(data["modules"][key])
    if isinstance(data.get("intervals"), dict):
        for key in MT_INTERVAL_DEFAULTS:
            if key in data["intervals"]:
                try:
                    intervals[key] = max(30, int(data["intervals"][key]))
                except (TypeError, ValueError):
                    pass
    important = existing["important_interfaces"]
    if isinstance(data.get("important_interfaces"), list):
        important = [str(item).strip()[:120] for item in data["important_interfaces"] if str(item).strip()]

    def secret(key):
        value = data.get(key)
        if value is None:
            return existing.get(key)
        value = str(value).strip()
        return encrypt_secret(value) if value else existing.get(key)

    write_user = data.get("write_user", existing.get("write_user"))
    if write_user is not None:
        write_user = str(write_user).strip()[:120] or None
    ssh_user = data.get("ssh_user", existing.get("ssh_user"))
    if ssh_user is not None:
        ssh_user = str(ssh_user).strip()[:120] or None

    with transaction() as conn:
        conn.execute(
            """
            INSERT INTO mt_device_settings (
                device_id, modules, intervals, important_interfaces,
                write_enabled, write_user, write_password, write_port, write_ssl,
                ssh_enabled, ssh_port, ssh_user, ssh_password, ssh_host_key,
                backup_enabled, backup_hour, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(device_id) DO UPDATE SET
                modules=excluded.modules, intervals=excluded.intervals,
                important_interfaces=excluded.important_interfaces,
                write_enabled=excluded.write_enabled, write_user=excluded.write_user,
                write_password=excluded.write_password, write_port=excluded.write_port,
                write_ssl=excluded.write_ssl, ssh_enabled=excluded.ssh_enabled,
                ssh_port=excluded.ssh_port, ssh_user=excluded.ssh_user,
                ssh_password=excluded.ssh_password, ssh_host_key=excluded.ssh_host_key,
                backup_enabled=excluded.backup_enabled, backup_hour=excluded.backup_hour,
                updated_at=excluded.updated_at
            """,
            (
                device_id,
                json.dumps(modules),
                json.dumps(intervals),
                json.dumps(important),
                1 if data.get("write_enabled", existing.get("write_enabled")) else 0,
                write_user,
                secret("write_password"),
                int(data.get("write_port") or existing.get("write_port") or 0) or None,
                1 if data.get("write_ssl", existing.get("write_ssl")) else 0,
                1 if data.get("ssh_enabled", existing.get("ssh_enabled")) else 0,
                int(data.get("ssh_port") or existing.get("ssh_port") or 22),
                ssh_user,
                secret("ssh_password"),
                data.get("ssh_host_key", existing.get("ssh_host_key")) or None,
                1 if data.get("backup_enabled", existing.get("backup_enabled")) else 0,
                int(data.get("backup_hour") if data.get("backup_hour") is not None
                    else (existing.get("backup_hour") if existing.get("backup_hour") is not None else 2)),
                now,
            ),
        )
    return get_mt_device_settings(device_id)

def get_mt_capabilities(device_id):
    with _lock:
        row = get_conn().execute(
            "SELECT * FROM mt_capabilities WHERE device_id = ?", (device_id,)
        ).fetchone()
    if row is None:
        return {"version": None, "major": None, "packages": [], "probes": {}, "checked_at": None}
    return {
        "version": row["routeros_version"],
        "major": row["major_version"],
        "packages": _json_loads(row["packages"], []),
        "probes": _json_loads(row["capabilities"], {}),
        "checked_at": row["checked_at"],
    }

def save_mt_capabilities(device_id, capabilities):
    now = _now()
    caps = capabilities or {}
    with transaction() as conn:
        conn.execute(
            """
            INSERT INTO mt_capabilities
                (device_id, routeros_version, major_version, packages, capabilities, checked_at, updated_at)
            VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(device_id) DO UPDATE SET
                routeros_version=excluded.routeros_version,
                major_version=excluded.major_version,
                packages=excluded.packages,
                capabilities=excluded.capabilities,
                checked_at=excluded.checked_at,
                updated_at=excluded.updated_at
            """,
            (
                device_id, caps.get("version"), caps.get("major"),
                json.dumps(caps.get("packages") or [], ensure_ascii=False),
                json.dumps(caps.get("probes") or {}, ensure_ascii=False),
                caps.get("checked_at") or now, now,
            ),
        )

def get_mt_system(device_id):
    with _lock:
        row = get_conn().execute(
            "SELECT * FROM mt_system WHERE device_id = ?", (device_id,)
        ).fetchone()
    if row is None:
        return None
    data = dict(row)
    data["packages"] = _json_loads(data.get("packages"), [])
    return data

def save_mt_system(device_id, system):
    now = _now()
    system = system or {}
    with transaction() as conn:
        conn.execute(
            """
            INSERT INTO mt_system (
                device_id, identity, model, serial, version, major, board, architecture,
                current_firmware, upgrade_firmware, firmware_status, license_level,
                license_expires, clock_time, clock_date, gmt_offset, time_zone_name,
                router_utc, drift_seconds, packages, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(device_id) DO UPDATE SET
                identity=excluded.identity, model=excluded.model, serial=excluded.serial,
                version=excluded.version, major=excluded.major, board=excluded.board,
                architecture=excluded.architecture, current_firmware=excluded.current_firmware,
                upgrade_firmware=excluded.upgrade_firmware, firmware_status=excluded.firmware_status,
                license_level=excluded.license_level, license_expires=excluded.license_expires,
                clock_time=excluded.clock_time, clock_date=excluded.clock_date,
                gmt_offset=excluded.gmt_offset, time_zone_name=excluded.time_zone_name,
                router_utc=excluded.router_utc, drift_seconds=excluded.drift_seconds,
                packages=excluded.packages, updated_at=excluded.updated_at
            """,
            (
                device_id, system.get("identity"), system.get("model"), system.get("serial"),
                system.get("version"), system.get("major"), system.get("board"),
                system.get("architecture"), system.get("current_firmware"),
                system.get("upgrade_firmware"), system.get("firmware_status"),
                system.get("license_level"), system.get("license_expires"),
                system.get("clock_time"), system.get("clock_date"), system.get("gmt_offset"),
                system.get("time_zone_name"), system.get("router_utc"),
                system.get("drift_seconds"),
                json.dumps(system.get("packages") or [], ensure_ascii=False), now,
            ),
        )

def get_mt_poll_state(device_id, layer=None):
    with _lock:
        if layer:
            rows = get_conn().execute(
                "SELECT * FROM mt_poll_state WHERE device_id = ? AND layer = ?",
                (device_id, layer),
            ).fetchall()
        else:
            rows = get_conn().execute(
                "SELECT * FROM mt_poll_state WHERE device_id = ? ORDER BY layer",
                (device_id,),
            ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["cursor"] = _json_loads(item.get("cursor"), {})
        item["truncated"] = _json_loads(item.get("truncated"), {})
        result.append(item)
    return result[0] if layer and result else (None if layer else result)

def set_mt_poll_state(device_id, layer, *, last_run, ok, error=None, cursor=None, truncated=None):
    previous = get_mt_poll_state(device_id, layer) or {}
    with transaction() as conn:
        conn.execute(
            """
            INSERT INTO mt_poll_state
                (device_id, layer, last_run, last_ok, last_error, retry_after, cursor, truncated)
            VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(device_id, layer) DO UPDATE SET
                last_run=excluded.last_run,
                last_ok=COALESCE(excluded.last_ok, mt_poll_state.last_ok),
                last_error=excluded.last_error,
                retry_after=excluded.retry_after,
                cursor=excluded.cursor,
                truncated=excluded.truncated
            """,
            (
                device_id, layer, last_run, last_run if ok else None,
                error, None,
                json.dumps(cursor if cursor is not None else previous.get("cursor") or {}),
                json.dumps(truncated if truncated is not None else previous.get("truncated") or {}),
            ),
        )

def replace_mt_snapshot(table, device_id, rows, ts, *, truncated=False, scope_prefix=None):
    """Ganti snapshot satu tabel secara upsert; baris yang hilang dibuang.

    ``scope_prefix`` membatasi penggantian pada kelompok baris tertentu
    (mis. hanya rute IPv4) agar kegagalan satu sub-modul tidak menghapus
    data sub-modul lain di tabel yang sama.

    Saat ``truncated`` baris lama tetap ditandai basi (tidak dihapus) agar
    pengguna melihat bahwa data dipotong, bukan seolah lengkap.
    """
    if table not in MT_SNAPSHOT_TABLES:
        raise ValueError(f"Tabel snapshot MikroTik tidak dikenal: {table}")
    records = []
    for row in rows or []:
        row_key = row.get("row_key")
        if not row_key:
            continue
        data = {key: value for key, value in row.items() if key != "row_key"}
        records.append((device_id, row_key, ts, 0, json.dumps(data, ensure_ascii=False)))
    scope_sql, scope_params = "", []
    if scope_prefix:
        scope_sql = " AND row_key LIKE ?"
        scope_params = [f"{scope_prefix}%"]
    with transaction() as conn:
        conn.execute(
            f"UPDATE {table} SET stale = 1 WHERE device_id = ?{scope_sql}",
            (device_id, *scope_params),
        )
        if records:
            conn.executemany(
                f"""
                INSERT INTO {table} (device_id, row_key, polled_at, stale, data)
                VALUES (?,?,?,?,?)
                ON CONFLICT(device_id, row_key) DO UPDATE SET
                    polled_at=excluded.polled_at, stale=0, data=excluded.data
                """,
                records,
            )
        if not truncated:
            conn.execute(
                f"DELETE FROM {table} WHERE device_id = ? AND stale = 1{scope_sql}",
                (device_id, *scope_params),
            )

def mark_mt_table_stale(table, device_id, scope_prefix=None):
    if table not in MT_SNAPSHOT_TABLES:
        return
    scope_sql, scope_params = "", []
    if scope_prefix:
        scope_sql = " AND row_key LIKE ?"
        scope_params = [f"{scope_prefix}%"]
    with transaction() as conn:
        conn.execute(
            f"UPDATE {table} SET stale = 1 WHERE device_id = ?{scope_sql}",
            (device_id, *scope_params),
        )

def list_mt_rows(table, device_id, *, include_stale=True):
    if table not in MT_SNAPSHOT_TABLES:
        raise ValueError(f"Tabel snapshot MikroTik tidak dikenal: {table}")
    where = "device_id = ?" + ("" if include_stale else " AND stale = 0")
    with _lock:
        rows = get_conn().execute(
            f"SELECT row_key, polled_at, stale, data FROM {table} WHERE {where}",
            (device_id,),
        ).fetchall()
    result = []
    for row in rows:
        data = _json_loads(row["data"], {})
        data["_row_key"] = row["row_key"]
        data["_polled_at"] = row["polled_at"]
        data["_stale"] = bool(row["stale"])
        result.append(data)
    return result

def mt_snapshot_updated_at(table, device_id):
    if table not in MT_SNAPSHOT_TABLES:
        return None
    with _lock:
        row = get_conn().execute(
            f"SELECT MAX(polled_at) AS ts FROM {table} WHERE device_id = ?", (device_id,)
        ).fetchone()
    return row["ts"] if row else None

def record_mt_logs(device_id, rows, polled_at=None):
    if not rows:
        return 0
    ts = polled_at or _now()
    with transaction() as conn:
        cursor = conn.executemany(
            """
            INSERT OR IGNORE INTO mt_logs (
                device_id, row_key, polled_at, log_time, topics, severity, message,
                is_login_failure, username, source_ip, service, stale
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,0)
            """,
            [
                (
                    device_id, row.get("row_key"), ts, row.get("log_time"), row.get("topics"),
                    row.get("severity"), row.get("message"), int(row.get("is_login_failure") or 0),
                    row.get("username"), row.get("source_ip"), row.get("service"),
                )
                for row in rows
                if row.get("row_key")
            ],
        )
        return cursor.rowcount

def list_mt_logs(device_id, *, severity=None, topics=None, search=None, limit=200, offset=0):
    where = ["device_id = ?"]
    params = [device_id]
    if severity:
        where.append("severity = ?")
        params.append(severity)
    if topics:
        where.append("lower(COALESCE(topics, '')) LIKE ?")
        params.append(f"%{topics.lower()}%")
    if search:
        like = f"%{search.strip().lower()}%"
        where.append("(lower(COALESCE(message, '')) LIKE ? OR lower(COALESCE(username, '')) LIKE ? OR lower(COALESCE(source_ip, '')) LIKE ?)")
        params.extend([like, like, like])
    where_sql = " AND ".join(where)
    with _lock:
        total = get_conn().execute(
            f"SELECT COUNT(*) AS n FROM mt_logs WHERE {where_sql}", params
        ).fetchone()["n"]
        rows = get_conn().execute(
            f"SELECT * FROM mt_logs WHERE {where_sql} ORDER BY id DESC LIMIT ? OFFSET ?",
            [
                *params,
                parse_query_int(limit, 200, min_value=1, max_value=1000),
                parse_query_int(offset, 0, min_value=0, max_value=SQLITE_INT64_MAX),
            ],
        ).fetchall()
    return [dict(row) for row in rows], total

def mt_log_severity_counts(device_id):
    with _lock:
        rows = get_conn().execute(
            "SELECT COALESCE(severity, 'info') AS severity, COUNT(*) AS n"
            " FROM mt_logs WHERE device_id = ? GROUP BY severity",
            (device_id,),
        ).fetchall()
    return {row["severity"]: row["n"] for row in rows}

def mt_login_failure_counts(device_id, since):
    with _lock:
        rows = get_conn().execute(
            """
            SELECT source_ip, COUNT(*) AS failures, MAX(polled_at) AS last_seen,
                   COUNT(DISTINCT username) AS usernames
            FROM mt_logs
            WHERE device_id = ? AND is_login_failure = 1 AND polled_at >= ?
                  AND source_ip IS NOT NULL
            GROUP BY source_ip ORDER BY failures DESC
            """,
            (device_id, since),
        ).fetchall()
    return [dict(row) for row in rows]

def replace_mt_findings(device_id, findings, detected_at=None):
    now = detected_at or _now()
    with transaction() as conn:
        conn.execute("UPDATE mt_security_findings SET stale = 1 WHERE device_id = ?", (device_id,))
        for finding in findings or []:
            key = finding.get("finding_key")
            if not key:
                continue
            conn.execute(
                """
                INSERT INTO mt_security_findings (
                    device_id, finding_key, category, severity, title, detail, remediation,
                    detected_at, last_seen, ignored, stale
                ) VALUES (?,?,?,?,?,?,?,?,?,0,0)
                ON CONFLICT(device_id, finding_key) DO UPDATE SET
                    category=excluded.category, severity=excluded.severity,
                    title=excluded.title, detail=excluded.detail,
                    remediation=excluded.remediation, last_seen=excluded.last_seen,
                    stale=0
                """,
                (
                    device_id, key, finding.get("category"), finding.get("severity"),
                    finding.get("title"), finding.get("detail"), finding.get("remediation"),
                    now, now,
                ),
            )
        conn.execute(
            "DELETE FROM mt_security_findings WHERE device_id = ? AND stale = 1", (device_id,)
        )

def list_mt_findings(device_id, *, include_ignored=True, include_stale=False):
    where = ["device_id = ?"]
    params = [device_id]
    if not include_ignored:
        where.append("ignored = 0")
    if not include_stale:
        where.append("stale = 0")
    with _lock:
        rows = get_conn().execute(
            f"SELECT * FROM mt_security_findings WHERE {' AND '.join(where)}"
            " ORDER BY CASE severity WHEN 'high' THEN 0 WHEN 'average' THEN 1"
            " WHEN 'warning' THEN 2 ELSE 3 END, title",
            params,
        ).fetchall()
    return [dict(row) for row in rows]

def get_mt_finding(finding_id):
    with _lock:
        row = get_conn().execute(
            "SELECT * FROM mt_security_findings WHERE id = ?", (finding_id,)
        ).fetchone()
    return dict(row) if row else None

def set_mt_finding_ignored(finding_id, *, ignored, username, reason=None):
    with transaction() as conn:
        cursor = conn.execute(
            """
            UPDATE mt_security_findings
            SET ignored=?, ignored_by=?, ignored_at=?, ignore_reason=?
            WHERE id=?
            """,
            (
                1 if ignored else 0,
                username if ignored else None,
                _now() if ignored else None,
                (reason or "")[:300] if ignored else None,
                finding_id,
            ),
        )
        return cursor.rowcount > 0

def mt_finding_count(device_id, *, open_only=True):
    where = ["device_id = ?", "stale = 0"]
    if open_only:
        where.append("ignored = 0")
    with _lock:
        row = get_conn().execute(
            f"SELECT COUNT(*) AS n FROM mt_security_findings WHERE {' AND '.join(where)}",
            (device_id,),
        ).fetchone()
    return row["n"]

def record_mt_metrics(device_id, ts, values):
    if not values:
        return
    with transaction() as conn:
        conn.executemany(
            "INSERT INTO mt_metric_history (device_id, ts, metric, value) VALUES (?,?,?,?)",
            [(device_id, ts, key, value) for key, value in values.items() if value is not None],
        )

def mt_metric_points(device_id, metric, since, limit=10000):
    with _lock:
        rows = get_conn().execute(
            "SELECT ts, value FROM mt_metric_history WHERE device_id = ? AND metric = ?"
            " AND ts >= ? ORDER BY ts ASC LIMIT ?",
            (device_id, metric, since, limit),
        ).fetchall()
    return [{"ts": row["ts"], "value": row["value"]} for row in rows]

def mt_metric_latest(device_id, metric):
    with _lock:
        row = get_conn().execute(
            "SELECT ts, value FROM mt_metric_history WHERE device_id = ? AND metric = ?"
            " ORDER BY ts DESC, id DESC LIMIT 1",
            (device_id, metric),
        ).fetchone()
    return dict(row) if row else None

def mt_metric_before(device_id, metric, before_ts):
    with _lock:
        row = get_conn().execute(
            "SELECT ts, value FROM mt_metric_history WHERE device_id = ? AND metric = ?"
            " AND ts <= ? ORDER BY ts DESC, id DESC LIMIT 1",
            (device_id, metric, before_ts),
        ).fetchone()
    return dict(row) if row else None

def mt_device_counts(device_id):
    with _lock:
        row = get_conn().execute(
            """
            SELECT
              (SELECT COUNT(*) FROM mt_ppp_sessions WHERE device_id=? AND stale=0) AS ppp_active,
              (SELECT COUNT(*) FROM mt_hotspot_sessions WHERE device_id=? AND stale=0) AS hotspot_active,
              (SELECT COUNT(*) FROM mt_wireless_clients WHERE device_id=? AND stale=0) AS wireless_clients,
              (SELECT COUNT(*) FROM mt_dhcp_leases WHERE device_id=? AND stale=0) AS dhcp_leases,
              (SELECT COUNT(*) FROM mt_arp WHERE device_id=? AND stale=0) AS arp_entries,
              (SELECT COUNT(*) FROM mt_routes WHERE device_id=? AND stale=0) AS routes,
              (SELECT COUNT(*) FROM mt_firewall_rules WHERE device_id=? AND stale=0) AS firewall_rules,
              (SELECT COUNT(*) FROM mt_address_lists WHERE device_id=? AND stale=0) AS address_lists,
              (SELECT COUNT(*) FROM mt_queues WHERE device_id=? AND stale=0) AS queues,
              (SELECT COUNT(*) FROM mt_services WHERE device_id=? AND stale=0) AS services,
              (SELECT COUNT(*) FROM mt_users WHERE device_id=? AND stale=0) AS router_users,
              (SELECT COUNT(*) FROM mt_security_findings WHERE device_id=? AND stale=0 AND ignored=0) AS security_open
            """,
            (device_id,) * 12,
        ).fetchone()
    data = dict(row)
    latest = mt_metric_latest(device_id, "connections")
    data["connections"] = latest["value"] if latest else None
    data["active_sessions"] = (data.get("ppp_active") or 0) + (data.get("hotspot_active") or 0)
    return data

def _mt_bulk_enrich(device_ids):
    """Enrichment armada MikroTik untuk banyak perangkat sekaligus (hindari N+1)."""
    enrich = {device_id: {
        "system": None,
        "counts": {key: 0 for key in (
            "ppp_active", "hotspot_active", "wireless_clients", "dhcp_leases",
            "arp_entries", "routes", "firewall_rules", "address_lists",
            "queues", "services", "router_users", "security_open",
        )} | {"connections": None},
        "interfaces_up": 0, "interfaces_total": 0,
        "open_problems": 0, "capabilities_checked_at": None,
    } for device_id in device_ids}
    if not device_ids:
        return enrich
    marks = ",".join("?" * len(device_ids))
    params = list(device_ids)
    with _lock:
        conn = get_conn()
        for table, key in (
            ("mt_ppp_sessions", "ppp_active"), ("mt_hotspot_sessions", "hotspot_active"),
            ("mt_wireless_clients", "wireless_clients"), ("mt_dhcp_leases", "dhcp_leases"),
            ("mt_arp", "arp_entries"), ("mt_routes", "routes"),
            ("mt_firewall_rules", "firewall_rules"), ("mt_address_lists", "address_lists"),
            ("mt_queues", "queues"), ("mt_services", "services"),
            ("mt_users", "router_users"), ("mt_security_findings", "security_open"),
        ):
            extra = " AND ignored = 0" if table == "mt_security_findings" else ""
            for row in conn.execute(
                f"SELECT device_id, COUNT(*) AS n FROM {table} WHERE stale = 0{extra}"
                f" AND device_id IN ({marks}) GROUP BY device_id", params,
            ).fetchall():
                enrich[row["device_id"]]["counts"][key] = row["n"]
        for row in conn.execute(
            f"SELECT h.device_id, h.value FROM mt_metric_history h"
            f" JOIN (SELECT device_id, MAX(id) AS last_id FROM mt_metric_history"
            f" WHERE metric = 'connections' AND device_id IN ({marks}) GROUP BY device_id) m"
            f" ON m.last_id = h.id", params,
        ).fetchall():
            enrich[row["device_id"]]["counts"]["connections"] = row["value"]
        for row in conn.execute(
            f"SELECT * FROM mt_system WHERE device_id IN ({marks})", params,
        ).fetchall():
            enrich[row["device_id"]]["system"] = dict(row)
        for row in conn.execute(
            f"SELECT device_id, checked_at FROM mt_capabilities WHERE device_id IN ({marks})", params,
        ).fetchall():
            enrich[row["device_id"]]["capabilities_checked_at"] = row["checked_at"]
        for row in conn.execute(
            f"SELECT device_id, COUNT(*) AS n FROM problems WHERE status = 'open'"
            f" AND device_id IN ({marks}) GROUP BY device_id", params,
        ).fetchall():
            enrich[row["device_id"]]["open_problems"] = row["n"]
        for row in conn.execute(
            f"SELECT latest.device_id, COUNT(*) AS total,"
            f" SUM(CASE WHEN s.running = 1 AND s.disabled = 0 THEN 1 ELSE 0 END) AS up"
            f" FROM (SELECT device_id, if_name, MAX(id) AS last_id FROM interface_stats"
            f" WHERE device_id IN ({marks}) GROUP BY device_id, if_name) latest"
            f" JOIN interface_stats s ON s.id = latest.last_id"
            f" GROUP BY latest.device_id", params,
        ).fetchall():
            enrich[row["device_id"]]["interfaces_up"] = row["up"] or 0
            enrich[row["device_id"]]["interfaces_total"] = row["total"] or 0
    return enrich

def query_mt_devices(search="", status="", version="", firmware="", model="", page=1, per_page=25):
    """Daftar perangkat MikroTik dengan filter/pagination sisi server."""
    page = parse_query_int(page, 1, min_value=1, max_value=QUERY_PAGE_MAX)
    per_page = parse_query_int(per_page, 25, min_value=1, max_value=100)
    where = ["(devices.type = 'mikrotik' OR devices.mikrotik_enabled = 1)"]
    params = []
    if search:
        like = f"%{search.strip().lower()}%"
        where.append(
            "(lower(devices.name) LIKE ? OR lower(devices.ip) LIKE ?"
            " OR lower(COALESCE(devices.mikrotik_model,'')) LIKE ?"
            " OR lower(COALESCE(devices.routeros_version,'')) LIKE ?"
            " OR lower(devices.location) LIKE ?)"
        )
        params.extend([like, like, like, like, like])
    if status:
        where.append("devices.status = ?")
        params.append(status)
    if version:
        where.append("COALESCE(devices.routeros_version, '') = ?")
        params.append(version)
    if model:
        where.append("COALESCE(devices.mikrotik_model, '') = ?")
        params.append(model)
    if firmware:
        where.append("COALESCE(sys.firmware_status, 'unknown') = ?")
        params.append(firmware)
    where_sql = "WHERE " + " AND ".join(where)
    sys_join = " LEFT JOIN mt_system AS sys ON sys.device_id = devices.id"
    with _lock:
        total = get_conn().execute(
            f"SELECT COUNT(*) AS n FROM devices {_LATEST_METRICS_JOIN} {sys_join} {where_sql}",
            params,
        ).fetchone()["n"]
        rows = get_conn().execute(
            f"SELECT {_LATEST_METRICS_SELECT}, sys.firmware_status AS _firmware_status"
            f" FROM devices {_LATEST_METRICS_JOIN} {sys_join} {where_sql}"
            " ORDER BY devices.name COLLATE NOCASE LIMIT ? OFFSET ?",
            [*params, per_page, (page - 1) * per_page],
        ).fetchall()
    enrich = _mt_bulk_enrich([row["id"] for row in rows])
    devices = []
    for row in rows:
        device = row_to_device(row)
        item = enrich.get(device["id"]) or _mt_bulk_enrich([device["id"]])[device["id"]]
        system = item["system"] or None
        counts = dict(item["counts"])
        counts["active_sessions"] = (counts.get("ppp_active") or 0) + (counts.get("hotspot_active") or 0)
        firmware_state = (system or {}).get("firmware_status") or "unknown"
        device.update(
            {
                "system": system or None,
                "counts": counts,
                "interfaces_up": item["interfaces_up"],
                "interfaces_total": item["interfaces_total"],
                "open_problems": item["open_problems"],
                "security_open": counts.get("security_open", 0),
                "firmware_status": firmware_state,
                "capabilities_checked_at": item["capabilities_checked_at"],
                "active_sessions": counts.get("active_sessions", 0),
            }
        )
        devices.append(device)
    return {
        "devices": devices,
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": max(1, -(-total // per_page)),
    }

def mt_filter_options():
    with _lock:
        rows = get_conn().execute(
            """
            SELECT DISTINCT routeros_version, mikrotik_model
            FROM devices
            WHERE (type = 'mikrotik' OR mikrotik_enabled = 1)
              AND (COALESCE(routeros_version, '') != '' OR COALESCE(mikrotik_model, '') != '')
            """
        ).fetchall()
    versions = sorted({row["routeros_version"] for row in rows if row["routeros_version"]})
    models = sorted({row["mikrotik_model"] for row in rows if row["mikrotik_model"]})
    return {"versions": versions, "models": models}

def mt_summary():
    with _lock:
        row = get_conn().execute(
            "SELECT COUNT(*) AS routers FROM devices"
            " WHERE type = 'mikrotik' OR mikrotik_enabled = 1"
        ).fetchone()
        pending = get_conn().execute(
            """
            SELECT COUNT(*) AS n FROM mt_system s JOIN devices d ON d.id = s.device_id
            WHERE (d.type = 'mikrotik' OR d.mikrotik_enabled = 1)
              AND s.firmware_status = 'pending'
            """
        ).fetchone()["n"]
        findings = get_conn().execute(
            """
            SELECT COUNT(*) AS n FROM mt_security_findings f JOIN devices d ON d.id = f.device_id
            WHERE (d.type = 'mikrotik' OR d.mikrotik_enabled = 1)
              AND f.stale = 0 AND f.ignored = 0
            """
        ).fetchone()["n"]
        sessions = get_conn().execute(
            """
            SELECT
              (SELECT COUNT(*) FROM mt_ppp_sessions s JOIN devices d ON d.id=s.device_id
                 WHERE s.stale=0 AND (d.type='mikrotik' OR d.mikrotik_enabled=1)) +
              (SELECT COUNT(*) FROM mt_hotspot_sessions s JOIN devices d ON d.id=s.device_id
                 WHERE s.stale=0 AND (d.type='mikrotik' OR d.mikrotik_enabled=1)) AS n
            """
        ).fetchone()["n"]
    return {
        "routers": row["routers"],
        "firmware_pending": pending,
        "security_open": findings,
        "active_sessions": sessions or 0,
    }

def set_device_parent(child_id, parent_id, username):
    """Terapkan saran topologi. Return (ok, message)."""
    if child_id == parent_id:
        return False, "Perangkat tidak boleh menjadi induk dirinya sendiri."
    child = get_device(child_id)
    parent = get_device(parent_id)
    if child is None or parent is None:
        return False, "Perangkat tidak ditemukan."
    # Cegah siklus sederhana: parent tidak boleh berada di bawah child.
    seen = set()
    cursor = parent
    while cursor is not None and cursor.get("parent_id") and cursor["id"] not in seen:
        seen.add(cursor["id"])
        if cursor["parent_id"] == child_id:
            return False, "Relasi ini akan membentuk siklus topologi."
        cursor = get_device(cursor["parent_id"])
    with transaction() as conn:
        conn.execute(
            "UPDATE devices SET parent_id=?, updated_at=? WHERE id=?",
            (parent_id, _now(), child_id),
        )
    return True, f"Menjadikan {parent['name']} induk dari {child['name']}."

def add_mt_action(device_id, *, username, action, target, params, result, verified, before, after, ip="-"):
    with transaction() as conn:
        cursor = conn.execute(
            """
            INSERT INTO mt_actions
                (device_id, ts, username, action, target, params, result, verified,
                 before_data, after_data, ip)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                device_id, _now(), username, action, target,
                json.dumps(params or {}, ensure_ascii=False), result,
                None if verified is None else (1 if verified else 0),
                json.dumps(before, ensure_ascii=False, default=str) if before is not None else None,
                json.dumps(after, ensure_ascii=False, default=str) if after is not None else None,
                ip,
            ),
        )
        return cursor.lastrowid

def list_mt_actions(device_id, limit=100):
    with _lock:
        rows = get_conn().execute(
            "SELECT * FROM mt_actions WHERE device_id = ? ORDER BY id DESC LIMIT ?",
            (device_id, min(500, max(1, int(limit)))),
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        for key in ("params", "before_data", "after_data"):
            item[key] = _json_loads(item.get(key), {}) if item.get(key) else None
        item["verified"] = None if item["verified"] is None else bool(item["verified"])
        result.append(item)
    return result

