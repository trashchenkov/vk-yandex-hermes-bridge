# MVP release/deploy checklist

This checklist is for the private-by-default MVP path:

VK Callback API → Yandex Cloud Function → Yandex Message Queue → VM worker → Hermes API → VK

Use it before exposing the VK community bridge to real users. Keep public auto-reply disabled unless you have separately completed the public launch checklist.

## 1. Preflight on the repo

Run from the repo root on the deploy machine or CI runner:

```bash
git status -sb
git pull --ff-only
make check
```

`make check` runs Python syntax, Python tests, Node syntax, shell syntax, and `docker compose config` when Docker Compose is available.

## 2. Required env

Copy `.env.example` to `.env` and set at least:

```text
VK_OWNER_ID=<owner_vk_user_id>
VK_GROUP_ID=<vk_group_id>
VK_GROUP_TOKEN=<vk_community_token>
HERMES_API_BASE=http://127.0.0.1:8642
QUEUE_URL=<yandex_message_queue_url>
AWS_ACCESS_KEY_ID=<service_account_key_id>
AWS_SECRET_ACCESS_KEY=<service_account_secret>
AWS_DEFAULT_REGION=ru-central1
DEDUP_DB=./state/vk-worker-dedup.sqlite3
TRACE_DB=./state/vk-worker-trace.sqlite3
REVIEW_DB=./state/vk-worker-review.sqlite3
POISON_DB=./state/vk-worker-poison.sqlite3
RATE_LIMIT_DB=./state/vk-worker-rate-limit.sqlite3
VK_PUBLIC_RATE_LIMIT_COUNT=60
VK_PUBLIC_RATE_LIMIT_WINDOW_SECONDS=3600
VK_POISON_MAX_RECEIVE_COUNT=5
```

Safety defaults to verify:

```text
VK_ALLOW_ALL_USERS=false
VK_PUBLIC_HERMES_REPLY=false
VK_EMERGENCY_LOCKDOWN=false
```

If you need an emergency stop, set:

```text
VK_EMERGENCY_LOCKDOWN=true
```

## 3. Local health checks

Run before starting or restarting services:

```bash
python vm-worker/vk_hermes_worker.py --doctor
python vm-worker/vk_hermes_worker.py --health
python vm-worker/vk_hermes_worker.py --smoke --state-dir /tmp/vk-hermes-smoke-state --fixture-dir fixtures/vk
```

For queue-mode replay without external services:

```bash
python vm-worker/vk_hermes_worker.py \
  --replay fixtures/vk/message_new_owner.json fixtures/vk/message_new_unknown.json fixtures/vk/duplicate_event.json \
  --dedup-db :memory: --trace-db :memory: --review-db :memory: \
  --fake-hermes-answer "deploy smoke answer"
```

## 4. Deploy/update Yandex Cloud Function

Build and deploy the callback function with the helper:

```bash
scripts/deploy-yandex-function.sh \
  --function-name <function_name> \
  --service-account-id <service_account_id> \
  --queue-url "$QUEUE_URL"
```

Then check in VK Callback API settings that the confirmation endpoint still responds and that new `message_new` events are delivered to the queue.

## 5. Queue mode worker

Queue mode is the production-oriented mode. It keeps Hermes API private on the VM and consumes Yandex Message Queue.

Install or update the systemd service:

```bash
scripts/install-vk-hermes-worker-service.sh
sudo systemctl daemon-reload
sudo systemctl restart vk-hermes-worker
sudo systemctl status vk-hermes-worker --no-pager
journalctl -u vk-hermes-worker -n 100 --no-pager
```

After restart, verify:

```bash
python vm-worker/vk_hermes_worker.py --health
```

## 6. Long Poll mode

Long Poll mode is useful for local/hobby deployment without Yandex Callback/Queue.

One-shot check:

```bash
python vm-worker/vk_hermes_worker.py --long-poll --once
```

Continuous run:

```bash
python vm-worker/vk_hermes_worker.py --long-poll
```

Do not run queue mode and Long Poll mode against the same VK community unless you intentionally want two consumers sharing the same state stores.

## 7. Docker option

Validate compose before switching runtime:

```bash
docker compose config
docker compose up -d --build
```

State lives in the configured volume/path; make sure `DEDUP_DB`, `TRACE_DB`, `REVIEW_DB`, `POISON_DB`, and `RATE_LIMIT_DB` remain persistent across container restarts.

## 8. Post-deploy smoke in VK

From the owner VK account, send a harmless message to the community and check:

```text
!trace <trace_id>
!pending
!poison
```

Expected result:

- owner messages get a Hermes-backed VK reply;
- public/unknown messages do not call the private Hermes backend unless explicitly configured;
- trace records are created;
- review and poison commands are owner-only;
- no secrets appear in logs or command replies.

## 9. Rollback

Fast safety rollback:

```bash
VK_EMERGENCY_LOCKDOWN=true python vm-worker/vk_hermes_worker.py --doctor
sudo systemctl restart vk-hermes-worker
```

Stop the worker completely:

```bash
sudo systemctl stop vk-hermes-worker
```

Code rollback:

```bash
git fetch origin
git checkout <known_good_commit_or_tag>
make check
sudo systemctl restart vk-hermes-worker
```

Yandex function rollback depends on your Cloud Function revision policy. If no revision rollback is available, redeploy the known-good repo commit with `scripts/deploy-yandex-function.sh`.

## 10. Release acceptance

Accept the MVP deploy only when all are true:

- `make check` passes;
- `python vm-worker/vk_hermes_worker.py --doctor` is OK;
- `python vm-worker/vk_hermes_worker.py --health` is OK;
- one owner message round-trip works in VK;
- one public/unknown message is denied/handoffed without private Hermes access;
- `!trace`, `!pending`, and `!poison` work from owner and are denied to public users;
- rollback command path is known before public exposure.
