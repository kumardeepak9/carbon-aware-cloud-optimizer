# config

Central configuration and observability setup for the Carbon-Aware Cloud Optimizer.

## Responsibilities

| Module | Purpose |
|---|---|
| `settings.py` | All application config via environment variables (Pydantic BaseSettings) |
| `logging.py` | Structured JSON / pretty logging initialisation (structlog) |

## Environment Variables

All variables are documented in `../.env.example`.  
Copy to `.env` at the project root and populate for local development:

```bash
cp .env.example .env
```

> ⚠️ **Never commit `.env` or any file containing real credentials.**

## Adding New Settings

1. Create a new `class XxxSettings(BaseSettings)` in `settings.py`.
2. Add it as a field on the `Settings` facade class.
3. Document the variables in `.env.example`.

## Logging Usage

```python
from config import configure_logging, get_logger

configure_logging(level="INFO", fmt="json")  # call once at startup
log = get_logger(__name__)

log.info("workload.scaled", reason="low_carbon", replicas=3)
```
