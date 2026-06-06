"""Perplexity (Sonar) Search MCP server configuration for the Agent SDK.

Runs the official `server-perplexity-ask` package over stdio — the SDK manages
the npx subprocess lifecycle. It wraps the Perplexity Sonar search API and
exposes the `perplexity_ask` tool. Returns an empty dict when no
PERPLEXITY_API_KEY is set so the agent degrades gracefully to yfinance-only
research.
"""
import os


def perplexity_mcp_servers() -> dict:
    api_key = os.getenv("PERPLEXITY_API_KEY", "").strip()
    if not api_key:
        return {}  # caller skips web-search tooling
    return {
        "perplexity-ask": {
            "command": "npx",
            "args": ["-y", "server-perplexity-ask"],
            "env": {"PERPLEXITY_API_KEY": api_key},
        }
    }


def perplexity_available() -> bool:
    return bool(os.getenv("PERPLEXITY_API_KEY", "").strip())
