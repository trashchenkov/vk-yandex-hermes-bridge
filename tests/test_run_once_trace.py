from __future__ import annotations

import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKER_PATH = ROOT / "vm-worker" / "vk_hermes_worker.py"


def load_worker():
    spec = importlib.util.spec_from_file_location("vk_hermes_worker", WORKER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeQueueClient:
    def __init__(self, payload: dict):
        self.payload = payload
        self.deleted = []

    def receive_message(self, **kwargs):
        return {"Messages": [{"ReceiptHandle": "r1", "Body": json.dumps(self.payload, ensure_ascii=False)}]}

    def delete_message(self, **kwargs):
        self.deleted.append(kwargs["ReceiptHandle"])


def vk_event(from_id: int, text: str = "hello", event_id: str = "run-once-trace") -> dict:
    return {
        "type": "message_new",
        "event_id": event_id,
        "object": {
            "message": {
                "id": 1001,
                "peer_id": from_id,
                "from_id": from_id,
                "text": text,
                "attachments": [],
            }
        },
    }


def test_run_once_passes_trace_store_to_processing(monkeypatch, tmp_path):
    worker = load_worker()
    monkeypatch.setenv("VK_OWNER_ID", "1")
    payload = vk_event(1, "hello")
    trace_id = worker.trace_id_for_payload(payload)
    monkeypatch.setattr(worker, "call_hermes", lambda vk: "answer")
    monkeypatch.setattr(worker, "reply_vk", lambda peer_id, message, **kwargs: None)
    trace_store = worker.TraceStore(tmp_path / "trace.sqlite3")
    client = FakeQueueClient(payload)

    assert worker.run_once(client, "queue-url", worker.DedupStore(tmp_path / "dedup.sqlite3"), trace_store=trace_store) == 1

    assert client.deleted == ["r1"]
    assert trace_store.get(trace_id)["hermes_status"] == "ok"
