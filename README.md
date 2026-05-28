# VK → Yandex Message Queue → Hermes Agent bridge

Production-oriented bridge for using VK community messages as a Hermes Agent communication channel without exposing the Hermes API Server to the public internet.

Architecture:

```text
VK Callback API
  → Yandex Cloud Function
  → Yandex Message Queue
  → VM worker polls queue outbound
  → local Hermes API Server at 127.0.0.1
  → VK messages.send
```

Why this shape:

- VK Callback API expects a quick `ok`; LLM/tool calls can be slow.
- Hermes has powerful tools and local access; keep the API Server private.
- Queue retries make transient failures visible instead of silently dropping updates.

## Contents

```text
yandex-vk-hermes-function/   Yandex Cloud Function, Node.js 18+
vm-worker/                   Python VM worker: queue → Hermes → VK
scripts/                     setup/build/systemd helpers
docs/                        deployment notes
.env.example                 safe template; real .env is ignored
```

## Security defaults

This repository is intended for private agent access, even if the VK community is public.

- Do not commit `.env`, tokens, callback secrets, queue credentials, or Hermes API keys.
- Keep Hermes API Server bound to `127.0.0.1` for this queue/polling design.
- Set `VK_SECRET` in VK Callback API and in the Yandex Function.
- Set `VK_ALLOWED_USERS=<your_vk_user_id>` on the VM worker.
- Do not set `VK_ALLOW_ALL_USERS=true` outside short smoke tests.

Unknown VK users are blocked before any Hermes call. By default they receive: `Бот приватный. Доступ к Hermes Agent ограничен.` Set `VK_UNAUTHORIZED_MODE` to `ignore` to silently ignore them, `notify_owner` to send only an owner alert, or `reply_and_notify` to do both. Owner alerts use `VK_OWNER_PEER_ID` and include only sanitized sender/trace/text preview metadata.

Set `VK_SHADOW_MODE=true` to test allowed-user traffic safely: the worker calls Hermes and stores trace status, but does not send the proposed answer to the original VK peer. Instead it sends a sanitized `Shadow VK answer` report to `VK_OWNER_PEER_ID`.

## Local replay and smoke checks

Use fake local checks before touching live VK/Yandex/Hermes credentials:

```bash
python vm-worker/vk_hermes_worker.py --smoke
python vm-worker/vk_hermes_worker.py --replay fixtures/vk/message_new_owner.json fixtures/vk/duplicate_event.json
```

`--replay` uses fake Hermes/VK sends and prints policy decisions, trace IDs, outbound payload shape, duplicate handling, and simulated failure outcomes for fixtures such as `hermes_timeout.json` and `vk_send_failure.json`.

## Health and structured logs

Use the health command to check worker mode, queue/Long Poll config, Hermes config, VK config, policy allowlist, and SQLite state stores without exposing secrets:

```bash
python vm-worker/vk_hermes_worker.py --health
python vm-worker/vk_hermes_worker.py --long-poll --health
```

For machine-parseable production logs set `LOG_FORMAT=json`. JSON logs include timestamp, level, logger and redacted message text.

## Quick start

1. Copy env template:

```bash
cp .env.example .env
chmod 600 .env
```

2. Prepare Yandex Cloud resources:

- Create a Yandex Message Queue: https://yandex.cloud/ru/docs/message-queue/quickstart/
- Create service accounts for the Cloud Function and VM worker: https://yandex.cloud/ru/docs/iam/operations/sa/create/
- Create static access keys for those service accounts: https://yandex.cloud/ru/docs/iam/operations/authentication/manage-access-keys/
- Grant the Cloud Function key write permission to the queue.
- Grant the VM worker key receive/delete permission.
- Role reference: https://yandex.cloud/ru/docs/iam/concepts/access-control/roles/

3. Configure VK community Callback API:

- enable community messages and bot capabilities: https://dev.vk.com/ru/api/bots/getting-started
- enable `message_new` events;
- set callback secret;
- configure the public Yandex Function URL;
- Callback API guide: https://dev.vk.com/ru/api/callback/getting-started
- VK `messages.send` reference: https://dev.vk.com/ru/method/messages.send

4. Build Cloud Function artifact:

```bash
./scripts/build-yandex-function-zip.sh
```

5. Create or update the Yandex Cloud Function:

- Cloud Functions overview: https://yandex.cloud/ru/docs/functions/
- Create a function: https://yandex.cloud/ru/docs/functions/quickstart/create-function/
- Function concepts and runtime settings: https://yandex.cloud/ru/docs/functions/concepts/function/

Deploy `/tmp/vk-hermes-function.zip` to Yandex Cloud Functions with handler `index.handler` and env vars from `.env` relevant to the function:

```text
BRIDGE_MODE=queue
QUEUE_URL=...
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
AWS_REGION=ru-central1
QUEUE_ENDPOINT=https://message-queue.api.cloud.yandex.net
VK_GROUP_ID=...
VK_CONFIRMATION_TOKEN=...
VK_SECRET=...
```

