"""Shared helpers for fake __event_call__ implementations."""


def is_probe(ev) -> bool:
    """True for the client-detection probe (_detect_client) sent before any card/confirmation/handoff."""
    return (isinstance(ev, dict) and ev.get("type") == "execute"
            and "return 'browser'" in (ev.get("data") or {}).get("code", ""))
