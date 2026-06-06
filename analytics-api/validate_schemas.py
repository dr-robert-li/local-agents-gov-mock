"""Schema-fidelity check for the Analytics API emulator.

Asserts the messages / claude_code / cost endpoints carry every required field
with the correct type and nesting. Exits non-zero on mismatch.
"""
import os
import sys
import json
import urllib.request

BASE = os.getenv("ANALYTICS_API_BASE", "http://localhost:8002")


def fetch(path: str) -> dict:
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        assert r.headers.get("content-type", "").startswith("application/json")
        return json.loads(r.read())


def check_messages() -> None:
    body = fetch("/v1/organizations/usage_report/messages"
                 "?starting_at=2026-01-01T00:00:00Z&ending_at=2026-12-31T00:00:00Z&bucket_width=1d")
    assert {"data", "has_more", "next_page"} <= set(body), "envelope keys missing"
    req = ["date", "model", "input_tokens", "output_tokens", "cache_read_tokens",
           "cache_creation_tokens", "service_tier", "workspace_id", "organization_id"]
    if not body["data"]:
        print("WARN messages: no records yet — run agent first. Envelope OK.")
        return
    for rec in body["data"]:
        for f in req:
            assert f in rec, f"messages record missing {f}"
        for f in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens"):
            assert isinstance(rec[f], int), f"{f} must be int"
    print(f"OK messages: {len(body['data'])} records.")


def check_claude_code() -> None:
    body = fetch("/v1/organizations/usage_report/claude_code?starting_at=2026-01-01")
    if not body["data"]:
        print("WARN claude_code: no records yet. Envelope OK.")
        return
    rec = body["data"][0]
    assert rec["actor"]["type"] == "user_actor", "actor.type wrong"
    assert "email_address" in rec["actor"], "actor.email_address missing"
    cm = rec["core_metrics"]
    for f in ("num_sessions", "lines_of_code", "commits_by_claude_code", "pull_requests_by_claude_code"):
        assert f in cm, f"core_metrics missing {f}"
    assert {"added", "removed"} <= set(cm["lines_of_code"]), "lines_of_code shape wrong"
    ta = rec["tool_actions"]
    for f in ("edit_tool", "multi_edit_tool", "write_tool", "notebook_edit_tool"):
        assert {"accepted", "rejected"} <= set(ta[f]), f"tool_actions.{f} shape wrong"
    mb = rec["model_breakdown"][0]
    assert {"input", "output", "cache_read", "cache_creation"} <= set(mb["tokens"]), "tokens shape"
    assert {"currency", "amount"} <= set(mb["estimated_cost"]), "estimated_cost shape"
    print(f"OK claude_code: {len(body['data'])} records.")


def check_cost() -> None:
    body = fetch("/v1/organizations/cost_report?starting_at=2026-01-01&ending_at=2026-12-31")
    assert {"data", "has_more", "next_page"} <= set(body), "cost envelope missing"
    print(f"OK cost_report: {len(body['data'])} records.")


def main() -> int:
    check_messages()
    check_claude_code()
    check_cost()
    print("ALL ANALYTICS SCHEMAS OK")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as e:
        print(f"SCHEMA FAIL: {e}")
        sys.exit(1)
