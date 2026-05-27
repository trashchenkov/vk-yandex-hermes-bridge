# VK → Yandex Cloud Function → Yandex Message Queue → Hermes Agent

Goal: keep Telegram working and add VK as a second communication channel through the already-confirmed VK Callback API webhook on Yandex Cloud Functions.

## Chosen production architecture

```text
VK Callback API
  → Yandex Cloud Function (Node.js, index.handler)
  → Yandex Message Queue
  → VM worker polls queue outbound
  → local Hermes API Server on 127.0.0.1:8642
  → VM worker sends answer via VK API messages.send
  → VK chat
```

This is Yandex-native at the public edge and does **not** expose Hermes API Server to the Internet.

## Files

- `yandex-vk-hermes-function/index.js` — Yandex Function handler. Default `BRIDGE_MODE=queue`.
- `yandex-vk-hermes-function/package.json` — Node package with AWS SQS SDK for Yandex Message Queue.
- `vm-worker/vk_hermes_worker.py` — VM-side queue poller and Hermes/VK processor.
- `vm-worker/requirements.txt` — Python dependencies for the worker.
- `scripts/setup-hermes-api-server.sh` — prepares Hermes API Server env vars on the VM.
- `scripts/install-vk-hermes-worker-service.sh` — installs the VM worker as a systemd service.

## Yandex resources

Create:

1. Yandex Message Queue, preferably FIFO if you want per-peer ordering. Standard queue is OK for MVP.
2. Service account static key for the Cloud Function with permission to send messages to the queue.
3. Service account static key for the VM worker with permission to receive/delete messages from the queue.

Minimum roles are usually:

- Cloud Function SA: `ymq.writer` on the queue/folder.
- VM worker SA/static key: `ymq.reader` on the queue/folder.

## Cloud Function env vars

Existing VK vars:

```text
VK_GROUP_ID
VK_CONFIRMATION_TOKEN
VK_SECRET
```

Queue vars:

```text
BRIDGE_MODE=queue
QUEUE_URL=https://message-queue.api.cloud.yandex.net/b1.../dj.../vk-hermes-queue
AWS_ACCESS_KEY_ID=<static key id with ymq.writer>
AWS_SECRET_ACCESS_KEY=<static key secret>
AWS_REGION=ru-central1
QUEUE_ENDPOINT=https://message-queue.api.cloud.yandex.net
```

`VK_GROUP_TOKEN` is not required in the callback function for queue mode; keep it on the VM worker instead.

## Hermes VM setup

Enable local Hermes API Server:

```bash
API_SERVER_HOST=127.0.0.1 API_SERVER_PORT=8642 ./scripts/setup-hermes-api-server.sh
systemctl restart hermes-gateway.service
curl -sS -H "Authorization: Bearer YOUR_API_SERVER_KEY" http://127.0.0.1:8642/v1/models
```

Install worker:

```bash
/root/vk-yandex-hermes-bridge/scripts/install-vk-hermes-worker-service.sh
nano /root/vk-yandex-hermes-bridge/.env
systemctl restart vk-hermes-worker.service
journalctl -u vk-hermes-worker.service -f
```

Worker `.env`:

```text
QUEUE_URL=<same queue URL>
AWS_ACCESS_KEY_ID=<static key id with ymq.reader>
AWS_SECRET_ACCESS_KEY=<static key secret>
AWS_REGION=ru-central1
QUEUE_ENDPOINT=https://message-queue.api.cloud.yandex.net

VK_GROUP_TOKEN=<VK community token with messages permission>
VK_API_VERSION=5.199
VK_ALLOWED_USERS=<comma-separated allowed VK user ids>
VK_ALLOW_ALL_USERS=false
VK_UNAUTHORIZED_REPLY=Бот приватный. Доступ к Hermes Agent ограничен.

HERMES_API_BASE=http://127.0.0.1:8642
HERMES_MODEL=hermes-agent
HERMES_TIMEOUT_MS=120000
```

The worker also loads `/root/.hermes/.env`, so it can use `API_SERVER_KEY` directly as `HERMES_API_KEY` fallback.

## Deploy with `yc` CLI

If `yc` is installed and authenticated:

```bash
cd /root/vk-yandex-hermes-bridge/yandex-vk-hermes-function
npm install --omit=dev
zip -r /tmp/vk-hermes-function.zip index.js package.json node_modules

yc serverless function version create \
  --function-name <function-name> \
  --runtime nodejs18 \
  --entrypoint index.handler \
  --memory 256m \
  --execution-timeout 10s \
  --source-path /tmp/vk-hermes-function.zip \
  --environment BRIDGE_MODE=queue,QUEUE_URL=<queue-url>,AWS_ACCESS_KEY_ID=<writer-key-id>,AWS_SECRET_ACCESS_KEY=<writer-secret>,AWS_REGION=ru-central1,QUEUE_ENDPOINT=https://message-queue.api.cloud.yandex.net,VK_GROUP_ID=<group-id>,VK_CONFIRMATION_TOKEN=<confirmation>,VK_SECRET=<secret>
```

Use secret-manager or console env editor if you do not want secrets in shell history.

## VK Callback behavior

- `confirmation` returns `VK_CONFIRMATION_TOKEN` as plain text.
- `message_new` validates `group_id` and `secret`, enqueues to Yandex Message Queue, then returns literal `ok` immediately.
- If enqueue fails, the function returns HTTP 500 so VK retries instead of dropping the event.
- VM worker deduplicates events in SQLite before replying.
- VM worker enforces `VK_ALLOWED_USERS` unless `VK_ALLOW_ALL_USERS=true`; unauthorized users are blocked before any Hermes API call.
- `/start`, `начать`, `/help`, `помощь` are handled by the VM worker without spending Hermes tokens.
- Hermes conversation/session key is `vk:<peer_id>`.

## Legacy modes

`index.js` still supports the old modes for debugging:

```text
BRIDGE_MODE=sync
BRIDGE_MODE=fire_and_forget
BRIDGE_INTERNAL_URL=...
BRIDGE_INTERNAL_SECRET=...
```

For production, prefer `BRIDGE_MODE=queue`.
