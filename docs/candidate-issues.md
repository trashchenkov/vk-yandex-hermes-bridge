# Candidate GitHub issues

`gh` is not installed in the current environment and no GitHub API token is configured, so these are prepared as import-ready issue drafts. They can be copied into GitHub issues or created later with `gh issue create`.

## Labels to create

- `priority:p0` — Must-have for safe MVP
- `priority:p1` — Important next milestone
- `priority:p2` — Nice to have / later
- `security` — Security, privacy, access control
- `dx` — Developer experience and setup
- `ci` — Continuous integration and release automation
- `observability` — Logs, metrics, health checks
- `vk-api` — VK API integration
- `yandex` — Yandex Cloud / Message Queue / Functions
- `public-mode` — Public community assistant mode
- `rag` — Knowledge base and retrieval
- `good first issue` — Good for first-time contributors

## Issue 1: Add doctor and security-check CLI

Labels: `priority:p0`, `security`, `dx`, `observability`

### Feature description

Add a CLI command that validates a deployment end-to-end and explains failures in human-readable terms.

### Motivation

The bridge depends on VK, Yandex Message Queue, Hermes API Server, local `.env` settings, and optionally systemd. Installation failures are otherwise hard to debug.

### Proposed solution

Add a command such as:

```bash
python -m vk_hermes_worker doctor
python -m vk_hermes_worker security-check
```

Checks should include:

- required env variables;
- `.env` file permissions;
- allowlist / `VK_ALLOW_ALL_USERS` safety;
- Yandex queue read/write access;
- Hermes `/v1/models` reachability;
- VK token validity;
- localhost binding for Hermes API in queue mode;
- worker service status when systemd is used.

### Acceptance criteria

- The command exits non-zero on failed critical checks.
- Output redacts secrets.
- Each failed check includes a concrete remediation hint.

## Issue 2: Add GitHub Actions CI

Labels: `priority:p0`, `ci`, `dx`, `good first issue`

### Feature description

Add CI checks for the Node.js function, Python worker, and shell scripts.

### Proposed solution

Create `.github/workflows/ci.yml` running:

```bash
node --check yandex-vk-hermes-function/index.js
npm ci --prefix yandex-vk-hermes-function
python3 -m py_compile vm-worker/vk_hermes_worker.py
bash -n scripts/setup-hermes-api-server.sh
bash -n scripts/install-vk-hermes-worker-service.sh
bash -n scripts/build-yandex-function-zip.sh
```

Optional follow-ups: ruff, shellcheck, gitleaks.

### Acceptance criteria

- CI runs on pull requests and pushes to `main`.
- CI does not require real VK/Yandex/Hermes secrets.

## Issue 3: Add replay and local emulator tooling

Labels: `priority:p0`, `dx`, `vk-api`, `observability`

### Feature description

Make VK events reproducible locally without real VK/Yandex/Hermes calls.

### Proposed solution

Add commands to replay saved event JSON fixtures with fake queue, fake Hermes, and fake VK sender.

Example:

```bash
python -m vk_hermes_worker replay examples/message_new.json --fake-hermes --dry-run
```

### Acceptance criteria

- Fixtures cover confirmation, message_new, unauthorized user, duplicate event, Hermes timeout, and VK send failure.
- Replay output shows policy decision and outbound message shape.

## Issue 4: Notify owner about unknown users

Labels: `priority:p0`, `security`, `public-mode`, `vk-api`

### Feature description

Add a configurable action for messages from users outside allowlist.

### Proposed solution

Support:

```text
VK_UNAUTHORIZED_MODE=ignore|reply|notify_owner|reply_and_notify
VK_OWNER_PEER_ID=...
```

Owner notifications should include sanitized sender id, peer id, text preview, and timestamp.

### Acceptance criteria

- Unknown users never trigger a Hermes API call.
- Owner notification redacts attachments/secrets where needed.
- Existing `VK_UNAUTHORIZED_REPLY` behavior remains compatible.

