FROM python:3.10-slim

# Install system dependencies (terutama 'ping' karena dibutuhkan oleh app.py)
RUN apt-get update && apt-get install -y --no-install-recommends iputils-ping curl \
    && rm -rf /var/lib/apt/lists/*

# User non-root dengan UID/GID mengikuti host (default 1000) agar bind-mount
# ./:/app tetap bisa tulis network.db/backups. Override saat build bila perlu:
#   docker compose build --build-arg UID=$(id -u) --build-arg GID=$(id -g)
ARG UID=1000
ARG GID=1000
RUN groupadd -g ${GID} nms 2>/dev/null || true \
    && useradd -m -u ${UID} -g ${GID} nms 2>/dev/null || useradd -m nms \
    && mkdir -p /app/backups && chown -R nms:nms /app

# Set direktori kerja di dalam container
WORKDIR /app

# Salin requirements dan install library Python (layer cache)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Salin kode aplikasi (file sensitif dikecualikan via .dockerignore)
COPY --chown=nms:nms . .

USER nms

EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:5000/health || exit 1

# Jalankan Gunicorn dengan 1 worker dan 4 thread.
# PENTING: tetap 1 worker — scheduler APScheduler in-process akan ganda
# (ping/alert duplikat) jika worker > 1.
CMD ["gunicorn", "-w", "1", "--threads", "4", "-b", "0.0.0.0:5000", "app:app"]
