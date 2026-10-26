# Open WebUI Smart Model Handoff

An [Open WebUI](https://github.com/open-webui/open-webui) **Filter** that lets admins keep users on a fast, cheap default model and offers a one-click switch to a specialist model when a request needs one.

**Status:** pre-1.0 (currently `0.9.0`). See [ROADMAP.md](ROADMAP.md) and [CHANGELOG.md](CHANGELOG.md).

## How it works
1. Attach the filter to your default model.
2. A small router/decision model compares the user's message to your list of specialist targets.
3. If one fits better, a popup card (specialist icon, name, description) asks: **Switch** or **Stay here**.
4. On Switch, the conversation continues in a new chat on the specialist model. If the original chat held only that one message, it is deleted and hidden from the sidebar.

## Features
- Admin-configurable target list (`targets_json` valve): `id`, `name`, `use_when`, `avoid_when`, `hint`. See [examples/targets.example.json](examples/targets.example.json). IDs must exactly match real Open WebUI model IDs.
- Model card popup with dark-mode support, Escape = stay, and a plain-popup fallback (`show_model_card`).
- Old single-message chat cleanup with immediate sidebar hiding and retries (`delete_old_chat`).
- Router API keys encrypted at rest with Fernet, keyed from `WEBUI_SECRET_KEY` (`encrypt_api_keys`).
- **Conduit app support (native).** On [Conduit](https://github.com/cogwheel0/conduit) the filter uses Conduit's `chat:handoff` event: Conduit shows its own switch dialog and opens the new chat with the prompt ready in the composer (not auto-sent), and the old one-message chat is deleted from the server side. Needs a Conduit build that includes [PR #890](https://github.com/cogwheel0/conduit/pull/890) (`conduit_native_handoff`, `conduit_handoff_timeout_s`, `delete_delay_s`).
- Graceful fallback for other clients that cannot run browser scripts, such as Open Relay: a text notice instead of an error, and the chat keeps working (`mobile_fallback`, `js_ack_timeout_s`).

## Install
1. In Open WebUI: **Admin Panel > Functions > +**, paste `model_handoff.py`, save, and enable it.
2. Open its valves and set the router model/API settings and `targets_json`.
3. Attach the filter to your default model.
4. Make sure `WEBUI_SECRET_KEY` is set in the server environment.

Requirements (installed automatically by Open WebUI): `httpx`, `cryptography`. Developed against Open WebUI v0.12.x.

## Development
```bash
pip install pytest httpx cryptography
pytest
```
Browser-script tests need `node` on the PATH.

## Known limitations
- Sidebar hiding depends on Open WebUI's sidebar markup and fails harmlessly if it changes.
- Key encryption saves back through Open WebUI's internal `Functions` model (not a public API). If that fails the filter logs "could not encrypt" and keeps working with the plaintext key.
- Encryption protects exports and DB dumps, not someone who can read both the server environment and the database.
- On Conduit, attachments are not carried over (the prompt lists them), the new chat's prompt is never auto-sent, and Conduit's sidebar may show the deleted chat until it refreshes.
- On other mobile apps (no `chat:handoff`) the old chat is not deleted, and the current model also answers after showing the notice.
- Server-side cleanup uses Open WebUI's internal `Chats` model (not a public API). If it changes, the filter logs a warning and leaves the old chat in place.

## Reporting issues
Please include your Open WebUI version, the filter version, and the server log lines containing `handoff`. For UI problems, add browser console lines starting with `handoff:`.

## License
MIT
