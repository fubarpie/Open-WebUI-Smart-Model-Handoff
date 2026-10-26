"""0.9.0: native Conduit handoff (chat:handoff), client detection, fallbacks and server-side cleanup."""
import asyncio
import json
import sys
import types

import pytest

import model_handoff
from conftest import is_probe
from model_handoff import Filter, HandoffSwitch

TARGET = "anthropic/claude-sonnet-4.5"
CONDUIT = {"error": "unsupported", "client": "conduit", "capabilities": ["chat:handoff"]}
NOTICE = "doesn't support automatic chat switching"
REAL_SLEEP = asyncio.sleep


def _flt(monkeypatch, **valves):
    monkeypatch.setenv("WEBUI_SECRET_KEY", "unit-test-secret")
    f = Filter()
    f.valves.api_key = "k"
    f.valves.encrypt_api_keys = False
    f.valves.decision_backend = "chat"
    f.valves.js_ack_timeout_s = 0.2
    for k, val in valves.items():
        setattr(f.valves, k, val)

    async def fake(model, messages, json_mode, timeout):
        return json.dumps({"scores": {"stay": 0.1, TARGET: 0.95}, "reason": "big task"})
    f._llm = fake
    f.scheduled = []
    f._schedule_cleanup = lambda chat_id, user_id: f.scheduled.append((chat_id, user_id))
    return f


def _body(msgs=None):
    return {"model": "flash", "messages": msgs or [{"role": "user", "content": "refactor my app"}]}


class Client:
    """Fake __event_call__: answers the probe and records everything else."""

    def __init__(self, probe=CONDUIT, handoff=None, handoff_exc=None, handoff_sleep=0, probe_exc=None,
                 probe_sleep=0, confirm=False, execute=None):
        self.execute = execute
        self.probe, self.handoff, self.handoff_exc, self.handoff_sleep = probe, handoff, handoff_exc, handoff_sleep
        self.probe_exc, self.probe_sleep, self.confirm = probe_exc, probe_sleep, confirm
        self.events, self.probes = [], 0

    async def __call__(self, ev):
        if is_probe(ev):
            self.probes += 1
            if self.probe_sleep:
                await asyncio.sleep(self.probe_sleep)
            if self.probe_exc:
                raise self.probe_exc
            return self.probe
        self.events.append(ev)
        if ev["type"] == "chat:handoff":
            if self.handoff_sleep:
                await asyncio.sleep(self.handoff_sleep)
            if self.handoff_exc:
                raise self.handoff_exc
            return self.handoff
        if ev["type"] == "confirmation":
            return self.confirm
        return self.execute  # card / handoff js (default: no usable answer)

    def types(self):
        return [e["type"] for e in self.events]


def _inlet(f, call, body=None, chat_id="c1", user="u1", emit=None):
    async def go():
        return await f.inlet(body or _body(), __user__={"id": user} if user else None, __event_call__=call,
                             __event_emitter__=emit, __metadata__={"chat_id": chat_id})
    return asyncio.run(go())


# ------------------------------------------------------------------ 1. happy path
def test_conduit_native_handoff(monkeypatch):
    f = _flt(monkeypatch)
    c = Client(handoff={"handled": True})
    with pytest.raises(HandoffSwitch, match="Switching"):
        _inlet(f, c)
    assert c.probes == 1
    assert c.types() == ["chat:handoff"]  # no confirmation, no card, no handoff js
    data = c.events[0]["data"]
    assert data["model_id"] == TARGET and "refactor my app" in data["prompt"]


def test_conduit_wrapped_handoff_reply_and_probe(monkeypatch):
    f = _flt(monkeypatch)
    c = Client(probe={"result": CONDUIT}, handoff={"result": {"handled": True}})
    with pytest.raises(HandoffSwitch):
        _inlet(f, c)
    assert c.types() == ["chat:handoff"]


