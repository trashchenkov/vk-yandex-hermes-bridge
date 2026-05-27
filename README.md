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

Unknown VK users are blocked before any Hermes call. By default they receive: `Бот приватный. Доступ к Hermes Agent ограничен.` Set `VK_UNAUTHORIZED_REPLY=` to silently ignore them.

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

## Verification

Run local checks before publishing/deploying:

```bash
node --check yandex-vk-hermes-function/index.js
python3 -m py_compile vm-worker/vk_hermes_worker.py
bash -n scripts/setup-hermes-api-server.sh
bash -n scripts/install-vk-hermes-worker-service.sh
bash -n scripts/build-yandex-function-zip.sh
```

## Operational notes

- Queue messages are deleted only after successful processing and VK reply.
- Duplicate VK events are filtered with a SQLite dedup store.
- `/help`, `помощь`, `/start`, `начать` are handled locally by the worker.
- Responses are chunked below VK's 9000 character message limit.
- Attachments are currently summarized by type and passed to Hermes as text metadata; native media forwarding can be added later.

See `docs/vk-yandex-hermes-bridge.md` for the detailed deployment reference.
