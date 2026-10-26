# Roadmap to 1.0.0

1.0.0 means: an admin can install this on a fresh Open WebUI, configure it in minutes, and trust it not to break chats, leak keys, or surprise users. Items are grouped into milestones; each should become a GitHub issue.

## 0.7 - Verify the unverified
- [ ] Confirm the valve save-back works in a real instance (function id vs module name); use the real function id if not.
- [ ] Confirm the card `execute` return value path; remove or simplify the fallback if proven.
- [ ] Confirm sidebar hiding against the real markup; add a console diagnostic mode.
- [ ] Run the test suite against a real `open_webui` install in CI.

## 0.8 - Routing quality
- [ ] Router prompt tuning plus a fixture set of sample messages with expected targets, scored in tests.
- [ ] Confidence threshold valve and "don't ask again this chat" memory after Stay.
- [ ] Per-user opt-out/opt-in (UserValves) and a per-chat snooze.
- [ ] Skip routing for very short, attachment-only or follow-up messages.
- [ ] Cost/latency controls: router timeout, max input length, caching of repeated decisions.

## 0.9 - Robustness and security
- [x] Conduit native handoff (`chat:handoff`) with client detection and server-side cleanup of the old chat (0.9.0). Still to verify on a real device: first-message chat id at inlet time, whether Conduit re-creates the old chat, and sidebar refresh.
- [ ] Open Relay and other mobile apps: no native handoff event yet; they keep the text-notice fallback.
- [ ] Never block a user message: every failure path degrades to "stay here", with tested logging.
- [ ] Router API key rotation story, plus key re-encryption when `WEBUI_SECRET_KEY` changes.
- [ ] Validate `targets_json` on save: unknown model IDs, duplicates, bad JSON give clear errors.
- [ ] Permissions: only offer targets the user can access.
- [ ] Review of the browser script for XSS and token handling; documented threat model.
- [ ] Handle multi-modal, tool-call and multi-turn context in the handoff payload.
- [ ] Accessibility pass on the card (focus trap, ARIA, keyboard, reduced motion).

## 1.0 - Release readiness
- [ ] Compatibility matrix of supported Open WebUI versions, with a stated policy.
- [ ] Full docs: install, valves reference, troubleshooting, FAQ with screenshots.
- [ ] CI (GitHub Actions): lint, tests, packaging check; issue and PR templates; CONTRIBUTING and SECURITY.md.
- [ ] Publish to the Open WebUI community functions directory.
- [ ] Tagged release with a migration note from 0.x valve names.
- [ ] Freeze the valve schema and follow semver afterwards.

## Later ideas (post-1.0)
- Auto-handoff mode without a popup, with per-target rules.
- Analytics: how often handoffs are offered, accepted, and declined.
- Learning from declines to improve routing.
- Localization of popup text.
