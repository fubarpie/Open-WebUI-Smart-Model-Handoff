"""
title: Model Handoff
author: Keenan Ventling
version: 0.9.0
license: MIT
description: Attach to a fast general-purpose model. When a request fits a specialist model better, asks the user (popup) and, if they agree, continues the conversation in a new chat on that model. If the original chat held only a single user message, it is deleted after the handoff and its sidebar entry is hidden immediately, with retries (valve: delete_old_chat). The confirmation popup shows a card with the specialist model's icon and description (valve: show_model_card). API keys are encrypted at rest (Fernet, key derived from WEBUI_SECRET_KEY; valve: encrypt_api_keys); 0.8.0 fixes the save-back so the stored valve really becomes enc:v1:... (the function id now comes from Open WebUI's __id__). The Conduit mobile app is supported natively through its chat:handoff event (valve: conduit_native_handoff): Conduit shows its own switch dialog and opens the new chat with the prompt ready in the composer, and the old one-message chat is deleted from the server side. On other clients that cannot run the browser script (e.g. Open Relay) the handoff degrades gracefully: the user gets a text notice to start a new chat on the specialist and the current chat keeps working (valves: mobile_fallback, js_ack_timeout_s). Admin-configurable target list. Developed against Open WebUI v0.12.3.
requirements: httpx, cryptography
"""

import asyncio
import base64
import hashlib
import inspect
import json
import logging
import os
import re
import time
from typing import Any, Awaitable, Callable, Optional

import httpx
from pydantic import BaseModel, Field

log = logging.getLogger("model_handoff")

EXAMPLE_TARGETS = [
    {
        "id": "anthropic/claude-sonnet-4.5",
        "name": "Deep Coding & Analysis",
        "use_when": "Writing or debugging substantial code, complex multi-step reasoning, careful analysis of long documents.",
        "avoid_when": "Casual chat, quick lookups, short rewrites, simple questions.",
        "hint": "Slower, but more thorough.",
    },
    {
        "id": "google/gemini-2.5-pro",
        "name": "Vision & Very Long Documents",
        "use_when": "Detailed analysis of images, PDFs or inputs that are very long.",
        "avoid_when": "Plain text questions that fit easily in a normal chat.",
        "hint": "Best for pictures and big files.",
    },
]

DECISION_SYSTEM = """You route chat requests to the best AI model.
Output ONLY JSON of the form:
{"scores": {"stay": <0-1>, "<model_id>": <0-1>, ...}, "reason": "<one short, friendly sentence a non-technical user will understand>"}
Each score is your confidence (0 to 1) that the option is the best choice for the user's LATEST request.
"stay" = the current general-purpose model is good enough. Give EVERY candidate a score.
Be conservative: score a specialist high only if the request clearly matches its use_when and does not match its avoid_when.
Never mention model ids in "reason"; use the friendly names."""


# ---------------------------------------------------------------- pure helpers
def validate_targets(raw: str) -> tuple[list[dict], list[str]]:
    """Parse the admin's JSON. Returns (valid_targets, human-readable errors)."""
    errors: list[str] = []
    try:
        data = json.loads(raw or "[]")
    except json.JSONDecodeError as e:
        return [], [f"targets_json is not valid JSON: {e}"]
    if not isinstance(data, list):
        return [], ["targets_json must be a JSON list"]
    out, seen = [], set()
    for i, t in enumerate(data):
        if not isinstance(t, dict) or not str(t.get("id", "")).strip():
            errors.append(f"target #{i + 1}: missing 'id'")
            continue
        tid = str(t["id"]).strip()
        if tid == "stay" or tid in seen:
            errors.append(f"target #{i + 1}: id '{tid}' is reserved or duplicated")
            continue
        seen.add(tid)
        out.append({
            "id": tid,
            "name": str(t.get("name") or tid),
            "use_when": str(t.get("use_when", "")),
            "avoid_when": str(t.get("avoid_when", "")),
            "hint": str(t.get("hint", "")),
        })
    return out, errors


def parse_scores(text: str, valid_ids: set[str]) -> tuple[dict[str, float], str]:
    """Read ({id: score}, reason) from the decision model's JSON reply.

    The decision model returns structured JSON only, so this parses it directly.
    Malformed output raises, and the caller treats that as 'stay on current model'.
    Unknown ids are dropped and scores are clamped to 0..1.
    """
    data = json.loads(text)
    scores = {
        k: max(0.0, min(1.0, float(v)))
        for k, v in data["scores"].items()
        if k == "stay" or k in valid_ids
    }
    return scores, str(data.get("reason", ""))[:300]


def build_decision_request(model: str, *, convo: str, has_files: bool, current_desc: str,
                           targets: list[dict]) -> tuple[dict, dict[str, str]]:
    """Body for POST /decisions: ONE choice question over 'stay' + each specialist.

    Option keys are neutral ('stay', 't0', 't1'...) because model ids contain '/' and '.';
    the returned map translates them back to real ids.
    """
    keymap = {"stay": "stay"}
    criteria = {"stay": f"{current_desc} Choose this unless a specialist clearly fits better."}
    for i, t in enumerate(targets):
        key = f"t{i}"
        keymap[key] = t["id"]
        criteria[key] = f"{t['name']}. Use when: {t['use_when']} Avoid when: {t['avoid_when']}"
    body = {
        "model": model,
        "state": {"conversation": convo, "latest_message_has_attachments": has_files},
        "questions": {"route": {
            "type": "choice",
            "instructions": "Which assistant is best for the user's LATEST message in this conversation?",
            "criteria": criteria,
        }},
    }
    return body, keymap


def parse_decision_answer(data: dict, keymap: dict[str, str]) -> dict[str, float]:
    """Decisions API reply -> {model id or 'stay': probability}."""
    probs = data["answers"]["route"]["probabilities"]
    return {keymap[k]: max(0.0, min(1.0, float(v))) for k, v in probs.items() if k in keymap}


def best_target(scores: dict[str, float]) -> tuple[Optional[str], float, float]:
    """(best specialist id, its score, stay score)."""
    stay = scores.get("stay", 0.0)
    cands = {k: v for k, v in scores.items() if k != "stay"}
    if not cands:
        return None, 0.0, stay
    best = max(cands, key=cands.get)
    return best, cands[best], stay