# ------------------------------------------------------------------ 2/3/6. never send chat:handoff to non-Conduit
@pytest.mark.parametrize("probe", [
    "browser", True, None, False, "", 0, [],
    {"error": "x", "client": "conduit"},                                   # old Conduit, no capabilities
    {"error": "x", "client": "conduit", "capabilities": []},
    {"error": "x", "client": "conduit", "capabilities": ["other:thing"]},  # capabilities lack chat:handoff
    {"error": "x", "client": "conduit", "capabilities": "chat:handoff"},   # malformed (string)
    {"client": "other", "capabilities": ["chat:handoff"]},
    {"result": {"client": "conduit"}},
    {"result": "browser"},
])
def test_non_conduit_never_gets_chat_handoff(monkeypatch, probe):
    f = _flt(monkeypatch)
    c = Client(probe=probe, confirm=False)
    out = _inlet(f, c)
    assert "chat:handoff" not in c.types()
    assert "confirmation" in c.types() or any(t == "execute" for t in c.types())  # legacy path ran
    assert out["messages"][-1]["content"] == "refactor my app"


def test_browser_accept_uses_legacy_handoff_js(monkeypatch):
    f = _flt(monkeypatch, show_model_card=False)
    c = Client(probe="browser", confirm=True, execute=True)
    with pytest.raises(HandoffSwitch):
        _inlet(f, c)
    assert c.types() == ["confirmation", "execute"]
    assert "sessionStorage.setItem('chat-input'" in c.events[1]["data"]["code"]


def test_valve_off_treats_conduit_as_other(monkeypatch):
    f = _flt(monkeypatch, conduit_native_handoff=False)
    c = Client(handoff={"handled": True})
    _inlet(f, c)
    assert c.probes == 0  # no probe needed when the feature is off
    assert "chat:handoff" not in c.types()


# ------------------------------------------------------------------ 5. failures
def _assert_notice(out, body):
    assert out["messages"][0]["role"] == "system" and NOTICE in out["messages"][0]["content"]
    assert out["messages"][-1] == body["messages"][-1]


@pytest.mark.parametrize("reason", ["model_unavailable", "not_active_chat", "busy", "duplicate", "invalid",
                                    "unsupported", "failed", "weird_new_reason"])
@pytest.mark.parametrize("fallback", [True, False])
def test_failure_reasons(monkeypatch, reason, fallback):
    f = _flt(monkeypatch, mobile_fallback=fallback)
    body = _body()
    c = Client(handoff={"handled": False, "reason": reason})
    out = _inlet(f, c, body)
    assert c.types() == ["chat:handoff"] and not f.scheduled
    if fallback:
        _assert_notice(out, body)
    else:
        assert out == body
    # cooldown: second message in the same chat is not re-asked
    c2 = Client(handoff={"handled": False, "reason": reason})
    two = _body([{"role": "user", "content": "refactor my app"}, {"role": "assistant", "content": "ok"},
                 {"role": "user", "content": "refactor more"}])
    assert _inlet(f, c2, two) == two
    assert c2.events == [] and c2.probes == 0


def test_declined_unchanged_no_notice_and_cooldown(monkeypatch):
    f = _flt(monkeypatch)
    body = _body()
    c = Client(handoff={"handled": False, "reason": "declined"})
    out = _inlet(f, c, body)
    assert out == body and not any(m["role"] == "system" for m in out["messages"])
    assert c.types() == ["chat:handoff"] and not f.scheduled
    c2 = Client(handoff={"handled": True})
    assert _inlet(f, c2, _body()) == body  # cooldown: n_user did not advance
    assert c2.events == []


@pytest.mark.parametrize("kind", ["timeout", "exception", "non_dict", "none", "empty_dict"])
@pytest.mark.parametrize("fallback", [True, False])
def test_transport_failures(monkeypatch, kind, fallback):
    f = _flt(monkeypatch, mobile_fallback=fallback, conduit_handoff_timeout_s=0.1)
    kwargs = {"timeout": dict(handoff_sleep=1, handoff={"handled": True}),
              "exception": dict(handoff_exc=RuntimeError("socket closed")),
              "non_dict": dict(handoff="yes"), "none": dict(handoff=None),
              "empty_dict": dict(handoff={})}[kind]
    body = _body()
    c = Client(**kwargs)
    out = _inlet(f, c, body)
    assert c.types() == ["chat:handoff"] and not f.scheduled
    if fallback:
        _assert_notice(out, body)
    else:
        assert out == body
    assert _inlet(f, Client(**kwargs), body) == body  # cooldown


