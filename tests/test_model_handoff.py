import asyncio
import json

from model_handoff import (Filter, build_handoff_prompt, build_js, classify,
                           parse_scores, text_of, validate_targets)


def test_parse_scores():
    s, r = parse_scores('{"scores":{"stay":0.1,"a":0.9,"zzz":1},"reason":"x"}', {"a"})
    assert s == {"stay": 0.1, "a": 0.9} and r == "x"  # unknown id dropped


def test_parse_clamps_and_defaults_reason():
    s, r = parse_scores('{"scores":{"stay":-1,"a":2}}', {"a"})
    assert s == {"stay": 0.0, "a": 1.0} and r == ""


def test_classify_tiers():
    assert classify({"stay": 0.1, "a": 0.9}, 0.8, 0.55, 0.25) == ("ask", "a")
    assert classify({"stay": 0.1, "a": 0.6}, 0.8, 0.55, 0.25) == ("hint", "a")
    assert classify({"stay": 0.65, "a": 0.85}, 0.8, 0.55, 0.25) == ("stay", None)
    assert classify({"stay": 0.1, "a": 0.3}, 0.8, 0.55, 0.25) == ("stay", None)
    assert classify({}, 0.8, 0.55, 0.25) == ("stay", None)


def test_validate_targets():
    ok, err = validate_targets('[{"id":"a"},{"id":"a"},{"id":"stay"},{"name":"x"}]')
    assert [t["id"] for t in ok] == ["a"] and len(err) == 3
    assert validate_targets("{bad")[0] == []
    assert validate_targets('{"id":"a"}')[1]


def test_prompt_cases():
    a = build_handoff_prompt(first_turn=True, latest="hi", reason="r", summary="", from_name="F", back_hint="")
    assert a == "hi"
    b = build_handoff_prompt(first_turn=False, latest="do it", reason="big code", summary="S", from_name="F", back_hint="")
    assert "big code" in b and "S" in b and b.endswith("do it")


def test_text_of():
    assert text_of({"content": [{"type": "text", "text": "a"}, {"type": "image_url"}]}) == "a"


def test_js_is_safe_and_valid_shape():
    js = build_js("m/x", "it's \"quoted\"\n</script>", [{"id": "f1"}], True)
    assert "m%2Fx" not in js and "encodeURIComponent(\"m/x\")" in js
    assert "sessionStorage.setItem('chat-input'" in js and "send-message-button" in js


def _run(flt, body, confirm):
    calls = []

    async def call(ev):
        calls.append(ev["type"])
        return confirm if ev["type"] == "confirmation" else True

    async def go():
        return await flt.inlet(body, __event_call__=call, __metadata__={"chat_id": "c1"})
    return calls, go


def _flt(scores):
    f = Filter()
    f.valves.api_key = "k"
    f.valves.decision_backend = "chat"
    f.valves.show_model_card = False  # these tests cover the plain confirmation path

    async def fake(model, messages, json_mode, timeout):
        return json.dumps({"scores": scores, "reason": "big task"})
    f._llm = fake
    return f


def test_decline_leaves_body_and_cools_down():
    f = _flt({"stay": 0.1, "anthropic/claude-sonnet-4.5": 0.95})
    body = {"model": "flash", "messages": [{"role": "user", "content": "refactor my app"}]}
    calls, go = _run(f, json.loads(json.dumps(body)), False)
    assert asyncio.run(go()) == body and calls == ["confirmation"]
    calls2, go2 = _run(f, json.loads(json.dumps(body)), False)
    asyncio.run(go2())
    assert calls2 == []  # cooldown: not asked again


def test_accept_executes_and_aborts():
    f = _flt({"stay": 0.1, "anthropic/claude-sonnet-4.5": 0.95})
    body = {"model": "flash", "messages": [{"role": "user", "content": "refactor my app"}]}
    calls, go = _run(f, body, True)
    try:
        asyncio.run(go())
        assert False, "expected abort"
    except Exception as e:
        assert "Switching" in str(e)
    assert calls == ["confirmation", "execute"]


def test_no_loop_on_target_model():
    f = _flt({"stay": 0.0, "anthropic/claude-sonnet-4.5": 1})
    body = {"model": "anthropic/claude-sonnet-4.5", "messages": [{"role": "user", "content": "x"}]}
    calls, go = _run(f, body, True)
    asyncio.run(go())
    assert calls == []


