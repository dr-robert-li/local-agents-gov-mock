"""Agent service entrypoint.

FastAPI control plane (health, manual run trigger, Claude Console session shim)
plus an APScheduler hourly job. The first run fires on startup so validation does
not wait for the cron. Each run:
  1. fetch yfinance data for all 10 tickers
  2. get a BUY/SELL/HOLD recommendation per ticker (Agent SDK, optionally
     enriched by the Perplexity Sonar MCP server, or a heuristic fallback)
  3. apply recommendations to the simulated portfolio
  4. record the run, seed Compliance + Analytics emulators, emit telemetry
  5. publish a portfolio snapshot to the UI via Redis
"""
import asyncio
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, HTTPException

import portfolio
import research
import telemetry
import redis_publisher as rp

COMPLIANCE_BASE = os.getenv("COMPLIANCE_API_BASE", "http://compliance-api:8001")
ANALYTICS_BASE = os.getenv("ANALYTICS_API_BASE", "http://analytics-api:8002")

_run_lock = asyncio.Lock()       # prevent overlapping runs
_last_status: dict = {"state": "idle", "run_id": None, "finished_at": None}


# --------------------------------------------------------------------------- #
# Core run
# --------------------------------------------------------------------------- #
async def execute_run(trigger: str = "manual") -> dict:
    if _run_lock.locked():
        return {"skipped": True, "reason": "a run is already in progress"}

    async with _run_lock:
        run_id = "run_" + uuid.uuid4().hex[:16]
        _last_status.update(state="running", run_id=run_id, finished_at=None)
        rp.reset_replay()
        rp.publish("run_start", {"run_id": run_id, "trigger": trigger,
                                 "tickers": research.TICKERS})
        tracer = telemetry.get_tracer()

        recommendations: list[dict] = []
        prices: dict[str, float] = {}
        total_usage = {"input_tokens": 0, "output_tokens": 0, "cache_read": 0,
                       "cache_creation": 0, "cost_usd": 0.0}
        sessions: list[tuple[str, list[dict]]] = []
        turns = 0
        tool_calls = 0

        with tracer.start_as_current_span("agent.run") as span:
            span.set_attribute("run_id", run_id)
            span.set_attribute("model", research.MODEL)
            span.set_attribute("ticker_count", len(research.TICKERS))

            for ticker in research.TICKERS:
                try:
                    data = await asyncio.to_thread(research.fetch_market_data, ticker)
                    if data.get("price"):
                        prices[ticker] = data["price"]
                    rec, usage, msgs, session_id = await research.research_ticker(data, run_id)
                    recommendations.append(rec)
                    for k in total_usage:
                        total_usage[k] += usage.get(k, 0) or 0
                    sessions.append((session_id, msgs))
                    turns += max(1, len(msgs))
                    tool_calls += 1
                except Exception as e:  # noqa: BLE001
                    # One ticker failing must not abort the whole run.
                    print(f"[run] {ticker} errored: {e}", flush=True)
                    rp.publish("assistant", {"ticker": ticker, "text": f"[skip] {ticker}: {e}"})

            # Apply to portfolio + persist run.
            portfolio.apply_recommendations(recommendations, prices)
            run_record = portfolio.record_run(run_id, recommendations, prices, total_usage)

            telemetry.record_usage(total_usage["input_tokens"],
                                   total_usage["output_tokens"],
                                   total_usage["cost_usd"],
                                   total_usage["cache_read"],
                                   total_usage["cache_creation"])
            telemetry.log_event(
                "agent.api_usage",
                f"run {run_id} tokens in={total_usage['input_tokens']} "
                f"out={total_usage['output_tokens']} cache_read={total_usage['cache_read']} "
                f"cost=${round(total_usage['cost_usd'],4)}",
                {"run_id": run_id, "input_tokens": total_usage["input_tokens"],
                 "output_tokens": total_usage["output_tokens"],
                 "cache_read_tokens": total_usage["cache_read"],
                 "cache_creation_tokens": total_usage["cache_creation"],
                 "cost_usd": total_usage["cost_usd"]})
            telemetry.log_event(
                "portfolio.snapshot",
                f"equity={run_record['total_equity']} cash={run_record['cash_balance']}",
                {"cash_balance": run_record["cash_balance"],
                 "total_equity": run_record["total_equity"],
                 "position_count": len(run_record["positions"])},
            )

        # Persist sessions for the Console shim.
        for session_id, msgs in sessions:
            research.save_session(session_id, run_id, msgs)

        # Publish portfolio snapshot to UI.
        rp.publish("portfolio", {"snapshot": portfolio.snapshot()})

        # Seed external emulators (best-effort).
        await _seed_emulators(run_id, sessions, recommendations, total_usage, turns, tool_calls)

        rp.publish("run_end", {"run_id": run_id,
                               "total_equity": run_record["total_equity"],
                               "cash_balance": run_record["cash_balance"]})
        _last_status.update(state="idle", run_id=run_id,
                            finished_at=datetime.now(timezone.utc).isoformat())
        print(f"[run] {run_id} complete via {trigger}: equity={run_record['total_equity']}",
              flush=True)
        return run_record


