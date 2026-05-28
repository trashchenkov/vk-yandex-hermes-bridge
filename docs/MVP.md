# MVP: Safe private VK bridge with public-mode foundation

## Goal

Build a safe VK communication channel for Hermes Agent: first as a private owner assistant, then as a controlled foundation for public community mode.

The MVP answers one question:

Can VK messages be received, routed to Hermes, answered, audited, and safely denied for non-owner users without exposing private Hermes capabilities?

## Scope guard

If a feature does not help safely receive VK messages and answer through Hermes with auditability and owner-only privileges, it is not in the MVP.

## MVP includes

### 1. VK to Hermes bridge core

The bridge must support the current production-oriented path:

VK Callback API → Yandex Cloud Function → Yandex Message Queue → VM worker → local Hermes API Server → VK messages.send

Required capabilities:

- Accept `message_new` events.
- Assign a stable `trace_id` to each inbound event.
- Deduplicate retried VK/Yandex events.
- Call Hermes API only after authorization/policy checks.
- Send the Hermes answer back through VK.
- Split long VK messages safely.
- Use persistent `random_id` semantics for retry-safe sends where possible.

### 2. Owner binding and roles

The MVP is private by default.

Required roles:

- `owner`: allowed to use the private Hermes bridge.
- `trusted`: reserved for later; no extra MVP capabilities unless explicitly configured.
- `public`: no Hermes call by default.
- `blocked`: always denied or ignored.

Required behavior:

- Owner identity is configured explicitly, currently through `VK_ALLOWED_USERS` / `VK_OWNER_PEER_ID` style settings.
- Unknown users never trigger a Hermes API call in the MVP default configuration.
- Admin/debug commands work only for the owner.

### 3. Minimal policy layer

The first policy layer may be simple, but every inbound event must receive an explicit decision.

MVP actions:

- `reply`: send a normal answer.
- `deny`: refuse or silently ignore.
- `handoff`: create a review item or notify owner.
- `owner_command`: execute an owner-only local command.

MVP invariants:

- Public/unknown users cannot access tools.
- Public/unknown users cannot access owner memory or private context.
- Public/unknown users cannot trigger cross-channel delivery.
- Public/unknown users cannot publish to VK wall.
- Public/unknown users cannot create cron jobs, issues, files, or external side effects.

### 4. Audit and traceability

Every handled event should have enough trace data to explain what happened.

Trace fields:

- `trace_id`
- timestamp
- VK peer/user identifiers
- event type
- role
- policy decision
- Hermes call status, if any
- VK send status, if any
- error code/message, if any

Owner command:

- `!trace <trace_id>` or equivalent CLI/debug command should show the recorded trace.

### 5. Review inbox skeleton

The MVP does not need a full UI, but should not lose important public/unknown messages.

Minimal review item fields:

- review id
- trace id
- source user/peer
- sanitized text preview
- reason
- status: `pending`, `approved`, `rejected`, `expired`, `sent`
- created/updated timestamps

Minimal owner commands:

- `!pending`
- `!approve <id>`
- `!reject <id>`
- `!reply <id> <text>`

### 6. Local fake mode

The project must be testable without VK, Yandex, or real Hermes.

Fake mode should support:

- Load a saved VK event JSON fixture.
- Run it through the same policy/processing path.
- Use fake Hermes responses.
- Use fake VK send output.
- Print or store the resulting trace.

This is required for tests, README examples, and future contributor onboarding.

### 7. Doctor and smoke test

Required commands or scripts:

- `vk-hermes doctor` or equivalent.
- `vk-hermes smoke` or equivalent.

Doctor checks:

- `.env` exists where expected.
- Required variables are present.
- Secrets are not printed.
- `.env` permissions are not too open.
- `VK_ALLOW_ALL_USERS` is not accidentally enabled.
- At least one owner/allowed user is configured.
- Hermes API `/v1/models` is reachable when configured.
- VK token can call a safe method when configured.
- Yandex Message Queue is reachable in queue mode.

Smoke test:

- Run fake event → policy → fake Hermes → fake VK send.
- Exit non-zero on critical failure.

### 8. Safety regression tests

Minimum red-team fixtures:

