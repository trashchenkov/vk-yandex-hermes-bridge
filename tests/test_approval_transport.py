from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKER_PATH = ROOT / "vm-worker" / "vk_hermes_worker.py"
FUNCTION_DIR = ROOT / "yandex-vk-hermes-function"


def load_worker():
    spec = importlib.util.spec_from_file_location("vk_hermes_worker", WORKER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeQueue:
    def __init__(self, payload: dict):
        self.payload = payload
        self.deleted = []

    def receive_message(self, **_kwargs):
        return {
            "Messages": [
                {
                    "MessageId": "m1",
                    "ReceiptHandle": "receipt-1",
                    "Body": json.dumps({"payload": self.payload}),
                }
            ]
        }

    def delete_message(self, **kwargs):
        self.deleted.append(kwargs["ReceiptHandle"])


def test_approval_worker_resolves_callback_on_independent_queue(tmp_path):
    worker = load_worker()
    store = worker.ApprovalStore(tmp_path / "approvals.sqlite3", clock=lambda: 1_000.0)
    record = store.create(run_id="run-1", peer_id="42", requester_id="42", command="cmd", description="reason", choices=["once", "deny"])
    payload = {
        "type": "message_event",
        "object": {
            "user_id": 42,
            "peer_id": 42,
            "event_id": "vk-event-1",
            "payload": {"t": "ha", "i": record["approval_id"], "c": "once"},
        },
    }
    queue = FakeQueue(payload)
    posted = []
    notified = []

    count = worker.run_approval_once(
        queue,
        "approval-queue",
        store,
        post_choice=lambda run_id, choice: posted.append((run_id, choice)) or {"resolved": 1},
        notify_func=lambda event, result, error=None: notified.append((event, result, error)),
    )

    assert count == 1
    assert posted == [("run-1", "once")]
    assert queue.deleted == ["receipt-1"]
    assert notified[0][1]["status"] == "approved"


def test_callback_function_routes_only_approval_events_to_approval_queue():
    script = r"""
const bridge = require('./index.js')._test;
const normal = {type:'message_new', object:{message:{peer_id:42,from_id:42,text:'hello'}}};
const command = {type:'message_new', object:{message:{peer_id:42,from_id:42,text:'!run-allow ABC123'}}};
const callback = {type:'message_event', object:{peer_id:42,user_id:42,payload:{t:'ha',i:'id',c:'once'}}};
console.log(JSON.stringify([
  bridge.queueUrlForPayload(normal),
  bridge.queueUrlForPayload(command),
  bridge.queueUrlForPayload(callback)
]));
"""
    env = os.environ.copy()
    env.update({"QUEUE_URL": "normal-queue", "APPROVAL_QUEUE_URL": "approval-queue"})
    result = subprocess.run(
        ["node", "-e", script], cwd=FUNCTION_DIR, env=env, text=True, capture_output=True
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == ["normal-queue", "approval-queue", "approval-queue"]


def test_callback_function_uses_event_id_for_fifo_deduplication():
    script = r"""
const bridge = require('./index.js')._test;
const base = {type:'message_event', group_id:7, object:{peer_id:42,user_id:42,conversation_message_id:99,payload:{t:'ha',i:'id',c:'once'}}};
const first = JSON.parse(JSON.stringify(base)); first.object.event_id = 'event-1';
const second = JSON.parse(JSON.stringify(base)); second.object.event_id = 'event-2';
const otherChoice = JSON.parse(JSON.stringify(base)); otherChoice.object.event_id = 'event-3'; otherChoice.object.payload.c = 'deny';
console.log(JSON.stringify([bridge.queueDedupId(first), bridge.queueDedupId(second), bridge.queueDedupId(otherChoice)]));
"""
    completed = subprocess.run(
        ["node", "-e", script],
        cwd=ROOT / "yandex-vk-hermes-function",
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "QUEUE_URL": "normal", "APPROVAL_QUEUE_URL": "approval"},
    )
    ids = json.loads(completed.stdout)
    assert len(set(ids)) == 3


def test_systemd_installer_defines_separate_approval_worker():
    text = (ROOT / "scripts" / "install-vk-hermes-worker-service.sh").read_text(encoding="utf-8")
    assert "vk-hermes-approval-worker.service" in text
    assert "--approval-worker" in text
    assert "APPROVAL_QUEUE_URL" in text


def test_deploy_helper_passes_approval_queue_without_printing_value():
    text = (ROOT / "scripts" / "deploy-yandex-function.sh").read_text(encoding="utf-8")
    assert "APPROVAL_QUEUE_URL" in text
    assert "APPROVAL_QUEUE_URL=***" in text
