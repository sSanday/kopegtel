"""nms.query_helpers — Optimized query patterns to avoid N+1 problems."""


def get_all_host_stats(c, time_range_hours=24):
    """Get stats for ALL hosts in single query instead of looping.

    Returns dict: {host: {total, up_count, avg_ms, min_ms, max_ms, avg_loss}}
    """
    c.execute(f"""
        SELECT 
            host,
            COUNT(*) AS total,
            SUM(CASE WHEN latency != -1 THEN 1 ELSE 0 END) AS up_count,
            AVG(CASE WHEN latency != -1 THEN latency END) AS avg_ms,
            MIN(CASE WHEN latency != -1 THEN latency END) AS min_ms,
            MAX(CASE WHEN latency != -1 THEN latency END) AS max_ms,
            AVG(CASE WHEN latency != -1 THEN packet_loss END) AS avg_loss
        FROM ping_logs
        WHERE timestamp > datetime('now','localtime','-{time_range_hours} hours')
        GROUP BY host
    """)

    result = {}
    for r in c.fetchall():
        result[r["host"]] = {
            "total": r["total"] or 0,
            "up_count": r["up_count"] or 0,
            "avg_ms": r["avg_ms"],
            "min_ms": r["min_ms"],
            "max_ms": r["max_ms"],
            "avg_loss": r["avg_loss"],
        }
    return result


def get_all_agent_metrics(c, time_range_minutes=5):
    """Get latest agent metrics for ALL hosts in single query.

    Returns dict: {host: {cpu, ram, disk, net_in, net_out, timestamp}}
    """
    c.execute(f"""
        SELECT host, cpu_percent, ram_percent, disk_percent, net_in, net_out, timestamp
        FROM agent_metrics
        WHERE id IN (
            SELECT MAX(id) FROM agent_metrics
            WHERE timestamp > datetime('now', 'localtime', '-{time_range_minutes} minutes')
            AND cpu_percent IS NOT NULL
            GROUP BY host
        )
    """)

    rows = c.fetchall()

    result = {}
    for r in rows:
        result[r["host"]] = {
            "cpu": round(r["cpu_percent"] or 0, 1),
            "ram": round(r["ram_percent"] or 0, 1),
            "disk": round(r["disk_percent"] or 0, 1),
            "net_in": round(r["net_in"] or 0, 2),
            "net_out": round(r["net_out"] or 0, 2),
            "timestamp": r["timestamp"],
        }
    return result


def get_host_ping_history(c, hosts, time_range_hours=24):
    """Get ping history for multiple hosts in single query.

    Args:
        hosts: list of host IPs/names
        time_range_hours: how many hours back to query

    Returns dict: {host: [(timestamp, latency), ...]}
    """
    placeholders = ",".join("?" * len(hosts))

    c.execute(
        f"""
        SELECT host, timestamp, latency
        FROM ping_logs
        WHERE host IN ({placeholders})
        AND timestamp > datetime('now','localtime','-{time_range_hours} hours')
        ORDER BY host, id ASC
    """,
        hosts,
    )

    result = {}
    for r in c.fetchall():
        host = r["host"]
        if host not in result:
            result[host] = []
        result[host].append((r["timestamp"], r["latency"]))

    return result


def get_service_uptime_map(c, time_range_hours=24):
    """Get uptime percentage for ALL services in single query.

    Returns dict: {service_id: uptime_percentage}
    """
    c.execute(f"""
        SELECT 
            service_id,
            COUNT(*) AS total,
            SUM(CASE WHEN status='ONLINE' THEN 1 ELSE 0 END) AS online
        FROM service_history
        WHERE timestamp > datetime('now','localtime','-{time_range_hours} hours')
        GROUP BY service_id
    """)

    result = {}
    for r in c.fetchall():
        total = r["total"] or 1
        online = r["online"] or 0
        result[r["service_id"]] = round((online / total * 100), 1)

    return result
