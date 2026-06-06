"""Simulated portfolio persisted to SQLite.

Starts at USD 1000 cash. Recommendation application rules:
  BUY  -> allocate up to 10% of *current cash* to the ticker at current price
  SELL -> liquidate the entire position at current price
  HOLD -> no change
Tracks positions, cash_balance, total_equity, and run_history.
"""
import json
import os
import sqlite3
from datetime import datetime, timezone

DB_PATH = os.getenv("DB_PATH", "/data/portfolio.db")
STARTING_CASH = 1000.0


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _conn() as c:
        c.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        c.execute(
            """CREATE TABLE IF NOT EXISTS positions (
                   ticker   TEXT PRIMARY KEY,
                   qty      REAL NOT NULL,
                   avg_cost REAL NOT NULL
               )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS run_history (
                   run_id       TEXT PRIMARY KEY,
                   timestamp    TEXT NOT NULL,
                   cash_balance REAL NOT NULL,
                   total_equity REAL NOT NULL,
                   recommendations TEXT NOT NULL
               )"""
        )
        # Token/cost columns added in a later version — SQLite has no
        # ADD COLUMN IF NOT EXISTS, so guard each against the live schema.
        existing = {r[1] for r in c.execute("PRAGMA table_info(run_history)").fetchall()}
        for col in ("input_tokens", "output_tokens", "cache_read_tokens",
                    "cache_creation_tokens"):
            if col not in existing:
                c.execute(f"ALTER TABLE run_history ADD COLUMN {col} INTEGER DEFAULT 0")
        if "cost_usd" not in existing:
            c.execute("ALTER TABLE run_history ADD COLUMN cost_usd REAL DEFAULT 0")

        # Seed starting cash exactly once.
        row = c.execute("SELECT value FROM meta WHERE key='cash_balance'").fetchone()
        if row is None:
            c.execute("INSERT INTO meta (key, value) VALUES ('cash_balance', ?)",
                      (str(STARTING_CASH),))


def get_cash() -> float:
    with _conn() as c:
        row = c.execute("SELECT value FROM meta WHERE key='cash_balance'").fetchone()
    return float(row["value"]) if row else STARTING_CASH


def _set_cash(value: float) -> None:
    with _conn() as c:
        c.execute("UPDATE meta SET value=? WHERE key='cash_balance'", (str(value),))


def get_positions() -> list[dict]:
    with _conn() as c:
        rows = c.execute("SELECT ticker, qty, avg_cost FROM positions WHERE qty > 0").fetchall()
    return [dict(r) for r in rows]


def _upsert_position(ticker: str, qty: float, avg_cost: float) -> None:
    with _conn() as c:
        if qty <= 1e-9:
            c.execute("DELETE FROM positions WHERE ticker=?", (ticker,))
        else:
            c.execute(
                """INSERT INTO positions (ticker, qty, avg_cost) VALUES (?,?,?)
                   ON CONFLICT(ticker) DO UPDATE SET qty=excluded.qty, avg_cost=excluded.avg_cost""",
                (ticker, qty, avg_cost),
            )


def _position(ticker: str) -> dict | None:
    with _conn() as c:
        row = c.execute("SELECT ticker, qty, avg_cost FROM positions WHERE ticker=?",
                        (ticker,)).fetchone()
    return dict(row) if row else None


def apply_recommendations(recommendations: list[dict], prices: dict[str, float]) -> None:
    """Mutate cash + positions per BUY/SELL/HOLD rules. Robust to missing prices."""
    cash = get_cash()
    for rec in recommendations:
        ticker = rec.get("ticker")
        action = (rec.get("recommendation") or "HOLD").upper()
        price = prices.get(ticker) or rec.get("price")
        if not ticker or not price or price <= 0:
            continue

        if action == "BUY":
            budget = cash * 0.10                      # up to 10% of current cash
            qty = budget / price
            if qty <= 0:
                continue
            existing = _position(ticker)
            if existing:
                total_qty = existing["qty"] + qty
                new_avg = (existing["qty"] * existing["avg_cost"] + qty * price) / total_qty
            else:
                total_qty, new_avg = qty, price
            cash -= qty * price
            _upsert_position(ticker, total_qty, new_avg)

        elif action == "SELL":
            existing = _position(ticker)
            if existing:
                cash += existing["qty"] * price        # liquidate fully
                _upsert_position(ticker, 0, 0)

    _set_cash(round(cash, 6))


