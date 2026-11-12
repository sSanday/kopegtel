"""Helper deret waktu bersama: rentang waktu dan downsampling titik grafik."""

from datetime import datetime, timedelta, timezone

RANGE_HOURS = {"1h": 1, "24h": 24, "7d": 24 * 7}


def downsample(points, max_points=300):
    if len(points) <= max_points:
        return points
    stride = -(-len(points) // max_points)  # ceil
    return points[::stride] + ([points[-1]] if points[-1] is not points[::stride][-1] else [])


def range_since(range_name):
    hours = RANGE_HOURS.get(range_name)
    if hours is None:
        return None
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