def classify(scores: dict[str, float], hard: float, soft: float, margin: float) -> tuple[str, Optional[str]]:
    """Return ('ask'|'hint'|'stay', target_id)."""
    best, score, stay = best_target(scores)
    if best is None or score - stay < margin:
        return "stay", None
    if score >= hard:
        return "ask", best
    if score >= soft:
        return "hint", best
    return "stay", None


def text_of(msg: dict) -> str:
    c = msg.get("content", "")
    if isinstance(c, list):
        return " ".join(p.get("text", "") for p in c if isinstance(p, dict) and p.get("type") == "text")
    return c or ""


def build_handoff_prompt(*, first_turn: bool, latest: str, reason: str, summary: str,
                         from_name: str, back_hint: str) -> str:
    """Case A: verbatim first input. Case B: briefing + compaction + latest request."""
    if first_turn:
        return latest
    return (
        f"[Handoff briefing] This conversation was transferred to you from {from_name}, "
        f"a general-purpose assistant, because: {reason or 'your specialty fits this request better'}.\n"
        "Continue naturally; do not mention this briefing unless asked.\n\n"
        f"Summary of the conversation so far:\n{summary}\n\n"
        f"{back_hint}"
        f"The user's latest request (please answer this now):\n{latest}"
    )


def build_js(model_id: str, prompt: str, files: list, auto_send: bool, delete_chat_id: str = "") -> str:
    """Browser-side script run via the 'execute' event. Client-side navigation (no reload):
    1. write the new-chat draft (prompt + files) into sessionStorage ('chat-input', restored by Chat.svelte)
    2. click a synthetic link to /?models=<id> (SvelteKit handles it as a router navigation)
    3. wait for the draft to appear in the editor, then click the send button.
    4. optional: once the new chat exists (URL is /c/<other id>), delete the old chat via the REST API.
       Only runs after a successful send, so the user's message is never lost."""
    draft = {
        "prompt": prompt, "files": files,
        "selectedToolIds": [], "selectedFilterIds": [],
        "webSearchEnabled": False, "imageGenerationEnabled": False, "codeInterpreterEnabled": False,
    }
    return f"""
sessionStorage.setItem('chat-input', {json.dumps(json.dumps(draft))});
const a = document.createElement('a');
a.href = '/?models=' + encodeURIComponent({json.dumps(model_id)});
document.body.appendChild(a); a.click(); a.remove();
(async () => {{
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  if (!{json.dumps(auto_send)}) return;
  const hasFiles = {json.dumps(bool(files))};
  const oldChat = {json.dumps(delete_chat_id)};
  // The sidebar list is not refetched after a DELETE made behind its back, so hide the stale entry
  // ourselves. display:none (not removal) keeps Svelte's own DOM bookkeeping intact. Retried because
  // the list can re-render and bring the entry back; fails harmlessly if the markup changes.
  const hideEntry = (id) => {{
    let hidden = 0;
    document.querySelectorAll('a[href="/c/' + id + '"]').forEach((link) => {{
      let node = link;
      for (let up = 0; up < 3; up++) {{
        const parent = node.parentElement;
        if (!parent || parent.tagName === 'NAV' || parent.querySelectorAll('a[href^="/c/"]').length !== 1) break;
        node = parent;
      }}
      if (node.style.display !== 'none') {{ node.style.display = 'none'; }}
      hidden++;
    }});
    return hidden;
  }};
  const dropFromSidebar = async (id) => {{
    let seen = 0;
    for (const wait of [0, 500, 1000, 1500, 2500, 4000]) {{
      await sleep(wait);
      try {{ seen += hideEntry(id); }} catch (e) {{ console.warn('handoff: sidebar refresh failed', e); }}
    }}
    if (!seen) console.info('handoff: old chat entry not found in sidebar (already gone or markup changed)');
  }};
  const cleanup = async () => {{
    if (!oldChat) return;
    for (let j = 0; j < 100; j++) {{
      await sleep(200);
      const m = location.pathname.match(/^\\/c\\/([^/]+)/);
      if (m && m[1] !== oldChat) {{
        await sleep(1500);  // let the aborted request on the old chat settle first
        let deleted = false;
        try {{
          const res = await fetch('/api/v1/chats/' + encodeURIComponent(oldChat), {{
            method: 'DELETE', headers: {{ Authorization: 'Bearer ' + localStorage.token }} }});
          deleted = res.ok || res.status === 404;
          if (!deleted) console.warn('handoff: old chat delete returned', res.status);
        }} catch (e) {{ console.warn('handoff: old chat cleanup failed', e); }}
        if (deleted) await dropFromSidebar(oldChat);
        return;
      }}
    }}
  }};
  for (let i = 0; i < 80; i++) {{
    await sleep(150);
    const el = document.getElementById('chat-input');
    const btn = document.getElementById('send-message-button');
    if (location.pathname === '/' && el && btn && !btn.disabled && (el.innerText || '').trim().length > 0) {{
      if (hasFiles) await sleep(800);
      btn.click();
      await cleanup();
      return;
    }}
  }}
}})();
return true;
"""


# ---------------------------------------------------------------- secrets at rest
ENC_PREFIX = "enc:v1:"


def _secret_bytes() -> bytes:
    """Server-side secret that never lives in the database (so valve exports / DB dumps don't contain it)."""
    return (os.environ.get("WEBUI_SECRET_KEY") or os.environ.get("WEBUI_JWT_SECRET_KEY") or "").encode()


def _fernet(secret: bytes):
    from cryptography.fernet import Fernet  # imported lazily so a missing package degrades instead of crashing
    # Domain-separated so this key is not reusable for anything else derived from the same secret.
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(b"model_handoff:v1:" + secret).digest()))


def is_encrypted(value: str) -> bool:
    return isinstance(value, str) and value.startswith(ENC_PREFIX)


def encrypt_secret(plain: str, secret: bytes) -> str:
    """'enc:v1:<fernet token>'. Raises if no server secret or cryptography is unavailable."""
    if not secret:
        raise RuntimeError("WEBUI_SECRET_KEY is not set; cannot encrypt")
    return ENC_PREFIX + _fernet(secret).encrypt(plain.encode()).decode()


