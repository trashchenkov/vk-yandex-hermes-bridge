from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKER_PATH = ROOT / "vm-worker" / "vk_hermes_worker.py"


def load_worker():
    spec = importlib.util.spec_from_file_location("vk_hermes_worker", WORKER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def vk_event(from_id: int, text: str = "public question") -> dict:
    return {
        "type": "message_new",
        "event_id": f"review-{from_id}-{text}",
        "object": {
            "message": {
                "id": abs(hash((from_id, text))) % 100000,
                "peer_id": from_id,
                "from_id": from_id,
                "text": text,
                "attachments": [],
            }
        },
    }


def test_review_store_creates_lists_and_updates_items(tmp_path):
    worker = load_worker()
    store = worker.ReviewStore(tmp_path / "review.sqlite3")

    item = store.create_item(
        kind="public_question",
        trace_id="vk-trace",
        peer_id="9",
        from_id="9",
        text="question",
        status="pending",
    )

    pending = store.list_pending()
    assert len(pending) == 1
    assert pending[0]["id"] == item["id"]
    assert pending[0]["kind"] == "public_question"
    assert pending[0]["trace_id"] == "vk-trace"
    assert pending[0]["status"] == "pending"

    updated = store.update_status(item["id"], "rejected")
    assert updated["status"] == "rejected"
    assert store.list_pending() == []


def test_public_handoff_creates_review_item_without_hermes(monkeypatch, tmp_path):
    worker = load_worker()
    monkeypatch.setenv("VK_OWNER_ID", "1")
    monkeypatch.setenv("VK_PUBLIC_HANDOFF", "true")
    payload = vk_event(9, "можно ли подключить Hermes к VK?")
    review_store = worker.ReviewStore(tmp_path / "review.sqlite3")
    hermes_calls = []
    sent = []
    monkeypatch.setattr(worker, "call_hermes", lambda vk: hermes_calls.append(vk) or "should not happen")
    monkeypatch.setattr(worker, "reply_vk", lambda peer_id, message, **kwargs: sent.append((peer_id, message, kwargs)))

    worker.process_payload(payload, worker.DedupStore(tmp_path / "dedup.sqlite3"), review_store=review_store)

    assert hermes_calls == []
    pending = review_store.list_pending()
    assert len(pending) == 1
    assert pending[0]["kind"] == "public_question"
    assert pending[0]["trace_id"] == worker.trace_id_for_payload(payload)
    assert pending[0]["text"] == "можно ли подключить Hermes к VK?"
    assert sent


def test_pending_approve_and_reject_owner_commands_use_review_store(monkeypatch, tmp_path):
    worker = load_worker()
    monkeypatch.setenv("VK_OWNER_ID", "1")
    review_store = worker.ReviewStore(tmp_path / "review.sqlite3")
    item = review_store.create_item(
        kind="public_question",
        trace_id="vk-trace",
        peer_id="9",
        from_id="9",
        text="question",
    )

    assert "#1 public_question" in worker.handle_owner_command(
        worker.normalize_vk_message(vk_event(1, "!pending")),
        worker.decide_policy(worker.normalize_vk_message(vk_event(1, "!pending"))),
        trace_store=None,
        review_store=review_store,
    )
    assert worker.handle_owner_command(
        worker.normalize_vk_message(vk_event(1, f"!approve {item['id']}")),
        worker.decide_policy(worker.normalize_vk_message(vk_event(1, f"!approve {item['id']}"))),
        trace_store=None,
        review_store=review_store,
    ) == f"Review item #{item['id']} approved."
    assert review_store.get(item["id"])["status"] == "approved"

    second = review_store.create_item(kind="public_question", trace_id="vk-2", peer_id="9", from_id="9", text="another")
    assert worker.handle_owner_command(
        worker.normalize_vk_message(vk_event(1, f"!reject {second['id']}")),
        worker.decide_policy(worker.normalize_vk_message(vk_event(1, f"!reject {second['id']}"))),
        trace_store=None,
        review_store=review_store,
    ) == f"Review item #{second['id']} rejected."
    assert review_store.get(second["id"])["status"] == "rejected"
