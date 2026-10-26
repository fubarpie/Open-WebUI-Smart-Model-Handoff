"""v0.7.0: graceful text fallback when the client cannot run the handoff script (mobile apps)."""
import asyncio
import json

import pytest

from model_handoff import Filter

TARGET = "anthropic/claude-sonnet-4.5"
EXPECTED = ("This looks like a good fit for {n}, but your current app doesn't support automatic chat "
            "switching. Please copy your prompt and start a new chat with the {n} model.")


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
    f._name = next(t["name"] for t in json.loads(f.valves.targets_json) if t["id"] == TARGET)
    return f


def _body(extra=None):
    msgs = (extra or []) + [{"role": "user", "content": "refactor my app"}]
    return {"model": "flash", "messages": msgs}


def _run(f, nav_result, body=None, chat="c1", events=None):
    """Card popup always accepts; the navigation script behaves per nav_result."""
    async def call(ev):
        code = ev["data"].get("code", "") if ev["type"] == "execute" else ""
        if "sessionStorage.setItem('chat-input'" in code:  # the navigation script
            if callable(nav_result):
                return await nav_result()
            return nav_result
        return True  # the card popup

    async def emit(ev):
        if events is not None:
            events.append(ev)

    return asyncio.run(f.inlet(body or _body(), __event_call__=call, __event_emitter__=emit,
                               __metadata__={"chat_id": chat}))


def test_script_confirmed_still_raises(monkeypatch):
    f = _flt(monkeypatch)
    with pytest.raises(Exception, match="Switching"):
        _run(f, True)


@pytest.mark.parametrize("result", [None, False, "", {"result": None}])
def test_unconfirmed_result_falls_back(monkeypatch, result):
    f = _flt(monkeypatch)
    out = _run(f, result)
    assert isinstance(out, dict) and out["messages"][-1]["content"] == "refactor my app"


def test_timeout_falls_back(monkeypatch):
    f = _flt(monkeypatch)

    async def hang():
        await asyncio.sleep(5)
        return True
    out = _run(f, hang)
    assert out["model"] == "flash"  # returned, not raised


def test_client_error_falls_back(monkeypatch):
    f = _flt(monkeypatch)

    async def boom():
        raise RuntimeError("no listener")
    assert _run(f, boom)["model"] == "flash"


def test_wrapped_true_counts_as_success(monkeypatch):
    f = _flt(monkeypatch)
    with pytest.raises(Exception, match="Switching"):
        _run(f, {"result": True})


def test_status_event_has_exact_text_and_clears_spinner(monkeypatch):
    f = _flt(monkeypatch)
    events = []
    _run(f, None, events=events)
    statuses = [e["data"] for e in events if e["type"] == "status"]
    assert statuses[-1]["done"] is True  # "Switching…" spinner is closed out
    assert statuses[-1]["description"] == EXPECTED.format(n=f._name)


def test_system_message_injected_without_mutating_original(monkeypatch):
    f = _flt(monkeypatch)
    body = _body()
    out = _run(f, None, body=body)
    assert out["messages"][0]["role"] == "system"
    assert EXPECTED.format(n=f._name) in out["messages"][0]["content"]
    assert len(body["messages"]) == 1  # caller's body untouched
    assert out["messages"][-1] == body["messages"][-1]  # user prompt intact


def test_existing_system_prompt_is_extended_not_replaced(monkeypatch):
    f = _flt(monkeypatch)
    body = _body([{"role": "system", "content": "You are terse."}])
    out = _run(f, None, body=body)
    sys_msgs = [m for m in out["messages"] if m["role"] == "system"]
    assert len(sys_msgs) == 1 and sys_msgs[0]["content"].startswith("You are terse.")
    assert EXPECTED.format(n=f._name) in sys_msgs[0]["content"]


def test_fallback_sets_cooldown_so_user_is_not_nagged(monkeypatch):
    f = _flt(monkeypatch)
    _run(f, None, chat="mob")
    calls = []

    async def call(ev):
        calls.append(ev["type"])
        return True
    out = asyncio.run(f.inlet(_body(), __event_call__=call, __metadata__={"chat_id": "mob"}))
    assert calls == [] and out["model"] == "flash"  # no popup within the cooldown


def test_valve_off_restores_old_behaviour(monkeypatch):
    f = _flt(monkeypatch)
    f.valves.mobile_fallback = False
    with pytest.raises(Exception, match="Switching"):
        _run(f, None)


def test_no_event_emitter_still_works(monkeypatch):
    f = _flt(monkeypatch)

    async def call(ev):
        return None if "sessionStorage" in ev["data"].get("code", "") else True
    out = asyncio.run(f.inlet(_body(), __event_call__=call, __metadata__={"chat_id": "c9"}))
    assert out["messages"][0]["role"] == "system"
