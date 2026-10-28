"""Hash-chained event logs: each event carries a hash over the previous hash and itself,
so an edited, removed or reordered event is detectable. Used by the run record, the plate
custody log and the saved engine state."""
from __future__ import annotations

import hashlib
import json


def link_hash(prev: str, event: dict) -> str:
    """sha256 over the previous hash and the canonical event (without its own event_hash)."""
    body = {k: v for k, v in event.items() if k != "event_hash"}
    return hashlib.sha256((prev + json.dumps(body, sort_keys=True, default=str)).encode()).hexdigest()


def chain_events(events: list[dict]) -> str:
    """Add event_hash to each event (sha256 over previous hash + canonical event). Returns chain head."""
    prev = "0" * 64
    for e in events:
        e["event_hash"] = link_hash(prev, e)
        prev = e["event_hash"]
    return prev


def verify_chain(events: list[dict]) -> bool:
    prev = "0" * 64
    for e in events:
        if e.get("event_hash") != link_hash(prev, e):
            return False
        prev = e["event_hash"]
    return True
