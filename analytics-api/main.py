"""Anthropic Usage & Cost / Claude Code Analytics API emulator.

Reproduces the response shapes of:
  - GET /v1/organizations/usage_report/messages
  - GET /v1/organizations/usage_report/claude_code
  - GET /v1/organizations/cost_report
byte-compatibly, so a real Admin key + base-URL swap is a drop-in replacement.
The agent service writes one record per run via /internal/record_run.
"""
import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import obs

DB_PATH = os.getenv("DB_PATH", "/data/analytics.db")
ORG_ID = os.getenv("POC_ORG_ID", "poc-org-00000000-0000-0000-0000-000000000001")
WORKSPACE_ID = os.getenv("POC_WORKSPACE_ID", "wrkspc-00000000-0000-0000-0000-000000000001")

app = FastAPI(title="Anthropic Usage & Cost API (POC Emulator)", version="1.0.0")
# Auto-trace every request + ship structured logs to OpenObserve.
obs.setup(app, "analytics-api")


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _conn() as c:
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS usage_runs (
                seq             INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id          TEXT,
                date            TEXT,
                model           TEXT,
                input_tokens    INTEGER,
                output_tokens   INTEGER,
                cache_read      INTEGER,
                cache_creation  INTEGER,
                num_sessions    INTEGER,
                actor_email     TEXT,
                cost_usd        REAL
            )
            """
        )


@app.on_event("startup")
def _startup() -> None:
    init_db()


# --------------------------------------------------------------------------- #
# Internal recording endpoint — agent writes one row per run.
# --------------------------------------------------------------------------- #
class RecordRequest(BaseModel):
    run_id: str
    model: str = "claude-sonnet-4-6"
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    num_sessions: int = 1
    actor_email: str = "agent@poc.local"
    cost_usd: float = 0.0


@app.post("/internal/record_run")
def record_run(req: RecordRequest) -> dict:
    today = datetime.now(timezone.utc).date().isoformat()
    with _conn() as c:
        c.execute(
            """INSERT INTO usage_runs
               (run_id, date, model, input_tokens, output_tokens, cache_read,
                cache_creation, num_sessions, actor_email, cost_usd)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (req.run_id, today, req.model, req.input_tokens, req.output_tokens,
             req.cache_read_tokens, req.cache_creation_tokens, req.num_sessions,
             req.actor_email, req.cost_usd),
        )
    obs.emit("analytics.record_run",
             f"recorded usage for {req.run_id}: in={req.input_tokens} out={req.output_tokens}",
             {"run_id": req.run_id, "model": req.model,
              "input_tokens": req.input_tokens, "output_tokens": req.output_tokens,
              "cost_usd": req.cost_usd})
    return {"recorded": True, "run_id": req.run_id}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _all_rows() -> list[sqlite3.Row]:
    with _conn() as c:
        return c.execute("SELECT * FROM usage_runs ORDER BY seq ASC").fetchall()


def _estimated_cost(r: sqlite3.Row) -> float:
    # Authoritative cost is the SDK-reported total_cost_usd stored at record time
    # (it accounts for cached input). Fall back to a list-price estimate only
    # when a stored cost is absent/zero.
    if r["cost_usd"]:
        return round(r["cost_usd"], 6)
    return round(r["input_tokens"] / 1e6 * 3.00 + r["output_tokens"] / 1e6 * 15.00, 6)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


