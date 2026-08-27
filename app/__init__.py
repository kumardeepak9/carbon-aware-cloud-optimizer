"""
app — GreenOps Demo Workload

A lightweight FastAPI application that serves as the demo workload
optimised by the GreenOps AI agent. It exposes:

    GET /health   — liveness probe
    GET /ready    — readiness probe
    GET /metrics  — Prometheus metrics (text exposition)
    POST /work    — simulated CPU work for load testing
"""
