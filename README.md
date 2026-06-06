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

### Runtime topology

All services share one bridge network (`poc-net`) and a named volume
(`poc-data`, holding the three SQLite DBs). External access is via the bound
ports; Redis is internal-only.

```
                        ┌────────── external ──────────┐
                        │  Yahoo Finance   Anthropic    │
                        │  (yfinance)      API (Claude) │
                        │                  Perplexity   │
                        └───────▲───────────▲──────────┘
                                │           │
   browser ──:3000──► web-ui ──┐│           │ (Agent SDK query loop,
                       (Fastify)│           │  Perplexity Sonar MCP/stdio)
                          ▲ SSE  │           │
                   pub/sub│      │  ┌────────┴─────────┐
                     ┌────┴────┐ └─►│      agent       │──:8080 health/run/sessions
                     │  redis  │◄───│  APScheduler +   │
                     └─────────┘    │  FastAPI         │
                                    └──┬───┬───┬───┬───┘
        portfolio.db  ◄────────────────┘   │   │   └──► OTLP ─► openobserve :5080
        (SQLite, /data)                     │   │            (traces/logs/metrics)
                                            │   └──► analytics-api :8002 (record_run)
                                            └──────► compliance-api :8001 (seed_run)
                                                       │            │
                          analytics.db ◄──────────────┘            └──► compliance.db
                          (usage/cost, /data)                           (audit events, /data)
                                  └─OTLP─► openobserve   compliance ─OTLP─► openobserve
```

### Per-run data flow

Hourly (APScheduler) and once on startup, `agent.execute_run()` does, per ticker
(10 US tech tickers), inside a root `agent.run` span:

1. **Fetch** price / volume / P-E / 52-week range / headlines via **yfinance**.
2. **Reason** — Agent SDK `query()` loop on `claude-sonnet-4-6`; tools
   pre-authorized via `allowed_tools` (`Bash`, `mcp__perplexity-ask__perplexity_ask`).
   Each tool call → `agent.tool_use` / `agent.mcp_call` span; assistant text,
   thinking, tool results stream to **Redis** → **web-ui SSE** → browser.
3. **Recommend** — structured `{ticker, recommendation, confidence, rationale}`
   parsed from the model (heuristic 52-week-range fallback if no API key).
4. **Apply** to the simulated portfolio (BUY ≤10% of cash, SELL liquidates, HOLD
   no-op); persist run + token/cost to `portfolio.db`.
5. **Emit** — token consumption (incl. cache read/creation) + USD cost as
   OpenObserve metrics; per-run `agent.api_usage` + `portfolio.snapshot` logs.
6. **Seed feeds** — POST canonical audit events to **compliance-api** and a usage
   record (tokens + SDK cost) to **analytics-api**, each persisted to SQLite and
   re-served on the Anthropic-compatible read endpoints.

### Drop-in / swap points

| Concern | Local POC | Production swap |
|---|---|---|
| Compliance feed | `compliance-api` emulator | `COMPLIANCE_API_BASE` → `https://api.anthropic.com` |
| Usage & cost feed | `analytics-api` emulator | `ANALYTICS_API_BASE` → `https://api.anthropic.com` + Admin key |
| Web search | Perplexity Sonar MCP | any MCP server in `mcp_config.py` |
| Model | `claude-sonnet-4-6` | `AGENT_MODEL` env var |

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
