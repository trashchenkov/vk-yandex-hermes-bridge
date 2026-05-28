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


def vk_event(from_id: int, text: str = "hello") -> dict:
    return {
        "type": "message_new",
        "event_id": f"shadow-{from_id}-{text}",
        "object": {
            "message": {
                "id": abs(hash((from_id, text))) % 100000,
                "peer_id": from_id,
                "from_id": from_id,
                "date": 1710000000,
                "text": text,
                "attachments": [],
            }
        },
    }


def test_shadow_mode_calls_hermes_but_sends_only_owner_report(monkeypatch, tmp_path):
    worker = load_worker()
    monkeypatch.setenv("VK_OWNER_ID", "1")
    monkeypatch.setenv("VK_OWNER_PEER_ID", "1001")
    monkeypatch.setenv("VK_TRUSTED_USERS", "9")
    monkeypatch.setenv("VK_SHADOW_MODE", "true")
    sent = []
    hermes_calls = []
    monkeypatch.setattr(worker, "call_hermes", lambda vk: hermes_calls.append(vk) or "proposed token=SECRET answer")
    monkeypatch.setattr(worker, "reply_vk", lambda peer_id, message, **kwargs: sent.append((peer_id, message, kwargs)))

    payload = vk_event(9, "question")
    trace_store = worker.TraceStore(tmp_path / "trace.sqlite3")
    worker.process_payload(payload, worker.DedupStore(tmp_path / "dedup.sqlite3"), trace_store=trace_store)

    assert len(hermes_calls) == 1
    assert hermes_calls[0]["from_id"] == "9"
    assert len(sent) == 1
    peer_id, message, kwargs = sent[0]
    assert peer_id == "1001"
    assert kwargs == {"trace_id": worker.trace_id_for_payload(payload)}
    assert "Shadow VK answer" in message
    assert "from=9" in message
    assert "peer=9" in message
    assert "role=trusted" in message
    assert "proposed=" in message
    assert "SECRET" not in message
    assert "[redacted]" in message

    record = trace_store.get(worker.trace_id_for_payload(payload))
    assert record["hermes_status"] == "ok"
    assert record["vk_status"] == "shadow_not_sent"
    assert record["decision"] == "reply"


def test_shadow_mode_without_owner_peer_does_not_reply_original_user(monkeypatch, tmp_path):
    worker = load_worker()
    monkeypatch.setenv("VK_OWNER_ID", "1")
    monkeypatch.setenv("VK_TRUSTED_USERS", "9")
    monkeypatch.setenv("VK_SHADOW_MODE", "true")
    monkeypatch.delenv("VK_OWNER_PEER_ID", raising=False)
    sent = []
    monkeypatch.setattr(worker, "call_hermes", lambda vk: "proposed answer")
    monkeypatch.setattr(worker, "reply_vk", lambda peer_id, message, **kwargs: sent.append((peer_id, message, kwargs)))

    # fallback owner notification target is owner id, never the original trusted user's peer
    worker.process_payload(vk_event(9, "question"), worker.DedupStore(tmp_path / "dedup.sqlite3"))

    assert [item[0] for item in sent] == ["1"]