6. Prepare Hermes API Server on the VM:

```bash
API_SERVER_HOST=127.0.0.1 API_SERVER_PORT=8642 ./scripts/setup-hermes-api-server.sh
systemctl restart hermes-gateway.service
```

Smoke test:

```bash
curl -sS -H "Authorization: Bearer YOUR_API_SERVER_KEY" http://127.0.0.1:8642/v1/models
```

7. Install and start worker:

```bash
./scripts/install-vk-hermes-worker-service.sh
systemctl restart vk-hermes-worker.service
journalctl -u vk-hermes-worker.service -f
```

## Worker env

Important VM worker settings:

```text
QUEUE_URL=...
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
VK_GROUP_TOKEN=...
VK_ALLOWED_USERS=<your_vk_user_id>
HERMES_API_BASE=http://127.0.0.1:8642
```

If `HERMES_API_KEY` is empty, the worker loads `API_SERVER_KEY` from `--hermes-env`, defaulting to `/root/.hermes/.env`.

## Local fake mode

You can replay saved VK event fixtures without real VK, Yandex Message Queue, or Hermes credentials:

```bash
VK_ALLOWED_USERS=254662087 python3 vm-worker/vk_hermes_worker.py \
  --fake-event fixtures/vk/message_new_owner.json \
  --fake-hermes-answer "Fake Hermes response" \
  --dedup-db :memory:
```

The command prints JSON with `trace_id`, `role`, `policy_decision`, whether Hermes would be called, and the fake outbound VK messages.

## VK Long Poll mode

For a simpler local/hobby deployment you can skip Yandex Cloud Function and Message Queue and let the VM worker poll VK directly:

```bash
python3 vm-worker/vk_hermes_worker.py --long-poll
```

For a single debug cycle:

```bash
python3 vm-worker/vk_hermes_worker.py --long-poll --once
```

Required settings are still `VK_GROUP_ID`, `VK_GROUP_TOKEN`, owner/trusted allowlist, and local `HERMES_API_BASE`/key. Hermes can remain bound to `127.0.0.1`; no public Hermes endpoint is needed.

Trade-offs versus Callback API + Queue:

- Long Poll is easier to run locally and needs no public callback URL or Yandex resources.
- Callback API + Queue is better for production durability: VK gets a quick `ok`, queue retries survive worker restarts, and long LLM/tool calls do not hold the VK polling connection.
- Both modes use the same `process_payload` pipeline, dedup store, trace store, review inbox, and access policy checks.

For a one-command fake E2E smoke check that needs no real VK, Yandex, or Hermes secrets:

```bash
VK_ALLOWED_USERS=254662087 python3 vm-worker/vk_hermes_worker.py \
  --smoke \
  --state-dir /tmp/vk-hermes-smoke
```

The smoke command runs owner fixture → policy → fake Hermes → fake VK send and public fixture → default deny. It exits `0` on success and non-zero on failure, and prints the trace id plus final fake outbound payload for each path.

Unknown users are denied before the fake Hermes call:

```bash
VK_ALLOWED_USERS=254662087 python3 vm-worker/vk_hermes_worker.py \
  --fake-event fixtures/vk/message_new_unknown.json \
  --dedup-db :memory:
```

## Verification

Run local checks before publishing/deploying:

```bash
node --check yandex-vk-hermes-function/index.js
python3 -m py_compile vm-worker/vk_hermes_worker.py
python3 -m pytest -q
bash -n scripts/setup-hermes-api-server.sh
bash -n scripts/install-vk-hermes-worker-service.sh
bash -n scripts/build-yandex-function-zip.sh
```

Operational status checks:

```bash
python3 vm-worker/vk_hermes_worker.py --health
python3 vm-worker/vk_hermes_worker.py --long-poll --health
LOG_FORMAT=json python3 vm-worker/vk_hermes_worker.py --health
```

`--health` reports worker, queue, Hermes, VK/policy, and state DB status with secrets redacted. Set `LOG_FORMAT=json` for machine-parseable logs.

## Roadmap

See `docs/MVP.md` for the current MVP scope and `docs/mvp-issues.md` for implementation-ready issue drafts.

Before enabling any public/community behavior, use `docs/public-launch-checklist.md`.

See `ROADMAP.md` for the broader roadmap: doctor/security checks, CI, replay tooling, shadow mode, VK Long Poll mode, policy engine, public FAQ/RAG mode, owner approvals, and native Hermes platform-plugin direction.

## Operational notes

- Queue messages are deleted only after successful processing and VK reply.
- Duplicate VK events are filtered with a SQLite dedup store.
- `/help`, `помощь`, `/start`, `начать` are handled locally by the worker.
- Responses are chunked below VK's 9000 character message limit.
- Attachments are currently summarized by type and passed to Hermes as text metadata; native media forwarding can be added later.

See `docs/vk-yandex-hermes-bridge.md` for the detailed deployment reference.
