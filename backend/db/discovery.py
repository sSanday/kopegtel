"""Pemindaian CIDR (Data Collection): pekerjaan scan & hasil."""

import json
import sqlite3

from .connection import (
    _lock,
    transaction,
    _now,
    get_conn,
)
from .schema import rebuild_scan_jobs_without_secrets


def create_scan_job(cidr, total, created_by):
    """Buat pekerjaan scan tanpa menyimpan kredensial apa pun.

    Community SNMP / kredensial RouterOS hanya diteruskan ke thread pemindaian
    di memori; setelah proses selesai tidak ada salinan yang tersisa di DB.
    """
    with transaction() as conn:
        cursor = conn.execute(
            "INSERT INTO scan_jobs (cidr, status, total, completed, found, created_by, created_at, results)"
            " VALUES (?, 'running', ?, 0, 0, ?, ?, '[]')",
            (cidr, int(total), created_by, _now()),
        )
        return cursor.lastrowid


def scrub_scan_credentials_file(path):
    """Bersihkan kolom kredensial scan lama dari satu berkas SQLite.

    Dipakai untuk backup historis (yang tidak dibuka oleh aplikasi saat start).
    Return True bila tabel scan_jobs diubah. Tidak pernah mencetak nilai secret.
    """
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        changed = rebuild_scan_jobs_without_secrets(conn)
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"integrity_check gagal setelah pembersihan: {integrity}")
        return changed
    finally:
        conn.close()


def update_scan_job(job_id, completed, results):
    with transaction() as conn:
        conn.execute(
            "UPDATE scan_jobs SET completed=?, found=?, results=? WHERE id=?",
            (int(completed), len(results), json.dumps(results, ensure_ascii=False), job_id),
        )

def finish_scan_job(job_id):
    with transaction() as conn:
        conn.execute(
            "UPDATE scan_jobs SET status='done', finished_at=? WHERE id=?",
            (_now(), job_id),
        )

def get_scan_job(job_id):
    with _lock:
        row = get_conn().execute("SELECT * FROM scan_jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        return None
    job = dict(row)
    try:
        job["results"] = json.loads(job.get("results") or "[]")
    except json.JSONDecodeError:
        job["results"] = []
    return job

def list_scan_jobs(limit=20):
    with _lock:
        rows = get_conn().execute(
            "SELECT id, cidr, status, total, completed, found, created_by, created_at, finished_at FROM scan_jobs ORDER BY id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    return [dict(r) for r in rows]

