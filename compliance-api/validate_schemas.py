"""Schema-fidelity check for the Compliance API emulator.

Fetches a live sample from the local emulator and asserts every required
Anthropic audit-event field is present with the correct type. Exits non-zero
on any mismatch so it can gate CI.
"""
import os
import sys
import urllib.request
import json

BASE = os.getenv("COMPLIANCE_API_BASE", "http://localhost:8001")

REQUIRED = {
    "id": str,
    "created_at": str,
    "actor_info": dict,
    "event": str,
    "event_info": dict,
    "entity_info": dict,
    "ip_address": str,
    "device_id": str,
    "user_agent": str,
    "client_platform": str,
}


def fetch(path: str) -> dict:
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        assert r.headers.get("content-type", "").startswith("application/json"), \
            "Content-Type must be application/json"
        return json.loads(r.read())


def main() -> int:
    body = fetch("/v1/organizations/audit_events?limit=10")
    for key in ("data", "has_more", "next_page"):
        assert key in body, f"missing envelope key: {key}"
    assert isinstance(body["data"], list), "data must be a list"
    assert isinstance(body["has_more"], bool), "has_more must be a bool"

    if not body["data"]:
        print("WARN: no events seeded yet — run the agent first. Envelope OK.")
        return 0

    for ev in body["data"]:
        for field, typ in REQUIRED.items():
            assert field in ev, f"event missing field: {field}"
            assert isinstance(ev[field], typ), f"{field} should be {typ.__name__}"
        assert ev["actor_info"]["type"] in ("user", "api_key"), "bad actor_info.type"

    # Single-event endpoint round-trips.
    one = fetch("/v1/organizations/audit_events/" + body["data"][0]["id"])
    assert one["id"] == body["data"][0]["id"], "single-event fetch mismatch"

    print(f"OK: {len(body['data'])} events validated against Anthropic schema.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as e:
        print(f"SCHEMA FAIL: {e}")
        sys.exit(1)
