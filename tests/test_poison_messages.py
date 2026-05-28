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


def vk_event(from_id: int = 1, text: str = "boom", event_id: str = "poison-event") -> dict:
    return {
        "type": "message_new",
        "event_id": event_id,
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
    stored = store.put({"trace_id": "t1", "message_id": "m1", "receive_count": 5, "error": "bad", "payload": {"ok": True}})

    records = store.list_recent(limit=1)

    assert records == [{"id": stored["id"], "trace_id": "t1", "message_id": "m1", "receive_count": 5, "error": "bad", "payload": {"ok": True}}]
    assert store.get(stored["id"]) == records[0]


def test_readme_documents_owner_poison_commands():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "!poison" in readme
    assert "poison message #1" in readme


def test_owner_poison_command_lists_recent_poison_messages(monkeypatch, tmp_path):
    worker = load_worker()
    monkeypatch.setenv("VK_OWNER_ID", "1")
    sent: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(worker, "reply_vk", lambda peer_id, text, **kwargs: sent.append((peer_id, text, kwargs)))
    poison_store = worker.PoisonStore(tmp_path / "poison.sqlite3")
    poison_store.put({"trace_id": "vk-deadbeef", "message_id": "m1", "receive_count": 5, "error": "Hermes down", "payload": vk_event(9, "public failed")})

    worker.process_payload(
        vk_event(1, "!poison", event_id="poison-list-command"),
        worker.DedupStore(tmp_path / "dedup.sqlite3"),
        poison_store=poison_store,
    )

    assert len(sent) == 1
    assert sent[0][0] == "1"
    assert "Poison messages:" in sent[0][1]
    assert "#1" in sent[0][1]
    assert "vk-deadbeef" in sent[0][1]
    assert "Hermes down" in sent[0][1]


def test_owner_poison_command_returns_detail_by_id(monkeypatch, tmp_path):
    worker = load_worker()
    monkeypatch.setenv("VK_OWNER_ID", "1")
    sent: list[str] = []
    monkeypatch.setattr(worker, "reply_vk", lambda peer_id, text, **kwargs: sent.append(text))
    poison_store = worker.PoisonStore(tmp_path / "poison.sqlite3")
    stored = poison_store.put({"trace_id": "vk-detail", "message_id": "m9", "receive_count": 7, "error": "token=secret", "payload": vk_event(9, "payload text")})

    worker.process_payload(
        vk_event(1, f"!poison {stored['id']}", event_id="poison-detail-command"),
        worker.DedupStore(tmp_path / "dedup.sqlite3"),
        poison_store=poison_store,
    )

    assert len(sent) == 1
    assert "Poison #" in sent[0]
    assert "vk-detail" in sent[0]
    assert "message_id=m9" in sent[0]
    assert "receive_count=7" in sent[0]
    assert "secret" not in sent[0]


def test_public_poison_command_is_denied_without_store_access(monkeypatch, tmp_path):
    worker = load_worker()
    monkeypatch.setenv("VK_OWNER_ID", "1")
    monkeypatch.setenv("VK_UNAUTHORIZED_MODE", "ignore")
    monkeypatch.setattr(worker, "call_hermes", lambda vk: (_ for _ in ()).throw(AssertionError("Hermes must not be called")))
    sent: list[str] = []
    monkeypatch.setattr(worker, "reply_vk", lambda peer_id, text, **kwargs: sent.append(text))

    worker.process_payload(
        vk_event(9, "!poison", event_id="public-poison-command"),
        worker.DedupStore(tmp_path / "dedup.sqlite3"),
        poison_store=worker.PoisonStore(tmp_path / "poison.sqlite3"),
    )

    assert sent == []
