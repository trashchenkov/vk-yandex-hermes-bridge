# Roadmap

This roadmap focuses on turning the bridge from a working VK-to-Hermes deployment recipe into a safe, observable, reusable community-assistant toolkit.

## Guiding principles

- Keep Hermes private by default: no public inbound access to the Hermes API Server.
- Treat VK communities as semi-public: unknown users must not get owner-level agent capabilities.
- Prefer explicit policies over implicit trust.
- Make setup and debugging easy enough for people who are not already familiar with VK Callback API, Yandex Cloud, or Hermes internals.
- Support a path from private owner assistant to safe public FAQ/RAG assistant.

## MVP hardening

### 1. Doctor and security-check CLI

Goal: make installation problems obvious.

Checks:

- `.env` exists and required variables are present.
- `.env` permissions are not too open.
- `VK_ALLOW_ALL_USERS` is not enabled accidentally.
- `VK_ALLOWED_USERS` or another policy allows at least one owner/admin.
- Yandex Message Queue is reachable.
- Hermes API Server answers `/v1/models`.
- VK token can call a safe API method.
- Worker service is installed/running when systemd is used.
- Hermes API is bound to localhost for the queue/polling deployment.

### 2. GitHub Actions CI

Goal: keep the public repo safe and reproducible.

Checks:

- `node --check yandex-vk-hermes-function/index.js`
- `npm ci` for the function package
- `python -m py_compile vm-worker/vk_hermes_worker.py`
- `bash -n scripts/*.sh`
- optional secret scan

### 3. Replay/emulator tooling

Goal: reproduce VK/Yandex/Hermes bugs locally.

Commands:

- replay a saved VK event JSON;
- run with fake queue and fake Hermes;
- simulate duplicate events, Hermes timeout, VK send failure, and unauthorized user.

### 4. Owner notifications for unknown users

Goal: make semi-public deployments safe but visible.

Modes:

- ignore unknown users;
- reply with a private-bot message;
- notify owner with sanitized text and sender id;
- optionally create a local audit entry.

### 5. Shadow mode

Goal: test public behavior without replying publicly.

The worker receives real events and produces proposed decisions/answers, but sends them only to the owner or logs them. This is the safest pre-launch mode for public communities.

## v0.2 usability and deployment

### 6. VK Long Poll mode

Goal: provide a no-Yandex quickstart for hobby deployments.

Long Poll mode should be documented as simpler but less production-oriented than Callback API + Queue.

### 7. Docker Compose

Goal: make the VM worker easy to run without systemd.

Include:

- worker container;
- persistent state volume;
- `.env` mount;
- healthcheck command.

### 8. One-command Yandex deploy helper

Goal: reduce cloud setup friction.

A helper script can build the function artifact, check `yc` authentication, create/update cloud resources when possible, and print the VK Callback URL.

### 9. Structured logs and health report

Goal: make production failures diagnosable.

Log fields:

- event id;
- peer id;
- authorization decision;
- queue message id;
- Hermes latency;
- VK send latency;
- status/error.

### 10. Dead-letter / poison-message handling

Goal: avoid endless retries on bad events.

After repeated failures, move the event to a local poison store or DLQ and notify the owner.

## v0.3 public community assistant

### 11. Policy engine

Goal: make access control explicit and configurable.

Example roles:

- owner: full private assistant;
- trusted: limited assistant;
- unknown: FAQ/RAG only;
- group chat: reply only on mention;
- maintenance: no Hermes calls.

### 12. Separate Hermes profile for public mode

Goal: isolate public users from the owner's private tools, memory, and context.

The bridge should support routing public requests to a dedicated safe Hermes profile with restricted toolsets.

### 13. Human-in-the-loop approvals

Goal: allow safe public replies without full automation.

For selected policies, Hermes drafts an answer, but the owner approves, edits, or rejects it via VK/Telegram.

### 14. Public FAQ/RAG mode

Goal: answer subscribers using the author's materials, not unrestricted model knowledge.

Sources may include:

- VK wall posts;
- GitHub repos/README files;
- Markdown notes;
- blog/RSS posts;
- uploaded documents.

Public answers should include sources or trigger handoff when no reliable source is found.

### 15. Freshness guard for AI topics

Goal: avoid overconfident answers from outdated AI content.

Sources should carry timestamps. Old sources, especially in fast-moving AI topics, should be marked as potentially outdated or require owner approval.

## v1.0 platform direction

### 16. Adapter architecture

Goal: make VK the first adapter, not the only one.

Extract interfaces for:

- source adapter;
- queue adapter;
- agent adapter;
- sink adapter;
- policy middleware.

### 17. Observability integrations

Goal: support serious deployments.

Potential features:

- Prometheus metrics;
- OpenTelemetry traces;
- status dashboard;
- weekly maintainer report.

### 18. Native Hermes platform plugin path

Goal: document or implement a migration path from external bridge to a Hermes gateway platform plugin.

This would enable native targets like `vk:<peer_id>`, cron delivery to VK, and deeper Hermes gateway integration.

## Good first issues

- Add GitHub Actions CI.
- Add `make check` or `scripts/check.sh`.
- Add Mermaid architecture diagram to README.
- Add replay fixtures for confirmation, message_new, unauthorized user, and duplicate event.
- Improve `.env.example` comments.
- Add a troubleshooting section for common VK/Yandex/Hermes errors.
