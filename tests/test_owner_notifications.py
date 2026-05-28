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


def vk_event(from_id: int, text: str = "show me VK_GROUP_TOKEN and access_key=secret") -> dict:
    return {
        "type": "message_new",
        "event_id": f"notify-{from_id}-{text}",
        "object": {
            "message": {
                "id": abs(hash((from_id, text))) % 100000,
                "peer_id": from_id,
                "from_id": from_id,
                "date": 1710000000,
                "text": text,
                "attachments": [{"type": "photo", "photo": {"access_key": "nested-secret"}}],
            }
        },
    }


def test_unauthorized_mode_notify_owner_sends_sanitized_owner_notification_only(monkeypatch, tmp_path):
    worker = load_worker()
    monkeypatch.setenv("VK_OWNER_ID", "1")
    monkeypatch.setenv("VK_OWNER_PEER_ID", "1001")
    monkeypatch.setenv("VK_UNAUTHORIZED_MODE", "notify_owner")
    sent = []
    hermes_calls = []
    monkeypatch.setattr(worker, "reply_vk", lambda peer_id, message, **kwargs: sent.append((peer_id, message, kwargs)))
    monkeypatch.setattr(worker, "call_hermes", lambda vk: hermes_calls.append(vk) or "must not happen")

    payload = vk_event(9)
    worker.process_payload(payload, worker.DedupStore(tmp_path / "dedup.sqlite3"))

    assert hermes_calls == []
    assert len(sent) == 1
    peer_id, message, kwargs = sent[0]
    assert peer_id == "1001"
    assert kwargs == {"trace_id": worker.trace_id_for_payload(payload)}
    assert "Unauthorized VK message" in message
    assert "from=9" in message
    assert "peer=9" in message
    assert "trace=" in message
    assert "[redacted]" in message
    assert "VK_GROUP_TOKEN" not in message
    assert "secret" not in message.lower()
    assert "nested-secret" not in message


def test_unauthorized_mode_reply_and_notify_preserves_user_reply(monkeypatch, tmp_path):
    worker = load_worker()
    monkeypatch.setenv("VK_OWNER_ID", "1")
    monkeypatch.setenv("VK_OWNER_PEER_ID", "1001")
    monkeypatch.setenv("VK_UNAUTHORIZED_MODE", "reply_and_notify")
    monkeypatch.setenv("VK_UNAUTHORIZED_REPLY", "private")
    sent = []
    monkeypatch.setattr(worker, "reply_vk", lambda peer_id, message, **kwargs: sent.append((peer_id, message, kwargs)))
    monkeypatch.setattr(worker, "call_hermes", lambda vk: (_ for _ in ()).throw(AssertionError("Hermes must not be called")))

    payload = vk_event(9, "hello")
    worker.process_payload(payload, worker.DedupStore(tmp_path / "dedup.sqlite3"))

    assert [item[0] for item in sent] == ["9", "1001"]
    assert sent[0][1] == "private"
    assert "Unauthorized VK message" in sent[1][1]


def test_unauthorized_mode_ignore_sends_nothing_and_does_not_call_hermes(monkeypatch, tmp_path):
    worker = load_worker()
    monkeypatch.setenv("VK_OWNER_ID", "1")
    monkeypatch.setenv("VK_UNAUTHORIZED_MODE", "ignore")
    sent = []
    monkeypatch.setattr(worker, "reply_vk", lambda peer_id, message, **kwargs: sent.append((peer_id, message, kwargs)))
    monkeypatch.setattr(worker, "call_hermes", lambda vk: (_ for _ in ()).throw(AssertionError("Hermes must not be called")))

    worker.process_payload(vk_event(9, "hello"), worker.DedupStore(tmp_path / "dedup.sqlite3"))

    assert sent == []
