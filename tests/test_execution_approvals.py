from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
WORKER_PATH = ROOT / "vm-worker" / "vk_hermes_worker.py"


def load_worker():
    spec = importlib.util.spec_from_file_location("vk_hermes_worker", WORKER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_approval_store_is_originator_peer_ttl_and_replay_safe(tmp_path):
    worker = load_worker()
    now = [1_000.0]
    store = worker.ApprovalStore(tmp_path / "approvals.sqlite3", clock=lambda: now[0])
    record = store.create(
        run_id="run-1",
        peer_id="42",
        requester_id="42",
        command="rm -rf /tmp/example",
        description="recursive delete",
        choices=["once", "session", "deny"],
        ttl_seconds=240,
    )

    with pytest.raises(worker.ApprovalDecisionError, match="unauthorized"):
        store.claim(record["approval_id"], approver_id="99", peer_id="42", choice="once")
    with pytest.raises(worker.ApprovalDecisionError, match="peer_mismatch"):
        store.claim(record["approval_id"], approver_id="42", peer_id="99", choice="once")

    claimed = store.claim(record["approval_id"], approver_id="42", peer_id="42", choice="once")
    assert claimed["run_id"] == "run-1"
    assert claimed["status"] == "resolving"
    store.finish(record["approval_id"], status="approved", choice="once", approver_id="42")

    with pytest.raises(worker.ApprovalDecisionError, match="already_resolved"):
        store.claim(record["approval_id"], approver_id="42", peer_id="42", choice="once")

    expired = store.create(
        run_id="run-2",
        peer_id="42",
        requester_id="42",
        command="systemctl restart example",
        description="service restart",
        choices=["once", "deny"],
        ttl_seconds=10,
    )
    now[0] += 11
    with pytest.raises(worker.ApprovalDecisionError, match="expired"):
        store.claim(expired["approval_id"], approver_id="42", peer_id="42", choice="once")


def test_approval_store_rejects_owner_when_owner_is_not_request_originator(tmp_path):
    worker = load_worker()
    store = worker.ApprovalStore(tmp_path / "approvals.sqlite3", clock=lambda: 1_000.0)
    record = store.create(
        run_id="run-other-user",
        peer_id="2000000001",
        requester_id="43",
        command="dangerous-command",
        description="test",
        choices=["once", "deny"],
    )

    with pytest.raises(worker.ApprovalDecisionError, match="unauthorized"):
        store.claim(
            record["approval_id"],
            approver_id="42",
            peer_id="2000000001",
            choice="once",
        )

    claimed = store.claim(
        record["approval_id"],
        approver_id="43",
        peer_id="2000000001",
        choice="deny",
    )
    assert claimed["approver_id"] == "43"


def test_approval_store_enforces_fifo_within_run(tmp_path):
    worker = load_worker()
    store = worker.ApprovalStore(tmp_path / "approvals.sqlite3", clock=lambda: 1_000.0)
    first = store.create(run_id="run-1", peer_id="42", requester_id="42", command="a", description="a", choices=["once", "deny"])
    second = store.create(run_id="run-1", peer_id="42", requester_id="42", command="b", description="b", choices=["once", "deny"])

    with pytest.raises(worker.ApprovalDecisionError, match="not_head"):
        store.claim(second["approval_id"], approver_id="42", peer_id="42", choice="deny")

    store.claim(first["approval_id"], approver_id="42", peer_id="42", choice="deny")
    store.finish(first["approval_id"], status="denied", choice="deny", approver_id="42")
    assert store.claim(second["approval_id"], approver_id="42", peer_id="42", choice="once")["status"] == "resolving"


def test_normalize_approval_decision_supports_callback_and_text_fallback():
    worker = load_worker()
    callback = {
        "type": "message_event",
        "object": {
            "user_id": 42,
            "peer_id": 42,
            "event_id": "vk-event-1",
            "payload": {"t": "ha", "i": "approval-1", "c": "session"},
        },
    }
    assert worker.normalize_approval_decision(callback) == {
        "approval_id": "approval-1",
        "choice": "session",
        "peer_id": "42",
        "approver_id": "42",
        "event_id": "vk-event-1",
    }

    text = {
        "type": "message_new",
        "object": {"message": {"peer_id": 42, "from_id": 42, "text": "!run-deny ABC123"}},
    }
    assert worker.normalize_approval_decision(text) == {
        "approval_code": "ABC123",
        "choice": "deny",
        "peer_id": "42",
        "approver_id": "42",
        "event_id": "",
    }


def test_handle_approval_request_persists_before_sending_originator_keyboard(monkeypatch, tmp_path):
    worker = load_worker()
    monkeypatch.setenv("VK_OWNER_ID", "42")
    monkeypatch.setenv("VK_OWNER_PEER_ID", "42")
    monkeypatch.setenv("APPROVAL_DB", str(tmp_path / "approvals.sqlite3"))
    sent = []
    monkeypatch.setattr(worker, "send_vk_message", lambda peer_id, message, **kwargs: sent.append((peer_id, message, kwargs)))

    record = worker.handle_approval_request(
        {"peer_id": "2000000001", "from_id": "43"},
        "run-1",
        {
            "event": "approval.request",
            "command": "export API_TOKEN=secret-value && rm -rf /tmp/example",
            "description": "recursive delete",
            "choices": ["once", "session", "always", "deny"],
        },
        trace_id="trace-1",
    )

    assert record["status"] == "pending"
    assert "secret-value" not in record["command_preview"]
    assert len(sent) == 1
    peer_id, message, kwargs = sent[0]
    assert peer_id == "2000000001"
    assert record["peer_id"] == "2000000001"
    assert record["requester_id"] == "43"
    assert "Требуется подтверждение" in message
    keyboard = json.loads(kwargs["keyboard"])
    labels = [button["action"]["label"] for row in keyboard["buttons"] for button in row]
    assert labels == ["Разрешить один раз", "До конца запуска", "Отклонить"]
    assert "Навсегда" not in labels
    button_payloads = [button["action"]["payload"] for row in keyboard["buttons"] for button in row]
    assert all(isinstance(payload, str) for payload in button_payloads)
    decoded = [json.loads(payload) for payload in button_payloads]
    assert {payload["i"] for payload in decoded} == {record["approval_id"]}
    assert {payload["c"] for payload in decoded} == {"once", "session", "deny"}


def test_resolve_approval_payload_posts_choice_and_marks_record(tmp_path):
    worker = load_worker()
    store = worker.ApprovalStore(tmp_path / "approvals.sqlite3", clock=lambda: 1_000.0)
    record = store.create(run_id="run-1", peer_id="42", requester_id="42", command="cmd", description="reason", choices=["once", "deny"])
    calls = []

    result = worker.resolve_approval_payload(
        {
            "type": "message_event",
            "object": {
                "user_id": 42,
                "peer_id": 42,
                "event_id": "vk-event-1",
                "payload": {"t": "ha", "i": record["approval_id"], "c": "once"},
            },
        },
        store,
        post_choice=lambda run_id, choice: calls.append((run_id, choice)) or {"resolved": 1},
    )

    assert calls == [("run-1", "once")]
    assert result["status"] == "approved"
    assert store.get(record["approval_id"])["status"] == "approved"


def test_no_longer_pending_approval_becomes_stale_instead_of_retrying_forever(tmp_path):
    worker = load_worker()
    store = worker.ApprovalStore(tmp_path / "approvals.sqlite3", clock=lambda: 1_000.0)
    record = store.create(run_id="run-1", peer_id="42", requester_id="42", command="cmd", description="reason", choices=["once", "deny"])

    def no_longer_pending(_run_id, _choice):
        raise worker.HermesApprovalNotPending("approval_not_pending")

    result = worker.resolve_approval_payload(
        {
            "type": "message_event",
            "object": {"user_id": 42, "peer_id": 42, "event_id": "e1", "payload": {"t": "ha", "i": record["approval_id"], "c": "once"}},
        },
        store,
        post_choice=no_longer_pending,
    )

    assert result["status"] == "stale"
    with pytest.raises(worker.ApprovalDecisionError, match="already_resolved"):
        store.claim(record["approval_id"], approver_id="42", peer_id="42", choice="once")



def test_transport_failure_becomes_uncertain_and_is_never_retried(tmp_path):
    worker = load_worker()
    store = worker.ApprovalStore(tmp_path / "approvals.sqlite3", clock=lambda: 1_000.0)
    record = store.create(run_id="run-1", peer_id="42", requester_id="42", command="cmd", description="reason", choices=["once", "deny"])

    def timeout_after_send(_run_id, _choice):
        raise TimeoutError("response lost")

    result = worker.resolve_approval_payload(
        {"type": "message_event", "object": {"user_id": 42, "peer_id": 42, "event_id": "e1", "payload": {"t": "ha", "i": record["approval_id"], "c": "once"}}},
        store,
        post_choice=timeout_after_send,
    )

    assert result["status"] == "uncertain"
    with pytest.raises(worker.ApprovalDecisionError, match="already_resolved"):
        store.claim(record["approval_id"], approver_id="42", peer_id="42", choice="once")


def test_worker_restart_terminalizes_interrupted_resolution(tmp_path):
    worker = load_worker()
    path = tmp_path / "approvals.sqlite3"
    store = worker.ApprovalStore(path, clock=lambda: 1_000.0)
    record = store.create(run_id="run-1", peer_id="42", requester_id="42", command="cmd", description="reason", choices=["once", "deny"])
    store.claim(record["approval_id"], approver_id="42", peer_id="42", choice="once")
    store.db.close()

    restarted = worker.ApprovalStore(path, clock=lambda: 1_001.0)
    assert restarted.recover_interrupted() == 1
    assert restarted.get(record["approval_id"])["status"] == "uncertain"


def test_call_hermes_with_progress_dispatches_approval_event(monkeypatch):
    worker = load_worker()
    monkeypatch.setenv("VK_OWNER_ID", "42")
    monkeypatch.setenv("HERMES_API_KEY", "key")
    handled = []

    class FakeResponse:
        ok = True
        status_code = 200
        text = ""

        def __init__(self, payload=None, lines=None):
            self.payload = payload or {}
            self.lines = lines or []

        def json(self):
            return self.payload

        def iter_lines(self, decode_unicode=False):
            for line in self.lines:
                yield line if decode_unicode else line.encode()

    monkeypatch.setattr(worker.requests, "post", lambda *a, **k: FakeResponse({"id": "run-1"}))

    def fake_get(url, **kwargs):
        if url.endswith("/events"):
            return FakeResponse(lines=[
                'data: {"event":"approval.request","command":"rm -rf /tmp/x","description":"recursive delete","choices":["once","session","deny"]}',
                "",
            ])
        return FakeResponse({"status": "completed", "output": "done"})

    monkeypatch.setattr(worker.requests, "get", fake_get)
    monkeypatch.setattr(worker, "handle_approval_request", lambda vk, run_id, event, trace_id=None: handled.append((vk, run_id, event, trace_id)))

    answer = worker.call_hermes_with_progress({"peer_id": "42", "from_id": "42", "text": "do", "attachments": []}, trace_id="trace-1")

    assert answer == "done"
    assert handled[0][1] == "run-1"
    assert handled[0][2]["event"] == "approval.request"