# --------------------------------------------------------------------------- #
# usage_report/messages — matches Anthropic Usage & Cost API.
# --------------------------------------------------------------------------- #
@app.get("/v1/organizations/usage_report/messages")
def usage_messages(
    starting_at: str,
    ending_at: Optional[str] = None,
    bucket_width: str = Query("1d", pattern="^(1m|1h|1d)$"),
    group_by: list[str] = Query(default=[]),
    models: list[str] = Query(default=[]),
    service_tiers: list[str] = Query(default=[]),
) -> JSONResponse:
    rows = _all_rows()
    # Aggregate per (date, model) bucket.
    buckets: dict[tuple, dict] = {}
    for r in rows:
        if models and r["model"] not in models:
            continue
        key = (r["date"], r["model"])
        b = buckets.setdefault(key, {
            "date": r["date"],
            "model": r["model"],
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
            "service_tier": "standard",
            "workspace_id": WORKSPACE_ID,
            "organization_id": ORG_ID,
        })
        b["input_tokens"] += r["input_tokens"]
        b["output_tokens"] += r["output_tokens"]
        b["cache_read_tokens"] += r["cache_read"]
        b["cache_creation_tokens"] += r["cache_creation"]

    data = list(buckets.values())
    return JSONResponse(
        content={"data": data, "has_more": False, "next_page": None},
        media_type="application/json",
    )


# --------------------------------------------------------------------------- #
# usage_report/claude_code — matches Claude Code Analytics API.
# --------------------------------------------------------------------------- #
@app.get("/v1/organizations/usage_report/claude_code")
def usage_claude_code(
    starting_at: str,
    limit: int = Query(20, ge=1, le=1000),
    page: Optional[str] = None,
) -> JSONResponse:
    rows = _all_rows()
    # One record per (date, actor).
    by_actor: dict[tuple, list[sqlite3.Row]] = {}
    for r in rows:
        by_actor.setdefault((r["date"], r["actor_email"]), []).append(r)

    data = []
    for (d, email), group in by_actor.items():
        tot_in = sum(g["input_tokens"] for g in group)
        tot_out = sum(g["output_tokens"] for g in group)
        tot_cr = sum(g["cache_read"] for g in group)
        tot_cc = sum(g["cache_creation"] for g in group)
        tot_cost = round(sum(_estimated_cost(g) for g in group), 6)
        sessions = sum(g["num_sessions"] for g in group)
        model = group[-1]["model"]
        data.append({
            "date": d,
            "actor": {"type": "user_actor", "email_address": email},
            "organization_id": ORG_ID,
            "customer_type": "api",
            "terminal_type": "docker",
            "core_metrics": {
                "num_sessions": sessions,
                "lines_of_code": {"added": 0, "removed": 0},
                "commits_by_claude_code": 0,
                "pull_requests_by_claude_code": 0,
            },
            "tool_actions": {
                "edit_tool": {"accepted": 0, "rejected": 0},
                "multi_edit_tool": {"accepted": 0, "rejected": 0},
                "write_tool": {"accepted": 0, "rejected": 0},
                "notebook_edit_tool": {"accepted": 0, "rejected": 0},
            },
            "model_breakdown": [{
                "model": model,
                "tokens": {
                    "input": tot_in,
                    "output": tot_out,
                    "cache_read": tot_cr,
                    "cache_creation": tot_cc,
                },
                "estimated_cost": {
                    "currency": "USD",
                    "amount": tot_cost,
                },
            }],
        })

    return JSONResponse(
        content={"data": data, "has_more": False, "next_page": None},
        media_type="application/json",
    )


# --------------------------------------------------------------------------- #
# cost_report
# --------------------------------------------------------------------------- #
@app.get("/v1/organizations/cost_report")
def cost_report(
    starting_at: str,
    ending_at: Optional[str] = None,
    group_by: list[str] = Query(default=[]),
) -> JSONResponse:
    rows = _all_rows()
    buckets: dict[tuple, dict] = {}
    for r in rows:
        key = (r["date"], r["model"])
        b = buckets.setdefault(key, {
            "date": r["date"],
            "organization_id": ORG_ID,
            "workspace_id": WORKSPACE_ID,
            "model": r["model"],
            "amount": 0.0,
            "currency": "USD",
        })
        b["amount"] = round(b["amount"] + _estimated_cost(r), 6)

    return JSONResponse(
        content={"data": list(buckets.values()), "has_more": False, "next_page": None},
        media_type="application/json",
    )
