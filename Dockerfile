# theapk · ghostprint — landing page
# Single-stage Python image. Stdlib-only server, numpy/trimesh/numpy-stl only for tag/verify CLI.
FROM python:3.12-slim

# Avoid pyc files + force stdout buffering for Coolify logs
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=8080 \
    HOST=0.0.0.0

# Install only the runtime deps. No system packages beyond what's strictly needed.
# We DO NOT install a full build chain — numpy-stl + trimesh both ship pre-built wheels.
WORKDIR /app

# Copy only what's needed to install first (better layer caching)
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Now copy the rest
COPY tag-print.py verify-print.py ./
COPY src/ ./src/
COPY landing/ ./landing/

# Non-root user
RUN useradd --create-home --shell /bin/bash app \
    && chown -R app:app /app
USER app

EXPOSE 8080

# Healthcheck hits /health; serve.py returns {"ok": true, "service": "ghostprint-landing"}
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; r=urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3); import sys; sys.exit(0 if r.status==200 else 1)"

# Use the existing stdlib server. Single process, threading; sufficient for v0.1.
# Coolify passes $PORT (default 8080); the CLI reads --port flag.
CMD ["python", "landing/serve.py", "--host", "0.0.0.0", "--port", "8080"]
