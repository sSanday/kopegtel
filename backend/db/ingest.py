"""E1 — penyimpanan syslog/trap, konfigurasi penerima, dan aturan kejadian.

Semua data dari UDP melewati modul ini. Tidak ada teks luar yang dieksekusi;
pesan hanya disimpan dan di-escape oleh klien saat ditampilkan.
"""

import hashlib
import json
from datetime import datetime, timedelta, timezone

from .connection import (
    _lock,
    transaction,
    _now,
    decrypt_secret,
    encrypt_secret,
    get_conn,
)

_SEVERITY_RANK = {"disaster": 5, "high": 4, "average": 3, "warning": 2, "information": 1}
_SYSLOG_SEVERITY = {
    0: "disaster", 1: "high", 2: "high", 3: "average",
    4: "warning", 5: "warning", 6: "information", 7: "information",
}


def dedup_hash(device_id, ts, message):
    """Kunci dedup: perangkat + menit + hash pesan (bukan isi mentah)."""
    minute = str(ts or "")[:16]
    digest = hashlib.sha256(str(message or "").strip().encode("utf-8", "replace")).hexdigest()
    return hashlib.sha256(f"{device_id}|{minute}|{digest}".encode("utf-8")).hexdigest()


def syslog_severity_name(value):
    try:
        return _SYSLOG_SEVERITY.get(int(value), "information")
    except (TypeError, ValueError):
        return "information"


# ---------------------------------------------------------
# Penerima (konfigurasi terenkripsi)
# ---------------------------------------------------------

def list_ingest_receivers(include_secret=False):
    with _lock:
        rows = get_conn().execute("SELECT * FROM ingest_receivers ORDER BY kind").fetchall()
    result = []
    for row in rows:
        try:
            config = json.loads(decrypt_secret(row["config"])) if row["config"] else {}
        except Exception:
            config = {}
        item = {
            "id": row["id"], "kind": row["kind"], "enabled": bool(row["enabled"]),
            "port": row["port"], "bind_address": row["bind_address"],
            "updated_at": row["updated_at"],
        }
        if include_secret:
            item["config"] = config
        else:
            item["has_communities"] = bool(config.get("communities"))
            item["communities_count"] = len(config.get("communities") or [])
            item["allow_v1"] = bool(config.get("allow_v1"))
            item["has_v3_users"] = bool(config.get("v3_users"))
        result.append(item)
    return result


def get_ingest_receiver(kind, include_secret=False):
    for item in list_ingest_receivers(include_secret=include_secret):
        if item["kind"] == kind:
            return item
    return None


def update_ingest_receiver(kind, *, enabled, port, bind_address, config=None):
    now = _now()
    existing = get_ingest_receiver(kind, include_secret=True)
    if existing is None:
        return False
    merged = dict(existing.get("config") or {})
    if config is not None:
        for key, value in config.items():
            if key == "communities":
                merged[key] = list(value or [])  # daftar kosong = hapus semua
            else:
                merged[key] = value
    with transaction() as conn:
        cursor = conn.execute(
            "UPDATE ingest_receivers SET enabled=?, port=?, bind_address=?, config=?, updated_at=?"
            " WHERE kind=?",
            (
                1 if enabled else 0, int(port), (bind_address or "0.0.0.0").strip(),
                encrypt_secret(json.dumps(merged, ensure_ascii=False)) if merged else "",
                now, kind,
            ),
        )
        return cursor.rowcount > 0


# ---------------------------------------------------------
# Data masuk (batch)
# ---------------------------------------------------------

