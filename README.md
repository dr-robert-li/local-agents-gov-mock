# Claude Agent SDK — Hourly Stock Research POC

A single `docker-compose up --build` stack that runs an hourly Claude agent which
researches the top-10 US tech stocks, simulates a portfolio, and emits enterprise
telemetry into local emulators of the Anthropic Compliance & Analytics APIs.

## Architecture

| Service | Port | Role |
|---|---|---|
| web-ui | 3000 | Dashboard — live SSE agent stream + portfolio |
| openobserve | 5080 | Observability UI + OTLP HTTP receiver |
| agent | 8080 | Health, manual run trigger, Claude Console session shim |
| compliance-api | 8001 | Anthropic Compliance API emulator (+ `/docs`) |
| analytics-api | 8002 | Anthropic Usage & Cost API emulator (+ `/docs`) |
| redis | internal | SSE pub/sub backbone |

The **agent** fetches market data (yfinance), asks Claude via the **Agent SDK
`query()` loop** for a BUY/SELL/HOLD call per ticker (optionally enriched by the
**Perplexity Sonar MCP** server over stdio), applies the calls to a simulated
USD-1000 portfolio in SQLite, and streams every message/tool-call to the UI (via
Redis) and to OpenObserve (via OTLP spans, logs, metrics).

> **Model:** default is `claude-sonnet-4-6` — override with the `AGENT_MODEL`
> env var.

## Prerequisites

- Docker + Docker Compose
- Internet access (image pulls, yfinance, Anthropic API)
- An `ANTHROPIC_API_KEY` for the live Agent SDK loop. **Without it the agent falls
  back to a deterministic 52-week-range heuristic** so the whole stack still runs
  end-to-end — useful for offline demos.

## Quickstart

```bash
cp .env.example .env
nano .env                 # paste ANTHROPIC_API_KEY (and optional PERPLEXITY_API_KEY)
docker-compose up --build
```

The agent runs once on startup (no need to wait for the hourly cron), then every
hour. Open <http://localhost:3000>.

## Trigger a manual run

```bash
curl -X POST http://localhost:8080/run         # or click "Trigger run" in the UI
```

## Connect the Anthropic Claude Console

The agent exposes the local conversation history in Anthropic Messages API shape:

```bash
curl http://localhost:8080/api/sessions
curl http://localhost:8080/api/sessions/<session_id>/messages
```

Point a Console at these endpoints to replay locally-run agent sessions.

## Perplexity Sonar MCP (optional)

Set `PERPLEXITY_API_KEY` in `.env` to enable live web-search enrichment via the
`perplexity_ask` tool from `npx server-perplexity-ask` (stdio). If absent, the
agent logs a warning and proceeds with yfinance-only data.

## Schema validation

Each emulator ships a `validate_schemas.py` that asserts response shapes match the
real Anthropic API:

```bash
docker compose exec compliance-api python validate_schemas.py
docker compose exec analytics-api python validate_schemas.py
```

## Drop-in replacement (real enterprise feeds)

Swap the emulators for the real Anthropic APIs with two env changes — no code or
schema changes elsewhere:

```bash
COMPLIANCE_API_BASE=https://api.anthropic.com
ANALYTICS_API_BASE=https://api.anthropic.com
ANTHROPIC_ADMIN_KEY=sk-ant-admin01-...
```

## Observability

Open <http://localhost:5080>, log in with `ZO_ROOT_USER_EMAIL` /
`ZO_ROOT_USER_PASSWORD`, and inspect the `agent.message`, `agent.tool_use`,
`agent.thinking`, and `portfolio.snapshot` streams plus `agent.run` traces.

## Persistence

All SQLite state (`portfolio.db`, `compliance.db`, `analytics.db`) lives on the
named `poc-data` volume and survives `docker-compose down && up`.