def compute_equity(prices: dict[str, float]) -> tuple[float, float, list[dict]]:
    """Return (cash, total_equity, enriched_positions) marked to current prices."""
    cash = get_cash()
    enriched = []
    holdings_value = 0.0
    for p in get_positions():
        price = prices.get(p["ticker"], p["avg_cost"])
        market_value = p["qty"] * price
        holdings_value += market_value
        enriched.append({
            "ticker": p["ticker"],
            "qty": round(p["qty"], 6),
            "avg_cost": round(p["avg_cost"], 4),
            "current_price": round(price, 4),
            "market_value": round(market_value, 2),
            "pnl": round((price - p["avg_cost"]) * p["qty"], 2),
        })
    return round(cash, 2), round(cash + holdings_value, 2), enriched


def record_run(run_id: str, recommendations: list[dict], prices: dict[str, float],
               usage: dict | None = None) -> dict:
    cash, equity, positions = compute_equity(prices)
    ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    u = usage or {}
    with _conn() as c:
        c.execute(
            """INSERT OR REPLACE INTO run_history
               (run_id, timestamp, cash_balance, total_equity, recommendations,
                input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens, cost_usd)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (run_id, ts, cash, equity, json.dumps(recommendations),
             int(u.get("input_tokens", 0)), int(u.get("output_tokens", 0)),
             int(u.get("cache_read", 0)), int(u.get("cache_creation", 0)),
             float(u.get("cost_usd", 0.0))),
        )
    return {"run_id": run_id, "timestamp": ts, "cash_balance": cash,
            "total_equity": equity, "positions": positions,
            "recommendations": recommendations, "token_usage": _run_tokens(u)}


def _run_tokens(u: dict) -> dict:
    return {"input_tokens": int(u.get("input_tokens", 0)),
            "output_tokens": int(u.get("output_tokens", 0)),
            "cache_read_tokens": int(u.get("cache_read", 0)),
            "cache_creation_tokens": int(u.get("cache_creation", 0)),
            "cost_usd": round(float(u.get("cost_usd", 0.0)), 6)}


def snapshot() -> dict:
    """Full portfolio snapshot for the web UI (no live prices — uses last-known)."""
    with _conn() as c:
        runs = c.execute(
            "SELECT * FROM run_history ORDER BY timestamp ASC"
        ).fetchall()
    history = [
        {"run_id": r["run_id"], "timestamp": r["timestamp"],
         "cash_balance": r["cash_balance"], "total_equity": r["total_equity"],
         "input_tokens": r["input_tokens"], "output_tokens": r["output_tokens"],
         "cache_read_tokens": r["cache_read_tokens"],
         "cache_creation_tokens": r["cache_creation_tokens"], "cost_usd": r["cost_usd"]}
        for r in runs
    ]
    # Cumulative token consumption + cost across all runs.
    totals = {
        "input_tokens": sum(r["input_tokens"] or 0 for r in runs),
        "output_tokens": sum(r["output_tokens"] or 0 for r in runs),
        "cache_read_tokens": sum(r["cache_read_tokens"] or 0 for r in runs),
        "cache_creation_tokens": sum(r["cache_creation_tokens"] or 0 for r in runs),
        "total_tokens": sum((r["input_tokens"] or 0) + (r["output_tokens"] or 0)
                            + (r["cache_read_tokens"] or 0) + (r["cache_creation_tokens"] or 0)
                            for r in runs),
        "cost_usd": round(sum(r["cost_usd"] or 0.0 for r in runs), 6),
    }
    last = runs[-1] if runs else None
    cash, equity, positions = compute_equity({})  # mark to avg cost when no live prices
    return {
        "cash_balance": cash,
        "total_equity": equity,
        "positions": positions,
        "last_run": (last["timestamp"] if last else None),
        "last_recommendations": (json.loads(last["recommendations"]) if last else []),
        "equity_history": history,
        "starting_cash": STARTING_CASH,
        "token_usage": totals,
    }
