"""
carbon/server.py — HTTP exposition server for GreenOps carbon metrics.

Serves ``greenops_carbon_*`` metrics on a dedicated pull endpoint so that
Prometheus can scrape them independently of the AI agent's own metrics.

Architecture
------------
The carbon exporter uses a **dedicated CollectorRegistry** (not the global
default) to serve only carbon metrics on port 8002.  This keeps the scrape
targets clean and ensures the ``greenops-carbon-exporter`` Prometheus job
sees only the metrics it owns.

    Port 8000  → Demo workload metrics (app/main.py)
    Port 8001  → AI agent self-metrics  (agent/agent.py)
    Port 8002  → Carbon / Electricity Maps metrics  ← this module

Usage::

    from carbon.server import CarbonMetricsServer

    server = CarbonMetricsServer(
        api_key="...",   # from ELECTRICITY_MAPS_API_KEY
        zone="DE",       # from ELECTRICITY_MAPS_ZONE
        port=8002,
    )
    await server.start()        # starts background HTTP server
    await server.run_forever()  # poll loop — blocks until cancelled
"""

from __future__ import annotations

import asyncio
import signal
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from typing import Any

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    generate_latest,
)

from carbon.exporter import CarbonMetricsExporter
from carbon.metrics import CarbonMetrics
from config import bootstrap, get_logger
from config.settings import get_settings

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# HTTP handler — serves Prometheus text format from a specific registry
# ---------------------------------------------------------------------------


def _make_handler(registry: CollectorRegistry) -> type:
    """
    Return a BaseHTTPRequestHandler subclass bound to ``registry``.

    Using a closure (not a class attribute) keeps the handler stateless and
    avoids the global registry entirely.
    """

    class CarbonMetricsHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path not in ("/metrics", "/"):
                self.send_response(404)
                self.end_headers()
                return
            payload = generate_latest(registry)
            self.send_response(200)
            self.send_header("Content-Type", CONTENT_TYPE_LATEST)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: ANN002
            # Silence the default noisy access log; structured logs are emitted
            # in do_GET via the outer ``log`` object if needed.
            pass

    return CarbonMetricsHandler


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------


class CarbonMetricsServer:
    """
    Combines a CarbonMetricsExporter with a Prometheus HTTP exposition server.

    Responsibilities
    ----------------
    - Creates an isolated CollectorRegistry (no global state pollution).
    - Creates a CarbonMetrics facade registered in that registry.
    - Creates a CarbonMetricsExporter that updates the isolated metrics.
    - Starts a background HTTP thread serving ``/metrics`` on ``port``.
    - Runs an async poll loop that calls ``exporter.update()`` at
      ``poll_interval_seconds`` intervals.

    Args:
        api_key:                Electricity Maps API key (never logged or exposed).
        zone:                   Grid zone identifier (e.g. "DE", "FR").
        port:                   HTTP exposition port (default: 8002).
        poll_interval_seconds:  How often to fetch fresh data from Electricity Maps.
        base_url:               Electricity Maps API base URL.
    """

    def __init__(
        self,
        api_key: str,
        zone: str = "DE",
        port: int = 8002,
        poll_interval_seconds: int = 60,
        base_url: str = "https://api.electricitymap.org/v3",
        max_data_age_seconds: float | None = None,
    ) -> None:
        self._zone = zone
        self._port = port
        self._poll_interval = poll_interval_seconds

        # Isolated registry — only greenops_carbon_* metrics live here
        self._registry = CollectorRegistry()
        self._metrics = CarbonMetrics(registry=self._registry)
        self._exporter = CarbonMetricsExporter(
            api_key=api_key,
            zone=zone,
            base_url=base_url,
            max_data_age_seconds=max_data_age_seconds,
            metrics=self._metrics,
        )
        self._http_server: HTTPServer | None = None
        self._http_thread: Thread | None = None
        self._running = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """
        Open the Electricity Maps HTTP client and start the metrics HTTP server.

        Must be called before ``run_forever()``.
        """
        await self._exporter.open()

        handler = _make_handler(self._registry)
        self._http_server = HTTPServer(("0.0.0.0", self._port), handler)
        self._http_thread = Thread(
            target=self._http_server.serve_forever,
            name="carbon-metrics-http",
            daemon=True,  # exits when main process exits
        )
        self._http_thread.start()
        self._running = True

        log.info(
            "carbon.server.started",
            zone=self._zone,
            port=self._port,
            poll_interval_seconds=self._poll_interval,
            metrics_url=f"http://0.0.0.0:{self._port}/metrics",
        )

    async def stop(self) -> None:
        """Shut down the HTTP server and close the Electricity Maps client."""
        self._running = False
        if self._http_server is not None:
            self._http_server.shutdown()
        await self._exporter.close()
        log.info("carbon.server.stopped", zone=self._zone)

    # ------------------------------------------------------------------
    # Poll loop
    # ------------------------------------------------------------------

    async def run_forever(self) -> None:
        """
        Poll Electricity Maps at ``poll_interval_seconds`` and update metrics.

        Runs until cancelled (e.g. by SIGTERM / KeyboardInterrupt).
        The exporter's ``update()`` method never raises, so this loop is stable
        even during extended API outages.
        """
        log.info(
            "carbon.server.poll_loop_started",
            zone=self._zone,
            interval_seconds=self._poll_interval,
        )
        while self._running:
            await self._exporter.update()
            try:
                await asyncio.sleep(self._poll_interval)
            except asyncio.CancelledError:
                log.info("carbon.server.poll_loop_cancelled", zone=self._zone)
                break

    async def run(self) -> None:
        """
        Convenience method: start the server and run the poll loop.

        Registers SIGTERM/SIGINT handlers for graceful shutdown.
        """
        await self.start()

        loop = asyncio.get_running_loop()
        stop_event = asyncio.Event()

        def _on_signal() -> None:
            log.info("carbon.server.signal_received")
            stop_event.set()

        for sig in (signal.SIGTERM, signal.SIGINT):
            with suppress(NotImplementedError, RuntimeError):
                loop.add_signal_handler(sig, _on_signal)

        try:
            poll_task = asyncio.create_task(self.run_forever())
            await stop_event.wait()
            poll_task.cancel()
            await asyncio.gather(poll_task, return_exceptions=True)
        finally:
            await self.stop()


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


async def main() -> None:
    """
    Standalone entrypoint for the carbon metrics server.

    Configuration is read from environment variables via ``get_settings()``.
    Intended for ``python -m carbon.server`` or the ``greenops-carbon`` CLI entry point.
    """
    bootstrap()
    settings = get_settings()

    server = CarbonMetricsServer(
        api_key=settings.electricity_maps.api_key.get_secret_value(),
        zone=settings.electricity_maps.zone,
        port=settings.prometheus.metrics_export_port + 2,  # 8000 + 2 = 8002
        poll_interval_seconds=settings.agent.poll_interval_seconds,
        base_url=settings.electricity_maps.base_url,
        max_data_age_seconds=settings.agent.max_carbon_data_age_seconds,
    )
    await server.run()


if __name__ == "__main__":
    asyncio.run(main())
