"""E3 — Traffic Flow: bucket agregat 5 menit, rollup jam, retensi, query UI.

Tidak ada flow mentah yang disimpan. Agregasi di memori oleh kolektor
(`flow.service`), lalu ditulis di sini dalam satu transaksi.
"""

import json
from datetime import datetime, timedelta, timezone

from .connection import (
    _days_ago,
    _lock,
    transaction,
    _now,
    get_conn,
)
from core.validation import SQLITE_INT64_MAX, parse_query_int

MAX_KEY_BYTES = 180
FLOW_KINDS = ("src", "dst", "pair", "app", "proto", "interface")
OTHER_KEY = "lainnya"


def _ts():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def bucket_start_ts(ts=None):
    moment = ts or datetime.now(timezone.utc)
    if isinstance(moment, str):
        moment = datetime.strptime(moment, "%Y-%m-%dT%H:%M:%SZ")
    moment = moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment
    floored = moment.replace(minute=(moment.minute // 5) * 5, second=0, microsecond=0)
    return floored.strftime("%Y-%m-%dT%H:%M:%SZ")


def hour_start_ts(ts):
    return str(ts)[:13] + ":00:00Z"


# ---------------------------------------------------------
# Insert & trim
# ---------------------------------------------------------

def insert_bucket_rows(rows, top_n=100):
    """Tulis bucket agregat dalam SATU transaksi, lalu pertahankan top-N.

    rows: iterable of (exporter_device_id, bucket_ts, kind, key, bytes, packets, flows)
    """
    if not rows:
        return 0
    agg = {}
    for exporter_id, bucket_ts, kind, key, bytes_, packets, flows in rows:
        normalized = str(key or "")[:MAX_KEY_BYTES] or "-"
        group = (int(exporter_id), str(bucket_ts), str(kind), normalized)
        item = agg.get(group)
        if item is None:
            agg[group] = [int(bytes_ or 0), int(packets or 0), int(flows or 0)]
        else:
            item[0] += int(bytes_ or 0)
            item[1] += int(packets or 0)
            item[2] += int(flows or 0)
    if not agg:
        return 0
    payload = [
        (v[0], v[1], v[2], group[0], group[1], group[2], group[3])
        for group, v in agg.items()
    ]
    top_n = max(1, min(1000, int(top_n or 100)))
    with transaction() as conn:
        conn.executemany(
            """
            INSERT INTO flow_buckets (bytes, packets, flows, exporter_device_id, bucket_ts, kind, key)
            VALUES (?,?,?,?,?,?,?)
            ON CONFLICT (bucket_ts, exporter_device_id, kind, key) DO UPDATE SET
              bytes = flow_buckets.bytes + excluded.bytes,
              packets = flow_buckets.packets + excluded.packets,
              flows = flow_buckets.flows + excluded.flows
            """,
            payload,
        )
        _trim_top_n(conn, top_n=top_n)
    return len(payload)


def _trim_top_n(conn, top_n=100):
    """Simpan N kunci terbesar per (bucket, eksporter, kind); sisanya ke 'lainnya'."""
    groups = conn.execute(
        "SELECT DISTINCT bucket_ts, exporter_device_id, kind FROM flow_buckets"
    ).fetchall()
    for group in groups:
        rows = conn.execute(
            "SELECT id, key, bytes, packets, flows FROM flow_buckets"
            " WHERE bucket_ts=? AND exporter_device_id=? AND kind=? AND key != ?"
            " ORDER BY bytes DESC, packets DESC",
            (group["bucket_ts"], group["exporter_device_id"], group["kind"], OTHER_KEY),
        ).fetchall()
        if len(rows) <= top_n:
            continue
        keep, rest = rows[:top_n], rows[top_n:]
        if not rest:
            continue
        sum_bytes = sum(row["bytes"] for row in rest)
        sum_packets = sum(row["packets"] for row in rest)
        sum_flows = sum(row["flows"] for row in rest)
        ids = [row["id"] for row in rest]
        placeholders = ",".join("?" for _ in ids)
        conn.execute(f"DELETE FROM flow_buckets WHERE id IN ({placeholders})", ids)
        conn.execute(
            """
            INSERT INTO flow_buckets (bytes, packets, flows, exporter_device_id, bucket_ts, kind, key)
            VALUES (?,?,?,?,?,?,?)
            ON CONFLICT (bucket_ts, exporter_device_id, kind, key) DO UPDATE SET
              bytes = flow_buckets.bytes + excluded.bytes,
              packets = flow_buckets.packets + excluded.packets,
              flows = flow_buckets.flows + excluded.flows
            """,
            (sum_bytes, sum_packets, sum_flows,
             group["exporter_device_id"], group["bucket_ts"], group["kind"], OTHER_KEY),
        )


# ---------------------------------------------------------
# Rollup per jam & retensi
# ---------------------------------------------------------

def rolled_hour_until():
    from .settings import get_setting

    return (get_setting("flow_hourly_rolled_until", "") or "").strip()


def _set_rolled_hour(value, conn=None):
    if conn is not None:
        conn.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES ('flow_hourly_rolled_until', ?, ?)"
            " ON CONFLICT (key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (value, _now()),
        )
        return
    from .settings import set_settings

    set_settings({"flow_hourly_rolled_until": value})


def rollup_hourly_buckets(conn=None, now=None):
    """Gabungkan bucket 5 menit dari jam yang sudah LENGKAP ke tabel per jam.

    Idempoten: hanya jam setelah `flow_hourly_rolled_until` yang diproses.
    """
    def _work(c):
        markers = c.execute(
            "SELECT DISTINCT substr(bucket_ts, 1, 13) AS hour FROM flow_buckets"
            " ORDER BY hour"
        ).fetchall()
        current_hour = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H")
        rolled_until = rolled_hour_until()
        rolled = []
        for marker in markers:
            hour = marker["hour"]
            if hour >= current_hour:
                continue  # jam berjalan: tunggu lengkap
            if rolled_until and hour <= rolled_until:
                continue  # sudah pernah di-rollup
            hour_ts = hour + ":00:00Z"
            rows = c.execute(
                """
                SELECT exporter_device_id, kind, key, SUM(bytes) AS b, SUM(packets) AS p, SUM(flows) AS f
                FROM flow_buckets WHERE substr(bucket_ts, 1, 13) = ?
                GROUP BY exporter_device_id, kind, key
                """,
                (hour,),
            ).fetchall()
            if rows:
                c.executemany(
                    """
                    INSERT INTO flow_buckets_hourly (bytes, packets, flows, exporter_device_id, hour_ts, kind, key)
                    VALUES (?,?,?,?,?,?,?)
                    ON CONFLICT (hour_ts, exporter_device_id, kind, key) DO UPDATE SET
                      bytes = flow_buckets_hourly.bytes + excluded.bytes,
                      packets = flow_buckets_hourly.packets + excluded.packets,
                      flows = flow_buckets_hourly.flows + excluded.flows
                    """,
                    [(row["b"], row["p"], row["f"], row["exporter_device_id"], hour_ts,
                      row["kind"], row["key"]) for row in rows],
                )
            rolled.append(hour_ts)
        if rolled:
            _set_rolled_hour(max(rolled), conn=c)
        return len(rolled)

    if conn is not None:
        return _work(conn)
    with transaction() as conn:
        result = _work(conn)
        return result


def purge_flow(days=7, hourly_days=90, conn=None):
    """Retensi: rollup jam berjalan, buang bucket 5 menit lama, buang jam lama."""
    def _work(c):
        rolled = rollup_hourly_buckets(conn=c)
        raw_deleted = c.execute(
            "DELETE FROM flow_buckets WHERE bucket_ts < ?", (_days_ago(days),)
        ).rowcount
        hourly_deleted = c.execute(
            "DELETE FROM flow_buckets_hourly WHERE hour_ts < ?", (_days_ago(hourly_days),)
        ).rowcount
        return {"rolled_hours": rolled, "flow_raw_deleted": raw_deleted,
                "flow_hourly_deleted": hourly_deleted}

    if conn is not None:
        return _work(conn)
    with transaction() as conn:
        result = _work(conn)
        return result


# ---------------------------------------------------------
# Query untuk UI/API
# ---------------------------------------------------------

def _range_cutoffs(range_key):
    now = datetime.now(timezone.utc)
    if range_key == "1h":
        return (_iso(now - timedelta(hours=1)), "raw")
    if range_key == "24h":
        return (_iso(now - timedelta(hours=24)), "hourly")
    return (_iso(now - timedelta(days=7)), "hourly")


def _source_table(grain):
    return "flow_buckets" if grain == "raw" else "flow_buckets_hourly"


def _ts_col(grain):
    return "bucket_ts" if grain == "raw" else "hour_ts"


def list_exporters():
    with _lock:
        rows = get_conn().execute(
            "SELECT id, name, ip, type, status FROM devices ORDER BY name COLLATE NOCASE"
        ).fetchall()
    return [dict(row) for row in rows]


def flow_summary(range_key="1h", exporter_id=None):
    cutoff, grain = _range_cutoffs(range_key)
    table = _source_table(grain)
    ts_col = _ts_col(grain)
    params = [cutoff]
    where = f"{ts_col} >= ?"
    if exporter_id:
        where += " AND exporter_device_id = ?"
        params.append(
            parse_query_int(exporter_id, 0, min_value=0, max_value=SQLITE_INT64_MAX)
        )
    with _lock:
        conn = get_conn()
        totals = conn.execute(
            f"""
            SELECT exporter_device_id, SUM(bytes) AS bytes, SUM(packets) AS packets,
                   SUM(flows) AS flows
            FROM {table} WHERE {where} GROUP BY exporter_device_id
            """,
            params,
        ).fetchall()
        series = conn.execute(
            f"""
            SELECT exporter_device_id, substr({ts_col}, 1, ?) AS bucket,
                   SUM(bytes) AS bytes
            FROM {table} WHERE {where}
            GROUP BY exporter_device_id, substr({ts_col}, 1, ?) ORDER BY bucket
            """,
            [13 if grain == "hourly" else 16, *params, 13 if grain == "hourly" else 16],
        ).fetchall()
    names = {row["id"]: row for row in list_exporters()}
    exporters = []
    for row in totals:
        device_id = row["exporter_device_id"]
        exporters.append({
            "device_id": device_id,
            "name": names.get(device_id, {}).get("name"),
            "ip": names.get(device_id, {}).get("ip"),
            "bytes": int(row["bytes"] or 0),
            "packets": int(row["packets"] or 0),
            "flows": int(row["flows"] or 0),
            "series": [
                {"ts": item["bucket"] + (":00:00Z" if grain == "hourly" else ":00Z"),
                 "bytes": int(item["bytes"] or 0)}
                for item in series
                if item["exporter_device_id"] == device_id
            ],
        })
    exporters.sort(key=lambda item: item["bytes"], reverse=True)
    return {
        "range": range_key,
        "grain": grain,
        "exporters": exporters,
        "total_bytes": sum(item["bytes"] for item in exporters),
        "total_packets": sum(item["packets"] for item in exporters),
        "total_flows": sum(item["flows"] for item in exporters),
    }


def flow_top(kind, range_key="1h", exporter_id=None, limit=10):
    assert kind in FLOW_KINDS
    cutoff, grain = _range_cutoffs(range_key)
    table = _source_table(grain)
    ts_col = _ts_col(grain)
    params = [kind, cutoff]
    where = f"kind = ? AND {ts_col} >= ?"
    if exporter_id:
        where += " AND exporter_device_id = ?"
        params.append(
            parse_query_int(exporter_id, 0, min_value=0, max_value=SQLITE_INT64_MAX)
        )
    limit = max(1, min(1000, int(limit or 10)))
    with _lock:
        rows = get_conn().execute(
            f"""
            SELECT key, SUM(bytes) AS bytes, SUM(packets) AS packets, SUM(flows) AS flows
            FROM {table} WHERE {where} GROUP BY key ORDER BY bytes DESC LIMIT ?
            """,
            [*params, limit],
        ).fetchall()
    return [
        {"key": row["key"], "bytes": int(row["bytes"] or 0),
         "packets": int(row["packets"] or 0), "flows": int(row["flows"] or 0)}
        for row in rows
    ]


def flow_conversations(range_key="1h", exporter_id=None, limit=10):
    return flow_top("pair", range_key=range_key, exporter_id=exporter_id, limit=limit)


def flow_interfaces(range_key="1h", exporter_id=None):
    return flow_top("interface", range_key=range_key, exporter_id=exporter_id, limit=1000)


def lease_host_map():
    """Peta {alamat_ip: host-name} dari lease DHCP semua perangkat (stale=0)."""
    mapping = {}
    with _lock:
        rows = get_conn().execute(
            "SELECT data FROM mt_dhcp_leases WHERE stale = 0"
        ).fetchall()
    for row in rows:
        try:
            data = json.loads(row["data"])
        except (TypeError, ValueError):
            continue
        address = data.get("address")
        name = data.get("host-name") or data.get("host_name")
        if address and name:
            mapping[str(address)] = str(name)[:128]
    return mapping


def host_name_for(device_id, ip):
    return lease_host_map().get(str(ip))


def flow_table_counts():
    tables = ["flow_buckets", "flow_buckets_hourly"]
    counts = {}
    with _lock:
        for table in tables:
            counts[table] = get_conn().execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
    return counts