async def _seed_emulators(run_id, sessions, recommendations, usage, turns, tool_calls) -> None:
    convo_session = sessions[0][0] if sessions else ("sess_" + uuid.uuid4().hex)
    tickers = [r["ticker"] for r in recommendations]
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            await client.post(f"{COMPLIANCE_BASE}/internal/seed_run", json={
                "session_id": convo_session,
                "conversation_id": "conv_" + run_id,
                "turns": max(1, turns),
                "tool_calls": max(1, tool_calls),
                "tickers": tickers,
            })
        except Exception as e:  # noqa: BLE001
            print(f"[seed] compliance failed: {e}", flush=True)
        try:
            await client.post(f"{ANALYTICS_BASE}/internal/record_run", json={
                "run_id": run_id,
                "model": research.MODEL,
                "input_tokens": usage["input_tokens"],
                "output_tokens": usage["output_tokens"],
                "cache_read_tokens": usage.get("cache_read", 0),
                "cache_creation_tokens": usage.get("cache_creation", 0),
                "num_sessions": max(1, len(sessions)),
                "cost_usd": usage["cost_usd"],
            })
        except Exception as e:  # noqa: BLE001
            print(f"[seed] analytics failed: {e}", flush=True)


# --------------------------------------------------------------------------- #
# Lifespan: init DBs, telemetry, scheduler, optional startup run.
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def lifespan(app: FastAPI):
    portfolio.init_db()
    research.init_sessions_db()
    telemetry.setup_telemetry()

    # Brief requirement: warn (don't fail) when the web-search key is absent.
    from mcp_config import perplexity_available
    if not perplexity_available():
        print("[warn] PERPLEXITY_API_KEY not set — skipping Perplexity Sonar web "
              "search; proceeding with yfinance-only data.", flush=True)

    scheduler = AsyncIOScheduler()
    scheduler.add_job(lambda: asyncio.create_task(execute_run("cron")),
                      "interval", hours=1, id="hourly_research")
    scheduler.start()
    app.state.scheduler = scheduler

    if os.getenv("RUN_ON_STARTUP", "false").lower() == "true":
        # Defer so the HTTP server (and thus /health) comes up immediately.
        asyncio.create_task(_delayed_startup_run())

    yield
    scheduler.shutdown(wait=False)


async def _delayed_startup_run() -> None:
    await asyncio.sleep(5)
    try:
        await execute_run("startup")
    except Exception as e:  # noqa: BLE001
        print(f"[startup-run] failed: {e}", flush=True)


app = FastAPI(title="Stock Research Agent", version="1.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Control plane
# --------------------------------------------------------------------------- #
@app.get("/health")
def health() -> dict:
    return {"status": "ok", "run": _last_status}


@app.post("/run")
async def trigger_run() -> dict:
    """Manually trigger a research run (non-blocking returns once complete)."""
    return await execute_run("manual")


@app.get("/api/portfolio")
def get_portfolio() -> dict:
    return portfolio.snapshot()


# ---- Claude Console replay shim (Anthropic Messages API shape) ----
@app.get("/api/sessions")
def api_sessions() -> dict:
    return {"data": research.list_sessions()}


@app.get("/api/sessions/{session_id}/messages")
def api_session_messages(session_id: str) -> dict:
    msgs = research.get_session_messages(session_id)
    if msgs is None:
        raise HTTPException(status_code=404, detail="session not found")
    return {"data": msgs, "session_id": session_id}
