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


def vk_event(from_id: int, text: str) -> dict:
    return {
        "type": "message_new",
        "event_id": f"rate-{from_id}-{text}",
        "object": {
            "message": {
                "id": abs(hash(text)) % 1_000_000,
                "peer_id": from_id,
                "from_id": from_id,
                "text": text,
                "attachments": [],
            }
        },
    }


class FakeQueueClient:
    def __init__(self, payloads: list[dict]):
        self.payloads = payloads
        self.deleted: list[str] = []

    def receive_message(self, **kwargs):
        return {
            "Messages": [
                {
                    "MessageId": f"m{i}",
                    "ReceiptHandle": f"r{i}",
                    "Body": json.dumps(payload, ensure_ascii=False),
                    "Attributes": {"ApproximateReceiveCount": "1"},
                }
                for i, payload in enumerate(self.payloads, start=1)
            ]
        }

    def delete_message(self, **kwargs):
        self.deleted.append(kwargs["ReceiptHandle"])



def test_rate_limit_store_allows_only_configured_events_per_window(monkeypatch, tmp_path):
    worker = load_worker()
    monkeypatch.setattr(worker.time, "time", lambda: 1_000.0)
    store = worker.RateLimitStore(tmp_path / "rate.sqlite3")

    assert store.allow("public:9", limit=2, window_seconds=60) is True
    assert store.allow("public:9", limit=2, window_seconds=60) is True
    assert store.allow("public:9", limit=2, window_seconds=60) is False

    monkeypatch.setattr(worker.time, "time", lambda: 1_061.0)
    assert store.allow("public:9", limit=2, window_seconds=60) is True


def test_public_handoff_is_rate_limited_before_review_queue_growth(monkeypatch, tmp_path):
    worker = load_worker()
    monkeypatch.setenv("VK_OWNER_ID", "1")
    monkeypatch.setenv("VK_PUBLIC_HANDOFF", "true")
    monkeypatch.setenv("VK_UNAUTHORIZED_MODE", "ignore")
    monkeypatch.setenv("VK_PUBLIC_RATE_LIMIT_COUNT", "1")
    monkeypatch.setenv("VK_PUBLIC_RATE_LIMIT_WINDOW_SECONDS", "3600")
    monkeypatch.setattr(worker, "call_hermes", lambda vk: (_ for _ in ()).throw(AssertionError("Hermes must not be called")))
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(worker, "reply_vk", lambda peer_id, text, **kwargs: sent.append((peer_id, text)))

    dedup = worker.DedupStore(tmp_path / "dedup.sqlite3")
    trace_store = worker.TraceStore(tmp_path / "trace.sqlite3")
    review_store = worker.ReviewStore(tmp_path / "review.sqlite3")
    rate_store = worker.RateLimitStore(tmp_path / "rate.sqlite3")

    first = vk_event(9, "first public question")
    second = vk_event(9, "second public question")

    worker.process_payload(first, dedup, trace_store=trace_store, review_store=review_store, rate_store=rate_store)
    worker.process_payload(second, dedup, trace_store=trace_store, review_store=review_store, rate_store=rate_store)

    assert len(review_store.list_pending()) == 1
    assert sent == []
    second_trace = trace_store.get(worker.trace_id_for_payload(second))
    assert second_trace is not None
    assert second_trace["role"] == "public"
    assert second_trace["decision"] == "deny"
    assert second_trace["reason"] == "public_rate_limited"
    assert second_trace["vk_status"] == "not_sent"


def test_queue_run_once_applies_public_rate_limit(monkeypatch, tmp_path):
    worker = load_worker()
    monkeypatch.setenv("VK_OWNER_ID", "1")
    monkeypatch.setenv("VK_PUBLIC_HANDOFF", "true")
    monkeypatch.setenv("VK_UNAUTHORIZED_MODE", "ignore")
    monkeypatch.setenv("VK_PUBLIC_RATE_LIMIT_COUNT", "1")
    monkeypatch.setenv("QUEUE_MAX_MESSAGES", "10")
    monkeypatch.setattr(worker, "call_hermes", lambda vk: (_ for _ in ()).throw(AssertionError("Hermes must not be called")))
    monkeypatch.setattr(worker, "reply_vk", lambda peer_id, text, **kwargs: None)
    first = vk_event(9, "queued first public question")
    second = vk_event(9, "queued second public question")
    client = FakeQueueClient([first, second])
    trace_store = worker.TraceStore(tmp_path / "trace.sqlite3")
    review_store = worker.ReviewStore(tmp_path / "review.sqlite3")

    assert worker.run_once(
        client,
        "queue-url",
        worker.DedupStore(tmp_path / "dedup.sqlite3"),
        trace_store=trace_store,
        review_store=review_store,
        rate_store=worker.RateLimitStore(tmp_path / "rate.sqlite3"),
    ) == 2

    assert client.deleted == ["r1", "r2"]
    assert len(review_store.list_pending()) == 1
    second_trace = trace_store.get(worker.trace_id_for_payload(second))
    assert second_trace["reason"] == "public_rate_limited"


def test_owner_traffic_is_not_rate_limited(monkeypatch, tmp_path):
    worker = load_worker()
    monkeypatch.setenv("VK_OWNER_ID", "1")
    monkeypatch.setenv("VK_PUBLIC_RATE_LIMIT_COUNT", "1")
    replies: list[str] = []
    monkeypatch.setattr(worker, "call_hermes", lambda vk: f"ok:{vk['text']}")
    monkeypatch.setattr(worker, "reply_vk", lambda peer_id, text, **kwargs: replies.append(text))

    dedup = worker.DedupStore(tmp_path / "dedup.sqlite3")
    rate_store = worker.RateLimitStore(tmp_path / "rate.sqlite3")

    worker.process_payload(vk_event(1, "owner one"), dedup, rate_store=rate_store)
    worker.process_payload(vk_event(1, "owner two"), dedup, rate_store=rate_store)

    assert replies == ["ok:owner one", "ok:owner two"]
