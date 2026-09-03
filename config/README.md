# config — configuration & observability

All runtime configuration comes from environment variables (or a local `.env` in
development). `config/settings.py` defines the schema; `config/logging.py`
configures structlog.

## Startup contract

Every process entry point (`agent.agent`, `agent.health_cli`, `gitops.cli`,
`reports.report`, `chat.cli`, `carbon.server`) calls `config.bootstrap()` first:

1. `configure_logging_from_env()` — applies `LOG_LEVEL` / `LOG_FORMAT`. Without
   this, `LOG_FORMAT=json` is read but never applied and production keeps the
   default human renderer.
2. `assert_safe_for_environment()` — raises `ConfigurationError` if a dev-only
   value is active while `APP_ENV=production` (see below).

**Fail-fast:** a missing *required* variable raises `pydantic.ValidationError`
from the relevant `*Settings` model at construction — the process never starts
with half a configuration.

## Variables

| Variable | Required | Default | Consumed by | Notes |
|---|---|---|---|---|
| `APP_ENV` | no | `development` | bootstrap | `development` \| `staging` \| `production` |
| `LOG_LEVEL` | no | `INFO` | logging | |
| `LOG_FORMAT` | no | `json` | logging | must be `json` when `APP_ENV=production` |
| `ELECTRICITY_MAPS_API_KEY` | **yes** (carbon exporter) | — | `carbon.server` | `SecretStr`; placeholders rejected |
| `ELECTRICITY_MAPS_BASE_URL` | no | `https://api.electricitymap.org/v3` | carbon exporter | must be `https://` |
| `ELECTRICITY_MAPS_ZONE` | no | `DE` | carbon exporter | normalised & pattern-checked |
| `ELECTRICITY_MAPS_CACHE_TTL_SECONDS` | no | `300` | carbon exporter | ≥ 30 |
| `K8S_NAMESPACE` | no | `greenops` | Prometheus query scope | no kube API client exists |
| `PROMETHEUS_API_URL` | prod: real value | `http://localhost:9090` | agent, health, chat, verifier | loopback rejected in production |
| `PROMETHEUS_METRICS_EXPORT_PORT` | no | `8000` | carbon exporter (+2 = 8002) | 1024–65535 |
| `AGENT_POLL_INTERVAL_SECONDS` | no | `60` | carbon exporter | ≥ 10 |
| `AGENT_MIN_REPLICAS` / `AGENT_MAX_REPLICAS` | no | `1` / `10` | safety policy | min ≤ max enforced |
| `AGENT_CPU_SAFETY_THRESHOLD` | no | `0.70` | safety policy | 0.0–1.0 |
| `AGENT_LATENCY_SLA_THRESHOLD_SECONDS` | no | `1.0` | safety policy | > 0 |
| `AGENT_MAX_SCALE_DOWN_PERCENTAGE` | no | `0.50` | safety policy | > 0, ≤ 1 |
| `AGENT_OPTIMIZATION_COOLDOWN_SECONDS` | no | `900` | safety policy | ≥ 0 |
| `AGENT_MAX_CARBON_DATA_AGE_SECONDS` | no | `600` | safety policy | ≥ 0 |
| `GREENOPS_GITOPS_REPO_PATH` | no | `.` | gitops workflow | |
| `GREENOPS_GITOPS_BASE_BRANCH` | no | `main` | gitops workflow | simple branch name |
| `GREENOPS_GITOPS_BRANCH_PREFIX` | no | `greenops` | gitops workflow | simple prefix |
| `GREENOPS_GITOPS_MANIFEST_PATH` | no | `k8s/overlays/prod/kustomization.yaml` | gitops workflow | must be under `k8s/`, no `..` |
| `GREENOPS_GITOPS_DEPLOYMENT_NAME` | no | `greenops-demo-workload` | gitops workflow | |
| `GREENOPS_GITOPS_CREATE_PULL_REQUEST` | no | `false` | gitops workflow | `true` ⇒ next two REQUIRED |
| `GREENOPS_GITOPS_GITHUB_REPOSITORY` | if PR creation | — | GitHub client | `owner/name` |
| `GREENOPS_GITOPS_GITHUB_TOKEN` | if PR creation | — | GitHub client | `SecretStr`; placeholder rejected in production |
| `GREENOPS_GITOPS_GITHUB_API_URL` | no | `https://api.github.com` | GitHub client | `https://`, no embedded creds |
| `REPORT_OUTPUT_DIR` | no | `./reports/output` | `reports.report` | |
| `REPORT_DECISION_HISTORY_PATH` | no | `./reports/decision-history.jsonl` | report + chat | append-only lifecycle log |
| `GRAFANA_ADMIN_USER` / `GRAFANA_ADMIN_PASSWORD` | password: **yes** | `admin` / — | docker-compose only | compose refuses to start without the password |

## Production safety (`APP_ENV=production`)

`assert_safe_for_environment()` (and `Settings()`) reject:

- `LOG_FORMAT != json`
- `PROMETHEUS_API_URL` on `localhost` / `127.0.0.1` / `*.local`
- `GREENOPS_GITOPS_GITHUB_TOKEN` unset or a placeholder while `CREATE_PULL_REQUEST=true`

These combinations are individually valid — they are only unsafe *because* the
process claims to be production, which no single settings model can see.

## Removed (were unused)

`PROMETHEUS_PUSHGATEWAY_URL` (nothing pushed), `AGENT_CARBON_INTENSITY_THRESHOLD_HIGH/LOW`
(thresholds live in `agent/policy.py`), `REPORT_SCHEDULE_CRON` / `REPORT_RECIPIENTS` /
`REPORT_SMTP_*` (no scheduler or mailer implemented). Stale keys left in an old
`.env` are ignored (`extra="ignore"`), not fatal.