## Issue 5: Add shadow mode

Labels: `priority:p1`, `security`, `public-mode`, `observability`

### Feature description

Allow production-like testing without public replies.

### Proposed solution

When `VK_SHADOW_MODE=true`, the worker should process authorized/public events, generate the policy decision and proposed answer, but send only a report to the owner or logs.

### Acceptance criteria

- No response is sent to the original user in shadow mode.
- Owner receives proposed answer and policy metadata when configured.
- Shadow mode is clearly visible in logs.

## Issue 6: Add VK Long Poll mode

Labels: `priority:p1`, `vk-api`, `dx`

### Feature description

Support a no-Yandex deployment mode based on VK Long Poll.

### Motivation

Yandex Callback API + Message Queue is production-oriented but heavy for quick local or hobby deployments.

### Acceptance criteria

- Long Poll mode uses the same worker processing pipeline and policy checks.
- README documents trade-offs versus Callback API + Queue.
- Long Poll mode can be run without exposing Hermes publicly.

## Issue 7: Add policy engine

Labels: `priority:p1`, `security`, `public-mode`

### Feature description

Replace ad-hoc allowlist logic with explicit policy rules.

### Proposed roles

- owner: full private assistant;
- trusted: limited assistant;
- unknown: FAQ/RAG only or blocked;
- group chat: reply only on mention;
- maintenance: no Hermes calls.

### Acceptance criteria

- Policies are configured in a file or env-compatible format.
- Every inbound event receives a logged policy decision.
- Unknown users cannot access owner capabilities.

## Issue 8: Add public FAQ/RAG mode over community materials

Labels: `priority:p1`, `public-mode`, `rag`, `security`

### Feature description

Provide a safe public assistant that answers from the author's materials rather than unrestricted model knowledge.

### Possible sources

- VK wall posts;
- GitHub README/docs;
- Markdown files;
- blog/RSS posts;
- PDFs/documents.

### Acceptance criteria

- Public answers cite sources when possible.
- If no reliable source is found, the bot hands off instead of hallucinating.
- Public mode can be routed to a separate Hermes profile/toolset.

## Issue 9: Add separate Hermes profile support for public mode

Labels: `priority:p1`, `security`, `public-mode`

### Feature description

Route public requests to a dedicated safe Hermes profile with separate memory and restricted tools.

### Acceptance criteria

- Owner/private requests and public requests can use different Hermes session keys/profiles.
- Public profile is documented with recommended disabled tools.
- Doctor/security-check warns if public mode uses the private owner profile.

## Issue 10: Add structured logs and health report

Labels: `priority:p1`, `observability`, `dx`

### Feature description

Emit structured logs and provide a health/status command.

### Log fields

- event id;
- peer id;
- authorization decision;
- queue message id;
- Hermes latency;
- VK send latency;
- status/error.

### Acceptance criteria

- Logs can be parsed as JSON when enabled.
- Health command reports queue, Hermes, VK, and worker status.
- Secrets are redacted.

## Issue 11: Add Docker Compose deployment

Labels: `priority:p2`, `dx`

### Feature description

Provide a Docker Compose recipe for running the worker.

### Acceptance criteria

- Compose file mounts `.env` and persistent state volume.
- README documents systemd vs Docker trade-offs.
- Container image does not bake in secrets.

## Issue 12: Add native media forwarding support

Labels: `priority:p2`, `vk-api`, `public-mode`

### Feature description

Handle inbound and outbound VK attachments beyond text metadata.

### Scope

- download inbound photos/documents under configured limits;
- optionally OCR images;
- pass safe files to Hermes for owner/trusted users;
- upload outbound `MEDIA:` replies to VK as document/photo.

### Acceptance criteria

- Attachment handling is gated by trust level.
- Size/MIME limits are enforced.
- Unsupported attachments degrade gracefully.
