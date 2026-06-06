# Changelog

All notable changes to this project are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); this POC is unversioned (`0.x`).

## [Unreleased]

### Added
- **Token consumption & cost tracking** end-to-end:
  - Agent captures `input`/`output` **and** `cache_read`/`cache_creation` tokens
    from the SDK `ResultMessage` (cache reads dominate real consumption and were
    previously discarded).
  - `run_history` carries per-run token columns + `cost_usd`; portfolio snapshot
    exposes per-run and cumulative `token_usage`.
  - OpenObserve metrics: `agent.api_usage.{input,output,cache_read,cache_creation,cost_usd}`
    counters + per-run `agent.api_usage` log event.
  - Web UI shows Total tokens + Total cost.
- **OpenTelemetry on both API emulators** — `compliance-api` and `analytics-api`
  now export FastAPI request spans + structured logs to OpenObserve
  (`obs.py`, auto-instrumentation).

### Changed
- **Web search provider: Brave → Perplexity Sonar MCP** (`server-perplexity-ask`,
  stdio). `PERPLEXITY_API_KEY` replaces `BRAVE_API_KEY`; absent key degrades
  gracefully to yfinance-only.
- **Default model → `claude-sonnet-4-6`** (override via `AGENT_MODEL`). Analytics
  list-price fallback updated to Sonnet rates ($3 / $15 per MTok).
- **Cost authority** — analytics endpoints now serve the SDK-reported
  `total_cost_usd` (cache-inclusive) instead of a recomputed list-price estimate
  that undercounted ~2×.

### Fixed
- Agent SDK `query()` failed under root because `permission_mode="bypassPermissions"`
  passes `--dangerously-skip-permissions` (CLI refuses as root). Switched to
  `allowed_tools` pre-authorization.
- OpenObserve healthcheck on a distroless image (added static busybox wrapper) and
  password-complexity boot panic.
- Web UI healthcheck `localhost` → `127.0.0.1` (busybox wget resolved IPv6 `::1`).
- Redis silently dropped pub/sub writes when the Docker VM disk filled
  (`stop-writes-on-bgsave-error`). Disabled persistence (`--save "" --appendonly no`).

### Removed
- One-shot backfill machinery (`POST /backfill` + `list_runs`/`update_run_usage`,
  emulator `/internal/usage_runs` and `/internal/conversations`) after the initial
  reconcile; unused imports.

## [0.1.0] — Initial POC

- `docker-compose` stack: agent (Claude Agent SDK), Anthropic Compliance &
  Analytics API emulators, Fastify web UI (SSE + portfolio), OpenObserve, Redis.
- Hourly + on-startup research over 10 US tech tickers via yfinance; simulated
  USD-1000 portfolio in SQLite.
- Anthropic-schema-faithful audit-event and usage/cost emulators with cursor
  pagination + `validate_schemas.py`.
- Live agent stream relayed agent → Redis → web UI over SSE; OTLP telemetry to
  OpenObserve; Claude Console session-replay shim.
