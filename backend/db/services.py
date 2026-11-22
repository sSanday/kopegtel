"""Layanan & SLA: daftar layanan, pemetaan perangkat, ketersediaan."""


from .connection import (
    _days_ago,
    _lock,
    transaction,
    _now,
    get_conn,
)
from .devices import (
    availability,
)

def list_services():
    with _lock:
        rows = get_conn().execute(
            """
            SELECT s.*,
                   (SELECT COUNT(*) FROM service_devices sd WHERE sd.service_id = s.id) AS device_count
            FROM services s ORDER BY s.name COLLATE NOCASE
            """
        ).fetchall()
        result = []
        for row in rows:
            device_rows = get_conn().execute(
                "SELECT d.id, d.name, d.status, d.ip FROM service_devices sd JOIN devices d ON d.id = sd.device_id WHERE sd.service_id = ? ORDER BY d.name",
                (row["id"],),
            ).fetchall()
            result.append({**dict(row), "devices": [dict(r) for r in device_rows]})
    return result

def get_service(service_id):
    with _lock:
        row = get_conn().execute("SELECT * FROM services WHERE id = ?", (service_id,)).fetchone()
        device_rows = (
            get_conn().execute(
                "SELECT d.id, d.name, d.status, d.ip FROM service_devices sd JOIN devices d ON d.id = sd.device_id WHERE sd.service_id = ?",
                (service_id,),
            ).fetchall()
            if row is not None
            else []
        )
    if row is None:
        return None
    return {**dict(row), "devices": [dict(r) for r in device_rows]}

def service_name_exists(name, exclude_id=None):
    with _lock:
        row = get_conn().execute(
            "SELECT id FROM services WHERE name = ? COLLATE NOCASE AND (? IS NULL OR id != ?)",
            (name, exclude_id, exclude_id),
        ).fetchone()
        return row is not None

def create_service(data):
    now = _now()
    with transaction() as conn:
        cursor = conn.execute(
            "INSERT INTO services (name, division, sla_target, mode, description, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            (data["name"], data.get("division", ""), float(data.get("sla_target", 99.0)),
             data.get("mode", "all"), data.get("description", ""), now, now),
        )
        return cursor.lastrowid

def update_service(service_id, data):
    now = _now()
    with transaction() as conn:
        cursor = conn.execute(
            "UPDATE services SET name=?, division=?, sla_target=?, mode=?, description=?, updated_at=? WHERE id=?",
            (data["name"], data.get("division", ""), float(data.get("sla_target", 99.0)),
             data.get("mode", "all"), data.get("description", ""), now, service_id),
        )
        return cursor.rowcount > 0

def delete_service(service_id):
    with transaction() as conn:
        cursor = conn.execute("DELETE FROM services WHERE id = ?", (service_id,))
        return cursor.rowcount > 0

def set_service_devices(service_id, device_ids):
    ids = [int(i) for i in dict.fromkeys(device_ids)]
    now = _now()
    with transaction() as conn:
        conn.execute("DELETE FROM service_devices WHERE service_id = ?", (service_id,))
        if ids:
            conn.executemany(
                "INSERT OR IGNORE INTO service_devices (service_id, device_id) VALUES (?,?)",
                [(service_id, i) for i in ids],
            )
        conn.execute("UPDATE services SET updated_at=? WHERE id=?", (now, service_id))

def service_availability(service_id, days=1):
    """Ketersediaan layanan = gabungan pemeriksaan perangkat penopang.

    Mode 'all' → layanan up bila SEMUA perangkat penopang up;
    mode 'any' → layanan up bila SETIKITNYA satu perangkat penopang up.
    Siklus tanpa pemeriksaan perangkat yang perlu dievaluasi dilewati.
    Pemeliharaan (maintenance=1) dikecualikan.
    """
    service = get_service(service_id)
    if service is None:
        return None
    devices = service["devices"]
    if not devices:
        return {"service": service, "availability_pct": None, "cycles": 0, "down_cycles": 0,
                "devices": [], "period_days": days}
    ids = [d["id"] for d in devices]
    placeholders = ",".join("?" * len(ids))
    since = _days_ago(days)
    with _lock:
        rows = get_conn().execute(
            f"SELECT checked_at, device_id, status FROM device_checks"
            f" WHERE device_id IN ({placeholders}) AND checked_at >= ? AND COALESCE(maintenance, 0) = 0"
            f" ORDER BY checked_at",
            [*ids, since],
        ).fetchall()

    by_ts = {}
    for row in rows:
        by_ts.setdefault(row["checked_at"], {})[row["device_id"]] = row["status"]

    mode = service["mode"]
    evaluated = down = 0
    for ts, states in by_ts.items():
        states = {int(k): v for k, v in states.items()}
        if mode == "all":
            if any(did not in states for did in ids):
                continue  # belum semua perangkat diperiksa pada siklus ini
            up = all(states[did] in ("online", "degraded") for did in ids)
        else:  # any
            if not any(did in states for did in ids):
                continue
            up = any(states.get(did) in ("online", "degraded") for did in ids)
        evaluated += 1
        if not up:
            down += 1

    pct = None
    if evaluated:
        pct = round(100.0 * (evaluated - down) / evaluated, 2)

    return {
        "service": service,
        "availability_pct": pct,
        "cycles": evaluated,
        "down_cycles": down,
        "period_days": days,
        "devices": [
            {**d, "availability": availability(d["id"], days)} for d in devices
        ],
    }

