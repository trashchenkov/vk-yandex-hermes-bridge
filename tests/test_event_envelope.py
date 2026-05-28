from __future__ import annotations

import importlib.util
from pathlib import Path
import logging


ROOT = Path(__file__).resolve().parents[1]
WORKER_PATH = ROOT / "vm-worker" / "vk_hermes_worker.py"


def load_worker():
    spec = importlib.util.spec_from_file_location("vk_hermes_worker", WORKER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_build_event_envelope_contains_stable_trace_and_vk_metadata():
    worker = load_worker()
    payload = {
        "type": "message_new",
        "event_id": "evt-123",
        "group_id": 777,
        "secret": "callback-secret-should-not-be-stored",
        "object": {
            "message": {
                "id": 42,
                "conversation_message_id": 24,
                "peer_id": 254662087,
                "from_id": 254662087,
                "date": 1710000000,
                "text": "Привет",
                "attachments": [
                    {"type": "photo", "photo": {"access_key": "secret-photo-key"}},
                    {"type": "doc", "doc": {"access_key": "secret-doc-key"}},
                ],
            }
        },
    }

    envelope = worker.build_event_envelope(payload)

    assert envelope["trace_id"].startswith("vk-")
    assert envelope["trace_id"] == worker.build_event_envelope(payload)["trace_id"]
    assert envelope["event_type"] == "message_new"
    assert envelope["vk"]["group_id"] == "777"
    assert envelope["vk"]["event_id"] == "evt-123"
    assert envelope["vk"]["peer_id"] == "254662087"
    assert envelope["vk"]["from_id"] == "254662087"
    assert envelope["vk"]["message_id"] == "42"
    assert envelope["vk"]["conversation_message_id"] == "24"
    assert envelope["text"] == "Привет"
    assert envelope["attachment_types"] == ["photo", "doc"]
    assert envelope["message_timestamp"] == 1710000000
    assert envelope["raw_event_sha256"] == worker.raw_event_hash(payload)


def test_event_envelope_does_not_store_raw_secrets_or_tokens():
    worker = load_worker()
    payload = {
        "type": "message_new",
        "event_id": "evt-secret",
        "secret": "vk-callback-secret",
        "access_token": "vk-token",
        "object": {
            "message": {
                "peer_id": 1,
                "from_id": 1,
                "text": "token-like text should remain only as message text",
                "attachments": [{"type": "photo", "photo": {"access_key": "attachment-secret"}}],
            }
        },
    }

    envelope = worker.build_event_envelope(payload)
    serialized = repr(envelope)

    assert "vk-callback-secret" not in serialized
    assert "vk-token" not in serialized
    assert "attachment-secret" not in serialized
    assert "token-like text should remain only as message text" in serialized


def test_fake_event_result_includes_envelope_for_trace_debugging(monkeypatch, tmp_path):
    worker = load_worker()
    monkeypatch.setenv("VK_ALLOWED_USERS", "254662087")
    fixture = ROOT / "fixtures" / "vk" / "message_new_owner.json"

    result = worker.run_fake_event(fixture, fake_hermes_answer="answer", dedup_path=tmp_path / "dedup.sqlite3")

    assert result["envelope"]["trace_id"] == result["trace_id"]
    assert result["envelope"]["vk"]["peer_id"] == "254662087"
    assert result["envelope"]["event_type"] == "message_new"


def test_process_payload_logs_trace_role_and_policy_decision(monkeypatch, tmp_path, caplog):
    worker = load_worker()
    monkeypatch.setenv("VK_ALLOWED_USERS", "254662087")
    payload = {
        "type": "message_new",
        "event_id": "log-event-1",
        "group_id": 123456789,
        "object": {
            "message": {
                "id": 301,
                "peer_id": 254662087,
                "from_id": 254662087,
                "text": "hello",
                "attachments": [],
            }
        },
    }
    sent = []
    monkeypatch.setattr(worker, "call_hermes", lambda vk: "answer")
    monkeypatch.setattr(worker, "reply_vk", lambda peer_id, text, **kwargs: sent.append((peer_id, text, kwargs)))

    caplog.set_level(logging.INFO, logger="vk_hermes_worker")
    worker.process_payload(payload, worker.DedupStore(tmp_path / "dedup.sqlite3"))

    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert worker.trace_id_for_payload(payload) in log_text
    assert "role=owner" in log_text
    assert "decision=reply" in log_text