def decrypt_secret(value: str, secret: bytes) -> str:
    """Plaintext values pass through unchanged. Raises on a wrong secret or corrupted token."""
    if not is_encrypted(value):
        return value
    if not secret:
        raise RuntimeError("WEBUI_SECRET_KEY is not set; cannot decrypt")
    return _fernet(secret).decrypt(value[len(ENC_PREFIX):].encode()).decode()


# ---------------------------------------------------------------- popup card
CARD_JS = r"""
const D = __DATA__;
const dark = document.documentElement.classList.contains('dark');
const C = dark
  ? { bg: '#1b1b1d', fg: '#ececec', sub: '#a1a1aa', card: '#26272b', line: '#3f3f46', btn: '#2f3035', pri: '#ececec', priFg: '#111111' }
  : { bg: '#ffffff', fg: '#111827', sub: '#6b7280', card: '#f3f4f6', line: '#e5e7eb', btn: '#f3f4f6', pri: '#111827', priFg: '#ffffff' };
return await new Promise((resolve) => {
  const prevFocus = document.activeElement;
  let done = false;
  let blobUrl = null;
  // textContent only (never innerHTML): model descriptions are admin/user text and must not inject markup.
  const el = (tag, css, text) => {
    const n = document.createElement(tag);
    if (css) n.style.cssText = css;
    if (text != null) n.textContent = text;
    return n;
  };
  const overlay = el('div', 'position:fixed;inset:0;z-index:99999;display:flex;align-items:center;justify-content:center;background:rgba(0,0,0,.5);padding:16px;');
  const dlg = el('div', 'background:' + C.bg + ';color:' + C.fg + ';border:1px solid ' + C.line + ';border-radius:16px;max-width:440px;width:100%;padding:20px;box-shadow:0 20px 50px rgba(0,0,0,.35);font-family:inherit;');
  dlg.setAttribute('role', 'dialog');
  dlg.setAttribute('aria-modal', 'true');
  dlg.setAttribute('aria-label', D.title);

  dlg.appendChild(el('div', 'font-size:17px;font-weight:600;margin-bottom:8px;', D.title));
  dlg.appendChild(el('div', 'font-size:14px;color:' + C.sub + ';white-space:pre-wrap;margin-bottom:14px;line-height:1.45;', D.message));

  // ---- model card
  const card = el('div', 'display:flex;gap:12px;align-items:center;background:' + C.card + ';border:1px solid ' + C.line + ';border-radius:12px;padding:12px;margin-bottom:14px;');
  const avatar = el('div', 'flex:none;width:44px;height:44px;border-radius:10px;display:flex;align-items:center;justify-content:center;font-weight:700;font-size:18px;background:' + C.pri + ';color:' + C.priFg + ';', (D.name || '?').trim().charAt(0).toUpperCase());
  const info = el('div', 'min-width:0;');
  info.appendChild(el('div', 'font-size:14px;font-weight:600;', D.name));
  const descEl = el('div', 'font-size:13px;color:' + C.sub + ';line-height:1.4;margin-top:2px;', D.fallback);
  info.appendChild(descEl);
  card.appendChild(avatar);
  card.appendChild(info);
  dlg.appendChild(card);

  dlg.appendChild(el('div', 'font-size:12px;color:' + C.sub + ';margin-bottom:16px;', D.footer));

  // ---- buttons
  const row = el('div', 'display:flex;justify-content:flex-end;gap:8px;');
  const btnCss = 'border-radius:10px;padding:8px 16px;font-size:14px;cursor:pointer;border:1px solid ' + C.line + ';';
  const cancel = el('button', btnCss + 'background:' + C.btn + ';color:' + C.fg + ';', D.cancelLabel);
  const ok = el('button', btnCss + 'background:' + C.pri + ';color:' + C.priFg + ';font-weight:600;', D.confirmLabel);
  cancel.type = 'button';
  ok.type = 'button';
  row.appendChild(cancel);
  row.appendChild(ok);
  dlg.appendChild(row);
  overlay.appendChild(dlg);

  const onKey = (e) => {
    if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); finish(false); }
  };
  function finish(v) {
    if (done) return;
    done = true;
    document.removeEventListener('keydown', onKey, true);
    overlay.remove();
    if (blobUrl) URL.revokeObjectURL(blobUrl);
    try { if (prevFocus && prevFocus.focus) prevFocus.focus(); } catch (e) {}
    resolve(v);
  }
  cancel.addEventListener('click', () => finish(false));
  ok.addEventListener('click', () => finish(true));
  document.addEventListener('keydown', onKey, true);
  document.body.appendChild(overlay);
  try { ok.focus(); } catch (e) {}

  // ---- enrich the card after it is already on screen (never delays the popup)
  const token = localStorage.token;
  const headers = { Authorization: 'Bearer ' + token };
  const timed = (url) => {
    const ac = new AbortController();
    setTimeout(() => ac.abort(), 5000);
    return fetch(url, { headers, signal: ac.signal });
  };
  const setImg = (src) => {
    const img = document.createElement('img');
    img.style.cssText = 'flex:none;width:44px;height:44px;border-radius:10px;object-fit:cover;';
    img.alt = '';
    img.onload = () => { if (!done && avatar.parentNode) avatar.replaceWith(img); };
    img.src = src;  // on error nothing happens: the letter avatar stays
  };
  (async () => {
    try {
      let meta = null;
      let r = await timed('/api/v1/models/model?id=' + encodeURIComponent(D.modelId));
      if (r.ok) {
        meta = (await r.json() || {}).meta || null;
      } else {
        // Not a workspace model entry: look it up in the full model list instead.
        r = await timed('/api/models');
        if (r.ok) {
          const list = (await r.json()).data || [];
          const m = list.find((x) => x.id === D.modelId);
          meta = (m && m.info && m.info.meta) || null;
        }
      }
      if (!meta || done) return;
      if (typeof meta.description === 'string' && meta.description.trim()) {
        descEl.textContent = meta.description.trim().slice(0, 220);
      }
      const src = meta.profile_image_url;
      if (typeof src !== 'string' || !src) return;
      if (src.startsWith('data:image/')) {
        setImg(src);
      } else if (src.startsWith('/') && !src.startsWith('//')) {
        const ir = await timed(src);  // same-origin; sent with the token in case the image route needs auth
        if (ir.ok && !done) { blobUrl = URL.createObjectURL(await ir.blob()); setImg(blobUrl); }
      } else if (/^https?:\/\//.test(src)) {
        setImg(src);
      }  // any other scheme (javascript:, file:, ...) is ignored
    } catch (e) { /* keep the fallback card */ }
  })();
});
"""


