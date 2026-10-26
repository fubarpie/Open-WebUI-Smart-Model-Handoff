# Roadmap to 1.0.0

1.0.0 means: an admin can install this on a fresh Open WebUI, configure it in minutes, and trust it not to break chats, leak keys, or surprise users. Items are grouped into milestones; each has (or should get) a GitHub issue.

_Last re-planned after v0.8.0. Milestone numbers continue from the shipped versions so the roadmap, changelog and tags agree._

## Done
- [x] Valve save-back verified in a real instance; uses the real function id via `__id__` (0.8.0).
- [x] Release automation: pushing a version bump to `main` creates the tag and GitHub Release (`release.yml`).
- [x] CI (GitHub Actions) runs the test suite.
- [x] Sidebar hiding of the old chat with retries, and DELETE status check (0.6.0).
- [x] Model card popup and Fernet encryption of API keys at rest (0.5.0).

## Attempted, not working
- [ ] Mobile fallback (0.7.0) does not work on Conduit or Open Relay in practice. Tracked under 1.1 below; not a 1.0 blocker.

## 0.9 - Safe to ship
- [ ] Never block a user message: every failure path degrades to "stay here", with tested logging.
- [ ] Validate `targets_json` on save: unknown model IDs, duplicates, bad JSON give clear errors.
- [ ] Permissions: only offer targets the user can access.
- [ ] Run the test suite against a real `open_webui` install in CI.
- [ ] Confirm the card `execute` return value path; remove or simplify the fallback if proven.
- [ ] Confirm sidebar hiding against the real markup; add a console diagnostic mode.

## 0.10 - Documentable
- [ ] Compatibility matrix of supported Open WebUI versions, with a stated policy.
- [ ] Full docs: install, valves reference, troubleshooting, FAQ with screenshots.
- [ ] CI additions: lint and a packaging check.
- [ ] Issue and PR templates, CONTRIBUTING and SECURITY.md.

## 1.0 - Release readiness
- [ ] Publish to the Open WebUI community functions directory.
- [ ] Migration note from 0.x valve names.
- [ ] Freeze the valve schema and follow semver afterwards.
- [ ] Document known limitations plainly: mobile apps (Conduit, Open Relay) are not supported yet, and key rotation and the browser-script threat model are post-1.0.

## 1.1 and later
### Mobile app support
- [ ] Make handoff work on Conduit and Open Relay (clients that cannot run the handoff script). Investigate a server-side-only path, then replace or remove the 0.7.0 fallback.

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
