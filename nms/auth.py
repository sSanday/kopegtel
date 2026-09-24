"""nms.auth — decorator auth untuk API."""

from functools import wraps

from flask import jsonify, request
from flask_login import current_user


def api_login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated:
            return jsonify({"error": "Unauthorized. Silakan login."}), 401

        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            is_json = request.is_json or (request.content_type or "").startswith(
                "application/json"
            )
            has_xrw = request.headers.get("X-Requested-With") == "XMLHttpRequest"
            if not (is_json or has_xrw):
                return (
                    jsonify(
                        {
                            "error": "CSRF check failed. Gunakan Content-Type: application/json atau X-Requested-With."
                        }
                    ),
                    403,
                )
        return f(*args, **kwargs)

    return decorated
