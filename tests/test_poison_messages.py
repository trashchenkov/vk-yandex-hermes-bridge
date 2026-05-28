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
    def __init__(self, payload: dict, *, receive_count: str = "1"):
        self.payload = payload
        self.receive_count = receive_count
        self.deleted: list[str] = []
        self.receive_kwargs: dict = {}

    def receive_message(self, **kwargs):
        self.receive_kwargs = kwargs
        return {
            "Messages": [
                {
                    "MessageId": "m1",
                    "ReceiptHandle": "r1",
                    "Body": json.dumps(self.payload, ensure_ascii=False),
                    "Attributes": {"ApproximateReceiveCount": self.receive_count},
                }
            ]
        }

    def delete_message(self, **kwargs):
        self.deleted.append(kwargs["ReceiptHandle"])


def vk_event(from_id: int = 1, text: str = "boom") -> dict:
    return {
        "type": "message_new",
        "event_id": "poison-event",
        "object": {
            "message": {
                "id": 501,
                "peer_id": from_id,
                "from_id": from_id,
                "text": text,
                "attachments": [],
            }
        },
    }


def test_run_once_leaves_failed_message_for_retry_below_poison_threshold(monkeypatch, tmp_path):
    worker = load_worker()
    monkeypatch.setenv("VK_OWNER_ID", "1")
    monkeypatch.setenv("VK_POISON_MAX_RECEIVE_COUNT", "3")
    monkeypatch.setattr(worker, "call_hermes", lambda vk: (_ for _ in ()).throw(RuntimeError("Hermes down")))
    client = FakeQueueClient(vk_event(), receive_count="2")
    poison_store = worker.PoisonStore(tmp_path / "poison.sqlite3")

    assert worker.run_once(
        client,
        "queue-url",
        worker.DedupStore(tmp_path / "dedup.sqlite3"),
        trace_store=worker.TraceStore(tmp_path / "trace.sqlite3"),
        poison_store=poison_store,
    ) == 1

    assert client.deleted == []
    assert poison_store.list_recent() == []
    assert "AttributeNames" in client.receive_kwargs
    assert "ApproximateReceiveCount" in client.receive_kwargs["AttributeNames"]


def test_run_once_moves_repeated_failure_to_poison_store_and_deletes_queue_message(monkeypatch, tmp_path):
    worker = load_worker()
    monkeypatch.setenv("VK_OWNER_ID", "1")
    monkeypatch.setenv("VK_POISON_MAX_RECEIVE_COUNT", "3")
    monkeypatch.setattr(worker, "call_hermes", lambda vk: (_ for _ in ()).throw(RuntimeError("Hermes down token=secret")))
    client = FakeQueueClient(vk_event(), receive_count="3")
    poison_store = worker.PoisonStore(tmp_path / "poison.sqlite3")

    assert worker.run_once(
        client,
        "queue-url",
        worker.DedupStore(tmp_path / "dedup.sqlite3"),
        trace_store=worker.TraceStore(tmp_path / "trace.sqlite3"),
        poison_store=poison_store,
    ) == 1

    assert client.deleted == ["r1"]
    records = poison_store.list_recent()
    assert len(records) == 1
    record = records[0]
    assert record["message_id"] == "m1"
    assert record["receive_count"] == 3
    assert record["trace_id"] == worker.trace_id_for_payload(vk_event())
    assert "Hermes down" in record["error"]
    assert "secret" not in json.dumps(record, ensure_ascii=False)
    assert record["payload"]["type"] == "message_new"


def test_poison_store_roundtrips_recent_records(tmp_path):
    worker = load_worker()
    store = worker.PoisonStore(tmp_path / "poison.sqlite3")
    store.put({"trace_id": "t1", "message_id": "m1", "receive_count": 5, "error": "bad", "payload": {"ok": True}})

    records = store.list_recent(limit=1)

    assert records == [{"trace_id": "t1", "message_id": "m1", "receive_count": 5, "error": "bad", "payload": {"ok": True}}]
