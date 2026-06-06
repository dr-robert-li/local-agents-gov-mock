"""Anthropic Compliance API emulator.

Reproduces the `/v1/organizations/audit_events` contract exactly so that a real
Admin API key + base-URL swap is a drop-in replacement. Events are stored in
SQLite and seeded by the agent service after each run via an internal endpoint.
"""
import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import obs

DB_PATH = os.getenv("DB_PATH", "/data/compliance.db")
ORG_ID = os.getenv("POC_ORG_ID", "poc-org-00000000-0000-0000-0000-000000000001")

app = FastAPI(title="Anthropic Compliance API (POC Emulator)", version="1.0.0")
# Auto-trace every request + ship structured logs to OpenObserve.
obs.setup(app, "compliance-api")


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #
def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _conn() as c:
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_events (
                seq        INTEGER PRIMARY KEY AUTOINCREMENT,  -- stable cursor ordering
                id         TEXT UNIQUE NOT NULL,
                created_at TEXT NOT NULL,
                event      TEXT NOT NULL,
                payload    TEXT NOT NULL                       -- full JSON event body
            )
            """
        )


@app.on_event("startup")
def _startup() -> None:
    init_db()


# --------------------------------------------------------------------------- #
# Canonical event construction
# --------------------------------------------------------------------------- #
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _build_event(event_type: str, actor: dict, event_info: dict, entity_info: dict) -> dict:
    """Assemble a single audit event matching the canonical Anthropic schema."""
    return {
        "id": str(uuid.uuid4()),
        "created_at": _now_iso(),
        "actor_info": actor,
        "event": event_type,
        "event_info": event_info,
        "entity_info": entity_info,
        "ip_address": "203.0.113.42",          # TEST-NET-3 documentation range
        "device_id": "device_" + uuid.uuid4().hex[:16],
        "user_agent": "claude-agent-sdk/0.1.0 (python/3.12)",
        "client_platform": "api",
    }


def _persist(event: dict) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO audit_events (id, created_at, event, payload) VALUES (?,?,?,?)",
            (event["id"], event["created_at"], event["event"], json.dumps(event)),
        )


# --------------------------------------------------------------------------- #
# Internal seeding endpoint — called by the agent after each run.
# --------------------------------------------------------------------------- #
class SeedRequest(BaseModel):
    session_id: str
    conversation_id: str
    actor_email: str = "agent@poc.local"
    turns: int = 1                       # one conversation_message_sent per turn
    tool_calls: int = 1                  # tool_use_initiated / tool_result_received pairs
    tickers: list[str] = []


@app.post("/internal/seed_run")
def seed_run(req: SeedRequest) -> dict:
    """Emit the canonical event sequence for one completed agent run."""
    user_actor = {"type": "user", "email_address": req.actor_email}
    api_actor = {"type": "api_key", "api_key_id": "apikey_" + uuid.uuid4().hex[:16]}
    convo_entity = {"type": "conversation", "id": req.conversation_id}

    created: list[dict] = []

    def emit(ev: dict) -> None:
        _persist(ev)
        created.append(ev)

    emit(_build_event("conversation_created", user_actor,
                      {"session_id": req.session_id}, convo_entity))

    for i in range(max(1, req.turns)):
        emit(_build_event("conversation_message_sent", api_actor,
                          {"turn": i, "role": "assistant"}, convo_entity))

    for i in range(max(1, req.tool_calls)):
        tool_entity = {"type": "tool_invocation", "id": "tooluse_" + uuid.uuid4().hex[:12]}
        emit(_build_event("tool_use_initiated", api_actor,
                          {"tool_name": "Bash", "index": i}, tool_entity))
        emit(_build_event("tool_result_received", api_actor,
                          {"tool_name": "Bash", "index": i, "is_error": False}, tool_entity))

    # Portfolio snapshot persisted as a file upload artifact.
    emit(_build_event("file_uploaded", user_actor,
                      {"filename": "portfolio_snapshot.json", "tickers": req.tickers},
                      {"type": "file", "id": "file_" + uuid.uuid4().hex[:16]}))

    emit(_build_event("conversation_completed", user_actor,
                      {"session_id": req.session_id, "status": "succeeded"}, convo_entity))

    obs.emit("compliance.seed_run",
             f"seeded {len(created)} audit events for {req.conversation_id}",
             {"conversation_id": req.conversation_id, "events": len(created),
              "tickers": ",".join(req.tickers)})
    return {"created": len(created), "ids": [e["id"] for e in created]}


# --------------------------------------------------------------------------- #
# Public Compliance API contract
# --------------------------------------------------------------------------- #
@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/v1/organizations/audit_events")
def list_audit_events(
    limit: int = Query(20, ge=1, le=1000),
    starting_after: Optional[str] = None,
    ending_before: Optional[str] = None,
) -> JSONResponse:
    """Cursor-paginated audit event feed (newest first).

    `starting_after` / `ending_before` are event ids, matching the Anthropic
    cursor-pagination convention. Returns `{data, has_more, next_page}`.
    """
    with _conn() as c:
        # Resolve cursor ids to their stable seq values.
        def seq_of(event_id: str) -> Optional[int]:
            row = c.execute("SELECT seq FROM audit_events WHERE id=?", (event_id,)).fetchone()
            return row["seq"] if row else None

        clauses, params = [], []
        if starting_after:
            s = seq_of(starting_after)
            if s is not None:
                clauses.append("seq < ?")   # newest-first: "after" = older rows
                params.append(s)
        if ending_before:
            e = seq_of(ending_before)
            if e is not None:
                clauses.append("seq > ?")
                params.append(e)

        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        # Fetch one extra row to compute has_more.
        rows = c.execute(
            f"SELECT payload FROM audit_events {where} ORDER BY seq DESC LIMIT ?",
            (*params, limit + 1),
        ).fetchall()

    has_more = len(rows) > limit
    rows = rows[:limit]
    data = [json.loads(r["payload"]) for r in rows]
    next_page = data[-1]["id"] if (has_more and data) else None

    return JSONResponse(
        content={"data": data, "has_more": has_more, "next_page": next_page},
        media_type="application/json",
    )


@app.get("/v1/organizations/audit_events/{event_id}")
def get_audit_event(event_id: str) -> dict:
    with _conn() as c:
        row = c.execute("SELECT payload FROM audit_events WHERE id=?", (event_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="audit event not found")
    return json.loads(row["payload"])
