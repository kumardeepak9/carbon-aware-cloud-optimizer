"""
app/main.py — GreenOps Demo Workload (FastAPI)

Endpoints
---------
GET  /health    Liveness probe — always 200 while process is alive.
GET  /ready     Readiness probe — 200 when ready, 503 when draining.
GET  /metrics   Prometheus text-format metrics scrape endpoint.
POST /work      Simulated CPU-bound work for load testing.
GET  /          Human-readable status page.

Configuration
-------------
All tunables are sourced from environment variables (see ConfigMap in k8s/).
No secrets are read in this module.

Usage
-----
    uvicorn app.main:app --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

import hashlib
import os
import time
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Literal

import structlog
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.metrics import (
    APP_INFO,
    APP_READY,
    HTTP_REQUEST_DURATION_SECONDS,
    HTTP_REQUESTS_TOTAL,
    WORK_DURATION_SECONDS,
    WORK_REQUESTS_TOTAL,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# App configuration (from environment / ConfigMap)
# ---------------------------------------------------------------------------
APP_VERSION: str = os.getenv("APP_VERSION", "0.1.0")
APP_NAME: str = os.getenv("APP_NAME", "greenops-demo-workload")
INSTANCE_ID: str = os.getenv("POD_NAME", "local")  # injected via Downward API


# ---------------------------------------------------------------------------
# Lifespan — startup / shutdown hooks
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Register app metadata on startup; drain gracefully on shutdown."""
    APP_INFO.info(
        {
            "version": APP_VERSION,
            "app_name": APP_NAME,
            "instance_id": INSTANCE_ID,
        }
    )
    APP_READY.set(1)
    log.info("app.started", version=APP_VERSION, instance=INSTANCE_ID)

    yield  # application runs here

    # Graceful shutdown: mark not-ready so load balancer stops routing traffic
    APP_READY.set(0)
    log.info("app.shutting_down", instance=INSTANCE_ID)


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------
app = FastAPI(
    title=APP_NAME,
    description=(
        "Demo workload for the Carbon-Aware Cloud Optimizer. "
        "Exposes health, readiness, metrics, and a load-test endpoint."
    ),
    version=APP_VERSION,
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Middleware — instrument every request with Prometheus counters + histogram
# ---------------------------------------------------------------------------
@app.middleware("http")
async def prometheus_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
    """Record request count and latency for every HTTP call."""
    start = time.perf_counter()
    response: Response = await call_next(request)
    duration = time.perf_counter() - start

    endpoint = request.url.path
    method = request.method
    status = str(response.status_code)

    HTTP_REQUESTS_TOTAL.labels(method=method, endpoint=endpoint, status_code=status).inc()
    HTTP_REQUEST_DURATION_SECONDS.labels(method=method, endpoint=endpoint).observe(duration)

    return response


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/", response_class=JSONResponse, include_in_schema=False)
async def root() -> dict:
    """Human-readable status response."""
    return {
        "app": APP_NAME,
        "version": APP_VERSION,
        "instance": INSTANCE_ID,
        "status": "running",
        "endpoints": {
            "health": "/health",
            "ready": "/ready",
            "metrics": "/metrics",
            "work": "POST /work",
            "docs": "/docs",
        },
    }


@app.get(
    "/health",
    summary="Liveness probe",
    description="Returns 200 as long as the process is alive. Used by Kubernetes liveness probe.",
    tags=["Probes"],
)
async def health() -> dict:
    """
    Liveness probe.

    Returns 200 OK while the process is running. Kubernetes restarts the pod
    if this endpoint fails to respond.
    """
    return {"status": "alive", "instance": INSTANCE_ID}


@app.get(
    "/ready",
    summary="Readiness probe",
    description="Returns 200 when ready to serve traffic; 503 during startup/drain.",
    tags=["Probes"],
)
async def ready() -> dict:
    """
    Readiness probe.

    Kubernetes stops routing traffic to the pod if this returns non-2xx.
    The GreenOps agent can also set APP_READY=0 before draining a pod.
    """
    if APP_READY._value.get() == 0:  # noqa: SLF001
        raise HTTPException(status_code=503, detail="Not ready")
    return {"status": "ready", "instance": INSTANCE_ID}


@app.get(
    "/metrics",
    summary="Prometheus metrics scrape endpoint",
    description="Text-format Prometheus metrics. Scraped by Prometheus every 15 s.",
    response_class=PlainTextResponse,
    tags=["Observability"],
)
async def metrics() -> PlainTextResponse:
    """
    Prometheus metrics in text exposition format (OpenMetrics-compatible).

    Scraped by Prometheus according to the ServiceMonitor / scrape config.
    """
    data = generate_latest()
    return PlainTextResponse(content=data, media_type=CONTENT_TYPE_LATEST)


# ---------------------------------------------------------------------------
# Work simulation endpoint
# ---------------------------------------------------------------------------

WorkIntensity = Literal["low", "medium", "high"]

_INTENSITY_ITERATIONS: dict[WorkIntensity, int] = {
    "low": 5_000,
    "medium": 50_000,
    "high": 500_000,
}


@app.post(
    "/work",
    summary="Simulate CPU-bound work",
    description=(
        "Accepts a JSON body `{\"intensity\": \"low\"|\"medium\"|\"high\"}`. "
        "Performs hash iterations to simulate CPU load. "
        "Use for load testing and to generate realistic metrics."
    ),
    tags=["Load Testing"],
)
async def work(intensity: WorkIntensity = "medium") -> dict:
    """
    Simulate CPU-bound work.

    The ``intensity`` query parameter controls the number of SHA-256 hash
    iterations performed:

    - ``low``    →  5,000  iterations  (~ms)
    - ``medium`` →  50,000 iterations  (~10s of ms)
    - ``high``   →  500,000 iterations (~100s of ms)

    Args:
        intensity: Work intensity level.

    Returns:
        JSON with duration_ms, iterations performed, and a result digest.
    """
    iterations = _INTENSITY_ITERATIONS[intensity]
    start = time.perf_counter()

    # CPU-bound simulation: chained SHA-256 hashing
    digest = b"greenops-seed"
    for _ in range(iterations):
        digest = hashlib.sha256(digest).digest()

    duration = time.perf_counter() - start

    WORK_REQUESTS_TOTAL.labels(intensity=intensity).inc()
    WORK_DURATION_SECONDS.labels(intensity=intensity).observe(duration)

    log.info(
        "work.completed",
        intensity=intensity,
        iterations=iterations,
        duration_ms=round(duration * 1000, 2),
        instance=INSTANCE_ID,
    )

    return {
        "status": "ok",
        "intensity": intensity,
        "iterations": iterations,
        "duration_ms": round(duration * 1000, 2),
        "result_digest": digest.hex()[:16],
        "instance": INSTANCE_ID,
    }