def add_syslog_events_batch(rows):
    if not rows:
        return 0
    payload = []
    for row in rows:
        message = str(row.get("message") or "")[:8192]
        ts = row.get("ts") or _now()
        payload.append((
            ts, row.get("received_at") or _now(), row["device_id"], row.get("source_ip") or "-",
            (row.get("hostname") or "")[:255] or None, row.get("facility"), row.get("severity"),
            (row.get("tag") or "")[:255] or None, message, (row.get("raw") or "")[:8192] or None,
            row.get("dedup_hash") or dedup_hash(row["device_id"], ts, message),
        ))
    with transaction() as conn:
        conn.executemany(
            "INSERT INTO syslog_events (ts, received_at, device_id, source_ip, hostname, facility,"
            " severity, tag, message, raw, dedup_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            payload,
        )
    return len(payload)


def add_snmp_traps_batch(rows):
    if not rows:
        return 0
    payload = []
    for row in rows:
        ts = row.get("ts") or _now()
        varbinds = row.get("varbinds")
        if not isinstance(varbinds, str):
            varbinds = json.dumps(varbinds or [], ensure_ascii=False)
        payload.append((
            ts, row.get("received_at") or _now(), row["device_id"], row.get("source_ip") or "-",
            str(row.get("trap_oid") or "")[:255], (row.get("trap_name") or "")[:120] or None,
            varbinds[:16384], (row.get("raw") or "")[:8192] or None,
            row.get("dedup_hash") or dedup_hash(row["device_id"], ts, varbinds),
        ))
    with transaction() as conn:
        conn.executemany(
            "INSERT INTO snmp_traps (ts, received_at, device_id, source_ip, trap_oid, trap_name,"
            " varbinds, raw, dedup_hash) VALUES (?,?,?,?,?,?,?,?,?)",
            payload,
        )
    return len(payload)


def _time_filters(where, params, start, end):
    if start:
        where.append("e.ts >= ?")
        params.append(start)
    if end:
        where.append("e.ts <= ?")
        params.append(end)


def list_syslog_events(*, device_id=None, severity=None, q=None, start=None, end=None,
                       page=1, per_page=50, limit=None):
    where, params = ["1=1"], []
    if device_id:
        where.append("e.device_id = ?")
        params.append(int(device_id))
    if severity not in (None, ""):
        where.append("e.severity = ?")
        params.append(int(severity))
    if q:
        where.append("(lower(e.message) LIKE ? OR lower(COALESCE(e.tag, '')) LIKE ?)")
        like = f"%{str(q).strip().lower()}%"
        params.extend([like, like])
    _time_filters(where, params, start, end)
    where_sql = " AND ".join(where)
    per_page = min(500, max(1, int(per_page or 50)))
    page = max(1, int(page or 1))
    with _lock:
        conn = get_conn()
        total = conn.execute(
            f"SELECT COUNT(*) AS n FROM syslog_events e WHERE {where_sql}", params
        ).fetchone()["n"]
        sql = (
            "SELECT e.*, d.name AS device_name, d.ip AS device_ip FROM syslog_events e"
            f" JOIN devices d ON d.id = e.device_id WHERE {where_sql}"
            " ORDER BY e.id DESC LIMIT ? OFFSET ?"
        )
        if limit is not None:
            rows = conn.execute(sql, [*params, min(10000, int(limit)), 0]).fetchall()
        else:
            rows = conn.execute(sql, [*params, per_page, (page - 1) * per_page]).fetchall()
    return {"total": total, "entries": [dict(row) for row in rows], "page": page, "per_page": per_page}


