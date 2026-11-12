"""Pemindaian CIDR (Data Collection): pekerjaan scan & hasil."""

import json

from .connection import (
    _lock,
    _now,
    get_conn,
)

def create_scan_job(cidr, total, created_by, community, mikrotik_user, mikrotik_password):
    with _lock:
        conn = get_conn()
        cursor = conn.execute(
            "INSERT INTO scan_jobs (cidr, status, total, completed, found, community, mikrotik_user, mikrotik_password, created_by, created_at, results)"
            " VALUES (?, 'running', ?, 0, 0, ?, ?, ?, ?, ?, '[]')",
            (cidr, int(total), community, mikrotik_user, mikrotik_password, created_by, _now()),
        )
        conn.commit()
        return cursor.lastrowid

def update_scan_job(job_id, completed, results):
    with _lock:
        conn = get_conn()
        conn.execute(
            "UPDATE scan_jobs SET completed=?, found=?, results=? WHERE id=?",
            (int(completed), len(results), json.dumps(results, ensure_ascii=False), job_id),
        )
        conn.commit()

def finish_scan_job(job_id):
    with _lock:
        conn = get_conn()
        conn.execute(
            "UPDATE scan_jobs SET status='done', finished_at=? WHERE id=?",
            (_now(), job_id),
        )
        conn.commit()

def get_scan_job(job_id, include_secrets=False):
    with _lock:
        row = get_conn().execute("SELECT * FROM scan_jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        return None
    job = dict(row)
    try:
        job["results"] = json.loads(job.get("results") or "[]")
    except json.JSONDecodeError:
        job["results"] = []
    if not include_secrets:
        job.pop("mikrotik_password", None)
        job.pop("community", None)
    return job

def list_scan_jobs(limit=20):
    with _lock:
        rows = get_conn().execute(
            "SELECT id, cidr, status, total, completed, found, created_by, created_at, finished_at FROM scan_jobs ORDER BY id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    return [dict(r) for r in rows]

