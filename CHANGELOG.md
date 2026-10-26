# Changelog
Convention: every behavior change bumps the version in the `model_handoff.py` header.

## 0.9.0
- **Conduit app support.** The filter now detects the [Conduit](https://github.com/cogwheel0/conduit) mobile app (PR #890, `chat:handoff`) and hands off natively: Conduit shows its own switch dialog and opens the new chat on the specialist with the prompt ready, **unsent**, in the composer. No popup of our own is shown on Conduit (`conduit_native_handoff`, `conduit_handoff_timeout_s`).
- **Client detection.** Before offering a switch, one cheap `execute` probe tells Conduit (replies with `client: conduit` and `chat:handoff` in `capabilities`) from a browser. `chat:handoff` is only ever sent to a positively detected Conduit, because a browser would wait forever. Older Conduit builds and other apps keep the 0.7.0 text-notice fallback. The result is cached per chat.
- **Server-side cleanup of the old chat.** After Conduit confirms the switch, a one-message chat is deleted straight from Open WebUI's database (no client involved): guarded (single user message, no real assistant reply, not pinned/archived/in a folder/shared, scoped to the user), delayed (`delete_delay_s`, then a second pass) and fully exception-safe (`delete_old_chat`; `auto_send` does not apply).
- Conduit failure reasons (`declined`, `model_unavailable`, `not_active_chat`, `busy`, `duplicate`, `invalid`, `unsupported`, `failed`, timeout) all degrade to staying in the current chat; a decline is silent and starts the normal cooldown.
- Attachments are not carried over by Conduit; the handoff prompt lists them so the user can re-attach.
- **Never-block guarantee.** `inlet` now catches any unexpected error in the handoff logic and delivers the user's message to the current model unchanged; only the deliberate switch signal escapes. A failing confirmation popup counts as "stay" instead of an error.
- Browser behaviour is unchanged, apart from one extra fast probe event before the popup.
## 0.8.0
- **Fix: API key encryption never saved back.** Open WebUI loads functions as modules named `function_<id>_<random hex>`, so the old `__module__`-as-id lookup never matched and the key stayed in plaintext (logged as "could not encrypt"). The filter now uses the `__id__` that Open WebUI passes to `inlet`, with a fallback that parses the real module-name pattern.
- Tests now use a fake `Functions` that only accepts the real function id, so this class of bug is caught.
## 0.7.0
- Mobile fallback: when the client cannot run the handoff script (e.g. the Conduit app), show a text notice, let the current model answer, and do not raise a generation error (`mobile_fallback`, `js_ack_timeout_s`).
## 0.6.0
- Hide the old chat's sidebar entry after a successful delete, with retries over ~10s.
- Check the DELETE status; only hide on 200/404, log otherwise.
## 0.5.0
- Model card popup (icon, name, description) with fallback to plain popup (`show_model_card`).
- Fernet encryption of API keys at rest (`encrypt_api_keys`).
## 0.4.0
- Delete the origin chat after handoff when it held a single user message (`delete_old_chat`).