def list_snmp_traps(*, device_id=None, q=None, start=None, end=None, page=1, per_page=50, limit=None):
    where, params = ["1=1"], []
    if device_id:
        where.append("e.device_id = ?")
        params.append(int(device_id))
    if q:
        where.append("(lower(e.trap_oid) LIKE ? OR lower(COALESCE(e.trap_name, '')) LIKE ?)")
        like = f"%{str(q).strip().lower()}%"
        params.extend([like, like])
    _time_filters(where, params, start, end)
    where_sql = " AND ".join(where)
    per_page = min(500, max(1, int(per_page or 50)))
    page = max(1, int(page or 1))
    with _lock:
        conn = get_conn()
        total = conn.execute(
            f"SELECT COUNT(*) AS n FROM snmp_traps e WHERE {where_sql}", params
        ).fetchone()["n"]
        sql = (
            "SELECT e.*, d.name AS device_name, d.ip AS device_ip FROM snmp_traps e"
            f" JOIN devices d ON d.id = e.device_id WHERE {where_sql}"
            " ORDER BY e.id DESC LIMIT ? OFFSET ?"
        )
        if limit is not None:
            rows = conn.execute(sql, [*params, min(10000, int(limit)), 0]).fetchall()
        else:
            rows = conn.execute(sql, [*params, per_page, (page - 1) * per_page]).fetchall()
    return {"total": total, "entries": [dict(row) for row in rows], "page": page, "per_page": per_page}


# ---------------------------------------------------------
# Aturan kejadian
# ---------------------------------------------------------

_RULE_FIELDS = (
    "name", "kind", "match_type", "pattern", "oid_prefix", "min_severity", "device_id",
    "group_id", "problem_kind", "problem_severity", "cooldown_seconds",
    "auto_resolve_minutes", "recovery_pattern", "resolves_rule_id", "enabled",
)


def list_ingest_rules():
    with _lock:
        rows = get_conn().execute(
            """
            SELECT r.*, d.name AS device_name, g.name AS group_name,
                   rr.name AS resolves_rule_name
            FROM ingest_rules r
            LEFT JOIN devices d ON d.id = r.device_id
            LEFT JOIN host_groups g ON g.id = r.group_id
            LEFT JOIN ingest_rules rr ON rr.id = r.resolves_rule_id
            ORDER BY r.kind, r.name COLLATE NOCASE
            """
        ).fetchall()
    return [dict(row) for row in rows]


def get_ingest_rule(rule_id):
    with _lock:
        row = get_conn().execute("SELECT * FROM ingest_rules WHERE id = ?", (rule_id,)).fetchone()
    return dict(row) if row else None


def ingest_rule_name_exists(name, exclude_id=None):
    with _lock:
        row = get_conn().execute(
            "SELECT id FROM ingest_rules WHERE name = ? COLLATE NOCASE AND (? IS NULL OR id != ?)",
            (name, exclude_id, exclude_id),
        ).fetchone()
    return row is not None


def create_ingest_rule(data):
    now = _now()
    with transaction() as conn:
        cursor = conn.execute(
            f"INSERT INTO ingest_rules ({', '.join(_RULE_FIELDS)}, created_at, updated_at)"
            f" VALUES ({', '.join('?' for _ in _RULE_FIELDS)}, ?, ?)",
            (*[data.get(key) for key in _RULE_FIELDS], now, now),
        )
        return cursor.lastrowid


def update_ingest_rule(rule_id, data):
    now = _now()
    with transaction() as conn:
        cursor = conn.execute(
            f"UPDATE ingest_rules SET {', '.join(f'{key}=?' for key in _RULE_FIELDS)}, updated_at=?"
            " WHERE id=?",
            (*[data.get(key) for key in _RULE_FIELDS], now, rule_id),
        )
        return cursor.rowcount > 0


def delete_ingest_rule(rule_id):
    with transaction() as conn:
        cursor = conn.execute("DELETE FROM ingest_rules WHERE id = ?", (rule_id,))
        return cursor.rowcount > 0


def get_device_by_ip(ip):
    with _lock:
        row = get_conn().execute(
            "SELECT id, name, ip, type, status, group_id, parent_id, poll_interval"
            " FROM devices WHERE ip = ?",
            (ip,),
        ).fetchone()
    return dict(row) if row else None


