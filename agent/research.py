"""Stock research engine.

Per ticker: pull market data from yfinance, then ask Claude (via the Agent SDK
query() loop) for a BUY/SELL/HOLD recommendation. The SDK path is used whenever
ANTHROPIC_API_KEY is present; otherwise a deterministic heuristic built on the
52-week range produces a recommendation so the whole stack still runs end-to-end.

All agent messages, thinking blocks, tool calls and results are streamed to Redis
(for the UI) and to OpenObserve (as spans/logs), and captured in Anthropic
Messages API shape for the Claude Console replay shim.
"""
import json
import os
import re
import sqlite3
import uuid
from datetime import datetime, timezone

import yfinance as yf

import telemetry
import redis_publisher as rp
from mcp_config import perplexity_mcp_servers, perplexity_available

TICKERS = ["AAPL", "MSFT", "NVDA", "GOOGL", "META", "AMZN", "TSLA", "AMD", "AVGO", "ORCL"]
# Default model is claude-sonnet-4-6 (current, balanced speed/intelligence).
# Override via the AGENT_MODEL env var.
MODEL = os.getenv("AGENT_MODEL", "claude-sonnet-4-6")
DB_PATH = os.getenv("DB_PATH", "/data/portfolio.db")


# --------------------------------------------------------------------------- #
# Session storage (Anthropic Messages API shape) for the Console replay shim.
# --------------------------------------------------------------------------- #
def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_sessions_db() -> None:
    with _conn() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS sessions (
                   session_id TEXT PRIMARY KEY,
                   created_at TEXT,
                   run_id     TEXT,
                   messages   TEXT
               )"""
        )


def _save_session(session_id: str, run_id: str, messages: list[dict]) -> None:
    with _conn() as c:
        c.execute(
            """INSERT OR REPLACE INTO sessions (session_id, created_at, run_id, messages)
               VALUES (?,?,?,?)""",
            (session_id, datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
             run_id, json.dumps(messages)),
        )


def list_sessions() -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT session_id, created_at, run_id FROM sessions ORDER BY created_at DESC"
        ).fetchall()
    return [{"id": r["session_id"], "created_at": r["created_at"], "run_id": r["run_id"]}
            for r in rows]


def get_session_messages(session_id: str) -> list[dict] | None:
    with _conn() as c:
        row = c.execute("SELECT messages FROM sessions WHERE session_id=?",
                        (session_id,)).fetchone()
    return json.loads(row["messages"]) if row else None


# --------------------------------------------------------------------------- #
# Market data
# --------------------------------------------------------------------------- #
def fetch_market_data(ticker: str) -> dict:
    """Pull price/volume/PE/52w range + recent news. Tolerant of yfinance errors."""
    out = {"ticker": ticker, "price": None, "volume": None, "pe": None,
           "high_52w": None, "low_52w": None, "news": []}
    try:
        t = yf.Ticker(ticker)
        info = {}
        try:
            info = t.fast_info if hasattr(t, "fast_info") else {}
        except Exception:  # noqa: BLE001
            info = {}
        # fast_info covers price/volume/range without a slow full .info call.
        out["price"] = _num(getattr(info, "last_price", None) or info.get("lastPrice")
                            if hasattr(info, "get") else getattr(info, "last_price", None))
        out["high_52w"] = _num(getattr(info, "year_high", None))
        out["low_52w"] = _num(getattr(info, "year_low", None))
        out["volume"] = _num(getattr(info, "last_volume", None))

        # Fall back to history if fast_info was empty.
        if out["price"] is None:
            hist = t.history(period="5d")
            if not hist.empty:
                out["price"] = _num(hist["Close"].iloc[-1])
                out["volume"] = _num(hist["Volume"].iloc[-1])
        if out["high_52w"] is None or out["low_52w"] is None:
            hist = t.history(period="1y")
            if not hist.empty:
                out["high_52w"] = _num(hist["High"].max())
                out["low_52w"] = _num(hist["Low"].min())

        # P/E from the (slower) info dict, guarded.
        try:
            full = t.info or {}
            out["pe"] = _num(full.get("trailingPE"))
            if out["price"] is None:
                out["price"] = _num(full.get("currentPrice") or full.get("regularMarketPrice"))
        except Exception:  # noqa: BLE001
            pass

        try:
            news = t.news or []
            out["news"] = [n.get("title") or n.get("content", {}).get("title")
                           for n in news[:5] if isinstance(n, dict)]
            out["news"] = [h for h in out["news"] if h]
        except Exception:  # noqa: BLE001
            out["news"] = []
    except Exception as e:  # noqa: BLE001
        print(f"[yfinance] {ticker} failed: {e}", flush=True)
    return out


def _num(v):
    try:
        if v is None:
            return None
        f = float(v)
        return f if f == f else None  # filter NaN
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Heuristic fallback recommendation
# --------------------------------------------------------------------------- #
def heuristic_recommendation(data: dict) -> dict:
    """52-week-range mean reversion: cheap -> BUY, expensive -> SELL, middle -> HOLD."""
    price, lo, hi = data.get("price"), data.get("low_52w"), data.get("high_52w")
    rec, conf, why = "HOLD", 0.5, "Insufficient data; holding."
    if price and lo and hi and hi > lo:
        pos = (price - lo) / (hi - lo)            # 0 = at low, 1 = at high
        if pos <= 0.35:
            rec, conf = "BUY", round(0.6 + (0.35 - pos), 2)
            why = f"Trading at {pos:.0%} of 52-week range — near lows, value entry."
        elif pos >= 0.80:
            rec, conf = "SELL", round(0.55 + (pos - 0.80), 2)
            why = f"Trading at {pos:.0%} of 52-week range — extended, trimming."
        else:
            rec, conf, why = "HOLD", 0.5, f"Mid-range at {pos:.0%} of 52-week band."
    return {
        "ticker": data["ticker"],
        "price": round(price, 2) if price else None,
        "recommendation": rec,
        "confidence": min(conf, 0.95),
        "rationale": why,
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


# --------------------------------------------------------------------------- #
# Agent SDK research
# --------------------------------------------------------------------------- #
def _build_prompt(data: dict) -> str:
    news = "\n".join(f"- {h}" for h in data.get("news", [])) or "- (none retrieved)"
    web_hint = (f"You MUST call the mcp__perplexity-ask__perplexity_ask tool exactly once to "
                f"fetch one recent analyst headline / sentiment for {data['ticker']}, then "
                f"factor it into your call.") if perplexity_available() else \
               "Web search is unavailable; reason from the data provided."
    return f"""You are a disciplined equity analyst. Analyse {data['ticker']}.

