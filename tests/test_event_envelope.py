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


def test_build_event_envelope_contains_traceable_vk_metadata_without_raw_secrets():
    worker = load_worker()
    payload = {
        "type": "message_new",
        "event_id": "evt-123",
        "group_id": 987,
        "secret": "callback-secret-value",
        "object": {
            "message": {
                "id": 42,
                "conversation_message_id": 24,
                "peer_id": 254662087,
                "from_id": 254662087,
                "date": 1710000000,
                "text": "Привет",
                "attachments": [{"type": "photo", "photo": {"access_key": "secret-photo-key"}}],
            }
        },
    }

    envelope = worker.build_event_envelope(payload)

    assert envelope["trace_id"].startswith("vk-")
    assert envelope["event_type"] == "message_new"
    assert envelope["vk"]["peer_id"] == "254662087"
    assert envelope["vk"]["from_id"] == "254662087"
    assert envelope["vk"]["message_id"] == "42"
    assert envelope["text"] == "Привет"
    assert envelope["attachments"] == [{"type": "photo"}]
    assert envelope["raw_event_sha256"] == worker.event_fingerprint(payload)
    assert "callback-secret-value" not in str(envelope)
    assert "secret-photo-key" not in str(envelope)


def test_trace_id_is_stable_for_duplicate_vk_event():
    worker = load_worker()
    fixture = ROOT / "fixtures" / "vk" / "message_new_owner.json"
    payload = worker.json.loads(fixture.read_text(encoding="utf-8"))

    first = worker.build_event_envelope(payload)
    second = worker.build_event_envelope(payload)

    assert first["trace_id"] == second["trace_id"]
    assert first["raw_event_sha256"] == second["raw_event_sha256"]
