"""Blueprint Masalah & aktivitas: daftar masalah, ack, umpan aktivitas."""
from flask import Blueprint, g, jsonify, request

import db
import netguard
from core.guard import audit, roles

bp = Blueprint("problems", __name__)


@bp.get("/api/problems")
def api_list_problems():
    status = (request.args.get("status") or "open").lower()
    if status not in ("open", "resolved", "all"):
        return jsonify({"error": "Status tidak valid (open|resolved|all)."}), 400
    try:
        limit = min(500, max(1, int(request.args.get("limit", "200"))))
    except ValueError:
        limit = 200
    if status == "all":
        problems = db.list_problems("open", limit) + db.list_problems("resolved", limit)
    else:
        problems = db.list_problems(status, limit)
    return jsonify({"problems": problems})


@bp.post("/api/problems/<int:problem_id>/ack")
@roles("admin", "operator")
def api_ack_problem(problem_id):
    data = request.get_json(silent=True) or {}
    acked = netguard.as_bool(data.get("acked", True))
    username = g.user["username"]
    if acked:
        ok = db.ack_problem(problem_id, username)
        action, summary = "problem.ack", "Mengakui masalah"
    else:
        ok = db.unack_problem(problem_id)
        action, summary = "problem.unack", "Membatalkan pengakuan masalah"
    if not ok:
        return jsonify({"error": "Masalah tidak ditemukan atau sudah selesai."}), 404
    audit(action, f"problem:{problem_id}", summary)
    return jsonify({"ok": True})


@bp.get("/api/activity")
def api_activity():
    try:
        limit = min(50, max(1, int(request.args.get("limit", "10"))))
    except ValueError:
        limit = 10
    return jsonify({"activity": db.recent_activity(limit)})
