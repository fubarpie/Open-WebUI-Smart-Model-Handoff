"""#8 never block a user message: every failure path degrades to 'stay on the current model'."""
import asyncio
import json

import pytest

import model_handoff
from model_handoff import Filter, HandoffSwitch

TARGET = "anthropic/claude-sonnet-4.5"


def _flt(monkeypatch):
    monkeypatch.setenv("WEBUI_SECRET_KEY", "unit-test-secret")
    f = Filter()
    f.valves.api_key = "k"
    f.valves.encrypt_api_keys = False
    f.valves.decision_backend = "chat"
    f.valves.js_ack_timeout_s = 0.2

    async def fake(model, messages, json_mode, timeout):
        return json.dumps({"scores": {"stay": 0.1, TARGET: 0.95}, "reason": "big task"})
    f._llm = fake
    return f


def _body():
    return {"model": "flash", "messages": [{"role": "user", "content": "refactor my app"}]}


def _run(f, call=None, emit=None, body=None):
    async def default_call(ev):
        return True
    body = body or _body()
    out = asyncio.run(f.inlet(body, __event_call__=call or default_call, __event_emitter__=emit,
                              __metadata__={"chat_id": "c1"}))
    return body, out


def test_switch_signal_still_escapes(monkeypatch):
    async def call(ev):
        return True
    with pytest.raises(HandoffSwitch, match="Switching"):
        _run(_flt(monkeypatch), call)


def test_decision_crash_returns_body(monkeypatch):
    f = _flt(monkeypatch)

    async def boom(*a, **k):
        raise RuntimeError("x")
    f._decide = boom
    body, out = _run(f)
    assert out is body


def test_unexpected_crash_anywhere_returns_body(monkeypatch):
    f = _flt(monkeypatch)
    monkeypatch.setattr(model_handoff, "classify", lambda *a, **k: (_ for _ in ()).throw(ValueError("bug")))
    body, out = _run(f)
    assert out is body


def test_unknown_choice_returns_body(monkeypatch):
    f = _flt(monkeypatch)
    monkeypatch.setattr(model_handoff, "classify", lambda *a, **k: ("switch", "not-a-target"))
    body, out = _run(f)
    assert out is body


def test_card_and_confirmation_both_fail_stays(monkeypatch):
    async def call(ev):
        raise RuntimeError("client gone")
    body, out = _run(_flt(monkeypatch), call)
    assert out is body


def test_failing_emitter_does_not_block_hint(monkeypatch):
    f = _flt(monkeypatch)
    monkeypatch.setattr(model_handoff, "classify", lambda *a, **k: ("hint", TARGET))

    async def emit(ev):
        raise RuntimeError("emitter down")
    body, out = _run(f, emit=emit)
    assert out is body


def test_failing_emitter_does_not_block_fallback(monkeypatch):
    f = _flt(monkeypatch)

    async def call(ev):
        code = ev["data"].get("code", "") if ev["type"] == "execute" else ""
        return None if "sessionStorage.setItem('chat-input'" in code else True

    async def emit(ev):
        raise RuntimeError("emitter down")
    body, out = _run(f, call, emit)
    assert out["messages"][-1] == body["messages"][-1]  # user message intact, reply still goes ahead


def test_prompt_builder_crash_returns_body(monkeypatch):
    f = _flt(monkeypatch)
    monkeypatch.setattr(model_handoff, "build_handoff_prompt", lambda **k: (_ for _ in ()).throw(KeyError("x")))
    body, out = _run(f)
    assert out is body
