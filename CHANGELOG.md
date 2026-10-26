# Changelog
Convention: every behavior change bumps the version in the `model_handoff.py` header.

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