Market data:
- price: {data.get('price')}
- volume: {data.get('volume')}
- trailing P/E: {data.get('pe')}
- 52-week high: {data.get('high_52w')}
- 52-week low: {data.get('low_52w')}
Recent headlines:
{news}

{web_hint}

First, use the Bash tool to compute where the price sits in the 52-week range, e.g.:
  python3 -c "print(round(({data.get('price')}-{data.get('low_52w')})/({data.get('high_52w')}-{data.get('low_52w')})*100,1))"
Then decide BUY, SELL, or HOLD. Respond with ONLY a JSON object on the final line:
{{"ticker":"{data['ticker']}","price":<number>,"recommendation":"BUY|SELL|HOLD","confidence":<0.0-1.0>,"rationale":"<one sentence>"}}"""


def _extract_json(text: str, data: dict) -> dict | None:
    """Pull the last JSON object out of the model's final text."""
    matches = re.findall(r"\{[^{}]*\"recommendation\"[^{}]*\}", text, re.DOTALL)
    if not matches:
        return None
    try:
        obj = json.loads(matches[-1])
        return {
            "ticker": obj.get("ticker", data["ticker"]),
            "price": obj.get("price") or data.get("price"),
            "recommendation": str(obj.get("recommendation", "HOLD")).upper(),
            "confidence": float(obj.get("confidence", 0.5)),
            "rationale": obj.get("rationale", ""),
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
    except (json.JSONDecodeError, ValueError, TypeError):
        return None


async def research_ticker(data: dict, run_id: str) -> tuple[dict, dict, list[dict], str]:
    """Return (recommendation, usage, session_messages, session_id).

    Uses the Agent SDK when ANTHROPIC_API_KEY is set; otherwise heuristic fallback.
    """
    ticker = data["ticker"]
    usage = {"input_tokens": 0, "output_tokens": 0, "cache_read": 0,
             "cache_creation": 0, "cost_usd": 0.0}
    session_id = "sess_" + uuid.uuid4().hex
    messages: list[dict] = []
    tracer = telemetry.get_tracer()

    if not os.getenv("ANTHROPIC_API_KEY", "").strip():
        # No API key: deterministic heuristic so the whole stack still runs.
        # We additionally emit a CLEARLY-SIMULATED agent.tool_use span + SSE
        # events so OpenObserve streams and the UI demonstrate the real shape.
        rec = heuristic_recommendation(data)
        with tracer.start_as_current_span("agent.run.ticker") as span:
            span.set_attribute("run_id", run_id)
            span.set_attribute("ticker", ticker)
            span.set_attribute("model", MODEL)
            span.set_attribute("simulated", True)   # mark non-Claude origin
            rp.publish("assistant", {"ticker": ticker,
                                     "text": f"[simulated/no-key] {ticker}: {rec['recommendation']} "
                                             f"({rec['confidence']}) — {rec['rationale']}"})
            telemetry.log_event("agent.message", f"[simulated] {ticker} {rec['recommendation']}",
                                {"session_id": session_id, "role": "assistant", "simulated": True})
            # Representative Bash tool_use span (what the real loop would emit).
            tool_input = {"command": f"python -c \"import yfinance; print('{ticker}')\""}
            rp.publish("tool_use", {"ticker": ticker, "tool_name": "Bash",
                                    "input": tool_input, "simulated": True})
            with tracer.start_as_current_span("agent.tool_use") as ts:
                ts.set_attribute("tool_name", "Bash")
                ts.set_attribute("tool_input", json.dumps(tool_input))
                ts.set_attribute("tool_result_preview", f"{ticker} price={data.get('price')}")
                ts.set_attribute("simulated", True)
            rp.publish("tool_result", {"ticker": ticker,
                                       "preview": f"price={data.get('price')}", "simulated": True})
            rp.publish("recommendation", {"ticker": ticker, "recommendation": rec})
        # Synthetic usage so downstream analytics has data even without a key.
        usage = {"input_tokens": 150, "output_tokens": 60, "cache_read": 0,
                 "cache_creation": 0, "cost_usd": 0.00036}
        messages = _console_messages(ticker, rec, fallback=True)
        return rec, usage, messages, session_id

    # ---- Real Agent SDK loop ----
    try:
        from claude_agent_sdk import query, ClaudeAgentOptions
    except Exception as e:  # noqa: BLE001
        print(f"[agent-sdk] import failed ({e}); using heuristic.", flush=True)
        rec = heuristic_recommendation(data)
        usage = {"input_tokens": 150, "output_tokens": 60, "cost_usd": 0.00036}
        return rec, usage, _console_messages(ticker, rec, fallback=True), session_id

    # NOTE: we pre-authorize tools via `allowed_tools` rather than
    # permission_mode="bypassPermissions". The latter makes the Claude Code CLI
    # pass --dangerously-skip-permissions, which the CLI refuses to run under
    # root (this container is root) — listing the tools explicitly grants the
    # same no-prompt execution without that flag.
    options = ClaudeAgentOptions(
        model=MODEL,
        allowed_tools=["Bash", "mcp__perplexity-ask__perplexity_ask"],
        mcp_servers=perplexity_mcp_servers(),
        max_turns=15,                       # hard cost guard per ticker
        system_prompt="You are a precise equity research analyst. Be concise.",
    )

    final_text = ""
    content_blocks: list[dict] = []
    with tracer.start_as_current_span("agent.run.ticker") as span:
        span.set_attribute("run_id", run_id)
        span.set_attribute("ticker", ticker)
        span.set_attribute("model", MODEL)
        try:
            async for message in query(prompt=_build_prompt(data), options=options):
                session_id = _handle_message(message, ticker, session_id, final_text,
                                             content_blocks, usage, tracer)
                # Accumulate assistant text for JSON extraction.
                t = _message_text(message)
                if t:
                    final_text += "\n" + t
        except Exception as e:  # noqa: BLE001
            print(f"[agent-sdk] {ticker} query error: {e}; partial result kept.", flush=True)
            rp.publish("assistant", {"ticker": ticker, "text": f"[error] {ticker}: {e}"})

    rec = _extract_json(final_text, data) or heuristic_recommendation(data)
    rp.publish("recommendation", {"ticker": ticker, "recommendation": rec})
    messages = [{
        "id": "msg_" + uuid.uuid4().hex, "type": "message", "role": "assistant",
        "content": content_blocks or [{"type": "text", "text": final_text.strip()}],
        "model": MODEL, "stop_reason": "end_turn",
        "usage": {"input_tokens": usage["input_tokens"], "output_tokens": usage["output_tokens"]},
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }]
    return rec, usage, messages, session_id


def _message_text(message) -> str:
    text = ""
    content = getattr(message, "content", None)
    if isinstance(content, list):
        for block in content:
            if type(block).__name__ == "TextBlock":
                text += getattr(block, "text", "")
    return text


def _handle_message(message, ticker, session_id, final_text, content_blocks, usage, tracer) -> str:
    """Stream one SDK message to Redis + OpenObserve; capture content blocks."""
    name = type(message).__name__

    if name == "SystemMessage":
        data = getattr(message, "data", {}) or {}
        sid = data.get("session_id")
        if sid:
            session_id = sid
        rp.publish("session", {"ticker": ticker, "session_id": session_id})

    elif name == "AssistantMessage":
        for block in getattr(message, "content", []) or []:
            bname = type(block).__name__
            if bname == "TextBlock":
                txt = getattr(block, "text", "")
                content_blocks.append({"type": "text", "text": txt})
                rp.publish("assistant", {"ticker": ticker, "text": txt})
                telemetry.log_event("agent.message", txt,
                                    {"session_id": session_id, "role": "assistant"})
            elif bname == "ThinkingBlock":
                think = getattr(block, "thinking", "")
                content_blocks.append({"type": "thinking", "thinking": think})
                rp.publish("thinking", {"ticker": ticker, "text": think})
                telemetry.log_event("agent.thinking", think,
                                    {"thinking_budget_tokens": len(think.split())})
            elif bname == "ToolUseBlock":
                tname = getattr(block, "name", "")
                tinput = getattr(block, "input", {})
                content_blocks.append({"type": "tool_use", "id": getattr(block, "id", ""),
                                       "name": tname, "input": tinput})
                rp.publish("tool_use", {"ticker": ticker, "tool_name": tname, "input": tinput})
                is_mcp = tname.startswith("mcp__perplexity")
                span_name = "agent.mcp_call" if is_mcp else "agent.tool_use"
                with tracer.start_as_current_span(span_name) as s:
                    s.set_attribute("tool_name", tname)
                    s.set_attribute("tool_input", json.dumps(tinput)[:500])
                    if is_mcp:
                        s.set_attribute("mcp_server", "perplexity-ask")
                        s.set_attribute("query", str(tinput.get("messages", tinput.get("query", ""))))

    elif name == "UserMessage":
        for block in getattr(message, "content", []) or []:
            if type(block).__name__ == "ToolResultBlock":
                result = getattr(block, "content", "")
                preview = json.dumps(result)[:300] if not isinstance(result, str) else result[:300]
                rp.publish("tool_result", {"ticker": ticker, "preview": preview})

    elif name == "ResultMessage":
        u = getattr(message, "usage", None) or {}
        if isinstance(u, dict):
            usage["input_tokens"] += int(u.get("input_tokens", 0))
            usage["output_tokens"] += int(u.get("output_tokens", 0))
            usage["cache_read"] += int(u.get("cache_read_input_tokens", 0))
            usage["cache_creation"] += int(u.get("cache_creation_input_tokens", 0))
        cost = getattr(message, "total_cost_usd", None)
        if cost:
            usage["cost_usd"] += float(cost)

    return session_id


def _console_messages(ticker: str, rec: dict, fallback: bool = False) -> list[dict]:
    text = (f"{ticker}: {rec['recommendation']} (confidence {rec['confidence']}). "
            f"{rec['rationale']}")
    return [{
        "id": "msg_" + uuid.uuid4().hex,
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": text}],
        "model": MODEL,
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 150 if fallback else 0, "output_tokens": 60 if fallback else 0},
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }]


def save_session(session_id: str, run_id: str, messages: list[dict]) -> None:
    _save_session(session_id, run_id, messages)