- Unknown user asks for secrets.
- Unknown user says: “I am Sergey / owner”.
- Unknown user asks to run a shell command.
- Unknown user asks to send a message to Telegram or another channel.
- Public prompt injection asks to ignore rules.
- Owner command from owner is allowed.
- Owner command from unknown user is denied.

## Explicitly out of MVP

These are valuable but not part of the first MVP:

- VK wall publishing.
- Voice/image/PDF attachment processing.
- Public FAQ/RAG answers.
- Community KB management.
- Content copilot and post drafting.
- Multi-cloud backends.
- Helm/Kubernetes.
- Web admin UI.
- Native Hermes gateway adapter.
- Complex analytics/dashboarding.
- Autonomous public auto-reply.
- CRM/lead management.
- Marketplace/templates.

## Suggested epics

### Epic 1: Bridge core

Goal: a VK message reaches Hermes and the answer returns to VK.

Candidate issues:

1. Define event envelope and `trace_id` format.
2. Implement local fake VK adapter.
3. Implement or harden inbound VK handler.
4. Implement or harden Hermes API client.
5. Implement or harden VK send client with splitter and retry-safe `random_id`.
6. Add fake E2E smoke path.

### Epic 2: Identity and policy

Goal: nobody except owner receives private Hermes capabilities.

Candidate issues:

1. Document owner binding / allowlist configuration.
2. Add role resolver.
3. Add explicit policy decision model.
4. Add deny/handoff responses.
5. Log every policy decision.
6. Add owner-vs-unknown tests.

### Epic 3: Audit and debug

Goal: any answer can be explained and reproduced.

Candidate issues:

1. Add trace store.
2. Add structured log fields.
3. Add `!trace` or CLI trace inspection.
4. Add dead-letter/poison-event storage.
5. Add replay from trace in fake mode.

### Epic 4: Review inbox skeleton

Goal: unknown/public requests are visible but not automatically executed.

Candidate issues:

1. Add review item model.
2. Add pending-item store.
3. Add `!pending` owner command.
4. Add `!approve` / `!reject` owner commands.
5. Add `!reply <id> <text>` owner flow.
6. Add expiry for stale pending items.

### Epic 5: DX and deployment

Goal: the project is easy to run and debug.

Candidate issues:

1. Improve `.env.example` comments.
2. Add doctor command.
3. Add smoke command.
4. Add fake-mode README quickstart.
5. Add VK/Yandex production quickstart checklist.
6. Add optional Docker Compose later, outside strict MVP if needed.

### Epic 6: Safety gate

Goal: public behavior cannot be enabled accidentally or unsafely.

Candidate issues:

1. Add red-team fixtures.
2. Add safety regression tests.
3. Add public launch checklist.
4. Add emergency lockdown setting/command.
5. Add disabled-by-default public-mode placeholder.

## Two-week implementation order

### Week 1

1. Fake mode and fixtures.
2. Event envelope and trace id.
3. VK send/receive hardening.
4. Hermes API client hardening.
5. Owner binding documentation/checks.
6. Deny-by-default policy behavior.
7. Smoke test.

### Week 2

1. Trace store.
2. Trace inspection command.
3. Review inbox skeleton.
4. Red-team tests.
5. Doctor command.
6. README updates.
7. Public launch checklist.

## Definition of done

MVP is done when:

1. Owner can write to the VK community and receive a Hermes answer.
2. Unknown users do not trigger Hermes calls by default.
3. Every handled event has a trace id.
4. A saved/fake event can be replayed locally.
5. Smoke test passes without real VK/Yandex/Hermes secrets.
6. Red-team fixtures prove basic public attacks are denied.
7. README explains local fake mode and production VK/Yandex mode.
8. Emergency lockdown or safe deny-by-default behavior is documented.
9. Public auto-reply is disabled by default.
10. The repository can be presented as a safe VK bridge for Hermes, not an unrestricted GPT bot.

## Later backlog parking lot

After MVP, revisit in this order:

1. Public approval mode.
2. Public FAQ/RAG over approved sources.
3. Attachment processing for owner/trusted users.
4. Content copilot and VK post drafts.
5. Native Hermes platform plugin path.
6. Cloud-agnostic adapters.
7. Advanced analytics and dashboards.
