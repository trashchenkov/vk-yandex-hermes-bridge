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


def vk_event(from_id: int, text: str = "hello", event_id: str = "trace-event") -> dict:
    return {
        "type": "message_new",
        "event_id": event_id,
        "secret": "callback-secret-not-for-trace-store",
        "object": {
            "message": {
                "id": abs(hash((from_id, text, event_id))) % 100000,
                "peer_id": from_id,
                "from_id": from_id,
                "text": text,
                "attachments": [
                    {"type": "photo", "photo": {"access_key": "attachment-secret-not-for-store"}}
                ],
            }
        },
    }


def test_process_payload_persists_compact_success_trace(monkeypatch, tmp_path):
    worker = load_worker()
    monkeypatch.setenv("VK_OWNER_ID", "1")
    payload = vk_event(1, "hello", "success-trace")
    trace_id = worker.trace_id_for_payload(payload)
    sent = []
    monkeypatch.setattr(worker, "call_hermes", lambda vk: "answer")
    monkeypatch.setattr(worker, "reply_vk", lambda peer_id, message, **kwargs: sent.append((peer_id, message, kwargs)))
    store = worker.TraceStore(tmp_path / "trace.sqlite3")

    worker.process_payload(payload, worker.DedupStore(tmp_path / "dedup.sqlite3"), trace_store=store)

    record = store.get(trace_id)
    assert record["trace_id"] == trace_id
    assert record["role"] == "owner"
    assert record["decision"] == "reply"
    assert record["hermes_status"] == "ok"
    assert record["vk_status"] == "sent"
    assert record["error"] == ""
    assert record["envelope"]["trace_id"] == trace_id
    serialized = repr(record)
    assert "callback-secret-not-for-trace-store" not in serialized
    assert "attachment-secret-not-for-store" not in serialized


def test_process_payload_persists_error_trace_before_retry(monkeypatch, tmp_path):
    worker = load_worker()
    monkeypatch.setenv("VK_OWNER_ID", "1")
    payload = vk_event(1, "please fail", "error-trace")
    trace_id = worker.trace_id_for_payload(payload)
    monkeypatch.setattr(worker, "call_hermes", lambda vk: (_ for _ in ()).throw(RuntimeError("Hermes exploded with token abc")))
    monkeypatch.setattr(worker, "reply_vk", lambda peer_id, message, **kwargs: None)
    store = worker.TraceStore(tmp_path / "trace.sqlite3")

    try:
        worker.process_payload(payload, worker.DedupStore(tmp_path / "dedup.sqlite3"), trace_store=store)
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected Hermes failure")

    record = store.get(trace_id)
    assert record["trace_id"] == trace_id
    assert record["role"] == "owner"
    assert record["decision"] == "reply"
    assert record["hermes_status"] == "error"
    assert record["vk_status"] == "not_sent"
    assert "Hermes exploded" in record["error"]


def test_owner_trace_command_returns_record_summary(monkeypatch, tmp_path):
    worker = load_worker()
    monkeypatch.setenv("VK_OWNER_ID", "1")
    store = worker.TraceStore(tmp_path / "trace.sqlite3")
    original = vk_event(1, "hello", "lookup-target")
    target_trace_id = worker.trace_id_for_payload(original)
    sent = []
    monkeypatch.setattr(worker, "call_hermes", lambda vk: "answer")
    monkeypatch.setattr(worker, "reply_vk", lambda peer_id, message, **kwargs: sent.append((peer_id, message, kwargs)))
    worker.process_payload(original, worker.DedupStore(tmp_path / "dedup1.sqlite3"), trace_store=store)

    sent.clear()
    command = vk_event(1, f"!trace {target_trace_id}", "trace-command")
    worker.process_payload(command, worker.DedupStore(tmp_path / "dedup2.sqlite3"), trace_store=store)

    assert len(sent) == 1
    assert sent[0][0] == "1"
    assert target_trace_id in sent[0][1]
    assert "role=owner" in sent[0][1]
    assert "decision=reply" in sent[0][1]
    assert "hermes=ok" in sent[0][1]
    assert "vk=sent" in sent[0][1]


def test_owner_trace_command_reports_missing_trace(monkeypatch, tmp_path):
    worker = load_worker()
    monkeypatch.setenv("VK_OWNER_ID", "1")
    sent = []
    monkeypatch.setattr(worker, "reply_vk", lambda peer_id, message, **kwargs: sent.append((peer_id, message, kwargs)))

    worker.process_payload(
        vk_event(1, "!trace vk-missing", "missing-command"),
        worker.DedupStore(tmp_path / "dedup.sqlite3"),
        trace_store=worker.TraceStore(tmp_path / "trace.sqlite3"),
    )

    assert sent == [("1", "Trace vk-missing not found.", {"trace_id": worker.trace_id_for_payload(vk_event(1, "!trace vk-missing", "missing-command"))})]