def test_build_decision_request_uses_safe_keys():
    from model_handoff import build_decision_request
    targets = [{"id": "anthropic/claude-sonnet-4.5", "name": "Coder", "use_when": "code", "avoid_when": "chat", "hint": ""}]
    body, keymap = build_decision_request("typesafe/jev-1.14", convo="user: hi", has_files=False,
                                          current_desc="General.", targets=targets)
    q = body["questions"]["route"]
    assert q["type"] == "choice" and set(q["criteria"]) == {"stay", "t0"}
    assert keymap == {"stay": "stay", "t0": "anthropic/claude-sonnet-4.5"}
    assert "Use when: code" in q["criteria"]["t0"]


def test_parse_decision_answer_maps_back_and_ignores_unknown():
    from model_handoff import parse_decision_answer
    data = {"answers": {"route": {"type": "choice", "choice": "t0", "confidence": 0.7,
                                  "probabilities": {"stay": 0.1, "t0": 0.9, "zzz": 0.5}}}}
    out = parse_decision_answer(data, {"stay": "stay", "t0": "a/b"})
    assert out == {"stay": 0.1, "a/b": 0.9}


def test_decisions_backend_end_to_end(monkeypatch):
    """Decisions API path: posts to decision_url, maps t0 back to the real model id, popup falls back to static text."""
    import model_handoff
    seen = {}

    class FakeResp:
        def raise_for_status(self): pass
        def json(self):
            return {"answers": {"route": {"type": "choice", "choice": "t0", "confidence": 0.9,
                                          "probabilities": {"stay": 0.05, "t0": 0.95, "t1": 0.0}}}}

    class FakeClient:
        def __init__(self, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def post(self, url, headers=None, json=None):
            seen.update(url=url, headers=headers, body=json)
            return FakeResp()

    monkeypatch.setattr(model_handoff.httpx, "AsyncClient", FakeClient)
    f = Filter()
    f.valves.api_key = "k"  # default backend = decisions
    f.valves.show_model_card = False
    body = {"model": "flash", "messages": [{"role": "user", "content": "refactor my app"}]}
    events = []

    async def call(ev):
        events.append(ev)
        return False  # decline, so we only assert on the popup

    asyncio.run(f.inlet(body, __event_call__=call, __metadata__={"chat_id": "c9"}))
    assert seen["url"] == "https://openrouter.ai/api/alpha/decisions"
    assert seen["headers"]["Authorization"] == "Bearer k"
    assert seen["body"]["model"] == "typesafe/jev-1.14"
    assert events and events[0]["type"] == "confirmation"
    assert "Deep Coding & Analysis" in events[0]["data"]["message"]


def test_js_cleanup_only_when_requested():
    assert "/api/v1/chats/" not in build_js("m", "p", [], True) or 'const oldChat = ""' in build_js("m", "p", [], True)
    js = build_js("m", "p", [], True, "old-1")
    assert 'const oldChat = "old-1"' in js and "method: 'DELETE'" in js


def _accepted_js(msgs, **valves):
    f = _flt({"stay": 0.1, "anthropic/claude-sonnet-4.5": 0.95})
    for k, val in valves.items():
        setattr(f.valves, k, val)
    codes = []

    async def call(ev):
        if ev["type"] == "execute":
            codes.append(ev["data"]["code"])
        return True

    async def go():
        try:
            await f.inlet({"model": "flash", "messages": msgs}, __event_call__=call, __metadata__={"chat_id": "c1"})
        except Exception:
            pass
    asyncio.run(go())
    return codes[0]


def test_cleanup_single_user_message():
    assert 'const oldChat = "c1"' in _accepted_js([{"role": "user", "content": "refactor my app"}])


def test_no_cleanup_with_history_or_valve_off():
    hist = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"},
            {"role": "user", "content": "refactor my app"}]
    assert 'const oldChat = ""' in _accepted_js(hist)
    assert 'const oldChat = ""' in _accepted_js([{"role": "user", "content": "x"}], delete_old_chat=False)
    assert 'const oldChat = ""' in _accepted_js([{"role": "user", "content": "x"}], auto_send=False)
