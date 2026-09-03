"""
tests/conftest.py — Shared pytest fixtures and configuration.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_settings_from_dotenv(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop `config.settings` classes from reading the developer's local `.env`.

    Every `*Settings` model declares `env_file=".env"`, so without this a real
    `.env` in the working tree silently supplies values the tests mean to leave
    unset (e.g. GREENOPS_GITOPS_GITHUB_TOKEN). Tests that need a value set it
    explicitly via `monkeypatch.setenv` or a constructor kwarg.
    """
    from pydantic_settings import BaseSettings

    import config.settings as cs

    for obj in vars(cs).values():
        if isinstance(obj, type) and issubclass(obj, BaseSettings):
            patched = dict(obj.model_config)
            patched["env_file"] = None
            monkeypatch.setattr(obj, "model_config", patched)


@pytest.fixture(autouse=True)
def reset_prometheus_registry():
    """
    Isolate Prometheus metrics between tests.

    prometheus_client uses a global CollectorRegistry by default.
    Without isolation, metric re-registration between test modules causes errors.
    This fixture is a no-op placeholder; individual tests that need metric
    isolation should use prometheus_client.REGISTRY.unregister() or a
    fresh registry.
    """
    yield
