from math import isfinite


def detect_anomalies(labels, values, min_samples=6, sensitivity=3.0):
    points = []
    valid = [
        (i, float(value))
        for i, value in enumerate(values)
        if value is not None and isfinite(float(value)) and float(value) >= 0
    ]
    if len(valid) < min_samples:
        return points
    window = [value for _, value in valid]
    median = sorted(window)[len(window) // 2]
    deviations = sorted(abs(value - median) for value in window)
    mad = deviations[len(deviations) // 2]
    scale = max(1.4826 * mad, 0.001)
    for index, value in valid:
        score = abs(value - median) / scale
        if score < sensitivity:
            continue
        severity = "high" if score >= sensitivity * 1.75 else "medium"
        points.append(
            {
                "index": index,
                "timestamp": labels[index] if index < len(labels) else None,
                "value": round(value, 2),
                "baseline": round(median, 2),
                "score": round(score, 2),
                "severity": severity,
                "reason": "Nilai jauh dari baseline median historis",
            }
        )
    return points


def summarize_anomalies(labels, values, min_samples=6, sensitivity=3.0):
    anomalies = detect_anomalies(labels, values, min_samples, sensitivity)
    return {
        "count": len(anomalies),
        "latest": anomalies[-1] if anomalies else None,
        "anomalies": anomalies,
    }
