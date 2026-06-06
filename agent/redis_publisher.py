"""Publishes agent-stream events to Redis pub/sub for the web UI to relay over SSE.

Channel: `agent-stream`. Each event is a JSON object with a `type` discriminator
(`session`, `assistant`, `thinking`, `tool_use`, `tool_result`, `recommendation`,
`portfolio`, `run_start`, `run_end`). Failures never break the agent run.
"""
import json
import os
from datetime import datetime, timezone

import redis

CHANNEL = "agent-stream"
_client: redis.Redis | None = None


def _conn() -> redis.Redis | None:
    global _client
    if _client is None:
        url = os.getenv("REDIS_URL", "redis://redis:6379")
        try:
            _client = redis.Redis.from_url(url, decode_responses=True,
                                           socket_connect_timeout=3)
            _client.ping()
        except Exception as e:  # noqa: BLE001
            print(f"[redis] connect failed: {e}", flush=True)
            _client = None
    return _client


def publish(event_type: str, payload: dict) -> None:
    """Best-effort publish. Also kept in a capped Redis list for run replay."""
    event = {
        "type": event_type,
        "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        **payload,
    }
    conn = _conn()
    if conn is None:
        return
    try:
        msg = json.dumps(event)
        conn.publish(CHANNEL, msg)
        # Keep the last 500 events so a freshly-connected browser can replay.
        conn.rpush("agent-stream-replay", msg)
        conn.ltrim("agent-stream-replay", -500, -1)
    except Exception as e:  # noqa: BLE001
        print(f"[redis] publish failed: {e}", flush=True)


def reset_replay() -> None:
    """Clear the replay buffer at the start of a new run."""
    conn = _conn()
    if conn is None:
        return
    try:
        conn.delete("agent-stream-replay")
    except Exception:  # noqa: BLE001
        pass