def if_name_for_index(device_id, if_index):
    """Petakan ifIndex SNMP ke nama interface dari snapshot mt_interfaces terakhir."""
    if if_index is None:
        return None
    try:
        wanted = int(if_index)
    except (TypeError, ValueError):
        return None
    with _lock:
        rows = get_conn().execute(
            "SELECT data FROM mt_interfaces WHERE device_id=? AND stale=0", (device_id,)
        ).fetchall()
    for row in rows:
        try:
            data = json.loads(row["data"])
        except (TypeError, ValueError):
            continue
        if data.get("if_index") == wanted:
            return data.get("name")
    return None


def parent_suppression_name(device_id):
    """Nama parent terdekat yang offline (maks. 10 tingkat), atau None."""
    current = device_id
    with _lock:
        conn = get_conn()
        for _ in range(10):
            row = conn.execute(
                "SELECT d.parent_id, p.name AS parent_name, p.status AS parent_status"
                " FROM devices d LEFT JOIN devices p ON p.id = d.parent_id WHERE d.id = ?",
                (current,),
            ).fetchone()
            if row is None or row["parent_id"] is None:
                return None
            if (row["parent_status"] or "").lower() == "offline":
                return row["parent_name"] or f"#{row['parent_id']}"
            current = row["parent_id"]
    return None


def record_ingest_problem(rule, device_id, severity, message):
    """Buka/perbarui problem aturan dengan agregasi + cooldown notifikasi.

    Return dict: id, is_new, should_notify, suppressed_by_parent.
    """
    now = _now()
    suppressed = parent_suppression_name(device_id)
    with transaction() as conn:
        row = conn.execute(
            "SELECT id, notified_at FROM problems WHERE device_id=? AND rule_id=? AND status='open'",
            (device_id, rule["id"]),
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE problems SET severity=?, message=?, event_count=event_count+1,"
                " last_seen_at=?, suppressed_by_parent=? WHERE id=?",
                (severity, str(message)[:300], now, suppressed, row["id"]),
            )
            should_notify = False
            if not suppressed:
                cooldown = max(0, int(rule.get("cooldown_seconds") or 0))
                last = row["notified_at"]
                if not last or _older_than(last, cooldown):
                    conn.execute("UPDATE problems SET notified_at=? WHERE id=?", (now, row["id"]))
                    should_notify = True
            return {"id": row["id"], "is_new": False, "should_notify": should_notify,
                    "suppressed_by_parent": suppressed}
        cursor = conn.execute(
            "INSERT INTO problems (device_id, kind, severity, message, status, started_at,"
            " rule_id, event_count, last_seen_at, notified_at, suppressed_by_parent)"
            " VALUES (?,?,?,?,'open',?,?,1,?,?,?)",
            (
                device_id, rule.get("problem_kind") or "syslog_rule", severity,
                str(message)[:300], now, rule["id"], now,
                None if suppressed else now, suppressed,
            ),
        )
        return {"id": cursor.lastrowid, "is_new": True, "should_notify": not suppressed,
                "suppressed_by_parent": suppressed}


def _older_than(ts, seconds):
    if seconds <= 0:
        return True
    try:
        then = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return True
    return (datetime.now(timezone.utc) - then).total_seconds() >= seconds


def get_problem(problem_id):
    with _lock:
        row = get_conn().execute("SELECT * FROM problems WHERE id = ?", (problem_id,)).fetchone()
    return dict(row) if row else None


def mark_problem_notified(problem_id):
    with transaction() as conn:
        conn.execute("UPDATE problems SET notified_at=? WHERE id=?", (_now(), problem_id))


def resolve_ingest_problem(device_id, rule_id):
    now = _now()
    with transaction() as conn:
        cursor = conn.execute(
            "UPDATE problems SET status='resolved', resolved_at=? WHERE device_id=? AND rule_id=?"
            " AND status='open'",
            (now, device_id, rule_id),
        )
        return cursor.rowcount


def resolve_rule_problems(rule_id):
    now = _now()
    with transaction() as conn:
        cursor = conn.execute(
            "UPDATE problems SET status='resolved', resolved_at=? WHERE rule_id=? AND status='open'",
            (now, rule_id),
        )
        return cursor.rowcount


