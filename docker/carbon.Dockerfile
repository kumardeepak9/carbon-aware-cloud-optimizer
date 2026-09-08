# ---------------------------------------------------------------------------
# Stage 1: dependency builder
# ---------------------------------------------------------------------------
FROM python:3.11-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

# Carbon exporter shares the main project requirements minus FastAPI/uvicorn
COPY carbon/requirements.txt ./requirements.txt
RUN pip install --upgrade pip \
    && pip install --no-cache-dir --prefix=/install -r requirements.txt

# ---------------------------------------------------------------------------
# Stage 2: production runtime
# ---------------------------------------------------------------------------
FROM python:3.11-slim AS runtime

LABEL org.opencontainers.image.title="greenops-carbon-exporter"
LABEL org.opencontainers.image.description="GreenOps AI — Electricity Maps → Prometheus carbon metrics exporter."
LABEL org.opencontainers.image.source="https://github.com/kumardeepak9/carbon-aware-cloud-optimizer"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN groupadd --gid 1001 appgroup \
    && useradd  --uid 1001 --gid 1001 --no-create-home --shell /sbin/nologin appuser

COPY --from=builder /install /usr/local

WORKDIR /app

# Copy only the packages the carbon exporter imports at runtime
COPY carbon/ ./carbon/
COPY config/ ./config/

USER 1001

EXPOSE 8002

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; data = urllib.request.urlopen('http://127.0.0.1:8002/metrics', timeout=3).read(); raise SystemExit(0 if b'greenops_carbon_data_available' in data else 1)"

CMD ["python", "-m", "carbon.server"]
