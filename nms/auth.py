"""nms.auth — decorator auth untuk API."""

from functools import wraps
from urllib.parse import urlparse

from flask import jsonify, request
from flask_login import current_user


def _same_origin_request():
    """False bila Origin/Referer ada dan tidak cocok dengan Host.

    Request non-browser (tanpa Origin/Referer) tetap diizinkan selama
    lolos pemeriksaan JSON/X-Requested-With di bawah.
    """
    host = (request.host or "").lower()
    for header in ("Origin", "Referer"):
        value = request.headers.get(header)
        if not value:
            continue
        try:
            parsed = urlparse(value)
        except ValueError:
            return False
        if not parsed.netloc or parsed.netloc.lower() != host:
            return False
    return True


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
            if not _same_origin_request():
                return (
                    jsonify({"error": "CSRF check failed. Origin tidak cocok."}),
                    403,
                )
        return f(*args, **kwargs)

    return decorated


def require_role(*allowed_roles):
    """Decorator untuk role-based access control.

    Usage:
        @app.route("/admin")
        @api_login_required
        @require_role("admin")
        def admin_endpoint():
            ...
    """

    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if not current_user.is_authenticated:
                return jsonify({"error": "Unauthorized"}), 401

            user_role = getattr(current_user, "role", None)
            if user_role not in allowed_roles:
                return (
                    jsonify(
                        {
                            "error": f"Forbidden. Required role: {', '.join(allowed_roles)}"
                        }
                    ),
                    403,
                )

            return f(*args, **kwargs)

        return decorated

    return decorator