def auto_resolve_ingest_problems():
    """Tutup problem aturan yang tidak menerima kejadian selama N menit."""
    now = datetime.now(timezone.utc)
    resolved = 0
    with transaction() as conn:
        rows = conn.execute(
            """
            SELECT p.id, p.device_id, p.rule_id, p.started_at, p.last_seen_at,
                   r.auto_resolve_minutes, r.name AS rule_name
            FROM problems p JOIN ingest_rules r ON r.id = p.rule_id
            WHERE p.status='open' AND r.auto_resolve_minutes > 0
            """
        ).fetchall()
        for row in rows:
            last = row["last_seen_at"] or row["started_at"]
            try:
                then = datetime.strptime(last, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue
            if (now - then).total_seconds() >= int(row["auto_resolve_minutes"]) * 60:
                conn.execute(
                    "UPDATE problems SET status='resolved', resolved_at=? WHERE id=?",
                    (now.strftime("%Y-%m-%dT%H:%M:%SZ"), row["id"]),
                )
                resolved += 1
    return resolved


def reevaluate_suppressed_problems():
    """Bersihkan penekanan bila parent sudah pulih. Return daftar problem yang perlu dikirim."""
    ready = []
    with transaction() as conn:
        rows = conn.execute(
            "SELECT * FROM problems WHERE status='open' AND suppressed_by_parent IS NOT NULL"
        ).fetchall()
        for row in rows:
            if parent_suppression_name(row["device_id"]):
                continue
            conn.execute(
                "UPDATE problems SET suppressed_by_parent=NULL, notified_at=? WHERE id=?",
                (_now(), row["id"]),
            )
            ready.append(dict(row, suppressed_by_parent=None))
    return ready


# ---------------------------------------------------------
# Dedup log MikroTik yang juga masuk lewat syslog
# ---------------------------------------------------------

def filter_syslog_duplicates(device_id, rows, polled_at=None):
    """Buang baris mt_logs yang pesannya sudah tercatat lewat syslog.

    Kunci mengikuti spesifikasi: perangkat + menit + hash pesan. Toleransi
    ±2 menit dipakai karena waktu RouterOS dan waktu terima bisa berbeda.
    """
    if not rows:
        return rows
    base = polled_at or _now()
    try:
        base_dt = datetime.strptime(base, "%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        base_dt = datetime.now(timezone.utc).replace(tzinfo=None)
    hashes = set()
    for offset in range(-2, 3):
        minute = (base_dt + timedelta(minutes=offset)).strftime("%Y-%m-%dT%H:%M")
        for row in rows:
            message = row.get("message")
            if message:
                hashes.add(dedup_hash(device_id, minute, message))
    if not hashes:
        return rows
    # SQLite membatasi jumlah parameter; pecah query IN menjadi beberapa bagian.
    found = set()
    ordered = sorted(hashes)
    for start_index in range(0, len(ordered), 500):
        chunk = ordered[start_index:start_index + 500]
        placeholders = ",".join("?" for _ in chunk)
        with _lock:
            found.update(
                row["dedup_hash"]
                for row in get_conn().execute(
                    f"SELECT dedup_hash FROM syslog_events WHERE device_id=? AND dedup_hash IN ({placeholders})",
                    (device_id, *chunk),
                ).fetchall()
            )
    if not found:
        return rows
    kept = []
    for row in rows:
        message = row.get("message")
        if message and any(
            dedup_hash(device_id, (base_dt + timedelta(minutes=offset)).strftime("%Y-%m-%dT%H:%M"), message)
            in found
            for offset in range(-2, 3)
        ):
            continue
        kept.append(row)
    return kept


def ingest_table_counts():
    tables = ["syslog_events", "snmp_traps", "ingest_rules"]
    counts = {}
    with _lock:
        for table in tables:
            counts[table] = get_conn().execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
    return counts
