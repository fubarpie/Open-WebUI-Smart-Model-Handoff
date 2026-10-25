"""Tests for 0.5.0: popup model card + API key encryption at rest."""
import asyncio
import json
import subprocess
import shutil
import sys
import types

import pytest

import model_handoff
from model_handoff import (ENC_PREFIX, Filter, build_card_js, decrypt_secret, encrypt_secret,
                           is_encrypted)

SECRET = b"unit-test-secret"
TARGET = "anthropic/claude-sonnet-4.5"


# ------------------------------------------------------------------ encryption
def test_roundtrip_and_prefix():
    tok = encrypt_secret("sk-abc123", SECRET)
    assert tok.startswith(ENC_PREFIX) and "sk-abc123" not in tok and is_encrypted(tok)
    assert decrypt_secret(tok, SECRET) == "sk-abc123"


def test_plaintext_passes_through_and_empty():
    assert decrypt_secret("sk-plain", SECRET) == "sk-plain"
    assert decrypt_secret("", b"") == ""  # nothing stored, nothing to decrypt


def test_wrong_secret_raises():
    tok = encrypt_secret("sk-abc123", SECRET)
    with pytest.raises(Exception):
        decrypt_secret(tok, b"a-different-secret")


def test_no_secret_cannot_encrypt_or_decrypt():
    with pytest.raises(RuntimeError):
        encrypt_secret("k", b"")
    with pytest.raises(RuntimeError):
        decrypt_secret(ENC_PREFIX + "x", b"")


def test_each_encryption_is_unique():
    assert encrypt_secret("k", SECRET) != encrypt_secret("k", SECRET)  # random IV


# ------------------------------------------------------------------ filter + keys
def _flt(monkeypatch, api_key, chat_key="", scores=None):
    monkeypatch.setenv("WEBUI_SECRET_KEY", SECRET.decode())
    f = Filter()
    f.valves.api_key = api_key
    f.valves.chat_api_key = chat_key
    f.valves.decision_backend = "chat"
    seen = {}

    async def fake(model, messages, json_mode, timeout):
        seen["key"] = (f._chat_api_key or f._api_key)
        return json.dumps({"scores": scores or {"stay": 0.1, TARGET: 0.95}, "reason": "big task"})
    f._llm = fake
    return f, seen


def _body():
    return {"model": "flash", "messages": [{"role": "user", "content": "refactor my app"}]}


def _drive(f, answer=False, calls=None):
    async def call(ev):
        if calls is not None:
            calls.append(ev)
        return answer

    async def go():
        return await f.inlet(_body(), __event_call__=call, __metadata__={"chat_id": "c1"})
    return asyncio.run(go())


def test_encrypted_valve_is_decrypted_for_requests(monkeypatch):
    f, seen = _flt(monkeypatch, encrypt_secret("sk-real", SECRET))
    f.valves.show_model_card = False
    _drive(f)
    assert seen["key"] == "sk-real"


