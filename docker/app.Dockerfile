# =============================================================================
# GreenOps Demo Workload — Dockerfile
# =============================================================================
# Multi-stage build: builder installs deps; final image is minimal.
# Runs as a non-root user (uid 1001) with a read-only filesystem.
# =============================================================================

# ---------------------------------------------------------------------------
# Stage 1: dependency builder
# ---------------------------------------------------------------------------
FROM python:3.11-slim AS builder

WORKDIR /build

# Install build tools then wipe them — keeps final layer small
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY app/requirements.txt ./requirements.txt
RUN pip install --upgrade pip \
    && pip install --no-cache-dir --prefix=/install -r requirements.txt

# ---------------------------------------------------------------------------
# Stage 2: production runtime
# ---------------------------------------------------------------------------
FROM python:3.11-slim AS runtime

LABEL org.opencontainers.image.title="greenops-demo-workload"
LABEL org.opencontainers.image.description="GreenOps AI demo workload — exposes health, metrics, and load-test endpoints."
LABEL org.opencontainers.image.source="https://github.com/your-org/carbon-aware-cloud-optimizer"

# Non-root user
RUN groupadd --gid 1001 appgroup \
    && useradd  --uid 1001 --gid 1001 --no-create-home --shell /sbin/nologin appuser

# Install runtime packages only
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Copy installed Python packages from builder
COPY --from=builder /install /usr/local

WORKDIR /app

# Copy application source
COPY app/ ./app/

# Writable tmp dir for uvicorn (read-only rootfs needs this for socket file)
RUN mkdir -p /tmp/uvicorn && chown 1001:1001 /tmp/uvicorn

USER 1001

EXPOSE 8080

# Health check baked into the image (Kubernetes probes take precedence)
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -sf http://localhost:8080/health || exit 1

CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8080", \
     "--workers", "2", \
     "--log-config", "/dev/null"]