# ------------------------------------------------------------------ 7/8. detection
def test_client_detection_cached_per_chat(monkeypatch):
    f = _flt(monkeypatch)
    c = Client(handoff={"handled": False, "reason": "declined"})
    _inlet(f, c)
    assert c.probes == 1
    f._declines.clear()  # make the next message eligible again
    _inlet(f, c)
    assert c.probes == 1 and c.types() == ["chat:handoff", "chat:handoff"]
    f._declines.clear()
    _inlet(f, c, chat_id="other-chat")
    assert c.probes == 2  # a different chat is probed separately


def test_browser_detection_also_cached(monkeypatch):
    f = _flt(monkeypatch, show_model_card=False)
    c = Client(probe="browser", confirm=False)
    _inlet(f, c)
    f._declines.clear()
    _inlet(f, c)
    assert c.probes == 1


@pytest.mark.parametrize("kw", [dict(probe_exc=RuntimeError("boom")), dict(probe_sleep=2)])
def test_probe_failure_is_other_and_never_blocks(monkeypatch, kw):
    f = _flt(monkeypatch)
    call = Client(**kw)
    assert asyncio.run(f._detect_client(call, "c1")) == "other"
    out = _inlet(_flt(monkeypatch), Client(**kw))
    assert out["messages"][-1]["content"] == "refactor my app"
    assert "chat:handoff" not in [e["type"] for e in Client(**kw).events]


def test_probe_failure_never_sends_chat_handoff(monkeypatch):
    f = _flt(monkeypatch)
    c = Client(probe_exc=RuntimeError("boom"), handoff={"handled": True})
    _inlet(f, c)
    assert "chat:handoff" not in c.types()


# ------------------------------------------------------------------ 9. attachments
def test_attachments_note_lists_names(monkeypatch):
    f = _flt(monkeypatch)
    c = Client(handoff={"handled": True})
    msgs = [{"role": "user", "content": "refactor my app",
             "files": [{"name": "app.py"}, {"file": {"filename": "notes.txt"}}, {"type": "file"}]}]
    with pytest.raises(HandoffSwitch):
        _inlet(f, c, _body(msgs))
    prompt = c.events[0]["data"]["prompt"]
    assert "attachments" in prompt and "app.py" in prompt and "notes.txt" in prompt
    assert "re-attach" in prompt


def test_no_attachments_note_without_files(monkeypatch):
    f = _flt(monkeypatch)
    c = Client(handoff={"handled": True})
    with pytest.raises(HandoffSwitch):
        _inlet(f, c)
    assert "attachments" not in c.events[0]["data"]["prompt"]


def test_attachments_without_names(monkeypatch):
    f = _flt(monkeypatch)
    c = Client(handoff={"handled": True})
    msgs = [{"role": "user", "content": "refactor my app", "files": [{"type": "file"}]}]
    with pytest.raises(HandoffSwitch):
        _inlet(f, c, _body(msgs))
    assert "had attachments" in c.events[0]["data"]["prompt"]


# ------------------------------------------------------------------ 10. cleanup scheduling
def test_cleanup_scheduled_after_handled(monkeypatch):
    f = _flt(monkeypatch)
    with pytest.raises(HandoffSwitch):
        _inlet(f, Client(handoff={"handled": True}), chat_id="real-chat", user="u7")
    assert f.scheduled == [("real-chat", "u7")]