def test_plaintext_valve_triggers_encrypt_and_save(monkeypatch):
    saved = {}

    class FakeFunctions:
        @staticmethod
        def get_function_by_id(fid):
            return object() if fid == model_handoff.Filter.__module__ else None

        @staticmethod
        async def update_function_valves_by_id(fid, valves):  # async variant must also work
            saved.update(fid=fid, valves=valves)
            return valves

    mod = types.ModuleType("open_webui.models.functions")
    mod.Functions = FakeFunctions
    for name in ("open_webui", "open_webui.models"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    monkeypatch.setitem(sys.modules, "open_webui.models.functions", mod)

    f, seen = _flt(monkeypatch, "sk-plain", "sk-chat")
    f.valves.show_model_card = False
    _drive(f)
    assert seen["key"] == "sk-chat"  # still works on the first call
    stored = saved["valves"]
    assert is_encrypted(stored["api_key"]) and is_encrypted(stored["chat_api_key"])
    assert decrypt_secret(stored["api_key"], SECRET) == "sk-plain"
    assert "sk-plain" not in json.dumps(stored) and "sk-chat" not in json.dumps(stored)
    assert stored["targets_json"] == f.valves.targets_json  # other valves preserved


def test_write_back_failure_keeps_working_and_warns_once(monkeypatch, caplog):
    monkeypatch.delitem(sys.modules, "open_webui", raising=False)  # import will fail
    f, seen = _flt(monkeypatch, "sk-plain")
    f.valves.show_model_card = False
    with caplog.at_level("WARNING", logger="model_handoff"):
        _drive(f)
        _drive(f)
    assert seen["key"] == "sk-plain"
    warns = [r for r in caplog.records if "could not encrypt" in r.getMessage()]
    assert len(warns) == 1 and "sk-plain" not in warns[0].getMessage()


def test_undecryptable_key_skips_cleanly_and_never_logs_secret(monkeypatch, caplog):
    tok = encrypt_secret("sk-real", b"old-secret")
    f, seen = _flt(monkeypatch, tok)
    calls = []
    with caplog.at_level("WARNING", logger="model_handoff"):
        out = _drive(f, calls=calls)
    assert out == _body() and calls == [] and "key" not in seen
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "cannot decrypt" in text and "api_key_undecryptable" in text and "sk-real" not in text


def test_encrypt_valve_off_leaves_plaintext(monkeypatch):
    f, seen = _flt(monkeypatch, "sk-plain")
    f.valves.encrypt_api_keys = False
    f.valves.show_model_card = False

    async def boom():
        raise AssertionError("must not persist")
    asyncio.run(f._persist_encrypted())  # returns immediately, nothing imported
    _drive(f)
    assert seen["key"] == "sk-plain"


# ------------------------------------------------------------------ popup card
def test_card_js_embeds_data_safely():
    nasty = "</script><img src=x onerror=alert(1)> \u2028 \"q\" 'q'"
    js = build_card_js(title=nasty, message=nasty, name=nasty, model_id="m/x", fallback=nasty, footer="f")
    assert ".innerHTML" not in js and "insertAdjacentHTML" not in js and "__DATA__" not in js
    assert "textContent" in js
    assert "javascript:" in js and "ignored" in js  # scheme allow-list is documented in the script


def test_card_accept_and_decline_use_one_execute_call(monkeypatch):
    f, _ = _flt(monkeypatch, encrypt_secret("k", SECRET))
    calls = []
    _drive(f, answer=False, calls=calls)  # card returns False => declined
    assert [c["type"] for c in calls] == ["execute"]
    assert "Switch to" in calls[0]["data"]["code"] and TARGET in calls[0]["data"]["code"]
    # declined => cooldown, asked only once
    calls2 = []
    _drive(f, answer=False, calls=calls2)
    assert calls2 == []


def test_card_accept_then_handoff(monkeypatch):
    f, _ = _flt(monkeypatch, encrypt_secret("k", SECRET))
    types_seen = []

    async def call(ev):
        types_seen.append(ev["type"])
        return True
    with pytest.raises(Exception, match="Switching"):
        asyncio.run(f.inlet(_body(), __event_call__=call, __metadata__={"chat_id": "c1"}))
    assert types_seen == ["execute", "execute"]  # card, then the navigation script


def test_card_falls_back_to_plain_confirmation(monkeypatch):
    f, _ = _flt(monkeypatch, encrypt_secret("k", SECRET))
    types_seen = []

    async def call(ev):
        types_seen.append(ev["type"])
        return None if ev["type"] == "execute" else False  # card gives no usable result
    asyncio.run(f.inlet(_body(), __event_call__=call, __metadata__={"chat_id": "c1"}))
    assert types_seen == ["execute", "confirmation"]


def test_card_error_falls_back_and_wrapped_result_accepted(monkeypatch):
    f, _ = _flt(monkeypatch, encrypt_secret("k", SECRET))
    seq = []

    async def boom(ev):
        seq.append(ev["type"])
        if ev["type"] == "execute":
            raise RuntimeError("frontend exploded")
        return False
    asyncio.run(f.inlet(_body(), __event_call__=boom, __metadata__={"chat_id": "c1"}))
    assert seq == ["execute", "confirmation"]

    f2, _ = _flt(monkeypatch, encrypt_secret("k", SECRET))
    seq2 = []

    async def wrapped(ev):
        seq2.append(ev["type"])
        return {"result": False}
    asyncio.run(f2.inlet(_body(), __event_call__=wrapped, __metadata__={"chat_id": "c2"}))
    assert seq2 == ["execute"]  # accepted without a second popup


def test_card_valve_off_uses_plain_confirmation(monkeypatch):
    f, _ = _flt(monkeypatch, encrypt_secret("k", SECRET))
    f.valves.show_model_card = False
    calls = []
    _drive(f, calls=calls)
    assert [c["type"] for c in calls] == ["confirmation"]
    assert "continue in a new chat" in calls[0]["data"]["message"]


# ------------------------------------------------------------------ browser script (needs node)
@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_card_js_runs_against_a_dom_stub(tmp_path):
    """Executes the real script against a tiny DOM stub: Switch resolves true, Stay/Escape resolve false,
    a hostile description is only ever assigned via textContent, and a javascript: icon is never loaded."""
    js = build_card_js(title="T", message="M", name="Deep Research", model_id="deep-research",
                       fallback="fallback text", footer="F")
    harness = r"""
const CODE = %s;
const mk = (tag) => ({ tag, style: {}, children: [], listeners: {}, attrs: {}, _t: '',
  set textContent(v) { this._t = String(v); }, get textContent() { return this._t; },
  appendChild(c) { this.children.push(c); c.parentNode = this; return c; },
  setAttribute(k, v) { this.attrs[k] = v; }, addEventListener(t, f) { this.listeners[t] = f; },
  remove() { this.removed = true; }, replaceWith(n) { this.replaced = n; }, focus() {} });
const all = [];
const doc = { documentElement: { classList: { contains: () => false } }, activeElement: null,
  body: mk('body'), kl: null,
  createElement(t) { const n = mk(t); all.push(n); return n; },
  addEventListener(t, f) { if (t === 'keydown') doc.kl = f; }, removeEventListener() {} };
const mode = process.argv[2];
const meta = { description: '<img src=x onerror=alert(1)>', profile_image_url: mode === 'badicon' ? 'javascript:alert(1)' : '/static/x.png' };
global.document = doc; global.localStorage = { token: 't' };
global.AbortController = class { constructor() { this.signal = {}; } abort() {} };
global.URL.createObjectURL = () => 'blob:x'; global.URL.revokeObjectURL = () => {};
const fetched = [];
global.fetch = async (u) => { fetched.push(u);
  if (u.startsWith('/api/v1/models/model')) return { ok: true, json: async () => ({ meta }) };
  return { ok: true, blob: async () => ({}) }; };
const run = new Function('return (async () => {' + CODE + '})();');
(async () => {
  const p = run();
  await new Promise((r) => setTimeout(r, 20));
  const overlay = doc.body.children[0];
  const btns = all.filter((n) => n.tag === 'button');
  const descs = all.filter((n) => n._t.includes('<img'));
  if (mode === 'esc') doc.kl({ key: 'Escape', preventDefault() {}, stopPropagation() {} });
  else if (mode === 'stay') btns[0].listeners.click();
  else btns[1].listeners.click();
  const res = await p;
  console.log(JSON.stringify({ res, removed: !!overlay.removed, desc: descs.length, fetched,
    imgs: all.filter((n) => n.tag === 'img').length }));
})();
""" % json.dumps(js)
    path = tmp_path / "h.js"
    path.write_text(harness)

    def run(mode):
        out = subprocess.run(["node", str(path), mode], capture_output=True, text=True, timeout=20)
        assert out.returncode == 0, out.stderr
        return json.loads(out.stdout.strip().splitlines()[-1])
    ok = run("switch")
    assert ok["res"] is True and ok["removed"] and ok["desc"] == 1  # description shown as text
    assert any(u.startswith("/api/v1/models/model?id=deep-research") for u in ok["fetched"])
    assert run("stay")["res"] is False
    assert run("esc")["res"] is False
    bad = run("badicon")
    assert bad["imgs"] == 0 and bad["res"] is True  # javascript: icon never turned into an <img>