def build_card_js(*, title: str, message: str, name: str, model_id: str, fallback: str,
                  footer: str, confirm_label: str = "Switch", cancel_label: str = "Stay here") -> str:
    """Browser script (run via the 'execute' event) that shows the handoff popup with a model card
    and resolves to true (switch) / false (stay)."""
    data = {"title": title, "message": message, "name": name, "modelId": model_id, "fallback": fallback,
            "footer": footer, "confirmLabel": confirm_label, "cancelLabel": cancel_label}
    return CARD_JS.replace("__DATA__", json.dumps(data))


class HandoffSwitch(Exception):
    """Deliberate signal that the browser is moving to the new chat: raised on purpose to stop the
    current model from answering. It is the ONLY exception inlet() lets escape."""


class Filter:
    class Valves(BaseModel):
        priority: int = Field(0, description="Filter priority (lower runs first).")
        enabled: bool = Field(True, description="Master switch.")
        decision_backend: str = Field(
            "decisions",
            description="'decisions' = structured Decisions API (e.g. Jev on OpenRouter; returns probabilities only, no text). "
                        "'chat' = any OpenAI-compatible chat model that is asked to reply with JSON.")
        decision_url: str = Field("https://openrouter.ai/api/alpha/decisions",
                                  description="Decisions API endpoint (used when decision_backend = 'decisions').")
        decision_model: str = Field(
            "typesafe/jev-1.14",
            description="Model that picks the best assistant. A decision model for 'decisions', a chat model id for 'chat'.")
        chat_base_url: str = Field(
            "https://openrouter.ai/api/v1",
            description="OpenAI-compatible base URL. Used for the conversation summary, and for decisions when decision_backend = 'chat'.")
        chat_json_mode: bool = Field(True, description="'chat' backend only: send response_format=json_object. Turn off if your endpoint rejects it.")
        api_key: str = Field("", description="API key (used for both endpoints unless chat_api_key is set).",
                             json_schema_extra={"input": {"type": "password"}})
        chat_api_key: str = Field("", description="Optional separate key for chat_base_url. Blank = api_key.",
                                  json_schema_extra={"input": {"type": "password"}})
        compaction_model: str = Field("google/gemini-2.5-flash",
                                      description="CHAT model that summarises long chats before a handoff (decision models cannot write text).")
        targets_json: str = Field(
            json.dumps(EXAMPLE_TARGETS, indent=2),
            description='JSON list of handoff targets. Each: {"id": "<model id exactly as in Open WebUI>", '
                        '"name": "<friendly name shown to users>", "use_when": "...", "avoid_when": "...", "hint": "<optional>"}',
        )
        current_model_description: str = Field(
            "A fast, general-purpose assistant for everyday questions, writing, and light tasks.",
            description="Tells the decision model what 'stay' means.")
        confidence_threshold: float = Field(0.80, description="Score needed to show the popup.")
        soft_threshold: float = Field(0.55, description="Score needed to show a gentle, non-blocking suggestion. Set above confidence_threshold to disable.")
        stay_margin: float = Field(0.25, description="Specialist must beat 'stay' by at least this much.")
        context_messages: int = Field(6, description="Recent messages sent to the decision model.")
        decline_cooldown: int = Field(5, description="User messages to wait before re-asking after a decline.")
        decision_timeout_s: float = Field(12.0, description="Fail open (stay) if the decision model is slower than this.")
        compaction_timeout_s: float = Field(45.0, description="Timeout for the summary step.")
        auto_send: bool = Field(True, description="Automatically send the handed-off message in the new chat.")
        offer_way_back: bool = Field(True, description="Tell the specialist how the user can return to the quick assistant.")
        delete_old_chat: bool = Field(True, description="After a handoff, delete the old chat if it contained only ONE user message (nothing worth keeping). In the browser this needs auto_send on. For the Conduit app the chat is deleted from the server side after Conduit confirms the switch (auto_send does not apply).")
        show_model_card: bool = Field(True, description="Show the specialist's icon and description in a card on the popup. Falls back to the plain popup if the card cannot be shown.")
        mobile_fallback: bool = Field(True, description="If the client cannot run the handoff script (native mobile apps, no browser DOM), do not abort the reply: show a text notice telling the user to start a new chat with the specialist, and answer in the current chat. Turn off to restore the old behaviour (raise an error to stop the reply).")
        js_ack_timeout_s: float = Field(10.0, description="How long to wait for the client to confirm it ran the handoff script before assuming it cannot (non-web client).")
        conduit_native_handoff: bool = Field(True, description="Use the Conduit app's native chat:handoff event (needs a Conduit build with PR #890). Conduit shows its own switch dialog and opens the new chat with the prompt UNSENT in the composer (auto_send does not apply). Turn off to treat Conduit like any other non-browser client.")
        conduit_handoff_timeout_s: float = Field(120.0, description="How long to wait for the Conduit user to answer the native switch dialog.")
        delete_delay_s: float = Field(3.0, description="Conduit only: seconds to wait after a confirmed handoff before deleting the old chat on the server (a second pass runs about 7s later). The delay lets the aborted request finish saving first.")
        encrypt_api_keys: bool = Field(True, description="Encrypt api_key / chat_api_key at rest (Fernet, key derived from WEBUI_SECRET_KEY). A plaintext key is encrypted and saved back automatically on first use.")
        log_decisions: bool = Field(True, description="Log scores and outcomes to the server log for tuning.")

    def __init__(self):
        self.valves = self.Valves()
        self._declines: dict[str, tuple[int, float]] = {}  # chat_id -> (user msg count, timestamp)
        self._api_key = ""       # resolved (decrypted) keys for the current request; never logged
        self._chat_api_key = ""
        self._persist_warned = False
        self._fid: Optional[str] = None  # real function id, set from __id__ in inlet
        self._clients: dict[str, tuple[str, float]] = {}  # chat_id -> (client kind, timestamp)
        self._tasks: set[asyncio.Task] = set()  # strong refs so background cleanups are not garbage collected

    # ---------------------------------------------------------------- internals
    def _log(self, **kw):
        if self.valves.log_decisions:
            log.info("handoff %s", json.dumps(kw, default=str))

    def _remember_decline(self, chat_id: str, n_user: int):
        now = time.time()
        if len(self._declines) > 2000:  # prune old entries (>24h)
            self._declines = {k: v for k, v in self._declines.items() if now - v[1] < 86400}
        self._declines[chat_id] = (n_user, now)

    def _resolve_keys(self) -> Optional[str]:
        """Decrypt the valve keys into memory. Returns an error string, or None on success."""
        secret = _secret_bytes()
        try:
            self._api_key = decrypt_secret(self.valves.api_key, secret)
            self._chat_api_key = decrypt_secret(self.valves.chat_api_key, secret)
        except Exception as e:  # wrong/rotated WEBUI_SECRET_KEY, corrupted value, missing cryptography
            self._api_key = self._chat_api_key = ""
            return f"{type(e).__name__}: {e}"
        return None

    def _function_id(self) -> str:
        """The Open WebUI function id used to save valves.

        Open WebUI passes it to inlet as __id__. As a fallback, parse it from the module name, which Open WebUI
        sets to 'function_<id>_<32 hex chars>' (it is NOT the bare id, which was the 0.5.0-0.7.0 bug).
        """
        if self._fid:
            return self._fid
        m = re.fullmatch(r"function_(.+)_[0-9a-f]{32}", type(self).__module__ or "")
        return m.group(1) if m else ""

    async def _persist_encrypted(self) -> None:
        """Best effort: encrypt plaintext keys and save the ciphertext back over the valves.

        Uses Open WebUI's internal Functions model, which is not a public API, so every step is guarded.
        On any failure the filter keeps working with the plaintext key and logs one warning.
        """
        v = self.valves
        if not v.encrypt_api_keys or not (v.api_key or v.chat_api_key):
            return
        if not any(k and not is_encrypted(k) for k in (v.api_key, v.chat_api_key)):
            return  # already encrypted
        try:
            secret = _secret_bytes()
            new = v.model_dump()
            for field in ("api_key", "chat_api_key"):
                if new[field] and not is_encrypted(new[field]):
                    new[field] = encrypt_secret(new[field], secret)
            from open_webui.models.functions import Functions  # internal API
            fid = self._function_id()
            if not fid:
                raise RuntimeError(f"cannot determine the function id (module '{type(self).__module__}')")
            found = Functions.get_function_by_id(fid)
            if inspect.isawaitable(found):
                found = await found
            if not found:
                raise RuntimeError(f"function id '{fid}' not found")
            res = Functions.update_function_valves_by_id(fid, new)
            if inspect.isawaitable(res):
                res = await res
            if not res:
                raise RuntimeError("update_function_valves_by_id returned nothing")
            self._log(outcome="api_keys_encrypted")
        except Exception as e:
            if not self._persist_warned:
                self._persist_warned = True
                log.warning("handoff could not encrypt the stored API key (%s: %s). The key still works but is "
                            "stored in plaintext. Set WEBUI_SECRET_KEY, or enter the key again.", type(e).__name__, e)

    async def _llm(self, model: str, messages: list[dict], json_mode: bool, timeout: float) -> str:
        payload: dict[str, Any] = {"model": model, "messages": messages, "temperature": 0}
        if json_mode and self.valves.chat_json_mode:
            payload["response_format"] = {"type": "json_object"}
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.post(
                f"{self.valves.chat_base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {self._chat_api_key or self._api_key}"},
                json=payload,
            )
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]

    async def _decide(self, msgs: list[dict], targets: list[dict]):
        """Returns (scores, reason). The 'decisions' backend has no prose, so its reason is ''."""
        v = self.valves
        convo = "\n".join(f"{m.get('role')}: {text_of(m)[:1500]}" for m in msgs[-v.context_messages:])
        has_files = bool(msgs[-1].get("files"))
        if v.decision_backend == "decisions":
            body, keymap = build_decision_request(
                v.decision_model, convo=convo, has_files=has_files,
                current_desc=v.current_model_description, targets=targets)
            async with httpx.AsyncClient(timeout=v.decision_timeout_s) as c:
                r = await c.post(v.decision_url, headers={"Authorization": f"Bearer {self._api_key}"}, json=body)
                r.raise_for_status()
                return parse_decision_answer(r.json(), keymap), ""
        cands = [{"id": "stay", "name": "Current model", "description": v.current_model_description}] + targets
        user = (f"Candidates:\n{json.dumps(cands, indent=1)}\n\n"
                f"Latest message has attachments: {has_files}\n\nConversation:\n{convo}")
        out = await self._llm(
            v.decision_model,
            [{"role": "system", "content": DECISION_SYSTEM}, {"role": "user", "content": user}],
            True, v.decision_timeout_s,
        )
        return parse_scores(out, {t["id"] for t in targets})

    async def _compact(self, msgs: list[dict]) -> str:
        transcript = "\n".join(f"{m.get('role')}: {text_of(m)[:3000]}" for m in msgs[:-1])
        prompt = ("Compact this conversation for a different AI model that is taking over. Preserve: "
                  "the user's goals, decisions made, key facts, names, numbers, code and identifiers, "
                  "and open questions. Be dense and neutral. Output only the summary.\n\n" + transcript[-24000:])
        return await self._llm(self.valves.compaction_model or self.valves.decision_model,
                               [{"role": "user", "content": prompt}], False, self.valves.compaction_timeout_s)

    async def _ask(self, call: Callable[[dict], Awaitable[Any]], target: dict, reason: str) -> bool:
        """Show the card popup; fall back to the plain confirmation if the card cannot be shown."""
        footer = "Your conversation will continue in a new chat. Choose Cancel to stay here."
        lead = reason or "This looks like a good fit for " + target["name"] + "."
        if self.valves.show_model_card:
            try:
                code = build_card_js(
                    title=f"Switch to {target['name']}?", message=lead, name=target["name"], model_id=target["id"],
                    fallback=target["use_when"] or target["hint"] or "A specialist model for this kind of request.",
                    footer="Your conversation will continue in a new chat.",
                    confirm_label="Switch", cancel_label="Stay here")
                res = await call({"type": "execute", "data": {"code": code}})
                if isinstance(res, dict):  # tolerate a wrapped result
                    res = res.get("result", res.get("value", res))
                if isinstance(res, bool):
                    return res
                self._log(outcome="card_no_result", got=repr(res)[:80])
            except Exception as e:
                self._log(outcome="card_error", error=str(e))
        hint = f"\n\n{target['hint']}" if target["hint"] else ""
        try:
            return bool(await call({"type": "confirmation", "data": {
                "title": f"Switch to {target['name']}?", "message": f"{lead}{hint}\n\n{footer}"}}))
        except Exception as e:  # cannot ask => do not switch
            self._log(outcome="confirm_error", error=str(e))
            return False

    async def _run_handoff_js(self, call: Callable[[dict], Awaitable[Any]], code: str) -> bool:
        """Run the navigation script; True only if the client confirmed it ran (the script ends with
        `return true`). Timeout, exception or any other result (None, False) means a client without a
        browser DOM, e.g. a native mobile app."""
        try:
            res = await asyncio.wait_for(call({"type": "execute", "data": {"code": code}}),
                                         timeout=self.valves.js_ack_timeout_s)
        except asyncio.TimeoutError:
            self._log(outcome="js_no_ack", reason="timeout")
            return False
        except Exception as e:
            self._log(outcome="js_no_ack", reason="error", error=str(e))
            return False
        if isinstance(res, dict):  # tolerate a wrapped result
            res = res.get("result", res.get("value", res))
        if res is True:
            return True
        self._log(outcome="js_no_ack", reason="result", got=repr(res)[:80])
        return False

    # ---------------------------------------------------------------- Conduit (native mobile app)
    @staticmethod
    def _unwrap(res: Any, key: str) -> Any:
        """Tolerate a client that wraps its reply as {'result': ...} / {'value': ...}."""
        if isinstance(res, dict) and key not in res:
            return res.get("result", res.get("value", res))
        return res

    @staticmethod
    def _is_conduit(res: Any) -> bool:
        """True only for Conduit's documented reply to an `execute` event: it carries client == 'conduit'
        and advertises the chat:handoff capability. Anything else (a browser's result, an older Conduit
        without the capability) must NOT be sent chat:handoff, because a browser never answers it."""
        res = Filter._unwrap(res, "client")
        if not isinstance(res, dict) or res.get("client") != "conduit":
            return False
        caps = res.get("capabilities")
        return isinstance(caps, (list, tuple, set)) and "chat:handoff" in caps

    async def _detect_client(self, call: Callable[[dict], Awaitable[Any]], chat_id: str) -> str:
        """Returns 'conduit' or 'other'. Probes with a trivial `execute`: a browser runs it and returns,
        Conduit answers at once with its client/capabilities dict. Cached per chat. Never raises."""
        now = time.time()
        cached = self._clients.get(chat_id) if chat_id else None
        if cached and now - cached[1] < 3600:
            return cached[0]
        kind = "other"
        try:
            res = await asyncio.wait_for(call({"type": "execute", "data": {"code": "return 'browser';"}}),
                                         timeout=self.valves.js_ack_timeout_s)
            if self._is_conduit(res):
                kind = "conduit"
        except asyncio.TimeoutError:
            self._log(chat=chat_id, outcome="client_probe", result="timeout")
        except Exception as e:
            self._log(chat=chat_id, outcome="client_probe", result="error", error=str(e))
        if chat_id:
            if len(self._clients) > 2000:
                self._clients = {k: x for k, x in self._clients.items() if now - x[1] < 3600}
            self._clients[chat_id] = (kind, now)
        self._log(chat=chat_id, outcome="client_probe", client=kind)
        return kind

    async def _conduit_handoff(self, call: Callable[[dict], Awaitable[Any]], model_id: str, prompt: str) -> tuple[bool, str]:
        """Send Conduit's native chat:handoff. Returns (handled, reason). Never raises.
        Only call after _detect_client returned 'conduit'."""
        try:
            res = await asyncio.wait_for(
                call({"type": "chat:handoff", "data": {"model_id": model_id, "prompt": prompt}}),
                timeout=self.valves.conduit_handoff_timeout_s)
        except asyncio.TimeoutError:
            return False, "timeout"
        except Exception as e:
            return False, f"error: {type(e).__name__}: {e}"
        res = self._unwrap(res, "handled")
        if isinstance(res, dict):
            if res.get("handled") is True:
                return True, ""
            return False, str(res.get("reason") or "unknown")
        return False, f"bad_reply: {repr(res)[:80]}"

    @staticmethod
    def _file_names(files: list) -> list[str]:
        names = []
        for f in files or []:
            if not isinstance(f, dict):
                continue
            inner = f.get("file") if isinstance(f.get("file"), dict) else {}
            name = f.get("name") or inner.get("filename") or (inner.get("meta") or {}).get("name")
            if name:
                names.append(str(name))
        return names

    @staticmethod
    def _chat_is_disposable(chat: Any) -> tuple[bool, str]:
        """Final check on the STORED chat right before deleting: a single user message, no real
        assistant reply, and not pinned / archived / in a folder / shared. Returns (ok, why_not)."""
        def attr(name: str) -> Any:
            return chat.get(name) if isinstance(chat, dict) else getattr(chat, name, None)
        if attr("pinned"):
            return False, "pinned"
        if attr("archived"):
            return False, "archived"
        if attr("folder_id"):
            return False, "in_folder"
        if attr("share_id"):
            return False, "shared"
        data = attr("chat") or {}
        msgs = (data.get("history") or {}).get("messages")
        if isinstance(msgs, dict):
            items = list(msgs.values())
        else:
            items = data.get("messages") or []
        n_user = sum(1 for m in items if isinstance(m, dict) and m.get("role") == "user")
        # an aborted request may leave an EMPTY assistant placeholder; only real replies count
        n_reply = sum(1 for m in items if isinstance(m, dict) and m.get("role") == "assistant"
                      and str(m.get("content") or "").strip())
        if n_user > 1:
            return False, "multiple_user_messages"
        if n_reply:
            return False, "has_assistant_reply"
        return True, ""

    async def _delete_chat_backend(self, chat_id: str, user_id: str) -> bool:
        """Delete the old chat straight from Open WebUI's database (the filter runs server-side, so the
        client is not involved). Guarded and fully exception-safe. Returns True if a delete was issued."""
        try:
            if not chat_id or chat_id.startswith("local:") or not user_id:
                self._log(chat=chat_id, outcome="cleanup_skipped", why="no_real_chat_or_user")
                return False
            from open_webui.models.chats import Chats  # internal API, not stable: every step is guarded

            async def maybe_await(x):
                return await x if inspect.isawaitable(x) else x

            chat = await maybe_await(Chats.get_chat_by_id_and_user_id(chat_id, user_id))
            if not chat:
                self._log(chat=chat_id, outcome="cleanup_skipped", why="not_found")
                return False
            ok, why = self._chat_is_disposable(chat)
            if not ok:
                self._log(chat=chat_id, outcome="cleanup_skipped", why=why)
                return False
            res = await maybe_await(Chats.delete_chat_by_id_and_user_id(chat_id, user_id))
            self._log(chat=chat_id, outcome="cleanup_deleted", result=bool(res))
            return True
        except Exception as e:
            log.warning("handoff could not delete the old chat %s (%s: %s)", chat_id, type(e).__name__, e)
            return False

    async def _cleanup_old_chat(self, chat_id: str, user_id: str) -> None:
        """Two passes: the aborted request may re-save the chat as it unwinds, so delete after a short
        delay and once more a few seconds later (the second pass is a no-op when the chat is gone)."""
        try:
            delay = max(0.0, float(self.valves.delete_delay_s))
            await asyncio.sleep(delay)
            await self._delete_chat_backend(chat_id, user_id)
            await asyncio.sleep(7.0)
            await self._delete_chat_backend(chat_id, user_id)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("handoff cleanup task failed (%s: %s)", type(e).__name__, e)

    def _schedule_cleanup(self, chat_id: str, user_id: str) -> None:
        task = asyncio.create_task(self._cleanup_old_chat(chat_id, user_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _build_prompt(self, msgs: list[dict], target: dict, reason: str, model: Optional[dict], n_user: int) -> str:
        first_turn = n_user <= 1
        summary = ""
        if not first_turn:
            try:
                summary = await self._compact(msgs)
            except Exception:
                summary = "\n".join(f"{m.get('role')}: {text_of(m)[:500]}" for m in msgs[-7:-1])
        back = ("(If the user wants quick everyday help again, suggest they start a new chat with the "
                "general assistant.)\n\n") if self.valves.offer_way_back else ""
        return build_handoff_prompt(
            first_turn=first_turn, latest=text_of(msgs[-1]), reason=reason or target["use_when"], summary=summary,
            from_name=(model or {}).get("name") or "a general assistant", back_hint=back)

    async def _conduit_flow(self, body: dict, *, msgs: list[dict], target: dict, reason: str, chat_id: str,
                            n_user: int, files: list, user: Optional[dict], model: Optional[dict], call, emitter) -> dict:
        """Handoff on the Conduit app. Conduit asks the user itself, so we show no popup of our own.
        Returns the body to continue with, or raises HandoffSwitch once Conduit confirms the switch."""
        v = self.valves
        await self._emit(emitter, f"Preparing the switch to {target['name']}…", False)
        prompt = await self._build_prompt(msgs, target, reason, model, n_user)
        names = self._file_names(files) if files else []
        if files:  # Conduit does not carry attachments over
            listed = f": {', '.join(names)}" if names else ""
            prompt += f"\n\n(The original message had attachments{listed}. They were not carried over; please re-attach them.)"
        handled, why = await self._conduit_handoff(call, target["id"], prompt)
        if handled:
            self._log(chat=chat_id, outcome="conduit_handled", choice=target["id"])
            n_assistant = sum(1 for m in msgs if m.get("role") == "assistant")
            user_id = (user or {}).get("id") or ""
            if (v.delete_old_chat and n_user == 1 and n_assistant == 0 and chat_id
                    and not chat_id.startswith("local:") and user_id):
                self._schedule_cleanup(chat_id, user_id)
            await self._emit(emitter, f"Switching to {target['name']}…", True)
            raise HandoffSwitch(f"Switching to {target['name']}…")
        self._remember_decline(chat_id, n_user)  # declined or failed: do not re-ask every message
        if why == "declined":
            self._log(chat=chat_id, outcome="conduit_declined", choice=target["id"])
            await self._emit(emitter, "Staying in this chat.", True)
            return body
        if why == "model_unavailable":
            log.error("handoff: Conduit says model '%s' is not available to this user. Check that the target id "
                      "in targets_json is an exact Open WebUI model id the user can access.", target["id"])
        self._log(chat=chat_id, outcome="conduit_failed", reason=why, choice=target["id"])
        if not v.mobile_fallback:
            await self._emit(emitter, "Staying in this chat.", True)
            return body
        text = self._fallback_text(target["name"])
        await self._emit(emitter, text, True)
        return self._with_notice(body, text)

    async def _emit(self, emitter, description: str, done: bool) -> None:
        """Status events are cosmetic: a failing emitter must never affect the reply."""
        if not emitter:
            return
        try:
            await emitter({"type": "status", "data": {"description": description, "done": done}})
        except Exception as e:
            self._log(outcome="emit_error", error=str(e))

    @staticmethod
    def _fallback_text(name: str) -> str:
        return (f"This looks like a good fit for {name}, but your current app doesn't support automatic "
                f"chat switching. Please copy your prompt and start a new chat with the {name} model.")

    @staticmethod
    def _with_notice(body: dict, text: str) -> dict:
        """Copy of body whose system prompt asks the model to open its reply with the notice, so the user
        sees it in any client (the status event alone may not be rendered everywhere)."""
        note = ("Begin your reply with exactly this notice on its own paragraph, word for word, then "
                f"answer the user's request normally:\n\"{text}\"")
        msgs = list(body.get("messages", []))
        if msgs and msgs[0].get("role") == "system":
            first = dict(msgs[0])
            first["content"] = f"{text_of(first)}\n\n{note}"
            msgs[0] = first
        else:
            msgs.insert(0, {"role": "system", "content": note})
        return {**body, "messages": msgs}

    # ---------------------------------------------------------------- hook
    async def inlet(
        self,
        body: dict,
        __user__: Optional[dict] = None,
        __event_call__: Optional[Callable[[dict], Awaitable[Any]]] = None,
        __event_emitter__: Optional[Callable[[dict], Awaitable[Any]]] = None,
        __metadata__: Optional[dict] = None,
        __task__: Optional[str] = None,
        __model__: Optional[dict] = None,
        __id__: Optional[str] = None,
    ) -> dict:
        """Never-block guarantee: whatever goes wrong inside the handoff logic, the user's message is
        delivered to the current model unchanged. Only the deliberate HandoffSwitch escapes."""
        try:
            return await self._inlet(body, __user__, __event_call__, __event_emitter__,
                                     __metadata__, __task__, __model__, __id__)
        except HandoffSwitch:
            raise
        except Exception as e:
            log.error("handoff failed, staying on the current model: %s: %s", type(e).__name__, e)
            return body

    async def _inlet(
        self,
        body: dict,
        __user__: Optional[dict] = None,
        __event_call__: Optional[Callable[[dict], Awaitable[Any]]] = None,
        __event_emitter__: Optional[Callable[[dict], Awaitable[Any]]] = None,
        __metadata__: Optional[dict] = None,
        __task__: Optional[str] = None,
        __model__: Optional[dict] = None,
        __id__: Optional[str] = None,
    ) -> dict:
        if __id__:
            self._fid = __id__
        v = self.valves
        meta = __metadata__ or {}
        msgs = body.get("messages", [])
        targets, errors = validate_targets(v.targets_json)
        if errors:
            log.warning("handoff config errors: %s", errors)
        key_error = self._resolve_keys() if v.enabled else None
        if key_error:
            log.error("handoff cannot decrypt the stored API key (%s). Re-enter it in the valves "
                      "(WEBUI_SECRET_KEY may have changed).", key_error)
        skip = next((why for why, bad in (
            ("disabled", not v.enabled),
            ("task_request", bool(__task__)),
            ("no_messages", not msgs),
            ("no_valid_targets", not targets),
            ("no___event_call__", not __event_call__),
            ("api_key_undecryptable", bool(key_error)),
            ("no_api_key", not self._api_key),
            ("last_msg_not_user", bool(msgs) and msgs[-1].get("role") != "user"),
            ("already_on_target", body.get("model") in {t["id"] for t in targets}),
        ) if bad), None)
        if skip:
            # Always logged (even with log_decisions off) so a silent filter is diagnosable.
            log.warning("handoff skipped: %s (model=%s)", skip, body.get("model"))
            return body

        await self._persist_encrypted()  # no-op once the stored key is already encrypted

        chat_id = meta.get("chat_id", "")
        n_user = sum(1 for m in msgs if m.get("role") == "user")
        prev = self._declines.get(chat_id)
        if prev and n_user - prev[0] < v.decline_cooldown:
            return body

        # 1. Score candidates; any failure => stay (fail open).
        try:
            scores, reason = await self._decide(msgs, targets)
        except Exception as e:
            self._log(chat=chat_id, outcome="decision_error", error=str(e))
            return body
        action, choice = classify(scores, v.confidence_threshold, v.soft_threshold, v.stay_margin)
        self._log(chat=chat_id, scores=scores, action=action, choice=choice)
        if action == "stay" or not choice:
            return body
        target = next(t for t in targets if t["id"] == choice)

        if action == "hint":  # gentle, non-blocking
            await self._emit(__event_emitter__,
                             f"Tip: for something like this, start a new chat with “{target['name']}”.", True)
            return body

        # 1b. Conduit app: native chat:handoff (Conduit asks the user itself). Detected, never assumed:
        # a browser would wait forever on this event.
        if v.conduit_native_handoff and await self._detect_client(__event_call__, chat_id) == "conduit":
            c_files = msgs[-1].get("files") or meta.get("files") or body.get("files") or []
            return await self._conduit_flow(
                body, msgs=msgs, target=target, reason=reason, chat_id=chat_id, n_user=n_user, files=c_files,
                user=__user__, model=__model__, call=__event_call__, emitter=__event_emitter__)

        # 2. Ask permission.
        ok = await self._ask(__event_call__, target, reason)
        if not ok:
            self._remember_decline(chat_id, n_user)
            self._log(chat=chat_id, outcome="declined", choice=choice)
            return body  # untouched => answered as if nothing happened

        # 3. Build the payload.
        last = msgs[-1]
        files = last.get("files") or meta.get("files") or body.get("files") or []
        await self._emit(__event_emitter__, f"Switching to {target['name']}…", False)
        first_turn = n_user <= 1
        summary = ""
        if not first_turn:
            try:
                summary = await self._compact(msgs)
            except Exception:
                summary = "\n".join(f"{m.get('role')}: {text_of(m)[:500]}" for m in msgs[-7:-1])
        back = ("(If the user wants quick everyday help again, suggest they start a new chat with the "
                "general assistant.)\n\n") if v.offer_way_back else ""
        prompt = build_handoff_prompt(
            first_turn=first_turn, latest=text_of(last), reason=reason or target["use_when"], summary=summary,
            from_name=(__model__ or {}).get("name") or "a general assistant", back_hint=back)

        # 4. Move the browser to the new chat.
        self._log(chat=chat_id, outcome="accepted", choice=choice, first_turn=first_turn)
        # Only clean up a real, saved single-message chat (not temporary 'local:' chats) and only if the
        # new chat will actually be sent automatically; otherwise the user's message would be lost.
        n_assistant = sum(1 for m in msgs if m.get("role") == "assistant")
        drop = (chat_id if (v.delete_old_chat and v.auto_send and n_user == 1 and n_assistant == 0
                            and chat_id and not chat_id.startswith("local:")) else "")
        self._log(chat=chat_id, cleanup=bool(drop))
        navigated = await self._run_handoff_js(__event_call__, build_js(choice, prompt, files, v.auto_send, drop))

        if navigated or not v.mobile_fallback:
            if not navigated:
                self._log(chat=chat_id, outcome="js_not_confirmed_raising")
            # 5. Stop the flash model from also answering in the old chat.
            raise HandoffSwitch(f"Switching to {target['name']}…")

        # 5b. The client could not run the script (e.g. a native mobile app). Do not strand the user
        # behind an error: tell them what to do, and let the current model answer so the chat still works.
        text = self._fallback_text(target["name"])
        self._log(chat=chat_id, outcome="mobile_fallback", choice=choice)
        self._remember_decline(chat_id, n_user)  # don't re-offer (and re-wait for the timeout) every message
        await self._emit(__event_emitter__, text, True)
        return self._with_notice(body, text)
