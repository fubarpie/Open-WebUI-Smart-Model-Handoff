# Roadmap to 1.0.0

1.0.0 means: an admin can install this on a fresh Open WebUI, configure it in minutes, and trust it not to break chats, leak keys, or surprise users. Items are grouped into milestones; each has (or should get) a GitHub issue.

_Last re-planned after v0.8.0. Milestone numbers continue from the shipped versions so the roadmap, changelog and tags agree._

## Done
- [x] Conduit native handoff (`chat:handoff`) with client detection and server-side cleanup of the old chat (0.9.0). Still to verify on a real device: first-message chat id at inlet time, whether Conduit re-creates the old chat, and sidebar refresh.
- [x] Valve save-back verified in a real instance; uses the real function id via `__id__` (0.8.0).
- [x] Release automation: pushing a version bump to `main` creates the tag and GitHub Release (`release.yml`).
- [x] CI (GitHub Actions) runs the test suite.
- [x] Mobile fallback for clients that cannot run the handoff script (Conduit builds without `chat:handoff`, Open Relay): shows a text notice, the current model answers, no generation error (0.7.0).
- [x] Sidebar hiding of the old chat with retries, and DELETE status check (0.6.0).
- [x] Model card popup and Fernet encryption of API keys at rest (0.5.0).

## 0.9 - Safe to ship
- [ ] Never block a user message: every failure path degrades to "stay here", with tested logging.
- [ ] Validate `targets_json` on save: unknown model IDs, duplicates, bad JSON give clear errors.
- [ ] Permissions: only offer targets the user can access.
- [ ] Run the test suite against a real `open_webui` install in CI.
- [ ] Confirm the card `execute` return value path; remove or simplify the card's plain-popup fallback if proven.
- [ ] Confirm sidebar hiding against the real markup; add a console diagnostic mode.
- [ ] Verify the `Chats` model method names used by the Conduit cleanup against a real Open WebUI install.

## 0.10 - Documentable
- [ ] Compatibility matrix of supported Open WebUI versions, with a stated policy.
- [ ] Full docs: install, valves reference, troubleshooting, FAQ with screenshots.
- [ ] CI additions: lint and a packaging check.
- [ ] Issue and PR templates, CONTRIBUTING and SECURITY.md.

## 1.0 - Release readiness
- [ ] Publish to the Open WebUI community functions directory.
- [ ] Migration note from 0.x valve names.
- [ ] Freeze the valve schema and follow semver afterwards.
- [ ] Document known limitations plainly: Conduit hands off natively (prompt is left unsent, attachments are not carried over); on other mobile apps (Open Relay) handoff does not auto-switch models, you only get the text notice; key rotation and the browser-script threat model are post-1.0.

## 1.1 and later
### Mobile app support
- [ ] Auto-switching on Open Relay and other clients that cannot run the handoff script (Conduit is covered natively since 0.9.0). Keep the existing 0.7.0 text-notice fallback as the safe default when the switch cannot be done.

### Routing quality
- [ ] Router prompt tuning plus a fixture set of sample messages with expected targets, scored in tests.
- [ ] Confidence threshold valve and "don't ask again this chat" memory after Stay.
- [ ] Per-user opt-out/opt-in (UserValves) and a per-chat snooze.
- [ ] Skip routing for very short, attachment-only or follow-up messages.
- [ ] Cost/latency controls: router timeout, max input length, caching of repeated decisions.

### Security and accessibility (deferred from the 1.0 fast track)
- [ ] Router API key rotation story, plus key re-encryption when `WEBUI_SECRET_KEY` changes.
- [ ] Review of the browser script for XSS and token handling; documented threat model.
- [ ] Accessibility pass on the card (focus trap, ARIA, keyboard, reduced motion).
- [ ] Handle multi-modal, tool-call and multi-turn context in the handoff payload.

## Later ideas
- Auto-handoff mode without a popup, with per-target rules (no earlier than 1.2; it cuts against "don't surprise users").
- Analytics: how often handoffs are offered, accepted, and declined.
- Learning from declines to improve routing.
- Localization of popup text.