@pytest.mark.parametrize("case", ["valve_off", "history", "multi_user", "local_chat", "no_user", "empty_chat_id"])
def test_cleanup_guards(monkeypatch, case):
    f = _flt(monkeypatch, delete_old_chat=(case != "valve_off"))
    body, chat_id, user = _body(), "real-chat", "u1"
    if case == "history":
        body = _body([{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"},
                      {"role": "user", "content": "refactor my app"}])
    elif case == "multi_user":
        body = _body([{"role": "user", "content": "hi"}, {"role": "user", "content": "refactor my app"}])
    elif case == "local_chat":
        chat_id = "local:abc"
    elif case == "no_user":
        user = None
    elif case == "empty_chat_id":
        chat_id = ""
    with pytest.raises(HandoffSwitch):
        _inlet(f, Client(handoff={"handled": True}), body, chat_id=chat_id, user=user)
    assert f.scheduled == []


def test_cleanup_not_scheduled_on_decline_or_failure(monkeypatch):
    for reply in ({"handled": False, "reason": "declined"}, {"handled": False, "reason": "failed"}, "junk"):
        f = _flt(monkeypatch)
        _inlet(f, Client(handoff=reply))
        assert f.scheduled == []


def test_real_schedule_runs_two_passes_in_loop(monkeypatch):
    """Use the real _schedule_cleanup inside the loop and drain the task: no 'Task was destroyed' leak."""
    f = _flt(monkeypatch)
    del f._schedule_cleanup  # restore the class method
    f.valves.delete_delay_s = 0
    sleeps, deletes = [], []

    async def fast_sleep(s, *a, **k):
        sleeps.append(s)
        await REAL_SLEEP(0)

    async def fake_delete(chat_id, user_id):
        deletes.append((chat_id, user_id))
        return True
    monkeypatch.setattr(model_handoff.asyncio, "sleep", fast_sleep)
    f._delete_chat_backend = fake_delete

    async def go():
        with pytest.raises(HandoffSwitch):
            await f.inlet(_body(), __user__={"id": "u1"}, __event_call__=Client(handoff={"handled": True}),
                          __metadata__={"chat_id": "c1"})
        assert f._tasks
        await asyncio.gather(*list(f._tasks))
        await REAL_SLEEP(0)
        assert not f._tasks
    asyncio.run(go())
    assert deletes == [("c1", "u1"), ("c1", "u1")] and sleeps == [0.0, 7.0]


# ------------------------------------------------------------------ 10b. backend deletion
def _msgs(*pairs):
    return {f"m{i}": {"role": r, "content": c} for i, (r, c) in enumerate(pairs)}


def _chat(msgs=None, as_object=False, **extra):
    msgs = msgs if msgs is not None else _msgs(("user", "hi"), ("assistant", ""))
    d = {"id": "c1", "chat": {"history": {"messages": msgs}}, "pinned": False, "archived": False,
         "folder_id": None, "share_id": None}
    d.update(extra)
    return types.SimpleNamespace(**d) if as_object else d


def _install_chats(monkeypatch, chat, is_async=False, get_exc=None, del_exc=None):
    calls = {"get": [], "delete": []}

    def get(cid, uid):
        calls["get"].append((cid, uid))
        if get_exc:
            raise get_exc
        return chat

    def delete(cid, uid):
        calls["delete"].append((cid, uid))
        if del_exc:
            raise del_exc
        return True

    class Chats:
        pass
    if is_async:
        async def aget(cid, uid): return get(cid, uid)
        async def adel(cid, uid): return delete(cid, uid)
        Chats.get_chat_by_id_and_user_id = staticmethod(aget)
        Chats.delete_chat_by_id_and_user_id = staticmethod(adel)
    else:
        Chats.get_chat_by_id_and_user_id = staticmethod(get)
        Chats.delete_chat_by_id_and_user_id = staticmethod(delete)
    mod = types.ModuleType("open_webui.models.chats")
    mod.Chats = Chats
    for name in ("open_webui", "open_webui.models"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    monkeypatch.setitem(sys.modules, "open_webui.models.chats", mod)
    return calls


def _delete(f, chat_id="c1", user="u1"):
    return asyncio.run(f._delete_chat_backend(chat_id, user))


@pytest.mark.parametrize("is_async", [False, True])
@pytest.mark.parametrize("as_object", [False, True])
def test_delete_disposable_chat(monkeypatch, is_async, as_object):
    calls = _install_chats(monkeypatch, _chat(as_object=as_object), is_async)
    assert _delete(_flt(monkeypatch)) is True
    assert calls["get"] == [("c1", "u1")] and calls["delete"] == [("c1", "u1")]


@pytest.mark.parametrize("is_async", [False, True])
@pytest.mark.parametrize("extra", [
    {"pinned": True}, {"archived": True}, {"folder_id": "f1"}, {"share_id": "s1"},
])
def test_delete_skips_protected_chats(monkeypatch, is_async, extra):
    calls = _install_chats(monkeypatch, _chat(**extra), is_async)
    assert _delete(_flt(monkeypatch)) is False
    assert calls["delete"] == []


def test_delete_skips_protected_object_chat(monkeypatch):
    calls = _install_chats(monkeypatch, _chat(as_object=True, pinned=True))
    assert _delete(_flt(monkeypatch)) is False and calls["delete"] == []


@pytest.mark.parametrize("msgs", [
    _msgs(("user", "a"), ("user", "b")),
    _msgs(("user", "a"), ("assistant", "a real reply")),
    _msgs(("user", "a"), ("assistant", "   x ")),
])
def test_delete_skips_used_chats(monkeypatch, msgs):
    calls = _install_chats(monkeypatch, _chat(msgs))
    assert _delete(_flt(monkeypatch)) is False and calls["delete"] == []


@pytest.mark.parametrize("content", ["", "   ", None])
def test_delete_tolerates_empty_assistant_placeholder(monkeypatch, content):
    calls = _install_chats(monkeypatch, _chat(_msgs(("user", "a"), ("assistant", content))))
    assert _delete(_flt(monkeypatch)) is True and calls["delete"] == [("c1", "u1")]


def test_delete_chat_list_style_messages(monkeypatch):
    chat = {"chat": {"messages": [{"role": "user", "content": "a"}, {"role": "assistant", "content": "r"}]}}
    calls = _install_chats(monkeypatch, chat)
    assert _delete(_flt(monkeypatch)) is False and calls["delete"] == []


@pytest.mark.parametrize("chat_id,user", [("local:xyz", "u1"), ("", "u1"), ("c1", ""), ("c1", None)])
def test_delete_skips_unsafe_ids(monkeypatch, chat_id, user):
    calls = _install_chats(monkeypatch, _chat())
    assert _delete(_flt(monkeypatch), chat_id, user) is False
    assert calls["get"] == [] and calls["delete"] == []


def test_delete_chat_not_found(monkeypatch):
    calls = _install_chats(monkeypatch, None)
    assert _delete(_flt(monkeypatch)) is False and calls["delete"] == []


def test_delete_never_raises_when_import_fails(monkeypatch):
    monkeypatch.delitem(sys.modules, "open_webui", raising=False)
    monkeypatch.delitem(sys.modules, "open_webui.models", raising=False)
    monkeypatch.delitem(sys.modules, "open_webui.models.chats", raising=False)
    assert _delete(_flt(monkeypatch)) is False


@pytest.mark.parametrize("is_async", [False, True])
@pytest.mark.parametrize("where", ["get", "delete"])
def test_delete_never_raises_on_backend_errors(monkeypatch, is_async, where):
    kw = {"get_exc": RuntimeError("db down")} if where == "get" else {"del_exc": RuntimeError("db down")}
    _install_chats(monkeypatch, _chat(), is_async, **kw)
    assert _delete(_flt(monkeypatch)) is False


def test_delete_garbage_chat_object_never_raises(monkeypatch):
    _install_chats(monkeypatch, {"chat": "not-a-dict"})
    assert _delete(_flt(monkeypatch)) is False


def test_cleanup_old_chat_two_passes(monkeypatch):
    f = _flt(monkeypatch, delete_delay_s=2.5)
    sleeps = []

    async def fast_sleep(s, *a, **k):
        sleeps.append(s)

    calls = _install_chats(monkeypatch, _chat())
    monkeypatch.setattr(model_handoff.asyncio, "sleep", fast_sleep)
    asyncio.run(f._cleanup_old_chat("c1", "u1"))
    assert sleeps == [2.5, 7.0]
    assert calls["delete"] == [("c1", "u1")] * 2 and calls["get"] == [("c1", "u1")] * 2


def test_cleanup_old_chat_never_raises(monkeypatch):
    f = _flt(monkeypatch, delete_delay_s=0)

    async def fast_sleep(s, *a, **k):
        return None

    async def boom(*a):
        raise RuntimeError("x")
    f._delete_chat_backend = boom
    monkeypatch.setattr(model_handoff.asyncio, "sleep", fast_sleep)
    asyncio.run(f._cleanup_old_chat("c1", "u1"))  # must not raise


# ------------------------------------------------------------------ misc: emitter errors
def test_failing_emitter_does_not_break_conduit_flow(monkeypatch):
    f = _flt(monkeypatch)

    async def emit(ev):
        raise RuntimeError("emitter down")
    with pytest.raises(HandoffSwitch):
        _inlet(f, Client(handoff={"handled": True}), emit=emit)
