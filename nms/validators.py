"""nms.validators — input validation helpers."""

import os
import re


def validate_ip_or_hostname(value):
    """Validate IP address or hostname.

    Returns: (valid: bool, error: str or None)
    """
    value = str(value or "").strip()

    if not value:
        return False, "IP/hostname required"

    ipv4_re = re.compile(r"^(\d{1,3}\.){3}\d{1,3}$")
    hostname_re = re.compile(r"^[a-zA-Z0-9]([a-zA-Z0-9.\-]{0,253}[a-zA-Z0-9])?$")

    if ipv4_re.match(value):
        try:
            parts = [int(p) for p in value.split(".")]
            if any(p > 255 for p in parts):
                return False, "Invalid IP format (octet > 255)"
        except ValueError:
            return False, "Invalid IP format"
        return True, None

    if hostname_re.match(value):
        return True, None

    return False, "Invalid IP/hostname format"


def validate_backup_filename(filename):
    """Validate backup filename to prevent path traversal.

    Returns: (valid: bool, sanitized_filename: str, error: str or None)
    """
    filename = str(filename or "").strip()

    if not filename:
        return False, None, "Filename required"

    # Remove path components
    filename = os.path.basename(filename)

    # Whitelist allowed extensions
    if not filename.endswith(".db"):
        return False, None, "File must be .db format"

    # Check for suspicious patterns
    if ".." in filename or "/" in filename or "\\" in filename:
        return False, None, "Invalid filename"

    return True, filename, None


def sanitize_like_search(value):
    """Sanitize value for SQL LIKE queries.
    
    Escapes special characters: %, _, \
    """
    value = str(value or "")
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def validate_role(role):
    """Validate user role.

    Returns: (valid: bool, error: str or None)
    """
    valid_roles = ("admin", "operator", "viewer")

    role = str(role or "").strip().lower()

    if role not in valid_roles:
        return False, f"Invalid role. Must be one of: {', '.join(valid_roles)}"

    return True, None


def validate_threshold(value, min_val=0, max_val=100):
    """Validate numeric threshold value.

    Returns: (valid: bool, numeric_value: float or None, error: str or None)
    """
    try:
        num = float(value)
    except (ValueError, TypeError):
        return False, None, "Value must be a number"

    if not (min_val <= num <= max_val):
        return False, None, f"Value must be between {min_val} and {max_val}"

    return True, num, None
